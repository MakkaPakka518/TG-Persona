"""多 AI API：迁移、密钥隔离、轮询、故障切换和 Token 统计。"""
import unittest
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

    async def test_referenced_provider_cannot_disable_or_delete(self):
        provider=(await self.b['create_ai_provider'](self.body()))['provider']['id'];db=self.b['conn'];db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit()
        job=(await self.b['create_ai_flock_job'](self.b['AIFlockJobInput'](name='引用',groups='-1',prompt='聊',provider_ids=provider)))['id']
        with self.assertRaises(HTTPException) as disabled:await self.b['update_ai_provider'](provider,self.body(key='',enabled=False))
        self.assertEqual(disabled.exception.status_code,409)
        db.execute('UPDATE ai_flock_jobs SET enabled=0 WHERE id=?',(job,));db.commit()
        with self.assertRaises(HTTPException) as deleted:await self.b['delete_ai_provider'](provider)
        self.assertEqual(deleted.exception.status_code,409)
        db.execute('DELETE FROM ai_flock_jobs');db.execute("INSERT INTO settings(key,value) VALUES('flock_learning_provider_ids',?)",(provider,));db.commit()
        with self.assertRaises(HTTPException):await self.b['delete_ai_provider'](provider)

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
