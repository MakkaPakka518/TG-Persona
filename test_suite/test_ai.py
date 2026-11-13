"""AI/验证码测试：只提取原函数、内存SQLite和假HTTP/OCR，绝不联网。"""
import asyncio
import base64
from types import SimpleNamespace
import struct
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zlib

from test_backend import load_backend, HTTPException


def png_bytes():
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!II5B', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\x00\x00\x00')) + chunk(b'IEND', b''))


class AITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_backend()
        self.b.update(_ai_test_lock=asyncio.Lock(), _ocr_guard=threading.Lock(), _ocr=None)
        self.b['set_setting']('ai_base', 'https://example.invalid/v1')
        self.b['set_setting']('ai_key', 'dummy-fixture-key')
        self.b['set_setting']('ai_model', 'dummy-model')
        self.b['set_setting']('ai_enabled', '1')
        self.b['set_setting']('ocr_enabled', '1')
        self.png = png_bytes()
        self.data_url = 'data:image/png;base64,' + base64.b64encode(self.png).decode()
        self.calls = []
        self.reply = '测试成功'
        self.response_status = 200
        self.response_data = None
        outer = self
        class Client:
            def __init__(self, **kwargs): outer.client_options = kwargs
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, url, **kwargs):
                outer.calls.append((url, kwargs))
                data = outer.response_data if outer.response_data is not None else {'choices': [{'message': {'content': outer.reply}}]}
                return SimpleNamespace(status_code=outer.response_status, json=lambda: data)
        self.b['httpx'] = SimpleNamespace(AsyncClient=Client)

    def tearDown(self):
        self.b['conn'].close()

    def body(self, **kwargs):
        values = dict(ai_base=None, ai_key=None, ai_model=None, ai_enabled=None, ocr_enabled=None,
                      image=self.data_url, length=0, mode='captcha',
                      prompt=self.b['DEFAULT_SIGNIN_IMAGE_PROMPT'], provider_ids='', options='')
        values.update(kwargs)
        return SimpleNamespace(**values)

    def config(self):
        return self.b['_test_ai_settings'](self.body())[0]

    async def test_ai_ping_sends_real_fixed_prompt_and_returns_reply(self):
        result = await self.b['test_ai_connection'](self.body(ai_enabled=False))
        self.assertTrue(result['ok']);self.assertEqual(result['reply'], '测试成功')
        url, options = self.calls[0]
        self.assertEqual(url, 'https://example.invalid/v1/chat/completions')
        self.assertIn('测试成功', options['json']['messages'][0]['content'])
        self.assertEqual(options['json']['model'], 'dummy-model')
        self.assertFalse(self.client_options['follow_redirects'])
        self.assertGreaterEqual(result['elapsed_ms'], 0)

    async def test_ai_unexpected_reply_is_not_false_success(self):
        self.reply = '不应该输出测试成功'
        result = await self.b['test_ai_connection'](self.body())
        self.assertFalse(result['ok']);self.assertEqual(result['reply'], self.reply)

    async def test_ai_current_form_config_does_not_save_or_replace_other_settings(self):
        result = await self.b['test_ai_connection'](self.body(ai_base='https://other.invalid/api/v4/', ai_key='another-dummy-key', ai_model='new-dummy'))
        self.assertTrue(result['ok']);self.assertEqual(self.calls[0][0], 'https://other.invalid/api/v4/chat/completions')
        self.assertEqual(self.b['get_setting']('ai_key'), 'dummy-fixture-key')
        self.assertEqual(self.b['get_setting']('ai_model'), 'dummy-model')

    async def test_blank_key_uses_saved_key(self):
        await self.b['test_ai_connection'](self.body(ai_key=''))
        self.assertEqual(self.calls[0][1]['headers']['Authorization'], 'Bearer dummy-fixture-key')

    async def test_http_api_error_and_key_redaction(self):
        self.response_status=401;self.response_data={'error':{'message':'invalid dummy-fixture-key Bearer another-dummy'}}
        result=await self.b['test_ai_connection'](self.body())
        self.assertFalse(result['ok']);self.assertIn('401',result['error'])
        self.assertNotIn('dummy-fixture-key',result['error']);self.assertNotIn('another-dummy',result['error'])

    async def test_missing_config_is_not_sent_to_network(self):
        result=await self.b['test_ai_connection'](self.body(ai_base=''))
        self.assertFalse(result['ok']);self.assertEqual(self.calls,[])

    async def test_invalid_url_is_rejected_without_network(self):
        for url in ('file:///tmp/private','https://user:password@example.invalid/v1','https://example.invalid/v1?key=secret'):
            result=await self.b['test_ai_connection'](self.body(ai_base=url))
            self.assertFalse(result['ok'])
        self.assertEqual(self.calls,[])

    async def test_endpoint_not_appended_twice(self):
        await self.b['test_ai_connection'](self.body(ai_base='https://example.invalid/v1/chat/completions/'))
        self.assertEqual(self.calls[0][0], 'https://example.invalid/v1/chat/completions')

    async def test_empty_or_malformed_response_is_clear_failure(self):
        self.response_data={'choices':[]}
        result=await self.b['test_ai_connection'](self.body());self.assertFalse(result['ok'])
        self.response_data=None;self.reply=''
        result=await self.b['test_ai_connection'](self.body());self.assertFalse(result['ok'])

    async def test_image_ai_success_reports_channel_and_correct_mime(self):
        self.reply='A7B3'
        result=await self.b['test_captcha_recognition'](self.body(length=4))
        self.assertTrue(result['ok']);self.assertEqual(result['text'],'A7B3');self.assertEqual(result['channel'],'ai')
        self.assertEqual(result['errors'],[])
        content=self.calls[0][1]['json']['messages'][0]['content']
        self.assertEqual(content[1]['image_url']['url'],self.data_url)

    async def test_object_image_mode_uses_custom_prompt_and_preserves_answer(self):
        self.reply='红色 连衣裙'
        result=await self.b['test_captcha_recognition'](self.body(mode='image',prompt='识别服装，只输出名称'))
        self.assertTrue(result['ok']);self.assertEqual(result['text'],'红色 连衣裙');self.assertEqual(result['channel'],'ai')
        content=self.calls[0][1]['json']['messages'][0]['content']
        self.assertIn('识别服装，只输出名称',content[0]['text']);self.assertEqual(content[1]['image_url']['url'],self.data_url)

    async def test_object_image_mode_never_falls_back_to_text_ocr(self):
        self.response_status=400;self.response_data={'error':{'message':'vision unavailable'}}
        self.b['_classify_local_image']=Mock(return_value='WRONG-OCR')
        result=await self.b['test_captcha_recognition'](self.body(mode='image',prompt='识别物体'))
        self.assertFalse(result['ok']);self.assertEqual(result['errors'][0]['channel'],'ai')
        self.b['_classify_local_image'].assert_not_called()

    async def test_object_image_mode_requires_and_returns_one_candidate(self):
        self.reply='苹果'
        result=await self.b['test_captcha_recognition'](self.body(mode='image',prompt='选择物体',options='苹果\n香蕉'))
        self.assertTrue(result['ok']);self.assertEqual(result['text'],'苹果')
        self.assertIn('候选项列表',self.calls[0][1]['json']['messages'][0]['content'][0]['text'])
        self.calls.clear();self.reply='西瓜'
        result=await self.b['test_captcha_recognition'](self.body(mode='image',prompt='选择物体',options='苹果\n香蕉'))
        self.assertFalse(result['ok']);self.assertIn('候选项',result['errors'][0]['message'])

    async def test_image_ai_failure_falls_back_to_local_ocr_and_shows_reason(self):
        self.response_status=400;self.response_data={'error':{'message':'model does not support images'}}
        self.b['_classify_local_image']=Mock(return_value='5K8P')
        result=await self.b['test_captcha_recognition'](self.body())
        self.assertTrue(result['ok']);self.assertEqual(result['channel'],'ocr');self.assertEqual(result['text'],'5K8P')
        self.assertEqual(result['errors'][0]['channel'],'ai')
        self.b['_classify_local_image'].assert_called_once_with(self.png)

    async def test_local_only_skips_ai(self):
        self.b['_classify_local_image']=Mock(return_value='LOCAL9')
        result=await self.b['test_captcha_recognition'](self.body(ai_enabled=False))
        self.assertTrue(result['ok']);self.assertEqual(result['channel'],'ocr');self.assertEqual(self.calls,[])

    async def test_both_disabled_is_explicit_failure(self):
        self.b['_classify_local_image']=Mock(return_value='unused')
        result=await self.b['test_captcha_recognition'](self.body(ai_enabled=False,ocr_enabled=False))
        self.assertFalse(result['ok']);self.assertEqual(result['channel'],'none');self.assertTrue(result['errors'])
        self.assertEqual(self.calls,[]);self.b['_classify_local_image'].assert_not_called()

    async def test_ai_only_failure_does_not_run_local(self):
        self.reply=''
        self.b['_classify_local_image']=Mock(return_value='unused')
        result=await self.b['test_captcha_recognition'](self.body(ocr_enabled=False))
        self.assertFalse(result['ok']);self.b['_classify_local_image'].assert_not_called()

    async def test_expected_length_failure_can_fall_back(self):
        self.reply='12345';self.b['_classify_local_image']=Mock(return_value='A2P')
        result=await self.b['test_captcha_recognition'](self.body(length=3))
        self.assertTrue(result['ok']);self.assertEqual(result['text'],'A2P');self.assertEqual(result['channel'],'ocr')
        self.assertIn('长度',result['errors'][0]['message'])

    async def test_local_failure_is_readable_and_does_not_crash(self):
        self.b['_classify_local_image']=Mock(side_effect=ModuleNotFoundError('ddddocr'))
        result=await self.b['test_captcha_recognition'](self.body(ai_enabled=False))
        self.assertFalse(result['ok']);self.assertIn('OCR',result['errors'][0]['message'])

    async def test_invalid_or_overlarge_image_never_calls_ai(self):
        invalid=('not data URL','data:image/png;base64,%%%%','data:image/jpeg;base64,'+base64.b64encode(self.png).decode(),
                 'data:image/png;base64,'+base64.b64encode(self.png+b'x'*(2*1024*1024)).decode())
        for value in invalid:
            with self.subTest(value=value[:40]), self.assertRaises(HTTPException) as caught:
                await self.b['test_captcha_recognition'](self.body(image=value))
            self.assertEqual(caught.exception.status_code,400)
        self.assertEqual(self.calls,[])

    async def test_busy_diagnostic_request_is_rejected(self):
        await self.b['_ai_test_lock'].acquire()
        try:
            with self.assertRaises(HTTPException) as caught:
                await self.b['test_ai_connection'](self.body())
            self.assertEqual(caught.exception.status_code,409);self.assertEqual(self.calls,[])
        finally:
            self.b['_ai_test_lock'].release()

    async def test_real_tasks_use_same_recognition_pipeline(self):
        self.reply='A7B3'
        text=await self.b['recognize_image_text'](self.png,4)
        self.assertEqual(text,'A7B3');self.assertEqual(len(self.calls),1)

    async def test_cached_local_ocr_initializes_once(self):
        classifier=Mock(return_value='7K8P')
        factory=Mock(return_value=SimpleNamespace(classification=classifier))
        with patch.dict('sys.modules',{'ddddocr':SimpleNamespace(DdddOcr=factory)}):
            for _ in range(2):
                result=await self.b['test_captcha_recognition'](self.body(ai_enabled=False))
                self.assertEqual(result['text'],'7K8P')
        factory.assert_called_once_with(show_ad=False);self.assertEqual(classifier.call_count,2)


    async def test_reasoning_effort_maps_to_openai_and_glm_payloads(self):
        call = self.b['_call_ai']
        async def payload_for(cfg):
            self.calls.clear()
            await call(cfg, 'x')
            return self.calls[-1][1]['json']
        base = {'base': 'https://example.invalid/v1', 'key': 'dummy-key'}
        openai_high = await payload_for({**base, 'model': 'gpt-x', 'reasoning_effort': 'high'})
        self.assertEqual(openai_high['reasoning_effort'], 'high')
        self.assertNotIn('thinking', openai_high)
        for effort in ('xhigh', 'max', 'medium', 'low', 'none'):
            payload = await payload_for({**base, 'model': 'gpt-x', 'reasoning_effort': effort})
            self.assertEqual(payload['reasoning_effort'], effort)
        self.assertNotIn('reasoning_effort', await payload_for({**base, 'model': 'gpt-x', 'reasoning_effort': 'default'}))
        glm_low = await payload_for({**base, 'model': 'glm-4.6', 'reasoning_effort': 'low'})
        self.assertEqual((glm_low['thinking'], glm_low['reasoning_effort']), ({'type': 'enabled'}, 'low'))
        glm_off = await payload_for({**base, 'model': 'glm-4.6', 'reasoning_effort': 'none'})
        self.assertEqual(glm_off['thinking'], {'type': 'disabled'})
        self.assertNotIn('reasoning_effort', glm_off)
        with self.assertRaisesRegex(ValueError, 'GLM-5.3'):
            await payload_for({**base, 'model': 'glm-5.3-flash', 'reasoning_effort': 'medium'})
        with self.assertRaisesRegex(ValueError, '思考强度'):
            await payload_for({**base, 'model': 'gpt-x', 'reasoning_effort': 'turbo'})

    async def test_client_profiles_inject_exact_headers_and_none_is_unchanged(self):
        call = self.b['_call_ai']
        async def headers_for(cfg):
            self.calls.clear()
            await call(cfg, 'x')
            return self.calls[-1][1]['headers']
        base = {'base': 'https://relay.invalid/v1', 'key': 'dummy-key', 'model': 'm'}
        self.assertEqual(await headers_for({**base, 'client_profile': 'none'}),
                         {'Authorization': 'Bearer dummy-key'})
        sid = 'ses_' + 'a' * 64
        oc = await headers_for({**base, 'client_profile': 'opencode', 'client_session_id': sid})
        self.assertEqual(oc['User-Agent'], 'opencode/1.19.5')
        self.assertEqual((oc['x-opencode-session-id'], oc['x-session-affinity'], oc['X-Session-Id']),
                         (sid, sid, sid))
        codex = await headers_for({**base, 'client_profile': 'codex'})
        self.assertTrue(codex['User-Agent'].startswith('codex_cli_rs/'))
        self.assertEqual((codex['originator'], codex['OpenAI-Beta']),
                         ('codex_cli_rs', 'responses=experimental'))
        self.assertNotIn('chatgpt-account-id', codex)
        claude = await headers_for({**base, 'client_profile': 'claude'})
        self.assertEqual(claude['User-Agent'], 'claude-cli/2.0.0 (external, cli)')
        self.assertEqual((claude['x-app'], claude['anthropic-version']), ('cli', '2023-06-01'))
        self.assertEqual(claude['Accept'], 'application/json')
        for headers in (oc, codex, claude):
            self.assertEqual(headers['Authorization'], 'Bearer dummy-key')
            self.assertNotIn('dummy-key', str({k: v for k, v in headers.items() if k != 'Authorization'}))
        with self.assertRaisesRegex(ValueError, '客户端伪装'):
            await headers_for({**base, 'client_profile': 'cursor'})

    async def test_glm53_assistant_mode_uses_low_reasoning_without_changing_image_calls(self):
        self.b['_call_ai'] = self.b['_call_ai']
        self.b['httpx'] = self.b['httpx']
        # Existing fake client captures the exact request body.
        self.b['_call_ai'].__globals__['httpx'] = self.b['httpx']
        import asyncio
        await self.b['_call_ai']({'base':'https://open.bigmodel.cn/api/paas/v4','key':'dummy-key','model':'glm-5.3-flash'},'task',1800,True)
        payload=self.calls[-1][1]['json']
        self.assertEqual(payload['thinking'],{'type':'enabled'})
        self.assertEqual(payload['reasoning_effort'],'low')
        self.assertGreaterEqual(payload['max_tokens'],8192)
        self.calls.clear()
        await self.b['_call_ai']({'base':'https://open.bigmodel.cn/api/paas/v4','key':'dummy-key','model':'glm-5.3-flash'},'image',20,False)
        self.assertNotIn('thinking',self.calls[-1][1]['json'])

    async def test_relay_keeps_compatible_token_budget_and_summary_can_omit_it(self):
        cfg = {'base': 'https://example.invalid/v1', 'key': 'dummy-key', 'model': 'gpt-6.1-sol', 'reasoning_effort': 'max'}
        await self.b['_call_ai'](cfg, '聊天', 500, feature='ai_flock')
        payload = self.calls[-1][1]['json']
        self.assertEqual(payload['max_tokens'], 500)
        self.assertNotIn('max_completion_tokens', payload)
        await self.b['_call_ai'](cfg, '总结', None, feature='summary')
        self.assertNotIn('max_completion_tokens', self.calls[-1][1]['json'])
        self.assertNotIn('max_tokens', self.calls[-1][1]['json'])
        self.response_data = {'choices': [{'message': {'content': '', 'reasoning_content': '思考内容'}, 'finish_reason': 'length'}]}
        with self.assertRaisesRegex(RuntimeError, '上限'):
            await self.b['_call_ai'](cfg, '聊天', 500, feature='ai_flock')

    async def test_summary_unlimited_omits_token_limits_for_all_request_variants(self):
        for model, assistant in [('plain-model', False), ('gpt-6.1-sol', False), ('glm-5.3-flash', True)]:
            with self.subTest(model=model):
                cfg = {'base': 'https://open.bigmodel.cn/api/paas/v4', 'key': 'dummy-key', 'model': model}
                await self.b['_call_ai'](cfg, '完整总结', None, assistant_mode=assistant, feature='summary')
                payload = self.calls[-1][1]['json']
                self.assertNotIn('max_tokens', payload)
                self.assertNotIn('max_completion_tokens', payload)
                self.assertEqual(self.client_options['timeout'], 90)

    async def test_reasoning_blocks_are_not_treated_as_final_text(self):
        cfg = {'base': 'https://example.invalid/v1', 'key': 'dummy-key', 'model': 'm'}
        for content in ([{'type': 'reasoning', 'text': '只有思考'}], {'text': '错误的正文格式'}):
            with self.subTest(content=content):
                self.response_data = {'choices': [{'message': {'content': content}}]}
                with self.assertRaises(RuntimeError):
                    await self.b['_call_ai'](cfg, '聊天', feature='ai_flock')
        self.response_data = {'choices': [{'message': {'content': [
            {'type': 'reasoning', 'text': '不可发送的思考'}, {'type': 'text', 'text': '正常回复'}]}}]}
        self.assertEqual(await self.b['_call_ai'](cfg, '聊天', feature='ai_flock'), '正常回复')

    async def test_summary_provider_truncation_tries_backup_instead_of_returning_partial_text(self):
        ids = []
        for note in ('首选', '备用'):
            ids.append((await self.b['create_ai_provider'](self.b['AIProviderInput'](
                note=note, base='https://example.invalid/v1', key='dummy-key', model=note)))['provider']['id'])
        original, outer = self.b['httpx'].AsyncClient, self
        complete = '完整的讨论结论。' * 300 + '总结完毕'
        class Responses(original):
            async def post(self, url, **kwargs):
                await super().post(url, **kwargs)
                first = len(outer.calls) == 1
                return SimpleNamespace(status_code=200, json=lambda: {'choices': [{
                    'message': {'content': '只有一半的总结' if first else complete},
                    'finish_reason': 'length' if first else 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 20}})
        self.b['httpx'] = SimpleNamespace(AsyncClient=Responses)
        trace = {}
        answer = await self.b['_call_ai_any']('完整总结', max_tokens=None, provider_ids=','.join(ids),
                                              feature='summary', trace=trace)
        self.assertEqual(answer, complete)
        self.assertEqual(len(self.calls), 2)
        self.assertIn('模型服务商', trace['failures'][0])
        self.assertEqual(self.b['conn'].execute("SELECT SUM(input_tokens+output_tokens) FROM ai_usage WHERE feature='summary'").fetchone()[0], 60)

    async def test_reasoning_only_response_switches_to_selected_backup(self):
        ids = []
        for note in ('首选', '备用'):
            ids.append((await self.b['create_ai_provider'](self.b['AIProviderInput'](
                note=note, base='https://example.invalid/v1', key='dummy-key', model=note)))['provider']['id'])
        original = self.b['httpx'].AsyncClient
        outer = self
        class Responses(original):
            async def post(self, url, **kwargs):
                response = await super().post(url, **kwargs)
                if len(outer.calls) == 1:
                    return SimpleNamespace(status_code=200, json=lambda: {'choices': [
                        {'message': {'content': None, 'reasoning_content': '只有思考没有正文'}, 'finish_reason': 'stop'}]})
                return response
        self.b['httpx'] = SimpleNamespace(AsyncClient=Responses)
        trace = {}
        result = await self.b['_call_ai_any']('聊聊天', provider_ids=','.join(ids), feature='ai_flock',
                                             validate=self.b['_flock_reply'], trace=trace)
        self.assertEqual(result, '测试成功')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(trace['provider_id'], ids[1])
        self.assertIn('没有最终正文', trace['failures'][0])


if __name__ == '__main__':
    unittest.main()
