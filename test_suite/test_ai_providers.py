"""多 AI API：迁移、密钥隔离、轮询、故障切换和 Token 统计。"""
import unittest
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


if __name__ == '__main__':
    unittest.main()
