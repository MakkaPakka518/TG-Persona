#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TG Persona —— Telegram 人形分身 (Userbot Watcher)

部署在小 VPS 上,登录你自己的 Telegram 账号:
- 可视化面板 (http://IP:端口) 完成登录、选群、配关键词
- 监听的群里有人发你设定的关键词时,以你的身份回复文本 / 点表情

启动:  uvicorn main:app --host 0.0.0.0 --port 8090 --no-proxy-headers
或:    python main.py            (内置 uvicorn,读取 .env 的 PORT)
安全配置: 默认不信任转发头。仅反向代理部署时配置 TRUSTED_PROXIES=127.0.0.1,::1
(支持逗号分隔 IP/CIDR,不要配置全网 CIDR),代理须覆盖 X-Forwarded-For / Proto。
外部 uvicorn 必须加 --no-proxy-headers,避免 request.client 在应用鉴权前被改写。
公网面板推荐 HTTPS;HTTPS/可信代理的 HTTPS 请求使用 Secure cookie,直连 HTTP 兼容。
"""
import asyncio
import base64
from collections import OrderedDict
import ipaddress
import threading
from typing import Optional
import math
import struct
import zlib
import json
import logging
import random
import re
import secrets
import sqlite3
import sys
import time
import os
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo      # Python 3.9+
except ImportError:                     # 3.8 没有 zoneinfo,退化为固定偏移
    ZoneInfo = None
from contextlib import asynccontextmanager
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None  # 通知/AI 需要 httpx;缺失时降级 urllib

from telethon import TelegramClient, errors, events
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import updater

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(mode=0o700, exist_ok=True)
DB_PATH = DATA / "config.db"
SESSION_PATH = str(DATA / "tg")           # Telethon 会话文件 (data/tg.session)
PW_FILE = DATA / "panel_password.txt"
AVATAR_FILE = DATA / "avatar.jpg"         # 登录时自动拉取的 TG 头像缓存

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("persona")

# ---------------- 配置存储 (SQLite) ----------------
conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
conn.row_factory = sqlite3.Row
try:  # 并发读写更稳,减少 database is locked
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=8000")
except sqlite3.Error:
    pass


def db_init():
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS panel_sessions (
            token TEXT PRIMARY KEY, created INTEGER, ip TEXT, ua TEXT, last_seen INTEGER
        );
        CREATE TABLE IF NOT EXISTS watched_groups (chat_id TEXT PRIMARY KEY, name TEXT, added_at INTEGER);
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keywords TEXT NOT NULL,
            regex INTEGER DEFAULT 0,
            action TEXT NOT NULL DEFAULT 'reply',        -- reply | react | both
            value TEXT NOT NULL,                          -- 回复内容(||分隔多条) 或 表情(react)
            reaction TEXT DEFAULT '',                     -- 表情(both 时使用,react 时留空)
            enabled INTEGER DEFAULT 1,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER, chat_id TEXT, rule_id INTEGER, msg_id INTEGER, action TEXT,
            content TEXT DEFAULT '', reply TEXT DEFAULT '', grp_name TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS automations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,                 -- signin | grabcodes | redpacket
            name TEXT NOT NULL,
            target TEXT DEFAULT '',             -- signin: @机器人 或 群ID
            command TEXT DEFAULT '',            -- signin: 签到命令
            win_start TEXT DEFAULT '08:00',     -- signin: 时间窗口开始 hh:mm
            win_end TEXT DEFAULT '12:00',       -- signin: 时间窗口结束 hh:mm
            click_btn INTEGER DEFAULT 0,        -- signin: 签到后自动点击回复按钮
            btn_text TEXT DEFAULT '',           -- signin: 按钮文字(留空自动匹配签到/确认/领取等)
            success_kw TEXT DEFAULT '',         -- signin: 成功关键词(机器人回复含它才算成功)
            retries INTEGER DEFAULT 2,          -- signin: 发送失败重试次数
            groups TEXT DEFAULT '',             -- grabcodes/redpacket: 指定群(@群名或ID,逗号分隔;留空=全部监听群)
            keywords TEXT DEFAULT '',           -- 触发关键词(逗号分隔)
            action TEXT DEFAULT 'reply',        -- grabcodes: reply(回复消息) | send(发送命令)
            reply_text TEXT DEFAULT '',         -- 回复/命令文本,{code} 替换为提取的邀请码
            cooldown INTEGER DEFAULT 30,        -- 同群同任务冷却(秒)
            poll_interval INTEGER DEFAULT 0,    -- grabcodes: 轮询探测开注间隔(秒,0=关闭仅监听)
            custom_steps TEXT DEFAULT '',       -- custom: 多步任务 JSON
            chain_retry INTEGER DEFAULT 1,      -- custom: 任务链级重试
            fail_kw TEXT DEFAULT '',            -- signin: 失败关键词(回复含它判失败重试)
            is_chat INTEGER DEFAULT 0,          -- signin: 目标是群组而非机器人
            points_pat TEXT DEFAULT '',         -- signin: 积分提取正则(日志显示 +N)
            enabled INTEGER DEFAULT 1,
            last_at TEXT DEFAULT '',            -- 最近执行(签到存日期 YYYY-MM-DD,防当日重复)
            created_at INTEGER
        );
        """
    )
    # 兼容旧库:补充新增列
    for col, ddl in (
        ("btn_text", "TEXT DEFAULT ''"),
        ("success_kw", "TEXT DEFAULT ''"),
        ("retries", "INTEGER DEFAULT 2"),
        ("fail_kw", "TEXT DEFAULT ''"),
        ("is_chat", "INTEGER DEFAULT 0"),
        ("points_pat", "TEXT DEFAULT ''"),
        ("poll_interval", "INTEGER DEFAULT 0"),
        ("custom_steps", "TEXT DEFAULT ''"),
        ("chain_retry", "INTEGER DEFAULT 1"),
        ("signin_captcha", "INTEGER DEFAULT 0"),
        ("btn_emoji", "TEXT DEFAULT ''"),
        ("click_times", "INTEGER DEFAULT 1"),
        ("click_interval", "INTEGER DEFAULT 300"),
        ("command_delay", "REAL DEFAULT 1.6"),
        ("button_delay", "REAL DEFAULT 2.0"),
        ("refresh_buttons", "INTEGER DEFAULT 1"),
        ("last_attempt", "INTEGER DEFAULT 0"),
        ("retry_count", "INTEGER DEFAULT 0"),
        ("retry_day", "TEXT DEFAULT ''"),
        ("next_run_at", "INTEGER DEFAULT 0"),
        ("last_status", "TEXT DEFAULT ''"),
    ):
        try:
            conn.execute(f"ALTER TABLE automations ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    for col, ddl in (("reaction", "TEXT DEFAULT ''"), ("groups", "TEXT DEFAULT ''")):
        try:
            conn.execute(f"ALTER TABLE rules ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    # 老记录没有 detail(这次才加的列),ok 默认 1 会显示成「成功」,改成 NULL 让前端显示「—」
    try:
        conn.execute("UPDATE hits SET ok=NULL WHERE (detail IS NULL OR detail='') AND ok=1")
    except sqlite3.Error:
        pass
    for col, ddl in (("content", "TEXT DEFAULT ''"), ("reply", "TEXT DEFAULT ''"), ("grp_name", "TEXT DEFAULT ''"),
                     ("ok", "INTEGER DEFAULT 1"), ("sender", "TEXT DEFAULT ''"), ("detail", "TEXT DEFAULT ''")):
        try:
            conn.execute(f"ALTER TABLE hits ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    # 重启时不把未完成的执行误记为成功;已有 next_run_at 保留退避保护。
    conn.execute("UPDATE automations SET last_status='interrupted' WHERE last_status='running'")
    conn.commit()


def add_hit(action, ok, *, chat_id="", rule_id=0, msg_id=0, content="", reply="",
            grp_name="", sender="", detail=None):
    """统一写命中日志:kind(动作) + 成败 + 结构化详情(前端展开用)"""
    try:
        detail_json = json.dumps(detail or {}, ensure_ascii=False)[:2000]
    except Exception:
        detail_json = "{}"
    conn.execute(
        "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action,content,reply,grp_name,ok,sender,detail) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()), str(chat_id), rule_id, msg_id, action, str(content)[:500],
         str(reply)[:500], str(grp_name)[:100], 1 if ok else 0, str(sender)[:80], detail_json),
    )
    conn.commit()


def purge_hits(days=None):
    """删除超过 N 天的命中日志(默认取设置 log_keep_days,0=永久保留)"""
    try:
        d = int(get_setting("log_keep_days", "30") if days is None else days)
    except Exception:
        d = 30
    if d <= 0:
        return 0
    cutoff = int(time.time()) - d * 86400
    cur = conn.execute("DELETE FROM hits WHERE ts < ?", (cutoff,))
    conn.commit()
    n = cur.rowcount or 0
    if n:
        try:
            conn.execute("VACUUM")
            conn.commit()
        except sqlite3.Error:
            pass
        log.info("已清理 %s 条过期日志(早于 %s 天)", n, d)
    return n


def get_setting(key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?",
        (key, value, value),
    )
    conn.commit()


db_init()

# ---------------- 时区 ----------------
def tz_name() -> str:
    return get_setting("tz") or os.environ.get("TZ") or "Asia/Shanghai"


# 系统缺 tzdata(精简镜像)时的固定偏移兜底
_TZ_FALLBACK = {
    "Asia/Shanghai": 8, "Asia/Hong_Kong": 8, "Asia/Taipei": 8, "Asia/Singapore": 8,
    "Asia/Tokyo": 9, "Asia/Seoul": 9, "Asia/Bangkok": 7, "Asia/Kolkata": 5.5,
    "UTC": 0, "Europe/London": 0, "Europe/Berlin": 1, "Europe/Moscow": 3,
    "America/New_York": -5, "America/Los_Angeles": -8,
}


def tz_valid(name: str) -> bool:
    if not name:
        return False
    if name in _TZ_FALLBACK:
        return True
    if ZoneInfo is not None:
        try:
            ZoneInfo(name)
            return True
        except Exception:
            pass
    return bool(re.fullmatch(r"[+-]\d{1,2}(:\d{2})?", name))


def tzinfo():
    name = tz_name()
    try:
        if ZoneInfo is None:
            raise RuntimeError("no zoneinfo")
        return ZoneInfo(name)
    except Exception:
        off = _TZ_FALLBACK.get(name)
        if off is None:
            m = re.fullmatch(r"([+-])(\d{1,2})(?::(\d{2}))?", name or "")
            off = 8
            if m:
                off = int(m.group(2)) + (int(m.group(3) or 0) / 60)
                off = -off if m.group(1) == "-" else off
        return timezone(timedelta(hours=off))


def now_local():
    """当前时间(按面板设置的时区),签到窗口/日期一律用它"""
    return datetime.now(tzinfo())


# ---------------- 监听群/规则内存缓存 ----------------
_cache = {"at": 0.0, "watched": set(), "rules": []}


def invalidate_cache():
    _cache["at"] = 0.0


def cache_snapshot():
    """5 秒内复用缓存,避免每条群消息都查一次 SQLite 阻塞事件循环"""
    if time.monotonic() - _cache["at"] > 5:
        _cache["watched"] = {r["chat_id"] for r in conn.execute("SELECT chat_id FROM watched_groups").fetchall()}
        _cache["rules"] = conn.execute("SELECT * FROM rules WHERE enabled=1 ORDER BY id").fetchall()
        _cache["at"] = time.monotonic()
    return _cache["watched"], _cache["rules"]


# ---------------- 全局状态 ----------------
client = None              # Telethon TelegramClient (登录后持活)
tg_state = {"phase": "idle", "phone": "", "api_ok": False, "me": None, "err": None}
tg_phone = ""
hits_count = {"total": 0}
watcher_task = None
handler_registered = False
last_hit_ts = {}           # (chat_id, rule_id) -> 上次命中时间
_running = {"paused": False}

# 通知 / 验证码 / 水群 / AI 状态
flock_buf = {}             # (chat_id, text) -> [ts1, ts2, ...] 相同消息时间戳(水群从众)
_ocr = None                # ddddocr 实例(惰性加载)
_ai_cfg = {}               # AI 验证码配置缓存

def send_notify(title, text=""):
    """失败/成功通知:Bark + TG Bot(可配置),后台异步发送不阻塞主流程"""
    bark = str(get_setting("notify_bark", "") or "").strip()
    bt = str(get_setting("notify_bt", "") or "").strip()
    bc = str(get_setting("notify_bc", "") or "").strip()

    async def _do():
        try:
            if bark:
                import urllib.parse
                url = bark.rstrip("/")
                if "://" not in url:
                    url = f"https://api.day.app/{url}"
                payload = "/" + urllib.parse.quote(title) + "/" + urllib.parse.quote(text)
                if httpx is not None:
                    try:
                        async with httpx.AsyncClient(timeout=8) as c:
                            await c.post(url + payload, params={"group": "TG-Persona"})
                    except Exception:
                        import urllib.request
                        urllib.request.urlopen(url + payload, timeout=8)
                else:
                    import urllib.request
                    urllib.request.urlopen(url + payload, timeout=8)
            if bt and bc:
                if httpx is not None:
                    async with httpx.AsyncClient(timeout=10) as c:
                        await c.post(f"https://api.telegram.org/bot{bt}/sendMessage",
                                     json={"chat_id": bc, "text": f"[TG-Persona] {title}\n{text}"})
                else:
                    import urllib.request
                    import urllib.parse as _up
                    req = urllib.request.Request(
                        f"https://api.telegram.org/bot{bt}/sendMessage",
                        data=json.dumps({"chat_id": bc, "text": f"[TG-Persona] {title}\n{text}"}).encode(),
                        headers={"Content-Type": "application/json"},
                    )
                    urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log.warning("通知发送失败: %s", e)

    try:
        asyncio.get_event_loop().create_task(_do())
    except Exception:
        pass


def notify_event(kind, name, detail=""):
    """kind: success/fail;按设置决定是否推送"""
    key = "notify_on_success" if kind == "success" else "notify_on_fail"
    if get_setting(key, "0") == "1":
        send_notify(f"{'✅' if kind=='success' else '❌'} {name}", detail)


def load_ai_cfg():
    _ai_cfg.update({
        "base": str(get_setting("ai_base", "") or "").strip(),
        "key": str(get_setting("ai_key", "") or "").strip(),
        "model": str(get_setting("ai_model", "") or "").strip(),
    })


_ocr_guard = threading.Lock()
_ai_test_lock = asyncio.Lock()
MAX_TEST_IMAGE = 2 * 1024 * 1024


def _image_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("请上传 PNG、JPG 或 WebP 图片")


def _decode_test_image(value):
    try:
        match = re.fullmatch(r"data:(image/(?:png|jpeg|jpg|webp));base64,([A-Za-z0-9+/=\r\n]+)", value)
        if not match:
            raise ValueError("图片格式无效，请重新选择 PNG、JPG 或 WebP")
        data = base64.b64decode(match[2], validate=True)
        if not data or len(data) > MAX_TEST_IMAGE:
            raise ValueError("图片不能为空，大小不能超过 2 MB")
        mime = _image_mime(data)
        declared = "image/jpeg" if match[1] == "image/jpg" else match[1]
        if mime != declared:
            raise ValueError("图片内容与格式不匹配")
        if mime == "image/png":
            if len(data) < 45 or data[12:16] != b"IHDR" or data[-8:-4] != b"IEND":
                raise ValueError("PNG 图片损坏或不完整，请重新上传")
            width, height = struct.unpack("!II", data[16:24])
            if not width or not height or width * height > 16_000_000:
                raise ValueError("图片分辨率过大，请裁剪为验证码区域")
        elif mime == "image/jpeg" and (len(data) < 16 or not data.endswith(b"\xff\xd9")):
            raise ValueError("JPG 图片损坏或不完整，请重新上传")
        elif mime == "image/webp" and (len(data) < 20 or struct.unpack("<I", data[4:8])[0] + 8 != len(data)):
            raise ValueError("WebP 图片损坏或不完整，请重新上传")
        return data
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from None


def _safe_ai_error(error, key=""):
    text = str(error)
    if key:
        text = text.replace(key, "[密钥已隐藏]")
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [已隐藏]", text)
    return text[:300] or type(error).__name__


def _ai_endpoint(base):
    from urllib.parse import urlsplit
    base = str(base or "").strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("AI API 地址应为有效的 HTTP(S) 基础地址，不含密码或查询参数")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


async def _call_ai(cfg, content, max_tokens=512):
    if not cfg.get("base") or not cfg.get("key") or not cfg.get("model"):
        raise ValueError("请填写 AI API 地址、密钥和模型名")
    endpoint = _ai_endpoint(cfg["base"])
    if httpx is None:
        raise RuntimeError("服务器未安装 httpx，请重新安装项目依赖")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.post(endpoint,
            headers={"Authorization": "Bearer " + cfg["key"]},
            json={"model": cfg["model"], "messages": [{"role": "user", "content": content}], "max_tokens": max_tokens})
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise RuntimeError("AI API 未返回有效 JSON（HTTP " + str(response.status_code) + "）") from None
        if not isinstance(data, dict):
            raise RuntimeError("AI 返回格式不兼容，需要 JSON 对象")
        if response.status_code >= 400 or data.get("error"):
            error = data.get("error") or {}
            message = error.get("message", "") if isinstance(error, dict) else str(error)
            raise RuntimeError("AI API 错误（HTTP " + str(response.status_code) + "）：" + (message or "请求被拒绝"))
        try:
            reply = data["choices"][0]["message"]["content"]
            if isinstance(reply, list):
                reply = "".join(str(part.get("text", "")) for part in reply if isinstance(part, dict))
            reply = str(reply or "").strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("AI 返回格式不兼容，缺少 choices/message/content") from None
        if not reply:
            raise RuntimeError("AI 未返回正文，请检查模型是否支持此接口或是否耗尽输出额度")
        return reply


def _classify_local_image(image_bytes):
    global _ocr
    with _ocr_guard:
        if _ocr is None:
            import ddddocr
            _ocr = ddddocr.DdddOcr(show_ad=False)
        return _ocr.classification(image_bytes)


async def _recognize_image_result(image_bytes, length=0, cfg=None, ai_on=None, ocr_on=None):
    """自动任务与面板测试共用识别链，测试只覆盖配置快照，不修改已保存设置。"""
    started = time.perf_counter()
    if cfg is None:
        cfg = {"base": str(get_setting("ai_base", "") or "").strip(),
               "key": str(get_setting("ai_key", "") or "").strip(),
               "model": str(get_setting("ai_model", "") or "gpt-4o-mini").strip()}
    ai_on = get_setting("ai_enabled", "0") == "1" if ai_on is None else ai_on
    ocr_on = get_setting("ocr_enabled", "1") == "1" if ocr_on is None else ocr_on
    errors = []
    def result(text="", channel="none"):
        return {"ok": bool(text), "text": text, "channel": channel, "errors": errors,
                "elapsed_ms": int((time.perf_counter() - started) * 1000)}
    def clean(value):
        text = "".join(ch for ch in str(value or "").strip() if ch.isalnum())[:16]
        if not text:
            raise ValueError("识别结果为空")
        if length and len(text) != length:
            raise ValueError("识别字符数与期望长度不一致（期望 " + str(length) + " 位）")
        return text
    if ai_on:
        try:
            content = [{"type": "text", "text": "识别图片中的验证码文字，只输出字符本身，不要解释" + ("，长度应为" + str(length) if length else "")},
                       {"type": "image_url", "image_url": {"url": "data:" + _image_mime(image_bytes) + ";base64," + base64.b64encode(image_bytes).decode("ascii")}}]
            return result(clean(await _call_ai(cfg, content)), "ai")
        except Exception as error:
            message = _safe_ai_error(error, cfg.get("key", ""))
            errors.append({"channel": "ai", "message": message})
            log.warning("AI 识别失败: %s", message)
    if ocr_on:
        try:
            loop = asyncio.get_running_loop()
            value = await asyncio.wait_for(loop.run_in_executor(None, _classify_local_image, image_bytes), timeout=30)
            return result(clean(value), "ocr")
        except Exception as error:
            message = "本地 OCR 不可用或识别失败：" + _safe_ai_error(error, cfg.get("key", ""))
            errors.append({"channel": "ocr", "message": message})
            log.warning("%s", message)
    if not ai_on and not ocr_on:
        errors.append({"channel": "none", "message": "请至少启用 AI 识别或本地 OCR"})
    return result()


async def recognize_image_text(image_bytes, length=0):
    return (await _recognize_image_result(image_bytes, length))["text"]


# ---------------- 面板鉴权:密码哈希 / 会话令牌 / 限速 / 验证码 ----------------
_captcha = OrderedDict()  # ip -> (code, ts),仅位图答案存在服务器内存
_login_fail = OrderedDict()
SESSION_TTL = 30 * 86400
CAPTCHA_TTL = 180
AUTH_CACHE_MAX = 4096
CAPTCHA_CACHE_MAX = 2048
_auth_gc_at = 0.0
RL_WINDOW = 600      # 失败统计窗口:10 分钟
RL_MAX = 10          # 窗口内失败上限,超过封 10 分钟
RL_BLOCK = 600
CAPTCHA_AFTER = 3    # 连续失败 3 次后开始要求验证码


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$%s$%s" % (salt, dk.hex())


def verify_password(pw: str) -> bool:
    stored = get_setting("panel_pw_hash", "") or ""
    if not stored.startswith("scrypt$"):
        return False
    try:
        _, salt, want = stored.split("$", 2)
        dk = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), want)
    except Exception:
        return False


