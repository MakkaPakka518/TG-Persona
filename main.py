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
from contextlib import asynccontextmanager
from pathlib import Path

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
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row


def db_init():
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
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
            ts INTEGER, chat_id TEXT, rule_id INTEGER, msg_id INTEGER, action TEXT
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
            enabled INTEGER DEFAULT 1,
            last_at TEXT DEFAULT '',            -- 最近执行(签到存日期 YYYY-MM-DD,防当日重复)
            created_at INTEGER
        );
        """
    )
    # 兼容旧库:补充新增列
    for col, ddl in (("btn_text", "TEXT DEFAULT ''"), ("success_kw", "TEXT DEFAULT ''"), ("retries", "INTEGER DEFAULT 2")):
        try:
            conn.execute(f"ALTER TABLE automations ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE rules ADD COLUMN reaction TEXT DEFAULT ''")
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

# ---------------- 全局状态 ----------------
client = None              # Telethon TelegramClient (登录后持活)
tg_state = {"phase": "idle", "phone": "", "api_ok": False, "me": None, "err": None}
tg_phone = ""
hits_count = {"total": 0}
watcher_task = None
handler_registered = False
last_hit_ts = {}           # (chat_id, rule_id) -> 上次命中时间
_running = {"paused": False}


def panel_password():
    """面板密码:env PANEL_PASSWORD > data/panel_password.txt(首次自动生成)> 打印日志"""
    env = os.environ.get("PANEL_PASSWORD")
    if env:
        return env
    if PW_FILE.exists():
        return PW_FILE.read_text().strip()
    pw = secrets.token_urlsafe(8)
    PW_FILE.write_text(pw)
    PW_FILE.chmod(0o600)
    log.info("面板密码已生成(写入 data/panel_password.txt): %s", pw)
    return pw


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
    rows = conn.execute("SELECT chat_id FROM watched_groups").fetchall()
    if not rows or str(event.chat_id) not in {r["chat_id"] for r in rows}:
        return
    text = (event.raw_text or "").strip()
    if not text:
        return

    rules = conn.execute(
        "SELECT * FROM rules WHERE enabled=1 ORDER BY id"
    ).fetchall()
    rule = next((r for r in rules if match_rule(r, text)), None)
    if rule is None:
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
    try:
        if action in ("reply", "both") and rule["value"]:
            reply = random.choice([s.strip() for s in rule["value"].split("||") if s.strip()])
            await event.reply(reply)
        if action in ("react", "both"):
            emo = (rule["reaction"] or rule["value"]).replace("\ufe0f", "")
            if emo:
                await client(
                    SendReactionRequest(
                        peer=await event.get_input_chat(),
                        msg_id=event.id,
                        reaction=[ReactionEmoji(emoticon=emo)],
                    )
                )
        hits_count["total"] += 1
        conn.execute(
            "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action) VALUES(?,?,?,?,?)",
            (int(time.time()), str(event.chat_id), rule["id"], event.id, action),
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
    """后台监听:登录成功后启动;断线自动重连"""
    global watcher_task
    while True:
        try:
            if client is not None:
                await client.run_until_disconnected()
        except Exception as e:
            log.warning("监听连接断开: %s", e)
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
            if a["type"] == "grabcodes":
                code = extract_code(text)
                rt = str(a["reply_text"] or "").replace("{code}", code)
                if not rt:
                    continue
                if a["action"] == "send":
                    await event.respond(rt)
                else:
                    await event.reply(rt)
            else:
                buttons = await event.get_buttons()
                target_btn = None
                for row in buttons:
                    for b in row:
                        if re.search(r"打开|领取|收下|拆|红包|🎁|领", b.text or ""):
                            target_btn = b
                            break
                    if target_btn:
                        break
                if not target_btn and buttons and buttons[0]:
                    target_btn = buttons[0][0]
                if target_btn:
                    await target_btn.click()
                    log.info("抢红包 群=%s 任务=%s", chat, a["name"])
            conn.execute(
                "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action) VALUES(?,?,?,?,?)",
                (int(time.time()), chat, -a["id"], event.id, a["type"]),
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


async def run_signin(a):
    """执行单条签到:发命令(支持占位符),可选点按钮,按成功关键词判定"""
    target = str(a["target"] or "").strip()
    cmd = render_placeholders(str(a["command"] or "").strip())
    if not target or not cmd:
        return
    ent = await resolve_entity(target)
    if ent is None:
        log.warning("签到目标解析失败: %s", target)
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
        return

    # 可选:点击回复按钮
    if a["click_btn"]:
        try:
            await asyncio.sleep(1.6)
            recent = await client.get_messages(ent, limit=3)
            for m in recent:
                if not m.out:
                    continue
                btns = await m.get_buttons()
                b = _find_btn(btns, a["btn_text"])
                if b:
                    await b.click()
                break
        except errors.FloodWaitError as e:
            log.warning("点按钮限流,等待 %ss", e.seconds)
            await asyncio.sleep(e.seconds + 1)
        except Exception:
            log.exception("签到点按钮异常")

    # 成功关键词判定:机器人回复含关键词才算成功
    ok = True
    skw = str(a["success_kw"] or "").strip()
    if skw:
        try:
            await asyncio.sleep(1.5)
            recent = await client.get_messages(ent, limit=4)
            bot_reply = next((m for m in recent if not m.out), None)
            reply_txt = (bot_reply.raw_text or "") if bot_reply else ""
            ok = skw.lower() in reply_txt.lower()
            if not ok:
                log.warning("签到未达成功条件(未找到「%s」),目标=%s", skw, target)
        except Exception:
            pass
    conn.execute(
        "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action) VALUES(?,?,?,?,?)",
        (int(time.time()), target, -a["id"], 0, "signin"),
    )
    conn.commit()
    log.info("签到%s 任务=%s 目标=%s", "成功" if ok else "(结果未知)", a["name"], target)


async def signin_loop():
    """每分钟检查一次:窗口内随机时刻执行当日签到"""
    global signin_task
    while True:
        try:
            if client is not None and tg_state["phase"] == "ready":
                autos = conn.execute(
                    "SELECT * FROM automations WHERE enabled=1 AND type='signin'"
                ).fetchall()
                today = time.strftime("%Y-%m-%d")
                nowm = time.localtime().tm_hour * 60 + time.localtime().tm_min
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
    """除登录/静态资源外,面板需要密码鉴权"""
    path = request.url.path
    if path.startswith("/api/panel/") or path.startswith("/static/") or path in ("/",):
        return await call_next(request)
    if path.startswith("/api/"):
        if request.cookies.get("panel_token") == panel_password():
            return await call_next(request)
        return JSONResponse({"error": "未登录面板"}, status_code=401)
    return await call_next(request)


# ---------- 面板登录 ----------
class PanelLogin(BaseModel):
    password: str


@app.post("/api/panel/login")
async def panel_login(body: PanelLogin):
    if body.password == panel_password():
        resp = JSONResponse({"ok": True})
        resp.set_cookie("panel_token", panel_password(), max_age=86400 * 30, httponly=True)
        return resp
    raise HTTPException(403, "密码错误")


@app.post("/api/panel/logout")
async def panel_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("panel_token")
    return resp


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
    global watcher_task, handler_registered, signin_task
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
@app.get("/api/dialogs")
async def list_dialogs(q: str = ""):
    if client is None or tg_state["phase"] != "ready":
        raise HTTPException(409, "TG 尚未登录")
    watched = {r["chat_id"] for r in conn.execute("SELECT chat_id FROM watched_groups").fetchall()}
    out = []
    try:
        async for d in client.iter_dialogs(limit=500):
            if not d.is_group:
                continue
            name = d.name or "(无名称)"
            cid = str(d.id)
            if q and q.lower() not in name.lower():
                continue
            out.append({
                "chat_id": cid,
                "name": name,
                "members": getattr(d.entity, "participants_count", None),
                "watched": cid in watched,
            })
    except Exception as e:
        raise HTTPException(500, f"拉取群列表失败: {e}")
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
    return {"ok": True}


@app.delete("/api/groups/{chat_id}")
async def remove_group(chat_id: str):
    conn.execute("DELETE FROM watched_groups WHERE chat_id=?", (chat_id,))
    conn.commit()
    return {"ok": True}


# ---------- 规则管理 ----------
class RuleIn(BaseModel):
    keywords: str = Field(min_length=1)
    regex: int = 0
    action: str = "reply"       # reply | react | both
    value: str = ""
    reaction: str = ""          # 表情(both 时独立存,react 时用 value)
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
        "INSERT INTO rules(keywords,regex,action,value,reaction,enabled,created_at) VALUES(?,?,?,?,?,?,?)",
        (body.keywords.strip(), body.regex, body.action, body.value.strip(), body.reaction.strip(), body.enabled, int(time.time())),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@app.put("/api/rules/{rid}")
async def update_rule(rid: int, body: RuleIn):
    conn.execute(
        "UPDATE rules SET keywords=?,regex=?,action=?,value=?,reaction=?,enabled=? WHERE id=?",
        (body.keywords.strip(), body.regex, body.action, body.value.strip(), body.reaction.strip(), body.enabled, rid),
    )
    conn.commit()
    return {"ok": True}


# ---------- 自动化管理 (签到 / 抢注 / 抢红包) ----------
class AutoIn(BaseModel):
    type: str = "signin"          # signin | grabcodes | redpacket
    name: str = ""
    target: str = ""              # signin: @机器人 或 群ID
    command: str = ""             # signin: 签到命令(支持 {num:N}/{word:N}/{alpha:N}/{uuid})
    win_start: str = "08:00"      # signin: 窗口开始
    win_end: str = "12:00"        # signin: 窗口结束
    click_btn: int = 0            # signin: 签到后自动点按钮
    btn_text: str = ""            # signin: 按钮文字(留空自动匹配)
    success_kw: str = ""          # signin: 成功关键词
    retries: int = 2              # signin: 发送失败重试次数
    groups: str = ""              # grabcodes/redpacket: 指定群(留空=全部监听群)
    keywords: str = ""            # 触发关键词
    action: str = "reply"         # grabcodes: reply | send
    reply_text: str = ""          # 回复/命令文本,{code} 提取邀请码
    cooldown: int = 30
    enabled: int = 1


def _valid_auto(body: AutoIn):
    if body.type not in ("signin", "grabcodes", "redpacket"):
        raise HTTPException(400, "类型必须是 signin / grabcodes / redpacket")
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
    if body.cooldown < 0 or body.cooldown > 86400:
        raise HTTPException(400, "冷却秒数无效")


@app.get("/api/autos")
async def list_autos():
    rows = conn.execute("SELECT * FROM automations ORDER BY id DESC").fetchall()
    return {"autos": [dict(r) for r in rows]}


@app.post("/api/autos")
async def create_auto(body: AutoIn):
    _valid_auto(body)
    cur = conn.execute(
        "INSERT INTO automations(type,name,target,command,win_start,win_end,click_btn,btn_text,success_kw,retries,"
        "groups,keywords,action,reply_text,cooldown,enabled,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (body.type, body.name.strip(), body.target.strip(), body.command.strip(),
         body.win_start.strip(), body.win_end.strip(), body.click_btn,
         body.btn_text.strip(), body.success_kw.strip(), body.retries,
         body.groups.strip(), body.keywords.strip(), body.action, body.reply_text.strip(),
         body.cooldown, body.enabled, int(time.time())),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@app.put("/api/autos/{aid}")
async def update_auto(aid: int, body: AutoIn):
    _valid_auto(body)
    conn.execute(
        "UPDATE automations SET type=?,name=?,target=?,command=?,win_start=?,win_end=?,click_btn=?,btn_text=?,success_kw=?,retries=?,"
        "groups=?,keywords=?,action=?,reply_text=?,cooldown=?,enabled=? WHERE id=?",
        (body.type, body.name.strip(), body.target.strip(), body.command.strip(),
         body.win_start.strip(), body.win_end.strip(), body.click_btn,
         body.btn_text.strip(), body.success_kw.strip(), body.retries,
         body.groups.strip(), body.keywords.strip(), body.action, body.reply_text.strip(),
         body.cooldown, body.enabled, aid),
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


@app.delete("/api/autos/{aid}")
async def delete_auto(aid: int):
    conn.execute("DELETE FROM automations WHERE id=?", (aid,))
    conn.commit()
    return {"ok": True}


@app.post("/api/autos/{aid}/run")
async def run_auto_now(aid: int):
    """手动立即执行一次签到任务"""
    a = conn.execute("SELECT * FROM automations WHERE id=? AND type='signin'", (aid,)).fetchone()
    if not a:
        raise HTTPException(404, "签到任务不存在")
    if client is None or tg_state["phase"] != "ready":
        raise HTTPException(409, "TG 尚未登录")
    asyncio.get_running_loop().create_task(run_signin(a))
    return {"ok": True}


@app.delete("/api/rules/{rid}")
async def delete_rule(rid: int):
    conn.execute("DELETE FROM rules WHERE id=?", (rid,))
    conn.commit()
    return {"ok": True}


# ---------- 设置 ----------
@app.get("/api/settings")
async def get_settings():
    keys = ["cooldown", "delay", "ignore_bots", "api_id", "api_hash", "theme", "avatar"]
    return {k: get_setting(k, {"cooldown": "20", "delay": "1.0 3.0", "ignore_bots": "1", "theme": "dark"}.get(k)) for k in keys}


@app.post("/api/settings")
async def update_settings(body: dict):
    for k, v in body.items():
        if k in ("cooldown", "delay", "ignore_bots", "api_id", "api_hash", "theme", "avatar"):
            set_setting(k, str(v))
    return {"ok": True}


@app.post("/api/panel/password")
async def change_panel_password(body: dict):
    pw = str(body.get("password", "")).strip()
    if len(pw) < 6:
        raise HTTPException(400, "面板密码至少 6 位")
    PW_FILE.write_text(pw)
    PW_FILE.chmod(0o600)
    return {"ok": True}


@app.post("/api/toggle")
async def toggle_paused(body: dict):
    _running["paused"] = bool(body.get("paused", False))
    return {"ok": True, "paused": _running["paused"]}


# ---------- 状态 & 日志 ----------
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
        "panel_need_change": not os.environ.get("PANEL_PASSWORD") and PW_FILE.exists(),
    }


@app.get("/api/logs")
async def get_logs(n: int = 60):
    rows = conn.execute(
        "SELECT ts,chat_id,rule_id,msg_id,action FROM hits ORDER BY id DESC LIMIT ?", (min(n, 200),)
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
