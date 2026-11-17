"""水群兜底回归：等待只认面板区间、SKIP 丢弃模型分钟、连续不说收紧、积极模式保留心跳。"""
import unittest

from test_backend import load_backend


class FlockGapTests(unittest.TestCase):
    def setUp(self):
        self.b = load_backend()
        self.hb = {}
        b = self.b
        b['_flock_hb_load'] = lambda job, chat: dict(self.hb.get((job, chat), {}))
        b['_flock_hb_save'] = lambda job, chat, data: self.hb.__setitem__((job, chat), dict(data))

    def test_gap_allows_one_minute(self):
        src = {'gap_min': 1, 'gap_max': 1, 'min_messages': 6, 'max_messages': 8}
        for _ in range(20):
            self.assertEqual(self.b['_flock_hb_gap'](src), 1)

    def test_gap_stays_in_panel_window(self):
        src = {'gap_min': 2, 'gap_max': 3}
        for _ in range(20):
            self.assertIn(self.b['_flock_hb_gap'](src), (2, 3))

    def test_gap_ignores_model_minutes(self):
        src = {'gap_min': 1, 'gap_max': 3}
        for minutes in (5, 8, 30):
            self.assertLessEqual(self.b['_flock_hb_gap'](src, minutes), 3)

    def test_skip_streak_tightens_fast(self):
        src = {'job_id': 7, 'gap_min': 3, 'gap_max': 3}
        low = self.b['_flock_hb_gap'](src)
        streak = self.b['_flock_hb_skip'](src, '-1001', 8)
        self.assertEqual(streak, 1)
        streak = self.b['_flock_hb_skip'](src, '-1001', 8)
        self.assertEqual(streak, 2)
        gap = (self.hb[(7, '-1001')]['next_wake_at'] - self.b['time'].time()) / 60
        self.assertLessEqual(gap, low)
        self.assertGreaterEqual(gap, 0.5)

    def test_split_drops_model_minutes_on_skip(self):
        self.assertEqual(self.b['_flock_split']('NO_REPLY 8')[:2], ('skip', 0))
        self.assertEqual(self.b['_flock_split'](self.b['_flock_reply']('SKIP 8'))[:2], ('skip', 0))


class FlockActiveTests(unittest.TestCase):
    def setUp(self):
        self.b = load_backend()
        self.b['_flock_active_now'] = lambda: True

    def test_active_keeps_heartbeat_and_bounds(self):
        got = self.b['_flock_apply_active']({
            'hourly_limit': -1, 'active_minutes': 3, 'inactive_mode': 1,
            'min_messages': 1, 'max_messages': 3, 'gap_min': 1, 'gap_max': 3})
        self.assertEqual(got['active_mode'], 1)
        self.assertEqual(got['inactive_mode'], 1)
        self.assertEqual(got['hourly_limit'], 12)
        self.assertEqual((got['min_messages'], got['max_messages']), (1, 2))
        self.assertEqual((got['gap_min'], got['gap_max']), (1, 3))

    def test_active_scales_existing_limit(self):
        got = self.b['_flock_apply_active']({'hourly_limit': 8, 'active_minutes': 5, 'inactive_mode': 1,
                                             'min_messages': 6, 'max_messages': 8})
        self.assertEqual(got['hourly_limit'], 18)
        self.assertEqual(got['active_minutes'], 10)

    def test_off_window_untouched(self):
        self.b['_flock_active_now'] = lambda: False
        src = {'hourly_limit': 8, 'inactive_mode': 1, 'min_messages': 6, 'max_messages': 8}
        self.assertEqual(self.b['_flock_apply_active'](dict(src)), src)

class FlockIdleReproTests(unittest.TestCase):
    """兜底心跳不该在没有新消息时每分钟重放同一个"不说"判断。"""

    def setUp(self):
        self.b = load_backend()
        self.gap = 8 * 60
        self.b['FLOCK_HB_IDLE_GAP'] = self.gap

    def test_new_message_wakes_and_records_cursor(self):
        data = {'last_msg_id': 100, 'last_idle_probe': 0}
        wake, delay = self.b['_flock_hb_needs_wake'](data, 120, 1000.0)
        self.assertTrue(wake)
        self.assertEqual(data['last_msg_id'], 120)
        self.assertEqual(delay, 0.0)

    def test_no_new_message_holds_off(self):
        now = 1000.0
        data = {'last_msg_id': 120, 'last_idle_probe': now - 60}
        wake, delay = self.b['_flock_hb_needs_wake'](data, 120, now)
        self.assertFalse(wake)
        self.assertEqual(delay, now + self.gap)

    def test_no_new_message_never_wakes_even_after_gap(self):
        # 没人说话就不该再花钱问模型"要不要开口"。
        now = 2000.0
        data = {'last_msg_id': 120, 'last_idle_probe': now - self.gap - 5}
        wake, delay = self.b['_flock_hb_needs_wake'](data, 120, now)
        self.assertFalse(wake)
        self.assertGreaterEqual(delay, now)

    def test_bot_only_traffic_counts_as_silence(self):
        now = 3000.0
        data = {'last_msg_id': 0}
        wake, delay = self.b['_flock_hb_needs_wake'](data, 0, now)
        self.assertFalse(wake)

    def test_unknown_latest_keeps_old_behaviour(self):
        data = {}
        wake, delay = self.b['_flock_hb_needs_wake'](data, None, 100.0)
        self.assertTrue(wake)
        self.assertEqual(delay, 0.0)

    def test_bare_number_is_silence(self):
        self.assertEqual(self.b['_flock_reply']('5'), 'NO_REPLY|0')
        self.assertEqual(self.b['_flock_reply'](' 12 '), 'NO_REPLY|0')

    def test_split_allows_one_minute(self):
        self.assertEqual(self.b['_flock_split']('1|好的'), ('reply', 1, '好的'))




class FlockFeatureSwitchTests(unittest.TestCase):
    """三个新功能都必须有任务级开关，关掉时不能有副作用。"""

    def setUp(self):
        self.b = load_backend()

    def test_skip_reason_off_keeps_plain_no_reply(self):
        self.assertEqual(self.b['_flock_reply']('SKIP|别人在答了'), 'NO_REPLY|0')
        self.assertEqual(self.b['_flock_split']('NO_REPLY|0')[:2], ('skip', 0))

    def test_skip_reason_on_keeps_short_note(self):
        self.assertEqual(self.b['_flock_reply']('SKIP|别人在答了', False, True), 'NO_REPLY|别人在答了')
        self.assertEqual(self.b['_flock_split']('NO_REPLY|别人在答了')[2], '别人在答了')

    def test_skip_note_is_clamped(self):
        got = self.b['_flock_reply']('SKIP|' + '很长的理由' * 20, False, True)
        self.assertLessEqual(len(got.split('|', 1)[1]), 24)

    def test_skip_note_strips_pipes(self):
        got = self.b['_flock_reply']('SKIP|a|b', False, True)
        self.assertNotIn('|', got.split('|', 1)[1])

    def test_default_switches_off(self):
        body = self.b['AIFlockJobInput'](name='x', groups='-1', prompt='聊')
        self.assertEqual((body.skip_reason, body.group_memory, body.quality_review), (0, 0, 0))


if __name__ == '__main__':
    unittest.main()
