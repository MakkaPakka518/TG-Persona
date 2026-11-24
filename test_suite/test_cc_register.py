"""CC 抢注专用回归：代码提取、任务字段、来源匹配与成功停用。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

class CCReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_suite.test_backend import BackendBase
        self.base = BackendBase
        BackendBase.setUp(self)

    async def asyncTearDown(self):
        await self.base.asyncTearDown(self)

    async def create(self, **changes):
        return await self.base.create(self, **changes)

    def row(self, aid):
        return self.base.row(self, aid)

    def make_cc_task(self, **changes):
        values = dict(name='CC 抢注', type='cc_register', groups='@CcEmby,@CC1036',
                      cc_username='testcc01', cc_email='testcc01@example.com', enabled=1)
        values.update(changes)
        return self.create(**values)

    async def test_extract_cc_codes(self):
        self.assertEqual(self.b['extract_cc_codes']('新码 INV-4C5B6440，备用 REG-ABC_123'),
                         ['INV-4C5B6440', 'REG-ABC_123'])
        self.assertEqual(self.b['extract_cc_codes']('INV-4C5B6440 已被使用'), [])
        self.assertEqual(self.b['extract_cc_codes']('没有邀请码'), [])

    async def test_cc_task_fields_and_credential(self):
        aid = await self.make_cc_task()
        row = self.row(aid)
        self.assertEqual(row['type'], 'cc_register')
        self.assertEqual(row['cc_username'], 'testcc01')
        self.assertEqual(row['cc_email'], 'testcc01@example.com')
        self.b['set_link_credential'](aid, 'testcc01', 'secret-123')
        self.assertEqual(self.b['get_link_credential'](aid), {'u': 'testcc01', 's': 'secret-123'})

    async def test_cc_source_matches_channel_and_sender(self):
        aid = await self.make_cc_task()
        a = self.row(aid)
        event = SimpleNamespace(chat_id=-100123, sender=SimpleNamespace(username='CC1036'),
                                sender_id=777)
        self.assertTrue(await self.b['_cc_source_matches'](a, event, '-100123'))
        event2 = SimpleNamespace(chat_id=-100456, sender=SimpleNamespace(username='someone'),
                                 sender_id=888)
        self.assertFalse(await self.b['_cc_source_matches'](a, event2, '-100456'))

    async def test_cc_level_nested_data(self):
        data = {'code': 200, 'data': {'vip_level': 2}}
        self.assertEqual(self.b['_cc_level'](data), 2)

    async def test_cc_register_once_tries_next_code(self):
        calls = []
        def fake_request(method, url, payload=None, **kwargs):
            calls.append(payload)
            if url.endswith('/api/user/register'):
                code = payload['voucher_code']
                if code == 'INV-BAD':
                    return 400, {'detail': '邀请码无效'}
                return 200, {'code': 200, 'message': 'ok', 'data': {'user_id': 1, 'username': payload['username'], 'access_token': 'tok', 'vip_level': 0}}
            return 200, {'code': 200, 'message': '登录成功', 'data': {'id': 1, 'username': payload['username_or_email'], 'vipLevel': 0, 'access_token': 'tok'}}
        self.b['_web_signin_request'] = Mock(side_effect=fake_request)
        result = self.b['_cc_register_once']('https://example.test', 'testcc01', 'testcc01@example.com',
                                            'secret-123', ['INV-BAD', 'INV-GOOD'])
        self.assertTrue(result['ok'])
        self.assertEqual(result['code'], 'INV-GOOD')
        self.assertIn('注册成功', result['message'])
        self.assertIn('(Lv.0)', result['message'])
        self.assertEqual([p['voucher_code'] for p in calls if 'voucher_code' in p], ['INV-BAD', 'INV-GOOD'])

    async def test_cc_event_success_disables_task(self):
        aid = await self.make_cc_task()
        self.b['set_link_credential'](aid, 'testcc01', 'secret-123')
        self.b['_cc_register_once'] = Mock(return_value={
            'ok': True, 'code': 'INV-4C5B6440', 'level': 0,
            'message': '注册成功！欢迎回来，testcc01 (Lv.0)'})
        self.b['get_runtime']().running['paused'] = False
        event = SimpleNamespace(is_group=False, is_channel=True, out=False, chat_id=-100123,
                                id=5, raw_text='新邀请码 INV-4C5B6440',
                                sender=SimpleNamespace(username='CC1036'), sender_id=777,
                                get_buttons=AsyncMock(return_value=[]))
        await self.b['on_cc_event'](event)
        worker = self.b['get_runtime']().auto_workers.get(aid)
        self.assertIsNotNone(worker)
        await worker
        row = self.row(aid)
        self.assertEqual(row['enabled'], 0)
        self.assertEqual(row['last_status'], 'success')
        self.assertEqual(row['cc_last_code'], 'INV-4C5B6440')
