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
        self.events=[];self.phase='before';self.fetch_count=0;self.answer_result='签到成功';self.deliver=True;self.image_mode=False;self.image_click=False;self.button_popup=''
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
                    return SimpleNamespace(message=outer.button_popup) if outer.button_popup else None
                button=SimpleNamespace(text='签到',click=AsyncMock(side_effect=click))
                return [photo_message(101,'欢迎使用测试机器人，请选择功能',1,buttons=[[button]])]
            if outer.phase=='prompt':
                buttons=None
                if outer.image_click:
                    async def choose_apple(): outer.events.append(('image_click','苹果'));outer.phase='result'
                    buttons=[[SimpleNamespace(text='苹果',click=AsyncMock(side_effect=choose_apple)),SimpleNamespace(text='香蕉',click=AsyncMock())]]
                return [photo_message(102,'请选择图片中的物体' if outer.image_mode else '请输入验证码（测试消息）',2,buttons=buttons),photo_message(101,'欢迎使用测试机器人',1)]
            if outer.phase=='result': return [message(104,outer.answer_result),photo_message(102,'请输入验证码（测试消息）',2)]
            return []
        async def recognize(data,*args,**kwargs):
            outer.events.append(('recognize',None));return 'FIXTURECODE'
        self.b['client']=SimpleNamespace(send_message=AsyncMock(side_effect=send),get_messages=AsyncMock(side_effect=get),download_media=AsyncMock(return_value=b'fake-image-no-real-captcha'))
        self.b['resolve_entity']=AsyncMock(return_value='entity')
        self.b['recognize_image_text']=AsyncMock(side_effect=recognize)
        self.b['recognize_image_answer']=AsyncMock(return_value='苹果')
        self.b['send_notify']=self.b['notify_event']=AsyncMock()
        # 通知函数是同步入口，使用普通 mock 避免产生未等待协程。
        from unittest.mock import Mock
        self.b['send_notify']=self.b['notify_event']=Mock()

    def tearDown(self): self.b['conn'].close()

    async def create(self,**values):
        values.setdefault('signin_captcha',1)
        body=self.b['AutoIn'](name='fixture',target='@dummy',command='/start',click_btn=1,**values)
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

    async def test_already_signed_popup_is_success_without_new_reply(self):
        self.button_popup='今日已签到，请勿重复签到'
        self.deliver=False
        aid=await self.create(signin_captcha=0,signin_recognition_mode='none')
        await self.run_flow(aid)
        self.assertEqual(self.row(aid)['last_status'],'success')
        self.assertEqual(self.b['client'].send_message.await_count,1)
        detail=self.b['conn'].execute('SELECT detail FROM hits ORDER BY id DESC').fetchone()[0]
        self.assertIn('今日已签到',detail);self.assertIn('按钮弹窗已确认签到结果',detail)

    async def test_custom_popup_success_keyword(self):
        self.button_popup='本日打卡済み：茶香已领取'
        self.deliver=False
        aid=await self.create(signin_captcha=0,signin_recognition_mode='none',popup_success_kw='本日打卡済み')
        await self.run_flow(aid)
        self.assertEqual(self.row(aid)['last_status'],'success')
        self.assertEqual(self.row(aid)['popup_success_kw'],'本日打卡済み')

    async def test_selected_ai_providers_reach_captcha_recognition(self):
        selected='0123456789abcdef,fedcba9876543210'
        aid=await self.create(ai_provider_ids=selected)
        await self.run_flow(aid)
        self.assertEqual(self.row(aid)['ai_provider_ids'],selected)
        self.assertEqual(self.b['recognize_image_text'].call_args.kwargs['provider_ids'],selected)

    async def test_ai_mode_finds_button_on_message_edited_after_command(self):
        provider = (await self.b['create_ai_provider'](self.b['AIProviderInput'](
            note='按钮 AI', base='https://example.invalid/v1', key='fixture-key', model='m')))['provider']['id']
        phase = {'value':'before'}
        other = SimpleNamespace(text='用户功能', click=AsyncMock())
        wanted = SimpleNamespace(text='🎯 签到', click=AsyncMock(side_effect=lambda:phase.update(value='result')))
        before = message(90, '旧菜单', buttons=[]);before.edit_date=None;before.photo=None;before.document=None
        edited = message(90, '请选择功能', buttons=[[other,wanted]]);edited.edit_date='now';edited.photo=None;edited.document=None
        success = message(101, '签到成功');success.edit_date=None;success.photo=None;success.document=None
        async def send(_entity, _text, **_kwargs):phase['value']='edited';return SimpleNamespace(id=100)
        async def get(_entity, **_kwargs):return [before] if phase['value']=='before' else [edited] if phase['value']=='edited' else [success]
        self.b['client']=SimpleNamespace(send_message=AsyncMock(side_effect=send),get_messages=AsyncMock(side_effect=get))
        self.b['_call_ai_any']=AsyncMock(return_value='2')
        aid=await self.create(signin_captcha=0,signin_recognition_mode='none',button_mode='ai',ai_provider_ids=provider,
                              btn_text='签到',command_delay=0,button_delay=0)
        with patch.object(asyncio,'sleep',new=AsyncMock()):
            self.assertTrue(await self.b['run_signin'](self.row(aid)))
        wanted.click.assert_awaited_once();other.click.assert_not_awaited()
        self.b['_call_ai_any'].assert_awaited_once()
        detail=self.b['conn'].execute('SELECT detail FROM hits ORDER BY id DESC').fetchone()[0]
        self.assertIn('"button_mode": "ai"',detail)

    async def test_fallback_mode_uses_latest_unchanged_menu_for_repeat_start(self):
        provider = (await self.b['create_ai_provider'](self.b['AIProviderInput'](
            note='按钮 AI', base='https://example.invalid/v1', key='fixture-key', model='m')))['provider']['id']
        wanted = SimpleNamespace(text='🎯 签到', click=AsyncMock(return_value=SimpleNamespace(message='今日已签到，请勿重复签到')))
        old_menu = message(90, '请选择功能', buttons=[[wanted]])
        old_menu.edit_date=None;old_menu.photo=None;old_menu.document=None
        self.b['client']=SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(id=100)),
            get_messages=AsyncMock(return_value=[old_menu]))
        self.b['_call_ai_any']=AsyncMock(side_effect=AssertionError('fallback script match must not call AI'))
        aid=await self.create(signin_captcha=0,signin_recognition_mode='none',button_mode='fallback',
                              ai_provider_ids=provider,btn_text='签到',command_delay=0,button_delay=0)
        with patch.object(asyncio,'sleep',new=AsyncMock()):
            self.assertTrue(await self.b['run_signin'](self.row(aid)))
        wanted.click.assert_awaited_once();self.b['_call_ai_any'].assert_not_awaited()
        detail=self.b['conn'].execute('SELECT detail FROM hits ORDER BY id DESC').fetchone()[0]
        self.assertIn('"button_source": "latest_history"',detail);self.assertIn('今日已签到',detail)

    async def test_image_mode_uses_custom_prompt_selected_apis_and_sends_object_name(self):
        self.image_mode=True;selected='0123456789abcdef';prompt='识别核心物体，只输出名称'
        aid=await self.create(signin_captcha=0,signin_recognition_mode='image',signin_image_prompt=prompt,ai_provider_ids=selected)
        await self.run_flow(aid)
        self.b['recognize_image_text'].assert_not_awaited()
        args=self.b['recognize_image_answer'].call_args.args
        self.assertEqual((args[1],args[2],args[3],args[4]),(prompt,'请选择图片中的物体',selected,[]))
        self.assertIn(('send','苹果'),self.events);self.assertEqual(self.row(aid)['last_status'],'success')

    async def test_image_mode_can_choose_matching_bot_button_instead_of_replying(self):
        self.image_mode=True;self.image_click=True
        aid=await self.create(signin_captcha=0,signin_recognition_mode='image',signin_image_action='click')
        await self.run_flow(aid)
        options=self.b['recognize_image_answer'].call_args.args[4]
        self.assertEqual(options,['苹果','香蕉']);self.assertIn(('image_click','苹果'),self.events)
        self.assertNotIn(('send','苹果'),self.events);self.assertEqual(self.row(aid)['last_status'],'success')

    async def test_image_wait_accepts_fresh_non_captcha_picture_but_rejects_welcome_card(self):
        target=photo_message(102,'图中是什么物品？',2)
        self.b['client'].get_messages=AsyncMock(return_value=[target])
        result=await self.b['_await_signin_image']('entity',1,101)
        self.assertEqual((result[0].id,result[2]),(102,'图中是什么物品？'))
        welcome=photo_message(103,'欢迎使用测试机器人',3)
        self.b['client'].get_messages=AsyncMock(return_value=[welcome])
        with patch.object(asyncio,'sleep',new=AsyncMock()):
            self.assertEqual(await self.b['_await_signin_image']('entity',0.01,102),(None,None,''))

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
        self.assertEqual(self.row(aid)['last_status'],'unconfirmed');self.assertEqual(self.row(aid)['last_at'],'')
        self.b['_signin_tick']();self.assertNotIn(aid,self.b['_auto_running'])

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

    async def test_scheduled_failures_notify_once_only_after_retry_budget_exhausted(self):
        aid=await self.create(retries=1,success_kw='永不命中')
        self.b['run_signin']=AsyncMock(return_value=False)
        self.b['_signin_deadline']=lambda *args: time.time()+3600
        self.b['notify_event'].reset_mock()
        await self.b['_execute_auto'](aid,manual=False)
        self.b['notify_event'].assert_not_called()
        self.b['conn'].execute('UPDATE automations SET next_run_at=0 WHERE id=?',(aid,));self.b['conn'].commit()
        await self.b['_execute_auto'](aid,manual=False)
        self.assertEqual(self.b['notify_event'].call_count,1)
        self.assertIn('本轮重试已结束',self.b['notify_event'].call_args.args[2])
        self.b['conn'].execute('UPDATE automations SET next_run_at=0 WHERE id=?',(aid,));self.b['conn'].commit()
        self.b['_signin_tick']()
        self.assertNotIn(aid,self.b['_auto_running'])
        self.assertEqual(self.b['notify_event'].call_count,1)

    async def test_success_balance_message_supplements_configured_keyword(self):
        text='🧧恭喜获得 | 4 问币\n💵当前持有 | 275 问币\n⌛签到日期 | 2026-10-30'
        self.assertTrue(self.b['_signin_succeeded'](text,'成功'))

    async def test_latest_success_beats_older_failure(self):
        self.b['client'].get_messages=AsyncMock(return_value=[message(108,'签到成功'),message(107,'验证码错误！')])
        text,status=await self.b['_await_signin_result']('entity',105,require_success=True)
        self.assertEqual((text,status),('签到成功','success'))

    async def test_unconfirmed_stops_same_day_scheduler_even_retries_change(self):
        aid=await self.create(retries=1)
        db=self.b['conn'];db.execute("UPDATE automations SET retry_day='2026-10-30',retry_count=1,last_status='unconfirmed',next_run_at=0 WHERE id=?",(aid,));db.commit()
        self.b['_signin_tick']();self.assertNotIn(aid,self.b['_auto_running'])
        body=self.b['AutoIn'](name='fixture',target='@dummy',command='/start',retries=0)
        await self.b['update_auto'](aid,body);self.assertEqual(self.row(aid)['last_status'],'unconfirmed')
        self.b['_signin_tick']();self.assertNotIn(aid,self.b['_auto_running'])

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
