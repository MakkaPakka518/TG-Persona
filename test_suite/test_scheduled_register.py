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
        phase, events = "history", []

        async def click():
            nonlocal phase
            events.append(("click", "开始注册"))
            phase = "trigger"

        button = SimpleNamespace(text="开始注册", click=AsyncMock(side_effect=click))
        wrong = SimpleNamespace(text="其他功能", click=AsyncMock())

        async def send(_entity, text):
            nonlocal phase
            events.append(("send", text))
            if text == "/start":
                phase = "menu"
                return SimpleNamespace(id=100)
            phase = "success"
            return SimpleNamespace(id=103)

        async def get_messages(_entity, **_kwargs):
            if phase == "history":
                return [message(99, "旧菜单", buttons=[[wrong]])]
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
        aid = await self.create(**self.values(history_first=1))
        ok = await self.b["run_scheduled_register"](self.row(aid))
        self.assertTrue(ok)
        self.b["send_notify"].assert_called_once()
        self.assertIn("抢注测试", self.b["send_notify"].call_args.args[0])
        self.assertEqual(events, [("send", "/start"), ("click", "开始注册"), ("send", "TEST-CODE")])
        hit = self.b["conn"].execute("SELECT action,ok,reply FROM hits ORDER BY id DESC").fetchone()
        self.assertEqual(tuple(hit), ("scheduled_register", 1, "注册成功"))

    async def test_history_button_skips_command(self):
        phase, events = "history", []

        async def click():
            nonlocal phase
            events.append(("click", "开始注册"));phase = "trigger"

        button = SimpleNamespace(text="开始注册", click=AsyncMock(side_effect=click))

        async def send(_entity, text):
            nonlocal phase
            events.append(("send", text))
            if text == "/start":
                raise AssertionError("历史按钮命中后不应发送指令")
            phase = "success";return SimpleNamespace(id=103)

        async def get_messages(_entity, **_kwargs):
            if phase == "history": return [message(99, "旧菜单", buttons=[[button]])]
            if phase == "trigger": return [message(102, "请输入注册码")]
            if phase == "success": return [message(104, "注册成功")]
            return []

        self.b["client"] = SimpleNamespace(send_message=AsyncMock(side_effect=send), get_messages=AsyncMock(side_effect=get_messages))
        self.b["resolve_entity"] = AsyncMock(return_value="entity")
        aid = await self.create(**self.values(history_first=1))
        self.b["send_notify"].reset_mock()
        result = await self.b["run_scheduled_register"](self.row(aid), test_only=True)
        self.assertTrue(result)
        self.b["send_notify"].assert_not_called()
        self.assertEqual(events, [("click", "开始注册"), ("send", "TEST-CODE")])
        detail = self.b["conn"].execute("SELECT detail FROM hits ORDER BY id DESC").fetchone()[0]
        self.assertIn('"button_source": "history"', detail)

    async def test_flow_test_does_not_consume_schedule(self):
        aid = await self.create(**self.values())
        before = tuple(self.row(aid)[key] for key in ("enabled", "last_attempt", "last_status", "scheduled_at"))
        self.b["run_scheduled_register"] = AsyncMock(return_value=True)
        result = await self.b["test_scheduled_auto"](aid)
        after = tuple(self.row(aid)[key] for key in ("enabled", "last_attempt", "last_status", "scheduled_at"))
        self.assertEqual(result, {"ok": True, "tested": True})
        self.assertEqual(after, before)
        self.b["run_scheduled_register"].assert_awaited_once()
        self.assertTrue(self.b["run_scheduled_register"].await_args.kwargs["test_only"])

    async def test_unconfirmed_log_keeps_real_button_source(self):
        aid = await self.create(**self.values(history_first=1))
        self.b["_task_unconfirmed"](self.row(aid), "未收到指定内容", "历史卡片", "",
                                     {"button_source": "history", "test_only": True, "command": "/start"})
        detail = self.b["conn"].execute("SELECT detail FROM hits ORDER BY id DESC").fetchone()[0]
        self.assertIn('"button_source": "history"', detail)
        self.assertIn('"test_only": true', detail)
        self.assertIn('"command": "/start"', detail)

    async def test_closed_popup_retries_button_until_it_disappears(self):
        phase, clicks = "history", 0

        async def click():
            nonlocal phase, clicks
            clicks += 1
            if clicks == 1:
                return SimpleNamespace(message="自助注册已关闭，等待开启")
            phase = "trigger"
            return SimpleNamespace(message="")

        button = SimpleNamespace(text="创建", click=AsyncMock(side_effect=click))

        async def send(_entity, text):
            nonlocal phase
            self.assertNotEqual(text, "/start")
            phase = "success";return SimpleNamespace(id=103)

        async def get_messages(_entity, **_kwargs):
            if phase == "history": return [message(99, "旧菜单", buttons=[[button]])]
            if phase == "trigger": return [message(102, "请输入注册码")]
            if phase == "success": return [message(104, "注册成功")]
            return []

        self.b["client"] = SimpleNamespace(send_message=AsyncMock(side_effect=send), get_messages=AsyncMock(side_effect=get_messages))
        self.b["resolve_entity"] = AsyncMock(return_value="entity")
        aid = await self.create(**self.values(history_first=1, btn_text="创建", fail_kw="注册已关闭,等待开启", poll_interval=2))
        result = await self.b["run_scheduled_register"](self.row(aid))
        self.assertTrue(result)
        self.assertEqual(clicks, 2)
        detail = self.b["conn"].execute("SELECT detail FROM hits ORDER BY id DESC").fetchone()[0]
        self.assertIn('"closed_retries": 1', detail)
        self.assertIn('"retry_interval": 2', detail)

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
                        {"btn_text": ""}, {"keywords": ""}, {"reply_text": ""}, {"success_kw": ""}, {"history_first": 2}):
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                await self.create(**self.values(**changes))


if __name__ == "__main__":
    unittest.main()
