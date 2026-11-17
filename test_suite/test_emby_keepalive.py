"""观看保号：身份自洽、播放计划、拉流控量、执行链路与自愈（全部离线）"""
import asyncio
import json
import random
import sqlite3
import time
import unittest
from unittest.mock import patch, AsyncMock

import httpx
from httpx import AsyncClient as RealAsyncClient

import emby_keepalive as ka


def make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ka.init_schema(db)
    return db


class FakeEmby:
    """把 httpx 请求转到内存假站点，同时记录请求序列"""

    def __init__(self, stream_status=206, login_ok=True, cached_token_valid=True,
                 play_session="ps-1", size=900 * 1024 * 1024, bitrate=8 * 1000 * 1000):
        self.requests = []
        self.stream_status = stream_status
        self.login_ok = login_ok
        self.cached_token_valid = cached_token_valid
        self.play_session = play_session
        self.size = size
        self.bitrate = bitrate
        self.content = b"x" * (2 * 1024 * 1024)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path, request.url.query.decode()))
        body = b""
        if request.content:
            body = request.content
        if path == "/emby/System/Info/Public":
            return httpx.Response(200, json={"ProductName": "Emby"})
        if path == "/emby/System/Info":
            return httpx.Response(200, json={})
        if path == "/emby/Users/Me":
            return httpx.Response(200, json={"Id": "u1"}) if self.cached_token_valid \
                else httpx.Response(401)
        if path == "/emby/Users/AuthenticateByName":
            if not self.login_ok:
                return httpx.Response(401, json={"error": "bad"})
            return httpx.Response(200, json={"AccessToken": "tok-new", "User": {"Id": "u1"}})
        if path == "/emby/Users":
            return httpx.Response(200, json=[{"Id": "u1", "Name": "test"}])
        if path == "/emby/Items":
            return httpx.Response(200, json={"Items": [{
                "Id": "i1", "Name": "示例剧集", "RunTimeTicks": 30 * 60 * 10000000}]})
        if path.endswith("/PlaybackInfo"):
            return httpx.Response(200, json={
                "PlaySessionId": self.play_session,
                "MediaSources": [{
                    "Id": "ms1", "Name": "1080p", "Size": self.size,
                    "MediaStreams": [{"Type": "Video", "BitRate": self.bitrate}],}]})
        if "/Videos/" in path and path.endswith("/stream"):
            if self.stream_status not in (200, 206):
                return httpx.Response(self.stream_status)
            return httpx.Response(206, content=self.content,
                                  headers={"Content-Range": "bytes 0-1/999"})
        if path == "/emby/Sessions/Playing":
            return httpx.Response(204)
        if path == "/emby/Sessions/Playing/Progress":
            return httpx.Response(204)
        if path == "/emby/Sessions/Playing/Stopped":
            return httpx.Response(204)
        if path.endswith("/UserData"):
            return httpx.Response(204)
        return httpx.Response(404, json={"path": path})

    def factory(self, *args, **kwargs):
        return RealAsyncClient(transport=httpx.MockTransport(self.handler), timeout=5.0,
                                 trust_env=False)


def account_row(db, **overrides):
    payload = {"name": "测试保活", "base_url": "https://emby.example.com",
               "username": "demo", "password": "pw", "template_key": "senplayer"}
    payload.update(overrides)
    account = ka.create_account(db, payload)
    return ka.get_account(db, account["id"])


