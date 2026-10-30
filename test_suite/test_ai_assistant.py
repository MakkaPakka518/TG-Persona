"""AI assistant tests: two in-memory account DBs and fake AI only."""
import asyncio
from contextvars import ContextVar
import json
import sqlite3
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_backend import load_backend
from test_task_management import FIXTURE
from ai_assistant import build_ai_assistant_router, parse_object, redact

CURRENT=ContextVar('assistant_test_account',default='a')
class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.bs={key:load_backend() for key in ('a','b')}
        for b in self.bs.values():
            b['conn'].close();b['conn']=sqlite3.connect(':memory:',check_same_thread=False)
            b['conn'].row_factory=sqlite3.Row;b['db_init']()
        self.reply=json.dumps({'type':'signin','name':'测试任务','target':'@dummy','command':'/start','button_delay':3})
        self.requests=[];self.ai_error=None
        async def fake_ai(cfg,content,**kw):
            self.requests.append((cfg,content,kw))
            if self.ai_error:raise self.ai_error
            return self.reply
        self.shared={'ai_base':'https://example.invalid/v1','ai_key':'dummy-provider-secret','ai_model':'fixture-model'}
        def settings(body):
            cfg={key:getattr(body,'ai_'+key,None) or self.shared['ai_'+key] for key in ('base','key','model')}
            return cfg,True,False
        app=FastAPI()
        @app.middleware('http')
        async def account(req,next_app):
            token=CURRENT.set(req.headers.get('x-test-account','a'))
            try:return await next_app(req)
            finally:CURRENT.reset(token)
        app.include_router(build_ai_assistant_router(lambda:self.bs[CURRENT.get()]['conn'],FIXTURE['AutoIn'],FIXTURE['_valid_auto'],FIXTURE['_auto_vals'],FIXTURE['_AUTO_COLS'],settings,fake_ai,lambda e,k:str(e).replace(k,'[已脱敏]'),self.shared.get))
        self.client=TestClient(app)
        self.block=patch('socket.socket.connect',side_effect=AssertionError('real network forbidden'));self.block.start()
    def tearDown(self):
        self.block.stop();self.client.close()
        for b in self.bs.values():b['conn'].close()
    def task(self,account='a',**extra):
        b=self.bs[account];m=FIXTURE['AutoIn'](name='假任务',target='@dummy',command='/start',**extra)
        cols=FIXTURE['_AUTO_COLS'];vals=FIXTURE['_auto_vals'](m)
        cur=b['conn'].execute('INSERT INTO automations('+cols+') VALUES('+','.join('?' for _ in vals)+')',vals);b['conn'].commit();return cur.lastrowid
    def draft(self):return self.client.post('/api/ai/task-draft',json={'text':'每天给 @dummy 发 /start','ai_key':''})
    def test_json_wrappers_and_unknown_fields(self):
        self.reply='说明\n```json\n'+json.dumps({'type':'signin','name':'A','target':'@dummy','command':'/start','system_secret':'ignore','enabled':1})+'\n```\n结束'
        response=self.draft();self.assertEqual(response.status_code,200,response.text)
        d=response.json();self.assertTrue(d['ok']);self.assertTrue(d['warnings']);self.assertEqual(d['draft']['enabled'],0)
        self.assertNotIn('system_secret',d['draft']);self.assertEqual(self.bs['a']['conn'].execute('SELECT COUNT(*) FROM automations').fetchone()[0],0)
        self.assertEqual(self.requests[0][0]['key'],'dummy-provider-secret')
    def test_invalid_delay_and_type_rejected(self):
        for patch_value in ({'button_delay':121},{'command_delay':-1},{'type':'unknown'},{'target':''}):
            self.reply=json.dumps({'type':'signin','name':'A','target':'@dummy','command':'/start',**patch_value})
            self.assertEqual(self.draft().status_code,422)
    def test_custom_steps_array_and_zero_delay(self):
        self.reply=json.dumps({'type':'custom','name':'A','target':'@dummy','custom_steps':[{'t':'send','v':'/start'},{'t':'wait','v':0},{'t':'until','success':'完成','timeout':10000}]})
        response=self.draft();self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(json.loads(response.json()['draft']['custom_steps'])[1]['v'],0)
    def test_captcha_step_not_generated_and_no_tg_calls(self):
        self.reply=json.dumps({'type':'custom','name':'A','target':'@dummy','custom_steps':[{'t':'captcha','v':4}]})
        self.assertEqual(self.draft().status_code,422)
    def test_ai_exception_redacted_and_no_change(self):
        self.ai_error=RuntimeError('Unauthorized dummy-provider-secret')
        d=self.draft().json();self.assertFalse(d['ok']);self.assertNotIn('dummy-provider-secret',d['error'])
    def test_empty_or_malformed_model_output(self):
        for value in ('','not JSON','{"type":"signin"}'):
            self.reply=value;self.assertEqual(self.draft().status_code,422)
    def test_missing_task_is_404_without_ai(self):
        response=self.client.post('/api/ai/diagnose',json={'id':999});self.assertEqual(response.status_code,404);self.assertEqual(self.requests,[])
    def test_diagnose_only_current_task_redacts_values_before_request(self):
        aid=self.task();bid=self.task('b')
        self.bs['a']['add_hit']('signin',False,rule_id=-aid,content='captcha FICTION123 phone +19457455379',reply='Bearer abcsecret sk-abcdefghijk FICTION123',detail={'captcha':'FICTION123','error':'dummy-provider-secret','image':'data:image/png;base64,AAA'})
        self.bs['b']['add_hit']('signin',False,rule_id=-bid,reply='OTHER_ACCOUNT_SECRET')
        self.reply=json.dumps({'analysis':'应该增加等待时间','suggestions':[{'field':'button_delay','value':4,'reason':'当前回复较晚'}]})
        d=self.client.post('/api/ai/diagnose',json={'id':aid}).json()
        self.assertTrue(d['ok']);self.assertEqual(len(d['timeline']),1)
        prompt=self.requests[0][1]
        for secret in ('FICTION123','19457455379','dummy-provider-secret','OTHER_ACCOUNT_SECRET','sk-abcdefghijk','data:image'):
            self.assertNotIn(secret,prompt)
        self.assertIn('****5379',prompt);self.assertEqual(d['suggestions'][0]['value'],4)
        self.assertEqual(self.bs['a']['conn'].execute('SELECT button_delay FROM automations').fetchone()[0],2)
    def test_suggestion_allowlist_and_log_limit(self):
        aid=self.task()
        for n in range(25):self.bs['a']['add_hit']('signin',False,rule_id=-aid,content='x'*200,reply='y'*400)
        self.reply=json.dumps({'analysis':'测试分析','suggestions':[{'field':'enabled','value':1},{'field':'button_delay','value':999},{'field':['bad'],'value':1},{'field':'win_start','value':'25:00'},{'field':'command_delay','value':3,'reason':'建议'}]})
        response=self.client.post('/api/ai/diagnose',json={'id':aid,'lines':3});d=response.json()
        self.assertTrue(d['ok'],d);self.assertEqual(len(d['suggestions']),1);self.assertEqual(len(d['timeline']),3)
        self.assertLessEqual(len(d['timeline'][0]['content']),120);self.assertLessEqual(len(d['timeline'][0]['reply']),200)
        self.assertEqual(self.client.post('/api/ai/diagnose',json={'id':aid,'lines':41}).status_code,422)
    def test_no_history_is_explicit_in_evidence(self):
        aid=self.task();self.reply=json.dumps({'analysis':'缺少执行记录，请先手动执行一次','suggestions':[]})
        d=self.client.post('/api/ai/diagnose',json={'id':aid}).json();self.assertTrue(d['ok']);self.assertEqual(d['timeline'],[])
        self.assertIn('"timeline": []',self.requests[0][1])
    def test_busy_requests_share_one_guard(self):
        import threading
        entered=threading.Event();release=threading.Event()
        original=self.reply
        async def slow(cfg,content,**kw):
            entered.set()
            while not release.is_set():await asyncio.sleep(.01)
            return original
        # Build a small separate router to hold the actual asynchronous lock.
        settings=lambda body:({'key':'fake','base':'https://example.invalid','model':'m'},True,False)
        app=FastAPI();app.include_router(build_ai_assistant_router(lambda:self.bs['a']['conn'],FIXTURE['AutoIn'],FIXTURE['_valid_auto'],FIXTURE['_auto_vals'],FIXTURE['_AUTO_COLS'],settings,slow,lambda e,k:str(e),self.shared.get))
        with TestClient(app) as client:
            responses=[]
            t=threading.Thread(target=lambda:responses.append(client.post('/api/ai/task-draft',json={'text':'test'})));t.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(client.post('/api/ai/diagnose',json={'id':1}).status_code,409)
            finally:release.set();t.join(5)
            self.assertEqual(responses[0].status_code,200)
    def test_model_output_cannot_echo_provider_key(self):
        self.reply=json.dumps({'name':'A','target':'@dummy','command':'dummy-provider-secret'})
        response=self.draft();self.assertEqual(response.status_code,422);self.assertNotIn('dummy-provider-secret',response.text)

if __name__=='__main__':unittest.main()
