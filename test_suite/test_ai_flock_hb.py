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


if __name__ == '__main__':
    unittest.main()
