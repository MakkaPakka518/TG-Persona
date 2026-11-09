"""One-shot scheduled registration tests; no real Telegram or runtime data."""
from types import SimpleNamespace
import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, Mock

from test_backend import load_backend, HTTPException, message


class ScheduledRegisterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_backend()
        self.now = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
        self.b["now_local"] = lambda: self.now
        self.b["tzinfo"] = lambda: timezone.utc

    async def asyncTearDown(self):
        tasks = [task for task in self.b["_auto_running"].values() if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.b["conn"].close()

    async def create(self, **values):
        return (await self.b["create_auto"](self.b["AutoIn"](**values)))["id"]

    def row(self, aid):
        return self.b["conn"].execute("SELECT * FROM automations WHERE id=?", (aid,)).fetchone()

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def values(self, **changes):
        values = dict(type="scheduled_register", name="抢注测试", scheduled_at="2026-10-30T09:00:01",
                      target="@dummy", command="/start", btn_text="开始注册", keywords="请输入注册码",
                      reply_text="TEST-CODE", success_kw="注册成功", result_timeout=30, retries=0)
        values.update(changes)
        return values

    async def test_full_flow_runs_in_order_and_logs_success(self):
        phase, events = "menu", []

        async def click():
            nonlocal phase
            events.append(("click", "开始注册"))
            phase = "trigger"

        button = SimpleNamespace(text="开始注册", click=AsyncMock(side_effect=click))

        async def send(_entity, text):
            nonlocal phase
            events.append(("send", text))
            if text == "/start":
                phase = "menu"
                return SimpleNamespace(id=100)
            phase = "success"
            return SimpleNamespace(id=103)

        async def get_messages(_entity, **_kwargs):
            if phase == "menu":
                return [message(101, "请选择操作", buttons=[[button]])]
            if phase == "trigger":
                return [message(102, "请输入注册码")]
            if phase == "success":
                return [message(104, "注册成功")]
            return []

        self.b["client"] = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                                             get_messages=AsyncMock(side_effect=get_messages))
        self.b["resolve_entity"] = AsyncMock(return_value="entity")
        self.b["notify_event"] = Mock()
        aid = await self.create(**self.values())
        ok = await self.b["run_scheduled_register"](self.row(aid))
        self.assertTrue(ok)
        self.assertEqual(events, [("send", "/start"), ("click", "开始注册"), ("send", "TEST-CODE")])
        hit = self.b["conn"].execute("SELECT action,ok,reply FROM hits ORDER BY id DESC").fetchone()
        self.assertEqual(tuple(hit), ("scheduled_register", 1, "注册成功"))

    async def test_due_task_executes_once_then_disables(self):
        aid = await self.create(**self.values())
        self.b["run_scheduled_register"] = AsyncMock(return_value=True)
        self.advance(1)
        self.b["_scheduled_register_tick"]()
        await self.b["_auto_running"][aid]
        row = self.row(aid)
        self.assertEqual((row["enabled"], row["last_status"]), (0, "success"))
        self.b["_scheduled_register_tick"]()
        self.assertNotIn(aid, self.b["_auto_running"])
        self.b["run_scheduled_register"].assert_awaited_once()

    async def test_requires_complete_future_second_precision_configuration(self):
        for changes in ({"scheduled_at": "2026-10-30T09:01"}, {"scheduled_at": "2026-10-30T08:59:59"},
                        {"btn_text": ""}, {"keywords": ""}, {"reply_text": ""}, {"success_kw": ""}):
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                await self.create(**self.values(**changes))


if __name__ == "__main__":
    unittest.main()
