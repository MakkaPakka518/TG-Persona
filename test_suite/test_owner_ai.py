"""Owner-only commands, memory isolation, learning metrics and daily budgets; offline only."""
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_backend import load_backend, HTTPException, message


class OwnerAITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_backend()
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = self.b['get_runtime']()
        for name in ('study', 'skill', 'knowledge'):
            setattr(type(self.runtime), name + '_file', Path(self.tmp.name) / (name + '.md'))
        self.runtime.tg_state = {'phase': 'ready', 'me': {'id': 42}}
        self.runtime.client = SimpleNamespace(get_messages=AsyncMock(return_value=[]))
        self.b['conn'].executemany('INSERT INTO watched_groups VALUES(?,?,0)', [('-1', '本群'), ('-2', '另一群')])
        self.b['conn'].commit()
        self.tasks = []
        def spawn(coro):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task
        type(self.runtime).spawn = staticmethod(spawn)

    async def asyncTearDown(self):
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.b['conn'].close()
        self.tmp.cleanup()

    async def provider(self):
        return (await self.b['create_ai_provider'](self.b['AIProviderInput'](
            note='测试', base='https://example.invalid/v1', key='fixture-secret', model='m')))['provider']['id']

    def event(self, text, mid=100, **changes):
        event = SimpleNamespace(is_group=True, out=True, sender_id=42, chat_id=-1, id=mid,
            raw_text=text, via_bot_id=None, fwd_from=None, post=False, is_reply=False,
            get_reply_message=AsyncMock(return_value=None),
            reply=AsyncMock(return_value=SimpleNamespace(id=mid + 1000, chat_id=-1)))
        for key, value in changes.items():
            setattr(event, key, value)
        return event

    def correction_event(self):
        self.b['set_setting']('flock_correction_enabled', '1')
        self.b['set_setting']('flock_correction_keywords', '改正\n纠正')
        replied = SimpleNamespace(id=10, chat_id=-1, out=True, raw_text='错误答案')
        self.b['_track_generated_message'](replied, '-1', '错误答案', 'ai_flock')
        return self.event('改正：正确答案是星期五', is_reply=True,
                          get_reply_message=AsyncMock(return_value=replied))

    async def test_defaults_disabled_and_budget_unlimited(self):
        settings = await self.b['get_owner_ai_settings']()
        self.assertEqual(settings['settings']['flock_correction_enabled'], '0')
        self.assertEqual(settings['settings']['flock_summary_enabled'], '0')
        self.assertEqual(settings['settings']['flock_summary_unlimited'], '0')
        self.assertEqual(settings['settings']['flock_owner_pause_enabled'], '0')
        self.assertTrue(all(item['limit'] == 0 for item in settings['budgets']))

    async def test_enabled_commands_require_keywords(self):
        for key in ('flock_correction_enabled', 'flock_summary_enabled'):
            with self.assertRaises(HTTPException):
                await self.b['update_owner_ai_settings'](self.b['OwnerAISettingsInput'](**{key: 1}))

    async def test_only_verified_owner_can_correct(self):
        event = self.correction_event()
        for changes in ({'out': False, 'sender_id': 7}, {'out': True, 'sender_id': 7},
                        {'sender_id': None}, {'fwd_from': object()}, {'via_bot_id': 5}, {'post': True}):
            bad = SimpleNamespace(**{**vars(event), **changes})
            self.assertFalse(await self.b['_handle_owner_ai_event'](bad))
        self.assertFalse(self.runtime.study_file.exists())
        event.reply.assert_not_awaited()

    async def test_correction_needs_prefix_reply_and_ai_target(self):
        template = self.correction_event()
        for text, reply, target in [('我觉得要改正', True, True), ('改正', True, True),
                                     ('改正：正确的答案', False, True), ('改正：正确的答案', True, False)]:
            event = self.event(text, is_reply=reply, get_reply_message=AsyncMock(return_value=(
                template.get_reply_message.return_value if target else SimpleNamespace(id=3, out=False))))
            await self.b['_handle_owner_ai_event'](event)
            event.reply.assert_not_awaited()
        self.assertFalse(self.runtime.study_file.exists())

    async def test_fixed_ack_bypasses_persona_and_saves_pinned_correction(self):
        event = self.correction_event()
        self.b['set_setting']('water_ai_prompt', '禁止说收到')
        self.b['_call_ai_any'] = AsyncMock(side_effect=AssertionError('correction must not need AI'))
        self.assertTrue(await self.b['_handle_owner_ai_event'](event))
        self.assertEqual(event.reply.await_args_list[0].args[0], '收到，正在写入记忆……')
        await asyncio.gather(*self.tasks)
        block = self.b['_study_blocks'](self.b['_read_study']())[0]
        self.assertTrue(block['meta']['pinned'])
        self.assertEqual(block['meta']['chat_id'], '-1')
        self.assertIn('正确答案是星期五', block['body'])
        self.assertNotIn('错误答案', block['body'])
        self.assertIn('正确答案是星期五', self.b['_owner_corrections']('-1'))
        self.assertEqual(self.b['_owner_corrections']('-2'), '')
        self.b['_call_ai_any'].assert_not_awaited()
        await self.b['_handle_owner_ai_event'](event)
        self.assertEqual(len(self.b['_study_blocks'](self.b['_read_study']())), 1)

    async def test_generated_owner_messages_cannot_trigger_commands_or_pause(self):
        event = self.correction_event()
        self.b['set_setting']('flock_owner_pause_enabled', '1')
        self.b['_track_generated_message'](event, '-1', event.raw_text)
        await self.b['_handle_owner_ai_event'](event)
        self.assertFalse(self.runtime.study_file.exists())
        self.assertFalse(self.b['_flock_owner_paused']('-1'))

    async def test_summary_is_exact_owner_keyword_and_current_group_only(self):
        await self.provider()
        self.b['set_setting']('flock_summary_enabled', '1')
        self.b['set_setting']('flock_summary_keywords', '最近他们聊了什么')
        self.b['_call_ai_any'] = AsyncMock(return_value='大家讨论了电影和晚餐。')
        now = datetime.now(timezone.utc)
        history = [SimpleNamespace(id=2, date=now, out=False, raw_text='今晚讨论电影', via_bot_id=None,
            fwd_from=None, post=False, get_sender=AsyncMock(return_value=SimpleNamespace(bot=False))),
            SimpleNamespace(id=1, date=now - timedelta(days=1), out=False, raw_text='旧消息')]
        self.runtime.client.get_messages.return_value = history
        for event in (self.event('最近他们聊了什么', out=False, sender_id=9),
                      self.event('他说最近他们聊了什么')):
            self.assertFalse(await self.b['_handle_owner_ai_event'](event))
        event = self.event('最近他们聊了什么')
        self.assertTrue(await self.b['_handle_owner_ai_event'](event))
        await asyncio.gather(*self.tasks)
        self.runtime.client.get_messages.assert_awaited_once_with(-1, limit=201, max_id=100)
        prompt = self.b['_call_ai_any'].await_args.args[0]
        self.assertIn('今晚讨论电影', prompt)
        self.assertNotIn('旧消息', prompt)
        self.assertIn('200–400 字', prompt)
        self.assertIn('3–5 个短要点', prompt)
        self.assertIn('不逐条复述', prompt)
        self.assertIn('最多 800 字', prompt)
        self.assertEqual(self.b['_call_ai_any'].await_args.kwargs['max_tokens'], 2500)
        self.assertIn('群聊摘要', event.reply.await_args.args[0])
        self.assertEqual(event.reply.await_args.kwargs['parse_mode'], None)

    async def test_pause_applies_to_manual_owner_and_expires(self):
        self.b['set_setting']('flock_owner_pause_enabled', '1')
        self.b['set_setting']('flock_owner_pause_minutes', '7')
        with patch.object(self.b['time'], 'time', return_value=10000):
            await self.b['_handle_owner_ai_event'](self.event('我来聊几句'))
            self.assertTrue(self.b['_flock_owner_paused']('-1'))
            self.assertFalse(self.b['_flock_owner_paused']('-2'))
        with patch.object(self.b['time'], 'time', return_value=10421):
            self.assertFalse(self.b['_flock_owner_paused']('-1'))

    async def test_unlimited_summary_does_not_slice_and_sends_all_parts(self):
        await self.provider()
        self.b['set_setting']('flock_summary_enabled', '1')
        self.b['set_setting']('flock_summary_unlimited', '1')
        now = datetime.now(timezone.utc)
        self.runtime.client.get_messages.return_value = [SimpleNamespace(id=2, date=now, out=False,
            raw_text='群里讨论了网络方案', get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)))]
        answer = ('群友讨论网络方案。' * 250 + '\n') * 2 + '😀' * 1800 + '完整总结结尾'
        self.b['_call_ai_any'] = AsyncMock(return_value=answer)
        event = self.event('总结群聊')
        event.reply.side_effect = [SimpleNamespace(id=mid, chat_id=-1) for mid in range(1000, 1010)]
        self.b['conn'].execute("INSERT INTO owner_ai_commands VALUES('-1',100,'summary','running')")
        await self.b['_run_owner_ai_command'](event, 'summary', '')
        summary_call = self.b['_call_ai_any'].await_args_list[0]
        self.assertIsNone(summary_call.kwargs['max_tokens'])
        self.assertNotIn('最多 800 字', summary_call.args[0])
        parts = [call.args[0] for call in event.reply.await_args_list]
        self.assertGreater(len(parts), 1)
        self.assertTrue(''.join(parts).endswith(answer))
        self.assertTrue(all(len(part.encode('utf-16-le')) // 2 <= 4096 for part in parts))
        for index, part in enumerate(parts):
            self.assertTrue(self.b['_is_generated_message']('-1', 1000 + index, part))
            row = self.b['conn'].execute("SELECT kind FROM generated_messages WHERE chat_id='-1' AND message_id=?",
                                         (1000 + index,)).fetchone()
            self.assertEqual(row[0], 'summary')

    async def test_summary_fits_one_telegram_message_without_local_slicing(self):
        await self.provider()
        self.b['set_setting']('flock_summary_enabled', '1')
        self.runtime.client.get_messages.return_value = [SimpleNamespace(id=2, date=datetime.now(timezone.utc),
            raw_text='群里讨论网络方案', get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)))]
        answer = '讨论结论。' * 650 + '😀' * 300 + '完整结尾'
        self.b['_call_ai_any'] = AsyncMock(return_value=answer)
        event = self.event('总结群聊')
        await self.b['_run_owner_ai_command'](event, 'summary', '')
        event.reply.assert_awaited_once()
        sent = event.reply.await_args.args[0]
        self.assertTrue(sent.endswith(answer))
        self.assertLessEqual(len(sent.encode('utf-16-le')) // 2, 4096)

    async def test_correction_and_summary_do_not_start_or_extend_chat_pause(self):
        await self.provider()
        self.b['set_setting']('flock_owner_pause_enabled', '1')
        self.b['set_setting']('flock_summary_enabled', '1')
        self.b['set_setting']('flock_summary_keywords', '最近他们聊了什么')
        correction = self.correction_event()
        self.assertTrue(await self.b['_handle_owner_ai_event'](correction))
        self.assertEqual(self.b['get_setting']('flock_owner_last_-1', ''), '')
        self.assertFalse(self.b['_flock_owner_paused']('-1'))
        await asyncio.gather(*self.tasks)
        self.assertIn('正确答案是星期五', self.b['_read_study']())
        self.b['set_setting']('flock_owner_last_-1', str(self.b['time'].time() - 60))
        original = self.b['get_setting']('flock_owner_last_-1')
        self.assertTrue(self.b['_flock_owner_paused']('-1'))
        summary = self.event('最近他们聊了什么', mid=101)
        self.assertTrue(await self.b['_handle_owner_ai_event'](summary))
        next_correction = self.correction_event()
        next_correction.id = 102
        self.assertTrue(await self.b['_handle_owner_ai_event'](next_correction))
        await asyncio.gather(*self.tasks)
        self.assertEqual(self.b['get_setting']('flock_owner_last_-1'), original)
        self.runtime.client.get_messages.assert_awaited_once()
        self.assertIn('没有可总结', summary.reply.await_args.args[0])
        self.assertIn('纠正已写入', next_correction.reply.await_args.args[0])

    async def test_manual_owner_speech_cancels_inflight_flock_before_send(self):
        provider = await self.provider()
        self.b['set_setting']('flock_owner_pause_enabled', '1')
        await self.b['create_ai_flock_job'](self.b['AIFlockJobInput'](
            name='测试', groups='-1', start_time='00:00', end_time='23:59', prompt='聊天', provider_ids=provider))
        self.runtime.client = SimpleNamespace(get_messages=AsyncMock(return_value=[message(2, '聊电影')]),
                                             send_message=AsyncMock())
        async def call(*_args, **_kwargs):
            await self.b['_handle_owner_ai_event'](self.event('我自己来回复'))
            return '一条不该发出的回复'
        self.b['_call_ai_any'] = AsyncMock(side_effect=call)
        with patch('asyncio.sleep', new=AsyncMock()):
            await self.b['_run_ai_flock'](-1, '聊电影')
        self.runtime.client.send_message.assert_not_awaited()

    async def test_incoming_owner_keywords_do_not_trigger_ordinary_ai(self):
        self.b['set_setting']('flock_summary_enabled', '1')
        self.b['set_setting']('flock_summary_keywords', '最近他们聊了什么')
        self.b['_collect_deep_incoming'] = AsyncMock()
        await self.b['on_group_message'](self.event('最近他们聊了什么', out=False, sender_id=7))
        self.assertEqual(self.tasks, [])
        self.b['_collect_deep_incoming'].assert_not_awaited()

    def test_skill_reference_follows_topic_instead_of_created_time(self):
        blocks = ""
        for index in range(12):
            body = ("- 电影话题规则 %s：聊片单" % index) if index < 6 else ("- 网络话题规则 %s：聊反代" % index)
            blocks = self.b['_append_skill_memory'](blocks, body, created_at=1000 + index, stage='detail')
        self.b['_write_skill'](blocks)
        movie = self.b['_skill_reference'](60, '-1', '今晚看什么电影，片单推荐')
        network = self.b['_skill_reference'](60, '-1', 'Cloudflare 反代怎么配置')
        self.assertIn('电影话题规则', movie)
        self.assertNotIn('网络话题规则', movie)
        self.assertIn('网络话题规则', network)
        self.assertNotIn('电影话题规则', network)

    def test_skill_reference_takes_whole_entries_and_prefers_unused(self):
        self.b['_write_skill']('')
        content = self.b['_append_skill_memory']('', '- 固定人格：简短自然', created_at=100, stage='detail')
        for index in range(20):
            content = self.b['_append_skill_memory'](content, '- 闲聊风格 %s 号' % index,
                                                     created_at=200 + index, stage='detail')
        self.b['_write_skill'](content)
        used = []
        reference = self.b['_skill_reference'](120, '-1', '闲聊', used_sink=used)
        self.assertTrue(used)
        self.assertNotIn('\n\n\n', reference)
        for body in reference.split('\n\n'):
            self.assertTrue(body.startswith('-'), body)
        self.b['_record_memory_usage']('skill', used)
        usage = self.b['_memory_usage']('skill')
        self.assertTrue(all(usage.get(item) == 1 for item in used if item))
        second = []
        self.b['_skill_reference'](120, '-1', '闲聊', used_sink=second)
        self.assertNotEqual(set(second), set(used))

    def test_group_memory_isolation_and_topic_ranking(self):
        content = self.b['_append_study']('', '- 本群聊电影', chat_id='-1')
        content = self.b['_append_study'](content, '- 另一群秘密话题', chat_id='-2')
        content = self.b['_append_study'](content, '- 本群讨论 Cloudflare 反代', chat_id='-1')
        self.b['_write_study'](content)
        self.b['_write_knowledge']('电影常识\n\nCloudflare 网络知识')
        reference = self.b['_reference_markdown'](1000, '-1', 'Cloudflare 反代怎么设置')
        self.assertNotIn('另一群秘密', reference)
        self.assertLess(reference.index('Cloudflare 反代'), reference.index('本群聊电影'))

    async def test_memory_edits_use_revision_and_preserve_pin(self):
        self.b['_write_study'](self.b['_append_study']('', '需要修改的内容', chat_id='-1'))
        listed = await self.b['list_ai_memory']()
        self.assertEqual(listed['items'][0]['group'], '本群')
        await self.b['edit_ai_memory']('study', listed['items'][0]['start'], self.b['MemoryEditInput'](
            version=listed['version'], body='已经纠正的内容'))
        self.assertTrue(self.b['_study_blocks'](self.b['_read_study']())[0]['meta']['pinned'])
        with self.assertRaises(HTTPException) as conflict:
            await self.b['edit_ai_memory']('study', 0, self.b['MemoryEditInput'](
                version=listed['version'], remove=True))
        self.assertEqual(conflict.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            await self.b['list_ai_memory'](file='../knowledge')

    async def test_learning_metrics_remain_after_processing(self):
        self.b['_schedule_ai_learning'] = Mock()
        self.b['_store_learning_samples']([(0, '-1', '主人写的', 'owner'), (0, '-1', '群友写的', 'group')])
        self.b['_learning_metric']('group', learned=1)
        self.b['conn'].execute("DELETE FROM ai_learning_samples WHERE role='group'")
        result = self.b['_learning_metrics']()
        self.assertEqual(result['today']['group'], {'collected': 1, 'learned': 1})
        self.assertEqual(result['total']['group']['pending'], 0)

    async def test_daily_budget_blocks_before_network_and_resets_by_date(self):
        provider = await self.provider()
        self.b['set_setting']('flock_daily_learning_tokens', '10')
        self.b['_record_ai_usage'](provider, {'prompt_tokens': 5, 'completion_tokens': 6}, 'learning')
        self.b['_call_ai_request'] = AsyncMock(return_value='ok')
        cfg = self.b['_ai_provider_configs']()[0]
        with self.assertRaises(self.b['AIBudgetExceeded']):
            await self.b['_call_ai'](cfg, 'x', feature='learning')
        self.b['_call_ai_request'].assert_not_awaited()
        self.assertEqual(await self.b['_call_ai'](cfg, 'x', feature='ai_flock'), 'ok')
        self.b['set_setting']('flock_summary_unlimited', '1')
        self.b['set_setting']('flock_daily_total_tokens', '10')
        with self.assertRaises(self.b['AIBudgetExceeded']):
            await self.b['_call_ai'](cfg, '总结', None, feature='summary')
        self.b['_beijing_now'] = lambda: datetime.now(timezone.utc) + timedelta(days=2)
        self.assertEqual(await self.b['_call_ai'](cfg, 'x', feature='learning'), 'ok')

    async def test_daily_budget_serializes_requests_and_counts_last_call(self):
        provider = await self.provider()
        self.b['set_setting']('flock_daily_total_tokens', '10')
        entered, release = asyncio.Event(), asyncio.Event()
        async def request(*_args, **_kwargs):
            entered.set()
            await release.wait()
            self.b['_record_ai_usage'](provider, {'prompt_tokens': 8, 'completion_tokens': 3}, 'learning')
            return 'ok'
        self.b['_call_ai_request'] = AsyncMock(side_effect=request)
        cfg = self.b['_ai_provider_configs']()[0]
        first = asyncio.create_task(self.b['_call_ai'](cfg, 'x', feature='learning'))
        await entered.wait()
        second = asyncio.create_task(self.b['_call_ai'](cfg, 'x', feature='ai_flock'))
        release.set()
        self.assertEqual(await first, 'ok')
        with self.assertRaises(self.b['AIBudgetExceeded']):
            await second
        self.b['_call_ai_request'].assert_awaited_once()

    async def test_pinned_study_correction_is_not_pruned(self):
        content = self.b['_append_study']('', '必须保留的纠正', chat_id='-1', source='correction', pinned=True)
        limit = self.b['KNOWLEDGE_MAX_BYTES']
        self.b['KNOWLEDGE_MAX_BYTES'] = len(content.encode()) + 350
        try:
            content = self.b['_append_study'](content, 'OLD' * 40)
            content = self.b['_append_study'](content, 'NEW' * 40)
        finally:
            self.b['KNOWLEDGE_MAX_BYTES'] = limit
        self.assertIn('必须保留的纠正', content)
        self.assertNotIn('OLD' * 40, content)

    async def test_reply_log_records_exact_reference_snapshot(self):
        provider = await self.provider()
        self.b['_write_study'](self.b['_append_study']('', '- 本群聊电影', chat_id='-1'))
        self.b['_write_knowledge']('电影常识')
        await self.b['create_ai_flock_job'](self.b['AIFlockJobInput'](
            name='测试', groups='-1', start_time='00:00', end_time='23:59', prompt='随便聊', provider_ids=provider))
        self.runtime.client = SimpleNamespace(get_messages=AsyncMock(return_value=[message(2, '电影如何')]),
                                             send_message=AsyncMock(return_value=SimpleNamespace(id=3, chat_id=-1)))
        async def call(_prompt, **kwargs):
            kwargs['trace'].update(provider_id=provider, provider='测试', model='m', reasoning_effort='high')
            return '大家聊电影呢'
        self.b['_call_ai_any'] = AsyncMock(side_effect=call)
        with patch('asyncio.sleep', new=AsyncMock()):
            await self.b['_run_ai_flock'](-1, '电影如何')
        hit = self.b['conn'].execute("SELECT id FROM hits WHERE action='ai_flock'").fetchone()
        self.assertIsNotNone(hit)
        trace = await self.b['get_ai_flock_trace'](hit[0])
        self.assertEqual(trace['call']['reasoning_effort'], 'high')
        self.assertIn('本群聊电影', trace['references']['study'])


if __name__ == '__main__':
    unittest.main()
