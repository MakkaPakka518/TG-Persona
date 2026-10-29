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
                      image=self.data_url, length=0)
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


if __name__ == '__main__':
    unittest.main()