def init_panel_password():
    """把 env PANEL_PASSWORD / 旧的明文文件迁移成 scrypt 哈希,并删掉明文副本"""
    if get_setting("panel_pw_hash"):
        try:
            if PW_FILE.exists():
                PW_FILE.unlink()
        except Exception:
            pass
        return
    src = os.environ.get("PANEL_PASSWORD") or ""
    if not src and PW_FILE.exists():
        try:
            src = PW_FILE.read_text().strip()
        except Exception:
            src = ""
    generated = False
    if len(src) < 6:
        src = secrets.token_urlsafe(9)
        generated = True
    set_setting("panel_pw_hash", hash_password(src))
    try:
        if PW_FILE.exists():
            PW_FILE.unlink()
    except Exception:
        pass
    if generated:
        log.warning("已生成面板密码(只显示这一次,请立刻记录): %s", src)
    else:
        log.info("面板密码已改为哈希存储,明文文件已删除(改密码用面板或 PANEL_PASSWORD 环境变量)")


def create_session(ip: str, ua: str) -> str:
    tok = secrets.token_urlsafe(32)
    ts = int(time.time())
    purge_sessions(ts)
    conn.execute("INSERT INTO panel_sessions(token,created,ip,ua,last_seen) VALUES(?,?,?,?,?)",
                 (tok, ts, ip[:64], (ua or "")[:160], ts))
    conn.commit()
    return tok


def purge_sessions(now=None):
    now = int(time.time() if now is None else now)
    conn.execute("DELETE FROM panel_sessions WHERE created IS NULL OR created<=? OR created>?",
                 (now - SESSION_TTL, now))
    conn.commit()


def session_ok(tok: str) -> bool:
    now = int(time.time())
    purge_sessions(now)
    if not tok or len(tok) > 256:
        return False
    row = conn.execute("SELECT created FROM panel_sessions WHERE token=?", (tok,)).fetchone()
    if row is None or not now - SESSION_TTL < row["created"] <= now:
        return False
    conn.execute("UPDATE panel_sessions SET last_seen=? WHERE token=?", (now, tok))
    conn.commit()
    return True


def drop_session(tok: str):
    if tok:
        conn.execute("DELETE FROM panel_sessions WHERE token=?", (tok,))
        conn.commit()


def drop_all_sessions():
    conn.execute("DELETE FROM panel_sessions")
    conn.commit()


def load_trusted_proxies():
    nets = []
    for item in os.environ.get("TRUSTED_PROXIES", "").split(","):
        if item.strip():
            try:
                nets.append(ipaddress.ip_network(item.strip(), strict=False))
            except ValueError:
                raise ValueError("TRUSTED_PROXIES 必须是逗号分隔的 IP/CIDR") from None
    return tuple(nets)


TRUSTED_PROXIES = load_trusted_proxies()


def _trusted_ip(host):
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in TRUSTED_PROXIES)
    except ValueError:
        return False


def client_ip(request) -> str:
    # 必须配合 uvicorn --no-proxy-headers,这里只认原始 TCP 对端。
    peer = request.client.host if request.client else "?"
    xff = request.headers.get("x-forwarded-for", "")
    if not _trusted_ip(peer) or not xff or len(xff) > 2048:
        return peer
    chain = xff.split(",")
    if len(chain) > 20:
        return peer
    try:
        chain = [str(ipaddress.ip_address(ip.strip())) for ip in chain]
    except ValueError:
        return peer
    # 从右侧可信跳点回溯,不把攻击者伪造的最左地址用作限速键。
    for hop in reversed(chain):
        if not _trusted_ip(peer):
            break
        peer = hop
    return peer


def request_https(request) -> bool:
    if request.scope.get("scheme") == "https":
        return True
    peer = request.client.host if request.client else "?"
    return _trusted_ip(peer) and request.headers.get("x-forwarded-proto", "").strip().lower() == "https"


def set_panel_cookie(response, token, request):
    response.set_cookie("panel_token", token, max_age=SESSION_TTL, httponly=True,
                        samesite="lax", secure=request_https(request))


def _auth_cache_gc(now):
    global _auth_gc_at
    if now - _auth_gc_at < 30 and len(_login_fail) < AUTH_CACHE_MAX and len(_captcha) < CAPTCHA_CACHE_MAX:
        return
    _auth_gc_at = now
    for ip, (_, ts) in list(_captcha.items()):
        if now - ts >= CAPTCHA_TTL:
            _captcha.pop(ip, None)
    for ip, st in list(_login_fail.items()):
        if now - st["first"] >= RL_WINDOW and now >= st["blocked_until"]:
            _login_fail.pop(ip, None)


def rl_state(ip: str):
    now = time.time()
    _auth_cache_gc(now)
    st = _login_fail.get(ip)
    if st and now - st["first"] >= RL_WINDOW and now >= st["blocked_until"]:
        _login_fail.pop(ip, None)
        st = None
    if st is None:
        if len(_login_fail) >= AUTH_CACHE_MAX:
            # 容量满时拒绝新的来源,不逐出被封禁者以免换 IP 绕过封禁。
            raise HTTPException(429, "登录请求过多,请稍后重试")
        st = {"n": 0, "first": now, "blocked_until": 0, "captcha_at": 0}
        _login_fail[ip] = st
    return st


def rl_blocked(ip: str) -> int:
    st = rl_state(ip)
    return int(st["blocked_until"] - time.time()) if st["blocked_until"] > time.time() else 0


def rl_need_captcha(ip: str) -> bool:
    return rl_state(ip)["n"] >= CAPTCHA_AFTER


def rl_fail(ip: str) -> int:
    st = rl_state(ip)
    st["n"] += 1
    if st["n"] >= RL_MAX:
        st["blocked_until"] = time.time() + RL_BLOCK
    return st["n"]


def rl_ok(ip: str):
    _login_fail.pop(ip, None)


