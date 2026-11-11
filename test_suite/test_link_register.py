"""Group deep-link registration tests; no real Telegram, network or runtime data."""
from types import SimpleNamespace
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_backend import load_backend, message


class LinkRegisterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_backend()
        self.b["resolve_entity"] = AsyncMock(return_value="entity")
        self.b["notify_event"] = Mock()
        self.errors = self.b["errors"]

    async def asyncTearDown(self):
        self.b["conn"].close()

    async def create(self, **values):
        body = dict(type="link_register", name="链接抢注",
                    btn_text="注册", keywords="已进入注册状态", success_kw="注册成功",
                    groups="", result_timeout=3, retries=0, link_mode=1, link_limit=4, link_wait=2)
        body.update(values)
        aid = (await self.b["create_auto"](self.b["AutoIn"](**body)))["id"]
        self.b["set_link_credential"](aid, "测试用户", "测试安全码")
        return aid

    def row(self, aid):
        return self.b["conn"].execute("SELECT * FROM automations WHERE id=?", (aid,)).fetchone()

    def link(self, payload):
        return "https://t.me/testkky_makka_bot?start=" + payload

    def test_parses_links_from_text_entities_and_buttons(self):
        text = "抢 " + self.link("TEST-30-Register_A1") + " 和 t.me/testkky_makka_bot?start=TEST-30-Register_B2"
        hidden = SimpleNamespace(url="https://t.me/testkky_makka_bot?start=TEST-30-Register_C3")
        button = SimpleNamespace(url="https://t.me/testkky_makka_bot?start=TEST-30-Register_D4")
        other = SimpleNamespace(url="https://t.me/evil_bot?start=TEST-30-Register_E5")
        msg = SimpleNamespace(raw_text=text, entities=[hidden, other],
                              reply_markup=SimpleNamespace(rows=[
                                  SimpleNamespace(buttons=[button, SimpleNamespace(url="https://example.com")])]))
        links = self.b["_message_links"](msg)
        self.assertEqual([item["payload"] for item in links], [
            "TEST-30-Register_A1", "TEST-30-Register_B2", "TEST-30-Register_C3",
            "TEST-30-Register_E5", "TEST-30-Register_D4"])
        self.assertEqual(links[3]["bot"], "evil_bot")
        self.assertEqual(self.b["_message_links"](SimpleNamespace(raw_text="https://t.me/a/b?start=X")), [])
        self.assertEqual(self.b["_message_links"](SimpleNamespace(raw_text="https://t.me/x?start=short")), [])
        for payload in ("appeal", "TEST-30-Other_code", "TEST-x-Register_code", "TEST-0-Register_code"):
            self.assertEqual(self.b["_message_links"](SimpleNamespace(
                raw_text=self.link(payload))), [])

    async def test_invalid_link_echo_advances_to_next_once(self):
        aid = await self.create()
        sends, events, phase = [], [], {"v": "old"}

        async def send(_entity, text, **_kwargs):
            sends.append(text)
            events.append(("send", text))
            if text == "/start TEST-30-Register_NEW2":
                phase["v"] = "new"
            return SimpleNamespace(id=50)

        async def get_messages(_entity, **_kwargs):
            if phase["v"] == "old":
                return [message(101, "此 TEST-30-Register_OLD1 注册码已被使用")]
            return [message(102, "已进入注册状态")]

        self.b["client"] = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                                           get_messages=AsyncMock(side_effect=get_messages))
        ok = await self.b["run_link_register"](
            self.row(aid), [{"bot": "testkky_makka_bot", "payload": "TEST-30-Register_OLD1"},
                            {"bot": "testkky_makka_bot", "payload": "TEST-30-Register_NEW2"}])
        self.assertFalse(ok)
        self.assertEqual(events, [("send", "/start TEST-30-Register_OLD1"),
                                  ("send", "/start TEST-30-Register_NEW2")])
        hit = self.b["conn"].execute("SELECT action,ok,detail FROM hits ORDER BY id DESC").fetchone()
        self.assertEqual(hit["action"], "link_register")
        self.assertEqual(hit["ok"], 0)
        self.assertIn("TEST-30-Register_NEW2", hit["detail"])
        self.b["notify_event"].assert_not_called()
        self.b["send_notify"].assert_not_called()

    async def test_invalid_reply_without_payload_echo_stops(self):
        aid = await self.create()
        self.b["client"] = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(id=100)),
            get_messages=AsyncMock(return_value=[message(101, "注册码已被使用")]))
        ok = await self.b["run_link_register"](
            self.row(aid), [{"bot": "testkky_makka_bot", "payload": "TEST-30-Register_OLD1"},
                            {"bot": "testkky_makka_bot", "payload": "TEST-30-Register_NEW2"}])
        self.assertFalse(ok)
        self.assertEqual(self.b["client"].send_message.await_count, 1)
        detail = json.loads(self.b["conn"].execute(
            "SELECT detail FROM hits ORDER BY id DESC").fetchone()["detail"])
        self.assertEqual(detail["status"], "unconfirmed")
        self.b["notify_event"].assert_not_called()

    async def test_existing_account_keyword_stops_relay_without_notification(self):
        aid = await self.create(existing_kw="无法启用续期功能")
        sends = []

        async def send(_entity, text, **_kwargs):
            sends.append(text)
            return SimpleNamespace(id=50)

        self.b["client"] = SimpleNamespace(
            send_message=AsyncMock(side_effect=send),
            get_messages=AsyncMock(return_value=[message(
                101, "很遗憾，您使用的是注册码，无法启用续期功能，请悉知")]))
        ok = await self.b["run_link_register"](
            self.row(aid), [{"bot": "testkky_makka_bot", "payload": "TEST-30-Register_FIRST"},
                            {"bot": "testkky_makka_bot", "payload": "TEST-30-Register_SECOND"}])
        self.assertFalse(ok)
        self.assertEqual(sends, ["/start TEST-30-Register_FIRST"])
        self.b["notify_event"].assert_not_called()
        detail = json.loads(self.b["conn"].execute(
            "SELECT detail FROM hits ORDER BY id DESC").fetchone()["detail"])
        self.assertEqual(detail["status"], "existing_account")

    async def test_invalid_reply_after_short_wait_still_caught_by_long_window(self):
        aid = await self.create(link_wait=2, result_timeout=4)
        polls = {"n": 0}
        sent = []

        async def send(_entity, text, **_kwargs):
            sent.append(text)
            return SimpleNamespace(id=50)

        async def get_messages(_entity, **_kwargs):
            polls["n"] += 1
            if polls["n"] < 8:
                return []
            if "TEST-30-Register_NEW2" in sent[-1]:
                return [message(200, "邀请卡片", buttons=[[SimpleNamespace(text="注册", click=AsyncMock())]])]
            return [message(150, "TEST-30-Register_OLD1 注册码已被使用")]

        self.b["client"] = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                                           get_messages=AsyncMock(side_effect=get_messages))
        ok = await self.b["run_link_register"](
            self.row(aid), [{"bot": "testkky_makka_bot", "payload": "TEST-30-Register_OLD1"},
                            {"bot": "testkky_makka_bot", "payload": "TEST-30-Register_NEW2"}])
        self.assertFalse(ok)
        self.assertEqual(sent, ["/start", "/start TEST-30-Register_OLD1",
                                "/start TEST-30-Register_NEW2"])

    async def test_ready_text_in_popup_skips_extra_wait(self):
        aid = await self.create(result_timeout=3)
        button = SimpleNamespace(text="注册", click=AsyncMock(
            return_value=SimpleNamespace(message="您已进入注册状态")))
        phase = {"v": "card"}

        async def send(_entity, text, **_kwargs):
            if text.startswith("测试用户"):
                phase["v"] = "success"
            return SimpleNamespace(id=50)

        async def get_messages(_entity, **_kwargs):
            if phase["v"] == "card":
                return [message(101, "邀请卡片", buttons=[[button]])]
            return [message(105, "注册成功")]

        self.b["client"] = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                                           get_messages=AsyncMock(side_effect=get_messages))
        ok = await self.b["run_link_register"](
            self.row(aid), [{"bot": "testkky_makka_bot", "payload": "TEST-30-Register_OK1"}])
        self.assertTrue(ok)
        button.click.assert_awaited_once()

    async def test_valid_card_registers_once_and_stops_relay(self):
        aid = await self.create()
        sends, events, phase = [], [], {"v": "card"}
        button = SimpleNamespace(text="注册", click=AsyncMock())

        async def send(_entity, text, **_kwargs):
            sends.append(text)
            events.append(("send", text))
            if text.startswith("测试用户"):
                phase["v"] = "success"
            return SimpleNamespace(id=50)

        async def click():
            phase["v"] = "ready"

        button.click.side_effect = click

        async def get_messages(_entity, **_kwargs):
            if phase["v"] == "card":
                return [message(101, "邀请卡片", buttons=[[button]])]
            if phase["v"] == "success":
                return [message(105, "注册成功")]
            return [message(104, "您已进入注册状态")]

        self.b["client"] = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                                           get_messages=AsyncMock(side_effect=get_messages))
        ok = await self.b["run_link_register"](
            self.row(aid), [{"bot": "testkky_makka_bot", "payload": "TEST-30-Register_OK1"},
                            {"bot": "testkky_makka_bot", "payload": "TEST-30-Register_NEVER2"}],
            source_chat=-100123)
        self.assertTrue(ok)
        self.assertEqual(events, [("send", "/start TEST-30-Register_OK1"),
                                  ("send", "测试用户 测试安全码")])
        button.click.assert_awaited_once()
        hit = self.b["conn"].execute("SELECT ok,detail FROM hits ORDER BY id DESC").fetchone()
        self.assertEqual(hit["ok"], 1)
        self.assertNotIn("测试安全码", hit["detail"])
        self.b["send_notify"].assert_called_once()

    async def test_success_can_reply_to_source_group(self):
        aid = await self.create(success_reply_enabled=1, success_reply_text="拿下",
                                success_reply_delay=7)
        sends, phase = [], {"v": "card"}

        async def send(entity, text, **_kwargs):
            sends.append((entity, text))
            if text.startswith("测试用户"):
                phase["v"] = "success"
            return SimpleNamespace(id=50)

        async def click():
            phase["v"] = "ready"

        button = SimpleNamespace(text="注册", click=AsyncMock(side_effect=click))

        async def get_messages(_entity, **_kwargs):
            if phase["v"] == "card":
                return [message(101, "邀请卡片", buttons=[[button]])]
            if phase["v"] == "success":
                return [message(105, "注册成功")]
            return [message(104, "您已进入注册状态")]

        self.b["client"] = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                                           get_messages=AsyncMock(side_effect=get_messages))
        with patch.object(asyncio, "sleep", new=AsyncMock()) as sleep:
            ok = await self.b["run_link_register"](
                self.row(aid), [{"bot": "testkky_makka_bot", "payload": "TEST-30-Register_OK1"}],
                source_chat=-100123)
        self.assertTrue(ok)
        sleep.assert_awaited_once_with(7)
        self.assertEqual(sends[-1], (-100123, "拿下"))
        detail = json.loads(self.b["conn"].execute(
            "SELECT detail FROM hits ORDER BY id DESC").fetchone()["detail"])
        self.assertEqual(detail["group_reply"], "拿下")
        self.assertEqual(detail["group_reply_delay"], 7)

    async def test_random_mode_sends_only_one_link(self):
        aid = await self.create(link_mode=0)
        self.b["client"] = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(id=100)),
            get_messages=AsyncMock(return_value=[message(101, "注册码已被使用 TEST-30-Register_B2")]))
        links = [{"bot": "testkky_makka_bot", "payload": "TEST-30-Register_A1"},
                 {"bot": "testkky_makka_bot", "payload": "TEST-30-Register_B2"}]
        await self.b["run_link_register"](self.row(aid), links)
        self.assertEqual(self.b["client"].send_message.await_count, 1)

    async def test_infers_bot_and_rejects_missing_credential(self):
        aid = await self.create()
        self.b["client"] = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(id=50)),
            get_messages=AsyncMock(return_value=[message(101, "TEST-30-Register_A1 注册码已被使用")]))
        ok = await self.b["run_link_register"](
            self.row(aid), [{"bot": "evil_bot", "payload": "TEST-30-Register_A1"}])
        self.assertFalse(ok)
        self.b["resolve_entity"].assert_awaited_with("@evil_bot")
        sent_before = self.b["client"].send_message.await_count
        self.b["_delete_link_credential"](aid)
        ok = await self.b["run_link_register"](
            self.row(aid), [{"bot": "testkky_makka_bot", "payload": "TEST-30-Register_A1"}])
        self.assertFalse(ok)
        self.assertEqual(self.b["client"].send_message.await_count, sent_before)

    def test_credential_roundtrip_and_not_in_task_row(self):
        self.b["set_link_credential"](7, "用户甲", "秘密乙")
        self.assertEqual(self.b["get_link_credential"](7), {"u": "用户甲", "s": "秘密乙"})
        self.b["set_link_credential"](7, "", "")
        self.assertEqual(self.b["get_link_credential"](7), {"u": "用户甲", "s": "秘密乙"})
        self.b["_delete_link_credential"](7)
        self.assertEqual(self.b["get_link_credential"](7), {})

    async def test_task_list_never_returns_credentials(self):
        aid = await self.create()
        self.b["notify_event"] = Mock()
        result = await self.b["list_autos"]()
        row = next(item for item in result["autos"] if item["id"] == aid)
        self.assertTrue(row["link_credential_set"])
        self.assertNotIn("测试安全码", json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
