import unittest
from test_backend import load_backend


class LearningFilterTests(unittest.TestCase):
    def setUp(self):
        self.b = load_backend()
        self.signal = self.b['_learning_signal']

    def test_noise_is_dropped(self):
        for text in ('哈哈哈😄', '确实', '好的好的', '1111', '！？', '笑死。'):
            keep, reason, tag = self.signal('-1', text)
            self.assertFalse(keep, text)
            self.assertTrue(reason)

    def test_meaningful_text_is_kept(self):
        keep, reason, tag = self.signal('-1', '这个活动名额太少，我放弃了')
        self.assertTrue(keep)
        self.assertEqual(reason, '')
        self.assertEqual(tag, '')

    def test_technical_text_kept_by_signal_words(self):
        for text in ('怎么部署 worker 到 cf 呢', '今天服务器又炸了', '端口 8080 打不开'):
            keep, reason, tag = self.signal('-1', text)
            self.assertTrue(keep, text)
            self.assertEqual(tag, 'signal')

    def test_low_signal_is_tagged_but_kept(self):
        keep, reason, tag = self.signal('-1', '确实挺有意思的')
        self.assertTrue(keep)
        self.assertEqual(tag, 'would_drop')

    def test_replay_dropped_on_third(self):
        texts = [self.signal('-2', '这个不错啊') for _ in range(3)]
        self.assertTrue(texts[0][0])
        self.assertTrue(texts[1][0])
        self.assertFalse(texts[2][0])
        self.assertEqual(texts[2][1], '短时复读')


if __name__ == '__main__':
    unittest.main()
