"""Account-local knowledge documents and read-only AI reference; no network."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_backend import load_backend, HTTPException, message


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.b = load_backend()
        self.addCleanup(self.b['conn'].close)
        self.base = Path(self.temp.name)
        self.b['BASE'] = self.base
        self.runtime = self.b['get_runtime']()
        type(self.runtime).knowledge_file = self.base / 'account-knowledge.md'
        type(self.runtime).skill_file = self.base / 'skill.md'
        self.b['BASE'] = self.base
        self.b['DATA'] = self.base / 'data'
        self.b['DATA'].mkdir(exist_ok=True)
        self.b['account_registry'] = SimpleNamespace(accounts={'default': self.runtime})
        self.b['updater'] = SimpleNamespace(current_version=lambda: '1.20.4', STATE={})
        self.b['data_migration'] = SimpleNamespace(create_package=lambda *args: (_ for _ in ()).throw(ValueError('not configured')))
        self.b['_migration_lock'] = asyncio.Lock()

    async def provider(self):
        return (await self.b['create_ai_provider'](self.b['AIProviderInput'](
            note='测试', base='https://example.invalid/v1', key='fixture-key', model='test')))['provider']['id']

    def test_shipped_knowledge_has_all_topics_and_fits_document_limit(self):
        content = (Path(__file__).resolve().parents[1] / 'knowledge.md').read_text(encoding='utf-8')
        self.assertLessEqual(len(content.encode()), self.b['KNOWLEDGE_MAX_BYTES'])
        for topic in ('生活常识', '网络信息工程基础', '代码入门技术', '代理与反代技术',
                      'Cloudflare 错误代码', '网络错误代码', '常用古诗词', '常用文学语录'):
            self.assertIn(topic, content)
        self.assertIn('125 秒', content)
        self.assertIn('CPU 时间', content)

    async def test_builtin_is_only_a_fallback_and_manual_save_preserves_it(self):
        self.assertEqual((await self.b['get_flock_knowledge']())['content'], '')
        builtin = self.base / 'knowledge.md'
        builtin.write_text('# 默认知识\nHTTP 404 表示未找到资源', encoding='utf-8')
        self.assertEqual(self.b['_read_knowledge'](), builtin.read_text())
        self.assertFalse(self.runtime.knowledge_file.exists())
        await self.b['update_flock_knowledge'](self.b['KnowledgeInput'](content='# 自定义知识'))
        self.assertEqual(self.b['_read_knowledge'](), '# 自定义知识')
        self.assertIn('404', builtin.read_text())
        self.assertEqual(self.runtime.knowledge_file.stat().st_mode & 0o777, 0o600)
        await self.b['update_flock_knowledge'](self.b['KnowledgeInput'](content=''))
        self.assertEqual(self.b['_read_knowledge'](), '')

    async def test_utf8_size_boundary_import_export_and_failed_write_keeps_document(self):
        limit = self.b['KNOWLEDGE_MAX_BYTES']
        self.assertEqual(limit, 5 * 1024 * 1024)
        content = '知' * (limit // 3) + 'X' * (limit % 3)
        result = await self.b['update_flock_knowledge'](self.b['KnowledgeInput'](content=content))
        self.assertEqual(result['bytes'], limit)
        with self.assertRaises(HTTPException):
            await self.b['update_flock_knowledge'](self.b['KnowledgeInput'](content=content + '知'))
        self.assertEqual(self.b['_read_knowledge'](), content)
        class Request:
            def __init__(self, chunks): self.chunks = chunks
            async def stream(self):
                for chunk in self.chunks: yield chunk
        await self.b['import_flock_knowledge'](Request([b'# ', '反代知识'.encode()]))
        exported = await self.b['export_flock_knowledge']()
        self.assertEqual(exported.body, '# 反代知识'.encode())
        self.assertIn('knowledge.md', exported.headers['Content-Disposition'])
        self.assertEqual(exported.headers['Cache-Control'], 'no-store')
        for chunks in ([b'\xff'], [b'X' * limit, b'X']):
            with self.assertRaises(HTTPException): await self.b['import_flock_knowledge'](Request(chunks))
        self.assertEqual(self.b['_read_knowledge'](), '# 反代知识')
        with patch('os.replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(HTTPException):
                await self.b['update_flock_knowledge'](self.b['KnowledgeInput'](content='new'))
        self.assertEqual(self.b['_read_knowledge'](), '# 反代知识')

    async def test_symlink_and_non_file_are_rejected(self):
        outside = self.base / 'outside.md'
        outside.write_text('unchanged')
        self.runtime.knowledge_file.symlink_to(outside)
        for action in (self.b['get_flock_knowledge'](), self.b['update_flock_knowledge'](
                self.b['KnowledgeInput'](content='overwrite'))):
            with self.assertRaises(HTTPException): await action
        self.assertEqual(outside.read_text(), 'unchanged')
        self.runtime.knowledge_file.unlink()
        self.runtime.knowledge_file.mkdir()
        with self.assertRaises(HTTPException): await self.b['get_flock_knowledge']()

    async def test_upload_uses_saved_document_and_does_not_change_it(self):
        self.b['_write_knowledge']('# 保存的知识')
        self.runtime.tg_state = {'phase': 'ready'}
        self.runtime.client = SimpleNamespace(send_file=AsyncMock())
        before = self.runtime.knowledge_file.read_bytes()
        result = await self.b['upload_flock_knowledge']()
        call = self.runtime.client.send_file.await_args
        self.assertEqual(call.args[0], 'me')
        self.assertEqual(call.args[1].name, 'knowledge.md')
        self.assertEqual(call.args[1].getvalue(), before)
        self.assertTrue(call.kwargs['force_document'])
        self.assertEqual(self.b['get_setting']('flock_knowledge_last_upload_date'), result['date'])
        self.assertEqual(self.runtime.knowledge_file.read_bytes(), before)

    async def test_water_knowledge_budget_none_limited_unlimited_and_read_only(self):
        provider = await self.provider()
        self.b['conn'].execute("INSERT INTO watched_groups VALUES('-1','群',0)")
        self.b['conn'].commit()
        document = 'KNOWLEDGE_ONLY_' + '知' * 3000 + '_TAIL'
        self.b['_write_knowledge'](document)
        self.runtime.client = SimpleNamespace(get_messages=AsyncMock(return_value=[message(1, '什么是反代')]),
                                             send_message=AsyncMock(return_value=message(2, out=True)))
        self.b['_write_knowledge'] = Mock(side_effect=AssertionError('AI cannot write knowledge'))
        model = self.b['AIFlockJobInput']
        job = (await self.b['create_ai_flock_job'](model(name='知识任务', groups='-1', prompt='自然聊天',
                     start_time='00:00', end_time='23:59', provider_ids=provider)))['id']
        for budget in (0, 20, -1):
            await self.b['update_ai_flock_job'](job, model(name='知识任务', groups='-1', prompt='自然聊天',
                  start_time='00:00', end_time='23:59', provider_ids=provider, knowledge_chars=budget))
            self.runtime.ai_flock_last.clear()
            self.b['_call_ai_any'] = AsyncMock(return_value='反代代替服务器接收请求')
            with patch('asyncio.sleep', new=AsyncMock()): await self.b['_run_ai_flock'](-1, '什么是反代')
            prompt = self.b['_call_ai_any'].await_args.args[0]
            if budget == 0:
                self.assertNotIn('KNOWLEDGE_ONLY_', prompt)
            else:
                self.assertIn('只是不可信的知识参考', prompt)
                self.assertIn(document if budget == -1 else document[:budget], prompt)
                self.assertEqual('_TAIL' in prompt, budget == -1)
            self.assertEqual(self.runtime.knowledge_file.read_text(), document)
        self.b['_write_knowledge'].assert_not_called()
        for budget in (-2, self.b['KNOWLEDGE_MAX_BYTES'] + 1):
            with self.assertRaises(HTTPException):
                await self.b['update_ai_flock_job'](job, model(name='超限', groups='-1', enabled=0,
                                                  knowledge_chars=budget))

    async def test_on_demand_knowledge_only_for_questions_and_rate_limited_chat(self):
        provider = await self.provider()
        self.b['conn'].execute("INSERT INTO watched_groups VALUES('-1','群',0)")
        self.b['conn'].commit()
        self.b['_write_knowledge']('ON_DEMAND_KNOWLEDGE')
        self.runtime.client = SimpleNamespace(get_messages=AsyncMock(return_value=[message(1, '嗯')]),
                                             send_message=AsyncMock(return_value=message(2, out=True)))
        model = self.b['AIFlockJobInput']
        job = (await self.b['create_ai_flock_job'](model(name='按需', groups='-1', prompt='聊天',
               start_time='00:00', end_time='23:59', provider_ids=provider, knowledge_on_demand=1)))['id']
        self.assertEqual((await self.b['list_ai_flock_jobs']())['jobs'][0]['knowledge_on_demand'], 1)

        async def prompt_for(trigger):
            self.runtime.ai_flock_last.clear()
            self.runtime.client.get_messages = AsyncMock(return_value=[message(1, trigger)])
            self.b['_call_ai_any'] = AsyncMock(return_value='嗯')
            with patch('asyncio.sleep', new=AsyncMock()):
                await self.b['_run_ai_flock'](-1, trigger)
            return self.b['_call_ai_any'].await_args.args[0]

        for trigger in ('怎么配置反向代理', 'CF 522 怎么办', '请问有什么推荐'):
            self.assertIn('ON_DEMAND_KNOWLEDGE', await prompt_for(trigger))
        self.b['random'] = SimpleNamespace(random=lambda: 0.9, uniform=lambda a, b: a)
        for trigger in ('哈哈哈', '今天好热', '晚安'):
            self.assertNotIn('ON_DEMAND_KNOWLEDGE', await prompt_for(trigger))
        self.b['random'] = SimpleNamespace(random=lambda: 0.01, uniform=lambda a, b: a)
        self.assertIn('ON_DEMAND_KNOWLEDGE', await prompt_for('今天好热'))
        self.assertNotIn('ON_DEMAND_KNOWLEDGE', await prompt_for('今天好热'))

        await self.b['update_ai_flock_job'](job, model(name='按需', groups='-1', prompt='聊天',
              start_time='00:00', end_time='23:59', provider_ids=provider, knowledge_on_demand=0))
        self.assertIn('ON_DEMAND_KNOWLEDGE', await prompt_for('哈哈哈'))

    async def test_occasional_knowledge_works_immediately_after_fresh_boot(self):
        """fresh host: monotonic() is small; empty history must not be treated as waiting."""
        provider = await self.provider()
        self.b['conn'].execute("INSERT INTO watched_groups VALUES('-1','群',0)")
        self.b['conn'].commit()
        self.b['_write_knowledge']('ON_DEMAND_KNOWLEDGE')
        self.runtime.client = SimpleNamespace(get_messages=AsyncMock(return_value=[message(1, '今天好热')]),
                                             send_message=AsyncMock(return_value=message(2, out=True)))
        await self.b['create_ai_flock_job'](self.b['AIFlockJobInput'](
            name='按需', groups='-1', prompt='聊天', start_time='00:00', end_time='23:59',
            provider_ids=provider, knowledge_on_demand=1))
        self.runtime.ai_knowledge_last.clear()
        self.b['random'] = SimpleNamespace(random=lambda: 0.01, uniform=lambda a, b: a)
        with patch.object(self.b['time'], 'monotonic', return_value=5.0):
            self.b['_call_ai_any'] = AsyncMock(return_value='嗯')
            with patch('asyncio.sleep', new=AsyncMock()):
                await self.b['_run_ai_flock'](-1, '今天好热')
        self.assertIn('ON_DEMAND_KNOWLEDGE', self.b['_call_ai_any'].await_args.args[0])

    async def test_knowledge_preview_modes_do_not_call_ai_or_write(self):
        self.b['_write_knowledge']('PREVIEW_KNOWLEDGE_' + '知' * 2000 + '_TAIL')
        ai = AsyncMock(side_effect=AssertionError('preview must not call AI'))
        self.b['_call_ai_any'] = ai
        before = self.runtime.knowledge_file.read_bytes()
        model = self.b['KnowledgePreviewInput']
        full = await self.b['preview_flock_knowledge'](model(text='随便聊聊', knowledge_chars=10))
        self.assertEqual((full['mode'], full['preview']), ('full', 'PREVIEW_KN'))
        none = await self.b['preview_flock_knowledge'](model(text='怎么配置代理', knowledge_chars=0))
        self.assertEqual((none['mode'], none['preview'], none['chars']), ('', '', 0))
        relevant = await self.b['preview_flock_knowledge'](model(text='CF 522 怎么办', knowledge_chars=40, knowledge_on_demand=1))
        self.assertEqual(relevant['mode'], 'relevant')
        self.assertTrue(relevant['preview'].startswith('PREVIEW_KNOWLEDGE_'))
        chat = await self.b['preview_flock_knowledge'](model(text='今天好热', knowledge_chars=-1, knowledge_on_demand=1))
        self.assertEqual((chat['mode'], chat['chars']), ('chat', 1200))
        ai.assert_not_awaited()
        self.assertEqual(self.runtime.knowledge_file.read_bytes(), before)

    async def test_ai_usage_summary_groups_day_feature_provider(self):
        db = self.b['conn']
        self.b['_record_ai_usage']('0123456789abcdef', {'prompt_tokens': 10, 'completion_tokens': 5}, 'ai_flock')
        self.b['_record_ai_usage']('0123456789abcdef', {'input_tokens': 7, 'output_tokens': 3}, 'learning')
        self.b['_record_ai_usage']('0123456789abcdef', {}, 'ai_flock')
        summary = await self.b['ai_usage_summary'](14)
        self.assertEqual(sum((item['input_tokens'] for item in summary['features']), 0), 17)
        self.assertEqual(sum((item['output_tokens'] for item in summary['features']), 0), 8)
        self.assertEqual({item['feature'] for item in summary['features']}, {'ai_flock', 'learning'})
        self.assertEqual(summary['days'][0]['input_tokens'], 17)
        self.assertEqual(summary['providers'][0]['input_tokens'], 17)

    async def test_auto_backup_settings_validate_and_never_leak_password(self):
        runtime = self.runtime
        runtime.label = '默认'
        model = self.b['AutoBackupInput']
        with self.assertRaises(HTTPException):
            await self.b['update_auto_backup'](model(enabled=1, time='25:00', account='default', password='longenough'))
        with self.assertRaises(HTTPException):
            await self.b['update_auto_backup'](model(enabled=1, time='04:30', account='missing', password='longenough'))
        with self.assertRaises(HTTPException):
            await self.b['update_auto_backup'](model(enabled=1, time='04:30', account='default', password='short'))
        await self.b['update_auto_backup'](model(enabled=1, time='04:30', account='default', caption_password=1, password='longenough'))
        state = await self.b['get_auto_backup']()
        self.assertEqual((state['enabled'], state['time'], state['account'], state['caption_password'], state['password_present']),
                         (True, '04:30', 'default', True, True))
        self.assertNotIn('longenough', str(state))
        self.assertEqual(self.b['get_setting']('auto_backup_password'), 'longenough')
        await self.b['update_auto_backup'](model(enabled=1, time='05:00', account='default', password=''))
        self.assertEqual(self.b['get_setting']('auto_backup_password'), 'longenough')

    async def test_auto_backup_uploads_then_prunes_remote_and_local_temp(self):
        self.b['set_setting']('auto_backup_enabled', '1')
        self.b['set_setting']('auto_backup_password', 'longenough')
        self.b['set_setting']('auto_backup_caption_password', '1')
        runtime = self.runtime
        runtime.tg_state = {'phase': 'ready'}
        package = self.base / 'tg-persona-migration-20260101-010101.tgp'
        package.write_bytes(b'package')
        fresh = SimpleNamespace(document=SimpleNamespace(attributes=[SimpleNamespace(file_name=package.name)]), delete=AsyncMock())
        old = [SimpleNamespace(document=SimpleNamespace(attributes=[SimpleNamespace(file_name='tg-persona-migration-old%d.tgp' % i)]), delete=AsyncMock()) for i in range(4)]
        other = SimpleNamespace(document=SimpleNamespace(attributes=[SimpleNamespace(file_name='not-a-backup.txt')]), delete=AsyncMock())
        runtime.client = SimpleNamespace(get_messages=AsyncMock(return_value=[fresh] + old + [other]), send_file=AsyncMock())
        work = self.base / '.migration-export-test'
        work.mkdir()
        self.b['data_migration'].create_package = lambda *args: (package, work)
        result = await self.b['_run_auto_backup']('manual')
        self.assertEqual((result['ok'], result['removed']), (True, 2))
        call = runtime.client.send_file.await_args
        self.assertEqual((call.args[0], call.args[1]), ('me', str(package)))
        self.assertTrue(call.kwargs['force_document'])
        self.assertIn('迁移包密码：longenough', call.kwargs['caption'])
        self.assertFalse(work.exists())
        self.assertEqual(self.b['get_setting']('auto_backup_last_error'), '')
        self.assertEqual(self.b['get_setting']('auto_backup_last_file'), package.name)

    async def test_auto_backup_skips_when_disabled_and_records_upload_failure(self):
        result = await self.b['_run_auto_backup']('schedule')
        self.assertEqual(result['skipped'], True)
        self.b['set_setting']('auto_backup_enabled', '1')
        self.b['set_setting']('auto_backup_password', 'longenough')
        package = self.base / 'tg-persona-migration-x.tgp'; package.write_bytes(b'x')
        self.b['data_migration'].create_package = lambda *args: (package, self.base / '.work-none')
        self.runtime.tg_state = {'phase': 'idle'}
        with self.assertRaisesRegex(ValueError, '未登录'):
            await self.b['_run_auto_backup']('manual')
        self.assertTrue(self.b['get_setting']('auto_backup_last_error'))

    async def test_learning_never_reads_or_writes_knowledge(self):
        await self.provider()
        self.b['conn'].execute("INSERT INTO watched_groups VALUES('-1','群',0)")
        self.b['set_setting']('flock_learning_groups', '-1')
        self.b['conn'].executemany("INSERT INTO ai_learning_samples(ts,chat_id,text) VALUES(0,'-1',?)",
                                   [('写作样本 ' + str(i),) for i in range(10)])
        self.b['conn'].commit()
        self.b['_write_knowledge']('KNOWLEDGE_DO_NOT_LEARN')
        before = self.runtime.knowledge_file.read_bytes()
        self.b['_read_knowledge'] = Mock(side_effect=AssertionError('learning cannot read knowledge'))
        self.b['_call_ai_any'] = AsyncMock(return_value='- 使用简短语句')
        await self.b['_learn_ai_flock'](force=True)
        self.assertNotIn('KNOWLEDGE_DO_NOT_LEARN', self.b['_call_ai_any'].await_args.args[0])
        self.assertEqual(self.runtime.knowledge_file.read_bytes(), before)
        self.b['_read_knowledge'].assert_not_called()

    async def test_registration_sessions_keep_their_knowledge_mode(self):
        await self.provider()
        for kind in ('link_register', 'button_register', 'code_register'):
            body = self.b['AutoIn'](type=kind, name=kind, groups='-1', target='@test_bot',
                   btn_text='注册', keywords='状态', success_kw='成功', ai_flock_enabled=1,
                   ai_flock_knowledge_chars=-1, ai_flock_knowledge_on_demand=1)
            row = (await self.b['create_auto'](body))['id']
            task = self.b['conn'].execute('SELECT * FROM automations WHERE id=?', (row,)).fetchone()
            self.assertTrue(self.b['_start_registration_ai_flock'](task, '-1'))
            source = self.b['_ai_flock_source']('-1')
            self.assertEqual((source['knowledge_chars'], source['knowledge_on_demand']), (-1, 1))


if __name__ == '__main__':
    unittest.main()
