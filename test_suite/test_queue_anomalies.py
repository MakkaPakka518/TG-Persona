"""Account queue/anomaly backend regression; memory SQLite and fake coroutines only."""
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock

from test_backend import load_backend, HTTPException
from account_runtime import AccountRuntime, get_runtime as live_runtime, use_runtime


class QueueAnomalyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_backend()
        self.b['notify_event'] = Mock()
        self.b['send_notify'] = Mock()
        self.b['SETTING_KEYS'] = ['task_concurrency']
        self.b['DEFAULTS'] = {'task_concurrency': '2'}

    async def asyncTearDown(self):
        rt = self.b['_auto_runtime_state']()
        tasks = list(getattr(rt, 'auto_workers', {}).values())
        dispatcher = getattr(rt, 'auto_queue_task', None)
        if dispatcher is not None and not dispatcher.done():
            tasks.append(dispatcher)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.b['conn'].close()

    async def create(self, **changes):
        values = dict(name='queue fixture', type='signin', target='@fake', command='/sign',
                      win_start='00:00', win_end='23:59')
        values.update(changes)
        return (await self.b['create_auto'](self.b['AutoIn'](**values)))['id']

    async def settle(self, count=8):
        for _ in range(count):
            await asyncio.sleep(0)

    async def test_desc_barrier_exact_concurrency_and_next_batch_setting(self):
        ids = [await self.create(type='custom', custom_steps='[{"t":"wait","v":0}]') for _ in range(5)]
        await self.b['update_settings']({'task_concurrency': '2'})
        gates = {aid: asyncio.Event() for aid in ids}
        starts, active = [], set()
        peak = [0]

        async def fake(aid, manual=False):
            starts.append(aid)
            active.add(aid)
            peak[0] = max(peak[0], len(active))
            try:
                await gates[aid].wait()
            finally:
                active.discard(aid)
            return {'retry': False, 'next_at': 0}

        self.b['_execute_auto'] = fake
        for aid in ids:
            self.assertTrue(self.b['_queue_auto'](aid, True))
        completions = list(self.b['get_runtime']().auto_tasks.values())
        await self.settle()
        self.assertEqual(starts, [5, 4])
        gates[5].set()
        await self.settle()
        self.assertEqual(starts, [5, 4], 'barrier must wait for the whole batch')
        await self.b['update_settings']({'task_concurrency': 1})
        gates[4].set()
        await self.settle()
        self.assertEqual(starts[:3], [5, 4, 3])
        gates[3].set(); await self.settle()
        self.assertEqual(starts[:4], [5, 4, 3, 2])
        gates[2].set(); await self.settle()
        self.assertEqual(starts, [5, 4, 3, 2, 1])
        gates[1].set()
        await asyncio.gather(*completions)
        self.assertEqual(peak[0], 2)

    async def test_retry_round_after_all_initial_and_duplicate_reservation(self):
        ids = [await self.create() for _ in range(3)]
        await self.b['update_settings']({'task_concurrency': '2'})
        attempts, order = {}, []

        async def fake(aid, manual=False):
            attempts[aid] = attempts.get(aid, 0) + 1
            order.append((attempts[aid], aid))
            await asyncio.sleep(0)
            return {'retry': attempts[aid] == 1, 'next_at': int(time.time())}

        self.b['_execute_auto'] = fake
        for aid in ids:
            self.assertTrue(self.b['_queue_auto'](aid))
            self.assertFalse(self.b['_queue_auto'](aid))
        completions = list(self.b['get_runtime']().auto_tasks.values())
        await asyncio.gather(*completions)
        self.assertEqual(order, [(1, 3), (1, 2), (1, 1), (2, 3), (2, 2), (2, 1)])

    async def test_stop_queued_running_and_all_without_notification_or_retry(self):
        ids = [await self.create() for _ in range(3)]
        await self.b['update_settings']({'task_concurrency': '1'})
        entered = asyncio.Event()

        async def hold(aid, manual=False):
            entered.set()
            await asyncio.Event().wait()

        self.b['_execute_auto'] = hold
        for aid in ids:
            self.b['_queue_auto'](aid, True)
        await entered.wait(); await self.settle()
        runtime = self.b['get_runtime']()
        running = next(iter(runtime.auto_workers))
        queued = sorted(runtime.auto_queued)[0]
        self.assertEqual((await self.b['stop_auto'](queued))['stopped'], [queued])
        self.assertEqual(self.b['conn'].execute('SELECT last_status FROM automations WHERE id=?', (queued,)).fetchone()[0], 'stopped')
        result = await self.b['stop_all_autos']()
        self.assertIn(running, result['stopped'])
        self.assertFalse(runtime.auto_tasks)
        self.assertFalse(runtime.auto_queued)
        self.b['notify_event'].assert_not_called()

    async def test_running_stop_records_manual_reason_without_retry_or_bark(self):
        aid = await self.create(retries=3)
        entered = asyncio.Event()

        async def hold(_):
            entered.set()
            await asyncio.Event().wait()

        self.b['run_signin'] = hold
        self.assertTrue(self.b['_queue_auto'](aid, True))
        completion = self.b['get_runtime']().auto_tasks[aid]
        await entered.wait()
        await self.b['stop_auto'](aid)
        await completion
        row = self.b['conn'].execute(
            'SELECT last_status,next_run_at,retry_count FROM automations WHERE id=?', (aid,)).fetchone()
        self.assertEqual((row['last_status'], row['next_run_at'], row['retry_count']), ('stopped', 0, 1))
        detail = self.b['conn'].execute(
            'SELECT detail FROM hits WHERE rule_id=? ORDER BY id DESC', (-aid,)).fetchone()[0]
        self.assertIn('用户手动停止', detail)
        self.b['notify_event'].assert_not_called()
        self.assertNotIn(aid, self.b['get_runtime']().auto_tasks)

    async def test_stop_all_is_account_local(self):
        old_get_runtime = self.b['get_runtime']
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'a').mkdir(); (root / 'b').mkdir()
            a = AccountRuntime('a' * 32, 'A', root / 'a', sqlite3.connect(':memory:'), 1)
            b = AccountRuntime('b' * 32, 'B', root / 'b', sqlite3.connect(':memory:'), 1)
            a.db.row_factory = b.db.row_factory = sqlite3.Row
            self.b['db_init'](a.db); self.b['db_init'](b.db)
            self.b['get_runtime'] = live_runtime
            gates = {a.id: asyncio.Event(), b.id: asyncio.Event()}

            async def hold(aid, manual=False):
                await gates[live_runtime().id].wait()
                return {'retry': False, 'next_at': 0}

            self.b['_execute_auto'] = hold
            try:
                with use_runtime(a):
                    self.assertTrue(self.b['_queue_auto'](1, True))
                    done_a = a.auto_tasks[1]
                with use_runtime(b):
                    self.assertTrue(self.b['_queue_auto'](1, True))
                    done_b = b.auto_tasks[1]
                await self.settle()
                with use_runtime(a):
                    self.assertEqual((await self.b['stop_all_autos']())['stopped'], [1])
                self.assertTrue(done_a.done())
                self.assertFalse(done_b.done())
                self.assertIn(1, b.auto_workers)
                gates[b.id].set()
                await done_b
                self.assertFalse(b.auto_tasks)
            finally:
                await a.cancel_tasks(); await b.cancel_tasks()
                a.clear_auto_queue(); b.clear_auto_queue()
                a.db.close(); b.db.close()
                self.b['get_runtime'] = old_get_runtime

    async def test_settings_validation_anomaly_actions_and_db_isolation(self):
        for bad in (True, 0, 6, 2.0, ' 2', '2.0', '２'):
            with self.subTest(bad=bad), self.assertRaises(HTTPException):
                await self.b['update_settings']({'task_concurrency': bad})
        await self.b['update_settings']({'task_concurrency': '5'})
        self.assertEqual((await self.b['get_settings']())['task_concurrency'], '5')

        confirm = await self.create()
        failed = await self.create()
        retry = await self.create(enabled=0)
        disable = await self.create()
        db = self.b['conn']
        db.execute("UPDATE automations SET last_status='failed',retry_count=1,last_attempt=123 WHERE id IN (?,?,?,?)",
                   (confirm, failed, retry, disable)); db.commit()
        listed = await self.b['list_anomalies']()
        self.assertEqual([x['id'] for x in listed['items']], [4, 3, 2, 1])
        self.assertEqual(listed['count'], 4)

        await self.b['resolve_anomaly'](confirm, {'action': 'confirm_success'})
        row = db.execute('SELECT last_status,retry_count,next_run_at FROM automations WHERE id=?', (confirm,)).fetchone()
        self.assertEqual(tuple(row), ('success', 0, 0))
        detail = db.execute('SELECT detail FROM hits WHERE rule_id=? ORDER BY id DESC', (-confirm,)).fetchone()[0]
        self.assertIn('manual_resolution', detail)

        await self.b['resolve_anomaly'](failed, {'action': 'mark_failed'})
        row = db.execute('SELECT retry_count,retries,next_run_at,last_status FROM automations WHERE id=?', (failed,)).fetchone()
        self.assertEqual((row[0], row[2], row[3]), (row[1] + 1, 0, 'failed'))

        ran = asyncio.Event()
        async def once(aid, manual=False):
            ran.set()
            return {'retry': False, 'next_at': 0}
        self.b['_execute_auto'] = once
        result = await self.b['resolve_anomaly'](retry, {'action': 'retry_once'})
        self.assertTrue(result['queued'])
        await ran.wait(); await self.settle()
        self.assertEqual(db.execute('SELECT enabled FROM automations WHERE id=?', (retry,)).fetchone()[0], 0)

        blocker = asyncio.Event()
        async def hold(aid, manual=False):
            await blocker.wait()
            return {'retry': False, 'next_at': 0}
        self.b['_execute_auto'] = hold
        self.b['_queue_auto'](disable, True); await self.settle()
        await self.b['resolve_anomaly'](disable, {'action': 'disable'})
        row = db.execute('SELECT enabled,last_status,next_run_at FROM automations WHERE id=?', (disable,)).fetchone()
        self.assertEqual(tuple(row), (0, 'stopped', 0))

        other = sqlite3.connect(':memory:'); other.row_factory = sqlite3.Row
        old = self.b['conn']
        self.b['conn'] = other
        self.b['db_init']()
        self.assertEqual((await self.b['list_anomalies']())['count'], 0)
        self.b['conn'] = old
        other.close()

    async def test_account_stop_clears_dispatcher_workers_and_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = AccountRuntime('fixture', 'fixture', Path(folder), sqlite3.connect(':memory:'), 1)
            sleeper = runtime.spawn(asyncio.Event().wait())
            runtime.auto_queue_task = sleeper
            runtime.auto_workers[1] = sleeper
            runtime.auto_queued.add(2)
            runtime.auto_pending.append({'id': 2, 'manual': True})
            runtime.auto_tasks[1] = asyncio.get_running_loop().create_future()
            runtime.auto_tasks[2] = asyncio.get_running_loop().create_future()
            await runtime.stop(secure_storage=False)
            self.assertFalse(runtime.tasks)
            self.assertFalse(runtime.auto_tasks)
            self.assertFalse(runtime.auto_pending)
            self.assertFalse(runtime.auto_workers)
            self.assertIsNone(runtime.auto_queue_task)
            runtime.db.close()


if __name__ == '__main__':
    unittest.main()
