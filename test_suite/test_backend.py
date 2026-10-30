"""后端无网络回归：仅 AST 装载定义/安全常量，SQLite 仅 :memory:。
不 import main、不执行顶层 DATA/密码/Telethon 初始化、不读取环境或真实 data。
运行：python3 -m unittest discover -s test_suite -p 'test_backend.py' -v
"""
import ast
import asyncio
import base64
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from contextvars import ContextVar
import hashlib
import hmac
import ipaddress
import json
import logging
import math
from pathlib import Path
import random
import re
import secrets
import sqlite3
import struct
import time
from types import SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zlib

SOURCE = Path(__file__).resolve().parents[1] / 'main.py'


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


class Model:
    def __init__(self, **kwargs):
        for name in self.__annotations__:
            setattr(self, name, kwargs.get(name, getattr(type(self), name, None)))


class Response:
    def __init__(self, body, **kwargs):
        self.body, self.cookies = body, {}

    def set_cookie(self, name, value, **kwargs):
        self.cookies[name] = dict(value=value, **kwargs)


class FloodWait(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


class Unauthorized(Exception):
    pass


def load_backend():
    # 只读源代码，绝不求值任何 BASE/DATA/conn/app/环境变量等顶层初始化。
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'), filename=str(SOURCE))
    safe_assignments = {'SESSION_TTL', 'CAPTCHA_TTL', 'AUTH_CACHE_MAX', 'CAPTCHA_CACHE_MAX',
                        'RL_WINDOW', 'RL_MAX', 'RL_BLOCK', 'CAPTCHA_AFTER', '_CAPTCHA_FONT', 'MAX_TEST_IMAGE',
                        '_AUTO_COLS', 'RETRY_BASE', 'GLOBAL_SETTING_KEYS', '_TZ_FALLBACK', '_failure_context'}
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.decorator_list = []
            selected.append(node)
        elif isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id in safe_assignments for t in node.targets):
            selected.append(node)
    names = {name: globals()[name] for name in ('ast', 'asyncio', 'base64', 'OrderedDict', 'datetime',
             'timezone', 'timedelta', 'hashlib', 'hmac', 'ipaddress', 'json', 'math', 'random', 're',
             'secrets', 'sqlite3', 'struct', 'time', 'zlib', 'ContextVar')}
    names.update(BaseModel=Model, Optional=Optional, Field=lambda default=None, **kw: default, Request=SimpleNamespace,
                 HTTPException=HTTPException, JSONResponse=Response, os=SimpleNamespace(environ={}),
                 log=Mock(), errors=SimpleNamespace(FloodWaitError=FloodWait, RPCError=Exception,
                 AuthKeyUnregisteredError=Unauthorized), ZoneInfo=None,
                 _captcha=OrderedDict(), _login_fail=OrderedDict(), _auth_gc_at=0.0,
                 TRUSTED_PROXIES=(), _auto_running={}, signin_plans={}, poll_last={}, auto_last_ts={},
                 _session_alerted=False, tg_state={'phase': 'ready', 'me': {'id': 1}, 'err': None},
                 _running={'paused': True}, client=SimpleNamespace(), notify_event=Mock(), send_notify=Mock())
    aliases = {'db': 'conn', 'auto_tasks': '_auto_running', 'running': '_running',
               'session_alerted': '_session_alerted', 'cache': '_cache', 'dialog_cache': '_dialog_cache'}
    dict_fields = {'cache','dialog_cache','running','tg_state','hits_count','last_hit_ts','flock_buf',
                   'auto_last_ts','signin_plans','poll_last','auto_tasks'}
    class RuntimeFixture:
        id = 'default'
        label = '测试默认账号'
        directory = Path('/tmp/never-read-backend-fixture')
        session_path = '/tmp/never-read-backend-fixture/tg'
        avatar_file = Path('/tmp/never-read-backend-fixture/avatar.jpg')
        def __getattr__(self, name):
            key = aliases.get(name, name)
            default = {} if name in dict_fields else set() if name=='tasks' else False if name in ('handler_registered','session_alerted','closing','closed','stopping') else '' if name=='tg_phone' else None
            return names.setdefault(key, default)
        def __setattr__(self, name, value):
            names[aliases.get(name, name)] = value
        def check_live(self):
            return None
        @asynccontextmanager
        async def lifecycle(self, **kwargs):
            yield self
        def spawn(self, coroutine):
            return asyncio.create_task(coroutine)
    class FixtureAccountError(Exception):
        def __init__(self, status, message):
            self.status=status
            super().__init__(message)
    runtime = RuntimeFixture()
    names.update(get_runtime=lambda:runtime, AccountError=FixtureAccountError)
    code = compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])), str(SOURCE), 'exec')
    # 禁止执行器读文件或创建真实数据库，即使未来代码引入意外初始化也立刻失败。
    with patch('builtins.open', side_effect=AssertionError('测试拒绝文件I/O')), \
         patch.object(sqlite3, 'connect', side_effect=AssertionError('测试拒绝真实SQLite连接')):
        exec(code, names)
    names['conn'] = sqlite3.connect(':memory:')
    names['conn'].row_factory = sqlite3.Row
    names['db_init']()
    names['notify_event'], names['send_notify'] = Mock(), Mock()
    return names


