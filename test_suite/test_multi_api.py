"""多账号端到端回归：临时复制代码，假 Telegram，禁止任何网络连接。"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class MultiAccountAPITests(unittest.TestCase):
    @unittest.skipUnless(all(importlib.util.find_spec(name) for name in ('fastapi','telethon','httpx')), '需要已声明接口测试依赖')
    def test_accounts_and_task_config_are_isolated(self):
        with tempfile.TemporaryDirectory(prefix='multi-api-env-') as home:
            env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':home,
                 'LD_LIBRARY_PATH':str(Path(sys.executable).resolve().parent.parent/'lib'),
                 'PANEL_PASSWORD':'Panel-fixture-password-123','TRUSTED_PROXIES':''}
            result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--probe'],env=env,
                                  capture_output=True,text=True,timeout=120)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)


def probe():
    import asyncio
    from contextlib import ExitStack
    import json
    import logging
    import socket
    import time
    from types import SimpleNamespace
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory(prefix='tg-multi-api-') as tmp:
        base=Path(tmp)
        required=('main.py','updater.py','VERSION','account_runtime.py','task_management.py','icon_library.py')
        for name in required:
            source=ROOT/name
            if not source.exists():raise AssertionError('新增模块尚未完成：'+name)
            (base/name).write_bytes(source.read_bytes())
        (base/'web').mkdir();(base/'web/index.html').write_text('isolated test fixture')
        os.environ['PANEL_PASSWORD']='Panel-fixture-password-123'
        os.environ['TRUSTED_PROXIES']=''
        sys.path.insert(0,str(base))
        import main
        import account_runtime
        logging.getLogger('httpx').setLevel(logging.WARNING)
        actors=[]
        class FakeClient:
            def __init__(self,*args,**kwargs):
                self.path=str(args[0]);self.authorized=False;self.phone='';self.sent=[];self.handlers=[]
                self.uid=1000+len(actors);self.msg=100;self.disconnected=asyncio.Event();actors.append(self)
            def is_connected(self):return not self.disconnected.is_set()
            async def connect(self):pass
            async def disconnect(self):self.disconnected.set()
            async def is_user_authorized(self):return self.authorized
            async def send_code_request(self,phone,*args,**kwargs):
                self.phone=phone;return SimpleNamespace(phone_code_hash='dummy-code-hash')
            async def sign_in(self,*args,**kwargs):
                if args and str(args[0]).isdigit():self.phone=str(args[0])
                self.authorized=True;return await self.get_me()
            async def get_me(self):return SimpleNamespace(id=self.uid,first_name='fixture',last_name='',username='fixture_'+str(self.uid),phone=self.phone)
            async def get_dialogs(self,*args,**kwargs):return []
            async def download_profile_photo(self,*args,**kwargs):return None
            def add_event_handler(self,handler,event):self.handlers.append(handler)
            def remove_event_handler(self,*args,**kwargs):pass
            async def run_until_disconnected(self):await self.disconnected.wait()
            async def log_out(self):self.authorized=False;self.disconnected.set()
            async def get_entity(self,peer):return peer
            async def send_message(self,ent,text,*args,**kwargs):
                await asyncio.sleep(0.01);self.sent.append((ent,text));self.msg+=2
                return SimpleNamespace(id=self.msg)
            async def get_messages(self,*args,**kwargs):
                return [SimpleNamespace(id=self.msg+1,raw_text='签到成功',out=False,photo=None,document=None,edit_date=None)] if self.sent else []
            async def download_media(self,*args,**kwargs):raise AssertionError('不允许真实图片识别')
        def no_network(*args,**kwargs):raise AssertionError('测试禁止真实网络')
        count=0
        def check(condition,message):
            nonlocal count
            if not condition:raise AssertionError(message)
            count+=1
        def body(response,status=200):
            if response.status_code!=status:raise AssertionError('接口状态异常：'+str(response.status_code)+' '+response.text[:300])
            return response.json()
        with ExitStack() as stack:
            stack.enter_context(patch.object(main,'TelegramClient',FakeClient))
            if hasattr(account_runtime,'TelegramClient'):stack.enter_context(patch.object(account_runtime,'TelegramClient',FakeClient))
            stack.enter_context(patch.object(socket.socket,'connect',no_network))
            stack.enter_context(patch('urllib.request.urlopen',side_effect=no_network))
            with TestClient(main.app) as panel:
                check(panel.get('/api/accounts').status_code==401,'多账号管理未登录必须拒绝')
                body(panel.post('/api/panel/login',json={'password':'Panel-fixture-password-123'}))
                initial=body(panel.get('/api/accounts'))
                check(any(a['id']=='default' for a in initial['accounts']),'旧账号必须作为default保留')
                a=body(panel.post('/api/accounts',json={'label':'账号甲'}))['id']
                b=body(panel.post('/api/accounts',json={'label':'账号乙'}))['id']
                check(a!=b and a!='default' and b!='default','新增账号ID唯一')
                ha={'X-Account-ID':a};hb={'X-Account-ID':b}
                common={'command':'/start','command_delay':0,'button_delay':0,'success_kw':'签到成功','enabled':0}
                ta=body(panel.post('/api/autos',headers=ha,json={**common,'name':'甲的任务','target':'@alpha'}))['id']
                tb=body(panel.post('/api/autos',headers=hb,json={**common,'name':'乙的任务','target':'@beta'}))['id']
                check([t['name'] for t in body(panel.get('/api/autos',headers=ha))['autos']]==['甲的任务'],'账号甲任务隔离')
                check([t['name'] for t in body(panel.get('/api/autos',headers=hb))['autos']]==['乙的任务'],'账号乙任务隔离')
                check(body(panel.get('/api/autos'))['autos']==[],'默认账号任务不混入新账号')
                check(panel.get('/api/autos',headers={'X-Account-ID':'../../outside'}).status_code in (400,404),'拒绝非法账号路径')
                body(panel.post('/api/rules',headers=ha,json={'keywords':'hello','action':'reply','value':'hi'}))
                check(body(panel.get('/api/rules',headers=hb))['rules']==[],'规则按账号隔离')
                body(panel.post('/api/groups',headers=ha,json={'chat_id':'-100123','name':'测试群'}))
                check(body(panel.get('/api/watched',headers=hb))['groups']==[],'监听群按账号隔离')
                body(panel.post('/api/settings',headers=ha,json={'api_id':'12345','api_hash':'dummy-api-hash-A','ai_key':'dummy-shared-ai-key'}))
                body(panel.post('/api/settings',headers=hb,json={'api_id':'54321','api_hash':'dummy-api-hash-B'}))
                check(body(panel.get('/api/settings',headers=ha))['api_hash']=='dummy-api-hash-A','甲的TG凭据不被乙覆盖')
                check(body(panel.get('/api/settings',headers=hb))['api_hash']=='dummy-api-hash-B','乙的TG凭据独立')
                check(body(panel.get('/api/settings',headers=hb))['ai_key']=='dummy-shared-ai-key','AI配置保持共享')
                snapshot=body(panel.get('/api/tasks/export',headers=ha))
                encoded=json.dumps(snapshot,ensure_ascii=False)
                check(snapshot['format']=='tg-persona-config','导出格式标识正确')
                check('dummy-api-hash-A' not in encoded and 'dummy-shared-ai-key' not in encoded,'导出不包含任何API密钥')
                check(snapshot['automations'][0]['name']=='甲的任务','导出只含当前账号任务')
                check('last_at' not in snapshot['automations'][0] and 'id' not in snapshot['automations'][0],'备份不携带运行状态或数据库主键')
                before=len(body(panel.get('/api/autos',headers=hb))['autos'])
                imp={'data':snapshot,'mode':'merge','preview':True,'activate':False}
                preview=body(panel.post('/api/tasks/import',headers=hb,json=imp))
                check(preview['preview'] is True,'先预览而不是直接覆盖')
                check(len(body(panel.get('/api/autos',headers=hb))['autos'])==before,'导入预览不修改任务')
                body(panel.post('/api/tasks/import',headers=hb,json={**imp,'preview':False}))
                imported=body(panel.get('/api/autos',headers=hb))['autos']
                check(len(imported)==before+1,'导入仅添加到当前账号乙')
                check(next(t for t in imported if t['name']=='甲的任务')['enabled']==0,'导入默认禁用任务避免自动执行')
                check(len(body(panel.get('/api/autos',headers=ha))['autos'])==1,'导入到乙不会修改甲')
                batch={'ids':[ta],'action':'update','patch':{'win_start':'09:00','win_end':'11:00'},'preview':True}
                body(panel.post('/api/autos/batch',headers=ha,json=batch))
                check(body(panel.get('/api/autos',headers=ha))['autos'][0]['win_start']=='08:00','批量预览不会保存时间')
                body(panel.post('/api/autos/batch',headers=ha,json={**batch,'preview':False}))
                check(body(panel.get('/api/autos',headers=ha))['autos'][0]['win_start']=='09:00','批量修改保存账号甲时间')
                check(next(t for t in body(panel.get('/api/autos',headers=hb))['autos'] if t['id']==tb)['win_start']=='08:00','同ID不会跨账号修改乙的时间')
                # 两个账户发送登录码后交错确认，必须分别使用各自手机号和客户端。
                body(panel.post('/api/tg/init',headers=ha,json={'api_id':12345,'api_hash':'dummy-api-hash-A'}))
                body(panel.post('/api/tg/init',headers=hb,json={'api_id':54321,'api_hash':'dummy-api-hash-B'}))
                body(panel.post('/api/tg/send_code',headers=ha,json={'phone':'+19457455379'}))
                body(panel.post('/api/tg/send_code',headers=hb,json={'phone':'+19457455380'}))
                body(panel.post('/api/tg/verify_code',headers=ha,json={'code':'11111'}))
                body(panel.post('/api/tg/verify_code',headers=hb,json={'code':'22222'}))
                sa=body(panel.get('/api/state',headers=ha));sb=body(panel.get('/api/state',headers=hb))
                check(sa['logged_in'] and sb['logged_in'],'多个账号保持同时登录')
                check(sa['tg']['me']['phone']=='19457455379','甲使用自己的登录状态')
                check(sb['tg']['me']['phone']=='19457455380','乙使用自己的登录状态')
                check(len({actor.path for actor in actors})>=2,'会话文件路径按账号隔离')
                body(panel.post('/api/autos/'+str(ta)+'/run',headers=ha))
                body(panel.post('/api/autos/'+str(tb)+'/run',headers=hb))
                for _ in range(50):
                    la=body(panel.get('/api/logs',headers=ha))['hits'];lb=body(panel.get('/api/logs',headers=hb))['hits']
                    if la and lb:break
                    time.sleep(0.02)
                check(bool(la) and bool(lb),'两账号各自产生任务日志')
                check(all(hit['chat_id']=='@alpha' for hit in la),'甲日志没有乙任务')
                check(all(hit['chat_id']=='@beta' for hit in lb),'乙日志没有甲任务')
                fa=next(actor for actor in actors if actor.phone=='19457455379')
                fb=next(actor for actor in actors if actor.phone=='19457455380')
                check(any(ent=='@alpha' for ent,_ in fa.sent),'甲任务由甲客户端执行')
                check(not any(ent=='@beta' for ent,_ in fa.sent),'甲客户端不执行乙任务')
                check(any(ent=='@beta' for ent,_ in fb.sent),'乙任务由乙客户端执行')
                body(panel.post('/api/tg/logout',headers=hb))
                check(body(panel.get('/api/state',headers=ha))['logged_in'],'退出乙不影响甲')
                check(not body(panel.get('/api/state',headers=hb))['logged_in'],'乙单独退出')
                check(panel.delete('/api/accounts/default').status_code in (400,409),'默认账号不能误删根数据')
                body(panel.delete('/api/accounts/'+b))
                check(body(panel.get('/api/state',headers=ha))['logged_in'],'删除乙不影响甲客户端')
                check(panel.get('/api/autos',headers=hb).status_code in (400,404),'删除账号不可继续读取数据库')
                body(panel.post('/api/panel/password',headers=ha,json={'old':'Panel-fixture-password-123','password':'Changed-fixture-password-456'}))
                check(panel.get('/api/accounts').status_code==200,'从非默认账号改面板密码仍保持当前会话')
        print('多账号完整接口 '+str(count)+' 项通过，全部 Telegram 调用为假客户端')


if __name__=='__main__':
    if '--probe' in sys.argv:probe()
    else:unittest.main()
