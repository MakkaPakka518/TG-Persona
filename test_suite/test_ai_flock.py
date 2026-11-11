"""AI 水群：时间段配置、抢注临时会话、上下文生成和启用门禁。"""
from datetime import datetime, timezone
from types import SimpleNamespace
import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from test_backend import load_backend, HTTPException, message


class AIFlockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b=load_backend();self.b['now_local']=lambda:datetime(2026,10,30,10,0,tzinfo=timezone.utc)
        runtime=self.b['get_runtime']();runtime.ai_flock_busy=False;runtime.ai_flock_busy_groups=set();runtime.ai_flock_last={};runtime.ai_flock_blocked={}
        runtime.client=SimpleNamespace(get_messages=AsyncMock(return_value=[message(3,'今天真热'),message(2,'是啊'),message(1,'我的旧消息',out=True)]),send_message=AsyncMock(return_value=message(4,out=True)))

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

    def test_legacy_registration_session_schema_adds_reply_budget(self):
        database=sqlite3.connect(':memory:')
        try:
            database.execute('CREATE TABLE ai_flock_sessions(chat_id TEXT PRIMARY KEY,source_task_id INTEGER,expires_at INTEGER NOT NULL,prompt TEXT NOT NULL)')
            self.b['db_init'](database)
            self.assertIn('replies',{row[1] for row in database.execute('PRAGMA table_info(ai_flock_sessions)')})
        finally: database.close()

    async def test_registration_flock_skips_noise_sees_own_history_and_stops_after_three(self):
        await self.provider()
        aid=(await self.b['create_auto'](self.b['AutoIn'](type='link_register',name='注册',btn_text='注册',keywords='注册状态',success_kw='成功',
            ai_flock_enabled=1,ai_flock_minutes=5)))['id']
        task=self.b['conn'].execute('SELECT * FROM automations WHERE id=?',(aid,)).fetchone();self.b['_start_registration_ai_flock'](task,-1002)
        runtime=self.b['get_runtime']();runtime.client.get_messages=AsyncMock(return_value=[message(3,'脚本？'),message(2,'终于抢到了',out=True),message(1,'这么快？')])
        self.b['_call_ai_any']=AsyncMock(return_value='NO_REPLY')
        await self.b['_run_ai_flock'](-1002,'还得优化');self.b['_call_ai_any'].assert_not_awaited()
        with patch('asyncio.sleep',new=AsyncMock()): await self.b['_run_ai_flock'](-1002,'脚本？')
        runtime.client.send_message.assert_not_awaited();instruction=self.b['_call_ai_any'].await_args.args[0]
        self.assertIn('北京时间为2026-10-30 18:00，工作日',instruction);self.assertIn('"role": "我"',instruction);self.assertIn('终于抢到了',instruction)
        runtime.ai_flock_last.clear();self.b['_call_ai_any'].reset_mock();self.b['_call_ai_any'].return_value='哈哈真手点的'
        with patch('asyncio.sleep',new=AsyncMock()): await self.b['_run_ai_flock'](-1002,'这么快？')
        runtime.client.send_message.assert_awaited_once_with(-1002,'哈哈真手点的',parse_mode=None)
        self.assertEqual(self.b['conn'].execute("SELECT replies FROM ai_flock_sessions WHERE chat_id='-1002'").fetchone()[0],1)
        self.b['conn'].execute("UPDATE ai_flock_sessions SET replies=3 WHERE chat_id='-1002'");self.b['conn'].commit();runtime.ai_flock_last.clear();self.b['_call_ai_any'].reset_mock()
        with patch('asyncio.sleep',new=AsyncMock()): await self.b['_run_ai_flock'](-1002,'你是机器人吗',True)
        self.b['_call_ai_any'].assert_not_awaited()

    async def test_registration_wait_and_reply_lookup_do_not_block_other_groups(self):
        await self.provider();runtime=self.b['get_runtime']();runtime.running['paused']=False
        aid=(await self.b['create_auto'](self.b['AutoIn'](type='link_register',name='注册',btn_text='注册',keywords='注册状态',success_kw='成功',ai_flock_enabled=1)))['id']
        self.b['_start_registration_ai_flock'](self.b['conn'].execute('SELECT * FROM automations WHERE id=?',(aid,)).fetchone(),-1002)
        entered=asyncio.Event();release=asyncio.Event()
        async def delay(_): entered.set();await release.wait()
        self.b['_call_ai_any']=AsyncMock(return_value='哈哈真手点的')
        with patch('asyncio.sleep',new=delay):
            task=asyncio.create_task(self.b['_run_ai_flock'](-1002,'脚本？'));await entered.wait()
            self.assertFalse(runtime.ai_flock_busy);release.set();await task
        self.b['cache_snapshot']=lambda:({'-1003'},[]);self.b['_run_ai_flock']=AsyncMock()
        event=SimpleNamespace(is_group=True,out=False,chat_id=-1003,raw_text='你是机器人吗',is_reply=True,
                              get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)),get_reply_message=AsyncMock(side_effect=RuntimeError('offline')))
        await self.b['on_group_message'](event);await asyncio.sleep(0)
        self.b['_run_ai_flock'].assert_awaited_once_with(-1003,'你是机器人吗',False)

    async def test_context_reply_is_short_logged_and_rate_limited(self):
        await self.provider();self.b['conn'].execute("INSERT INTO watched_groups(chat_id,name,added_at) VALUES('-1001','测试群',0)");self.b['conn'].commit()
        for key,value in {'water_ai_enabled':'1','water_ai_groups':'-1001','water_ai_start':'09:00','water_ai_end':'11:00','water_ai_prompt':'像普通群友'}.items():self.b['set_setting'](key,value)
        self.b['_call_ai_any']=AsyncMock(return_value='确实，今天挺热的')
        with patch('asyncio.sleep',new=AsyncMock()): await self.b['_run_ai_flock'](-1001,'今天真热')
        runtime=self.b['get_runtime']();runtime.client.send_message.assert_awaited_once_with(-1001,'确实，今天挺热的',parse_mode=None)
        self.assertNotIn('我的旧消息',self.b['_call_ai_any'].await_args.args[0])
        self.assertEqual(self.b['conn'].execute("SELECT action,reply FROM hits ORDER BY id DESC").fetchone()['action'],'ai_flock')
        await self.b['_run_ai_flock'](-1001,'第二条');runtime.client.send_message.assert_awaited_once()


if __name__=='__main__': unittest.main()
