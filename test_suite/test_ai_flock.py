"""AI 水群：时间段配置、抢注临时会话、上下文生成和启用门禁。"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from test_backend import load_backend, HTTPException, message


class AIFlockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b=load_backend();self.b['now_local']=lambda:datetime(2026,10,30,10,0,tzinfo=timezone.utc)
        self.b['_beijing_now']=lambda:datetime(2026,10,30,18,0,tzinfo=timezone(timedelta(hours=8)))
        runtime=self.b['get_runtime']();runtime.ai_flock_busy=False;runtime.ai_flock_busy_groups=set();runtime.ai_flock_last={};runtime.ai_flock_blocked={}
        runtime.client=SimpleNamespace(get_messages=AsyncMock(return_value=[message(3,'今天真热'),message(2,'是啊'),message(1,'我的旧消息',out=True)]),send_message=AsyncMock(return_value=message(4,out=True)))

    def tearDown(self): self.b['conn'].close()

    async def provider(self):
        return (await self.b['create_ai_provider'](self.b['AIProviderInput'](note='测试',base='https://example.invalid/v1',key='fixture-key',model='vision')))['provider']['id']

    async def test_job_crud_isolation_overlap_and_provider_validation(self):
        provider=await self.provider();db=self.b['conn']
        db.executemany("INSERT INTO watched_groups(chat_id,name,added_at) VALUES(?,?,0)",[('-1','一群'),('-2','二群')]);db.commit()
        model=self.b['AIFlockJobInput']
        first=await self.b['create_ai_flock_job'](model(name='早班',groups='-1',start_time='09:00',end_time='11:00',prompt='自然聊',provider_ids=provider,reaction='👍',context_messages=3,message_chars=50,skill_chars=70,max_tokens=333))
        with self.assertRaises(HTTPException):
            await self.b['create_ai_flock_job'](model(name='冲突',groups='-1,-2',start_time='10:00',end_time='12:00',prompt='聊天',provider_ids=provider))
        second=await self.b['create_ai_flock_job'](model(name='隔离',groups='-2',start_time='10:00',end_time='12:00',prompt='另一个人格',provider_ids=provider))
        source=self.b['_ai_flock_source']('-1');self.assertEqual((source['job_id'],source['provider_ids'],source['reaction']),(first['id'],provider,'👍'))
        self.assertEqual(tuple(source[key] for key in ('context_messages','message_chars','skill_chars','max_tokens')),(3,50,70,333))
        self.assertEqual(len((await self.b['list_ai_flock_jobs']())['jobs']),2)
        with self.assertRaises(HTTPException):
            await self.b['update_ai_flock_job'](second['id'],model(name='坏选择',groups='-2',start_time='10:00',end_time='12:00',prompt='聊天',provider_ids='f'*16))
        with self.assertRaises(HTTPException):
            await self.b['create_ai_flock_job'](model(name='坏预算',groups='-2',start_time='12:00',end_time='13:00',prompt='聊天',context_messages=0))
        self.assertTrue((await self.b['delete_ai_flock_job'](second['id']))['ok'])

    async def test_group_removal_cleans_jobs_learning_and_stale_collectors(self):
        provider=await self.provider();db=self.b['conn'];db.executemany("INSERT INTO watched_groups VALUES(?,?,0)",[('-1','一'),('-2','二')]);db.commit();model=self.b['AIFlockJobInput']
        keep=(await self.b['create_ai_flock_job'](model(name='保留',groups='-1,-2',start_time='09:00',end_time='11:00',prompt='聊',provider_ids=provider)))['id']
        empty=(await self.b['create_ai_flock_job'](model(name='清空',groups='-1',start_time='11:00',end_time='12:00',prompt='聊',provider_ids=provider)))['id']
        self.b['set_setting']('flock_learning_groups','-1,-2');db.executemany("INSERT INTO ai_learning_samples(ts,chat_id,text) VALUES(0,?,?)",[('-1','REMOVED RAW'),('-2','KEEP RAW')]);db.commit();await self.b['remove_group']('-1')
        self.assertEqual((db.execute('SELECT groups,enabled FROM ai_flock_jobs WHERE id=?',(keep,)).fetchone()[:]),('-2',1));self.assertEqual((db.execute('SELECT groups,enabled FROM ai_flock_jobs WHERE id=?',(empty,)).fetchone()[:]),('',0));self.assertEqual(self.b['get_setting']('flock_learning_groups'),'-2')
        self.assertEqual([row[0] for row in db.execute('SELECT text FROM ai_learning_samples')],['KEEP RAW']);db.execute('DELETE FROM ai_learning_samples');db.commit()
        self.b['set_setting']('flock_learning_groups','-1');self.b['set_setting']('flock_deep_learning_enabled','1');self.b['set_setting']('flock_deep_learning_start','2026-10-30T17:00');self.b['set_setting']('flock_deep_learning_end','2026-10-30T19:00')
        event=SimpleNamespace(is_group=True,chat_id=-1,id=1,raw_text='不应采集',via_bot_id=None,fwd_from=None,post=False,get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)))
        await self.b['_collect_deep_incoming'](event);self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_learning_samples').fetchone()[0],0)
        self.b['set_setting']('flock_learning_groups','-2');self.b['set_setting']('flock_learning_enabled','1');self.b['set_setting']('flock_learning_start','17:00');self.b['set_setting']('flock_learning_end','19:00');self.b['set_setting']('flock_learning_provider_ids',provider);db.executemany("INSERT INTO ai_learning_samples(ts,chat_id,text) VALUES(0,?,?)",[('-1','REMOVED SECRET '+str(i)) for i in range(10)]+[('-2','VALID STYLE '+str(i)) for i in range(10)]);db.commit();self.b['_call_ai_any']=AsyncMock(return_value='- safe style')
        with tempfile.TemporaryDirectory() as directory:
            type(self.b['get_runtime']()).skill_file=__import__('pathlib').Path(directory)/'skill.md';await self.b['_learn_ai_flock']();prompt=self.b['_call_ai_any'].await_args.args[0]
        self.assertNotIn('REMOVED SECRET',prompt);self.assertIn('VALID STYLE',prompt)
        db.execute('DELETE FROM watched_groups');db.execute("INSERT INTO watched_groups VALUES('-3','新群',0)");self.b['_reconcile_group_configuration'](db);db.commit();self.assertEqual(db.execute('SELECT enabled FROM ai_flock_jobs WHERE id=?',(keep,)).fetchone()[0],0);self.assertEqual(self.b['get_setting']('flock_learning_groups'),'');self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_learning_samples').fetchone()[0],0)

    async def test_optional_persona_uses_skill_and_combines_when_present(self):
        provider=await self.provider();db=self.b['conn'];db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit();model=self.b['AIFlockJobInput']
        with self.assertRaises(HTTPException):
            await self.b['create_ai_flock_job'](model(name='无风格',groups='-1',start_time='09:00',end_time='11:00',prompt='',provider_ids=provider))
        runtime=self.b['get_runtime']();original=type(runtime).skill_file
        with tempfile.TemporaryDirectory() as directory:
            type(runtime).skill_file=__import__('pathlib').Path(directory)/'skill.md';self.b['_write_skill']('- 主人常用短句')
            try:
                created=await self.b['create_ai_flock_job'](model(name='纯模仿',groups='-1',start_time='09:00',end_time='11:00',prompt='',provider_ids=provider))
                with self.assertRaises(HTTPException):await self.b['update_ai_flock_job'](created['id'],model(name='无预算',groups='-1',start_time='09:00',end_time='11:00',prompt='',provider_ids=provider,skill_chars=0))
                runtime.client.get_messages=AsyncMock(return_value=[message(1,'今天天气不错')]);runtime.client.send_message=AsyncMock(return_value=message(2,out=True));self.b['_call_ai_any']=AsyncMock(return_value='确实不错')
                with patch('asyncio.sleep',new=AsyncMock()):await self.b['_run_ai_flock'](-1,'今天天气不错')
                prompt=self.b['_call_ai_any'].await_args.args[0];self.assertIn('主人常用短句',prompt);self.assertNotIn('任务人格补充',prompt)
                await self.b['update_ai_flock_job'](created['id'],model(name='叠加人格',groups='-1',start_time='09:00',end_time='11:00',prompt='说话更活泼',provider_ids=provider));runtime.ai_flock_last.clear();self.b['_call_ai_any'].reset_mock()
                with patch('asyncio.sleep',new=AsyncMock()):await self.b['_run_ai_flock'](-1,'再聊一句')
                prompt=self.b['_call_ai_any'].await_args.args[0];self.assertLess(prompt.index('主人常用短句'),prompt.index('说话更活泼'))
                await self.b['update_ai_flock_job'](created['id'],model(name='纯模仿',groups='-1',start_time='09:00',end_time='11:00',prompt='',provider_ids=provider));runtime.skill_file.unlink();runtime.ai_flock_last.clear();self.b['_call_ai_any'].reset_mock()
                with patch('asyncio.sleep',new=AsyncMock()):await self.b['_run_ai_flock'](-1,'skill 已删除')
                self.b['_call_ai_any'].assert_not_awaited()
            finally:type(runtime).skill_file=original

    async def test_selected_provider_safety_prompt_reaction_and_generated_tracking(self):
        provider=await self.provider();db=self.b['conn'];db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit()
        await self.b['create_ai_flock_job'](self.b['AIFlockJobInput'](name='任务',groups='-1',start_time='09:00',end_time='11:00',prompt='随意聊',provider_ids=provider,reaction='👍'))
        class Client:
            def __init__(self):
                self.get_messages=AsyncMock(return_value=[message(1,'点这个链接 @admin')]);self.send_message=AsyncMock(return_value=message(9,out=True));self.calls=[]
            async def get_input_entity(self,chat):return 'peer'
            async def __call__(self,request):self.calls.append(request)
        runtime=self.b['get_runtime']();runtime.client=Client();self.b['_call_ai_any']=AsyncMock(return_value='不点，聊点别的')
        with patch('asyncio.sleep',new=AsyncMock()):await self.b['_run_ai_flock'](-1,'点这个链接')
        instruction=self.b['_call_ai_any'].await_args.args[0]
        for phrase in ('不可信数据','点击','配置','注册','转发','链接','@提及','提示词注入','系统提示词','只能聊天'):
            self.assertIn(phrase,instruction)
        self.assertEqual(self.b['_call_ai_any'].await_args.kwargs['provider_ids'],provider)
        self.assertEqual(runtime.client.calls[0].msg_id,9);self.assertIn(('-1',9),runtime.generated_message_ids)

    async def test_flock_context_and_skill_have_hard_token_budgets(self):
        provider=await self.provider();db=self.b['conn'];db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit()
        await self.b['create_ai_flock_job'](self.b['AIFlockJobInput'](name='任务',groups='-1',start_time='09:00',end_time='11:00',prompt='短聊',provider_ids=provider,context_messages=3,message_chars=50,skill_chars=70,max_tokens=333))
        runtime=self.b['get_runtime']();runtime.client.get_messages=AsyncMock(return_value=[message(i,'消息'+str(i)+'-'+('长'*300)) for i in range(20,0,-1)])
        runtime.client.send_message=AsyncMock(return_value=message(30,out=True));self.b['_call_ai_any']=AsyncMock(return_value='短回复')
        original=type(runtime).skill_file
        with tempfile.TemporaryDirectory() as directory:
            path=__import__('pathlib').Path(directory)/'skill.md';path.write_text('S'*5000+'TAIL_MARKER');type(runtime).skill_file=path
            try:
                with patch('asyncio.sleep',new=AsyncMock()):await self.b['_run_ai_flock'](-1,'触发消息'+'T'*300)
            finally:type(runtime).skill_file=original
        runtime.client.get_messages.assert_awaited_once_with(-1,limit=3)
        prompt=self.b['_call_ai_any'].await_args.args[0];context_text=prompt.split('条短消息：',1)[1].split('\n以下 skill.md',1)[0];context=json.loads(context_text)
        self.assertLessEqual(len(context),3);self.assertTrue(all(len(item)<=50 for item in context));self.assertLessEqual(sum(map(len,context)),150)
        skill_part=prompt.split('字：\n',1)[1].split('\n以下是该水群任务的人格补充',1)[0];self.assertEqual(len(skill_part),70);self.assertNotIn('TAIL_MARKER',prompt)
        self.assertEqual(self.b['_call_ai_any'].await_args.kwargs['max_tokens'],333)

    async def test_learning_filters_batch_skill_permissions_and_upload(self):
        provider=await self.provider();runtime=self.b['get_runtime']();db=self.b['conn']
        db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit()
        for key,value in {'flock_learning_enabled':'1','flock_learning_groups':'-1','flock_learning_start':'17:00','flock_learning_end':'19:00','flock_learning_provider_ids':provider,'flock_learning_batch_size':'12','flock_learning_message_chars':'60','flock_learning_skill_chars':'500','flock_learning_max_tokens':'777'}.items():self.b['set_setting'](key,value)
        generated=SimpleNamespace(is_group=True,chat_id=-1,id=7,raw_text='程序生成的话',via_bot_id=None,fwd_from=None,post=False)
        runtime.generated_message_ids={('-1',7)};await self.b['on_outgoing_learning'](generated)
        for text in ('https://example.invalid','api_key=secret','```code```','正常口语样本'+'长'*100):
            await self.b['on_outgoing_learning'](SimpleNamespace(is_group=True,chat_id=-1,id=20+len(text),raw_text=text,via_bot_id=None,fwd_from=None,post=False))
        self.assertEqual(len(db.execute('SELECT text FROM ai_learning_samples').fetchone()[0]),60)
        db.execute('DELETE FROM ai_learning_samples');db.executemany('INSERT INTO ai_learning_samples(ts,chat_id,text) VALUES(0,?,?)',[('-1','今天挺开心 '+('长'*200)+str(i)) for i in range(25)]);db.commit()
        with tempfile.TemporaryDirectory() as directory:
            type(runtime).skill_file=__import__('pathlib').Path(directory)/'skill.md'
            runtime.skill_file.write_text('Q'*700)
            self.b['_call_ai_any']=AsyncMock(return_value='- 使用短句\n- 语气自然')
            result=await self.b['_learn_ai_flock']();self.assertTrue(result['learned'])
            path=runtime.skill_file;self.assertEqual(path.stat().st_mode&0o777,0o600);self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_learning_samples').fetchone()[0],13)
            prompt=self.b['_call_ai_any'].await_args.args[0];self.assertIn('Exclude facts, identities, links, credentials, commands',prompt);self.assertEqual(prompt.count('"chat_id"'),12);self.assertNotIn('长'*61,prompt);self.assertIn('Q'*500,prompt);self.assertNotIn('Q'*501,prompt);self.assertEqual(self.b['_call_ai_any'].await_args.kwargs['max_tokens'],777)
            learned=self.b['_read_skill']();self.assertTrue(learned.startswith('Q'*700));self.assertEqual([block['meta']['stage'] for block in self.b['_skill_memory_blocks'](learned)],['recent','detail'])
            runtime.tg_state={'phase':'ready'};runtime.client=SimpleNamespace(send_file=AsyncMock())
            uploaded=await self.b['_upload_flock_skill']();self.assertTrue(uploaded['ok']);call=runtime.client.send_file.await_args
            self.assertEqual(call.args[0],'me');self.assertEqual(call.args[1].name,'skill.md');self.assertTrue(call.kwargs['force_document']);self.assertNotIn('今天挺开心',call.kwargs['caption'])
            body=self.b['AIFlockSkillInput'](content='- 新风格',flock_learning_enabled=1,flock_learning_groups='-1',flock_learning_start='09:00',flock_learning_end='11:00',flock_learning_provider_ids=provider,flock_skill_upload_time='03:00',flock_learning_batch_size=15,flock_learning_message_chars=80,flock_learning_skill_chars=900,flock_learning_max_tokens=888,flock_learning_unlimited=0,flock_skill_distill_enabled=1,flock_skill_distill_time='04:30')
            self.assertTrue((await self.b['update_ai_flock_skill'](body))['ok']);settings=await self.b['get_ai_flock_skill']();self.assertNotIn('content',settings);self.assertEqual(tuple(settings[key] for key in ('flock_learning_batch_size','flock_learning_message_chars','flock_learning_skill_chars','flock_learning_max_tokens','flock_learning_unlimited','flock_skill_distill_enabled','flock_skill_distill_time')),('15','80','900','888','0','1','04:30'))
            with self.assertRaises(HTTPException):await self.b['update_ai_flock_skill'](self.b['AIFlockSkillInput'](flock_learning_batch_size=9))
            with self.assertRaises(HTTPException):await self.b['update_ai_flock_skill'](self.b['AIFlockSkillInput'](flock_learning_unlimited=2))
            with self.assertRaises(HTTPException):await self.b['update_ai_flock_skill'](self.b['AIFlockSkillInput'](flock_skill_distill_enabled=2))
            with self.assertRaises(HTTPException):await self.b['update_ai_flock_skill'](self.b['AIFlockSkillInput'](flock_skill_distill_time='25:00'))
            class Request:
                async def stream(self):yield '- 导入风格'.encode()
            self.assertEqual((await self.b['import_ai_flock_skill'](Request()))['bytes'],len('- 导入风格'.encode()))
            exported=await self.b['export_ai_flock_skill']();self.assertEqual(exported.body,'- 导入风格'.encode());self.assertEqual(exported.headers['Cache-Control'],'no-store')
            outside=__import__('pathlib').Path(directory)/'outside';outside.write_text('safe');path.unlink();path.symlink_to(outside)
            with self.assertRaises(ValueError):self.b['_write_skill']('overwrite')
            self.assertEqual(outside.read_text(),'safe')

    async def test_skill_one_mib_boundary_and_symlink_safety(self):
        runtime=self.b['get_runtime']()
        with tempfile.TemporaryDirectory() as directory:
            type(runtime).skill_file=__import__('pathlib').Path(directory)/'skill.md';limit=self.b['SKILL_MAX_BYTES']
            self.b['_write_skill']('A'*limit);self.assertEqual(len(self.b['_read_skill']()),limit);self.assertEqual(runtime.skill_file.stat().st_mode&0o777,0o600)
            with self.assertRaises(ValueError):self.b['_write_skill']('A'*(limit+1))
            class Request:
                def __init__(self,payload):self.payload=payload
                async def stream(self):yield self.payload
            self.assertEqual((await self.b['import_ai_flock_skill'](Request(b'B'*limit)))['bytes'],limit)
            with self.assertRaises(HTTPException):await self.b['import_ai_flock_skill'](Request(b'B'*(limit+1)))
            outside=__import__('pathlib').Path(directory)/'outside';outside.write_text('safe');runtime.skill_file.unlink();runtime.skill_file.symlink_to(outside)
            with self.assertRaises(ValueError):self.b['_write_skill']('overwrite')
            self.assertEqual(outside.read_text(),'safe')

    async def test_capacity_failure_stops_paid_retries_before_provider(self):
        await self.provider();runtime=self.b['get_runtime']();db=self.b['conn']
        db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit()
        for key,value in {'flock_learning_enabled':'1','flock_learning_groups':'-1','flock_learning_start':'17:00','flock_learning_end':'19:00'}.items():self.b['set_setting'](key,value)
        db.executemany("INSERT INTO ai_learning_samples(ts,chat_id,text) VALUES(0,'-1',?)",[('样本足够长'+str(i),) for i in range(10)]);db.commit()
        with tempfile.TemporaryDirectory() as directory:
            type(runtime).skill_file=__import__('pathlib').Path(directory)/'skill.md';pinned='P'*(self.b['SKILL_MAX_BYTES']-60*1024);runtime.skill_file.write_text(pinned);self.b['_call_ai_any']=AsyncMock(return_value='- paid')
            with self.assertRaises(ValueError):await self.b['_learn_ai_flock']()
            self.b['_call_ai_any'].assert_not_awaited();self.assertEqual(self.b['get_setting']('flock_learning_enabled'),'0');self.assertEqual(self.b['_read_skill'](),pinned);self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_learning_samples').fetchone()[0],10)
            pinned='W'*(self.b['SKILL_MAX_BYTES']-250*1024);runtime.skill_file.write_text(pinned);self.b['set_setting']('flock_learning_unlimited','1');self.b['set_setting']('flock_deep_learning_enabled','1');self.b['set_setting']('flock_learning_unlimited_start','2026-10-30T17:00');self.b['set_setting']('flock_learning_unlimited_end','2026-10-30T19:00');self.b['set_setting']('flock_deep_learning_start','2026-10-30T17:00');self.b['set_setting']('flock_deep_learning_end','2026-10-30T19:00');self.b['_call_ai_any'].reset_mock()
            with self.assertRaises(ValueError):await self.b['_learn_ai_flock']()
            self.b['_call_ai_any'].assert_not_awaited();self.assertEqual(tuple(self.b['get_setting'](key) for key in ('flock_learning_unlimited','flock_deep_learning_enabled')),('0','0'));self.assertEqual(self.b['_read_skill'](),pinned)
            old=self.b['_skill_memory_block']('- old detail','detail',int(self.b['time'].time())-8*86400);pinned='P'*(self.b['SKILL_MAX_BYTES']-60*1024-len(old));runtime.skill_file.write_text(pinned+old);self.b['set_setting']('flock_skill_distill_enabled','1');self.b['_call_ai_any'].reset_mock()
            with self.assertRaises(ValueError):await self.b['_distill_flock_skill']()
            self.b['_call_ai_any'].assert_not_awaited();self.assertEqual(self.b['get_setting']('flock_skill_distill_enabled'),'0');self.assertEqual(self.b['_read_skill'](),pinned+old)

    async def test_learning_unlimited_uses_hard_caps_and_ignores_normal_budgets(self):
        provider=await self.provider();runtime=self.b['get_runtime']();db=self.b['conn']
        db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit()
        settings={'flock_learning_enabled':'1','flock_learning_groups':'-1','flock_learning_start':'09:00','flock_learning_end':'11:00','flock_learning_provider_ids':provider,'flock_learning_batch_size':'10','flock_learning_message_chars':'40','flock_learning_skill_chars':'500','flock_learning_max_tokens':'100','flock_learning_unlimited':'1','flock_learning_unlimited_start':'2026-10-30T17:00','flock_learning_unlimited_end':'2026-10-30T19:00'}
        for key,value in settings.items():self.b['set_setting'](key,value)
        event=lambda mid,text:SimpleNamespace(is_group=True,chat_id=-1,id=mid,raw_text=text,via_bot_id=None,fwd_from=None,post=False)
        await self.b['on_outgoing_learning'](event(1,'自然长句'+'全'*5000));await self.b['on_outgoing_learning'](event(2,'https://example.invalid/private'));await self.b['on_outgoing_learning'](event(3,'/admin secret'))
        self.assertEqual([len(row[0]) for row in db.execute('SELECT text FROM ai_learning_samples')],[4096])
        db.execute('DELETE FROM ai_learning_samples');samples=[('-1','样本'+str(i)+'-'+('Z'*700)) for i in range(200)];db.executemany('INSERT INTO ai_learning_samples(ts,chat_id,text) VALUES(0,?,?)',samples);db.commit()
        with tempfile.TemporaryDirectory() as directory:
            skill_length=700000;type(runtime).skill_file=__import__('pathlib').Path(directory)/'skill.md';runtime.skill_file.write_text('Q'*skill_length)
            self.b['_call_ai_any']=AsyncMock(return_value='- 保留完整风格规则')
            result=await self.b['_learn_ai_flock']();prompt=self.b['_call_ai_any'].await_args.args[0]
        self.assertEqual(result['samples'],200);self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_learning_samples').fetchone()[0],0)
        self.assertEqual(prompt.count('"chat_id"'),200);self.assertIn('Z'*700,prompt);self.assertEqual(prompt.count('Q'),skill_length)
        self.assertEqual(self.b['_call_ai_any'].await_args.kwargs['max_tokens'],32768)

    async def test_beijing_windows_boundaries_and_auto_disable(self):
        await self.provider();db=self.b['conn'];db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit()
        body=self.b['AIFlockSkillInput'](flock_learning_groups='-1',flock_learning_unlimited=1,
            flock_learning_unlimited_start='2026-10-30T18:00',flock_learning_unlimited_end='2026-10-30T19:00',
            flock_deep_learning_enabled=1,flock_deep_learning_start='2026-10-30T17:00',flock_deep_learning_end='2026-10-30T18:30')
        await self.b['update_ai_flock_skill'](body)
        bj=timezone(timedelta(hours=8));self.assertTrue(self.b['_high_cost_active']('flock_learning_unlimited',datetime(2026,10,30,18,0,tzinfo=bj)))
        self.assertFalse(self.b['_high_cost_active']('flock_learning_unlimited',datetime(2026,10,30,19,0,tzinfo=bj)))
        self.assertTrue(self.b['_high_cost_active']('flock_deep_learning_enabled',datetime(2026,10,30,18,0,tzinfo=bj),'flock_deep_learning'))
        self.b['now_local']=lambda:datetime(2035,1,1,0,0,tzinfo=timezone.utc)
        disabled=self.b['_expire_high_cost_modes'](datetime(2026,10,30,19,0,tzinfo=bj));self.assertEqual(set(disabled),{'flock_learning_unlimited','flock_deep_learning_enabled'})
        self.assertEqual((self.b['get_setting']('flock_learning_unlimited'),self.b['get_setting']('flock_deep_learning_enabled')),('0','0'))
        self.b['_beijing_now']=lambda:datetime(2026,10,30,19,0,tzinfo=bj);self.b['set_setting']('flock_learning_unlimited','1');status=await self.b['get_ai_flock_skill']();self.assertEqual(self.b['get_setting']('flock_learning_unlimited'),'0');self.assertNotIn('content',status)
        with self.assertRaises(HTTPException):await self.b['update_ai_flock_skill'](self.b['AIFlockSkillInput'](flock_learning_unlimited=1,flock_learning_unlimited_start='2026-10-30T19:00',flock_learning_unlimited_end='2026-10-30T18:00'))

    async def test_deep_collects_roles_and_bootstraps_once(self):
        provider=await self.provider();runtime=self.b['get_runtime']();db=self.b['conn'];db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit()
        for key,value in {'flock_learning_groups':'-1','flock_learning_provider_ids':provider,'flock_deep_learning_enabled':'1','flock_deep_learning_start':'2026-10-30T17:00','flock_deep_learning_end':'2026-10-30T19:00'}.items():self.b['set_setting'](key,value)
        event=lambda mid,text,out=False:SimpleNamespace(is_group=True,chat_id=-1,id=mid,raw_text=text,out=out,via_bot_id=None,fwd_from=None,post=False,get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)))
        runtime.generated_message_ids={('-1',3)};await self.b['on_outgoing_learning'](event(1,'主人长消息',True));await self.b['_collect_deep_incoming'](event(2,'群友长消息'));await self.b['on_outgoing_learning'](event(3,'生成消息',True))
        self.assertEqual([tuple(row) for row in db.execute('SELECT text,role FROM ai_learning_samples ORDER BY id')],[('主人长消息','owner'),('群友长消息','group')])
        db.execute('DELETE FROM ai_learning_samples');db.commit()
        history=[event(10,'历史主人',True),event(9,'历史群友')];runtime.client=SimpleNamespace(get_messages=AsyncMock(return_value=history));runtime.tg_state={'phase':'ready'}
        first=await self.b['_bootstrap_deep_learning']();second=await self.b['_bootstrap_deep_learning']()
        self.assertEqual((first['loaded'],first['samples'],second['loaded']),(True,2,False));runtime.client.get_messages.assert_awaited_once_with('-1',limit=200)
        self.assertEqual({row[0] for row in db.execute('SELECT role FROM ai_learning_samples')},{'owner','group'})

    async def test_generated_message_ledger_survives_runtime_cache_and_history(self):
        runtime=self.b['get_runtime']();db=self.b['conn'];db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit()
        for key,value in {'flock_learning_enabled':'1','flock_learning_groups':'-1','flock_learning_start':'17:00','flock_learning_end':'19:00','flock_deep_learning_enabled':'1','flock_deep_learning_start':'2026-10-30T17:00','flock_deep_learning_end':'2026-10-30T19:00'}.items():self.b['set_setting'](key,value)
        sent=message(77,'自动发送',out=True);sent.chat_id=-1;runtime.client=SimpleNamespace(send_message=AsyncMock(return_value=sent));await self.b['_send_generated_message'](-1,'自动发送');runtime.generated_message_ids.clear()
        outgoing=SimpleNamespace(is_group=True,chat_id=-1,id=77,raw_text='自动发送',via_bot_id=None,fwd_from=None,post=False)
        await self.b['on_outgoing_learning'](outgoing);self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_learning_samples').fetchone()[0],0)
        sent.get_sender=AsyncMock(return_value=SimpleNamespace(bot=False));runtime.client=SimpleNamespace(get_messages=AsyncMock(return_value=[sent]));runtime.tg_state={'phase':'ready'}
        await self.b['_bootstrap_deep_learning']();self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_learning_samples').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM generated_messages WHERE chat_id=? AND message_id=?',('-1',77)).fetchone()[0],1)

    async def test_high_cost_context_split_processes_all_and_retains_failures(self):
        provider=await self.provider();runtime=self.b['get_runtime']();db=self.b['conn']
        db.execute("INSERT INTO watched_groups VALUES('-1','群',0)");db.commit()
        for key,value in {'flock_learning_groups':'-1','flock_learning_provider_ids':provider,'flock_learning_unlimited':'1','flock_learning_unlimited_start':'2026-10-30T17:00','flock_learning_unlimited_end':'2026-10-30T19:00'}.items():self.b['set_setting'](key,value)
        rows=[('-1',('FAIL' if i==3 else '样本')+str(i)+'内容足够长','owner' if i%2 else 'group') for i in range(10)];db.executemany('INSERT INTO ai_learning_samples(ts,chat_id,text,role) VALUES(0,?,?,?)',rows);db.commit()
        with tempfile.TemporaryDirectory() as directory:
            type(runtime).skill_file=__import__('pathlib').Path(directory)/'skill.md';runtime.skill_file.write_text('PIN')
            async def model(prompt,*_args,**_kwargs):
                count=prompt.count('"role"')
                if count>1:raise RuntimeError('context_length_exceeded')
                if 'FAIL3' in prompt:raise RuntimeError('provider offline')
                return '- safe style'
            self.b['_call_ai_any']=AsyncMock(side_effect=model);result=await self.b['_learn_ai_flock']()
            self.assertEqual((result['samples'],result['failed_samples']),(9,1));self.assertEqual([tuple(row) for row in db.execute('SELECT text FROM ai_learning_samples')],[('FAIL3内容足够长',)])
            self.assertEqual(len(self.b['_skill_memory_blocks'](self.b['_read_skill']())),18)
        self.assertTrue(self.b['_is_context_length_error'](RuntimeError('AI API 未返回有效 JSON（HTTP 413）')))
        self.assertTrue(self.b['_is_context_length_error'](RuntimeError('context_length_exceeded: maximum context window')))

    async def test_recent_memory_waits_one_day_then_becomes_detail(self):
        await self.provider();runtime=self.b['get_runtime']();now=int(self.b['time'].time());pinned='PINNED\n'
        with tempfile.TemporaryDirectory() as directory:
            type(runtime).skill_file=__import__('pathlib').Path(directory)/'skill.md';runtime.skill_file.write_text(pinned+self.b['_skill_memory_block']('owner: full sanitized message','recent',now-86399))
            self.b['_call_ai_any']=AsyncMock(return_value='- learned detail')
            result=await self.b['_distill_flock_skill']();self.assertFalse(result['changed']);self.b['_call_ai_any'].assert_not_awaited();self.assertIn('owner: full sanitized message',self.b['_read_skill']())
            block=self.b['_skill_memory_blocks'](self.b['_read_skill']())[0];block['meta']['created_at']=now-86400
            runtime.skill_file.write_text(pinned+self.b['_skill_memory_block'](block['body'],'recent',now-86400))
            await self.b['_distill_flock_skill']();blocks=self.b['_skill_memory_blocks'](self.b['_read_skill']());self.assertEqual(blocks[0]['meta']['stage'],'detail');self.assertTrue(self.b['_read_skill']().startswith(pinned))

    async def test_distill_detail_after_seven_days_preserves_pinned_content(self):
        await self.provider();runtime=self.b['get_runtime']();now=int(self.b['time'].time());pinned='# PINNED MANUAL\nkeep exactly\n'
        with tempfile.TemporaryDirectory() as directory:
            type(runtime).skill_file=__import__('pathlib').Path(directory)/'skill.md'
            runtime.skill_file.write_text(pinned+'\n'+self.b['_skill_memory_block']('- detailed style rule with several words','detail',now-8*86400))
            self.b['_call_ai_any']=AsyncMock(return_value='- concise style')
            result=await self.b['_distill_flock_skill']();content=self.b['_read_skill']();blocks=self.b['_skill_memory_blocks'](content)
            self.assertTrue(result['changed']);self.assertTrue(content.startswith(pinned));self.assertEqual([(b['meta']['stage'],b['body']) for b in blocks],[('summary','- concise style')])
            stats=await self.b['get_ai_flock_skill']();self.assertEqual((stats['generated_block_count'],stats['summary_block_count']),(1,1))

    async def test_distill_old_summary_requires_three_fade_passes(self):
        await self.provider();runtime=self.b['get_runtime']();now=int(self.b['time'].time());pinned='MANUAL PIN\n'
        with tempfile.TemporaryDirectory() as directory:
            type(runtime).skill_file=__import__('pathlib').Path(directory)/'skill.md';runtime.skill_file.write_text(pinned+self.b['_skill_memory_block']('- a fairly long summary style rule','summary',now-61*86400))
            self.b['_call_ai_any']=AsyncMock(side_effect=['- shorter summary','- short','- x'])
            await self.b['_distill_flock_skill']();block=self.b['_skill_memory_blocks'](self.b['_read_skill']())[0];self.assertEqual((block['meta']['stage'],block['meta']['fade_count']),('fading',1));self.assertTrue(self.b['_read_skill']().startswith(pinned))
            await self.b['_distill_flock_skill']();block=self.b['_skill_memory_blocks'](self.b['_read_skill']())[0];self.assertEqual(block['meta']['fade_count'],2);self.assertTrue(self.b['_read_skill']().startswith(pinned))
            await self.b['_distill_flock_skill']();self.assertEqual(self.b['_skill_memory_blocks'](self.b['_read_skill']()),[]);self.assertTrue(self.b['_read_skill']().startswith(pinned))

    async def test_distill_failure_keeps_file_and_daily_marker_unset(self):
        await self.provider();runtime=self.b['get_runtime']();now=int(self.b['time'].time())
        with tempfile.TemporaryDirectory() as directory:
            type(runtime).skill_file=__import__('pathlib').Path(directory)/'skill.md';original='PIN\n'+self.b['_skill_memory_block']('- old detail memory','detail',now-8*86400);runtime.skill_file.write_text(original)
            self.b['_call_ai_any']=AsyncMock(side_effect=RuntimeError('offline'))
            with self.assertRaises(RuntimeError):await self.b['_distill_flock_skill']()
            self.assertEqual(self.b['_read_skill'](),original);self.assertEqual(self.b['get_setting']('flock_skill_last_distill_date',''),'')
            self.b['_call_ai_any']=AsyncMock(return_value='- summary')
            await self.b['_distill_flock_skill']();self.assertEqual(self.b['get_setting']('flock_skill_last_distill_date',''),'2026-10-30')

    async def test_scheduled_flock_requires_provider_group_prompt_and_window(self):
        self.b['conn'].execute("INSERT INTO watched_groups(chat_id,name,added_at) VALUES('-1001','测试群',0)");self.b['conn'].commit()
        body={'water_ai_enabled':'1','water_ai_groups':'-1001','water_ai_start':'09:00','water_ai_end':'11:00','water_ai_prompt':'自然聊天'}
        with self.assertRaises(HTTPException): await self.b['update_settings'](body)
        await self.provider();await self.b['update_settings'](body)
        source=self.b['_ai_flock_source']('-1001');self.assertEqual((source['kind'],source['prompt']),('scheduled','自然聊天'))
        self.assertEqual(tuple(source[key] for key in ('context_messages','message_chars','skill_chars','max_tokens')),(6,120,3000,200))
        self.b['now_local']=lambda:datetime(2026,10,30,12,0,tzinfo=timezone.utc)
        self.assertIsNone(self.b['_ai_flock_source']('-1001'))

    async def test_registration_flock_has_independent_prompt_and_absolute_expiry(self):
        with self.assertRaises(HTTPException):
            await self.b['create_auto'](self.b['AutoIn'](type='link_register',name='无 API',btn_text='注册',keywords='注册状态',success_kw='成功',ai_flock_enabled=1))
        await self.provider()
        provider=self.b['_ai_provider_configs']()[0]['id']
        aid=(await self.b['create_auto'](self.b['AutoIn'](type='link_register',name='注册',btn_text='注册',keywords='注册状态',success_kw='成功',
            ai_flock_enabled=1,ai_flock_minutes=5,ai_flock_prompt='注册成功人格',ai_flock_provider_ids=provider,ai_flock_reaction='👍',ai_flock_context_messages=4,ai_flock_message_chars=90,ai_flock_skill_chars=800,ai_flock_max_tokens=444)))['id']
        task=self.b['conn'].execute('SELECT * FROM automations WHERE id=?',(aid,)).fetchone()
        self.assertTrue(self.b['_start_registration_ai_flock'](task,-1002))
        source=self.b['_ai_flock_source']('-1002');self.assertEqual((source['kind'],source['task_id'],source['prompt']),('registration',aid,'注册成功人格'))
        self.assertEqual((source['provider_ids'],source['reaction']),(provider,'👍'))
        self.assertEqual(tuple(source[key] for key in ('context_messages','message_chars','skill_chars','max_tokens')),(4,90,800,444))
        self.assertGreaterEqual(source['expires_at'],int(self.b['time'].time())+299)

    async def test_registration_session_replacement_replaces_old_expiry(self):
        await self.provider();auto=self.b['AutoIn']
        first=(await self.b['create_auto'](auto(type='link_register',name='长',btn_text='注册',keywords='状态',success_kw='成功',ai_flock_enabled=1,ai_flock_minutes=60)))['id'];self.b['_start_registration_ai_flock'](self.b['conn'].execute('SELECT * FROM automations WHERE id=?',(first,)).fetchone(),-9)
        old=self.b['_ai_flock_source'](-9)['expires_at']
        second=(await self.b['create_auto'](auto(type='link_register',name='短',btn_text='注册',keywords='状态',success_kw='成功',ai_flock_enabled=1,ai_flock_minutes=2)))['id'];self.b['_start_registration_ai_flock'](self.b['conn'].execute('SELECT * FROM automations WHERE id=?',(second,)).fetchone(),-9)
        source=self.b['_ai_flock_source'](-9);self.assertEqual(source['task_id'],second);self.assertLess(source['expires_at'],old);self.assertLessEqual(source['expires_at'],int(self.b['time'].time())+121)

    def test_legacy_registration_session_schema_adds_reply_budget(self):
        database=sqlite3.connect(':memory:')
        try:
            database.execute('CREATE TABLE ai_flock_sessions(chat_id TEXT PRIMARY KEY,source_task_id INTEGER,expires_at INTEGER NOT NULL,prompt TEXT NOT NULL)')
            self.b['db_init'](database)
            self.assertTrue({'replies','provider_ids','reaction','context_messages','message_chars','skill_chars','max_tokens'}<={row[1] for row in database.execute('PRAGMA table_info(ai_flock_sessions)')})
            self.assertTrue({'context_messages','message_chars','skill_chars','max_tokens'}<={row[1] for row in database.execute('PRAGMA table_info(ai_flock_jobs)')})
            self.assertIn('role',{row[1] for row in database.execute('PRAGMA table_info(ai_learning_samples)')})
            self.assertTrue({'chat_id','message_id','ts','text'}<={row[1] for row in database.execute('PRAGMA table_info(generated_messages)')})
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
