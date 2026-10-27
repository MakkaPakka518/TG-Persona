#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TG Persona —— Telegram 人形分身 (Userbot Watcher)

部署在小 VPS 上,登录你自己的 Telegram 账号:
- 可视化面板 (http://IP:端口) 完成登录、选群、配关键词
- 监听的群里有人发你设定的关键词时,以你的身份回复文本 / 点表情

启动:  uvicorn main:app --host 0.0.0.0 --port 8090
或:    python main.py            (内置 uvicorn,读取 .env 的 PORT)
"""
import asyncio
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
    for col, ddl in (("content", "TEXT DEFAULT ''"), ("reply", "TEXT DEFAULT ''"), ("grp_name", "TEXT DEFAULT ''")):
        try:
            conn.execute(f"ALTER TABLE hits ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    conn.commit()


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


async def recognize_image_text(image_bytes, length=0):
    """验证码识别:ai_enabled / ocr_enabled 双开关;都开启时 AI 优先,失败自动兜底本地 OCR"""
    if not _ai_cfg:
        load_ai_cfg()
    ai_on = get_setting("ai_enabled", "0") == "1"
    ocr_on = get_setting("ocr_enabled", "1") == "1"
    if ai_on and _ai_cfg.get("base") and _ai_cfg.get("key"):
        try:
            import base64 as _b64
            if httpx is None:
                raise RuntimeError("httpx 未安装")
            b64 = _b64.b64encode(image_bytes).decode()
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    (_ai_cfg["base"].rstrip("/")) + "/chat/completions",
                    headers={"Authorization": f"Bearer {_ai_cfg['key']}"},
                    json={
                        "model": _ai_cfg.get("model") or "gpt-4o-mini",
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": f"识别图片中的验证码文字,只输出字符本身,不要任何解释{'，长度应为'+str(length) if length else ''}"},
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                            ],
                        }],
                        "max_tokens": 20,
                    },
                )
                d = r.json()
                txt = (d["choices"][0]["message"]["content"] or "").strip()
                txt = "".join(ch for ch in txt if ch.isalnum())[:16]
            if txt:
                return txt
            log.warning("AI 识别结果为空")
        except Exception as e:
            log.warning("AI 识别失败: %s", e)
        if not ocr_on:
            return ""   # AI 开但失败,且本地关 → 不再兜底
    if ocr_on:
        # 本地 ddddocr
        try:
            global _ocr
            if _ocr is None:
                import ddddocr
                _ocr = ddddocr.DdddOcr(show_ad=False)
            res = _ocr.classification(image_bytes)
            return "".join(ch for ch in (res or "") if ch.isalnum())[:16]
        except Exception as e:
            log.warning("本地 OCR 不可用(可开启 AI 识别): %s", e)
            return ""
    return ""   # 两个开关都关


# ---------------- 面板鉴权:密码哈希 / 会话令牌 / 限速 / 验证码 ----------------
_captcha = {}        # ip -> (code, ts)
_login_fail = {}     # ip -> {"n": 连续失败, "first": 窗口起点, "blocked_until": 解封时间}
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
    conn.execute("DELETE FROM panel_sessions WHERE created < ?", (ts - 30 * 86400,))
    conn.execute("INSERT INTO panel_sessions(token,created,ip,ua,last_seen) VALUES(?,?,?,?,?)",
                 (tok, ts, ip[:64], (ua or "")[:160], ts))
    conn.commit()
    return tok


def session_ok(tok: str) -> bool:
    if not tok:
        return False
    return conn.execute("SELECT 1 FROM panel_sessions WHERE token=?", (tok,)).fetchone() is not None


def drop_session(tok: str):
    if tok:
        conn.execute("DELETE FROM panel_sessions WHERE token=?", (tok,))
        conn.commit()


def drop_all_sessions():
    conn.execute("DELETE FROM panel_sessions")
    conn.commit()


def client_ip(request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def rl_state(ip: str):
    now = time.time()
    st = _login_fail.get(ip)
    if not st or now - st["first"] > RL_WINDOW:
        st = {"n": 0, "first": now, "blocked_until": 0}
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


def gen_captcha(ip: str) -> str:
    """图形验证码:答案绑定 IP,3 分钟内有效,用过即失效"""
    import random as _r
    code = "".join(_r.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=4))
    _captcha[ip] = (code, time.time())
    chars = "".join(
        f'<text x="{28 + i * 26}" y="{42}" font-size="30" font-family="Arial" font-weight="bold" '
        f'fill="#{_r.choice(["4af", "a4d", "5ad", "6cf", "8e8"])}" '
        f'transform="rotate({_r.randint(-18, 18)} {28 + i * 26} 42)">{c}</text>'
        for i, c in enumerate(code)
    )
    lines = "".join(
        f'<line x1="{_r.randint(0, 120)}" y1="{_r.randint(0, 50)}" '
        f'x2="{_r.randint(0, 120)}" y2="{_r.randint(0, 50)}" stroke="#666" stroke-width="1"/>'
        for _ in range(6)
    )
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="140" height="50" viewBox="0 0 140 50">'
            f'<rect width="140" height="50" fill="#1b1e28" rx="8"/>{lines}{chars}</svg>')


def check_captcha(ip: str, ans: str) -> bool:
    code, ts = _captcha.pop(ip, ("", 0.0))
    if not code or time.time() - ts > 300:
        return False
    return ans.strip().upper() == code


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

    rule = next((r for r in rules if match_rule(r, text)), None)
    if rule is None:
        return
    # 规则可以只对部分群生效(留空 = 全部监听群)
    rgroups = {x.strip() for x in re.split(r"[,，]", (rule["groups"] or "")) if x.strip()}
    if rgroups and str(event.chat_id) not in rgroups:
        return

    cooldown = int(get_setting("cooldown", "20"))
    now = time.monotonic()
    key = (event.chat_id, rule["id"])
    if now - last_hit_ts.get(key, -1e9) < cooldown:
        return

    if get_setting("ignore_bots", "1") == "1":
        try:
            sender = await event.get_sender()
            if getattr(sender, "bot", False):
                return
        except Exception:
            pass

    last_hit_ts[key] = now

    try:
        dmin, dmax = [float(x) for x in str(get_setting("delay", "1.0 3.0")).split()]
    except Exception:
        dmin, dmax = 1.0, 3.0
    await asyncio.sleep(random.uniform(min(dmin, dmax), max(dmin, dmax)))

    action = rule["action"]
    reply_desc = ""
    try:
        if action in ("reply", "both") and rule["value"]:
            reply = random.choice([s.strip() for s in rule["value"].split("||") if s.strip()])
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
        grp_name = ""
        try:
            g = conn.execute("SELECT name FROM watched_groups WHERE chat_id=?", (str(event.chat_id),)).fetchone()
            if g:
                grp_name = g["name"]
        except Exception:
            pass
        hits_count["total"] += 1
        conn.execute(
            "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action,content,reply,grp_name) VALUES(?,?,?,?,?,?,?,?)",
            (int(time.time()), str(event.chat_id), rule["id"], event.id, action, text[:200], reply_desc[:200], grp_name),
        )
        conn.commit()
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
        return await client.get_entity(str(peer).strip())
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
            conn.execute(
                "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action,content,reply,grp_name) VALUES(?,?,?,?,?,?,?,?)",
                (int(time.time()), chat, -a["id"], event.id, a["type"], text[:200], reply_desc[:200], grp_name),
            )
            conn.commit()
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


def _find_btn(btns, btn_text):
    """按文字找按钮;btn_text 留空则自动匹配签到/打卡/确认等"""
    texts = [x.strip() for x in re.split(r"[,，]", str(btn_text or "")) if x.strip()]
    for row in btns:
        for b in row:
            t = b.text or ""
            if texts:
                if any(k.lower() in t.lower() for k in texts):
                    return b
            elif re.search(r"签到|打卡|确认|领取|√|收下|完成", t):
                return b
    return None


def _pick_cmd(command):
    """命令支持 | 分隔多条,随机选一条"""
    parts = [p.strip() for p in str(command or "").split("|") if p.strip()]
    return render_placeholders(random.choice(parts) if parts else str(command or "").strip())


async def _fetch_reply(ent):
    """取与目标最近的非自己消息文本(判定成功/失败/积分用)"""
    try:
        recent = await client.get_messages(ent, limit=5)
        for m in recent:
            if m.out:
                continue
            return (m.raw_text or ""), m
    except Exception:
        pass
    return "", None


async def run_signin(a):
    """执行单条签到:发命令(多条随机|占位符),可选点按钮;成功/失败关键词判定,积分提取,失败重试"""
    target = str(a["target"] or "").strip()
    cmd = _pick_cmd(a["command"])
    if not target or not cmd:
        return
    ent = await resolve_entity(target)
    if ent is None:
        log.warning("签到目标解析失败: %s", target)
        notify_event("fail", a["name"], f"签到目标解析失败: {target}")
        return
    retries = max(1, min(int(a["retries"] or 2), 5))
    sent = False
    for attempt in range(retries):
        try:
            await client.send_message(ent, cmd)
            sent = True
            break
        except errors.FloodWaitError as e:
            log.warning("签到限流,等待 %ss", e.seconds)
            await asyncio.sleep(e.seconds + 1)
        except errors.RPCError as e:
            log.warning("签到发送失败(尝试 %s/%s): %s", attempt + 1, retries, e)
            await asyncio.sleep(2)
        except Exception:
            log.exception("签到发送异常")
            await asyncio.sleep(2)
    if not sent:
        log.warning("签到最终失败(重试 %s 次): %s", retries, target)
        notify_event("fail", a["name"], f"发送失败(重试{retries}次): {target}")
        return

    # 可选:点击回复按钮(bot 回复消息上的键盘,不区分是否自己发出)
    clicked_btn = False
    if a["click_btn"]:
        try:
            await asyncio.sleep(1.6)
            _, btns = await _find_latest_buttons(ent)
            b = _find_btn(btns, a["btn_text"])
            if b:
                await b.click()
                clicked_btn = True
                log.info("签到点击按钮: %s", b.text)
        except errors.FloodWaitError as e:
            log.warning("点按钮限流,等待 %ss", e.seconds)
            await asyncio.sleep(e.seconds + 1)
        except Exception:
            log.exception("签到点按钮异常")

    # 可选:点签到后出现图片验证码,自动识别并回复
    cap_ans = ""
    if a["signin_captcha"]:
        # 刚点完按钮,给 bot 一点时间发验证码;若没点按钮则直接等
        cap_m, img = await _await_signin_captcha(ent, 25)
        if img:
            cap_ans = await recognize_image_text(img)
            if cap_ans:
                try:
                    await client.send_message(ent, cap_ans)
                    log.info("签到验证码已回复: %s", cap_ans)
                except Exception:
                    log.exception("验证码回复异常")
            else:
                log.warning("签到验证码识别为空")
                notify_event("fail", a["name"], "验证码识别为空(检查 AI/本地 OCR 开关)")
        else:
            log.info("签到未等到验证码图片")

    # 成功/失败关键词判定 + 积分提取
    ok, points, fail = True, "", False
    skw = str(a["success_kw"] or "").strip()
    fkw = str(a["fail_kw"] or "").strip()
    ppat = str(a["points_pat"] or "").strip()
    reply_txt = ""
    if skw or fkw or ppat:
        # 刚回复验证码后多等一点,让 bot 出最终结果
        await asyncio.sleep(2.5 if cap_ans else 1.5)
        reply_txt, _ = await _fetch_reply(ent)
        rl = reply_txt.lower()
        if fkw and any(k.strip().lower() in rl for k in re.split(r"[,，]", fkw) if k.strip()):
            ok, fail = False, True
        elif skw:
            ok = any(k.strip().lower() in rl for k in re.split(r"[,，]", skw) if k.strip())
        if ppat:
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
        notify_event("fail", a["name"], f"回复含失败词「{fkw}」: {reply_txt[:120]}")
    elif ok is False:
        desc += " (未达成功条件)"
        notify_event("fail", a["name"], f"未找到成功词「{skw}」: {reply_txt[:120]}")
    else:
        notify_event("success", a["name"], f"签到成功{('· 积分 '+points) if points else ''} · {target}")
    conn.execute(
        "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action,content,reply,grp_name) VALUES(?,?,?,?,?,?,?,?)",
        (int(time.time()), target, -a["id"], 0, "signin", cmd[:200], desc[:200], target),
    )
    conn.commit()
    log.info("签到%s 任务=%s 目标=%s", "失败" if fail or ok is False else "成功", a["name"], target)


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


async def _find_latest_buttons(ent, limit=6):
    """取目标最近消息中带按钮的那条"""
    try:
        msgs = await client.get_messages(ent, limit=limit)
        for m in msgs:
            try:
                btns = await m.get_buttons()
            except Exception:
                btns = []
            if btns:
                return m, btns
    except Exception:
        pass
    return None, []


async def _await_signin_captcha(ent, timeout=25):
    """等待目标发来带图片的验证码消息,返回 (message, image_bytes);超时返回 (None,None)"""
    deadline = time.monotonic() + timeout
    checked = set()
    while time.monotonic() < deadline:
        try:
            for m in await client.get_messages(ent, limit=8):
                if m.out or m.id in checked:
                    continue
                checked.add(m.id)
                has_img = bool(getattr(m, "photo", None))
                if not has_img and getattr(m, "document", None):
                    has_img = str(getattr(m.document, "mime_type", "")).startswith("image/")
                if not has_img:
                    continue
                img = await client.download_media(m, file=bytes)
                if img:
                    return m, img
        except Exception:
            pass
        await asyncio.sleep(1.5)
    return None, None


async def run_custom(a):
    """自定义多步任务:send/wait/click/captcha/until,链级重试"""
    target = str(a["target"] or "").strip()
    try:
        steps = json.loads(a["custom_steps"] or "[]")
    except Exception:
        steps = []
    if not target or not steps:
        return
    ent = await resolve_entity(target)
    if ent is None:
        log.warning("自定义任务目标解析失败: %s", target)
        notify_event("fail", a["name"], f"目标解析失败: {target}")
        return
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
                    await client.send_message(ent, text)
                    log_desc.append(f"发送「{text}」")
                elif t == "wait":
                    ms = max(0, int(s.get("v") or 1000))
                    await asyncio.sleep(ms / 1000)
                    log_desc.append(f"等待{ms}ms")
                elif t == "click":
                    m, btns = await _find_latest_buttons(ent)
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
                        await client.send_message(ent, render_placeholders(send_txt).replace("{code}", ans))
                        log_desc.append(f"发送「{send_txt}」")
                    else:
                        await client.send_message(ent, ans)
                elif t == "until":
                    timeout = max(1, int(s.get("timeout") or 30000))
                    succ = str(s.get("success") or "")
                    fail = str(s.get("fail") or "")
                    deadline = time.monotonic() + timeout / 1000
                    matched, failed = False, False
                    while time.monotonic() < deadline:
                        rt, _ = await _fetch_reply(ent)
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
    conn.execute(
        "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action,content,reply,grp_name) VALUES(?,?,?,?,?,?,?,?)",
        (int(time.time()), target, -a["id"], 0, "custom", f"自定义任务: {a['name']}"[:200], hits_desc, target),
    )
    conn.commit()
    if final_ok:
        notify_event("success", a["name"], hits_desc)
    else:
        notify_event("fail", a["name"], hits_desc)
    log.info("自定义任务%s 任务=%s 目标=%s", "成功" if final_ok else "失败", a["name"], target)


# 抢注轮询(开注探测):每间隔给机器人发探测命令,回复含开注关键词即注册
poll_last = {}
poll_task = None


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
                        await client.send_message(ent, probe)
                        await asyncio.sleep(2)
                        rt, _ = await _fetch_reply(ent)
                        if match_keywords(a["keywords"] or "开注,开放注册,邀请码,发码,注册,开放", rt):
                            reg = render_placeholders(
                                str(a["reply_text"] or "").replace("{code}", extract_code(rt) or "REG")
                            )
                            if reg:
                                await client.send_message(ent, reg)
                                grp_name = a["target"]
                                conn.execute(
                                    "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action,content,reply,grp_name) VALUES(?,?,?,?,?,?,?,?)",
                                    (int(time.time()), a["target"], -a["id"], 0, "grabcodes",
                                     f"轮询{probe} 命中开注"[:200], f"注册: {reg}"[:200], grp_name),
                                )
                                conn.commit()
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
    """每分钟检查一次:窗口内随机时刻执行当日签到"""
    global signin_task
    while True:
        try:
            if client is not None and tg_state["phase"] == "ready":
                autos = conn.execute(
                    "SELECT * FROM automations WHERE enabled=1 AND type='signin'"
                ).fetchall()
                _now = now_local()
                today = _now.strftime("%Y-%m-%d")
                nowm = _now.hour * 60 + _now.minute
                for a in autos:
                    if a["last_at"] == today:
                        continue
                    w0, w1 = _win_minutes(a["win_start"]), _win_minutes(a["win_end"])
                    if w0 is None or w1 is None:
                        continue
                    if w0 > w1:
                        w0, w1 = w1, w0
                    plan_key = (a["id"], today)
                    if plan_key not in signin_plans:
                        signin_plans[plan_key] = random.randint(w0, w1)
                    if nowm >= signin_plans[plan_key]:
                        await run_signin(a)
                        conn.execute("UPDATE automations SET last_at=? WHERE id=?", (today, a["id"]))
                        conn.commit()
                if len(signin_plans) > 500:
                    signin_plans.clear()
        except Exception as e:
            log.warning("签到循环异常: %s", e)
        await asyncio.sleep(60)


# ---------------- 生命周期 ----------------
@asynccontextmanager
async def lifespan(app):
    global client
    set_setting("uptime_since", get_setting("uptime_since", str(int(time.time()))))
    # 若已有会话,启动时自动登录并监听
    try:
        api_id = int(get_setting("api_id", "0"))
        api_hash = get_setting("api_hash", "")
        if api_id and api_hash and (DATA / "tg.session").exists():
            client = TelegramClient(SESSION_PATH, api_id, api_hash)
            await client.connect()
            if await client.is_user_authorized():
                await finish_login()
    except Exception as e:
        log.warning("自动登录失败(面板可重试): %s", e)
    yield
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
    return {"svg": gen_captcha(ip), "required": rl_need_captcha(ip)}


@app.post("/api/panel/login")
async def panel_login(body: PanelLogin, request: Request):
    ip = client_ip(request)
    left = rl_blocked(ip)
    if left:
        raise HTTPException(429, f"尝试次数过多,请 {left // 60 + 1} 分钟后再试")
    if rl_need_captcha(ip) and not check_captcha(ip, body.captcha):
        raise HTTPException(400, "验证码错误或已过期,点图片刷新后重试")
    if not verify_password(body.password):
        n = rl_fail(ip)
        msg = "密码错误" if n < CAPTCHA_AFTER else f"密码错误(连续 {n} 次,之后需要验证码)"
        if n >= RL_MAX:
            msg = "尝试次数过多,已暂时锁定 10 分钟"
        raise HTTPException(403, msg)
    rl_ok(ip)
    tok = create_session(ip, request.headers.get("user-agent", ""))
    resp = JSONResponse({"ok": True})
    resp.set_cookie("panel_token", tok, max_age=30 * 86400, httponly=True, samesite="lax")
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
    if client is not None:
        return
    try:
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
    global watcher_task, handler_registered, signin_task, poll_task
    tg_state["phase"] = "ready"
    tg_state["err"] = None
    me = me or await client.get_me()
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
    if body.type not in ("signin", "grabcodes", "redpacket", "custom"):
        raise HTTPException(400, "类型必须是 signin / grabcodes / redpacket / custom")
    if not body.name.strip():
        raise HTTPException(400, "请填写任务名称")
    if body.type == "signin":
        if not body.target.strip() or not body.command.strip():
            raise HTTPException(400, "签到需要填写目标(@机器人)和命令")
        if _win_minutes(body.win_start) is None or _win_minutes(body.win_end) is None:
            raise HTTPException(400, "时间窗口格式需为 hh:mm")
        if body.retries < 0 or body.retries > 5:
            raise HTTPException(400, "重试次数需在 0-5 之间")
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


_AUTO_COLS = ("type,name,target,command,win_start,win_end,click_btn,btn_text,success_kw,fail_kw,points_pat,is_chat,retries,signin_captcha,"
              "groups,keywords,action,reply_text,poll_interval,custom_steps,chain_retry,cooldown,enabled")


def _auto_vals(body: AutoIn, extra=None):
    return [
        body.type, body.name.strip(), body.target.strip(), body.command.strip(),
        body.win_start.strip(), body.win_end.strip(), body.click_btn,
        body.btn_text.strip(), body.success_kw.strip(), body.fail_kw.strip(),
        body.points_pat.strip(), body.is_chat, body.retries, body.signin_captcha,
        body.groups.strip(), body.keywords.strip(), body.action, body.reply_text.strip(),
        body.poll_interval, body.custom_steps.strip(), body.chain_retry,
        body.cooldown, body.enabled,
    ] + (extra or [])


@app.get("/api/autos")
async def list_autos():
    rows = conn.execute("SELECT * FROM automations ORDER BY id DESC").fetchall()
    return {"autos": [dict(r) for r in rows]}


@app.post("/api/autos")
async def create_auto(body: AutoIn):
    _valid_auto(body)
    cur = conn.execute(
        f"INSERT INTO automations({_AUTO_COLS},created_at) VALUES({','.join('?' * (len(_AUTO_COLS.split(',')) + 1))})",
        _auto_vals(body, [int(time.time())]),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@app.put("/api/autos/{aid}")
async def update_auto(aid: int, body: AutoIn):
    _valid_auto(body)
    cols = _AUTO_COLS.split(",")
    conn.execute(
        f"UPDATE automations SET {','.join(c + '=?' for c in cols)} WHERE id=?",
        _auto_vals(body, [aid]),
    )
    conn.commit()
    return {"ok": True}


@app.post("/api/autos/{aid}/toggle")
async def toggle_auto(aid: int):
    r = conn.execute("SELECT enabled FROM automations WHERE id=?", (aid,)).fetchone()
    if not r:
        raise HTTPException(404, "任务不存在")
    v = 0 if r["enabled"] else 1
    conn.execute("UPDATE automations SET enabled=? WHERE id=?", (v, aid))
    conn.commit()
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
    return {"ok": True, "id": cur.lastrowid}


@app.delete("/api/autos/{aid}")
async def delete_auto(aid: int):
    conn.execute("DELETE FROM automations WHERE id=?", (aid,))
    conn.commit()
    return {"ok": True}


@app.post("/api/autos/{aid}/run")
async def run_auto_now(aid: int):
    """手动立即执行:signin/custom"""
    a = conn.execute("SELECT * FROM automations WHERE id=? AND type IN ('signin','custom')", (aid,)).fetchone()
    if not a:
        raise HTTPException(404, "该类型任务不可手动执行或不存在")
    if client is None or tg_state["phase"] != "ready":
        raise HTTPException(409, "TG 尚未登录")
    if a["type"] == "signin":
        asyncio.get_running_loop().create_task(run_signin(a))
    else:
        asyncio.get_running_loop().create_task(run_custom(a))
    return {"ok": True}


@app.delete("/api/rules/{rid}")
async def delete_rule(rid: int):
    conn.execute("DELETE FROM rules WHERE id=?", (rid,))
    conn.commit()
    invalidate_cache()
    return {"ok": True}


# ---------- 设置 ----------
SETTING_KEYS = ["tz", "cooldown", "delay", "ignore_bots", "api_id", "api_hash", "theme", "avatar",
                "notify_bark", "notify_bt", "notify_bc", "notify_on_success", "notify_on_fail",
                "ai_base", "ai_key", "ai_model", "ai_enabled", "ocr_enabled",
                "flock_enabled", "flock_count", "flock_minutes", "flock_pool"]
DEFAULTS = {"cooldown": "20", "delay": "1.0 3.0", "ignore_bots": "1", "theme": "dark",
            "flock_count": "5", "flock_minutes": "5", "ocr_enabled": "1"}


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


@app.post("/api/panel/password")
async def change_panel_password(body: dict, request: Request):
    pw = str(body.get("password", "")).strip()
    old = str(body.get("old", "") or "")
    if len(pw) < 8:
        raise HTTPException(400, "面板密码至少 8 位")
    if old and not verify_password(old):
        raise HTTPException(403, "原密码不对")
    set_setting("panel_pw_hash", hash_password(pw))
    drop_all_sessions()
    # 当前设备自动续一个会话,其它设备会被踢下线
    tok = create_session(client_ip(request), request.headers.get("user-agent", ""))
    resp = JSONResponse({"ok": True})
    resp.set_cookie("panel_token", tok, max_age=30 * 86400, httponly=True, samesite="lax")
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


@app.get("/api/logs")
async def get_logs(n: int = 60):
    rows = conn.execute(
        "SELECT ts,chat_id,rule_id,msg_id,action,content,reply,grp_name FROM hits ORDER BY id DESC LIMIT ?",
        (min(n, 200),),
    ).fetchall()
    return {"hits": [dict(r) for r in rows]}


# ---------- 前端 ----------
@app.get("/")
async def index():
    return FileResponse(BASE / "web" / "index.html")


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
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
