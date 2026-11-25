"""学习队列保留上限与输出截断拆分回归。"""
import unittest
class LearningRetentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_suite.test_backend import BackendBase as Base
        self.base = Base
        Base.setUp(self)

    async def asyncTearDown(self):
        await self.base.asyncTearDown(self)

    def test_learning_queue_is_capped_per_role(self):
        samples = [(1000 + i, '-1001', 'group-%s' % i, 'group') for i in range(250)]
        samples += [(2000 + i, '-1001', 'owner-%s' % i, 'owner') for i in range(120)]
        self.b['_store_learning_samples'](samples)
        db = self.b['conn']
        counts = {row[0]: row[1] for row in db.execute(
            "SELECT role, COUNT(*) FROM ai_learning_samples GROUP BY role").fetchall()}
        self.assertEqual(counts['group'], 200)
        self.assertEqual(counts['owner'], 100)
        self.assertEqual(db.execute("SELECT text FROM ai_learning_samples WHERE role='group' ORDER BY id LIMIT 1").fetchone()[0],
                         'group-50')
        self.assertEqual(db.execute("SELECT text FROM ai_learning_samples WHERE role='owner' ORDER BY id LIMIT 1").fetchone()[0],
                         'owner-20')

    def test_output_limit_is_retryable_by_split(self):
        self.assertTrue(self.b['_is_context_length_error'](
            RuntimeError('AI 输出达到本次上限（600），蒸馏结果不完整；改用更少条目或提高上限')))
        self.assertTrue(self.b['_is_context_length_error'](
            RuntimeError('context length exceeded')))
