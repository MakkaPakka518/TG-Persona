"""Manual flock tests use model output, saved account config and tracked sends; no network."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from test_backend import load_backend, HTTPException


class ManualFlockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.b = load_backend()
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = self.b['get_runtime']()
        for name in ('skill', 'study', 'knowledge'):
            setattr(type(self.runtime), name + '_file', Path(self.temp.name) / (name + '.md'))
        self.runtime.tg_state = {'phase': 'ready', 'me': {'id': 42}}
        self.runtime.client = SimpleNamespace(
            get_messages=AsyncMock(return_value=[]),
            send_message=AsyncMock(return_value=SimpleNamespace(id=501, chat_id=-1, out=True)))
        self.db = self.b['conn']
        self.db.executemany('INSERT INTO watched_groups VALUES(?,?,0)', [('-1', '群一'), ('-2', '群二')])
        self.db.commit()
        self.provider = (await self.b['create_ai_provider'](self.b['AIProviderInput'](
            note='测试线路', base='https://example.invalid/v1', key='fixture-test-key',
            model='fixture-model', reasoning_effort='high', client_profile='codex')))['provider']['id']
        self.job = (await self.b['create_ai_flock_job'](self.b['AIFlockJobInput'](
            name='测试任务', enabled=0, groups='-1', start_time='00:00', end_time='00:01',
            prompt='口语自然', provider_ids=self.provider, max_tokens=600, message_chars=40)))['id']

    async def asyncTearDown(self):
        self.db.close()
        self.temp.cleanup()

    async def run_test(self, text='问大家今晚聊什么', chat_id='-1', job=None):
        return await self.b['test_ai_flock_job'](self.job if job is None else job,
                                                self.b['AIFlockTestInput'](chat_id=chat_id, text=text))

    async def test_saved_model_generates_then_sends_and_records_trace(self):
        calls = []
        class Client:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def post(self, url, **kwargs):
                calls.append((url, kwargs))
                return SimpleNamespace(status_code=200, json=lambda: {
                    'choices': [{'message': {'content': '今晚大家有什么安排呀'}}],
                    'usage': {'prompt_tokens': 20, 'completion_tokens': 9}})
        self.b['httpx'] = SimpleNamespace(AsyncClient=Client)
        self.b['_write_skill']('- 主人喜欢简短口语')
        self.b['_write_study'](self.b['_append_study']('', '- 本群喜欢电影', chat_id='-1'))
        self.b['_write_knowledge']('晚间休闲知识')
        request_text = '用我的语气问问今晚大家都要做什么，不要提测试。' + '请简短自然。' * 12
        result = await self.run_test(request_text)
        self.assertTrue(result['sent'])
        self.assertEqual(result['reply'], '今晚大家有什么安排呀')
        self.assertEqual(len(calls), 1)
        payload = calls[0][1]['json']
        self.assertEqual((payload['model'], payload['max_tokens'], payload['reasoning_effort']),
                         ('fixture-model', 600, 'high'))
        self.assertIn('codex_cli_rs/', calls[0][1]['headers']['User-Agent'])
        prompt = payload['messages'][0]['content']
        for text in (request_text, '口语自然', '主人喜欢简短口语', '本群喜欢电影', '晚间休闲知识'):
            self.assertIn(text, prompt)
        self.runtime.client.send_message.assert_awaited_once_with(-1, result['reply'], parse_mode=None)
        self.assertNotEqual(result['reply'], request_text)
        row = self.db.execute('SELECT kind FROM generated_messages WHERE chat_id=? AND message_id=501', ('-1',)).fetchone()
        self.assertEqual(row[0], 'ai_flock')
        trace = await self.b['get_ai_flock_trace'](result['hit_id'])
        self.assertEqual(trace['call']['provider_id'], self.provider)
        self.assertIn('本群喜欢电影', trace['references']['study'])
        hit = self.db.execute('SELECT detail FROM hits WHERE id=?', (result['hit_id'],)).fetchone()[0]
        self.assertIn('"source": "test"', hit)
        self.assertNotIn('fixture-test-key', hit)
        self.assertEqual(self.db.execute('SELECT enabled FROM ai_flock_jobs WHERE id=?', (self.job,)).fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT SUM(input_tokens+output_tokens) FROM ai_usage WHERE feature=?', ('ai_flock',)).fetchone()[0], 29)

    async def test_model_failure_and_invalid_outputs_never_send_input(self):
        self.b['_call_ai_any'] = AsyncMock(side_effect=RuntimeError('provider offline'))
        with self.assertRaises(HTTPException) as caught:
            await self.run_test()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(self.runtime.ai_flock_busy_groups, set())
        for reply in ('NO_REPLY', '', 'https://invalid.example/path', '/start', '多行\n内容', '长' * 81):
            self.runtime.ai_flock_last.clear()
            self.b['_call_ai_any'] = AsyncMock(return_value=reply)
            result = await self.run_test()
            self.assertFalse(result['sent'])
            self.assertTrue(result['reason'])
        self.runtime.client.send_message.assert_not_awaited()

    async def test_rejects_bad_input_foreign_groups_and_missing_job_before_ai(self):
        self.b['_call_ai_any'] = AsyncMock(return_value='不该发送')
        for changes in ({'text': '   '}, {'chat_id': '@someone'}, {'chat_id': '123'},
                        {'chat_id': '-2'}, {'job': 999}):
            with self.assertRaises(HTTPException):
                await self.run_test(**changes)
        self.runtime.tg_state['phase'] = 'idle'
        with self.assertRaises(HTTPException) as caught:
            await self.run_test()
        self.assertEqual(caught.exception.status_code, 409)
        self.b['_call_ai_any'].assert_not_awaited()
        self.runtime.client.send_message.assert_not_awaited()

    async def test_busy_and_cooldown_prevent_duplicate_send(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return '只发一条'
        self.b['_call_ai_any'] = AsyncMock(side_effect=model)
        first = asyncio.create_task(self.run_test())
        await entered.wait()
        try:
            with self.assertRaises(HTTPException) as caught:
                await self.run_test()
            self.assertEqual(caught.exception.status_code, 409)
        finally:
            release.set()
            await first
        with self.assertRaises(HTTPException):
            await self.run_test()
        self.runtime.client.send_message.assert_awaited_once()

    async def test_pause_budget_and_floodwait_block_requests(self):
        self.b['_call_ai_any'] = AsyncMock(return_value='不该发送')
        self.b['set_setting']('flock_owner_pause_enabled', '1')
        self.b['set_setting']('flock_owner_last_-1', str(self.b['time'].time()))
        with self.assertRaises(HTTPException) as caught:
            await self.run_test()
        self.assertEqual(caught.exception.status_code, 409)
        self.b['set_setting']('flock_owner_pause_enabled', '0')
        self.b['set_setting']('flock_daily_flock_tokens', '1')
        self.b['_record_ai_usage'](self.provider, {'prompt_tokens': 1}, 'ai_flock')
        with self.assertRaises(HTTPException) as caught:
            await self.run_test()
        self.assertEqual(caught.exception.status_code, 429)
        self.b['set_setting']('flock_daily_flock_tokens', '0')
        self.runtime.ai_flock_blocked['-1'] = self.b['time'].monotonic() + 60
        with self.assertRaises(HTTPException):
            await self.run_test()
        self.b['_call_ai_any'].assert_not_awaited()
        self.runtime.client.send_message.assert_not_awaited()

    async def test_config_change_or_owner_speech_during_generation_cancels_send(self):
        for change in ('pause', 'prompt', 'group'):
            with self.subTest(change=change):
                self.b['set_setting']('flock_owner_pause_enabled', '0')
                self.db.execute("UPDATE ai_flock_jobs SET groups='-1',prompt='口语自然' WHERE id=?", (self.job,))
                async def model(*_args, **_kwargs):
                    if change == 'pause':
                        self.b['set_setting']('flock_owner_pause_enabled', '1')
                        self.b['set_setting']('flock_owner_last_-1', str(self.b['time'].time()))
                    else:
                        field, value = ('prompt', '新的人格') if change == 'prompt' else ('groups', '-2')
                        self.db.execute('UPDATE ai_flock_jobs SET ' + field + '=? WHERE id=?', (value, self.job))
                    return '不该发送'
                self.b['_call_ai_any'] = AsyncMock(side_effect=model)
                with self.assertRaises(HTTPException):
                    await self.run_test()
        self.runtime.client.send_message.assert_not_awaited()

    async def test_sent_message_stays_success_when_logging_fails(self):
        self.b['_call_ai_any'] = AsyncMock(return_value='已经发送成功')
        self.b['add_hit'] = Mock(side_effect=OSError('disk full'))
        result = await self.run_test()
        self.assertTrue(result['sent'])
        self.assertIn('日志保存失败', result['warning'])
        self.runtime.client.send_message.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
