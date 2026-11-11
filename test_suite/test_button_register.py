"""群按钮开注：内存库、假 Telegram，验证完整事件到私聊的执行顺序。"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_backend import load_backend, message, HTTPException


class ButtonRegisterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_backend()
        self.b['_running']['paused'] = False
        self.b['set_setting']('ignore_bots', '1')
        self.b['conn'].execute("INSERT INTO watched_groups(chat_id,name) VALUES('-100123','开注群')")
        self.b['conn'].commit()

    async def asyncTearDown(self):
        workers = list(self.b['_auto_runtime_state']().auto_workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.b['conn'].close()

    async def create(self, **changes):
        values = dict(type='button_register', name='开注公告', announcement_kw='已开启定时注册',
                      announcement_button='点击注册', btn_text='注册', keywords='已进入注册状态',
                      success_kw='注册成功', fail_kw='注册失败', result_timeout=1)
        values.update(changes)
        aid = (await self.b['create_auto'](self.b['AutoIn'](**values)))['id']
        await self.b['save_link_credential'](aid, {'user': 'tester', 'secret': 'test-password'})
        return aid

    def row(self, aid):
        return self.b['conn'].execute('SELECT * FROM automations WHERE id=?', (aid,)).fetchone()

    def event(self, text='已开启定时注册', url='http://t.me/example_bot?start=ignored', title='点击注册', mid=100):
        msg = message(mid, text)
        msg.reply_markup = SimpleNamespace(rows=[SimpleNamespace(buttons=[SimpleNamespace(text=title, url=url)])])
        return SimpleNamespace(message=msg, raw_text=text, id=mid, chat_id=-100123,
                               is_group=True, out=False, sender=SimpleNamespace(bot=True))

    def fake_bot(self, failure='', edited=False):
        self.actions, phase = [], {'value': 'old'}
        ent = SimpleNamespace(bot=True, id=77)
        self.b['resolve_entity'] = AsyncMock(return_value=ent)

        async def click():
            self.actions.append(('click', '注册'))
            phase['value'] = 'ready'
            return SimpleNamespace(message='注册已关闭' if failure == 'popup' else '')

        self.button = SimpleNamespace(text='注册', click=AsyncMock(side_effect=click))

        async def send(peer, text, **kw):
            self.actions.append(('send', peer, text))
            if peer == -100123:
                return message(200, text, out=True)
            if text == 'tester test-password':
                phase['value'] = 'result'
                return message(104, text, out=True)
            phase['value'] = 'card'
            return message(101, text, out=True)

        async def history(peer, **kw):
            if phase['value'] == 'old':
                return [message(90, '注册成功', buttons=[[self.button]])]
            if phase['value'] == 'card':
                return [message(102, '注册已关闭' if failure == 'command' else '欢迎注册',
                                buttons=[] if failure == 'command' else [[self.button]])]
            if phase['value'] == 'ready':
                return [message(102 if edited else 103, '已进入注册状态')]
            return [message(105, '注册失败' if failure == 'result' else '注册成功 tester test-password')]

        self.b['client'] = SimpleNamespace(send_message=AsyncMock(side_effect=send), get_messages=AsyncMock(side_effect=history))
        return ent

    async def test_url_button_ignores_start_and_requires_both_matches(self):
        aid = await self.create()
        a = self.row(aid)
        for url in ('http://t.me/example_bot', 'https://t.me/example_bot?start=anything',
                    'tg://resolve?domain=example_bot&start=anything'):
            self.assertEqual(self.b['_announcement_bot'](self.event(url=url).message, a), 'example_bot')
        for event in (self.event(text='聊天消息'), self.event(title='查看帮助'),
                      self.event(url='https://example.com/example_bot'), self.event(url=None)):
            self.assertIsNone(self.b['_announcement_bot'](event.message, a))
        plain = message(100, '已开启定时注册 https://t.me/example_bot')
        self.assertIsNone(self.b['_announcement_bot'](plain, a))
        self.assertEqual(self.b['_message_links'](self.event().message), [])

    async def test_bot_announcement_triggers_once_and_keeps_listening(self):
        aid = await self.create()
        ent = self.fake_bot(edited=True)
        event = self.event()
        await self.b['on_link_event'](event)
        await asyncio.gather(*list(self.b['_auto_runtime_state']().auto_workers.values()))
        self.assertEqual(self.actions, [('send', ent, '/start'), ('click', '注册'),
                                        ('send', ent, 'tester test-password')])
        self.b['send_notify'].assert_called_once()
        self.b['notify_event'].assert_not_called()
        self.assertEqual(self.row(aid)['enabled'], 1)
        self.assertEqual(self.row(aid)['last_status'], 'success')
        self.b['db_init']()  # 持久化去重：重启后旧公告仍不会再执行。
        await self.b['on_link_event'](event)
        self.assertFalse(self.b['_auto_runtime_state']().auto_workers)
        self.assertEqual(len(self.actions), 3)
        result = await self.b['get_logs'](kind='button_register')
        self.assertNotIn('test-password', json.dumps(result))

    async def test_link_registration_also_accepts_bot_sender(self):
        aid = await self.create(type='link_register')
        event = self.event(text='t.me/example_bot?start=TEST-30-Register_abc', url=None)
        self.b['run_link_register'] = AsyncMock(return_value=True)
        await self.b['on_link_event'](event)
        await asyncio.gather(*list(self.b['_auto_runtime_state']().auto_workers.values()))
        self.b['run_link_register'].assert_awaited_once()
        args = self.b['run_link_register'].await_args
        self.assertEqual(args.args[0]['id'], aid)
        self.assertEqual(args.kwargs['source_chat'], -100123)

    async def test_custom_command_and_optional_delayed_group_reply(self):
        aid = await self.create(command='/register', success_reply_enabled=1,
                                success_reply_text='拿下', success_reply_delay=5)
        ent = self.fake_bot()
        async def wait(seconds):
            self.actions.append(('wait', seconds))
        with patch.object(asyncio, 'sleep', new=AsyncMock(side_effect=wait)):
            self.assertTrue(await self.b['run_button_register'](self.row(aid), 'example_bot', -100123))
        self.assertEqual(self.actions, [('send', ent, '/register'), ('click', '注册'),
                                        ('send', ent, 'tester test-password'), ('wait', 5),
                                        ('send', -100123, '拿下')])

    async def test_closed_or_failed_never_retries_or_notifies(self):
        aid = await self.create(success_reply_enabled=1, success_reply_text='拿下')
        for stage in ('command', 'popup', 'result'):
            with self.subTest(stage=stage):
                self.fake_bot(failure=stage)
                self.assertFalse(await self.b['run_button_register'](self.row(aid), 'example_bot', -100123))
                self.assertLessEqual(self.button.click.await_count, 1)
                self.assertEqual(sum(x[0] == 'send' and x[2] == '/start' for x in self.actions), 1)
                self.assertFalse(any(x[0] == 'send' and x[1] == -100123 for x in self.actions))
                self.b['send_notify'].assert_not_called()
                self.b['notify_event'].assert_not_called()

    async def test_timeout_does_not_use_old_button_or_send_credentials(self):
        aid = await self.create()
        self.fake_bot()
        self.b['client'].get_messages = AsyncMock(return_value=[message(90, '旧注册成功', buttons=[[self.button]])])
        self.assertFalse(await self.b['run_button_register'](self.row(aid), 'example_bot', -100123))
        self.button.click.assert_not_awaited()
        self.assertEqual(len(self.actions), 1)
        self.b['send_notify'].assert_not_called()

    async def test_scope_pause_disabled_and_non_bot_target(self):
        aid = await self.create(groups='-100999')
        for event in (self.event(),):
            await self.b['on_link_event'](event)
        self.assertFalse(self.b['_auto_runtime_state']().auto_workers)
        self.b['conn'].execute("UPDATE automations SET groups='' WHERE id=?", (aid,))
        self.b['_running']['paused'] = True
        await self.b['on_link_event'](self.event())
        self.assertFalse(self.b['_auto_runtime_state']().auto_workers)
        self.b['_running']['paused'] = False
        self.b['conn'].execute('UPDATE automations SET enabled=0 WHERE id=?', (aid,))
        await self.b['on_link_event'](self.event())
        self.assertFalse(self.b['_auto_runtime_state']().auto_workers)
        self.fake_bot()
        self.b['resolve_entity'].return_value = SimpleNamespace(bot=False)
        self.assertFalse(await self.b['run_button_register'](self.row(aid), 'person', -100123))
        self.assertEqual(self.actions, [])

    async def test_validation_defaults_and_duplicate_preserves_options(self):
        aid = await self.create()
        row = self.row(aid)
        self.assertEqual(row['command'], '/start')
        self.assertEqual(row['success_reply_enabled'], 0)
        copy_id = (await self.b['duplicate_auto'](aid))['id']
        self.assertEqual(self.row(copy_id)['announcement_kw'], row['announcement_kw'])
        for changes in ({'announcement_kw': ''}, {'announcement_button': ''},
                        {'success_reply_enabled': 1, 'success_reply_text': ''}):
            with self.assertRaises(HTTPException):
                await self.create(**changes)
