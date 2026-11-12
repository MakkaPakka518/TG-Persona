"""Account-local knowledge documents and read-only AI reference; no network."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
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

    async def test_registration_sessions_keep_their_knowledge_budget(self):
        await self.provider()
        for kind in ('link_register', 'button_register', 'code_register'):
            body = self.b['AutoIn'](type=kind, name=kind, groups='-1', target='@test_bot',
                   btn_text='注册', keywords='状态', success_kw='成功', ai_flock_enabled=1,
                   ai_flock_knowledge_chars=-1)
            row = (await self.b['create_auto'](body))['id']
            task = self.b['conn'].execute('SELECT * FROM automations WHERE id=?', (row,)).fetchone()
            self.assertTrue(self.b['_start_registration_ai_flock'](task, '-1'))
            self.assertEqual(self.b['_ai_flock_source']('-1')['knowledge_chars'], -1)


if __name__ == '__main__':
    unittest.main()