# 5x7 位图字形(不产生 SVG/text/font/PNG 文本元数据)。随机仿射+正弦扭曲后写 RGB PNG。
_CAPTCHA_FONT = {
    "2": "0e11010204081f", "3": "1e01010e01011e", "4": "02060a121f0202",
    "5": "1f10101e01011e", "6": "0e10101e11110e", "7": "1f010204080808",
    "8": "0e11110e11110e", "9": "0e11110f01010e", "A": "0e11111f111111",
    "B": "1e11111e11111e", "C": "0e11101010110e", "D": "1e11111111111e",
    "E": "1f10101e10101f", "F": "1f10101e101010", "G": "0e11101711110f",
    "H": "1111111f111111", "J": "0702020212120c", "K": "11121418141211",
    "L": "1010101010101f", "M": "111b1515111111", "N": "11191513111111",
    "P": "1e11111e101010", "R": "1e11111e141211", "S": "0f10100e01011e",
    "T": "1f040404040404", "U": "1111111111110e", "V": "11111111110a04",
    "W": "11111115151b11", "X": "11110a040a1111", "Y": "11110a04040404",
    "Z": "1f01020408101f",
}


def gen_captcha(ip: str) -> str:
    """返回完整 PNG data URL;答案绑定 IP,严格 3 分钟,任何验证均消耗一次。"""
    now = time.time()
    st = rl_state(ip)
    if now - st.get("captcha_at", 0) < 1:
        raise HTTPException(429, "验证码刷新过快")
    if ip not in _captcha and len(_captcha) >= CAPTCHA_CACHE_MAX:
        raise HTTPException(429, "验证码请求过多,请稍后重试")
    st["captcha_at"] = now
    rng = secrets.SystemRandom()
    code = "".join(secrets.choice(tuple(_CAPTCHA_FONT)) for _ in range(4))
    width, height = 160, 60
    pixels = bytearray([24, 29, 40] * width * height)

    def dot(x, y, color):
        if 0 <= x < width and 0 <= y < height:
            start = (y * width + x) * 3
            pixels[start:start + 3] = bytes(color)

    for i, char in enumerate(code):
        rows = bytes.fromhex(_CAPTCHA_FONT[char])
        tilt, phase = rng.uniform(-0.22, 0.22), rng.uniform(0, math.tau)
        y0 = rng.randint(12, 19)
        color = (rng.randint(140, 220), rng.randint(175, 245), rng.randint(185, 255))
        for y in range(28):
            for x in range(20):
                if rows[y // 4] & (1 << (4 - x // 4)):
                    xx = round(15 + i * 34 + x + tilt * (y - 14) + 2 * math.sin(y / 7 + phase))
                    yy = round(y0 + y + 1.8 * math.sin(x / 9 + phase))
                    dot(xx, yy, color)
    for _ in range(160):
        dot(rng.randrange(width), rng.randrange(height), (95, 105, 130))
    for _ in range(3):
        y0, phase = rng.randrange(height), rng.uniform(0, math.tau)
        for x in range(width):
            dot(x, round(y0 + 5 * math.sin(x / 17 + phase)), (75, 85, 110))

    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff)

    raw = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    _captcha[ip] = (code, now)
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def check_captcha(ip: str, ans: str) -> bool:
    code, ts = _captcha.pop(ip, ("", 0.0))
    if not code or not 0 <= time.time() - ts < CAPTCHA_TTL:
        return False
    return hmac.compare_digest(str(ans or "").strip().upper(), code)


init_panel_password()


# ---------------- 关键词匹配 ----------------
def match_rule(rule, text):
    if not text:
        return False
    kws = rule["keywords"]
    if rule["regex"]:
        try:
            return re.search(kws, text, re.I) is not None
        except re.error:
            return False
    low = text.lower()
    return any(k.strip().lower() in low for k in re.split(r"[,，]", kws) if k.strip())


# ---------------- Telethon 监听 ----------------
async def sender_name_of(event):
    """尽量取消息发送者显示名,失败返回空串"""
    try:
        sender = await event.get_sender()
        if sender is None:
            return ""
        nm = getattr(sender, "first_name", "") or ""
        ln = getattr(sender, "last_name", "") or ""
        return (nm + (" " + ln if ln else "")).strip() or getattr(sender, "username", "") or ""
    except Exception:
        return ""


async def on_group_message(event):
    if _running["paused"] or not event.is_group or event.out:
        return
    watched, rules = cache_snapshot()
    if not watched or str(event.chat_id) not in watched:
        return
    text = (event.raw_text or "").strip()
    if not text:
        return

    # 自动水群(从众模式,默认关):5 分钟内同群同文本出现 N 次 → 跟一条话术
    if get_setting("flock_enabled", "0") == "1" and not text.startswith("/"):
        try:
            count = max(2, min(int(get_setting("flock_count", "5")), 30))
            minutes = max(1, min(int(get_setting("flock_minutes", "5")), 60))
            key = (str(event.chat_id), text[:80])
            ts_list = flock_buf.setdefault(key, [])
            now = time.monotonic()
            ts_list[:] = [t for t in ts_list if now - t < minutes * 60]
            ts_list.append(now)
            if len(ts_list) >= count:
                flock_buf.pop(key, None)
                pool = [p.strip() for p in re.split(r"[,，]", str(get_setting("flock_pool", "") or "")) if p.strip()]
                if pool:
                    await asyncio.sleep(random.uniform(1.0, 3.0))
                    await event.reply(random.choice(pool))
                    log.info("水群跟风 群=%s 消息=%s", event.chat_id, text[:40])
        except Exception:
            pass

    # 一条消息可以命中多条规则:每条规则各自执行、各自冷却
    cooldown = int(get_setting("cooldown", "20"))
    now = time.monotonic()
    hit_rules = []
    for r in rules:
        if not match_rule(r, text):
            continue
        # 规则可以只对部分群生效(留空 = 全部监听群)
        rgroups = {x.strip() for x in re.split(r"[,，]", (r["groups"] or "")) if x.strip()}
        if rgroups and str(event.chat_id) not in rgroups:
            continue
        if now - last_hit_ts.get((event.chat_id, r["id"]), -1e9) < cooldown:
            continue
        hit_rules.append(r)
    if not hit_rules:
        return

    sender_name = ""
    try:
        sender = await event.get_sender()
        if get_setting("ignore_bots", "1") == "1" and getattr(sender, "bot", False):
            return
        nm = (getattr(sender, "first_name", "") or "") + ((" " + sender.last_name) if getattr(sender, "last_name", None) else "")
        sender_name = nm.strip() or getattr(sender, "username", "") or ""
    except Exception:
        pass

    grp_name = ""
    try:
        g = conn.execute("SELECT name FROM watched_groups WHERE chat_id=?", (str(event.chat_id),)).fetchone()
        if g:
            grp_name = g["name"]
    except Exception:
        pass

    try:
        dmin, dmax = [float(x) for x in str(get_setting("delay", "1.0 3.0")).split()]
    except Exception:
        dmin, dmax = 1.0, 3.0

    for idx, rule in enumerate(hit_rules):
        # 多条规则之间额外错开一点,避免一口气刷屏
        if idx:
            await asyncio.sleep(random.uniform(0.6, 1.8))
        last_hit_ts[(event.chat_id, rule["id"])] = time.monotonic()
        if not idx:
            await asyncio.sleep(random.uniform(min(dmin, dmax), max(dmin, dmax)))
        action = rule["action"]
        reply_desc = ""
        try:
            if action in ("reply", "both") and rule["value"]:
                reply = random.choice([x.strip() for x in rule["value"].split("||") if x.strip()])
                reply_desc = reply
                await event.reply(reply)
            if action in ("react", "both"):
                emo = (rule["reaction"] or rule["value"]).replace("\ufe0f", "")
                if emo:
                    reply_desc = (reply_desc + " + " + emo) if reply_desc else emo
                    await client(
                        SendReactionRequest(
                            peer=await event.get_input_chat(),
                            msg_id=event.id,
                            reaction=[ReactionEmoji(emoticon=emo)],
                        )
                    )
            hits_count["total"] += 1
            add_hit(action, True, chat_id=event.chat_id, rule_id=rule["id"], msg_id=event.id,
                    content=text, reply=reply_desc, grp_name=grp_name, sender=sender_name,
                    detail={"kind": "rule", "group": grp_name or str(event.chat_id), "sender": sender_name,
                            "message": text, "action": action, "reply": reply_desc,
                            "rule": rule["keywords"]})
            log.info("命中 #%s 群=%s 动作=%s", rule["id"], event.chat_id, action)
        except errors.FloodWaitError as e:
            log.warning("触发限流,等待 %ss", e.seconds)
            await asyncio.sleep(e.seconds + 1)
        except errors.RPCError as e:
            log.warning("操作失败(群=%s): %s", event.chat_id, e)
        except Exception:
            log.exception("未知错误")


async def watcher_loop():
    """后台监听:登录成功后启动;断线自动重建连接(而不是空转)"""
    global client, handler_registered
    backoff = 0
    while True:
        try:
            if client is None:
                await ensure_client()
            if client is not None:
                await client.run_until_disconnected()
        except Exception as e:
            log.warning("监听连接断开: %s", e)
        # 走到这里说明连接已经没了:清理 → 退避 → 重新建连并注册事件
        try:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            client = None
            handler_registered = False   # finish_login 会重新注册事件处理器
            backoff = min(backoff + 1, 12)
            await asyncio.sleep(min(60, 5 * backoff))
            await ensure_client()
            if client is not None and await client.is_user_authorized():
                backoff = 0
                log.info("监听连接已恢复")
                continue
        except Exception as e:
            log.warning("重连失败: %s", e)
            await asyncio.sleep(5)


# ---------------- 自动化:签到 / 抢注 / 抢红包 ----------------
auto_last_ts = {}        # (chat_id, auto_id) -> 上次执行时刻
signin_plans = {}        # (auto_id, YYYY-MM-DD) -> 今日计划执行分钟数(窗口内随机)
signin_task = None


def match_keywords(kws, text):
    """逗号分隔关键词,任意命中即 True(忽略大小写)"""
    if not text:
        return False
    low = text.lower()
    return any(k.strip().lower() in low for k in re.split(r"[,，]", kws or "") if k.strip())


def extract_code(text):
    """从消息中提取疑似邀请码/注册码(≥5 位字母数字下划线组合)"""
    m = re.search(r"[A-Za-z0-9_-]{5,}", text or "")
    return m.group(0) if m else ""


async def resolve_entity(peer):
    """解析 @username / 数字ID -> entity;失败返回 None"""
    if not peer or not str(peer).strip():
        return None
    try:
        value = str(peer).strip()
        return await client.get_entity(int(value) if re.fullmatch(r"[+-]?[0-9]+", value) else value)
    except Exception:
        return None


async def on_auto_event(event):
    """抢注 / 抢红包:监控群出现触发关键词时自动行动"""
    if _running["paused"] or not event.is_group or event.out:
        return
    autos = conn.execute(
        "SELECT * FROM automations WHERE enabled=1 AND type IN ('grabcodes','redpacket')"
    ).fetchall()
    if not autos:
        return
    chat = str(event.chat_id)
    text = (event.raw_text or "").strip()
    sender_name = await sender_name_of(event)

    watched = {r["chat_id"] for r in conn.execute("SELECT chat_id FROM watched_groups").fetchall()}
    now = time.monotonic()
    for a in autos:
        groups = [g.strip() for g in str(a["groups"] or "").split(",") if g.strip()]
        if groups:
            in_scope = False
            for g in groups:
                if g == chat or (g.startswith("@") and str(getattr(await resolve_entity(g), "id", "")) == chat):
                    in_scope = True
                    break
            if not in_scope:
                continue
        elif chat not in watched:
            continue

        # 红包消息可无文本(带按钮/贴纸);抢注必须有文本
        if a["type"] == "grabcodes" and not match_keywords(a["keywords"], text):
            continue
        if a["type"] == "redpacket":
            if text and not match_keywords(a["keywords"], text):
                continue

        key = (chat, a["id"])
        cd = int(a["cooldown"] or 30)
        if now - auto_last_ts.get(key, -1e9) < cd:
            continue
        auto_last_ts[key] = now

        try:
            grp_name = ""
            try:
                g = conn.execute("SELECT name FROM watched_groups WHERE chat_id=?", (str(chat),)).fetchone()
                if g:
                    grp_name = g["name"]
            except Exception:
                pass
            if a["type"] == "grabcodes":
                code = extract_code(text)
                rt = str(a["reply_text"] or "").replace("{code}", code)
                if not rt:
                    continue
                if a["action"] == "send":
                    await event.respond(rt)
                else:
                    await event.reply(rt)
                reply_desc = rt
                notify_event("success", a["name"], f"群内命中邀请码并注册: {rt[:120]}")
            else:
                reply_desc = ""
                buttons = await event.get_buttons()
                target_btn = None
                for row in buttons:
                    for b in row:
                        if re.search(r"打开|领取|收下|拆|红包|🎁|领", b.text or ""):
                            target_btn = b
                            reply_desc = "点击红包按钮"
                            break
                    if target_btn:
                        break
                if not target_btn and buttons and buttons[0]:
                    target_btn = buttons[0][0]
                    reply_desc = "点击第一个按钮"
                if target_btn:
                    await target_btn.click()
                    log.info("抢红包 群=%s 任务=%s", chat, a["name"])
            add_hit(a["type"], True, chat_id=chat, rule_id=-a["id"], msg_id=event.id,
                    content=text, reply=reply_desc, grp_name=grp_name, sender=sender_name,
                    detail={"kind": a["type"], "task": a["name"], "group": grp_name or str(chat),
                            "sender": sender_name, "message": text, "reply": reply_desc,
                            "action": "回复消息" if a["action"] != "send" else "群内发送"})
        except errors.FloodWaitError as e:
            log.warning("自动化触发限流,等待 %ss", e.seconds)
            await asyncio.sleep(e.seconds + 1)
        except errors.RPCError as e:
            log.warning("自动化操作失败(群=%s): %s", chat, e)
        except Exception:
            log.exception("自动化执行异常")


def _win_minutes(s):
    """hh:mm -> 当天分钟数;非法返回 None"""
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s).strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi


