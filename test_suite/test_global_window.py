"""统一时间/任务图标/上次结果：只用内存数据库与假运行时。"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from test_backend import load_backend, HTTPException


class GlobalWindowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b=load_backend()
        self.now=datetime(2026,10,30,9,0,tzinfo=timezone.utc)
        self.b['now_local']=lambda:self.now
        self.other_plans={(99,'2026-10-30'):1}
        self.b['account_registry']=SimpleNamespace(accounts={
            'default':SimpleNamespace(signin_plans=self.b['signin_plans']),
            'other':SimpleNamespace(signin_plans=self.other_plans)})
        self.b['DEFAULTS']={'signin_global_enabled':'0','signin_global_start':'00:05','signin_global_end':'00:41'}
        self.b['SETTING_KEYS']=list(self.b['DEFAULTS'])+['tz']
    def tearDown(self):self.b['conn'].close()
    async def create(self,**extra):
        return (await self.b['create_auto'](self.b['AutoIn'](name='fixture',target='@dummy',command='/sign',**extra)))['id']
    def row(self,aid):return self.b['conn'].execute('SELECT * FROM automations WHERE id=?',(aid,)).fetchone()
    async def window(self,start='09:00',end='10:00',enabled='1'):
        await self.b['update_settings']({'signin_global_enabled':enabled,'signin_global_start':start,'signin_global_end':end})
    async def test_default_off_preserves_individual_window(self):
        aid=await self.create(win_start='08:00',win_end='12:00')
        self.assertEqual(self.b['_effective_signin_window'](self.row(aid)),('08:00','12:00'))
    async def test_global_overrides_start_and_deadline_but_not_saved_fields(self):
        aid=await self.create(win_start='08:00',win_end='12:00')
        await self.window('09:15','10:30')
        a=self.row(aid)
        self.assertEqual(self.b['_effective_signin_window'](a),('09:15','10:30'))
        self.assertEqual(a['win_start'],'08:00')
        deadline=self.b['_signin_deadline'](a,self.now)
        self.assertEqual(datetime.fromtimestamp(deadline,timezone.utc).strftime('%H:%M'),'10:30')
    async def test_global_outside_window_prevents_old_individual_schedule(self):
        aid=await self.create(win_start='08:00',win_end='12:00')
        await self.window('00:05','00:41')
        self.b['_signin_tick']()
        self.assertNotIn(aid,self.b['_auto_running'])
    async def test_global_window_clears_all_plans_without_cancelling_task(self):
        aid=await self.create()
        self.b['signin_plans'][(aid,'2026-10-30')]=500
        sentinel=object();self.b['_auto_running'][aid]=sentinel
        await self.window()
        self.assertEqual(self.b['signin_plans'],{})
        self.assertEqual(self.other_plans,{})
        self.assertIs(self.b['_auto_running'][aid],sentinel)
        self.b['_auto_running'].clear()
    async def test_invalid_window_does_not_partially_change_settings(self):
        await self.window()
        for data in ({'signin_global_start':'11:00','signin_global_end':'10:00'},{'signin_global_enabled':'2'},{'signin_global_end':'24:00'}):
            with self.subTest(data=data),self.assertRaises(HTTPException):await self.b['update_settings'](data)
        self.assertEqual(self.b['get_setting']('signin_global_start'),'09:00')
        self.assertEqual(self.b['get_setting']('signin_global_enabled'),'1')
    async def test_task_icon_persists_and_copies(self):
        aid=await self.create(icon_url='https://example.invalid/icon.png')
        copied=(await self.b['duplicate_auto'](aid))['id']
        self.assertEqual(self.row(copied)['icon_url'],'https://example.invalid/icon.png')
        for bad in ('javascript:alert(1)','data:image/png;base64,aaa','https://user:password@example.invalid/icon.png','https://[bad'):
            with self.subTest(bad=bad),self.assertRaises(HTTPException):await self.create(icon_url=bad)
    async def test_list_has_effective_window_and_last_result(self):
        aid=await self.create()
        self.b['add_hit']('signin',False,rule_id=-aid,reply='fixture rejection',detail={'error':'rejected'})
        await self.window()
        item=(await self.b['list_autos']())['autos'][0]
        self.assertTrue(item['uses_global_window'])
        self.assertEqual(item['effective_win_start'],'09:00')
        self.assertEqual(item['last_result']['reply'],'fixture rejection')
        self.assertEqual(item['last_result']['ok'],0)
    async def test_global_window_not_applied_to_other_task_types(self):
        aid=await self.create(type='keepalive',reply_text='hi',poll_interval=60)
        await self.window()
        item=(await self.b['list_autos']())['autos'][0]
        self.assertFalse(item['uses_global_window'])
        self.assertEqual(item['effective_win_start'],'08:00')


if __name__=='__main__':unittest.main()
