"""消息时序/延迟回归，只用假消息与假识别器，不读取真实验证码或连接 TG。"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_backend import load_backend, message, HTTPException


def photo_message(mid, text='', media_id=1, sender=7, buttons=None):
    m=message(mid,text,photo=SimpleNamespace(id=media_id),buttons=buttons)
    m.sender_id=sender;m.edit_date=None;m.reply_markup=None
    return m


class SigninFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b=load_backend()
        self.b['now_local']=lambda: datetime(2026,10,30,9,0,tzinfo=timezone.utc)
        self.events=[];self.phase='before';self.fetch_count=0;self.answer_result='签到成功';self.deliver=True
        outer=self
        async def send(ent,text):
            outer.events.append(('send',text))
            if text=='/start': outer.phase='menu';return SimpleNamespace(id=100)
            outer.phase='result';return SimpleNamespace(id=103)
        async def get(ent,**kwargs):
            outer.fetch_count+=1;outer.events.append(('fetch',outer.phase))
            if outer.phase=='before': return [photo_message(90,'欢迎使用测试机器人',1)]
            if outer.phase in ('menu','clicked'):
                index=outer.fetch_count
                async def click():
                    outer.events.append(('click',index));outer.phase='clicked'
                button=SimpleNamespace(text='签到',click=AsyncMock(side_effect=click))
                return [photo_message(101,'欢迎使用测试机器人，请选择功能',1,buttons=[[button]])]
            if outer.phase=='prompt': return [photo_message(102,'请输入验证码（测试消息）',2),photo_message(101,'欢迎使用测试机器人',1)]
            if outer.phase=='result': return [message(104,outer.answer_result),photo_message(102,'请输入验证码（测试消息）',2)]
            return []
        async def recognize(data,*args):
            outer.events.append(('recognize',None));return 'FIXTURECODE'
        self.b['client']=SimpleNamespace(send_message=AsyncMock(side_effect=send),get_messages=AsyncMock(side_effect=get),download_media=AsyncMock(return_value=b'fake-image-no-real-captcha'))
        self.b['resolve_entity']=AsyncMock(return_value='entity')
        self.b['recognize_image_text']=AsyncMock(side_effect=recognize)
        self.b['send_notify']=self.b['notify_event']=AsyncMock()
        # 通知函数是同步入口，使用普通 mock 避免产生未等待协程。
        from unittest.mock import Mock
        self.b['send_notify']=self.b['notify_event']=Mock()

    def tearDown(self): self.b['conn'].close()

    async def create(self,**values):
        body=self.b['AutoIn'](name='fixture',target='@dummy',command='/start',click_btn=1,signin_captcha=1,
            **values)
        return (await self.b['create_auto'](body))['id']

    def row(self,aid): return self.b['conn'].execute('SELECT * FROM automations WHERE id=?',(aid,)).fetchone()

    async def sleep(self,seconds):
        self.events.append(('sleep',seconds))
        if self.phase=='clicked' and self.deliver and seconds==self.button_delay: self.phase='prompt'

    async def run_flow(self,aid):
        self.button_delay=self.row(aid)['button_delay']
        with patch.object(asyncio,'sleep',new=AsyncMock(side_effect=self.sleep)):
            await self.b['_execute_auto'](aid,manual=True)

    async def test_configured_delays_are_applied_in_order(self):
        aid=await self.create(command_delay=3,button_delay=5)
        await self.run_flow(aid)
        actions=[x for x in self.events if x[0]!='fetch']
        self.assertEqual(actions[:6],[('send','/start'),('sleep',3.0),actions[2],('sleep',5.0),('recognize',None),('send','FIXTURECODE')])
        self.assertEqual(actions[2][0],'click')
        self.assertEqual(self.row(aid)['last_status'],'success')
        chosen=self.b['client'].download_media.call_args.args[0]
        self.assertEqual(chosen.id,102)
        detail=self.b['conn'].execute('SELECT detail FROM hits ORDER BY id DESC').fetchone()[0]
        self.assertIn('"captcha_message_id": 102',detail)

    async def test_zero_delays_are_preserved_and_executed(self):
        aid=await self.create(command_delay=0,button_delay=0)
        await self.run_flow(aid)
        self.assertEqual([s for kind,s in self.events if kind=='sleep'][:2],[0.0,0.0])
        self.assertEqual(self.row(aid)['last_status'],'success')

    async def test_fixed_mode_does_not_fetch_between_clicks(self):
        aid=await self.create(click_times=3,refresh_buttons=0)
        await self.run_flow(aid)
        indices=[i for i,x in enumerate(self.events) if x[0]=='click']
        self.assertEqual(len(indices),3)
        self.assertEqual(len({self.events[i][1] for i in indices}),1)
        self.assertFalse(any(x[0]=='fetch' for x in self.events[indices[0]+1:indices[-1]]))

    async def test_refresh_mode_uses_latest_button_each_time(self):
        aid=await self.create(click_times=3,refresh_buttons=1)
        await self.run_flow(aid)
        clicks=[x[1] for x in self.events if x[0]=='click']
        self.assertEqual(len(set(clicks)),3)

    async def test_only_welcome_picture_never_enters_recognition(self):
        aid=await self.create();self.deliver=False
        await self.run_flow(aid)
        self.b['recognize_image_text'].assert_not_awaited()
        self.b['client'].download_media.assert_not_awaited()
        self.assertEqual(self.b['client'].send_message.call_count,1)
        self.assertEqual(self.row(aid)['last_at'],'')

    async def test_known_rejection_is_failure_even_without_fail_keyword(self):
        aid=await self.create();self.answer_result='验证码错误！'
        await self.run_flow(aid)
        self.assertEqual(self.row(aid)['last_at'],'')
        self.assertEqual(self.row(aid)['last_status'],'failed')
        self.assertEqual(self.b['conn'].execute('SELECT ok FROM hits ORDER BY id DESC').fetchone()[0],0)

    async def test_unconfirmed_result_not_marked_success(self):
        aid=await self.create();self.answer_result='请输入验证码'
        await self.run_flow(aid)
        self.assertEqual(self.row(aid)['last_status'],'failed');self.assertEqual(self.row(aid)['last_at'],'')

    async def test_custom_success_keyword_is_respected(self):
        aid=await self.create(success_kw='积分到账');self.answer_result='积分到账'
        await self.run_flow(aid);self.assertEqual(self.row(aid)['last_status'],'success')

    async def test_caption_only_edit_cannot_turn_logo_into_new_picture(self):
        old=photo_message(101,'欢迎使用测试机器人',1)
        baseline={101:self.b['_message_signature'](old)}
        old.raw_text='请输入验证码（测试消息）'
        self.b['client'].get_messages=AsyncMock(return_value=[old])
        with patch.object(asyncio,'sleep',new=AsyncMock()):
            self.assertEqual(await self.b['_await_signin_captcha']('entity',0.01,101,baseline),(None,None))
        self.b['client'].download_media.assert_not_awaited()

    async def test_new_media_on_same_message_is_detected(self):
        old=photo_message(101,'欢迎使用测试机器人',1)
        baseline={101:self.b['_message_signature'](old)}
        edited=photo_message(101,'请输入验证码（测试消息）',2)
        self.b['client'].get_messages=AsyncMock(return_value=[edited])
        result=await self.b['_await_signin_captcha']('entity',1,101,baseline)
        self.assertEqual(result[0].id,101)
        self.b['client'].download_media.assert_awaited_once_with(edited,file=bytes)

    async def test_delayed_caption_rechecked_instead_of_permanently_ignored(self):
        initial=photo_message(102,'',2)
        updated=photo_message(102,'请输入验证码（测试消息）',2)
        self.b['client'].get_messages=AsyncMock(side_effect=[[initial],[updated]])
        with patch.object(asyncio,'sleep',new=AsyncMock()):
            result=await self.b['_await_signin_captcha']('entity',1,101)
        self.assertEqual(result[0].id,102)
        self.assertEqual(self.b['client'].get_messages.call_count,2)

    async def test_adjacent_prompt_requires_same_sender(self):
        picture=photo_message(102,'',2,sender=7)
        hint=message(103,'请输入验证码');hint.sender_id=8
        self.b['client'].get_messages=AsyncMock(return_value=[hint,picture])
        with patch.object(asyncio,'sleep',new=AsyncMock()):
            result=await self.b['_await_signin_captcha']('entity',0.01,101)
        self.assertEqual(result,(None,None));self.b['client'].download_media.assert_not_awaited()

    async def test_correct_adjacent_prompt_is_associated(self):
        picture=photo_message(102,'',2,sender=7)
        hint=message(103,'请输入验证码');hint.sender_id=7
        self.b['client'].get_messages=AsyncMock(return_value=[hint,picture])
        result=await self.b['_await_signin_captcha']('entity',1,101)
        self.assertEqual(result[0].id,102)

    async def test_error_card_never_selected_as_picture_prompt(self):
        card=photo_message(102,'验证码错误！',1)
        self.b['client'].get_messages=AsyncMock(return_value=[card])
        with patch.object(asyncio,'sleep',new=AsyncMock()):
            self.assertEqual(await self.b['_await_signin_captcha']('entity',0.01,101),(None,None))
        self.b['client'].download_media.assert_not_awaited()

    async def test_failure_not_hidden_by_newer_prompt(self):
        self.b['client'].get_messages=AsyncMock(return_value=[message(107,'请输入验证码'),message(106,'验证码错误！')])
        text,status=await self.b['_await_signin_result']('entity',105,require_success=True)
        self.assertEqual(status,'failed');self.assertIn('验证码错误',text)

    async def test_edited_bot_confirmation_is_current_not_old_history(self):
        pending=photo_message(102,'请输入验证码',2)
        baseline={102:self.b['_message_signature'](pending)}
        confirmed=photo_message(102,'签到成功',2)
        self.b['client'].get_messages=AsyncMock(return_value=[confirmed])
        _,status=await self.b['_await_signin_result']('entity',103,baseline=baseline,require_success=True)
        self.assertEqual(status,'success')

    async def test_delay_and_mode_validation_and_copy(self):
        aid=await self.create(command_delay=0,button_delay=120,refresh_buttons=0)
        copied=(await self.b['duplicate_auto'](aid))['id']
        self.assertEqual(self.row(copied)['command_delay'],0)
        self.assertEqual(self.row(copied)['button_delay'],120)
        self.assertEqual(self.row(copied)['refresh_buttons'],0)
        for values in ({'command_delay':-1},{'button_delay':121},{'command_delay':float('nan')},{'refresh_buttons':2}):
            with self.subTest(values=values), self.assertRaises(HTTPException): await self.create(**values)


if __name__ == '__main__':
    unittest.main()
