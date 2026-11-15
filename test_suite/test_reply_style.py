"""回复分泡、发送去重、自动知识合并与时间衰减。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest

from test_backend import load_backend, message


class ReplyStyleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_backend()
        self.b['_beijing_now'] = lambda: datetime(2026, 11, 15, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        self.tmp = tempfile.TemporaryDirectory()
        runtime = self.b['get_runtime']()
        runtime.knowledge_auto_file = Path(self.tmp.name) / 'knowledge-auto.md'

    def tearDown(self):
        self.b['conn'].close()
        self.tmp.cleanup()

    def test_short_reply_is_never_split(self):
        self.assertEqual(self.b['_bubble_parts']('好的', 1.0), ['好的'])

    def test_split_probability_zero_keeps_one_bubble(self):
        text = '今天红包好多啊。你们抢到了吗？'
        self.assertEqual(self.b['_bubble_parts'](text, 0.0), [text])

    def test_split_on_sentence_boundary(self):
        parts = self.b['_bubble_parts']('今天红包好多啊。你们抢到了吗？', 1.0)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0], '今天红包好多啊。')
        self.assertEqual(''.join(parts), '今天红包好多啊。你们抢到了吗？')

    def test_split_caps_at_three_bubbles(self):
        text = ('第一句话很长很长很长。第二句话也不短不短不短。'
                '第三句话同样比较比较长。第四句话还是那么那么那么长。五。')
        parts = self.b['_bubble_parts'](text, 1.0)
        self.assertLessEqual(len(parts), 3)
        self.assertEqual(''.join(parts), text)

    async def test_send_bubbled_reply_skips_duplicate_within_window(self):
        b = self.b
        sent = []

        async def fake_send(entity, text, **kwargs):
            sent.append(text)
            return b['_track_generated_message'](message(1, text, out=True), 'chat', text, 'test')

        b['_send_generated_message'] = fake_send
        await b['_send_bubbled_reply']('chat', 'chat', '好的收到啊', 'test', 0.0, dedupe=True)
        self.assertEqual(len(sent), 1)
        result, count = await b['_send_bubbled_reply']('chat', 'chat', '好的收到啊', 'test', 0.0, dedupe=True)
        self.assertIsNone(result)
        self.assertEqual(count, 0)

    async def test_fixed_announcement_is_never_deduped(self):
        b = self.b
        sent = []

        async def fake_send(entity, text, **kwargs):
            sent.append(text)
            return b['_track_generated_message'](message(1, text, out=True), 'chat', text, 'test')

        b['_send_generated_message'] = fake_send
        # 签到播报、红包回群是固定文案，同一分钟触发两次也必须照发。
        for _ in range(2):
            await b['_send_bubbled_reply']('chat', 'chat', '签到成功', 'redpacket', 0.0)
        self.assertEqual(sent, ['签到成功', '签到成功'])

    async def test_knowledge_auto_merges_instead_of_dropping(self):
        b = self.b
        path = b['get_runtime']().knowledge_auto_file
        old = '我遇到版本问题会先核实发行渠道和版本号，再决定是否重新安装。'
        path.write_text('<!-- tg-persona-auto:%s -->\n%s\n<!-- /tg-persona-auto -->\n'
                        % ('{"created_at": 1, "verified": 1}', old), encoding='utf-8')
        count = b['_append_knowledge_auto'](
            ['我遇到版本相关问题时，会先核实发行渠道和版本号，再判断是否需要重新安装，另外要看看签名是否一致。'],
            'chat', (1,), ())
        text = path.read_text(encoding='utf-8')
        # 新正文需要重新验证，同时保留可追溯的旧版本。
        self.assertEqual(text.count('<!-- tg-persona-auto:'), 1)
        self.assertIn('签名是否一致', text)
        self.assertIn('"verified": 0', text)
        self.assertGreaterEqual(count, 1)

    async def test_knowledge_auto_exact_duplicate_is_ignored(self):
        b = self.b
        path = b['get_runtime']().knowledge_auto_file
        same = '这个群的签到命令会变，发之前先看群公告。'
        path.write_text('<!-- tg-persona-auto:%s -->\n%s\n<!-- /tg-persona-auto -->\n'
                        % ('{"created_at": 1, "verified": 0}', same), encoding='utf-8')
        count = b['_append_knowledge_auto']([same], 'chat', (1,), ())
        self.assertEqual(count, 0)
        self.assertEqual(path.read_text(encoding='utf-8').count('<!-- tg-persona-auto:'), 1)

    def test_stale_auto_entry_sinks_in_ranking(self):
        b = self.b
        now = int(b['time'].time())
        old_ts = now - 60 * 86400
        content = ''.join(
            '<!-- tg-persona-auto:%s -->\n%s\n<!-- /tg-persona-auto -->' % (json.dumps(meta, ensure_ascii=False), body)
            for meta, body in (
                ({'created_at': old_ts, 'verified': 0}, '旧的没验证的条目，讲的是某个老命令怎么用。'),
                ({'created_at': now, 'verified': 0}, '新的条目，讲的是当前版本该怎么用。'),
            ))
        entries = b['_knowledge_auto_entries'](content, [('命令', 1)])
        self.assertEqual(entries[0]['body'], '新的条目，讲的是当前版本该怎么用。')
        self.assertEqual(entries[-1]['meta']['stale'], 1)


if __name__ == '__main__':
    unittest.main()