class ProfileTests(unittest.TestCase):
    def test_headers_are_consistent(self):
        profile = {"client": "SenPlayer", "version": "6.2.2", "device": "iPhone",
                   "device_id": "9c4e7a2b-6d8f-4b3a-9e2c-1f5d8a0b7e64", "ua": "SenPlayer/6.2.2"}
        headers = ka.build_headers(profile)
        self.assertEqual(headers["User-Agent"], "SenPlayer/6.2.2")
        self.assertIn('Client="SenPlayer"', headers["X-Emby-Authorization"])
        self.assertIn('DeviceId="9c4e7a2b-6d8f-4b3a-9e2c-1f5d8a0b7e64"', headers["X-Emby-Authorization"])
        self.assertEqual(headers["x-emby-device-id"], profile["device_id"])
        self.assertEqual(headers["x-emby-client-version"], "6.2.2")
        self.assertEqual(headers["Accept-Language"], "en-US,en;q=0.9")

    def test_validate_profile_rejects_mismatch(self):
        ok, msg = ka.validate_profile("SenPlayer", "6.2.2", "iPhone", "SenPlayer/6.2.2", "abc12345")
        self.assertTrue(ok, msg)
        for bad in (
            ("SenPlayer", "6.2.2", "iPhone", "Yamby/6.2.2", "abc12345"),
            ("Emby Web", "4.8.0", "Chrome", "Emby Web/4.8.0", "abc12345"),
            ("SenPlayer", "", "iPhone", "SenPlayer/6.2.2", "abc12345"),
            ("SenPlayer", "6.2.2", "iPhone", "SenPlayer/6.2.2", "no"),
        ):
            ok, _ = ka.validate_profile(*bad)
            self.assertFalse(ok, bad)

    def test_template_seeded_and_device_fixed(self):
        db = make_db()
        templates = ka.list_templates(db)
        self.assertTrue(any(t["key"] == "senplayer" and t["builtin"] for t in templates))
        first = account_row(db)
        second = account_row(db, name="第二个")
        self.assertTrue(ka.DEVICE_ID_RE.match(first["device_id"]))
        self.assertNotEqual(first["device_id"], second["device_id"])
        # 身份绑定后再次执行不再变
        again = ka.profile_of(first)
        self.assertEqual(again["device_id"], first["device_id"])


class RhythmTests(unittest.TestCase):
    def test_play_seconds_map(self):
        self.assertEqual(ka.play_sec_by_runtime(15 * 60 * 10000000), 90)
        self.assertEqual(ka.play_sec_by_runtime(60 * 60 * 10000000), 390)
        self.assertGreaterEqual(ka.play_sec_by_runtime(0), 90)
        self.assertLessEqual(ka.play_sec_by_runtime(0), 390)
        self.assertEqual(ka.play_sec_by_runtime(35 * 60 * 10000000), 223)

    def test_build_plan_is_monotonic_and_exact(self):
        random.seed(7)
        start, play = 100 * 10000000, 190
        end = start + play * 10000000
        plan, meta = ka.build_plan(start, end, play)
        self.assertEqual(len(plan), 19)
        self.assertEqual(plan[-1], end)
        for earlier, later in zip(plan, plan[1:]):
            self.assertGreaterEqual(later, earlier)
        self.assertTrue(meta["buffer_ticks"] >= 1)
        self.assertTrue(meta["seeks"])

    def test_cap_bytes(self):
        cap = ka.cap_bytes(4096 * 1024 * 1024, 8 * 1000 * 1000, 90, 400)
        self.assertEqual(cap, 400 * 1024 * 1024)
        need = int(90 * 8 * 1000 * 1000 / 8)
        self.assertEqual(ka.cap_bytes(10 * 1024 * 1024, 8 * 1000 * 1000, 90, 400), need)
        self.assertEqual(ka.cap_bytes(0, 0, 90, 120), 120 * 1024 * 1024)

    def test_next_run_not_on_same_day(self):
        for _ in range(50):
            offset = ka.next_run_offset_ms(3)
            self.assertGreaterEqual(offset, int(1.4 * 86400000))
            self.assertLessEqual(offset, int(2.0 * 86400000) + 1)


class RunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = make_db()
        self.fake = FakeEmby()

    async def _run(self, account, **kwargs):
        kwargs.setdefault("probe", False)
        with patch.object(ka.httpx, "AsyncClient", self.fake.factory), \
             patch.object(ka, "HEARTBEAT_SEC", 0.01), \
             patch.object(ka, "CLEAN_DELAY_SEC", 0), \
             patch.object(ka, "SEGMENT_MB", 1), \
             patch.object(ka, "STALL_RANGE", (0, 0)):
            return await ka.run_keepalive(self.db, account, key="test", **kwargs)

    async def test_happy_path_and_cleanup(self):
        account = account_row(self.db)
        result = await self._run(account)
        self.assertTrue(result["ok"], result["msg"])
        names = [s["name"] for s in result["steps"]]
        for expected in ("正在打开媒体库", "正在加载媒体库", "正在浏览详情页",
                         "正在获取播放地址", "视频加载中", "开始观看",
                         "本次观看结束", "正在退出播放", "已清除观看记录"):
            self.assertIn(expected, names)
        self.assertGreater(result["bytes"], 0)
        paths = [p for _m, p, _q in self.fake.requests]
        self.assertEqual(paths.count("/emby/Sessions/Playing"), 1)
        self.assertGreaterEqual(paths.count("/emby/Sessions/Playing/Progress"), 1)
        self.assertIn("/emby/Users/u1/Items/i1/UserData", paths)

    async def test_expired_token_relogin(self):
        account = account_row(self.db)
        self.db.execute("UPDATE emby_accounts SET access_token='old-token', user_id='u1' WHERE id=?",
                        (account["id"],))
        self.db.commit()
        account = ka.get_account(self.db, account["id"])
        self.fake.cached_token_valid = False
        result = await self._run(account)
        self.assertTrue(result["ok"], result["msg"])
        names = [s["name"] for s in result["steps"]]
        self.assertIn("正在验证账号", names)
        self.assertIn("/emby/Users/AuthenticateByName", [p for _m, p, _q in self.fake.requests])

    async def test_not_playable_does_not_report_session(self):
        account = account_row(self.db)
        self.fake.stream_status = 404
        result = await self._run(account)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_playable")
        paths = [p for _m, p, _q in self.fake.requests]
        self.assertNotIn("/emby/Sessions/Playing", paths)

    async def test_clean_disabled_skips_userdata(self):
        account = account_row(self.db, clean_userdata=0)
        result = await self._run(account)
        self.assertTrue(result["ok"], result["msg"])
        paths = [p for _m, p, _q in self.fake.requests]
        self.assertNotIn("/emby/Users/u1/Items/i1/UserData", paths)

    async def test_probe_mode_checks_only(self):
        account = account_row(self.db, mode="probe")
        result = await self._run(account)
        self.assertTrue(result["ok"])
        paths = [p for _m, p, _q in self.fake.requests]
        self.assertNotIn("/emby/Sessions/Playing", paths)

    async def test_startup_recover_stops_leftover_session(self):
        account = account_row(self.db)
        self.db.execute("INSERT INTO emby_jobs(account_id,status,item_id,play_session_id,"
                        "media_source_id,play_sec,end_ticks,updated_at,started_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (account["id"], "running", "i1", "ps-old", "ms1", 60,
                         600000000, int(time.time()) - 60, int(time.time()) - 120))
        self.db.commit()
        with patch.object(ka.httpx, "AsyncClient", self.fake.factory):
            count = await ka.startup_recover(self.db, "test")
        self.assertEqual(count, 1)
        paths = [p for _m, p, _q in self.fake.requests]
        self.assertIn("/emby/Sessions/Playing/Stopped", paths)

    async def test_failure_sets_retry_without_consuming_interval(self):
        account = account_row(self.db)
        self.fake.login_ok = False
        self.db.execute("UPDATE emby_accounts SET access_token='' WHERE id=?", (account["id"],))
        self.db.commit()
        account = ka.get_account(self.db, account["id"])
        result = await self._run(account)
        self.assertFalse(result["ok"])
        cfg = ka.get_config(self.db)
        ka._finish(self.db, "test", account, 0, result, cfg, None)
        row = ka.get_account(self.db, account["id"])
        self.assertEqual(row["last_result"], "fail")
        self.assertGreaterEqual(row["retry_at"], int(time.time()) + 3 * 3600)


class QueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = make_db()

    async def test_cancel_waiting_job_removes_it(self):
        account = account_row(self.db)
        job = ka.enqueue(self.db, account["id"], "manual")
        self.assertTrue(ka.cancel_job(self.db, job))
        self.assertEqual(ka.queue_snapshot(self.db), [])

    async def test_advance_respects_concurrency(self):
        first = account_row(self.db, name="A")
        second = account_row(self.db, name="B")
        ka.enqueue(self.db, first["id"], "manual")
        ka.enqueue(self.db, second["id"], "manual")
        with patch.object(ka, "_run_job", new=AsyncMock()) as run:
            await ka.advance(self.db, "test")
            await asyncio.sleep(0)
        rows = self.db.execute("SELECT account_id,status FROM emby_jobs").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(ka.queue_snapshot(self.db)[0]["status"], "running")
        self.assertEqual(run.await_count, 1)


if __name__ == "__main__":
    unittest.main()
