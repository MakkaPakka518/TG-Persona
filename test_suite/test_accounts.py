"""Multi-account regression: real main in a temporary root, fake Telegram factory.
No production data, phone, session, AI, OCR or network is used.
Run: python3 -m unittest discover -s test_suite -p test_accounts.py -v
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from telethon import errors
from account_runtime import AccountError, MAX_ACCOUNTS, use_runtime


class FakeTelegramClient:
    instances = []
    authorized = set()
    simultaneous = 0
    max_simultaneous = 0

    def __init__(self, session, api_id, api_hash):
        self.session = session
        self.api_id, self.api_hash = api_id, api_hash
        self.connected = False
        self.handlers = []
        self.codes, self.signins, self.sent = [], [], []
        self.disconnected = asyncio.Event()
        self.need_password = False
        self.logout_calls = 0
        self.__class__.instances.append(self)

    async def connect(self):
        cls = self.__class__
        cls.simultaneous += 1
        cls.max_simultaneous = max(cls.max_simultaneous, cls.simultaneous)
        try:
            await asyncio.sleep(0)
            Path(self.session + '.session').write_bytes(b'NOT A TELEGRAM SESSION')
            self.connected = True
        finally:
            cls.simultaneous -= 1

    async def disconnect(self):
        self.connected = False
        self.disconnected.set()

    async def run_until_disconnected(self):
        await self.disconnected.wait()

    async def is_user_authorized(self):
        return self.session in self.authorized

    async def send_code_request(self, phone):
        await asyncio.sleep(0)
        self.codes.append(phone)
        return SimpleNamespace(phone_code_hash='fake-' + phone)

    async def sign_in(self, phone=None, code=None, password=None):
        await asyncio.sleep(0)
        self.signins.append((phone, code, password))
        if self.need_password and password is None:
            raise errors.SessionPasswordNeededError(request=None)
        self.authorized.add(self.session)
        return await self.get_me()

    async def get_me(self):
        return SimpleNamespace(id=self.instances.index(self) + 1, first_name='Fake', last_name=None,
                               username='', phone='')

    async def get_dialogs(self, limit=200):
        return []

    async def iter_dialogs(self, limit=500):
        for name in ('fake-group',):
            yield SimpleNamespace(id=self.instances.index(self) + 1, name=name, is_group=True,
                                  entity=SimpleNamespace(participants_count=3))

    async def download_profile_photo(self, entity, file):
        Path(file).write_bytes(b'FAKE AVATAR ' + str(self.api_id).encode())

    def add_event_handler(self, callback, event):
        self.handlers.append(callback)

    async def get_entity(self, peer):
        return peer

    async def send_message(self, entity, text):
        await asyncio.sleep(0)
        self.sent.append((entity, text))
        return SimpleNamespace(id=len(self.sent) + 100, out=True)

    async def get_messages(self, entity, limit=10):
        return []

    async def delete_messages(self, entity, ids):
        return None

    async def log_out(self):
        self.logout_calls += 1
        self.authorized.discard(self.session)
        Path(self.session + '.session').unlink(missing_ok=True)
        return True


class AccountsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='persona-accounts-')
        self.base = Path(self.temp.name)
        (self.base / 'data').mkdir(mode=0o700)
        FakeTelegramClient.instances = []
        FakeTelegramClient.authorized = set()
        FakeTelegramClient.simultaneous = FakeTelegramClient.max_simultaneous = 0
        self.guards = [patch.object(socket.socket, 'connect', side_effect=AssertionError('NO NETWORK')),
                       patch.object(socket, 'create_connection', side_effect=AssertionError('NO NETWORK')),
                       patch.object(urllib.request, 'urlopen', side_effect=AssertionError('NO NETWORK')),
                       patch.dict(os.environ, {'PANEL_PASSWORD': 'offline-panel-test', 'TRUSTED_PROXIES': ''}),
                       patch.dict(sys.modules, {'updater': SimpleNamespace(snapshot=Mock(return_value={'ok': True}),
                            fetch_latest=Mock(), start_update=Mock(return_value={'ok': True}),
                            list_backups=Mock(return_value={'backups': []}), cleanup_backups=Mock(return_value={'ok': True}))})]
        for guard in self.guards:
            guard.start()
        self.main = self.load_main()
        self.token = self.main.create_session('offline', 'test')
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app), base_url='http://offline',
                                     cookies={'panel_token': self.token})

    def load_main(self):
        # Import a physical temporary copy: main's BASE/DATA are never real paths.
        destination = self.base / 'main.py'
        shutil.copyfile(ROOT / 'main.py', destination)
        spec = importlib.util.spec_from_file_location('persona_offline_main', destination)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Explicit public factory injection; any accidental real network is blocked.
        module.TelegramClient = FakeTelegramClient
        module._call_ai = AsyncMock(side_effect=AssertionError('NO AI'))
        module.recognize_image_text = AsyncMock(side_effect=AssertionError('NO CHALLENGE'))
        return module

    async def asyncTearDown(self):
        await self.http.aclose()
        if not self.main.account_registry.shutting_down:
            await self.main.account_registry.shutdown()
        for guard in reversed(self.guards):
            guard.stop()
        self.temp.cleanup()

    def rt(self, aid='default'):
        return self.main.account_registry.select(aid)

    async def req(self, method, path, aid=None, **kwargs):
        headers = kwargs.pop('headers', {})
        if aid is not None:
            headers['X-Account-ID'] = aid
        return await self.http.request(method, path, headers=headers, **kwargs)

    async def create(self, label='second'):
        response = await self.req('POST', '/api/accounts', json={'label': label})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['id']

    async def login(self, aid, api_id=123, phone='00000000001', code='00001'):
        self.assertEqual((await self.req('POST', '/api/tg/init', aid, json={'api_id': api_id, 'api_hash': 'offline-fake-hash'})).status_code, 200)
        self.assertEqual((await self.req('POST', '/api/tg/send_code', aid, json={'phone': phone})).status_code, 200)
        self.assertEqual((await self.req('POST', '/api/tg/verify_code', aid, json={'code': code})).status_code, 200)

    async def auto(self, aid, text='hello', **changes):
        body = dict(type='custom', name='offline task', target='@fake', chain_retry=0,
                    custom_steps=json.dumps([{'t': 'send', 'v': text}, {'t': 'wait', 'v': 0}]))
        body.update(changes)
        response = await self.req('POST', '/api/autos', aid, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['id']

    async def test_profiles_contract_and_permissions(self):
        aid = await self.create('  新账号  ')
        self.assertRegex(aid, r'^[0-9a-f]{32}$')
        result = (await self.req('GET', '/api/accounts')).json()
        self.assertEqual(result['default_id'], 'default')
        profile = next(x for x in result['accounts'] if x['id'] == aid)
        self.assertEqual(profile['label'], '新账号')
        self.assertEqual(profile['phase'], 'idle')
        self.assertFalse(profile['logged_in'])
        self.assertEqual(FakeTelegramClient.instances, [])
        directory = self.base / 'data' / 'accounts' / aid
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / 'config.db').stat().st_mode & 0o777, 0o600)
        self.assertEqual((await self.req('PUT', '/api/accounts/' + aid, json={'label': 'renamed'})).status_code, 200)
        self.assertEqual(self.rt(aid).label, 'renamed')

    async def test_selector_rejects_invalid_unknown_after_root_auth(self):
        for aid, status in [('../default', 400), ('', 400), ('DEFAULT', 400), ('f'*32, 404)]:
            response = await self.req('GET', '/api/state', aid)
            self.assertEqual(response.status_code, status, response.text)
        self.http.cookies.clear()
        self.assertEqual((await self.req('GET', '/api/state', '../bad')).status_code, 401)
        self.assertEqual((await self.req('GET', '/api/accounts')).status_code, 401)

    async def test_parallel_clients_login_codes_and_two_factor(self):
        aid = await self.create()
        await asyncio.gather(self.login('default', 123, '00000000001', '00001'),
                             self.login(aid, 456, '00000000002', '00002'))
        first, second = self.rt(), self.rt(aid)
        self.assertIsNot(first.client, second.client)
        self.assertEqual(first.client.signins, [('00000000001', '00001', None)])
        self.assertEqual(second.client.signins, [('00000000002', '00002', None)])
        self.assertNotEqual(first.client.session, second.client.session)
        self.assertGreaterEqual(FakeTelegramClient.max_simultaneous, 2)
        self.assertEqual(len(first.client.handlers), 2)
        self.assertEqual(len(second.client.handlers), 2)
        self.assertTrue(first.watcher_task is not second.watcher_task)
        self.assertTrue(first.health_task is not second.health_task)
        self.assertEqual((Path(second.session_path + '.session')).stat().st_mode & 0o777, 0o600)
        self.assertEqual(second.avatar_file.stat().st_mode & 0o777, 0o600)
        # A separate new profile can enter 2FA without changing either ready client.
        third = await self.create('2fa')
        await self.req('POST', '/api/tg/init', third, json={'api_id': 789, 'api_hash': 'offline-fake-hash'})
        self.rt(third).client.need_password = True
        await self.req('POST', '/api/tg/send_code', third, json={'phone': '00000000003'})
        result = await self.req('POST', '/api/tg/verify_code', third, json={'code': '00003'})
        self.assertTrue(result.json()['need_password'])
        self.assertEqual((await self.req('POST', '/api/tg/verify_password', third, json={'password': 'offline-2fa'})).status_code, 200)
        self.assertEqual(first.tg_state['phase'], 'ready')
        self.assertEqual(second.tg_state['phase'], 'ready')

    async def test_tasks_logs_rules_groups_and_pause_isolated(self):
        aid = await self.create()
        await asyncio.gather(self.login('default', 123), self.login(aid, 456, '00000000002'))
        one, two = await self.auto('default', 'one'), await self.auto(aid, 'two')
        self.assertEqual((one, two), (1, 1))  # same DB IDs must not share runtime locks
        for selected, word in [('default', 'first'), (aid, 'second')]:
            await self.req('POST', '/api/groups', selected, json={'chat_id': word, 'name': word})
            response = await self.req('POST', '/api/rules', selected, json={'keywords': word, 'action': 'reply', 'value': word})
            self.assertEqual(response.status_code, 200, response.text)
            with use_runtime(self.rt(selected)):
                watched, rules = self.main.cache_snapshot()
                self.assertEqual(watched, {word})
                self.assertEqual(rules[0]['keywords'], word)
        responses = await asyncio.gather(self.req('POST', '/api/autos/1/run', 'default'), self.req('POST', '/api/autos/1/run', aid))
        self.assertTrue(all(x.status_code == 200 for x in responses))
        tasks = [task for runtime in (self.rt(), self.rt(aid)) for task in runtime.auto_tasks.values() if task]
        await asyncio.gather(*tasks)
        self.assertEqual(self.rt().client.sent, [('@fake', 'one')])
        self.assertEqual(self.rt(aid).client.sent, [('@fake', 'two')])
        first_logs = (await self.req('GET', '/api/logs', 'default')).json()['hits']
        second_logs = (await self.req('GET', '/api/logs', aid)).json()['hits']
        self.assertIn('one', json.dumps(first_logs))
        self.assertNotIn('two', json.dumps(first_logs))
        self.assertIn('two', json.dumps(second_logs))
        self.assertNotIn('one', json.dumps(second_logs))
        await self.req('POST', '/api/toggle', aid, json={'paused': True})
        self.assertTrue(self.rt(aid).running['paused'])
        self.assertFalse(self.rt().running['paused'])
        await self.req('POST', '/api/logs/clear', aid, json={})
        self.assertTrue((await self.req('GET', '/api/logs', 'default')).json()['hits'])
        self.assertFalse((await self.req('GET', '/api/logs', aid)).json()['hits'])

    async def test_event_callbacks_bind_explicit_owner(self):
        aid = await self.create()
        first, second = self.rt(), self.rt(aid)
        first.client, second.client = object(), object()
        seen = []
        async def event_callback(event):
            await asyncio.sleep(0)
            seen.append((self.main.get_current_account_id(), self.main.get_account_db()))
            self.main.add_hit('reply', True, content=event)
        callback_a = first.bind_event(event_callback, first.client)
        callback_b = second.bind_event(event_callback, second.client)
        # Dispatch A's callback while B is selected and vice versa.
        with use_runtime(second):
            task_a = asyncio.create_task(callback_a('first'))
        with use_runtime(first):
            task_b = asyncio.create_task(callback_b('second'))
        await asyncio.gather(task_a, task_b)
        self.assertEqual({aid for aid, _ in seen}, {'default', aid})
        self.assertEqual(first.db.execute('SELECT content FROM hits').fetchone()[0], 'first')
        self.assertEqual(second.db.execute('SELECT content FROM hits').fetchone()[0], 'second')
        first.client = None
        await callback_a('stale')
        self.assertEqual(first.db.execute('SELECT COUNT(*) FROM hits').fetchone()[0], 1)
        second.client = None

    async def test_logout_cancels_owned_work_not_other_account(self):
        aid = await self.create()
        await asyncio.gather(self.login('default'), self.login(aid, 456, '00000000002'))
        other_client = self.rt().client
        deleted_client = self.rt(aid).client
        owned = self.rt(aid).spawn(asyncio.Event().wait())
        other = self.rt().spawn(asyncio.Event().wait())
        await asyncio.sleep(0)
        self.assertEqual((await self.req('POST', '/api/tg/logout', aid)).status_code, 200)
        self.assertTrue(owned.cancelled())
        self.assertFalse(other.done())
        self.assertEqual(deleted_client.logout_calls, 1)
        self.assertIs(self.rt().client, other_client)
        self.assertTrue(other_client.connected)
        self.assertEqual(self.rt(aid).tg_phone, '')
        self.assertEqual(self.rt(aid).tg_state['phase'], 'idle')
        self.assertTrue(self.main.session_ok(self.token))

    async def test_global_settings_and_panel_sessions_always_root(self):
        aid = await self.create()
        response = await self.req('POST', '/api/settings', aid, json={'api_id': '456', 'api_hash': 'fake-private',
                       'avatar': 'fake-avatar', 'cooldown': '77', 'ai_model': 'fake-model', 'notify_on_fail': '0'})
        self.assertEqual(response.status_code, 200)
        root, profile = self.rt().db, self.rt(aid).db
        self.assertIsNone(root.execute("SELECT value FROM settings WHERE key='api_id'").fetchone())
        self.assertEqual(profile.execute("SELECT value FROM settings WHERE key='api_id'").fetchone()[0], '456')
        self.assertEqual(root.execute("SELECT value FROM settings WHERE key='ai_model'").fetchone()[0], 'fake-model')
        self.assertIsNone(profile.execute("SELECT value FROM settings WHERE key='ai_model'").fetchone())
        self.assertIsNone(profile.execute("SELECT value FROM settings WHERE key='panel_pw_hash'").fetchone())
        self.assertEqual(profile.execute('SELECT COUNT(*) FROM panel_sessions').fetchone()[0], 0)
        with use_runtime(self.rt(aid)):
            self.assertTrue(self.main.verify_password('offline-panel-test'))
            token = self.main.create_session('second', 'second')
            self.assertTrue(self.main.session_ok(token))
        sessions = await self.req('GET', '/api/panel/sessions', '../invalid')
        self.assertEqual(sessions.status_code, 200)
        self.assertEqual(sessions.json()['count'], 2)
        self.assertEqual((await self.req('GET', '/api/state', aid)).json()['sessions'], 2)
        self.assertEqual((await self.req('GET', '/api/version', '../invalid')).status_code, 200)
        self.assertEqual((await self.req('GET', '/api/backups', aid)).status_code, 200)
        self.assertEqual((await self.req('POST', '/api/update', aid, json={})).status_code, 200)
        response = await self.req('POST', '/api/panel/password', aid,
                                  json={'old': 'offline-panel-test', 'password': 'offline-new-password'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.main.verify_password('offline-new-password'))
        self.assertFalse(self.main.session_ok(token))
        self.assertIsNone(profile.execute("SELECT value FROM settings WHERE key='panel_pw_hash'").fetchone())

    async def test_default_legacy_migration_and_restart_persistence(self):
        # Seed a legacy root after closing the initial temporary application.
        await self.http.aclose()
        await self.main.account_registry.shutdown()
        root_path = self.base / 'data' / 'config.db'
        with sqlite3.connect(root_path) as db:
            db.execute("INSERT INTO watched_groups VALUES ('old', 'legacy group', 1)")
            db.execute("INSERT INTO rules(keywords,value,created_at) VALUES ('old', 'legacy value', 1)")
            db.execute("INSERT INTO settings(key,value) VALUES ('api_id','123')")
            db.execute("INSERT INTO settings(key,value) VALUES ('api_hash','offline-fake-hash')")
            stored_hash = db.execute("SELECT value FROM settings WHERE key='panel_pw_hash'").fetchone()[0]
        legacy_session = self.base / 'data' / 'tg.session'
        legacy_session.write_bytes(b'LEGACY FAKE SESSION')
        FakeTelegramClient.authorized.add(str(legacy_session.with_suffix('')))
        self.main = self.load_main()
        self.token = self.main.create_session('offline', 'test')
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app), base_url='http://offline', cookies={'panel_token': self.token})
        aid = await self.create('persistent')
        task_id = await self.auto(aid, 'persisted')
        with use_runtime(self.rt(aid)):
            self.main.set_setting('api_id', '456')
            self.main.set_setting('api_hash', 'offline-fake-hash')
            self.main.add_hit('custom', True, content='persistent-log')
            self.main.get_account_db().execute("UPDATE automations SET next_run_at=9876543210,last_status='running' WHERE id=?", (task_id,))
            self.main.get_account_db().commit()
        (self.rt(aid).directory / 'tg.session').write_bytes(b'SECOND FAKE SESSION')
        FakeTelegramClient.authorized.add(self.rt(aid).session_path)
        # A third profile has credentials but no session: startup must not connect.
        no_session = await self.create('no session')
        with use_runtime(self.rt(no_session)):
            self.main.set_setting('api_id', '789')
            self.main.set_setting('api_hash', 'offline-fake-hash')
        await self.http.aclose()
        await self.main.account_registry.shutdown()
        self.main = self.load_main()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app), base_url='http://offline', cookies={'panel_token': self.token})
        self.assertEqual(self.rt().db.execute("SELECT value FROM settings WHERE key='panel_pw_hash'").fetchone()[0], stored_hash)
        self.assertEqual(self.rt().db.execute('SELECT value FROM rules').fetchone()[0], 'legacy value')
        row = self.rt(aid).db.execute('SELECT * FROM automations').fetchone()
        self.assertEqual(row['last_status'], 'interrupted')
        self.assertEqual(row['next_run_at'], 9876543210)
        self.assertIn('persisted', row['custom_steps'])
        self.assertEqual(self.rt(aid).label, 'persistent')
        self.assertEqual(self.rt(aid).db.execute('SELECT content FROM hits').fetchone()[0], 'persistent-log')
        async with self.main.lifespan(self.main.app):
            self.assertEqual(self.rt().tg_state['phase'], 'ready')
            self.assertEqual(self.rt(aid).tg_state['phase'], 'ready')
            self.assertIsNone(self.rt(no_session).client)
            self.assertNotEqual(self.rt().client.session, self.rt(aid).client.session)
        self.assertTrue(all(runtime.closed for runtime in self.main.account_registry.accounts.values()))

    async def test_delete_waits_request_and_cancels_executing_task(self):
        aid = await self.create()
        await self.login(aid)
        task_id = await self.auto(aid)
        runtime = self.rt(aid)
        started, hold = asyncio.Event(), asyncio.Event()
        async def long_task(row):
            started.set()
            await hold.wait()
        self.main.run_custom = long_task
        with use_runtime(runtime):
            self.assertTrue(self.main._queue_auto(task_id, manual=True))
            task = runtime.auto_tasks[task_id]
            self.assertEqual(self.main.get_running_task_ids(), frozenset({task_id}))
        await started.wait()
        root = self.rt().db
        async with runtime.request_lease():
            deleting = asyncio.create_task(self.main.account_registry.delete(aid))
            await asyncio.sleep(0)
            self.assertFalse(deleting.done())
            with self.assertRaises(AccountError) as error:
                self.main.account_registry.select(aid)
            self.assertEqual(error.exception.status, 409)
        await deleting
        self.assertTrue(task.cancelled())
        self.assertFalse(runtime.directory.exists())
        self.assertTrue(runtime.closed)
        self.assertTrue(self.main.session_ok(self.token))
        self.assertEqual(root.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 1)
        self.assertEqual((await self.req('DELETE', '/api/accounts/default')).status_code, 400)
        self.assertEqual((await self.req('GET', '/api/state', aid)).status_code, 404)

    async def test_symlinks_rejected_without_touching_target(self):
        aid = await self.create()
        runtime = self.rt(aid)
        target = self.base / 'outside'
        target.mkdir()
        marker = target / 'marker'
        marker.write_text('preserve')
        runtime.avatar_file.symlink_to(marker)
        self.assertEqual((await self.req('GET', '/api/state', aid)).status_code, 400)
        self.assertEqual((await self.req('DELETE', '/api/accounts/' + aid)).status_code, 200)
        self.assertEqual(marker.read_text(), 'preserve')
        aid = await self.create('directory link')
        runtime = self.rt(aid)
        original = runtime.directory.with_name('saved')
        runtime.directory.rename(original)
        runtime.directory.symlink_to(target, target_is_directory=True)
        self.assertEqual((await self.req('GET', '/api/state', aid)).status_code, 400)
        self.assertEqual((await self.req('DELETE', '/api/accounts/' + aid)).status_code, 200)
        self.assertFalse(runtime.directory.exists())
        self.assertTrue(original.exists())
        self.assertEqual(marker.read_text(), 'preserve')

    async def test_limit_and_invalid_labels_and_login_stage(self):
        self.assertEqual((await self.req('POST', '/api/accounts', json={'label': '   '})).status_code, 400)
        self.assertEqual((await self.req('POST', '/api/tg/verify_code', json={'code': '00000'})).status_code, 409)
        self.assertEqual((await self.req('POST', '/api/tg/verify_password', json={'password': 'fake'})).status_code, 409)
        for index in range(MAX_ACCOUNTS - 1):
            await self.create('profile-' + str(index))
        self.assertEqual((await self.req('POST', '/api/accounts', json={'label': 'overflow'})).status_code, 400)
        self.assertEqual(len(FakeTelegramClient.instances), 0)


    async def test_custom_zero_wait_and_until_needs_actual_reply(self):
        runtime = self.rt()
        runtime.client = SimpleNamespace(get_entity=AsyncMock(return_value='entity'),
            get_messages=AsyncMock(return_value=[]), send_message=AsyncMock(return_value=SimpleNamespace(id=100)))
        def config(steps):
            return {'id': 1, 'type': 'custom', 'name': 'offline semantics', 'target': '@fake',
                    'chain_retry': 1, 'custom_steps': json.dumps(steps)}
        with patch.object(asyncio, 'sleep', new=AsyncMock()) as sleep:
            self.assertTrue(await self.main.run_custom(config([{'t': 'wait', 'v': 0}])))
            sleep.assert_awaited_once_with(0)
        # No success/fail words still require an actual fresh reply, including a
        # media-only message. No Telegram/client network operation is performed.
        for replies, expected in [([], False), ([SimpleNamespace(id=90, out=False, raw_text='old')], False),
                                  ([SimpleNamespace(id=101, out=False, raw_text='')], True)]:
            runtime.client.get_messages = AsyncMock(side_effect=[[], replies])
            with patch.object(self.main.time, 'monotonic', side_effect=[0, 0, 2]), \
                 patch.object(asyncio, 'sleep', new=AsyncMock()):
                ok = await self.main.run_custom(config([{'t': 'send', 'v': 'fake'}, {'t': 'until', 'timeout': 1}]))
            self.assertEqual(ok, expected)
        runtime.client = None

    async def test_click_until_excludes_menu_but_accepts_current_edit(self):
        runtime = self.rt()
        for edit, expected in [(False, False), (True, True)]:
            button = SimpleNamespace(text='continue', click=AsyncMock())
            menu = SimpleNamespace(id=110, out=False, raw_text='welcome', edit_date=None, photo=None,
                                   get_buttons=AsyncMock(return_value=[[button]]))
            updated = SimpleNamespace(id=110, out=False, raw_text='completed', edit_date='edited', photo=None)
            runtime.client = SimpleNamespace(get_entity=AsyncMock(return_value='entity'),
                get_messages=AsyncMock(side_effect=[[], [menu], [menu], [updated if edit else menu]]))
            steps = [{'t': 'click', 'v': 'continue'}, {'t': 'until', 'timeout': 1}]
            a = {'id': 1, 'type': 'custom', 'name': 'offline click', 'target': '@fake', 'chain_retry': 1,
                 'custom_steps': json.dumps(steps)}
            with patch.object(self.main.time, 'monotonic', side_effect=[0, 0, 2]), \
                 patch.object(asyncio, 'sleep', new=AsyncMock()):
                self.assertEqual(await self.main.run_custom(a), expected)
            button.click.assert_awaited_once()
        runtime.client = None

    async def test_shutdown_stages_all_writers_before_any_disconnect(self):
        aid = await self.create()
        runtimes = [self.rt(), self.rt(aid)]
        stages = []
        async def writer(runtime):
            try:
                await asyncio.Event().wait()
            finally:
                # Every cancelled task can still access its own DB AND root.
                self.main.get_account_db().execute('SELECT COUNT(*) FROM hits').fetchone()
                self.main.get_setting('tz', 'shared-root')
                stages.append('cancel-' + runtime.id)
        async def disconnected(runtime):
            self.assertEqual(len([x for x in stages if x.startswith('cancel-')]), 2)
            for other in runtimes:
                other.db.execute('SELECT COUNT(*) FROM hits').fetchone()
            stages.append('disconnect-' + runtime.id)
        for runtime in runtimes:
            self.assertIsNone(runtime._lifecycle_lock)
            async def on_disconnect(runtime=runtime):
                await disconnected(runtime)
            runtime.client = SimpleNamespace(disconnect=AsyncMock(side_effect=on_disconnect))
            runtime.spawn(writer(runtime))
        await asyncio.sleep(0)
        await self.main.account_registry.shutdown()
        self.assertEqual(len(stages), 4)
        self.assertTrue(all(runtime.closed for runtime in runtimes))
        with self.assertRaises(sqlite3.ProgrammingError):
            self.main.conn.execute('SELECT 1')


    async def test_broken_profiles_restart_and_cleanup_failure_is_orphan_only(self):
        missing, broken, healthy = await self.create('missing'), await self.create('broken'), await self.create('healthy')
        missing_path, broken_path = self.rt(missing).directory, self.rt(broken).directory
        await self.http.aclose()
        await self.main.account_registry.shutdown()
        shutil.rmtree(missing_path)
        (broken_path / 'config.db').write_bytes(b'NOT SQLITE')
        self.main = self.load_main()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app), base_url='http://offline', cookies={'panel_token': self.token})
        accounts = (await self.req('GET', '/api/accounts')).json()['accounts']
        self.assertEqual({x['id'] for x in accounts if x['phase'] == 'error'}, {missing, broken})
        for aid in ('default', healthy):
            self.assertEqual((await self.req('GET', '/api/state', aid)).status_code, 200)
        for aid in (missing, broken):
            self.assertEqual((await self.req('GET', '/api/state', aid)).status_code, 409)
        self.assertEqual((await self.req('DELETE', '/api/accounts/' + missing)).status_code, 200)
        with patch('account_runtime.shutil.rmtree', side_effect=OSError('offline simulated cleanup failure')):
            response = await self.req('DELETE', '/api/accounts/' + broken)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['residual'])
        self.assertIn('账号已注销', response.json()['warning'])
        self.assertTrue(broken_path.exists())
        self.assertEqual((await self.req('GET', '/api/state', broken)).status_code, 404)
        await self.http.aclose()
        await self.main.account_registry.shutdown()
        self.main = self.load_main()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app), base_url='http://offline', cookies={'panel_token': self.token})
        self.assertEqual(set(self.main.account_registry.accounts), {'default', healthy})
        self.assertTrue(self.main.session_ok(self.token))
        self.assertEqual((await self.req('GET', '/api/state', healthy)).status_code, 200)

    async def test_avatar_query_only_and_root_auth_before_selector(self):
        aid = await self.create()
        self.rt().avatar_file.write_bytes(b'default-avatar')
        self.rt(aid).avatar_file.write_bytes(b'second-avatar')
        response = await self.req('GET', '/avatar?account_id=' + aid)
        self.assertEqual(response.content, b'second-avatar')
        self.assertEqual((await self.req('GET', '/avatar')).content, b'default-avatar')
        self.assertEqual((await self.req('GET', '/avatar?account_id=../bad')).status_code, 400)
        self.assertEqual((await self.req('GET', '/avatar?account_id=' + 'f'*32)).status_code, 404)
        # API query parameters must never select another account.
        self.assertFalse((await self.req('GET', '/api/state?account_id=' + aid)).json()['paused'])
        await self.req('POST', '/api/toggle', aid, json={'paused': True})
        self.assertFalse((await self.req('GET', '/api/state?account_id=' + aid)).json()['paused'])
        self.http.cookies.clear()
        self.assertEqual((await self.req('GET', '/avatar?account_id=../bad')).status_code, 401)


if __name__ == '__main__':
    unittest.main()
