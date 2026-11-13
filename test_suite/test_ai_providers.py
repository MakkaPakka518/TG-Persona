"""多 AI API：迁移、密钥隔离、轮询、故障切换和 Token 统计。"""
import unittest
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_backend import load_backend, HTTPException


class AIProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_backend()

    async def asyncTearDown(self):
        self.b['conn'].close()

    def body(self, **changes):
        values = dict(note='主线路', base='https://example.invalid/v1', key='secret-one',
                      model='model-one', enabled=True)
        values.update(changes)
        return self.b['AIProviderInput'](**values)

    def test_legacy_config_migrates_once_and_keys_never_leave_public_shape(self):
        for key, value in {'ai_base':'https://legacy.invalid/v1','ai_key':'legacy-secret','ai_model':'legacy-model'}.items():
            self.b['set_setting'](key, value)
        rows = self.b['_ai_provider_rows']()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['note'], '原有配置')
        public = self.b['_public_ai_provider'](rows[0])
        self.assertTrue(public['key_present'])
        self.assertNotIn('key', public)
        self.assertNotIn('legacy-secret', str(public))
        self.b['_save_ai_providers']([])
        self.assertEqual(self.b['_ai_provider_rows'](), [])
        cfg = self.b['_test_ai_settings'](SimpleNamespace(
            ai_base=None, ai_key=None, ai_model=None, ai_enabled=None, ocr_enabled=None))[0]
        self.assertEqual(cfg, {'_use_all': True})

    async def test_crud_preserves_key_usage_and_supports_notes(self):
        created = await self.b['create_ai_provider'](self.body())
        provider_id = created['provider']['id']
        self.assertEqual(created['provider']['note'], '主线路')
        self.assertNotIn('secret-one', str(created))
        self.b['_record_ai_usage'](provider_id, {'prompt_tokens': 12, 'completion_tokens': 7})
        updated = await self.b['update_ai_provider'](provider_id, self.body(
            note='备用线路', key='', model='model-two', enabled=False))
        self.assertEqual((updated['provider']['input_tokens'], updated['provider']['output_tokens']), (12, 7))
        row = self.b['_ai_provider_rows']()[0]
        self.assertEqual(row['key'], 'secret-one')
        self.assertEqual((row['note'], row['model'], row['enabled']), ('备用线路', 'model-two', False))
        listed = await self.b['list_ai_providers']()
        self.assertEqual(listed['enabled_count'], 0)
        self.assertNotIn('secret-one', str(listed))
        self.assertTrue((await self.b['delete_ai_provider'](provider_id))['ok'])
        with self.assertRaises(HTTPException):
            await self.b['delete_ai_provider'](provider_id)

    async def test_reasoning_effort_persists_defaults_and_validates(self):
        created = await self.b['create_ai_provider'](self.body(reasoning_effort='xhigh'))
        provider_id = created['provider']['id']
        self.assertEqual(created['provider']['reasoning_effort'], 'xhigh')
        self.assertEqual(self.b['_ai_provider_rows']()[0]['reasoning_effort'], 'xhigh')
        legacy = {'id': 'a' * 16, 'note': '旧配置', 'base': 'https://example.invalid/v1',
                  'key': 'k', 'model': 'm', 'enabled': True}
        self.b['set_setting']('ai_providers', json.dumps([legacy]))
        self.assertEqual(self.b['_ai_provider_rows']()[0]['reasoning_effort'], 'default')
        self.b['_save_ai_providers']([])
        for effort in self.b['AI_REASONING_EFFORTS']:
            self.assertTrue((await self.b['create_ai_provider'](self.body(reasoning_effort=effort)))['ok'])
        self.b['_save_ai_providers']([])
        with self.assertRaises(HTTPException):
            await self.b['create_ai_provider'](self.body(reasoning_effort='turbo'))
        with self.assertRaises(HTTPException):
            await self.b['create_ai_provider'](self.body(model='glm-5.3', reasoning_effort='medium'))
        kept = (await self.b['create_ai_provider'](self.body(reasoning_effort='max')))['provider']['id']
        self.assertEqual((await self.b['update_ai_provider'](kept, self.body(
            note='改名', key='', reasoning_effort='max', enabled=False)))['provider']['reasoning_effort'], 'max')

    async def test_client_profile_session_is_stable_and_resets_only_on_switch(self):
        created = await self.b['create_ai_provider'](self.body(client_profile='opencode'))
        provider = created['provider']
        self.assertEqual(provider['client_profile'], 'opencode')
        row = self.b['_ai_provider_rows']()[0]
        sid = row['client_session_id']
        self.assertRegex(sid, r'^ses_[a-f0-9]{64}$')
        self.assertNotIn(sid, str(provider))
        await self.b['update_ai_provider'](provider['id'], self.body(client_profile='opencode', note='改名', key=''))
        self.assertEqual(self.b['_ai_provider_rows']()[0]['client_session_id'], sid)
        await self.b['update_ai_provider'](provider['id'], self.body(client_profile='codex', key=''))
        codex_sid = self.b['_ai_provider_rows']()[0]['client_session_id']
        self.assertRegex(codex_sid, r'^[a-f0-9-]{36}$')
        self.assertNotEqual(codex_sid, sid)
        await self.b['update_ai_provider'](provider['id'], self.body(client_profile='none', key=''))
        row = self.b['_ai_provider_rows']()[0]
        self.assertEqual((row['client_profile'], row['client_session_id']), ('none', ''))
        with self.assertRaises(HTTPException):
            await self.b['create_ai_provider'](self.body(client_profile='cursor'))

    async def test_round_robin_selected_pool_and_failure_fallback(self):
        first = (await self.b['create_ai_provider'](self.body(note='一号')))['provider']['id']
        second = (await self.b['create_ai_provider'](self.body(note='二号', key='secret-two', model='model-two')))['provider']['id']
        calls = []
        async def fake(cfg, *_args, **_kwargs):
            calls.append(cfg['id'])
            if cfg['id'] == first:
                raise RuntimeError('first failed secret-one')
            return 'ok'
        self.b['_call_ai'] = fake
        self.assertEqual(await self.b['_call_ai_any']('hello', provider_ids=first+','+second), 'ok')
        self.assertEqual(calls, [first, second])
        calls.clear()
        self.assertEqual(await self.b['_call_ai_any']('hello', provider_ids=first+','+second), 'ok')
        self.assertEqual(calls, [second])
        calls.clear()
        self.assertEqual(await self.b['_call_ai_any']('hello', provider_ids=second), 'ok')
        self.assertEqual(calls, [second])

    async def test_flock_slow_and_invalid_provider_fallback_only_within_selected_pool(self):
        first = (await self.b['create_ai_provider'](self.body(note='慢线路')))['provider']['id']
        second = (await self.b['create_ai_provider'](self.body(note='备用')))['provider']['id']
        await self.b['create_ai_provider'](self.body(note='未选中'))
        self.b['AI_FLOCK_PROVIDER_TIMEOUT'] = .01
        self.b['AI_FLOCK_TOTAL_TIMEOUT'] = .2
        calls = []
        async def slow(cfg, *_args):
            calls.append(cfg['id'])
            if cfg['id'] == first:
                await asyncio.Event().wait()
            return '正常回复'
        self.b['_call_ai'] = slow
        trace = {}
        reply = await self.b['_call_ai_any']('聊聊天', provider_ids=first+','+second, feature='ai_flock',
                                            validate=self.b['_flock_reply'], trace=trace)
        self.assertEqual(reply, '正常回复')
        self.assertEqual(calls, [first, second])
        self.assertIn('超时', trace['failures'][0])
        self.b['_ai_rr'].clear()
        self.b['get_runtime']().ai_flock_provider_retry_at.clear()
        calls.clear()
        async def invalid(cfg, *_args):
            calls.append(cfg['id'])
            return '长' * 100 if cfg['id'] == first else '简短\n回复'
        self.b['_call_ai'] = invalid
        self.assertEqual(await self.b['_call_ai_any']('聊聊天', provider_ids=first+','+second,
            feature='ai_flock', validate=self.b['_flock_reply']), '简短 回复')
        self.assertEqual(calls, [first, second])
        calls.clear()
        with self.assertRaisesRegex(RuntimeError, '没有可切换的备用线路'):
            await self.b['_call_ai_any']('聊聊天', provider_ids=first, feature='ai_flock', validate=self.b['_flock_reply'])
        self.assertEqual(calls, [first])

    async def test_flock_reserves_time_for_healthy_provider_after_two_stalled_providers(self):
        ids = [(await self.b['create_ai_provider'](self.body(note=note)))['provider']['id']
               for note in ('不出正文一', '不出正文二', '正常线路')]
        self.b['AI_FLOCK_PROVIDER_TIMEOUT'] = .08
        self.b['AI_FLOCK_TOTAL_TIMEOUT'] = .09
        calls, cancelled = [], []
        async def request(cfg, *_args):
            calls.append(cfg['id'])
            if cfg['id'] != ids[-1]:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(cfg['id'])
            return '备用线路回复'
        self.b['_call_ai'] = request
        trace = {}
        answer = await self.b['_call_ai_any']('聊天', provider_ids=','.join(ids), feature='ai_flock', trace=trace)
        self.assertEqual(answer, '备用线路回复')
        self.assertEqual(calls, ids)
        self.assertEqual(cancelled, ids[:2])
        self.assertEqual(len(trace['failures']), 2)

    async def test_recently_failed_flock_provider_is_deprioritized_then_can_recover(self):
        bad = (await self.b['create_ai_provider'](self.body(note='故障')))['provider']['id']
        good = (await self.b['create_ai_provider'](self.body(note='正常')))['provider']['id']
        calls = []
        async def request(cfg, *_args):
            calls.append(cfg['id'])
            return '' if cfg['id'] == bad else '正常回复'
        self.b['_call_ai'] = request
        for _ in range(3):
            self.assertEqual(await self.b['_call_ai_any']('聊天', provider_ids=bad+','+good,
                feature='ai_flock', validate=self.b['_flock_reply']), '正常回复')
        self.assertEqual(calls, [bad, good, good, good])
        self.b['get_runtime']().ai_flock_provider_retry_at[bad] = 0
        self.b['_ai_rr'].clear()
        self.b['_call_ai'] = AsyncMock(return_value='恢复后的回复')
        self.assertEqual(await self.b['_call_ai_any']('聊天', provider_ids=bad+','+good,
            feature='ai_flock', validate=self.b['_flock_reply']), '恢复后的回复')
        self.assertEqual(self.b['_call_ai'].await_args.args[0]['id'], bad)
        self.assertNotIn(bad, self.b['get_runtime']().ai_flock_provider_retry_at)

    async def test_flock_explicit_no_reply_does_not_force_provider_fallback(self):
        first = (await self.b['create_ai_provider'](self.body(note='主线路')))['provider']['id']
        second = (await self.b['create_ai_provider'](self.body(note='备用')))['provider']['id']
        self.b['_call_ai'] = AsyncMock(return_value='NO_REPLY')
        result = await self.b['_call_ai_any']('聊聊天', provider_ids=first+','+second, feature='ai_flock', validate=self.b['_flock_reply'])
        self.assertEqual(result, 'NO_REPLY')
        self.b['_call_ai'].assert_awaited_once()

    async def test_all_fail_error_is_redacted_and_provider_limit_validates(self):
        await self.b['create_ai_provider'](self.body())
        async def fail(cfg, *_args, **_kwargs):
            raise RuntimeError('Bearer '+cfg['key'])
        self.b['_call_ai'] = fail
        with self.assertRaisesRegex(RuntimeError, '主线路') as caught:
            await self.b['_call_ai_any']('hello')
        self.assertNotIn('secret-one', str(caught.exception))
        with self.assertRaises(HTTPException):
            await self.b['create_ai_provider'](self.body(base='not-a-url'))

    async def test_usage_accepts_openai_and_input_output_shapes(self):
        provider_id = (await self.b['create_ai_provider'](self.body()))['provider']['id']
        self.b['_record_ai_usage'](provider_id, {'prompt_tokens': 3, 'completion_tokens': 4})
        self.b['_record_ai_usage'](provider_id, {'input_tokens': 5, 'output_tokens': 6})
        self.b['_record_ai_usage'](provider_id, {'prompt_tokens': 'invalid'})
        row = self.b['_ai_provider_rows']()[0]
        self.assertEqual((row['input_tokens'], row['output_tokens']), (8, 10))

    async def test_delete_provider_cleans_every_reference_and_disables_only_orphaned_features(self):
        provider=(await self.b['create_ai_provider'](self.body()))['provider']['id']
        backup=(await self.b['create_ai_provider'](self.body(note='备用',key='two')))['provider']['id']
        db=self.b['conn'];db.executemany("INSERT INTO watched_groups VALUES(?,?,0)",[('-1','一群'),('-2','二群')]);db.commit()
        only=(await self.b['create_ai_flock_job'](self.b['AIFlockJobInput'](name='单线路',groups='-1',prompt='聊',provider_ids=provider)))['id']
        mixed=(await self.b['create_ai_flock_job'](self.b['AIFlockJobInput'](name='多线路',groups='-2',prompt='聊',provider_ids=provider+','+backup)))['id']
        reg_only=(await self.b['create_auto'](self.b['AutoIn'](type='link_register',name='注册单线路',btn_text='注册',keywords='状态',success_kw='成功',ai_flock_enabled=1,ai_flock_prompt='聊天',ai_flock_provider_ids=provider)))['id']
        reg_mixed=(await self.b['create_auto'](self.b['AutoIn'](type='link_register',name='注册多线路',btn_text='注册',keywords='状态',success_kw='成功',ai_flock_enabled=1,ai_flock_prompt='聊天',ai_flock_provider_ids=provider+','+backup)))['id']
        signin=(await self.b['create_auto'](self.b['AutoIn'](type='signin',name='图片签到',target='@dummy_bot',command='/sign',ai_provider_ids=provider,signin_recognition_mode='image',signin_image_prompt='识别图片')))['id']
        db.executemany("INSERT INTO ai_flock_sessions(chat_id,source_task_id,expires_at,prompt,provider_ids) VALUES(?,?,9999999999,'x',?)",[('-10',reg_only,provider),('-11',reg_mixed,provider+','+backup)])
        db.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",[
            ('flock_learning_provider_ids',provider),('flock_learning_enabled','1'),('flock_learning_unlimited','1'),
            ('flock_deep_learning_enabled','1'),('flock_skill_distill_enabled','1'),
            ('flock_owner_provider_ids',provider),('flock_summary_enabled','1')]);db.commit()
        with self.assertRaises(HTTPException) as disabled:await self.b['update_ai_provider'](provider,self.body(key='',enabled=False))
        self.assertEqual(disabled.exception.status_code,409)
        deleted=await self.b['delete_ai_provider'](provider)
        self.assertTrue(deleted['ok']);self.assertEqual(deleted['affected_references'],7);self.assertGreaterEqual(deleted['configurations_changed'],9)
        self.assertEqual([row['id'] for row in self.b['_ai_provider_rows']()],[backup])
        self.assertEqual(tuple(db.execute('SELECT provider_ids,enabled FROM ai_flock_jobs WHERE id=?',(only,)).fetchone()),('',0))
        self.assertEqual(tuple(db.execute('SELECT provider_ids,enabled FROM ai_flock_jobs WHERE id=?',(mixed,)).fetchone()),(backup,1))
        self.assertEqual(tuple(db.execute('SELECT ai_flock_provider_ids,ai_flock_enabled,enabled FROM automations WHERE id=?',(reg_only,)).fetchone()),('',0,1))
        self.assertEqual(tuple(db.execute('SELECT ai_flock_provider_ids,ai_flock_enabled FROM automations WHERE id=?',(reg_mixed,)).fetchone()),(backup,1))
        self.assertEqual(tuple(db.execute('SELECT ai_provider_ids,signin_recognition_mode,signin_captcha,enabled FROM automations WHERE id=?',(signin,)).fetchone()),('', 'none',0,1))
        self.assertIsNone(db.execute("SELECT 1 FROM ai_flock_sessions WHERE chat_id='-10'").fetchone())
        self.assertEqual(db.execute("SELECT provider_ids FROM ai_flock_sessions WHERE chat_id='-11'").fetchone()[0],backup)
        self.assertEqual(tuple(self.b['get_setting'](key) for key in ('flock_learning_provider_ids','flock_learning_enabled','flock_learning_unlimited','flock_deep_learning_enabled','flock_skill_distill_enabled')),('','0','0','0','0'))
        self.assertEqual((self.b['get_setting']('flock_owner_provider_ids'),self.b['get_setting']('flock_summary_enabled')),('','0'))

    async def test_delete_last_provider_disables_features_that_used_all_providers(self):
        provider=(await self.b['create_ai_provider'](self.body()))['provider']['id'];db=self.b['conn']
        db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit()
        job=(await self.b['create_ai_flock_job'](self.b['AIFlockJobInput'](name='全部线路',groups='-1',prompt='聊',provider_ids='')))['id']
        registration=(await self.b['create_auto'](self.b['AutoIn'](type='link_register',name='注册全部线路',btn_text='注册',keywords='状态',success_kw='成功',ai_flock_enabled=1,ai_flock_prompt='聊天',ai_flock_provider_ids='')))['id']
        signin=(await self.b['create_auto'](self.b['AutoIn'](type='signin',name='签到全部线路',target='@dummy_bot',command='/sign',ai_provider_ids='',signin_recognition_mode='image',signin_image_prompt='识别图片')))['id']
        db.execute("INSERT INTO ai_flock_sessions(chat_id,source_task_id,expires_at,prompt,provider_ids) VALUES('-10',?,9999999999,'x','')",(registration,))
        db.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",[
            ('flock_learning_provider_ids',''),('flock_learning_enabled','1'),
            ('flock_owner_provider_ids',''),('flock_summary_enabled','1')]);db.commit()
        deleted=await self.b['delete_ai_provider'](provider)
        self.assertEqual(deleted['affected_references'],5)
        self.assertEqual(db.execute('SELECT enabled FROM ai_flock_jobs WHERE id=?',(job,)).fetchone()[0],0)
        self.assertEqual(db.execute('SELECT ai_flock_enabled FROM automations WHERE id=?',(registration,)).fetchone()[0],0)
        self.assertEqual(tuple(db.execute('SELECT signin_recognition_mode,enabled FROM automations WHERE id=?',(signin,)).fetchone()),('none',1))
        self.assertIsNone(db.execute("SELECT 1 FROM ai_flock_sessions WHERE chat_id='-10'").fetchone())
        self.assertEqual((self.b['get_setting']('flock_learning_enabled'),self.b['get_setting']('flock_summary_enabled')),('0','0'))

    async def test_stale_provider_ids_reconcile_but_disabled_existing_survives(self):
        active=(await self.b['create_ai_provider'](self.body(note='active')))['provider']['id'];disabled=(await self.b['create_ai_provider'](self.body(note='disabled',key='two',enabled=False)))['provider']['id'];db=self.b['conn'];db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit()
        job=(await self.b['create_ai_flock_job'](self.b['AIFlockJobInput'](name='旧引用',enabled=0,groups='-1',prompt='聊',provider_ids=disabled)))['id'];auto=(await self.b['create_auto'](self.b['AutoIn'](type='link_register',name='注册',btn_text='注册',keywords='状态',success_kw='成功',enabled=0,ai_flock_provider_ids=disabled)))['id']
        stale='f'*16;legacy='a'*64;db.execute('UPDATE ai_flock_jobs SET provider_ids=? WHERE id=?',(disabled+','+stale+','+legacy,job));db.execute('UPDATE automations SET ai_flock_provider_ids=? WHERE id=?',(active+','+stale,auto));db.execute("INSERT INTO ai_flock_sessions(chat_id,source_task_id,expires_at,prompt,provider_ids) VALUES('-1',?,9999999999,'x',?)",(auto,stale+','+disabled));db.execute("INSERT INTO settings(key,value) VALUES('flock_learning_provider_ids',?)",(legacy+','+disabled,));db.commit()
        await self.b['list_ai_providers']()
        self.assertEqual(db.execute('SELECT provider_ids FROM ai_flock_jobs WHERE id=?',(job,)).fetchone()[0],disabled);self.assertEqual(db.execute('SELECT ai_flock_provider_ids FROM automations WHERE id=?',(auto,)).fetchone()[0],active);self.assertEqual(db.execute("SELECT provider_ids FROM ai_flock_sessions WHERE chat_id='-1'").fetchone()[0],disabled);self.assertEqual(self.b['get_setting']('flock_learning_provider_ids'),disabled)
        db.execute('UPDATE ai_flock_jobs SET enabled=1,provider_ids=? WHERE id=?',(stale,job));db.execute('UPDATE automations SET ai_flock_enabled=1,ai_flock_provider_ids=? WHERE id=?',(stale,auto));db.execute("UPDATE ai_flock_sessions SET provider_ids=? WHERE chat_id='-1'",(stale,));db.execute("UPDATE settings SET value=? WHERE key='flock_learning_provider_ids'",(stale,));db.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",[(key,'1') for key in ('flock_learning_enabled','flock_learning_unlimited','flock_deep_learning_enabled','flock_skill_distill_enabled')]);db.commit()
        await self.b['list_ai_providers']()
        self.assertEqual(tuple(db.execute('SELECT provider_ids,enabled FROM ai_flock_jobs WHERE id=?',(job,)).fetchone()),('',0));self.assertEqual(tuple(db.execute('SELECT ai_flock_provider_ids,ai_flock_enabled FROM automations WHERE id=?',(auto,)).fetchone()),('',0));self.assertIsNone(db.execute("SELECT 1 FROM ai_flock_sessions WHERE chat_id='-1'").fetchone());self.assertEqual(self.b['get_setting']('flock_learning_provider_ids'),'');self.assertEqual(tuple(self.b['get_setting'](key) for key in ('flock_learning_enabled','flock_learning_unlimited','flock_deep_learning_enabled','flock_skill_distill_enabled')),('0','0','0','0'))

    async def test_model_discovery_saved_blank_key_normalizes_bounds_and_redacts(self):
        provider=(await self.b['create_ai_provider'](self.body(base='https://example.invalid/v1/chat/completions')))['provider']['id']
        self.b['_validate_model_discovery_host']=lambda _endpoint:'93.184.216.34'
        self.b['_fetch_models_pinned']=AsyncMock(return_value=json.dumps({'data':[{'id':'m'+str(i)} for i in range(510)]+[{'id':'m1'},{}]}).encode())
        result=await self.b['discover_ai_models'](self.b['AIModelsInput'](provider_id=provider,key=''))
        self.b['_fetch_models_pinned'].assert_awaited_once_with('https://example.invalid/v1/models','secret-one','93.184.216.34');self.assertEqual(len(result['models']),500);self.assertNotIn('secret-one',str(result))
        self.b['_fetch_models_pinned']=AsyncMock(side_effect=RuntimeError('Bearer secret-one'))
        with self.assertRaises(HTTPException) as caught:await self.b['discover_ai_models'](self.b['AIModelsInput'](provider_id=provider))
        self.assertNotIn('secret-one',str(caught.exception))

    async def test_pinned_backend_ignores_original_host_for_tcp_connect(self):
        backend=self.b['_PinnedNetworkBackend'].__new__(self.b['_PinnedNetworkBackend']);backend.address='93.184.216.34';backend.backend=SimpleNamespace(connect_tcp=AsyncMock(return_value='stream'))
        self.assertEqual(await backend.connect_tcp('api.example.com',443,15,None,[]),'stream')
        backend.backend.connect_tcp.assert_awaited_once_with('93.184.216.34',443,15,None,[])

    async def test_model_discovery_rejects_saved_key_base_change_and_private_hosts(self):
        provider=(await self.b['create_ai_provider'](self.body()))['provider']['id']
        with self.assertRaises(HTTPException):
            await self.b['discover_ai_models'](self.b['AIModelsInput'](provider_id=provider,base='https://changed.invalid/v1',key=''))
        self.b['httpx']=SimpleNamespace(AsyncClient=AsyncMock(side_effect=AssertionError('NO HTTP')))
        with self.assertRaises(HTTPException):
            await self.b['discover_ai_models'](self.b['AIModelsInput'](base='http://127.0.0.1/v1',key='temporary-secret'))
        original=self.b['socket'].getaddrinfo
        self.b['socket'].getaddrinfo=lambda *_args,**_kwargs:[(2,1,6,'',('10.0.0.8',443))]
        try:
            with self.assertRaises(HTTPException) as caught:
                await self.b['discover_ai_models'](self.b['AIModelsInput'](base='https://public-name.invalid/v1',key='temporary-secret'))
            self.assertNotIn('temporary-secret',str(caught.exception))
        finally:self.b['socket'].getaddrinfo=original


if __name__ == '__main__':
    unittest.main()