def render_placeholders(s):
    """命令占位符:{num:N} 随机N位数字 / {word:N} 随机N位小写字母 / {alpha:N} 随机N位大小写字母数字 / {uuid}"""
    def rep(m):
        n = max(1, int(m.group(m.lastindex) or 6))
        if m.group(1):   # word
            return "".join(random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(n))
        if m.group(2):   # num
            return "".join(random.choice("0123456789") for _ in range(n))
        return "".join(random.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(n))  # alpha
    s = re.sub(r"\{word:(\d+)\}|\{num:(\d+)\}|\{alpha:(\d+)\}", rep, s, flags=re.I)
    return s.replace("{uuid}", secrets.token_hex(8))


def _norm_emoji(x):
    return str(x or "").replace("\ufe0f", "").replace("\ufe0e", "").strip()


def _find_btn(btns, btn_text, btn_emoji=""):
    """找按钮:先看表情(文字会变、图标不变),再看文字;都没配就自动匹配"""
    emojis = [_norm_emoji(x) for x in re.split(r"[,，\s]", str(btn_emoji or "")) if _norm_emoji(x)]
    texts = [x.strip() for x in re.split(r"[,，]", str(btn_text or "")) if x.strip()]
    rows = [b for row in btns for b in row]
    if emojis:
        for b in rows:
            t = _norm_emoji(b.text or "")
            if any(e and e in t for e in emojis):
                return b
    if texts:
        for b in rows:
            t = b.text or ""
            if any(k.lower() in t.lower() for k in texts):
                return b
        return None
    if emojis:
        return None
    for b in rows:
        t = b.text or ""
        if re.search(r"签到|打卡|确认|领取|√|收下|完成|🧧|🎁|📅", t):
            return b
    return None


def _pick_cmd(command):
    """命令支持 | 分隔多条,随机选一条"""
    parts = [p.strip() for p in str(command or "").split("|") if p.strip()]
    return render_placeholders(random.choice(parts) if parts else str(command or "").strip())


async def _fetch_reply(ent, min_id=0):
    """只读取本轮发出消息之后的非自己消息,禁止历史回复误报成功。"""
    try:
        recent = await client.get_messages(ent, limit=10)
        for m in recent:
            if m.out or m.id <= min_id:
                continue
            return (m.raw_text or ""), m
    except Exception:
        pass
    return "", None


class _RetryLater(Exception):
    def __init__(self, seconds, message="Telegram 限流"):
        super().__init__(message)
        self.seconds = max(60, int(seconds) + 1)


def _task_failure(a, message, content=""):
    add_hit(a["type"], False, chat_id=a["target"], rule_id=-a["id"], grp_name=a["name"],
            content=content, detail={"kind": a["type"], "task": a["name"],
                                     "error": str(message)[:200]})
    notify_event("fail", a["name"], str(message)[:200])
    log.warning("任务失败 %s: %s", a["name"], message)
    return False


def _signin_failed(text, configured=""):
    defaults = "验证码错误,验证码不正确,验证码无效,验证码已过期,验证码过期,验证失败,签到失败,未签到成功,invalid captcha,wrong captcha,captcha expired,captcha error"
    return match_keywords(configured + "," + defaults, text)


def _signin_succeeded(text, configured=""):
    defaults = "签到成功,签到完成,今日已签到,已经签到,已签到,check-in successful,check in successful,already checked in"
    return match_keywords(configured or defaults, text)


async def _await_signin_result(ent, min_id, success_kw="", fail_kw="", baseline=None, require_success=False, timeout=12):
    """等待本轮新回复；明确错误优先，验证码提交不能仅因发送成功而成功。"""
    deadline = time.monotonic() + timeout
    last_text = ""
    polls = 0
    while time.monotonic() < deadline and polls < max(1, math.ceil(timeout) + 1):
        polls += 1
        try:
            fresh = [m for m in await client.get_messages(ent, limit=12) if _fresh_reply(m, min_id, baseline)]
            text = "\n".join(str(getattr(m, "raw_text", "") or "") for m in reversed(fresh)).strip()
            if text:
                last_text = text
                if _signin_failed(text, fail_kw):
                    return text, "failed"
                if _signin_succeeded(text, success_kw):
                    return text, "success"
                if not require_success and not success_kw:
                    return text, "received"
        except errors.FloodWaitError as error:
            raise _RetryLater(error.seconds) from error
        except Exception:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(1.0, remaining))
    return last_text, "unknown"


async def run_signin(a):
    """单次签到尝试;重试/成功时间由统一执行器管理,不在协程内无限重发。"""
    target = str(a["target"] or "").strip()
    cmd = _pick_cmd(a["command"])
    if not target or not cmd:
        return _task_failure(a, "签到缺少目标或命令")
    ent = await resolve_entity(target)
    if ent is None:
        return _task_failure(a, "签到目标解析失败", cmd)
    captcha_baseline = await _message_snapshot(ent) if a["signin_captcha"] else None
    try:
        sent_msg = await client.send_message(ent, cmd)
        min_id = sent_msg.id
        captcha_min_id = min_id
    except errors.FloodWaitError as e:
        raise _RetryLater(e.seconds) from e
    except Exception as e:
        return _task_failure(a, f"签到发送失败: {e}", cmd)

    # 两段延迟都使用异步等待，不阻塞面板和其它任务。
    await asyncio.sleep(float(a["command_delay"]))
    # 可选:点击本轮回复按钮。
    clicked_btn = False
    btn_text_clicked = ""
    if a["click_btn"]:
        try:
            button_message, btns = await _find_latest_buttons(ent, min_id=min_id)
            b = _find_btn(btns, a["btn_text"], a["btn_emoji"])
            if not b:
                return _task_failure(a, "本轮签到未找到匹配按钮", cmd)
            if b:
                times = max(1, min(int(a["click_times"] or 1), 20))
                gap = max(50, min(int(a["click_interval"] or 300), 3000)) / 1000.0
                for i in range(times):
                    if i:
                        await asyncio.sleep(gap)
                        if a["refresh_buttons"]:
                            # 刷新模式重新读取按钮；固定模式直接复用原按钮。
                            try:
                                latest_button_message, btns2 = await _find_latest_buttons(ent, min_id=min_id)
                                nb = _find_btn(btns2, a["btn_text"], a["btn_emoji"])
                                if nb:
                                    b = nb
                                    button_message = latest_button_message
                            except Exception:
                                pass
                    if a["signin_captcha"] and (i == 0 or a["refresh_buttons"]):
                        # 必须在点击前取快照，点击产生的新消息不能被误纳入旧边界。
                        captcha_baseline = await _message_snapshot(ent)
                        if button_message is not None:
                            captcha_baseline.setdefault(button_message.id, _message_signature(button_message))
                        captcha_min_id = max([min_id] + list(captcha_baseline))
                    await b.click()
                clicked_btn = True
                btn_text_clicked = getattr(b, "text", "") or ""
                log.info("签到点击按钮: %s ×%s", b.text, times)
                await asyncio.sleep(float(a["button_delay"]))
        except errors.FloodWaitError as e:
            raise _RetryLater(e.seconds) from e
        except Exception as e:
            return _task_failure(a, f"签到点按钮失败: {e}", cmd)

    # 可选:点签到后出现图片验证码,自动识别并回复
    cap_ans, cap_m = "", None
    outcome_baseline = None
    if a["signin_captcha"]:
        cap_m, img = await _await_signin_captcha(ent, 25, min_id=captcha_min_id, baseline=captcha_baseline)
        if not img:
            return _task_failure(a, "本轮未收到带验证提示的新图片；已忽略欢迎图和旧图，未提交识别结果", cmd)
        cap_ans = await recognize_image_text(img)
        if not cap_ans:
            return _task_failure(a, "验证码识别为空(检查 AI/本地 OCR 开关)", cmd)
        try:
            outcome_baseline = await _message_snapshot(ent)
            cap_sent = await client.send_message(ent, cap_ans)
            min_id = cap_sent.id
            log.info("签到验证码已回复")
        except errors.FloodWaitError as e:
            raise _RetryLater(e.seconds) from e
        except Exception as e:
            return _task_failure(a, f"验证码回复失败: {e}", cmd)

    # 未配置关键词也识别常见明确错误；验证码提交后必须有成功确认。
    ok, points, fail = True, "", False
    skw = str(a["success_kw"] or "").strip()
    fkw = str(a["fail_kw"] or "").strip()
    ppat = str(a["points_pat"] or "").strip()
    reply_txt, failure_reason = "", ""
    if skw or fkw or ppat or cap_ans:
        reply_txt, result_status = await _await_signin_result(ent, min_id, skw, fkw,
            baseline=outcome_baseline, require_success=bool(cap_ans or skw))
        fail = result_status == "failed"
        ok = result_status in ("success", "received")
        if fail:
            failure_reason = "机器人明确拒绝本次请求：" + reply_txt[:120]
        elif not ok:
            failure_reason = "未收到本轮签到成功确认" + ("，请配置正确的成功关键词" if not skw else "（" + skw + "）")
    else:
        reply_txt, _ = await _fetch_reply(ent, min_id=min_id)
        fail = _signin_failed(reply_txt, fkw)
        ok = not fail
        if fail:
            failure_reason = "机器人明确拒绝本次请求：" + reply_txt[:120]
    if ppat and reply_txt:
        try:
            m = re.search(ppat, reply_txt)
            if m:
                points = m.group(1) if m.groups() else m.group(0)
        except re.error:
            pass
    desc = f"发送: {cmd}"
    if a["click_btn"]:
        desc += " + 点按钮"
    if cap_ans:
        desc += f" + 验证码「{cap_ans}」"
    if points:
        desc += f" · 积分 {points}"
    if fail:
        desc += " (失败)"
        notify_event("fail", a["name"], failure_reason)
    elif ok is False:
        desc += " (未达成功条件)"
        notify_event("fail", a["name"], failure_reason)
    else:
        notify_event("success", a["name"], f"签到成功{('· 积分 '+points) if points else ''} · {target}")
    add_hit("signin", not (fail or ok is False), chat_id=target, rule_id=-a["id"], grp_name=a["name"],
            content=cmd, reply=reply_txt or desc, sender="",
            detail={"kind": "signin", "task": a["name"], "bot": target, "command": cmd,
                    "reply": reply_txt, "button": btn_text_clicked,
                    "clicks": (int(a["click_times"] or 1) if a["click_btn"] else 0), "captcha": cap_ans,
                    "captcha_message_id": cap_m.id if cap_m is not None else None,
                    "captcha_prompt": str(getattr(cap_m, "raw_text", "") or "")[:160],
                    "command_delay": a["command_delay"], "button_delay": a["button_delay"],
                    "refresh_buttons": a["refresh_buttons"], "error": failure_reason,
                    "points": points, "note": desc})
    log.info("签到%s 任务=%s 目标=%s", "失败" if fail or ok is False else "成功", a["name"], target)
    return not (fail or ok is False)


async def _select_btn_ai(btns, hint):
    """AI 从按钮列表中选择应点击的按钮,返回按钮对象;AI 未开启或无配置返回 None"""
    if not _ai_cfg:
        load_ai_cfg()
    if get_setting("ai_enabled", "0") != "1" or not (_ai_cfg.get("base") and _ai_cfg.get("key")):
        return None
    flat = [b.text for row in btns for b in row]
    if not flat:
        return None
    try:
        if httpx is None:
            raise RuntimeError("httpx 未安装")
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                (_ai_cfg["base"].rstrip("/")) + "/chat/completions",
                headers={"Authorization": f"Bearer {_ai_cfg['key']}"},
                json={
                    "model": _ai_cfg.get("model") or "gpt-4o-mini",
                    "messages": [{"role": "user", "content": (
                        f"以下是一个 Telegram 内联键盘的按钮: {flat}\n"
                        f"根据提示「{hint}」选择应点击的那个按钮,只输出按钮上的原文,不要任何解释"
                    )}],
                    "max_tokens": 30,
                },
            )
            d = r.json()
            ans = (d["choices"][0]["message"]["content"] or "").strip()
        ans = ans.strip().strip('"').strip("'")
        for row in btns:
            for b in row:
                if (b.text or "").strip() == ans or ans in (b.text or ""):
                    return b
    except Exception as e:
        log.warning("AI 选按钮失败: %s", e)
    return None


async def _find_latest_buttons(ent, limit=6, min_id=0):
    """只取本轮目标回复上带按钮的消息,不点击旧键盘。"""
    try:
        msgs = await client.get_messages(ent, limit=limit)
        for m in msgs:
            if m.out or m.id <= min_id:
                continue
            try:
                btns = await m.get_buttons()
            except Exception:
                btns = []
            if btns:
                return m, btns
    except Exception:
        pass
    return None, []


def _message_signature(message):
    photo = getattr(message, "photo", None)
    document = getattr(message, "document", None)
    return (str(getattr(message, "raw_text", "") or ""),
            getattr(photo, "id", bool(photo)), getattr(document, "id", bool(document)),
            str(getattr(message, "edit_date", "") or ""))


async def _message_snapshot(ent):
    try:
        return {m.id: _message_signature(m) for m in await client.get_messages(ent, limit=12) if not m.out}
    except Exception:
        return {}


def _fresh_reply(message, min_id, baseline=None):
    if message.out:
        return False
    if message.id > min_id:
        return True
    return bool(baseline and message.id in baseline and _message_signature(message) != baseline[message.id])


def _captcha_prompt(text):
    text = str(text or "").lower()
    if any(term in text for term in ("验证码错误", "验证码无效", "验证码过期", "欢迎使用", "选择您要使用的功能")):
        return False
    return (any(term in text for term in ("验证码", "校验码", "captcha", "verification code"))
            and any(term in text for term in ("输入", "填写", "回复", "发送", "enter", "input", "send", "type")))


