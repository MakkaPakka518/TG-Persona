"""群诗词签到回归：分三条发送、只认回复自己的消息、只领自己的盲盒、成功后删前两句。"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_backend import load_backend, message


def reply_message(mid, text, reply_to, buttons=None, out=False):
    item = message(mid, text, out=out, buttons=buttons)
    item.reply_to_msg_id = reply_to
    item.sender_id = 900
    return item


class PoemSigninTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_backend()
        self.b['send_notify'] = self.b['notify_event'] = Mock()
        self.sent = []

        async def send(entity, text, **kwargs):
            item = message(100 + len(self.sent), text, out=True)
            item.delete = AsyncMock()
            self.sent.append(item)
            return item

        self.b['client'] = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                                           get_messages=AsyncMock(return_value=[]))
        self.b['resolve_entity'] = AsyncMock(return_value='entity')

    def tearDown(self):
        self.b['conn'].close()

    async def create(self, **changes):
        values = dict(name='诗词签到', type='signin_poem', target='-1001234567890',
                      command='夕阳无限好，', reply_text='只是近黄昏。', keywords='就位',
                      signin_poem_mode=1, click_btn=1, btn_text='盲盒', btn_emoji='🎁',
                      command_delay=1.5, result_timeout=6, poem_box_wait=0,
                      poem_delete_on_success=1)
        values.update(changes)
        return (await self.b['create_auto'](self.b['AutoIn'](**values)))['id']

    def row(self, aid):
        return self.b['conn'].execute('SELECT * FROM automations WHERE id=?', (aid,)).fetchone()

    async def test_three_messages_and_only_own_reply_and_box(self):
        aid = await self.create()
        own_box = SimpleNamespace(text='🎁 打开盲盒', data=b'box_self',
                                  click=AsyncMock(return_value=SimpleNamespace(message='恭喜获得音书币：随机奖励')))
        other_box = SimpleNamespace(text='🎁 打开盲盒', data=b'box_other', click=AsyncMock())
        self.b['client'].get_messages = AsyncMock(return_value=[
            reply_message(201, '🎉 恭喜！幸运盲盒已被领取！', 100, [[own_box]]),
            reply_message(202, '🎉 恭喜！幸运盲盒已被领取！', 999, [[other_box]]),
            reply_message(203, '✨签到成功\n获得积分 2,您的总积分 104\n连续签到 27 天', 102),
        ])
        with patch.object(asyncio, 'sleep', new=AsyncMock()):
            await self.b['_execute_auto'](aid, manual=True)

        self.assertEqual([m.raw_text for m in self.sent], ['夕阳无限好，', '只是近黄昏。', '就位'])
        own_box.click.assert_awaited_once()
        other_box.click.assert_not_awaited()
        self.sent[0].delete.assert_awaited_once()
        self.sent[1].delete.assert_awaited_once()
        self.sent[2].delete.assert_not_awaited()
        row = self.row(aid)
        self.assertEqual(row['last_status'], 'success')
        self.assertEqual(row['signin_poem_mode'], 1)
        detail = self.b['conn'].execute('SELECT detail FROM hits ORDER BY id DESC').fetchone()[0]
        self.assertIn('"mode": "poem_group"', detail)
        self.assertIn('"points": "2"', detail)
        self.assertIn('"box": "success"', detail)
        self.assertIn('"deleted_messages": [100, 101]', detail)
        self.assertIn('"sent_messages": [100, 101, 102]', detail)

    async def test_other_people_box_is_never_clicked(self):
        aid = await self.create(click_btn=1, poem_box_wait=0)
        other_box = SimpleNamespace(text='🎁 打开盲盒', data=b'box_other', click=AsyncMock())
        self.b['client'].get_messages = AsyncMock(return_value=[
            reply_message(202, '🎉 恭喜！幸运盲盒已被领取！', 999, [[other_box]]),
            reply_message(203, '签到成功，获得积分 5', 102),
        ])
        with patch.object(asyncio, 'sleep', new=AsyncMock()):
            await self.b['_execute_auto'](aid, manual=True)

        other_box.click.assert_not_awaited()
        detail = self.b['conn'].execute('SELECT detail FROM hits ORDER BY id DESC').fetchone()[0]
        self.assertIn('"box": "not_found"', detail)

    async def test_delete_can_be_disabled_and_delete_failure_does_not_fail_signin(self):
        aid = await self.create(poem_delete_on_success=0)
        self.b['client'].get_messages = AsyncMock(return_value=[
            reply_message(203, '签到成功，获得积分 1', 102),
        ])
        with patch.object(asyncio, 'sleep', new=AsyncMock()):
            await self.b['_execute_auto'](aid, manual=True)
        self.sent[0].delete.assert_not_awaited()
        self.sent[1].delete.assert_not_awaited()

        aid = await self.create(poem_delete_on_success=1)
        self.sent.clear()
        self.b['client'].get_messages = AsyncMock(return_value=[
            reply_message(303, '签到成功，获得积分 1', 102),
        ])
        with patch.object(asyncio, 'sleep', new=AsyncMock()):
            await self.b['_execute_auto'](aid, manual=True)
        # 删除失败也只记录，不影响成功判定；测试消息本身已带 AsyncMock，这里换成抛错。
        self.b['client'].get_messages = AsyncMock(return_value=[
            reply_message(403, '签到成功，获得积分 1', 102),
        ])
        first = message(100, '夕阳无限好，', out=True)
        first.delete = AsyncMock(side_effect=RuntimeError('no permission'))
        second = message(101, '只是近黄昏。', out=True)
        second.delete = AsyncMock()
        final = message(102, '就位', out=True)
        self.b['client'].send_message = AsyncMock(side_effect=[first, second, final])
        with patch.object(asyncio, 'sleep', new=AsyncMock()):
            await self.b['_execute_auto'](aid, manual=True)
        detail = self.b['conn'].execute('SELECT detail FROM hits ORDER BY id DESC').fetchone()[0]
        self.assertIn('no permission', detail)

    async def test_multi_poem_pairs_choose_one_aligned_pair(self):
        aid = await self.create(command='甲句|乙句', reply_text='丙句|丁句', click_btn=0, poem_delete_on_success=0)
        first = message(100, '乙句', out=True)
        second = message(101, '丁句', out=True)
        final = message(102, '就位', out=True)
        self.b['client'].send_message = AsyncMock(side_effect=[first, second, final])
        self.b['client'].get_messages = AsyncMock(return_value=[reply_message(303, '签到成功', 102)])
        with patch.object(self.b['random'], 'randrange', return_value=1), patch.object(asyncio, 'sleep', new=AsyncMock()):
            await self.b['_execute_auto'](aid, manual=True)
        self.assertEqual([c.args[1] for c in self.b['client'].send_message.await_args_list], ['乙句', '丁句', '就位'])
        detail = self.b['conn'].execute('SELECT detail FROM hits ORDER BY id DESC').fetchone()[0]
        self.assertIn('"poem_index": 2', detail)
        self.assertIn('"poem_total": 2', detail)

    async def test_standard_signin_path_is_unchanged(self):
        aid = await self.create(type='signin', signin_poem_mode=0, reply_text='', keywords='', click_btn=0,
                                command='/checkin', success_kw='签到成功')
        self.sent.clear()
        self.b['client'].get_messages = AsyncMock(return_value=[message(203, '签到成功')])
        with patch.object(asyncio, 'sleep', new=AsyncMock()):
            await self.b['_execute_auto'](aid, manual=True)
        self.assertEqual([m.raw_text for m in self.sent], ['/checkin'])
        self.assertEqual(self.row(aid)['last_status'], 'success')


if __name__ == '__main__':
    unittest.main()