def request(peer='198.51.100.1', scheme='http', **headers):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers, scope={'scheme': scheme}, cookies={})


def message(mid, text='', out=False, buttons=None, photo=None):
    return SimpleNamespace(id=mid, raw_text=text, out=out, photo=photo, media=None,
                           get_buttons=AsyncMock(return_value=buttons or []))


class BackendBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_backend()
        self.now = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
        self.clock = patch.object(time, 'time', return_value=self.now.timestamp())
        self.clock_value = self.clock.start()
        self.b['now_local'] = lambda: datetime.fromtimestamp(time.time(), timezone.utc)
        self.b['client'] = SimpleNamespace(get_entity=AsyncMock(return_value='entity'),
            send_message=AsyncMock(return_value=message(100, out=True)), get_messages=AsyncMock(return_value=[]),
            download_media=AsyncMock(return_value=b'image'), is_user_authorized=AsyncMock(return_value=True))
        self.b['conn'].execute("INSERT INTO settings VALUES ('panel_pw_hash', ?)",
                               (self.b['hash_password']('test-password-123'),))
        self.b['conn'].commit()

    async def asyncTearDown(self):
        tasks = [t for t in self.b['_auto_running'].values() if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.b['conn'].close()
        self.clock.stop()

    def advance(self, seconds):
        self.clock_value.return_value += seconds

    async def create(self, **changes):
        values = dict(name='test', type='signin', target='@dummy', command='/sign', win_start='08:00', win_end='12:00')
        values.update(changes)
        result = await self.b['create_auto'](self.b['AutoIn'](**values))
        return result['id']

    def row(self, aid):
        return self.b['conn'].execute('SELECT * FROM automations WHERE id=?', (aid,)).fetchone()

    async def execute(self, aid, manual=False):
        self.assertTrue(self.b['_queue_auto'](aid, manual))
        task = self.b['_auto_running'][aid]
        await task

    async def test_sessions_strict_ttl_and_cleanup(self):
        token = self.b['create_session']('test', 'test')
        self.assertTrue(self.b['session_ok'](token))
        self.advance(self.b['SESSION_TTL'])
        self.assertFalse(self.b['session_ok'](token))
        self.assertEqual(self.b['conn'].execute('SELECT COUNT(*) FROM panel_sessions').fetchone()[0], 0)

    async def test_sessions_reject_null_and_future(self):
        db = self.b['conn']
        db.executemany('INSERT INTO panel_sessions(token,created) VALUES (?,?)', [('null', None), ('future', int(time.time())+1)])
        db.commit()
        self.assertFalse(self.b['session_ok']('null'))
        self.assertFalse(self.b['session_ok']('future'))

    async def test_proxy_safe_default_and_trusted_chain(self):
        req = request(**{'x-forwarded-for': '192.0.2.99', 'x-forwarded-proto': 'https'})
        self.assertEqual(self.b['client_ip'](req), '198.51.100.1')
        self.assertFalse(self.b['request_https'](req))
        self.b['TRUSTED_PROXIES'] = (ipaddress.ip_network('127.0.0.1/32'), ipaddress.ip_network('10.0.0.0/8'))
        req = request('127.0.0.1', **{'x-forwarded-for': '192.0.2.99, 198.51.100.2, 10.0.0.2', 'x-forwarded-proto': 'https'})
        self.assertEqual(self.b['client_ip'](req), '198.51.100.2')
        self.assertTrue(self.b['request_https'](req))
        req.headers['x-forwarded-for'] = 'bogus, 10.0.0.2'
        self.assertEqual(self.b['client_ip'](req), '127.0.0.1')

    async def test_cookie_https_and_http(self):
        for scheme in ('http', 'https'):
            response = Response({})
            self.b['set_panel_cookie'](response, 'dummy', request(scheme=scheme))
            cookie = response.cookies['panel_token']
            self.assertEqual(cookie['secure'], scheme == 'https')
            self.assertTrue(cookie['httponly'])
            self.assertEqual(cookie['samesite'], 'lax')

    async def test_captcha_png_no_text_once_and_exact_ttl(self):
        result = await self.b['panel_captcha'](request())
        self.assertEqual(set(result), {'image', 'required'})
        png = base64.b64decode(result['image'].split(',')[1])
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertNotIn(b'tEXt', png)
        self.assertNotIn(b'iTXt', png)
        answer = self.b['_captcha']['198.51.100.1'][0]
        self.assertTrue(self.b['check_captcha']('198.51.100.1', answer.lower()))
        self.assertFalse(self.b['check_captcha']('198.51.100.1', answer))
        self.advance(2)
        self.b['gen_captcha']('198.51.100.1')
        answer = self.b['_captcha']['198.51.100.1'][0]
        self.advance(180)
        self.assertFalse(self.b['check_captcha']('198.51.100.1', answer))

    async def test_captcha_wrong_answer_consumes_and_refresh_rate(self):
        self.b['gen_captcha']('test')
        with self.assertRaises(HTTPException) as cm:
            self.b['gen_captcha']('test')
        self.assertEqual(cm.exception.status_code, 429)
        self.assertFalse(self.b['check_captcha']('test', 'WRONG'))
        self.assertNotIn('test', self.b['_captcha'])

    async def test_auth_caches_bounded_and_expire(self):
        self.b['AUTH_CACHE_MAX'] = 3
        for ip in ('a', 'b', 'c'):
            self.b['rl_fail'](ip)
        with self.assertRaises(HTTPException):
            self.b['rl_fail']('d')
        self.assertEqual(len(self.b['_login_fail']), 3)
        self.advance(601)
        self.b['rl_fail']('d')
        self.assertLessEqual(len(self.b['_login_fail']), 3)
        self.b['CAPTCHA_CACHE_MAX'] = 1
        self.b['gen_captcha']('d')
        with self.assertRaises(HTTPException):
            self.b['gen_captcha']('e')
        self.assertEqual(len(self.b['_captcha']), 1)

    async def test_password_change_requires_correct_original(self):
        for body in ({'password': 'new-password-456'}, {'password': 'new-password-456', 'old': 'wrong'}):
            with self.assertRaises(HTTPException) as cm:
                await self.b['change_panel_password'](body, request())
            self.assertEqual(cm.exception.status_code, 403)
        old_token = self.b['create_session']('dummy', 'dummy')
        response = await self.b['change_panel_password']({'password': 'new-password-456', 'old': 'test-password-123'}, request(scheme='https'))
        self.assertTrue(self.b['verify_password']('new-password-456'))
        self.assertFalse(self.b['session_ok'](old_token))
        self.assertTrue(response.cookies['panel_token']['secure'])

    async def test_resolve_numeric_id_and_preserve_links(self):
        for value, expected in (('-100123', -100123), ('123', 123), ('@dummy', '@dummy'), ('https://t.me/dummy', 'https://t.me/dummy')):
            await self.b['resolve_entity'](value)
            self.b['client'].get_entity.assert_awaited_with(expected)

    async def test_health_temporary_failure_not_session_revoked(self):
        self.b['client'].is_user_authorized.side_effect = TimeoutError()
        await self.b['_health_check_once']()
        self.assertEqual(self.b['tg_state']['phase'], 'ready')
        self.assertIsNotNone(self.b['tg_state']['health_error'])
        self.b['send_notify'].assert_not_called()

    async def test_health_explicit_unauthorized_error_rechecks_and_recovers(self):
        self.b['client'].is_user_authorized.return_value = False
        await self.b['_health_check_once']()
        await self.b['_health_check_once']()
        self.assertEqual(self.b['tg_state']['phase'], 'error')
        self.assertEqual(self.b['send_notify'].call_count, 1)
        self.b['client'].is_user_authorized.return_value = True
        await self.b['_health_check_once']()
        self.assertEqual(self.b['tg_state']['phase'], 'ready')
        self.assertIsNone(self.b['tg_state']['err'])
        self.assertFalse(self.b['_session_alerted'])

    async def test_health_known_revoked_exception_vs_rpc_failure(self):
        self.b['client'].is_user_authorized.side_effect = Unauthorized()
        await self.b['_health_check_once']()
        self.assertEqual(self.b['tg_state']['phase'], 'error')
        self.b['client'].is_user_authorized.side_effect = RuntimeError('network')
        await self.b['_health_check_once']()
        self.assertEqual(self.b['send_notify'].call_count, 1)

    async def test_health_does_not_overwrite_logout_during_check(self):
        async def logout_during_check():
            self.b['client'] = None
            self.b['tg_state']['phase'] = 'idle'
            return False
        self.b['client'].is_user_authorized.side_effect = logout_during_check
        await self.b['_health_check_once']()
        self.assertEqual(self.b['tg_state']['phase'], 'idle')
        self.b['send_notify'].assert_not_called()

    async def test_new_keepalive_waits_interval_without_fake_success(self):
        aid = await self.create(type='keepalive', reply_text='hello', poll_interval=86400)
        a = self.row(aid)
        self.assertEqual(a['last_at'], '')
        self.assertEqual(a['last_attempt'], 0)
        self.assertEqual(a['next_run_at'], int(time.time()) + 86400)
        self.b['_keepalive_tick']()
        self.assertNotIn(aid, self.b['_auto_running'])

    async def test_keepalive_failure_short_retry_then_success_interval(self):
        aid = await self.create(type='keepalive', reply_text='hello', poll_interval=86400)
        self.b['run_keepalive'] = AsyncMock(return_value=False)
        await self.execute(aid, manual=True)
        a = self.row(aid)
        self.assertEqual(a['last_at'], '')
        self.assertEqual(a['last_attempt'], int(time.time()))
        self.assertEqual(a['next_run_at'], int(time.time())+60)
        self.assertEqual(a['retry_count'], 1)
        self.b['_keepalive_tick']()
        self.assertNotIn(aid, self.b['_auto_running'])
        self.advance(60)
        self.b['run_keepalive'].return_value = True
        self.b['_keepalive_tick']()
        await self.b['_auto_running'][aid]
        a = self.row(aid)
        self.assertEqual(a['last_at'], str(int(time.time())))
        self.assertEqual(a['next_run_at'], int(time.time())+86400)
        self.assertEqual(a['retry_count'], 0)

    async def test_keepalive_retry_exhaustion_waits_full_interval(self):
        aid = await self.create(type='keepalive', reply_text='hello', poll_interval=86400, retries=1)
        self.b['run_keepalive'] = AsyncMock(return_value=False)
        await self.execute(aid, True)
        self.advance(60)
        await self.execute(aid)
        self.assertEqual(self.row(aid)['retry_count'], 2)
        self.assertEqual(self.row(aid)['next_run_at'], int(time.time())+86400)
        self.b['_keepalive_tick']()
        self.assertNotIn(aid, self.b['_auto_running'])

    async def test_signin_only_success_marks_today_and_failure_capped(self):
        aid = await self.create(retries=1)
        self.b['signin_plans'][(aid, '2026-10-30')] = 480
        self.b['run_signin'] = AsyncMock(return_value=False)
        await self.execute(aid)
        self.assertEqual(self.row(aid)['last_at'], '')
        self.advance(60)
        self.b['_signin_tick']()
        await self.b['_auto_running'][aid]
        self.assertEqual(self.row(aid)['retry_count'], 2)
        self.advance(120)
        self.b['_signin_tick']()
        self.assertNotIn(aid, self.b['_auto_running'])
        self.b['run_signin'].return_value = True
        await self.execute(aid, manual=True)
        self.assertEqual(self.row(aid)['last_at'], '2026-10-30')
        listed = await self.b['list_autos']()
        self.assertTrue(listed['autos'][0]['signed_today'])
        self.advance(86400)
        self.assertFalse((await self.b['list_autos']())['autos'][0]['signed_today'])

    async def test_signin_never_starts_at_or_after_window_end(self):
        aid = await self.create(win_end='09:00')
        self.b['signin_plans'][(aid, '2026-10-30')] = 480
        self.b['_signin_tick']()
        self.assertNotIn(aid, self.b['_auto_running'])
        self.advance(60)
        self.b['_signin_tick']()
        self.assertNotIn(aid, self.b['_auto_running'])

    async def test_signin_window_timeout_is_failure_not_success(self):
        aid = await self.create(win_end='09:01')
        self.b['run_signin'] = AsyncMock(return_value=True)
        self.b['_signin_deadline'] = lambda *_: time.time()-1
        await self.execute(aid)
        self.assertEqual(self.row(aid)['last_at'], '')
        self.assertEqual(self.row(aid)['last_status'], 'unconfirmed')
        self.b['run_signin'].assert_not_awaited()
        self.assertEqual(self.b['conn'].execute("SELECT COUNT(*) FROM hits WHERE ok=0").fetchone()[0], 1)

    async def test_duplicate_update_and_running_conflict(self):
        aid = await self.create(type='keepalive', reply_text='hello', poll_interval=3600)
        self.b['run_keepalive'] = AsyncMock(return_value=True)
        await self.execute(aid, True)
        copied = (await self.b['duplicate_auto'](aid))['id']
        self.assertEqual(self.row(copied)['last_at'], '')
        self.assertEqual(self.row(copied)['next_run_at'], int(time.time())+3600)
        self.b['signin_plans'][(aid, '2026-10-30')] = 500
        await self.b['update_auto'](aid, self.b['AutoIn'](type='keepalive', name='test', target='@other', reply_text='hi', poll_interval=7200))
        self.assertEqual(self.row(aid)['last_at'], '')
        self.assertNotIn((aid, '2026-10-30'), self.b['signin_plans'])
        self.assertEqual(self.row(aid)['next_run_at'], int(time.time())+7200)
        self.b['_auto_running'][aid] = None
        with self.assertRaises(HTTPException) as cm:
            await self.b['update_auto'](aid, self.b['AutoIn'](name='test', target='@dummy', command='/sign'))
        self.assertEqual(cm.exception.status_code, 409)
        self.b['_auto_running'].pop(aid)

    async def test_manual_and_scheduled_execution_cannot_overlap(self):
        aid = await self.create()
        event = asyncio.Event()
        async def hold(_):
            await event.wait()
            return True
        self.b['run_signin'] = hold
        await self.b['run_auto_now'](aid)
        task = self.b['_auto_running'][aid]
        with self.assertRaises(HTTPException) as cm:
            await self.b['run_auto_now'](aid)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertFalse(self.b['_queue_auto'](aid))
        event.set()
        await task
        self.assertEqual(self.row(aid)['last_status'], 'success')
        self.assertTrue(self.b['_running']['paused'])  # 监听暂停不变成自动化全局暂停

    async def test_floodwait_logged_and_backoff_honored(self):
        aid = await self.create(type='keepalive', reply_text='hello', poll_interval=86400)
        self.b['client'].send_message.side_effect = FloodWait(900)
        await self.execute(aid, True)
        a = self.row(aid)
        self.assertEqual(a['last_at'], '')
        self.assertEqual(a['next_run_at'], int(time.time())+901)
        self.assertEqual(self.b['conn'].execute('SELECT COUNT(*) FROM hits WHERE ok=0').fetchone()[0], 1)

    async def test_reply_and_button_helpers_filter_old_and_own(self):
        button = SimpleNamespace(text='签到', click=AsyncMock())
        old = message(90, 'success', buttons=[[button]])
        own = message(120, 'success', out=True, buttons=[[button]])
        fresh = message(110, 'new response')
        self.b['client'].get_messages.return_value = [own, fresh, old]
        text, reply = await self.b['_fetch_reply']('entity', min_id=100)
        self.assertEqual((text, reply.id), ('new response', 110))
        self.assertEqual(await self.b['_find_latest_buttons']('entity', min_id=100), (None, []))
        old.get_buttons.assert_not_awaited()
        own.get_buttons.assert_not_awaited()

    async def test_signin_old_success_does_not_mark_new_attempt_successful(self):
        aid = await self.create(success_kw='success')
        self.b['client'].get_messages.return_value = [message(90, 'success')]
        with patch.object(asyncio, 'sleep', new=AsyncMock()):
            await self.execute(aid, True)
        self.assertEqual(self.row(aid)['last_at'], '')
        self.assertEqual(self.row(aid)['last_status'], 'unconfirmed')
        self.assertEqual(self.b['conn'].execute('SELECT ok FROM hits ORDER BY id DESC').fetchone()[0], 0)

    async def test_signin_new_success_marks_successful(self):
        aid = await self.create(success_kw='success')
        self.b['client'].get_messages.return_value = [message(101, 'success')]
        with patch.object(asyncio, 'sleep', new=AsyncMock()):
            await self.execute(aid, True)
        self.assertEqual(self.row(aid)['last_at'], '2026-10-30')

    async def test_signin_old_button_not_clicked(self):
        aid = await self.create(click_btn=1)
        button = SimpleNamespace(text='签到', click=AsyncMock())
        self.b['client'].get_messages.return_value = [message(90, buttons=[[button]])]
        with patch.object(asyncio, 'sleep', new=AsyncMock()):
            await self.execute(aid, True)
        button.click.assert_not_awaited()
        self.assertEqual(self.row(aid)['last_at'], '')

    async def test_captcha_helper_rejects_old_picture(self):
        self.b['client'].get_messages.return_value = [message(90, photo=object()), message(105, out=True, photo=object())]
        # 只跑一次检查然后超时，绝不真正等待或下载旧图。
        with patch.object(time, 'monotonic', side_effect=[0, 0, 30]), \
             patch.object(asyncio, 'sleep', new=AsyncMock()):
            self.assertEqual(await self.b['_await_signin_captcha']('entity', min_id=100), (None, None))
        self.b['client'].download_media.assert_not_awaited()

    async def test_custom_send_until_does_not_use_old_success(self):
        aid = await self.create(type='custom', custom_steps=json.dumps([{'t': 'send', 'v': '/start'}, {'t': 'until', 'success': 'success', 'timeout': 1}]))
        self.b['client'].get_messages.return_value = [message(90, 'success')]
        with patch.object(time, 'monotonic', side_effect=[0, 2]), \
             patch.object(asyncio, 'sleep', new=AsyncMock()):
            # 直接调用 worker，避免修改 monotonic 影响事件循环计时。
            self.assertFalse(await self.b['run_custom'](self.row(aid)))
        self.assertEqual(self.b['conn'].execute('SELECT ok FROM hits ORDER BY id DESC').fetchone()[0], 0)

    async def test_custom_old_captcha_not_recognized(self):
        aid = await self.create(type='custom', custom_steps=json.dumps([{'t': 'send', 'v': '/start'}, {'t': 'captcha'}]))
        self.b['client'].get_messages.return_value = [message(90, photo=object())]
        self.assertFalse(await self.b['run_custom'](self.row(aid)))
        self.b['client'].download_media.assert_not_awaited()

    async def test_poll_records_probe_min_id_ast(self):
        node = next(n for n in ast.parse(SOURCE.read_text()).body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'poll_loop')
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == '_fetch_reply']
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(k.arg == 'min_id' and isinstance(k.value, ast.Name) and k.value.id == 'min_id' for k in calls[0].keywords))

    async def test_db_migration_adds_attempt_and_preserves_legacy_success(self):
        aid = await self.create()
        self.b['conn'].execute('UPDATE automations SET last_at=?,last_status=? WHERE id=?', ('2026-10-30', 'running', aid))
        self.b['conn'].commit()
        self.b['db_init']()
        a = self.row(aid)
        self.assertEqual(a['last_at'], '2026-10-30')
        self.assertEqual(a['last_status'], 'interrupted')
        self.assertEqual(a['last_attempt'], 0)
        self.assertIn('next_run_at', a.keys())


if __name__ == '__main__':
    unittest.main()