async def _await_signin_captcha(ent, timeout=25, min_id=0, baseline=None):
    """只选本次动作后带明确验证提示的图片，欢迎图和错误卡片不进入识别。"""
    deadline = time.monotonic() + timeout
    checked = set()
    polls = 0
    while time.monotonic() < deadline and polls < max(1, math.ceil(timeout) + 1):
        polls += 1
        try:
            messages = list(await client.get_messages(ent, limit=12))
            for m in messages:
                signature = (m.id, _message_signature(m))
                if not _fresh_reply(m, min_id, baseline) or signature in checked:
                    continue
                has_img = bool(getattr(m, "photo", None))
                if not has_img and getattr(m, "document", None):
                    has_img = str(getattr(m.document, "mime_type", "")).startswith("image/")
                if not has_img:
                    continue
                if m.id <= min_id and baseline and m.id in baseline and _message_signature(m)[1:3] == baseline[m.id][1:3]:
                    continue  # 只编辑欢迎卡片文字不代表换了图片，必须媒体本身变化。
                prompt = _captcha_prompt(getattr(m, "raw_text", ""))
                # 支持图片和提示分开发送，但提示必须是同一发送者的相邻新消息。
                if not prompt and not str(getattr(m, "raw_text", "") or "").strip() and not getattr(m, "reply_markup", None):
                    prompt = any(hint.id - m.id == 1 and _fresh_reply(hint, min_id, baseline)
                                 and getattr(hint, "sender_id", None) == getattr(m, "sender_id", None)
                                 and _captcha_prompt(getattr(hint, "raw_text", "")) for hint in messages)
                if not prompt:
                    continue  # 后续编辑/新提示到来仍可重新检查，不能永久按 id 忽略。
                checked.add(signature)
                img = await client.download_media(m, file=bytes)
                if img:
                    return m, img
        except errors.FloodWaitError as error:
            raise _RetryLater(error.seconds) from error
        except Exception:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(1.0, remaining))
    return None, None


async def run_custom(a):
    """自定义多步任务:send/wait/click/captcha/until,链级重试"""
    target = str(a["target"] or "").strip()
    try:
        steps = json.loads(a["custom_steps"] or "[]")
    except Exception:
        steps = []
    if not target or not steps:
        return _task_failure(a, "自定义任务缺少目标或步骤")
    ent = await resolve_entity(target)
    if ent is None:
        return _task_failure(a, "自定义任务目标解析失败")
    # 无初始 send 的链也必须从启动时的水位开始,不能用历史成功/键盘/图片。
    recent = await client.get_messages(ent, limit=1)
    min_id = max((m.id for m in recent), default=0)
    chain = max(1, min(int(a["chain_retry"] or 1), 5))
    final_desc, final_ok = "", False
    for attempt in range(chain):
        log_desc = []
        ok = True
        try:
            for s in steps:
                t = s.get("t")
                if t == "send":
                    text = render_placeholders(str(s.get("v") or ""))
                    sent_msg = await client.send_message(ent, text)
                    min_id = sent_msg.id
                    log_desc.append(f"发送「{text}」")
                elif t == "wait":
                    ms = max(0, int(s.get("v") or 1000))
                    await asyncio.sleep(ms / 1000)
                    log_desc.append(f"等待{ms}ms")
                elif t == "click":
                    m, btns = await _find_latest_buttons(ent, min_id=min_id)
                    if not btns:
                        raise Exception("未找到带按钮的消息")
                    b = None
                    if s.get("ai"):
                        b = await _select_btn_ai(btns, str(s.get("v") or "下一步"))
                    if not b and s.get("any") and btns and btns[0]:
                        b = btns[0][0]
                    if not b:
                        b = _find_btn(btns, str(s.get("v") or ""))
                    if not b:
                        raise Exception("未找到匹配按钮")
                    await b.click()
                    log_desc.append(f"点击按钮「{b.text}」")
                elif t == "captcha":
                    length = max(1, min(int(s.get("v") or 4), 16))
                    img = None
                    try:
                        msgs = await client.get_messages(ent, limit=5)
                        for m in msgs:
                            if m.out or m.id <= min_id:
                                continue
                            if getattr(m, "photo", None) or getattr(m, "media", None):
                                img = await client.download_media(m, file=bytes)
                                if img:
                                    break
                    except Exception:
                        pass
                    if not img:
                        raise Exception("未找到验证码图片")
                    ans = await recognize_image_text(img, length)
                    if not ans:
                        raise Exception("验证码识别失败(未配置 AI 或本地 OCR 不可用)")
                    log_desc.append(f"识别验证码「{ans}」")
                    send_txt = str(s.get("send") or "")
                    if send_txt:
                        sent_msg = await client.send_message(ent, render_placeholders(send_txt).replace("{code}", ans))
                        log_desc.append(f"发送「{send_txt}」")
                    else:
                        sent_msg = await client.send_message(ent, ans)
                    min_id = sent_msg.id
                elif t == "until":
                    timeout = max(1, int(s.get("timeout") or 30000))
                    succ = str(s.get("success") or "")
                    fail = str(s.get("fail") or "")
                    deadline = time.monotonic() + timeout / 1000
                    matched, failed = False, False
                    while time.monotonic() < deadline:
                        rt, _ = await _fetch_reply(ent, min_id=min_id)
                        rl = rt.lower()
                        if fail and any(k.strip().lower() in rl for k in re.split(r"[,，]", fail) if k.strip()):
                            failed = True
                            break
                        if succ and any(k.strip().lower() in rl for k in re.split(r"[,，]", succ) if k.strip()):
                            matched = True
                            break
                        if not succ and not fail:
                            matched = True
                            break
                        await asyncio.sleep(2)
                    if failed:
                        raise Exception(f"回复含失败词「{fail}」")
                    if not matched:
                        raise Exception(f"等待回复超时({timeout}ms)")
                    log_desc.append(f"收到期望回复({succ or fail or '任意'})")
        except Exception as e:
            ok = False
            log_desc.append(f"失败: {e}")
        final_desc = " → ".join(log_desc)
        if ok:
            final_ok = True
            break
    hits_desc = final_desc[:200] or "执行完成"
    add_hit("custom", final_ok, chat_id=target, rule_id=-a["id"], grp_name=a["name"],
            content=f"自定义任务: {a['name']}", reply=final_desc,
            detail={"kind": "custom", "task": a["name"], "bot": target, "steps": final_desc,
                    "ok": final_ok})
    if final_ok:
        notify_event("success", a["name"], hits_desc)
    else:
        notify_event("fail", a["name"], hits_desc)
    log.info("自定义任务%s 任务=%s 目标=%s", "成功" if final_ok else "失败", a["name"], target)
    return final_ok


# 抢注轮询(开注探测):每间隔给机器人发探测命令,回复含开注关键词即注册
poll_last = {}
poll_task = None
housekeeping_task = None
health_task = None
_session_alerted = False
keepalive_task = None


async def run_keepalive(a, reason="定时"):
    """在指定群发一条内容,可选 N 秒后删除(Emby 保号用)"""
    if client is None or tg_state["phase"] != "ready":
        return _task_failure(a, "TG 尚未就绪")
    target = str(a["target"] or "").strip()
    parts = [x.strip() for x in str(a["reply_text"] or "").split("||") if x.strip()]
    if not target or not parts:
        return _task_failure(a, "保号缺少目标或内容")
    txt = render_placeholders(random.choice(parts))
    delay = max(0, min(int(a["cooldown"] or 0), 86400))
    try:
        ent = await resolve_entity(target)
        if ent is None:
            return _task_failure(a, "保号目标解析失败", txt)
        msg = await client.send_message(ent, txt)
    except errors.FloodWaitError as e:
        raise _RetryLater(e.seconds) from e
    except Exception as e:
        add_hit("keepalive", False, chat_id=target, rule_id=-a["id"], grp_name=target, content=txt,
                detail={"kind": "keepalive", "task": a["name"], "group": target, "message": txt,
                        "error": str(e)[:200], "reason": reason})
        notify_event("fail", a["name"], f"发言失败: {e}")
        return False
    add_hit("keepalive", True, chat_id=target, rule_id=-a["id"], grp_name=target, content=txt,
            detail={"kind": "keepalive", "task": a["name"], "group": target, "message": txt,
                    "delete_after": delay, "reason": reason})
    notify_event("success", a["name"],
                 f"已在 {target} 发言" + (f",{delay} 秒后删除" if delay else " (不删除)"))
    log.info("保号发言 群=%s 任务=%s 删除=%ss", target, a["name"], delay)
    if delay:
        async def _del(group_ent=ent, mid=msg.id, name=a["name"]):
            await asyncio.sleep(delay)
            try:
                await client.delete_messages(group_ent, [mid])
                log.info("保号消息已删除 群=%s 任务=%s", target, name)
            except Exception as e:
                log.warning("删除保号消息失败: %s", e)
        asyncio.get_running_loop().create_task(_del())
    return True


_auto_running = {}  # id -> Task;启动任务前同步占位,手动/定时共享同一锁
RETRY_BASE = 60


def _retry_limit(a):
    return 1 + max(0, min(int(a["retries"] or 0), 5))


def _forget_auto_schedule(aid):
    for key in list(signin_plans):
        if key[0] == aid:
            signin_plans.pop(key, None)
    poll_last.pop(aid, None)
    for key in list(auto_last_ts):
        if key[1] == aid:
            auto_last_ts.pop(key, None)


def _reset_auto_schedule(aid, clear_success=False):
    _forget_auto_schedule(aid)
    a = conn.execute("SELECT * FROM automations WHERE id=?", (aid,)).fetchone()
    now = int(time.time())
    last = "" if clear_success else (a["last_at"] or "")
    next_at = 0
    if a["type"] == "keepalive":
        try:
            base = int(last) if last else now
        except ValueError:
            base = now
            last = ""
        next_at = max(now, base + max(60, int(a["poll_interval"] or 60)))
    conn.execute("UPDATE automations SET last_at=?,last_attempt=0,retry_count=0,retry_day='',"
                 "next_run_at=?,last_status='' WHERE id=?", (last, next_at, aid))
    conn.commit()


def _signin_deadline(a, current):
    end = _win_minutes(a["win_end"])
    return current.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0).timestamp()


def _queue_auto(aid, manual=False):
    if aid in _auto_running:
        return False
    _auto_running[aid] = None  # 不等待协程开始就占锁
    try:
        _auto_running[aid] = asyncio.create_task(_execute_auto(aid, manual))
    except BaseException:
        _auto_running.pop(aid, None)
        raise
    return True


async def _execute_auto(aid, manual=False):
    """持久尝试/指数退避/成功状态;手动也走同一执行器,失败不污染 last_at。"""
    a = None
    try:
        a = conn.execute("SELECT * FROM automations WHERE id=?", (aid,)).fetchone()
        if a is None or (not manual and not a["enabled"]):
            return
        if client is None or tg_state["phase"] != "ready":
            return
        now = int(time.time())
        today = now_local().strftime("%Y-%m-%d")
        count = int(a["retry_count"] or 0)
        if (a["type"] == "signin" and a["retry_day"] != today) or (manual and count >= _retry_limit(a)):
            count = 0
        if a["type"] == "keepalive" and count >= _retry_limit(a):
            count = 0  # 上轮耗尽后只在完整间隔届满开启下一轮
        count += 1
        backoff = min(3600, RETRY_BASE * 2 ** (count - 1))
        conn.execute("UPDATE automations SET last_attempt=?,retry_count=?,retry_day=?,"
                     "last_status='running',next_run_at=? WHERE id=?",
                     (now, count, today, now + backoff, aid))
        conn.commit()
        a = conn.execute("SELECT * FROM automations WHERE id=?", (aid,)).fetchone()
        ok, status = False, "failed"
        try:
            if a["type"] == "signin":
                if manual:
                    ok = bool(await run_signin(a))
                else:
                    remaining = _signin_deadline(a, now_local()) - time.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError("签到窗口已结束")
                    ok = bool(await asyncio.wait_for(run_signin(a), timeout=remaining))
            elif a["type"] == "keepalive":
                ok = bool(await run_keepalive(a, "手动" if manual else "定时"))
            else:
                ok = bool(await run_custom(a))
        except _RetryLater as e:
            backoff = max(backoff, e.seconds)
            _task_failure(a, f"Telegram 限流,至少 {backoff} 秒后重试")
        except asyncio.CancelledError:
            status = "interrupted"
            _task_failure(a, "任务被中断,未记为成功")
            raise
        except asyncio.TimeoutError:
            _task_failure(a, "签到超过窗口结束,已取消本轮后续动作")
        except Exception as e:
            _task_failure(a, f"执行异常: {e}")
        finally:
            finished = int(time.time())
            next_at = finished + backoff
            if ok:
                stamp = now_local().strftime("%Y-%m-%d") if a["type"] == "signin" else str(finished)
                next_at = finished + max(60, int(a["poll_interval"] or 60)) if a["type"] == "keepalive" else 0
                conn.execute("UPDATE automations SET last_at=?,retry_count=0,next_run_at=?,"
                             "last_status='success' WHERE id=?", (stamp, next_at, aid))
            else:
                if a["type"] == "keepalive" and count >= _retry_limit(a):
                    next_at = finished + max(backoff, max(60, int(a["poll_interval"] or 60)))
                conn.execute("UPDATE automations SET next_run_at=?,last_status=? WHERE id=?",
                             (next_at, status, aid))
            conn.commit()
    finally:
        _auto_running.pop(aid, None)


def _keepalive_tick():
    if client is None or tg_state["phase"] != "ready":
        return
    now = int(time.time())
    for a in conn.execute("SELECT * FROM automations WHERE enabled=1 AND type='keepalive' AND target!=''").fetchall():
        if a["id"] in _auto_running:
            continue
        if not a["next_run_at"]:
            # 兼容旧任务;未成功的新任务不能用 last_at 伪造一次成功。
            _reset_auto_schedule(a["id"])
            continue
        if now >= a["next_run_at"]:
            _queue_auto(a["id"])


