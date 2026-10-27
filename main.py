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
            value TEXT NOT NULL,                          -- 回复内容(||分隔多条) 或 表情
            enabled INTEGER DEFAULT 1,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER, chat_id TEXT, rule_id INTEGER, msg_id INTEGER, action TEXT
        );
        """
    )
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
    if rule.get("regex"):
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
            emo = rule["value"].replace("\ufe0f", "")
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
    global watcher_task, handler_registered
    tg_state["phase"] = "ready"
    tg_state["err"] = None
    me = me or await client.get_me()
    tg_state["me"] = {
        "id": me.id,
        "name": getattr(me, "first_name", "") + (" " + me.last_name if getattr(me, "last_name", None) else ""),
        "username": getattr(me, "username", "") or "",
        "phone": getattr(me, "phone", "") or "",
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
    if not handler_registered:
        client.add_event_handler(on_group_message, events.NewMessage(incoming=True))
        handler_registered = True
    if watcher_task is None or watcher_task.done():
        watcher_task = asyncio.create_task(watcher_loop())
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
    if body.action == "react" and (len(body.value.replace("\ufe0f", "")) > 8):
        raise HTTPException(400, "表情只能填一个 emoji")
    if body.action == "react" and not body.value.strip():
        raise HTTPException(400, "表情不能为空")
    cur = conn.execute(
        "INSERT INTO rules(keywords,regex,action,value,enabled,created_at) VALUES(?,?,?,?,?,?)",
        (body.keywords.strip(), body.regex, body.action, body.value.strip(), body.enabled, int(time.time())),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@app.put("/api/rules/{rid}")
async def update_rule(rid: int, body: RuleIn):
    conn.execute(
        "UPDATE rules SET keywords=?,regex=?,action=?,value=?,enabled=? WHERE id=?",
        (body.keywords.strip(), body.regex, body.action, body.value.strip(), body.enabled, rid),
    )
    conn.commit()
    return {"ok": True}


@app.delete("/api/rules/{rid}")
async def delete_rule(rid: int):
    conn.execute("DELETE FROM rules WHERE id=?", (rid,))
    conn.commit()
    return {"ok": True}


# ---------- 设置 ----------
@app.get("/api/settings")
async def get_settings():
    keys = ["cooldown", "delay", "ignore_bots", "api_id", "api_hash"]
    return {k: get_setting(k, {"cooldown": "20", "delay": "1.0 3.0", "ignore_bots": "1"}.get(k)) for k in keys}


@app.post("/api/settings")
async def update_settings(body: dict):
    for k, v in body.items():
        if k in ("cooldown", "delay", "ignore_bots", "api_id", "api_hash"):
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


# ---------------- 启动 ----------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
