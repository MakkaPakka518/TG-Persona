"""兑换码任务复用链接注册执行器；仅内存库和假 Telegram 客户端。"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_backend import load_backend, message, HTTPException


class CodeRegisterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_backend()
        self.b['_running']['paused'] = False
        self.b['set_setting']('ignore_bots', '1')

    async def asyncTearDown(self):
        workers = list(self.b['_auto_runtime_state']().auto_workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.b['conn'].close()

    async def create(self, **changes):
        values = dict(type='code_register', name='兑换码', target='@fixed_bot', groups='-100123',
                      btn_text='注册', keywords='已进入注册状态', success_kw='成功',
                      existing_kw='无法启用续期功能', result_timeout=1, link_mode=1, link_limit=4)
        values.update(changes)
        aid = (await self.b['create_auto'](self.b['AutoIn'](**values)))['id']
        await self.b['save_link_credential'](aid, {'user': 'test-user', 'secret': 'test-secret'})
        return aid

    def row(self, aid):
        return self.b['conn'].execute('SELECT * FROM automations WHERE id=?', (aid,)).fetchone()

    def codes(self, text):
        return self.b['_message_register_codes'](message(1, text), '@fixed_bot')

    def test_extracts_plain_codes_in_order_and_deduplicates(self):
        text = ('管理员：\nTEST-30-Register_VZHv8TyzC8\nTEST-30-Register_YDioX4Ye5M\n'
                '`TEST-30-Register_LXOUvVg0XU`\nTEST-30-Register_VZHv8TyzC8')
        extracted = self.codes(text)
        self.assertEqual([x['payload'] for x in extracted], [
            'TEST-30-Register_VZHv8TyzC8', 'TEST-30-Register_YDioX4Ye5M', 'TEST-30-Register_LXOUvVg0XU'])
        self.assertTrue(all(x['bot'] == 'fixed_bot' for x in extracted))
        self.assertEqual(self.b['_message_links'](message(1, text)), [])
        self.assertEqual(self.codes('appeal TEST-30-Other_x TEST-x-Register_x TEST-30-Register_'), [])
        self.assertEqual(self.codes('t.me/other_bot?start=TEST-30-Register_hidden'), [])

    async def test_single_group_and_bot_are_required(self):
        for group in ('', '-100123,-100456', '-100123，-100456', 'all'):
            with self.subTest(group=group), self.assertRaises(HTTPException):
                await self.create(groups=group)
        for target in ('', 'https://t.me/fixed_bot', '@bot name'):
            with self.subTest(target=target), self.assertRaises(HTTPException):
                await self.create(target=target)
        aid = await self.create()
        data = dict(self.row(aid))
        data['groups'] = '-100123,-100456'
        with self.assertRaises(HTTPException):
            await self.b['update_auto'](aid, self.b['AutoIn'](**data))
        self.assertEqual(self.row(aid)['groups'], '-100123')

    async def test_only_bound_group_triggers_and_bot_messages_are_accepted(self):
        aid = await self.create()
        self.b['run_link_register'] = AsyncMock(return_value=True)
        event = SimpleNamespace(message=message(10, 'TEST-30-Register_code'), id=10,
                                chat_id=-100456, is_group=True, out=False, sender=SimpleNamespace(bot=True))
        await self.b['on_link_event'](event)
        self.b['run_link_register'].assert_not_awaited()
        event.chat_id = -100123
        await self.b['on_link_event'](event)
        await asyncio.gather(*list(self.b['_auto_runtime_state']().auto_workers.values()))
        call = self.b['run_link_register'].await_args
        self.assertEqual(call.args[0]['id'], aid)
        self.assertEqual(call.args[1], [{'bot': 'fixed_bot', 'payload': 'TEST-30-Register_code'}])
        self.assertEqual(call.kwargs['source_chat'], -100123)
        self.b['run_link_register'].reset_mock()
        event.out = True
        await self.b['on_link_event'](event)
        event.out = False
        self.b['_running']['paused'] = True
        await self.b['on_link_event'](event)
        self.b['run_link_register'].assert_not_awaited()

    async def test_initializes_unused_bot_then_relays_to_one_success(self):
        aid = await self.create(success_reply_enabled=1, success_reply_text='拿下', success_reply_delay=0)
        phase, actions = {'value': 'new'}, []
        ent = SimpleNamespace(bot=True, id=123)
        self.b['resolve_entity'] = AsyncMock(return_value=ent)
        first, second, third = ['TEST-30-Register_' + suffix for suffix in ('old', 'valid', 'unused')]

        async def send(peer, text, **kwargs):
            actions.append(('send', peer, text))
            phase['value'] = {'/start': 'init', '/start ' + first: 'invalid',
                              '/start ' + second: 'card', 'test-user test-secret': 'result'}.get(text, phase['value'])
            return message({'init': 1, 'invalid': 3, 'card': 5, 'result': 8}[phase['value']], out=True)

        async def click():
            actions.append(('click',))
            phase['value'] = 'ready'

        button = SimpleNamespace(text='注册', click=AsyncMock(side_effect=click))

        async def history(peer, **kwargs):
            return {'new': [], 'init': [message(2, '欢迎')], 'invalid': [message(4, first + ' 注册码已被使用')],
                    'card': [message(6, '请注册', buttons=[[button]])], 'ready': [message(7, '已进入注册状态')],
                    'result': [message(9, '注册成功')]}[phase['value']]

        self.b['client'] = SimpleNamespace(send_message=AsyncMock(side_effect=send), get_messages=AsyncMock(side_effect=history))
        ok = await self.b['run_link_register'](self.row(aid), self.codes('\n'.join((first, second, third))), source_chat=-100123)
        self.assertTrue(ok)
        self.b['resolve_entity'].assert_awaited_once_with('@fixed_bot')
        self.assertEqual(actions, [('send', ent, '/start'), ('send', ent, '/start ' + first),
                                   ('send', ent, '/start ' + second), ('click',),
                                   ('send', ent, 'test-user test-secret'), ('send', -100123, '拿下')])
        self.b['send_notify'].assert_called_once()
        self.b['notify_event'].assert_not_called()
        hit = (await self.b['get_logs'](kind='code_register'))['hits'][0]
        self.assertEqual(hit['action'], 'code_register')
        self.assertNotIn('test-secret', json.dumps(hit))

    async def test_random_mode_uses_preset_bot_and_sends_only_one(self):
        aid = await self.create(link_mode=0)
        ent = SimpleNamespace(bot=True)
        self.b['resolve_entity'] = AsyncMock(return_value=ent)
        self.b['client'] = SimpleNamespace(send_message=AsyncMock(return_value=message(3, out=True)),
                                           get_messages=AsyncMock(return_value=[message(4, 'TEST-30-Register_b 注册码已被使用')]))
        candidates = self.codes('TEST-30-Register_a TEST-30-Register_b')
        candidates[1]['bot'] = 'not-the-configured-bot'
        with patch.object(self.b['random'], 'choice', return_value=candidates[1]):
            await self.b['run_link_register'](self.row(aid), candidates)
        self.b['resolve_entity'].assert_awaited_once_with('@fixed_bot')
        self.b['client'].send_message.assert_awaited_once_with(ent, '/start TEST-30-Register_b')
        self.b['notify_event'].assert_not_called()

    async def test_existing_account_stops_relay_and_remains_quiet(self):
        aid = await self.create()
        self.b['resolve_entity'] = AsyncMock(return_value=SimpleNamespace(bot=True))
        self.b['client'] = SimpleNamespace(send_message=AsyncMock(return_value=message(3, out=True)),
                                           get_messages=AsyncMock(return_value=[message(4, '无法启用续期功能')]))
        await self.b['run_link_register'](self.row(aid), self.codes('TEST-30-Register_a TEST-30-Register_b'))
        self.assertEqual(self.b['client'].send_message.await_count, 1)
        self.b['send_notify'].assert_not_called()
        hit = (await self.b['get_logs'](kind='code_register'))['hits'][0]
        self.assertEqual(hit['detail']['status'], 'existing_account')

    async def test_latest_probe_reads_only_configured_group(self):
        aid = await self.create()
        self.b['resolve_entity'] = AsyncMock(return_value='single-group')
        self.b['client'] = SimpleNamespace(get_messages=AsyncMock(return_value=[message(10, 'TEST-30-Register_x')]))
        self.assertEqual(await self.b['_latest_link_probe'](self.row(aid)), self.codes('TEST-30-Register_x'))
        self.b['resolve_entity'].assert_awaited_once_with('-100123')
