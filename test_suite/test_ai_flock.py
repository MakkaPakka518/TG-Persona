"""AI 水群：时间段配置、抢注临时会话、上下文生成和启用门禁。"""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_backend import load_backend, HTTPException, message


class AIFlockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b=load_backend();self.b['now_local']=lambda:datetime(2026,10,30,10,0,tzinfo=timezone.utc)
        runtime=self.b['get_runtime']();runtime.ai_flock_busy=False;runtime.ai_flock_busy_groups=set();runtime.ai_flock_last={};runtime.ai_flock_blocked={}
        runtime.client=SimpleNamespace(get_messages=AsyncMock(return_value=[message(3,'今天真热'),message(2,'是啊')]),send_message=AsyncMock(return_value=message(4,out=True)))

    def tearDown(self): self.b['conn'].close()

    async def provider(self):
        await self.b['create_ai_provider'](self.b['AIProviderInput'](note='测试',base='https://example.invalid/v1',key='fixture-key',model='vision'))

    async def test_scheduled_flock_requires_provider_group_prompt_and_window(self):
        self.b['conn'].execute("INSERT INTO watched_groups(chat_id,name,added_at) VALUES('-1001','测试群',0)");self.b['conn'].commit()
        body={'water_ai_enabled':'1','water_ai_groups':'-1001','water_ai_start':'09:00','water_ai_end':'11:00','water_ai_prompt':'自然聊天'}
        with self.assertRaises(HTTPException): await self.b['update_settings'](body)
        await self.provider();await self.b['update_settings'](body)
        source=self.b['_ai_flock_source']('-1001');self.assertEqual((source['kind'],source['prompt']),('scheduled','自然聊天'))
        self.b['now_local']=lambda:datetime(2026,10,30,12,0,tzinfo=timezone.utc)
        self.assertIsNone(self.b['_ai_flock_source']('-1001'))

    async def test_registration_flock_has_independent_prompt_and_absolute_expiry(self):
        with self.assertRaises(HTTPException):
            await self.b['create_auto'](self.b['AutoIn'](type='link_register',name='无 API',btn_text='注册',keywords='注册状态',success_kw='成功',ai_flock_enabled=1))
        await self.provider()
        aid=(await self.b['create_auto'](self.b['AutoIn'](type='link_register',name='注册',btn_text='注册',keywords='注册状态',success_kw='成功',
            ai_flock_enabled=1,ai_flock_minutes=5,ai_flock_prompt='注册成功人格')))['id']
        task=self.b['conn'].execute('SELECT * FROM automations WHERE id=?',(aid,)).fetchone()
        self.assertTrue(self.b['_start_registration_ai_flock'](task,-1002))
        source=self.b['_ai_flock_source']('-1002');self.assertEqual((source['kind'],source['task_id'],source['prompt']),('registration',aid,'注册成功人格'))
        self.assertGreaterEqual(source['expires_at'],int(self.b['time'].time())+299)

    async def test_context_reply_is_short_logged_and_rate_limited(self):
        await self.provider();self.b['conn'].execute("INSERT INTO watched_groups(chat_id,name,added_at) VALUES('-1001','测试群',0)");self.b['conn'].commit()
        for key,value in {'water_ai_enabled':'1','water_ai_groups':'-1001','water_ai_start':'09:00','water_ai_end':'11:00','water_ai_prompt':'像普通群友'}.items():self.b['set_setting'](key,value)
        self.b['_call_ai_any']=AsyncMock(return_value='确实，今天挺热的')
        with patch('asyncio.sleep',new=AsyncMock()): await self.b['_run_ai_flock'](-1001,'今天真热')
        runtime=self.b['get_runtime']();runtime.client.send_message.assert_awaited_once_with(-1001,'确实，今天挺热的',parse_mode=None)
        self.assertEqual(self.b['conn'].execute("SELECT action,reply FROM hits ORDER BY id DESC").fetchone()['action'],'ai_flock')
        await self.b['_run_ai_flock'](-1001,'第二条');runtime.client.send_message.assert_awaited_once()


if __name__=='__main__': unittest.main()