def _signin_tick():
    if client is None or tg_state["phase"] != "ready":
        return
    current = now_local()
    today = current.strftime("%Y-%m-%d")
    nowm = current.hour * 60 + current.minute
    now = int(time.time())
    for key in list(signin_plans):
        if key[1] != today:
            signin_plans.pop(key, None)
    for a in conn.execute("SELECT * FROM automations WHERE enabled=1 AND type='signin'").fetchall():
        if a["id"] in _auto_running or a["last_at"] == today:
            continue
        w0, w1 = _win_minutes(a["win_start"]), _win_minutes(a["win_end"])
        # 同日半开区间 [start,end),严格不允许在 win_end 之后开始/重试。
        if w0 is None or w1 is None or w0 >= w1 or not w0 <= nowm < w1:
            continue
        if a["retry_day"] == today:
            if a["retry_count"] >= _retry_limit(a) or now < a["next_run_at"]:
                continue
        elif a["retry_count"]:
            conn.execute("UPDATE automations SET retry_count=0,next_run_at=0 WHERE id=?", (a["id"],))
            conn.commit()
        key = (a["id"], today)
        if key not in signin_plans:
            signin_plans[key] = nowm if a["retry_day"] == today and a["retry_count"] else random.randint(w0, w1 - 1)
        if nowm >= signin_plans[key]:
            _queue_auto(a["id"])


async def housekeeping_loop():
    """每小时:清理过期日志"""
    while True:
        try:
            purge_hits()
            purge_sessions()
            _auth_cache_gc(time.time())
        except Exception as e:
            log.warning("日志清理异常: %s", e)
        await asyncio.sleep(3600)


async def keepalive_loop():
    """每分钟调度;短失败重试与完整成功间隔使用独立时间字段。"""
    while True:
        try:
            _keepalive_tick()
        except Exception as e:
            log.warning("保号循环异常: %s", e)
        await asyncio.sleep(60)


async def _health_check_once():
    """临时网络/RPC 故障不等同于撤销授权,error 状态仍持续复查。"""
    global _session_alerted
    checked_client = client
    if checked_client is None or tg_state["phase"] not in ("ready", "error"):
        return
    try:
        ok = await asyncio.wait_for(checked_client.is_user_authorized(), timeout=20)
    except Exception as e:
        unauthorized = tuple(cls for name in ("AuthKeyUnregisteredError", "SessionRevokedError",
                                               "UserDeactivatedError", "UserDeactivatedBanError",
                                               "AuthKeyDuplicatedError")
                             if isinstance((cls := getattr(errors, name, None)), type))
        if unauthorized and isinstance(e, unauthorized):
            ok = False
        else:
            if client is checked_client and tg_state["phase"] in ("ready", "error"):
                tg_state["health_error"] = f"临时连接异常: {type(e).__name__}"
            log.debug("心跳临时故障: %s", type(e).__name__)
            return
    # 心跳期间用户可能退出/重新登录;不能覆盖新客户端的状态。
    if client is not checked_client or tg_state["phase"] not in ("ready", "error"):
        return
    if not ok:
        tg_state["phase"] = "error"
        tg_state["err"] = "会话已失效,请重新登录"
        tg_state["health_error"] = None
        if not _session_alerted:
            _session_alerted = True
            send_notify("⚠️ TG 会话失效", "明确检测到登录已失效,请打开面板重新登录")
            log.warning("会话失效,已推送提醒")
    else:
        if tg_state["phase"] == "error" and not tg_state.get("me"):
            await finish_login()
        tg_state.update({"phase": "ready", "err": None, "health_error": None})
        if _session_alerted:
            log.info("会话已恢复正常")
        _session_alerted = False


async def health_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await _health_check_once()
        except Exception as e:
            log.debug("心跳检查异常: %s", e)


async def poll_loop():
    """轮询 grabcodes 任务:探测开注 → 自动注册"""
    global poll_task
    while True:
        try:
            if client is not None and tg_state["phase"] == "ready":
                autos = conn.execute(
                    "SELECT * FROM automations WHERE enabled=1 AND type='grabcodes' AND poll_interval>0 AND target!=''"
                ).fetchall()
                now = time.time()
                for a in autos:
                    interval = max(10, int(a["poll_interval"] or 60))
                    if now - poll_last.get(a["id"], 0) < interval:
                        continue
                    ent = await resolve_entity(a["target"])
                    if ent is None:
                        continue
                    probe = _pick_cmd(a["command"] or "/start")
                    try:
                        probe_msg = await client.send_message(ent, probe)
                        min_id = probe_msg.id
                        await asyncio.sleep(2)
                        rt, _ = await _fetch_reply(ent, min_id=min_id)
                        if match_keywords(a["keywords"] or "开注,开放注册,邀请码,发码,注册,开放", rt):
                            reg = render_placeholders(
                                str(a["reply_text"] or "").replace("{code}", extract_code(rt) or "REG")
                            )
                            if reg:
                                await client.send_message(ent, reg)
                                grp_name = a["target"]
                                add_hit("grabcodes", True, chat_id=a["target"], rule_id=-a["id"], grp_name=a["name"],
                                        content=f"轮询 {probe} 命中开注", reply=rt,
                                        detail={"kind": "grabcodes", "task": a["name"], "bot": a["target"],
                                                "probe": probe, "reply": rt, "registered": reg})
                                log.info("抢注轮询命中! %s -> %s", a["target"], reg)
                                notify_event("success", a["name"], f"检测到开注并注册: {reg}")
                                poll_last[a["id"]] = now + max(interval, 3600)  # 成功后冷却 1h
                                continue
                    except errors.FloodWaitError as e:
                        log.warning("轮询限流,等待 %ss", e.seconds)
                        await asyncio.sleep(e.seconds + 1)
                    except Exception:
                        log.exception("轮询探测异常")
                    poll_last[a["id"]] = now
        except Exception as e:
            log.warning("轮询循环异常: %s", e)
        await asyncio.sleep(10)


async def signin_loop():
    """同日窗口内随机执行;失败有限退避,成功才设置 last_at。"""
    while True:
        try:
            _signin_tick()
        except Exception as e:
            log.warning("签到循环异常: %s", e)
        await asyncio.sleep(60)


