"""Durable delivery/recovery and real authenticated feature APIs, offline only."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import persona_store as store
import persona_features as features
import test_accounts
import test_backend
from account_runtime import use_runtime


class DurableTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_backend.BackendBase.setUp
    asyncTearDown = test_backend.BackendBase.asyncTearDown
    create = test_backend.BackendBase.create
    row = test_backend.BackendBase.row

    async def test_log_long_unicode_payload_remains_valid_json(self):
        b = self.b
        b['add_hit']('custom', False, detail={'message': '中文"\\\n' * 10000, 'items': [1, 2]})
        raw = b['conn'].execute('SELECT detail FROM hits').fetchone()[0]
        result = json.loads(raw)
        self.assertEqual(result['items'], [1, 2])
        self.assertLessEqual(len(result['message']), 12000)

    async def test_bubbles_dedupe_logical_reply_per_chat_and_preserve_unknown(self):
        b = self.b
        b['_bubble_pause'] = AsyncMock()
        b['_send_generated_message'] = AsyncMock(return_value=test_backend.message(10, out=True))
        text = '这是一条足够长的第一句话。还有一条足够长的第二句话。'
        _, count = await b['_send_bubbled_reply']('one', 'one', text, ratio=1, dedupe=True)
        self.assertEqual(count, 2)
        self.assertEqual((await b['_send_bubbled_reply']('one', 'one', text, ratio=0, dedupe=True))[1], 0)
        self.assertEqual((await b['_send_bubbled_reply']('two', 'two', text, ratio=0, dedupe=True))[1], 1)
        b['_send_generated_message'].side_effect = TimeoutError('unknown')
        with self.assertRaises(TimeoutError):
            await b['_send_bubbled_reply']('three', 'three', text, dedupe=True)
        self.assertEqual((await b['_send_bubbled_reply']('three', 'three', text, dedupe=True))[1], 0)
        self.assertEqual(b['conn'].execute("SELECT status FROM reply_deliveries WHERE chat_id='three'").fetchone()[0], 'unknown')

    async def test_failed_send_clears_intent_without_suppressing_other_chats(self):
        b = self.b
        b['client'].send_message.side_effect = test_backend.FloodWait(5)
        with self.assertRaises(test_backend.FloodWait):
            await b['_send_generated_message'](123, 'hello')
        self.assertFalse(b['_sent_recently'](123, 'hello'))
        self.assertFalse(b['_is_generated_message'](456, text='hello'))
        self.assertEqual(b['conn'].execute('SELECT COUNT(*) FROM generated_messages').fetchone()[0], 0)

    async def test_timeout_and_cancellation_keep_generated_intent_for_both_senders(self):
        b = self.b
        from telethon import errors
        b['errors'] = errors
        for error in (TimeoutError('unknown'), asyncio.CancelledError(), errors.TimedOutError(None, 'TIMEOUT'),
                      errors.ServerError(None, 'INTERNAL')):
            for event_send in (False, True):
                b['conn'].execute('DELETE FROM generated_messages')
                sender = AsyncMock(side_effect=error)
                b['client'].send_message = sender
                event = SimpleNamespace(chat_id=123, reply=sender)
                with self.assertRaises(type(error)):
                    if event_send:
                        await b['_event_generated_message'](event, 'reply', 'hello')
                    else:
                        await b['_send_generated_message'](123, 'hello')
                self.assertTrue(b['_is_generated_message'](123, text='hello'))
                self.assertFalse(b['_is_generated_message'](456, text='hello'))
                self.assertEqual(b['conn'].execute("SELECT kind FROM generated_messages LIMIT 1").fetchone()[0], 'intent')

    async def test_telegram_timeout_keeps_workflow_pending_and_dedupes_bubbles(self):
        b = self.b
        from telethon import errors
        b['errors'] = errors
        b['client'].send_message.side_effect = errors.TimedOutError(None, 'TIMEOUT')
        aid = await self.create(type='custom', custom_steps='[{"t":"send","text":"/start"}]')
        self.assertFalse(await b['run_custom'](self.row(aid)))
        self.assertEqual(store.load_checkpoint(b['conn'], aid)['state'], 'pending')
        with self.assertRaises(errors.TimedOutError):
            await b['_send_bubbled_reply'](123, 123, 'hello', dedupe=True)
        self.assertEqual((await b['_send_bubbled_reply'](123, 123, 'hello', dedupe=True))[1], 0)
        self.assertEqual(b['client'].send_message.await_count, 2)

    async def test_existing_measurements_migrate_without_losing_history(self):
        db = self.b['conn']
        store.measure(db, {}, 'summary', 10, True, {}, {})
        db.execute('ALTER TABLE ai_measurements DROP COLUMN task_kind')
        store.init(db)
        self.assertEqual(db.execute('SELECT task_kind FROM ai_measurements').fetchone()[0], 'automation')
        with store.scope(task_id=1, task_kind='summary'):
            store.measure(db, {}, 'summary', 20, True, {}, {})
        self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_measurements').fetchone()[0], 2)

    async def test_owner_pause_stops_remaining_bubbles(self):
        b = self.b
        b['_bubble_pause'] = AsyncMock()
        b['_flock_owner_paused'] = lambda chat: b['_send_generated_message'].await_count > 0
        b['_send_generated_message'] = AsyncMock(return_value=test_backend.message(10, out=True))
        _, count = await b['_send_bubbled_reply']('one', 'one', '这是一条足够长的第一句话。还有一条足够长的第二句话。', kind='ai_flock', ratio=1, dedupe=True)
        self.assertEqual(count, 1)

    async def test_delayed_deletion_survives_reinitialization_and_retries(self):
        b = self.b
        aid = await self.create(type='keepalive', reply_text='hello', cooldown=10, poll_interval=3600)
        self.assertTrue(await b['run_keepalive'](self.row(aid)))
        store.init(b['conn'])
        b['conn'].execute('UPDATE pending_deletions SET due=0')
        b['client'].delete_messages = AsyncMock(side_effect=ConnectionError('offline'))
        await b['_delete_pending_messages']()
        self.assertEqual(b['conn'].execute('SELECT attempts FROM pending_deletions').fetchone()[0], 1)
        b['client'].delete_messages.side_effect = None
        b['conn'].execute('UPDATE pending_deletions SET due=0')
        await b['_delete_pending_messages']()
        self.assertEqual(b['conn'].execute('SELECT COUNT(*) FROM pending_deletions').fetchone()[0], 0)

    async def test_workflow_retries_wait_without_resending_completed_command(self):
        b = self.b
        aid = await self.create(type='custom', custom_steps=json.dumps([
            {'t':'send', 'text':'/start'}, {'t':'wait_reply', 'success':'done', 'timeout_ms':1}]))
        with patch.object(time, 'monotonic', side_effect=[0, 2]):
            self.assertFalse(await b['run_custom'](self.row(aid)))
        b['client'].get_messages.side_effect = [[test_backend.message(101, 'done')], [test_backend.message(101, 'done')]]
        self.assertTrue(await b['run_custom'](self.row(aid)))
        self.assertEqual(b['client'].send_message.await_count, 1)
        self.assertEqual(b['conn'].execute('SELECT state FROM workflow_checkpoints').fetchone()[0], 'complete')

    async def test_workflow_reconciles_timeout_against_actual_outgoing_message(self):
        b = self.b
        aid = await self.create(type='custom', custom_steps=json.dumps([{'t':'send', 'text':'/start'}]))
        b['client'].send_message.side_effect = TimeoutError('unknown')
        self.assertFalse(await b['run_custom'](self.row(aid)))
        b['client'].get_messages.return_value = [test_backend.message(101, '/start', out=True)]
        self.assertTrue(await b['run_custom'](self.row(aid)))
        self.assertEqual(b['client'].send_message.await_count, 1)

    async def test_reconciled_final_step_is_complete_and_query_failure_keeps_intent(self):
        b = self.b
        aid = await self.create(type='custom', custom_steps=json.dumps([{'t':'send', 'text':'/start'}]))
        b['client'].send_message.side_effect = TimeoutError('unknown')
        self.assertFalse(await b['run_custom'](self.row(aid)))
        b['client'].get_messages.side_effect = test_backend.FloodWait(5)
        with self.assertRaises(b['_RetryLater']):
            await b['run_custom'](self.row(aid))
        self.assertEqual(store.load_checkpoint(b['conn'], aid)['state'], 'pending')
        b['client'].get_messages.side_effect = None
        b['client'].get_messages.return_value = [test_backend.message(101, '/start', out=True)]
        self.assertTrue(await b['run_custom'](self.row(aid)))
        self.assertEqual(store.load_checkpoint(b['conn'], aid)['state'], 'complete')
        self.assertEqual(b['client'].send_message.await_count, 1)

    async def test_deleted_task_cannot_leave_checkpoint_for_reused_id(self):
        aid = await self.create(type='custom', custom_steps='[{"t":"send","text":"/start"}]')
        store.save_checkpoint(self.b['conn'], aid, 'x', 0, 'pending', {})
        await self.b['delete_auto'](aid)
        self.assertIsNone(store.load_checkpoint(self.b['conn'], aid))

    async def test_restart_finishes_complete_schedule_without_queuing_duplicate(self):
        b = self.b
        aid = await self.create(type='custom', enabled=0,
            custom_trigger='{"mode":"schedule","at":"2026-10-30T10:00:00"}',
            custom_steps='[{"t":"send","text":"/start"}]')
        a = self.row(aid)
        signature = store.digest(json.dumps([b['parse_steps'](a['custom_steps']), a['target']], ensure_ascii=False))
        store.save_checkpoint(b['conn'], aid, signature, 1, 'complete', {})
        b['conn'].execute("UPDATE automations SET last_status='interrupted',enabled=1 WHERE id=?", (aid,))
        queued = []
        b['_queue_auto'] = lambda task_id, **kwargs: queued.append(task_id)
        await b['_recover_workflows']()
        self.assertEqual(queued, [])
        self.assertEqual((self.row(aid)['last_status'], self.row(aid)['enabled']), ('success', 0))
        b['conn'].execute("UPDATE automations SET last_status='interrupted',enabled=1 WHERE id=?", (aid,))
        store.save_checkpoint(b['conn'], aid, signature, 0, 'pending', {})
        await b['_recover_workflows']()
        self.assertEqual(queued, [aid])

    async def test_profile_controls_actual_request_and_measurement_context(self):
        b = self.b
        cfg = {'id':'fixture', 'base':'https://example.invalid/v1', 'key':'fixture', 'model':'m', 'reasoning_effort':'minimal'}
        profile = {'reasoning_parameter':'reasoning_effort', 'reasoning_levels':['minimal'],
                   'token_parameter':'max_completion_tokens', 'max_output_tokens':128, 'vision':False}
        b['conn'].execute('INSERT INTO provider_profiles VALUES(?,?,?)', ('fixture', json.dumps(profile), 0))
        response = SimpleNamespace(status_code=200, json=lambda: {'choices':[{'message':{'content':'OK'}}], 'usage':{'prompt_tokens':5, 'completion_tokens':2}})
        client = SimpleNamespace(post=AsyncMock(return_value=response))
        manager = AsyncMock()
        manager.__aenter__.return_value = client
        b['httpx'] = SimpleNamespace(AsyncClient=lambda **kw: manager)
        with store.scope(task_id=42, chat_id='123'):
            self.assertEqual(await b['_call_ai_observed'](cfg, 'hello', None, False, 'summary'), 'OK')
        payload = client.post.call_args.kwargs['json']
        self.assertEqual(payload['max_completion_tokens'], 128)
        self.assertEqual(payload['reasoning_effort'], 'minimal')
        self.assertNotIn('max_tokens', payload)
        measurement = b['conn'].execute('SELECT * FROM ai_measurements').fetchone()
        self.assertEqual((measurement['task_id'], measurement['chat_id'], measurement['input_tokens'], measurement['ok']), (42, '123', 5, 1))
        with self.assertRaisesRegex(ValueError, '图片输入'):
            await b['_call_ai_observed'](cfg, [{'type':'image_url'}], 32, False, 'image')
        self.assertEqual(client.post.await_count, 1)

    async def test_provider_timeout_marks_health_and_records_failed_measurement(self):
        b = self.b
        cfg = {'id':'a'*16, 'model':'m'}
        async def timeout(*args):
            cfg['_provider_timeout'] = True
            raise asyncio.CancelledError()
        b['_call_ai_request'] = timeout
        with patch.dict(b, _record_ai_health=unittest.mock.Mock()) as namespace:
            with self.assertRaises(asyncio.CancelledError):
                await b['_call_ai_observed'](cfg, 'hello', 32, False, 'ai_flock')
            namespace['_record_ai_health'].assert_called_once()
        self.assertEqual(b['conn'].execute('SELECT ok FROM ai_measurements').fetchone()[0], 0)

    async def test_checkpoint_redacts_and_restores_credentials(self):
        b = self.b
        store.save_checkpoint(b['conn'], 1, 'signature', 1, 'ready',
            {'context': {'credential': {'secret':'private'}, 'var': {'answer':'user=alice;code=private'}}}, {'u':'alice', 's':'private'})
        raw = b['conn'].execute('SELECT context FROM workflow_checkpoints').fetchone()[0]
        self.assertNotIn('private', raw)
        self.assertNotIn('alice', raw)
        restored = store.load_checkpoint(b['conn'], 1, {'u':'alice', 's':'private'})
        self.assertEqual(restored['context']['context']['var']['answer'], 'user=alice;code=private')

    async def test_negation_and_numbers_create_conflicts_without_changing_verified_knowledge(self):
        b = self.b
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'knowledge-auto.md'
            b['get_runtime']().knowledge_auto_file = path
            old = '这个服务支持每个账号绑定3个设备，需要在面板设置里操作。'
            path.write_text('<!-- tg-persona-auto:{"created_at":1,"verified":1} -->\n'+old+'\n<!-- /tg-persona-auto -->', encoding='utf-8')
            for new in ['这个服务不支持每个账号绑定3个设备，需要在面板设置里操作。', '这个服务支持每个账号绑定6个设备，需要在面板设置里操作。']:
                b['_append_knowledge_auto']([new], 'chat', [1])
            self.assertEqual(b['conn'].execute('SELECT COUNT(*) FROM memory_conflicts').fetchone()[0], 2)
            self.assertIn(old, path.read_text(encoding='utf-8'))
            self.assertIn('"verified":1', path.read_text(encoding='utf-8'))

    async def test_similar_items_within_one_learning_batch_keep_complete_updated_sentence(self):
        with tempfile.TemporaryDirectory() as folder:
            self.b['get_runtime']().knowledge_auto_file = Path(folder) / 'knowledge-auto.md'
            old = '我遇到版本问题会先核实发行渠道和版本号，再决定是否重新安装。'
            new = '我遇到版本相关问题时，会先核实发行渠道和版本号，再判断是否需要重新安装。'
            self.b['_append_knowledge_auto']([old, new], 'chat')
            entries = self.b['_knowledge_auto_entries'](self.b['_read_knowledge_auto']())
            self.assertEqual([entry['body'] for entry in entries], [new])


class FeatureAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_accounts.AccountsTest.asyncSetUp
    asyncTearDown = test_accounts.AccountsTest.asyncTearDown
    load_main = test_accounts.AccountsTest.load_main
    req = test_accounts.AccountsTest.req
    create = test_accounts.AccountsTest.create
    rt = test_accounts.AccountsTest.rt
    auto = test_accounts.AccountsTest.auto

    async def checkpoint_fixture(self):
        response = await self.req('POST', '/api/autos', json={'type':'custom', 'name':'recover', 'target':'@dummy_bot',
            'custom_steps':'[{"t":"send","text":"/start"}]', 'enabled':0})
        self.assertEqual(response.status_code, 200, response.text)
        aid = response.json()['id']
        row = self.rt().db.execute('SELECT * FROM automations WHERE id=?', (aid,)).fetchone()
        signature = store.digest(json.dumps([self.main.parse_steps(row['custom_steps']), row['target']], ensure_ascii=False))
        store.save_checkpoint(self.rt().db, aid, signature, 0, 'pending',
            {'watermarks':{'@dummy_bot':0}, 'baselines':{'@dummy_bot':None}, 'intent':{'kind':'send','dest':'@dummy_bot','min_id':0,'digest':store.digest('/start')}})
        return aid, store.load_checkpoint(self.rt().db, aid)['updated']

    async def test_recovery_final_step_completes_and_stale_operation_is_rejected(self):
        rt = self.rt()
        aid, updated = await self.checkpoint_fixture()
        rt.client = SimpleNamespace(get_messages=AsyncMock(return_value=[SimpleNamespace(id=1, out=True, raw_text='/start')]))
        rt.tg_state['phase'] = 'ready'
        with patch.object(self.main, 'resolve_entity', AsyncMock(return_value='entity')):
            response = await self.req('POST', '/api/persona/recovery/'+str(aid), json={'action':'completed','updated':updated})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(store.load_checkpoint(rt.db, aid)['state'], 'complete')
        self.assertEqual(rt.db.execute('SELECT last_status FROM automations WHERE id=?',(aid,)).fetchone()[0], 'success')
        self.assertEqual((await self.req('POST', '/api/persona/recovery/'+str(aid), json={'action':'retry','updated':updated})).status_code, 409)
        rt.client = None

    async def test_recovery_does_not_overwrite_checkpoint_changed_during_lookup(self):
        rt = self.rt()
        aid, updated = await self.checkpoint_fixture()
        async def lookup(*args, **kwargs):
            rt.db.execute('UPDATE workflow_checkpoints SET updated=updated+1 WHERE task_id=?',(aid,))
            rt.db.commit()
            return []
        rt.client = SimpleNamespace(get_messages=lookup)
        rt.tg_state['phase'] = 'ready'
        with patch.object(self.main, 'resolve_entity', AsyncMock(return_value='entity')):
            response = await self.req('POST', '/api/persona/recovery/'+str(aid), json={'action':'completed','updated':updated})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(store.load_checkpoint(rt.db, aid)['state'], 'pending')
        rt.client = None

    async def test_summary_config_change_during_generation_prevents_send(self):
        rt = self.rt()
        rt.db.execute("INSERT INTO watched_groups VALUES('123','fixture',0)")
        rt.db.commit()
        await self.req('POST','/api/persona/summaries',json={'name':'daily','groups':['123']})
        job = dict(rt.db.execute('SELECT * FROM summary_jobs').fetchone())
        msg = SimpleNamespace(id=2, date=datetime.now(timezone.utc), raw_text='新版本发布', out=False, get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)))
        rt.client = SimpleNamespace(get_messages=AsyncMock(return_value=[msg]))
        async def generate(*args, **kwargs):
            rt.db.execute('UPDATE summary_jobs SET updated=updated+1')
            rt.db.commit()
            return '新版本发布。'
        with use_runtime(rt), patch.object(self.main,'resolve_entity',AsyncMock(return_value='entity')), \
                patch.object(self.main,'_call_ai_any',generate), patch.object(self.main,'_send_generated_message',AsyncMock()) as send:
            chat, day = features.reserve_summaries(vars(self.main),job,manual=True)[0]
            await features.run_summary(vars(self.main),job,chat,day)
            send.assert_not_awaited()
        self.assertEqual(rt.db.execute('SELECT status FROM summary_runs').fetchone()[0],'cancelled')
        rt.client = None

    async def test_auth_and_account_isolation_for_histories(self):
        other = await self.create()
        run = store.start_run(self.rt().db, 1, name='private-run')
        store.event(self.rt().db, 'test', {'value':42}, run)
        self.assertEqual((await self.req('GET', '/api/persona/runs')).json()['items'][0]['id'], run)
        self.assertEqual((await self.req('GET', '/api/persona/runs', other)).json()['items'], [])
        self.assertEqual((await self.req('GET', '/api/persona/runs/'+run, other)).status_code, 404)
        self.http.cookies.clear()
        self.assertEqual((await self.req('GET', '/api/persona/runs')).status_code, 401)

    async def test_memory_diff_restore_requires_current_digest(self):
        with use_runtime(self.rt()):
            self.main._write_study('first version')
            self.main._write_study('second version')
        versions = (await self.req('GET', '/api/persona/memory/versions?file=study.md')).json()
        selected = versions['items'][1]['id']
        detail = (await self.req('GET', '/api/persona/memory/versions/'+str(selected))).json()
        self.assertIn('-second version', detail['diff'])
        with use_runtime(self.rt()):
            self.main._write_study('concurrent change')
        url = '/api/persona/memory/versions/'+str(selected)+'/restore'
        self.assertEqual((await self.req('POST', url, json={'digest':detail['digest']})).status_code, 409)
        detail = (await self.req('GET', '/api/persona/memory/versions/'+str(selected))).json()
        self.assertEqual((await self.req('POST', url, json={'digest':detail['digest']})).status_code, 200)
        self.assertEqual(self.rt().study_file.read_text(encoding='utf-8'), 'first version')

    async def test_conflict_resolution_is_versioned_and_requires_reverification(self):
        old, new = '允许使用旧版本', '不允许使用旧版本'
        with use_runtime(self.rt()):
            self.main._write_knowledge_auto('<!-- tg-persona-auto:{"verified":1} -->\n'+old+'\n<!-- /tg-persona-auto -->')
            store.conflict(self.rt().db, old, new, {'chat_id':'123'})
        cid = (await self.req('GET', '/api/persona/memory/conflicts')).json()['items'][0]['id']
        self.assertEqual((await self.req('POST', '/api/persona/memory/conflicts/'+str(cid), json={'choice':'new'})).status_code, 200)
        content = self.rt().knowledge_auto_file.read_text(encoding='utf-8')
        self.assertIn(new, content)
        self.assertIn('"verified": 0', content)
        self.assertGreaterEqual(self.rt().db.execute('SELECT COUNT(*) FROM memory_versions').fetchone()[0], 3)

    async def test_profile_validation_and_cost_preserve_currency_and_unknown_usage(self):
        cfg = {'id':'a'*16, 'note':'test', 'base':'https://example.invalid/v1', 'key':'secret', 'model':'m', 'enabled':True}
        with patch.object(self.main, '_ai_provider_rows', return_value=[cfg]):
            url = '/api/persona/profiles/'+cfg['id']
            self.assertEqual((await self.req('PUT', url, json={'reasoning_parameter':'shell'})).status_code, 422)
            self.assertEqual((await self.req('PUT', url, json={'reasoning_levels':['minimal'], 'input_price':2, 'output_price':6, 'cached_price':1, 'currency':'USD'})).status_code, 200)
        profile = store.profile(self.rt().db, cfg['id'])
        store.measure(self.rt().db, cfg, 'summary', 100, True, {'prompt_tokens':1000, 'completion_tokens':100, 'prompt_tokens_details':{'cached_tokens':200}}, profile)
        store.measure(self.rt().db, cfg, 'summary', 200, False, {}, profile)
        item = (await self.req('GET', '/api/persona/metrics')).json()['items'][0]
        self.assertEqual((item['calls'], item['successful'], item['unknown_usage'], item['unpriced']), (2, 1, 1, 1))
        self.assertAlmostEqual(item['cost'], .0024)
        self.assertEqual(item['currency'], 'USD')

    async def test_metrics_keep_same_numbered_tasks_in_separate_groups(self):
        for kind in ('automation', 'ai_flock', 'summary'):
            with store.scope(task_id=1, task_kind=kind):
                store.measure(self.rt().db, {}, 'summary', 100, True, {}, {})
        items = (await self.req('GET', '/api/persona/metrics?group=task')).json()['items']
        self.assertEqual({item['label'] for item in items}, {'automation:1', 'ai_flock:1', 'summary:1'})
        self.assertTrue(all(item['calls'] == 1 for item in items))

    async def test_cancelled_summary_loop_finishes_all_reserved_groups(self):
        rt = self.rt()
        rt.db.executemany('INSERT INTO watched_groups VALUES(?,?,0)', [('123', 'one'), ('456', 'two')])
        rt.db.commit()
        response = await self.req('POST', '/api/persona/summaries', json={'name':'daily','groups':['123','456']})
        jid = response.json()['id']
        rt.db.execute("UPDATE summary_jobs SET enabled=1,at='00:00' WHERE id=?", (jid,))
        rt.db.commit()
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def get_messages(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                started.set()
            await release.wait()
            return []
        rt.client = SimpleNamespace(get_messages=get_messages)
        rt.tg_state['phase'] = 'ready'
        with use_runtime(rt), patch.object(self.main, 'resolve_entity', AsyncMock(return_value='entity')):
            worker = asyncio.create_task(features.summary_loop(vars(self.main)))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                rt.client = None
        rows = list(rt.db.execute('SELECT status FROM summary_runs'))
        self.assertEqual([row[0] for row in rows], ['failed', 'failed'])

    async def test_summary_defaults_disabled_and_daily_reservation_is_account_scoped(self):
        rt = self.rt()
        rt.db.execute("INSERT INTO watched_groups(chat_id,name,added_at) VALUES('123','fixture',0)")
        rt.db.commit()
        response = await self.req('POST', '/api/persona/summaries', json={'name':'daily', 'groups':['123']})
        self.assertEqual(response.status_code, 200, response.text)
        job = dict(rt.db.execute('SELECT * FROM summary_jobs').fetchone())
        self.assertEqual(job['enabled'], 0)
        with use_runtime(rt):
            self.assertEqual(features.reserve_summaries(vars(self.main), job), [])
            self.assertEqual(len(features.reserve_summaries(vars(self.main), job, manual=True)), 1)
            self.assertEqual(features.reserve_summaries(vars(self.main), job, manual=True), [])
        other = await self.create()
        self.assertEqual((await self.req('GET', '/api/persona/summaries', other)).json()['items'], [])
        self.assertEqual((await self.req('POST', '/api/persona/summaries', other, json={'name':'bad', 'groups':['123']})).status_code, 400)

    async def test_summary_sends_to_saved_messages_and_records_unknown_delivery(self):
        rt = self.rt()
        rt.db.execute("INSERT INTO watched_groups(chat_id,name,added_at) VALUES('123','fixture',0)")
        rt.db.commit()
        msg = SimpleNamespace(id=5, date=datetime.now(timezone.utc), raw_text='新版本今天发布', out=False, get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)))
        rt.client = SimpleNamespace(get_messages=AsyncMock(return_value=[msg]))
        rt.tg_state['phase'] = 'ready'
        job_input = {'name':'daily','groups':['123'],'topics':'版本发布'}
        await self.req('POST', '/api/persona/summaries', json=job_input)
        job = dict(rt.db.execute('SELECT * FROM summary_jobs').fetchone())
        with use_runtime(rt), patch.object(self.main, 'resolve_entity', AsyncMock(return_value='entity')), \
                patch.object(self.main, '_call_ai_any', AsyncMock(return_value='今天发布了新版本。')), \
                patch.object(self.main, '_send_generated_message', AsyncMock(side_effect=TimeoutError('unknown'))) as send:
            chat, day = features.reserve_summaries(vars(self.main), job, manual=True)[0]
            await features.run_summary(vars(self.main), job, chat, day)
            self.assertEqual(send.call_args.args[0], 'me')
            self.assertEqual(rt.db.execute('SELECT status FROM summary_runs').fetchone()[0], 'unknown')
            self.assertEqual(features.reserve_summaries(vars(self.main), job, manual=True), [])
        rt.client = None

    async def test_historical_replay_compares_same_snapshot_without_telegram_send(self):
        rt = self.rt()
        aid = await self.auto('default', enabled=0)
        buttons = [[SimpleNamespace(text='Join', url='https://t.me/fake_bot?start=service-30-Register_code')]]
        msg = SimpleNamespace(id=4, raw_text='launch', get_buttons=AsyncMock(return_value=buttons))
        rt.client = SimpleNamespace(get_messages=AsyncMock(return_value=[msg]), send_message=AsyncMock())
        rt.tg_state['phase'] = 'ready'
        with patch.object(self.main, 'resolve_entity', AsyncMock(return_value='entity')):
            response = await self.req('POST', '/api/persona/replays/capture/'+str(aid))
        self.assertEqual(response.status_code, 200, response.text)
        sid = response.json()['id']
        rt.db.execute("UPDATE automations SET btn_text='Join' WHERE id=?", (aid,)); rt.db.commit()
        response = await self.req('POST', '/api/persona/replays/'+str(sid)+'/compare')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('btn_text', response.json()['config_changes'])
        self.assertEqual(response.json()['after']['sources'][0]['messages'][0]['script_match'], 'Join')
        rt.client.send_message.assert_not_awaited()
        self.assertEqual(rt.client.get_messages.await_count, 1)
        rt.client = None


if __name__ == '__main__':
    unittest.main()