# ---------------- 生命周期 ----------------
@asynccontextmanager
async def lifespan(app):
    global client, health_task
    set_setting("uptime_since", get_setting("uptime_since", str(int(time.time()))))
    try:
        purge_hits()
    except Exception:
        pass
    # 若已有会话,启动时自动登录并监听
    try:
        api_id = int(get_setting("api_id", "0"))
        api_hash = get_setting("api_hash", "")
        if api_id and api_hash and (DATA / "tg.session").exists():
            client = TelegramClient(SESSION_PATH, api_id, api_hash)
            await client.connect()
            if await client.is_user_authorized():
                await finish_login()
            else:
                tg_state.update({"phase": "error", "err": "会话未授权,请重新登录"})
    except Exception as e:
        if client is not None:
            tg_state.update({"phase": "error", "err": "自动连接暂时失败,后台继续复查"})
        log.warning("自动登录失败(面板可重试): %s", e)
    if health_task is None or health_task.done():
        health_task = asyncio.create_task(health_loop())
    yield
    pending = [task for task in (watcher_task, signin_task, poll_task, keepalive_task,
                                housekeeping_task, health_task, *_auto_running.values())
               if task is not None and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass
    conn.close()


# ---------------- FastAPI ----------------
app = FastAPI(title="TG Persona", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def panel_auth(request: Request, call_next):
    """除登录接口与首页外,面板和头像都要已登录的会话令牌"""
    path = request.url.path
    if path in ("/", "/api/panel/login") or path.startswith("/static/") or path.startswith("/api/panel/captcha"):
        return await call_next(request)
    if path.startswith("/api/") or path == "/avatar":
        if session_ok(request.cookies.get("panel_token", "")):
            return await call_next(request)
        return JSONResponse({"error": "未登录面板"}, status_code=401)
    return await call_next(request)


# ---------- 面板登录 ----------
class PanelLogin(BaseModel):
    password: str
    captcha: str = ""


@app.get("/api/panel/captcha")
async def panel_captcha(request: Request):
    ip = client_ip(request)
    return {"image": gen_captcha(ip), "required": rl_need_captcha(ip)}


@app.post("/api/panel/login")
async def panel_login(body: PanelLogin, request: Request):
    ip = client_ip(request)
    left = rl_blocked(ip)
    if left:
        raise HTTPException(429, f"尝试次数过多,请 {left // 60 + 1} 分钟后再试")
    if rl_need_captcha(ip):
        if not check_captcha(ip, body.captcha):
            rl_fail(ip)
            raise HTTPException(400, "验证码错误或已过期,点图片刷新后重试")
    else:
        _captcha.pop(ip, None)
    if not verify_password(body.password):
        n = rl_fail(ip)
        msg = "密码错误" if n < CAPTCHA_AFTER else f"密码错误(连续 {n} 次,之后需要验证码)"
        if n >= RL_MAX:
            msg = "尝试次数过多,已暂时锁定 10 分钟"
        raise HTTPException(403, msg)
    rl_ok(ip)
    tok = create_session(ip, request.headers.get("user-agent", ""))
    resp = JSONResponse({"ok": True})
    set_panel_cookie(resp, tok, request)
    return resp


@app.post("/api/panel/logout")
async def panel_logout(request: Request):
    drop_session(request.cookies.get("panel_token", ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("panel_token")
    return resp


@app.post("/api/panel/logout-all")
async def panel_logout_all():
    """退出所有设备(改完密码后建议点一次)"""
    drop_all_sessions()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("panel_token")
    return resp


@app.get("/api/panel/sessions")
async def panel_sessions(request: Request):
    purge_sessions()
    cur = request.cookies.get("panel_token", "")
    rows = conn.execute("SELECT created, ip, ua, token FROM panel_sessions ORDER BY created DESC").fetchall()
    return {"count": len(rows), "now": sum(1 for r in rows if r["token"] == cur),
            "list": [{"created": r["created"], "ip": r["ip"], "ua": (r["ua"] or "")[:60]} for r in rows[:20]]}


# ---------- TG 登录 ----------
class TGInit(BaseModel):
    api_id: int
    api_hash: str


class SendCode(BaseModel):
    phone: str


class VerifyCode(BaseModel):
    code: str


class VerifyPassword(BaseModel):
    password: str


@app.post("/api/tg/init")
async def tg_init(body: TGInit):
    """保存 API 凭证并建立连接"""
    global client
    if body.api_id <= 0 or len(body.api_hash) < 10:
        raise HTTPException(400, "API_ID 或 API_HASH 无效(在 my.telegram.org 获取)")
    set_setting("api_id", str(body.api_id))
    set_setting("api_hash", body.api_hash)
    await ensure_client()
    return {"ok": True}


async def ensure_client():
    global client, tg_state
    if client is not None and tg_state["phase"] != "error":
        return
    try:
        if client is not None:
            await client.connect()
            if await client.is_user_authorized():
                await finish_login()
            return
        api_id = int(get_setting("api_id", "0"))
        api_hash = get_setting("api_hash", "")
        if not api_id or not api_hash:
            raise HTTPException(400, "请先填写 API_ID / API_HASH")
        client = TelegramClient(SESSION_PATH, api_id, api_hash)
        await client.connect()
        if await client.is_user_authorized():
            await finish_login()
    except HTTPException:
        raise
    except Exception as e:
        tg_state["phase"] = "error"
        tg_state["err"] = str(e)
        raise HTTPException(500, f"连接失败: {e}")


@app.post("/api/tg/send_code")
async def tg_send_code(body: SendCode):
    global tg_phone
    phone = body.phone.replace(" ", "").replace("-", "").replace("+", "")
    if not re.fullmatch(r"\d{8,15}", phone):
        raise HTTPException(400, "手机号格式不对,请带国家码,如 8613800138000")
    await ensure_client()
    try:
        await client.send_code_request(phone)
        tg_phone = phone
        tg_state.update({"phase": "code_sent", "phone": phone, "err": None})
        return {"ok": True}
    except errors.PhoneNumberInvalidError:
        raise HTTPException(400, "手机号无效,请带国家码")
    except errors.ApiIdInvalidError:
        raise HTTPException(400, "API_ID 或 API_HASH 无效")
    except errors.FloodWaitError as e:
        raise HTTPException(429, f"请求太频繁,请等 {e.seconds} 秒")
    except Exception as e:
        raise HTTPException(500, f"发送验证码失败: {e}")


@app.post("/api/tg/verify_code")
async def tg_verify_code(body: VerifyCode):
    try:
        result = await client.sign_in(tg_phone, body.code.strip())
        await finish_login(result)
        return {"ok": True}
    except errors.SessionPasswordNeededError:
        tg_state["phase"] = "need_password"
        return {"ok": True, "need_password": True}
    except errors.PhoneCodeInvalidError:
        raise HTTPException(400, "验证码错误")
    except errors.PhoneCodeExpiredError:
        raise HTTPException(400, "验证码已过期,请重新获取")
    except Exception as e:
        raise HTTPException(500, f"登录失败: {e}")


@app.post("/api/tg/verify_password")
async def tg_verify_password(body: VerifyPassword):
    try:
        result = await client.sign_in(password=body.password)
        await finish_login(result)
        return {"ok": True}
    except errors.PasswordHashInvalidError:
        raise HTTPException(400, "两步验证密码错误")
    except Exception as e:
        raise HTTPException(500, f"登录失败: {e}")


async def finish_login(me=None):
    global watcher_task, handler_registered, signin_task, poll_task, keepalive_task, housekeeping_task, health_task, _session_alerted
    me = me or await client.get_me()
    tg_state["phase"] = "ready"
    tg_state["err"] = None
    tg_state["health_error"] = None
    _session_alerted = False
    tg_state["me"] = {
        "id": me.id,
        "name": getattr(me, "first_name", "") + (" " + me.last_name if getattr(me, "last_name", None) else ""),
        "username": getattr(me, "username", "") or "",
        "phone": getattr(me, "phone", "") or "",
        "avatar": get_setting("avatar", ""),   # 自定义头像 URL,空则用 /avatar(TG 头像缓存)
    }
    try:
        os.chmod(SESSION_PATH + ".session", 0o600)
    except Exception:
        pass
    # 预热实体缓存,保证私有群 ID 可解析
    try:
        await client.get_dialogs(limit=200)
    except Exception:
        pass
    # 拉取 TG 头像缓存,面板展示用(失败则保留占位)
    try:
        await client.download_profile_photo(entity="me", file=str(AVATAR_FILE))
    except Exception:
        pass
    if not handler_registered:
        client.add_event_handler(on_group_message, events.NewMessage(incoming=True))
        client.add_event_handler(on_auto_event, events.NewMessage(incoming=True))
        handler_registered = True
    if watcher_task is None or watcher_task.done():
        watcher_task = asyncio.create_task(watcher_loop())
    if signin_task is None or signin_task.done():
        signin_task = asyncio.create_task(signin_loop())
    if poll_task is None or poll_task.done():
        poll_task = asyncio.create_task(poll_loop())
    global keepalive_task
    if keepalive_task is None or keepalive_task.done():
        keepalive_task = asyncio.create_task(keepalive_loop())
    if housekeeping_task is None or housekeeping_task.done():
        housekeeping_task = asyncio.create_task(housekeeping_loop())
    if health_task is None or health_task.done():
        health_task = asyncio.create_task(health_loop())
    log.info("已登录: %s (id=%s)", tg_state["me"]["name"], me.id)


@app.post("/api/tg/logout")
async def tg_logout():
    global client, watcher_task, handler_registered
    handler_registered = False
    if client is not None:
        try:
            await client.log_out()
        except Exception:
            pass
        try:
            await client.disconnect()
        except Exception:
            pass
        client = None
    tg_state.update({"phase": "idle", "me": None, "err": None})
    return {"ok": True}


# ---------- 群管理 ----------
@app.get("/api/watched")
async def list_watched():
    """已监听群(直接读 DB,毫秒级,不依赖全量拉取)"""
    rows = conn.execute("SELECT chat_id,name,added_at FROM watched_groups").fetchall()
    return {"groups": [{"chat_id": r["chat_id"], "name": r["name"], "added_at": r["added_at"]} for r in rows]}


_dialog_cache = {"ts": 0.0, "groups": []}
DIALOG_TTL = 60.0


async def _fetch_all_dialogs():
    out = []
    async for d in client.iter_dialogs(limit=500):
        if not d.is_group:
            continue
        out.append({
            "chat_id": str(d.id),
            "name": d.name or "(无名称)",
            "members": getattr(d.entity, "participants_count", None),
        })
    return out


@app.get("/api/dialogs")
async def list_dialogs(q: str = ""):
    if client is None or tg_state["phase"] != "ready":
        raise HTTPException(409, "TG 尚未登录")
    now = time.time()
    if not _dialog_cache["groups"] or now - _dialog_cache["ts"] > DIALOG_TTL:
        try:
            _dialog_cache["groups"] = await _fetch_all_dialogs()
            _dialog_cache["ts"] = time.time()
        except Exception as e:
            if not _dialog_cache["groups"]:
                raise HTTPException(500, f"拉取群列表失败: {e}")
    watched = {r["chat_id"] for r in conn.execute("SELECT chat_id FROM watched_groups").fetchall()}
    out = []
    for g in _dialog_cache["groups"]:
        g = dict(g)
        g["watched"] = g["chat_id"] in watched
        if q and q.lower() not in g["name"].lower():
            continue
        out.append(g)
    out.sort(key=lambda x: (not x["watched"], -(x["members"] or 0)))
    return {"groups": out}


@app.post("/api/groups")
async def add_group(body: dict):
    chat_id = str(body.get("chat_id", ""))
    name = str(body.get("name", "") or f"群 {chat_id}")
    if not chat_id:
        raise HTTPException(400, "缺少 chat_id")
    conn.execute(
        "INSERT INTO watched_groups(chat_id,name,added_at) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET name=?",
        (chat_id, name, int(time.time()), name),
    )
    conn.commit()
    invalidate_cache()
    return {"ok": True}


@app.delete("/api/groups/{chat_id}")
async def remove_group(chat_id: str):
    conn.execute("DELETE FROM watched_groups WHERE chat_id=?", (chat_id,))
    conn.commit()
    invalidate_cache()
    return {"ok": True}


# ---------- 规则管理 ----------
class RuleIn(BaseModel):
    keywords: str = Field(min_length=1)
    regex: int = 0
    action: str = "reply"       # reply | react | both
    value: str = ""
    reaction: str = ""          # 表情(both 时独立存,react 时用 value)
    groups: str = ""            # 只在这些群里生效(逗号分隔 chat_id,留空=全部监听群)
    enabled: int = 1


@app.get("/api/rules")
async def list_rules():
    rows = conn.execute("SELECT * FROM rules ORDER BY id DESC").fetchall()
    return {"rules": [dict(r) for r in rows]}


@app.post("/api/rules")
async def create_rule(body: RuleIn):
    if body.action not in ("reply", "react", "both"):
        raise HTTPException(400, "动作必须是 reply / react / both")
    if body.regex:
        try:
            re.compile(body.keywords)
        except re.error:
            raise HTTPException(400, "正则表达式无效")
    if body.action in ("reply", "both") and not body.value.strip():
        raise HTTPException(400, "回复内容不能为空")
    if body.action == "react" and not body.value.strip():
        raise HTTPException(400, "请选择表情")
    if body.action == "react" and (len(body.value.replace("\ufe0f", "")) > 8):
        raise HTTPException(400, "表情只能填一个 emoji")
    if body.action == "both" and not body.value.strip():
        raise HTTPException(400, "回复内容不能为空")
    cur = conn.execute(
        "INSERT INTO rules(keywords,regex,action,value,reaction,groups,enabled,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (body.keywords.strip(), body.regex, body.action, body.value.strip(), body.reaction.strip(),
         (body.groups or "").strip(), body.enabled, int(time.time())),
    )
    conn.commit()
    invalidate_cache()
    return {"ok": True, "id": cur.lastrowid}


@app.put("/api/rules/{rid}")
async def update_rule(rid: int, body: RuleIn):
    conn.execute(
        "UPDATE rules SET keywords=?,regex=?,action=?,value=?,reaction=?,groups=?,enabled=? WHERE id=?",
        (body.keywords.strip(), body.regex, body.action, body.value.strip(), body.reaction.strip(),
         (body.groups or "").strip(), body.enabled, rid),
    )
    conn.commit()
    invalidate_cache()
    return {"ok": True}


@app.post("/api/rules/{rid}/toggle")
async def toggle_rule(rid: int):
    """面板开关:翻转启用状态(不必回传整条规则)"""
    r = conn.execute("SELECT enabled FROM rules WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "规则不存在")
    en = 0 if r["enabled"] else 1
    conn.execute("UPDATE rules SET enabled=? WHERE id=?", (en, rid))
    conn.commit()
    invalidate_cache()
    return {"ok": True, "enabled": en}


# ---------- 自动化管理 (签到 / 抢注 / 抢红包 / 自定义) ----------
class AutoIn(BaseModel):
    type: str = "signin"          # signin | grabcodes | redpacket | custom
    name: str = ""
    target: str = ""              # signin/custom: @机器人 或 群ID;grabcodes: 轮询机器人(可选)
    command: str = ""             # signin: 命令(| 分隔多条随机);grabcodes: 轮询探测命令(默认 /start)
    win_start: str = "08:00"      # signin: 窗口开始
    win_end: str = "12:00"        # signin: 窗口结束
    click_btn: int = 0            # signin: 签到后自动点按钮
    btn_text: str = ""            # signin: 按钮文字(留空自动匹配)
    btn_emoji: str = ""           # signin: 按钮里的表情(文字会变、图标不变时用它)
    click_times: int = 1          # signin: 连点次数
    click_interval: int = 300     # signin: 连点间隔(毫秒)
    command_delay: float = Field(1.6, ge=0, le=120)  # 命令后等待秒数
    button_delay: float = Field(2.0, ge=0, le=120)   # 点击按钮后等待秒数
    refresh_buttons: int = Field(1, ge=0, le=1)       # 连点前刷新/固定按钮
    success_kw: str = ""          # signin: 成功关键词
    fail_kw: str = ""             # signin: 失败关键词(回复含它判失败)
    points_pat: str = ""          # signin: 积分提取正则
    is_chat: int = 0              # signin: 目标是群组
    retries: int = 2              # signin: 发送失败重试次数
    signin_captcha: int = 0       # signin: 点签到后出现图片验证码,自动识别并回复
    groups: str = ""              # grabcodes/redpacket: 指定群(留空=全部监听群)
    keywords: str = ""            # 触发关键词
    action: str = "reply"         # grabcodes: reply | send
    reply_text: str = ""          # 回复/命令文本,{code} 提取邀请码
    poll_interval: int = 0        # grabcodes: 轮询间隔(秒,0=关闭)
    custom_steps: str = ""        # custom: 步骤 JSON
    chain_retry: int = 1          # custom: 链级重试
    cooldown: int = 30
    enabled: int = 1


def _valid_auto(body: AutoIn):
    if body.type not in ("signin", "grabcodes", "redpacket", "custom", "keepalive"):
        raise HTTPException(400, "类型必须是 signin / grabcodes / redpacket / custom")
    if not body.name.strip():
        raise HTTPException(400, "请填写任务名称")
    if body.type == "signin":
        if not body.target.strip() or not body.command.strip():
            raise HTTPException(400, "签到需要填写目标(@机器人)和命令")
        if _win_minutes(body.win_start) is None or _win_minutes(body.win_end) is None:
            raise HTTPException(400, "时间窗口格式需为 hh:mm")
        if _win_minutes(body.win_start) >= _win_minutes(body.win_end):
            raise HTTPException(400, "签到窗口须同日开始早于结束,结束时刻不再执行")
        if body.retries < 0 or body.retries > 5:
            raise HTTPException(400, "重试次数需在 0-5 之间")
        if body.click_times < 1 or body.click_times > 20:
            raise HTTPException(400, "连点次数需在 1-20 之间")
        if body.click_interval < 50 or body.click_interval > 3000:
            raise HTTPException(400, "连点间隔需在 50-3000 毫秒之间")
        if not all(math.isfinite(value) and 0 <= value <= 120 for value in (body.command_delay, body.button_delay)):
            raise HTTPException(400, "命令后和按钮后延迟需为 0-120 秒")
        if body.refresh_buttons not in (0, 1):
            raise HTTPException(400, "按钮刷新模式只能为 0 或 1")
    if body.type == "keepalive":
        if not body.target.strip():
            raise HTTPException(400, "保号任务需要填写群(@用户名 或群ID)")
        if not body.reply_text.strip():
            raise HTTPException(400, "保号任务需要填写要发送的内容")
        if body.poll_interval < 60 or body.poll_interval > 90 * 86400:
            raise HTTPException(400, "发送间隔需在 60 秒 - 90 天之间")
        if body.cooldown < 0 or body.cooldown > 86400:
            raise HTTPException(400, "删除延迟需在 0-86400 秒之间")
    if body.type == "grabcodes" and not body.reply_text.strip():
        raise HTTPException(400, "抢注需要填写动作文本(支持 {code} 占位符)")
    if body.type == "grabcodes" and body.poll_interval:
        if body.poll_interval < 10 or body.poll_interval > 86400:
            raise HTTPException(400, "轮询间隔需在 10-86400 秒之间")
    if body.type == "custom":
        try:
            steps = json.loads(body.custom_steps or "[]")
            if not isinstance(steps, list) or not steps:
                raise ValueError
            for s in steps:
                if not isinstance(s, dict) or s.get("t") not in ("send", "wait", "click", "captcha", "until"):
                    raise ValueError
        except Exception:
            raise HTTPException(400, "步骤配置无效(格式: [{\"t\":\"send\",\"v\":\"/start\"},...])")
    if body.cooldown < 0 or body.cooldown > 86400:
        raise HTTPException(400, "冷却秒数无效")


_AUTO_COLS = ("type,name,target,command,win_start,win_end,click_btn,btn_text,btn_emoji,click_times,click_interval,"
              "success_kw,fail_kw,points_pat,is_chat,retries,signin_captcha,"
              "groups,keywords,action,reply_text,poll_interval,custom_steps,chain_retry,cooldown,enabled,command_delay,button_delay,refresh_buttons")


def _auto_vals(body: AutoIn, extra=None):
    return [
        body.type, body.name.strip(), body.target.strip(), body.command.strip(),
        body.win_start.strip(), body.win_end.strip(), body.click_btn,
        body.btn_text.strip(), body.btn_emoji.strip(), body.click_times, body.click_interval,
        body.success_kw.strip(), body.fail_kw.strip(),
        body.points_pat.strip(), body.is_chat, body.retries, body.signin_captcha,
        body.groups.strip(), body.keywords.strip(), body.action, body.reply_text.strip(),
        body.poll_interval, body.custom_steps.strip(), body.chain_retry,
        body.cooldown, body.enabled, body.command_delay, body.button_delay, body.refresh_buttons,
    ] + (extra or [])


@app.get("/api/autos")
async def list_autos():
    rows = conn.execute("SELECT * FROM automations ORDER BY id DESC").fetchall()
    return {"autos": [dict(r, signed_today=(r["type"] == "signin" and r["last_at"] == now_local().strftime("%Y-%m-%d")),
                            running=(r["id"] in _auto_running)) for r in rows]}


@app.post("/api/autos")
async def create_auto(body: AutoIn):
    _valid_auto(body)
    cur = conn.execute(
        f"INSERT INTO automations({_AUTO_COLS},created_at) VALUES({','.join('?' * (len(_AUTO_COLS.split(',')) + 1))})",
        _auto_vals(body, [int(time.time())]),
    )
    conn.commit()
    _reset_auto_schedule(cur.lastrowid)
    return {"ok": True, "id": cur.lastrowid}


@app.put("/api/autos/{aid}")
async def update_auto(aid: int, body: AutoIn):
    _valid_auto(body)
    if aid in _auto_running:
        raise HTTPException(409, "任务正在执行,请完成后再修改")
    previous = conn.execute("SELECT * FROM automations WHERE id=?", (aid,)).fetchone()
    if previous is None:
        raise HTTPException(404, "任务不存在")
    cols = _AUTO_COLS.split(",")
    conn.execute(
        f"UPDATE automations SET {','.join(c + '=?' for c in cols)} WHERE id=?",
        _auto_vals(body, [aid]),
    )
    conn.commit()
    changed = any(previous[key] != value for key, value in (
        ("type", body.type), ("target", body.target.strip()), ("command", body.command.strip()),
        ("reply_text", body.reply_text.strip()), ("custom_steps", body.custom_steps.strip())))
    _reset_auto_schedule(aid, clear_success=changed)
    return {"ok": True}


@app.post("/api/autos/{aid}/toggle")
async def toggle_auto(aid: int):
    if aid in _auto_running:
        raise HTTPException(409, "任务正在执行,请完成后再切换")
    r = conn.execute("SELECT enabled FROM automations WHERE id=?", (aid,)).fetchone()
    if not r:
        raise HTTPException(404, "任务不存在")
    v = 0 if r["enabled"] else 1
    conn.execute("UPDATE automations SET enabled=? WHERE id=?", (v, aid))
    conn.commit()
    _reset_auto_schedule(aid)
    return {"ok": True, "enabled": v}


@app.post("/api/autos/{aid}/duplicate")
async def duplicate_auto(aid: int):
    """一键复制任务"""
    r = conn.execute("SELECT * FROM automations WHERE id=?", (aid,)).fetchone()
    if not r:
        raise HTTPException(404, "任务不存在")
    d = dict(r)
    cur = conn.execute(
        f"INSERT INTO automations({_AUTO_COLS},created_at) VALUES({','.join('?' * (len(_AUTO_COLS.split(',')) + 1))})",
        [d[c] if c in d else "" for c in _AUTO_COLS.split(",")] + [int(time.time())],
    )
    conn.commit()
    _reset_auto_schedule(cur.lastrowid)
    return {"ok": True, "id": cur.lastrowid}


@app.delete("/api/autos/{aid}")
async def delete_auto(aid: int):
    if aid in _auto_running:
        raise HTTPException(409, "任务正在执行,请完成后再删除")
    _forget_auto_schedule(aid)
    conn.execute("DELETE FROM automations WHERE id=?", (aid,))
    conn.commit()
    return {"ok": True}


@app.post("/api/autos/{aid}/run")
async def run_auto_now(aid: int):
    """手动立即执行:signin/custom"""
    a = conn.execute("SELECT * FROM automations WHERE id=? AND type IN ('signin','custom','keepalive')", (aid,)).fetchone()
    if not a:
        raise HTTPException(404, "该类型任务不可手动执行或不存在")
    if client is None or tg_state["phase"] != "ready":
        raise HTTPException(409, "TG 尚未登录")
    if not _queue_auto(aid, manual=True):
        raise HTTPException(409, "任务正在执行,请勿重复提交")
    return {"ok": True, "running": True}


@app.delete("/api/rules/{rid}")
async def delete_rule(rid: int):
    conn.execute("DELETE FROM rules WHERE id=?", (rid,))
    conn.commit()
    invalidate_cache()
    return {"ok": True}


# ---------- 设置 ----------
SETTING_KEYS = ["tz", "log_keep_days", "cooldown", "delay", "ignore_bots", "api_id", "api_hash", "theme", "avatar",
                "notify_bark", "notify_bt", "notify_bc", "notify_on_success", "notify_on_fail",
                "ai_base", "ai_key", "ai_model", "ai_enabled", "ocr_enabled",
                "flock_enabled", "flock_count", "flock_minutes", "flock_pool"]
DEFAULTS = {"cooldown": "20", "delay": "1.0 3.0", "ignore_bots": "1", "theme": "dark",
            "flock_count": "5", "flock_minutes": "5", "ocr_enabled": "1", "log_keep_days": "30"}


@app.get("/api/settings")
async def get_settings():
    return {k: get_setting(k, DEFAULTS.get(k)) for k in SETTING_KEYS}


@app.post("/api/settings")
async def update_settings(body: dict):
    if "tz" in body:
        name = str(body.get("tz") or "").strip()
        if name and not tz_valid(name):
            raise HTTPException(400, f"时区无效:{name}(例: Asia/Shanghai)")
    for k, v in body.items():
        if k in SETTING_KEYS:
            set_setting(k, str(v))
    if any(k in body for k in ("ai_base", "ai_key", "ai_model")):
        load_ai_cfg()
    return {"ok": True}


class AITestSettings(BaseModel):
    ai_base: Optional[str] = Field(None, max_length=2048)
    ai_key: Optional[str] = Field(None, max_length=4096)
    ai_model: Optional[str] = Field(None, max_length=256)
    ai_enabled: Optional[bool] = None
    ocr_enabled: Optional[bool] = None


class CaptchaTestInput(AITestSettings):
    image: str = Field(..., min_length=1, max_length=MAX_TEST_IMAGE * 4 // 3 + 2048)
    length: int = Field(0, ge=0, le=16)


def _test_ai_settings(body):
    cfg = {}
    for name in ("base", "key", "model"):
        supplied = getattr(body, "ai_" + name)
        saved = str(get_setting("ai_" + name, "") or "").strip()
        cfg[name] = saved if supplied is None or (name == "key" and not supplied.strip()) else supplied.strip()
    ai_on = get_setting("ai_enabled", "0") == "1" if body.ai_enabled is None else body.ai_enabled
    ocr_on = get_setting("ocr_enabled", "1") == "1" if body.ocr_enabled is None else body.ocr_enabled
    return cfg, ai_on, ocr_on


@app.post("/api/ai/test")
async def test_ai_connection(body: AITestSettings):
    """测试当前填写配置，不保存；只发送固定测试提示，不触发 Telegram 任务。"""
    if _ai_test_lock.locked():
        raise HTTPException(409, "已有识别或 AI 测试正在执行，请稍后重试")
    cfg, _, _ = _test_ai_settings(body)
    started = time.perf_counter()
    async with _ai_test_lock:
        try:
            reply = await _call_ai(cfg, "这是连接测试。请只回复四个字：测试成功。不要解释，不要添加其它文字。")
            ok = reply.strip() == "测试成功"
            return {"ok": ok, "reply": reply, "error": "" if ok else "AI 已返回内容，但没有按要求回复“测试成功”",
                    "elapsed_ms": int((time.perf_counter() - started) * 1000)}
        except Exception as error:
            return {"ok": False, "reply": "", "error": _safe_ai_error(error, cfg.get("key", "")),
                    "elapsed_ms": int((time.perf_counter() - started) * 1000)}


@app.post("/api/captcha/test")
async def test_captcha_recognition(body: CaptchaTestInput):
    if _ai_test_lock.locked():
        raise HTTPException(409, "已有识别或 AI 测试正在执行，请稍后重试")
    image = _decode_test_image(body.image)
    cfg, ai_on, ocr_on = _test_ai_settings(body)
    async with _ai_test_lock:
        return await _recognize_image_result(image, body.length, cfg, ai_on, ocr_on)


@app.post("/api/panel/password")
async def change_panel_password(body: dict, request: Request):
    pw = str(body.get("password", "")).strip()
    old = str(body.get("old", "") or "")
    if len(pw) < 8:
        raise HTTPException(400, "面板密码至少 8 位")
    ip = client_ip(request)
    if rl_blocked(ip):
        raise HTTPException(429, "尝试次数过多,请稍后重试")
    if not old or not verify_password(old):
        rl_fail(ip)
        raise HTTPException(403, "必须填写有效原密码")
    rl_ok(ip)
    set_setting("panel_pw_hash", hash_password(pw))
    drop_all_sessions()
    # 当前设备自动续一个会话,其它设备会被踢下线
    tok = create_session(client_ip(request), request.headers.get("user-agent", ""))
    resp = JSONResponse({"ok": True})
    set_panel_cookie(resp, tok, request)
    return resp


@app.post("/api/toggle")
async def toggle_paused(body: dict):
    _running["paused"] = bool(body.get("paused", False))
    return {"ok": True, "paused": _running["paused"]}


# ---------- 状态 & 日志 ----------
@app.get("/api/time")
async def get_time():
    """面板显示服务器时间 / 时区,用来核对签到窗口"""
    return {"tz": tz_name(), "now": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": int(time.time()), "tzlist": [
                "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Taipei", "Asia/Tokyo", "Asia/Seoul",
                "Asia/Singapore", "Asia/Bangkok", "Asia/Kolkata", "UTC", "Europe/London",
                "Europe/Berlin", "Europe/Moscow", "America/New_York", "America/Los_Angeles",
            ]}


@app.get("/api/state")
async def get_state():
    rules = conn.execute("SELECT COUNT(*) c FROM rules WHERE enabled=1").fetchone()["c"]
    groups = conn.execute("SELECT COUNT(*) c FROM watched_groups").fetchone()["c"]
    return {
        "tg": tg_state,
        "logged_in": tg_state["phase"] == "ready",
        "running": not _running["paused"],
        "paused": _running["paused"],
        "stats": {
            "watched_groups": groups,
            "rules": rules,
            "hits": hits_count["total"],
            "uptime_since": get_setting("uptime_since", str(int(time.time()))),
        },
        "panel_need_change": False,
        "sessions": conn.execute("SELECT COUNT(*) c FROM panel_sessions").fetchone()["c"],
        "tz": tz_name(),
        "now": now_local().strftime("%Y-%m-%d %H:%M"),
    }


@app.post("/api/logs/clear")
async def clear_logs(body: dict = None):
    """手动清理日志:days 给了按天数删,没有则全清"""
    days = (body or {}).get("days")
    if days is None:
        cur = conn.execute("DELETE FROM hits")
        conn.commit()
        n = cur.rowcount or 0
        try:
            conn.execute("VACUUM")
            conn.commit()
        except sqlite3.Error:
            pass
        return {"ok": True, "deleted": n, "mode": "all"}
    n = purge_hits(int(days))
    return {"ok": True, "deleted": n, "mode": "days", "days": int(days)}


@app.get("/api/logs")
async def get_logs(n: int = 60, kind: str = "", q: str = ""):
    """kind: reply(关键词回复) / signin / grabcodes / redpacket / custom;留空=全部"""
    kind = (kind or "").strip()
    if kind == "reply":
        where, params = "action IN ('reply','react','both')", []
    elif kind in ("signin", "grabcodes", "redpacket", "custom", "keepalive"):
        where, params = "action=?", [kind]
    else:
        where, params = "1=1", []
    if q.strip():
        # 搜索:群名 / 发送人 / 消息 / 回复 / 详情
        like = "%" + q.strip()[:60] + "%"
        where += " AND (grp_name LIKE ? OR sender LIKE ? OR content LIKE ? OR reply LIKE ? OR detail LIKE ?)"
        params = list(params) + [like] * 5
    rows = conn.execute(
        "SELECT ts,chat_id,rule_id,msg_id,action,content,reply,grp_name,ok,sender,detail FROM hits "
        "WHERE " + where + " ORDER BY id DESC LIMIT ?",
        (*params, min(n, 200)),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d.get("detail") or "{}")
        except Exception:
            d["detail"] = {}
        out.append(d)
    if q.strip():
        return {"hits": out, "counts": {}}
    counts = {"reply": 0, "signin": 0, "grabcodes": 0, "redpacket": 0, "custom": 0, "keepalive": 0}
    for r in conn.execute("SELECT action, COUNT(*) c FROM hits GROUP BY action").fetchall():
        k = "reply" if r["action"] in ("reply", "react", "both") else r["action"]
        if k in counts:
            counts[k] += r["c"]
    return {"hits": out, "counts": counts}


# ---------- 前端 ----------
@app.get("/")
async def index():
    return FileResponse(BASE / "web" / "index.html")


# ---------------- 在线更新 ----------------
@app.get("/api/version")
async def get_version(check: int = 0):
    """check=1 时实时向 GitHub 查询最新版本;否则返回缓存"""
    if check:
        try:
            updater.fetch_latest()
        except Exception as e:
            raise HTTPException(502, f"检查更新失败:{e}")
    return updater.snapshot()


class BackupCleanupInput(BaseModel):
    mode: str = "keep_latest"
    keep: int = Field(1, ge=0, le=20, strict=True)


@app.get("/api/backups")
async def get_backup_files():
    try:
        return await asyncio.get_running_loop().run_in_executor(None, updater.list_backups)
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from None


@app.post("/api/backups/cleanup")
async def cleanup_backup_files(body: BackupCleanupInput):
    """只清理识别到的历史备份；活动更新/待恢复事务由更新锁二次保护。"""
    try:
        return await asyncio.get_running_loop().run_in_executor(None,
            lambda: updater.cleanup_backups(body.mode, body.keep))
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from None


@app.post("/api/update")
async def run_update(body: dict = None):
    """后台执行更新;前端轮询 /api/version 看进度"""
    force = bool((body or {}).get("force", False))
    return updater.start_update(force=force)


@app.get("/avatar")
async def avatar():
    """返回缓存的 TG 头像;不存在或失败时前端自动回退首字母占位"""
    if not AVATAR_FILE.exists():
        raise HTTPException(404, "no avatar")
    return FileResponse(AVATAR_FILE, media_type="image/jpeg")


# ---------------- 启动 ----------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", proxy_headers=False)
