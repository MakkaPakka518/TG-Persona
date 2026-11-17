#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Emby 观看保号（伪装观看）

从反代项目里剥离出来的「自动保活」逻辑，落进 TG Persona：
- 身份层：固定 Client / Device / Version / UA / DeviceId，一台设备长期不变（防止刷爆 Emby 设备数）
- 播放链路：探活 → 登录（token 缓存复用）→ 取片 → PlaybackInfo → 首段拉流校验 →
  起播 → 10 秒心跳（前 1~2 跳起播缓冲、1~2 次快进）→ 停止 → 延迟清观看记录
- 调度：队列 + 并发上限 + 失败不消耗间隔（默认 4 小时后重试）+ 连续 4 次失败熔断 12 小时
- 自愈：进程重启时把残留的“正在播放”会话补发 Stopped 并清记录

本模块只做“观看”，不做反代、不做下载，也不改写别人的请求头。
"""
import asyncio
import json
import logging
import random
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:          # pragma: no cover - 缺 httpx 时由上层报错
    httpx = None

log = logging.getLogger("persona.emby")

TICK_SEC = 20                 # 调度循环间隔
HEARTBEAT_SEC = 10            # 播放心跳间隔（真实播放器节奏）
PLAN_STEP_SEC = 10            # 播放计划粒度（与心跳间隔独立，测试可只压缩心跳）
STALL_RANGE = (2.0, 5.0)      # 模拟网络卡顿时多等几秒
SEGMENT_MB = 20               # 每跳拉流分段
CLEAN_DELAY_SEC = 1.5          # Stopped 落库后再清观看记录
ZOMBIE_SEC = 300              # running 任务超过该时长无更新 → 判僵尸
DEVICE_ID_RE = re.compile(r"^[0-9a-zA-Z-]{8,64}$")

BUILTIN_PROFILE = {
    "client": "SenPlayer",
    "version": "6.2.2",
    "device": "iPhone",
    "device_id": "9c4e7a2b-6d8f-4b3a-9e2c-1f5d8a0b7e64",
    "ua": "SenPlayer/6.2.2 (iPhone; iOS 17.5; Scale/3.00)",
}

# 预设身份：与 CF 面板「保活专用 UA」同一套值，可直接套用
BUILTIN_TEMPLATES = [
    ("senplayer", "SenPlayer", "SenPlayer", "6.2.2", "iPhone", "SenPlayer/6.2.2"),
    ("eplayerx", "EplayerX", "EplayerX", "10.0.22", "iPhone", "EplayerX/10.0.22"),
    ("forward", "Forward", "Forward", "1.3.18", "iPhone", "Forward-Standard/1.3.18"),
    ("rex", "Rex", "Rex", "1.0.1", "iPhone17,2", "Rex/1.0.1"),
    ("hills", "Hills", "Hills", "1.6.0", "PD2505", "Hills/1.6.0 (android; 16)"),
    ("yamby", "Yamby", "Yamby", "2.0.0.5", "vivo-V2505A", "Yamby/2.0.0.5(Android)"),
]
WEB_CLIENTS = ("emby web", "emby theater", "emby for web", "web", "theater")

DEFAULT_CONFIG = {
    "concurrency": 1,
    "retry_hours": 4,
    "cap_mb": 400,
    "notify_mode": "fail",          # all | fail | off
    "default_profile": dict(BUILTIN_PROFILE),
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS emby_templates (
    key TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    client TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '', ua TEXT NOT NULL DEFAULT '',
    device_id TEXT NOT NULL DEFAULT '', builtin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS emby_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '', base_url TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '', password TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '',
    access_token TEXT NOT NULL DEFAULT '',
    client TEXT NOT NULL DEFAULT '', device TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '', ua TEXT NOT NULL DEFAULT '',
    device_id TEXT NOT NULL DEFAULT '', template_key TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'play', enabled INTEGER NOT NULL DEFAULT 1,
    interval_days REAL NOT NULL DEFAULT 3, stream_cap_mb INTEGER NOT NULL DEFAULT 400,
    clean_userdata INTEGER NOT NULL DEFAULT 1, notify_mode TEXT NOT NULL DEFAULT 'fail',
    next_run_at INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0, circuit_until INTEGER NOT NULL DEFAULT 0,
    last_run_at INTEGER NOT NULL DEFAULT 0, last_result TEXT NOT NULL DEFAULT '',
    last_detail TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS emby_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'auto',
    status TEXT NOT NULL DEFAULT 'waiting',
    started_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0,
    item_id TEXT NOT NULL DEFAULT '', item_name TEXT NOT NULL DEFAULT '',
    media_source_id TEXT NOT NULL DEFAULT '', play_session_id TEXT NOT NULL DEFAULT '',
    start_ticks INTEGER NOT NULL DEFAULT 0, end_ticks INTEGER NOT NULL DEFAULT 0,
    play_sec INTEGER NOT NULL DEFAULT 0, elapsed INTEGER NOT NULL DEFAULT 0,
    bytes_read INTEGER NOT NULL DEFAULT 0, cap_bytes INTEGER NOT NULL DEFAULT 0,
    steps_json TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS emby_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0, msg TEXT NOT NULL DEFAULT '',
    item TEXT NOT NULL DEFAULT '', play_sec INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0, steps_json TEXT NOT NULL DEFAULT '[]',
    finished_at INTEGER NOT NULL DEFAULT 0
);
"""

_LIVE: Dict[str, dict] = {}       # key(账号上下文) -> 实时进度
_TASKS: Dict[str, dict] = {}      # key -> {job_id: task}
_ADVANCING: set = set()


# ---------------------------------------------------------------- 基础工具
def _now() -> int:
    return int(time.time())


def new_device_id() -> str:
    return str(uuid.uuid4())


def init_schema(db) -> None:
    """建表 + 播种内置身份模板（幂等，不覆盖用户改过的自定义模板）"""
    db.executescript(SCHEMA_SQL)
    for key, name, client, version, device, ua in BUILTIN_TEMPLATES:
        db.execute(
            "INSERT OR IGNORE INTO emby_templates(key,name,client,version,device,ua,device_id,builtin) "
            "VALUES(?,?,?,?,?,?,?,1)",
            (key, name, client, version, device, ua, ""),
        )
    db.commit()


def _row_dict(row) -> Optional[dict]:
    return dict(row) if row is not None else None


def _as_int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_list(text) -> list:
    try:
        value = json.loads(text or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def redact(url: str) -> str:
    """日志/错误里的 api_key 一律打码"""
    return re.sub(r"api_key=[^&\s]+", "api_key=***", str(url or ""))


# ---------------------------------------------------------------- 身份层
def validate_profile(client: str, version: str, device: str, ua: str, device_id: str) -> Tuple[bool, str]:
    """保存前校验身份自洽，避免 UA 是 SenPlayer、Client 却是 Emby Web 这种一眼假的组合"""
    client = (client or "").strip()
    version = (version or "").strip()
    device = (device or "").strip()
    ua = (ua or "").strip()
    device_id = (device_id or "").strip()
    if not client or len(client) > 40:
        return False, "Client 不能为空且不超过 40 字"
    if client.lower() in WEB_CLIENTS or "web" in client.lower():
        return False, "Client 不能用 Web 身份（很多 Emby 会拒绝 Web 客户端）"
    if not version or not re.search(r"\d", version):
        return False, "Version 必须包含版本号"
    if not device or len(device) > 60:
        return False, "Device 不能为空且不超过 60 字"
    if not device_id or not DEVICE_ID_RE.match(device_id):
        return False, "DeviceId 需为 8~64 位字母、数字或连字符"
    if not ua or len(ua) > 300:
        return False, "UA 不能为空且不超过 300 字"
    if version not in ua:
        return False, "UA 里必须包含 Version（%s）" % version
    if not ua.lower().startswith(client.lower()):
        return False, "UA 应当以 Client 开头（如 %s/%s），否则身份不自洽" % (client, version)
    if "web" in ua.lower() and "web" not in client.lower():
        return False, "UA 不能用 Web 浏览器标识"
    return True, ""


def build_headers(profile: dict) -> dict:
    """所有请求（登录/取片/播放/心跳/停止/清记录）统一走这里，禁止各处手拼"""
    client = profile["client"]
    version = profile["version"]
    device = profile["device"]
    device_id = profile["device_id"]
    return {
        "X-Emby-Authorization": 'MediaBrowser Client="%s", Device="%s", DeviceId="%s", Version="%s"'
                                 % (client, device, device_id, version),
        "User-Agent": profile["ua"],
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "x-emby-client": client,
        "x-emby-client-version": version,
        "x-emby-device-name": device,
        "x-emby-device-id": device_id,
    }


def profile_of(account: dict) -> dict:
    return {
        "client": account.get("client") or BUILTIN_PROFILE["client"],
        "version": account.get("version") or BUILTIN_PROFILE["version"],
        "device": account.get("device") or BUILTIN_PROFILE["device"],
        "device_id": account.get("device_id") or BUILTIN_PROFILE["device_id"],
        "ua": account.get("ua") or BUILTIN_PROFILE["ua"],
    }


# ---------------------------------------------------------------- 节奏层
def play_sec_by_runtime(runtime_ticks: int) -> int:
    """时长按片长映射：15 分钟→90 秒、60 分钟→390 秒，线性；未知片长 90~390 随机"""
    minutes = (runtime_ticks or 0) / 600000000.0
    if minutes <= 0:
        return 90 + random.randint(0, 300)
    t = (minutes - 15) / 45.0
    t = min(1.0, max(0.0, t))
    return int(round(90 + t * 300))


def build_plan(start_ticks: int, end_ticks: int, play_sec: int,
               step_sec: int = PLAN_STEP_SEC) -> Tuple[List[int], dict]:
    """把播放切成每 10 秒一跳：前 1~2 跳按 45% 速度（起播缓冲）、中段 1~2 次快进，
    位置序列严格单调、总和等于 end_ticks。"""
    n = max(1, int(round((play_sec or step_sec) / float(step_sec))))
    total = max(10000000, int(end_ticks) - int(start_ticks))
    base = total / float(n)
    delta = [base] * n
    meta = {"buffer_ticks": 0, "seeks": []}
    buffer_n = min(n - 1, 2 if n >= 5 else 1)
    if buffer_n > 0:
        meta["buffer_ticks"] = buffer_n
        cut = base * 0.55 * buffer_n
        for i in range(buffer_n):
            delta[i] = base * 0.45
        rest = n - buffer_n
        if rest > 0:
            for i in range(buffer_n, n):
                delta[i] += cut / rest
    seek_count = (2 if random.random() < 0.5 else 1) if n >= 8 else (1 if n >= 5 else 0)
    for s in range(seek_count):
        lo = int(n * (0.3 + s * 0.32))
        hi = max(lo, int(n * (0.45 + s * 0.3)))
        if hi <= lo:
            continue
        k = random.randint(lo, hi)
        if k >= n - 1:
            continue
        later = n - k - 1
        jump = min(base * later * 0.55, total * (0.06 + random.random() * 0.05))
        if jump <= 0:
            continue
        delta[k] += jump
        for i in range(k + 1, n):
            delta[i] = max(base * 0.3, delta[i] - jump / float(later))
        meta["seeks"].append({"tick": k + 1, "jump": int(round(jump / 10000000.0))})
    diff = total - sum(delta)
    delta[n - 1] = max(1, delta[n - 1] + diff)
    plan = []
    cur = int(start_ticks)
    for i in range(n):
        cur += int(round(delta[i]))
        plan.append(min(cur, int(end_ticks)))
    plan[n - 1] = int(end_ticks)
    for i in range(1, n):
        if plan[i] < plan[i - 1]:
            plan[i] = plan[i - 1]
    return plan, meta


def cap_bytes(size_bytes: int, bit_rate: int, play_sec: int, cap_mb: int = 400) -> int:
    """拉流上限 = max(文件 35%, 播放时长所需流量)，再受面板上限约束"""
    hard = max(1, _as_int(cap_mb, 400)) * 1024 * 1024
    need = int(play_sec * bit_rate / 8.0) if play_sec > 0 and bit_rate > 0 else 0
    by_pct = int(size_bytes * 0.35) if size_bytes > 0 else 0
    cap = max(by_pct, need)
    if cap <= 0:
        return hard
    return min(cap, hard)


def next_run_offset_ms(days: float) -> int:
    """成功后的下次时间：天数设置只是上限，实际在「上限的 50% ~ 上限」之间随机。

    连续随机（不取整到天），所以每次连「日期」和「几点几分」都会变，
    不会出现每 14 天准点看一次这种一眼就假的规律。
    """
    days = max(0.1, min(60, _as_float(days, 3)))
    lo = days * 0.5
    hi = max(lo, days)
    offset_days = lo + random.random() * (hi - lo)
    return max(1, int(offset_days * 86400000))


def fmt_pos(ticks) -> str:
    sec = max(0, int(_as_int(ticks) / 10000000))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return ("%02d:" % h if h else "") + "%02d:%02d" % (m, s)


# ---------------------------------------------------------------- 配置
def get_config(db) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        row = db.execute("SELECT value FROM settings WHERE key='emby_ka_config'").fetchone()
        if row and row["value"]:
            data = json.loads(row["value"])
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if v is not None})
    except (TypeError, ValueError, Exception):
        pass
    profile = cfg.get("default_profile") or {}
    if not isinstance(profile, dict):
        profile = {}
    merged = dict(BUILTIN_PROFILE)
    for key in ("client", "version", "device", "ua", "device_id"):
        if profile.get(key):
            merged[key] = str(profile[key])
    cfg["default_profile"] = merged
    cfg["concurrency"] = max(1, min(3, _as_int(cfg.get("concurrency"), 1)))
    cfg["retry_hours"] = max(0.5, min(48, _as_float(cfg.get("retry_hours"), 4)))
    cfg["cap_mb"] = max(20, min(1024, _as_int(cfg.get("cap_mb"), 400)))
    if cfg.get("notify_mode") not in ("all", "fail", "off"):
        cfg["notify_mode"] = "fail"
    return cfg


def save_config(db, patch: dict) -> dict:
    cfg = get_config(db)
    if "concurrency" in patch:
        cfg["concurrency"] = max(1, min(3, _as_int(patch.get("concurrency"), 1)))
    if "retry_hours" in patch:
        cfg["retry_hours"] = max(0.5, min(48, _as_float(patch.get("retry_hours"), 4)))
    if "cap_mb" in patch:
        cfg["cap_mb"] = max(20, min(1024, _as_int(patch.get("cap_mb"), 400)))
    if "notify_mode" in patch and patch.get("notify_mode") in ("all", "fail", "off"):
        cfg["notify_mode"] = patch["notify_mode"]
    if isinstance(patch.get("default_profile"), dict):
        p = patch["default_profile"]
        ok, msg = validate_profile(p.get("client", ""), p.get("version", ""),
                                   p.get("device", ""), p.get("ua", ""),
                                   p.get("device_id") or new_device_id())
        if not ok:
            raise ValueError(msg)
        cfg["default_profile"] = {
            "client": p["client"].strip(), "version": p["version"].strip(),
            "device": p["device"].strip(), "ua": p["ua"].strip(),
            "device_id": (p.get("device_id") or "").strip() or new_device_id(),
        }
    db.execute("INSERT INTO settings(key,value) VALUES('emby_ka_config',?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(cfg),))
    db.commit()
    return cfg


# ---------------------------------------------------------------- 模板
def list_templates(db) -> List[dict]:
    rows = db.execute("SELECT * FROM emby_templates ORDER BY builtin DESC, name").fetchall()
    return [dict(r) for r in rows]


def save_template(db, payload: dict) -> dict:
    key = str(payload.get("key") or "").strip()[:40]
    if not key or not re.match(r"^[a-zA-Z0-9_-]{2,40}$", key):
        raise ValueError("模板标识需为 2~40 位字母/数字/下划线")
    client = str(payload.get("client") or "").strip()
    version = str(payload.get("version") or "").strip()
    device = str(payload.get("device") or "").strip()
    ua = str(payload.get("ua") or "").strip()
    device_id = str(payload.get("device_id") or "").strip() or new_device_id()
    ok, msg = validate_profile(client, version, device, ua, device_id)
    if not ok:
        raise ValueError(msg)
    db.execute("INSERT INTO emby_templates(key,name,client,version,device,ua,device_id,builtin) "
               "VALUES(?,?,?,?,?,?,?,0) ON CONFLICT(key) DO UPDATE SET "
               "name=excluded.name, client=excluded.client, version=excluded.version, "
               "device=excluded.device, ua=excluded.ua, device_id=excluded.device_id",
               (key, str(payload.get("name") or client)[:40], client, version, device, ua, device_id))
    db.commit()
    return dict(db.execute("SELECT * FROM emby_templates WHERE key=?", (key,)).fetchone())


def delete_template(db, key: str) -> None:
    row = db.execute("SELECT builtin FROM emby_templates WHERE key=?", (key,)).fetchone()
    if not row:
        raise ValueError("模板不存在")
    if row["builtin"]:
        raise ValueError("内置模板不可删除")
    db.execute("DELETE FROM emby_templates WHERE key=?", (key,))
    db.commit()


# ---------------------------------------------------------------- 账号
def _normalize_base(url: str) -> str:
    base = str(url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base, re.I):
        raise ValueError("站点地址需以 http:// 或 https:// 开头")
    if len(base) > 300:
        raise ValueError("站点地址过长")
    return base


def public_account(row) -> dict:
    data = _row_dict(row) or {}
    for secret in ("password", "access_token"):
        data.pop(secret, None)
    data["has_password"] = bool(row["password"]) if row is not None else False
    data["has_api_key"] = bool(row["api_key"]) if row is not None else False
    return data


def list_accounts(db) -> List[dict]:
    rows = db.execute("SELECT * FROM emby_accounts ORDER BY id").fetchall()
    return [public_account(r) for r in rows]


def get_account(db, account_id: int) -> Optional[dict]:
    row = db.execute("SELECT * FROM emby_accounts WHERE id=?", (account_id,)).fetchone()
    return _row_dict(row)


def create_account(db, payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("账号名称不能为空")
    base = _normalize_base(payload.get("base_url"))
    mode = payload.get("mode") if payload.get("mode") in ("play", "probe") else "play"
    template_key = str(payload.get("template_key") or "").strip()
    profile = _profile_from_payload(db, payload, template_key)
    interval = max(0.1, min(60, _as_float(payload.get("interval_days"), 3)))
    cap_mb = max(20, min(1024, _as_int(payload.get("stream_cap_mb"), get_config(db)["cap_mb"])))
    now = _now()
    cur = db.execute(
        "INSERT INTO emby_accounts(name,base_url,username,password,api_key,user_id,access_token,"
        "client,device,version,ua,device_id,template_key,mode,enabled,interval_days,stream_cap_mb,"
        "clean_userdata,notify_mode,next_run_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, base, str(payload.get("username") or "").strip()[:120],
         str(payload.get("password") or ""), str(payload.get("api_key") or "").strip()[:400],
         str(payload.get("user_id") or "").strip()[:120], "",
         profile["client"], profile["device"], profile["version"], profile["ua"], profile["device_id"],
         template_key, mode, 0 if payload.get("enabled") in (0, False, "0") else 1,
         interval, cap_mb, 0 if payload.get("clean_userdata") in (0, False, "0") else 1,
         payload.get("notify_mode") if payload.get("notify_mode") in ("all", "fail", "off") else "fail",
         now if payload.get("enabled") not in (0, False, "0") else 0, now),
    )
    db.commit()
    return public_account(db.execute("SELECT * FROM emby_accounts WHERE id=?", (cur.lastrowid,)).fetchone())


def _profile_from_payload(db, payload: dict, template_key: str) -> dict:
    if template_key:
        row = db.execute("SELECT * FROM emby_templates WHERE key=?", (template_key,)).fetchone()
        if not row:
            raise ValueError("身份模板不存在: %s" % template_key)
        client, version = row["client"], row["version"]
        device, ua = row["device"], row["ua"]
        device_id = row["device_id"] or str(payload.get("device_id") or "").strip() or new_device_id()
    else:
        cfg = get_config(db)["default_profile"]
        client = str(payload.get("client") or cfg["client"]).strip()
        version = str(payload.get("version") or cfg["version"]).strip()
        device = str(payload.get("device") or cfg["device"]).strip()
        ua = str(payload.get("ua") or cfg["ua"]).strip()
        device_id = str(payload.get("device_id") or "").strip() or new_device_id()
    ok, msg = validate_profile(client, version, device, ua, device_id)
    if not ok:
        raise ValueError(msg)
    return {"client": client, "version": version, "device": device, "ua": ua, "device_id": device_id}


def update_account(db, account_id: int, payload: dict) -> dict:
    row = db.execute("SELECT * FROM emby_accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise ValueError("账号不存在")
    changes = {}
    if "name" in payload:
        name = str(payload.get("name") or "").strip()[:60]
        if not name:
            raise ValueError("账号名称不能为空")
        changes["name"] = name
    if "base_url" in payload:
        changes["base_url"] = _normalize_base(payload.get("base_url"))
    for field in ("username", "api_key", "user_id"):
        if field in payload:
            changes[field] = str(payload.get(field) or "").strip()[:400]
    if payload.get("password"):
        changes["password"] = str(payload.get("password"))
        changes["access_token"] = ""      # 换密码后旧 token 作废
    if "mode" in payload and payload.get("mode") in ("play", "probe"):
        changes["mode"] = payload["mode"]
    if "enabled" in payload:
        enabled = 0 if payload.get("enabled") in (0, False, "0") else 1
        changes["enabled"] = enabled
        if enabled and not row["next_run_at"]:
            changes["next_run_at"] = _now()
    if "interval_days" in payload:
        changes["interval_days"] = max(0.1, min(60, _as_float(payload.get("interval_days"), 3)))
    if "stream_cap_mb" in payload:
        changes["stream_cap_mb"] = max(20, min(1024, _as_int(payload.get("stream_cap_mb"), 400)))
    if "clean_userdata" in payload:
        changes["clean_userdata"] = 0 if payload.get("clean_userdata") in (0, False, "0") else 1
    if "notify_mode" in payload and payload.get("notify_mode") in ("all", "fail", "off"):
        changes["notify_mode"] = payload["notify_mode"]

    profile_fields = ("client", "version", "device", "ua", "device_id", "template_key")
    if payload.get("template_key"):
        profile = _profile_from_payload(db, payload, str(payload["template_key"]))
        changes.update(profile)
        changes["template_key"] = str(payload["template_key"])
    elif any(field in payload for field in ("client", "version", "device", "ua", "device_id")):
        client = str(payload.get("client") or row["client"]).strip()
        version = str(payload.get("version") or row["version"]).strip()
        device = str(payload.get("device") or row["device"]).strip()
        ua = str(payload.get("ua") or row["ua"]).strip()
        device_id = str(payload.get("device_id") or row["device_id"]).strip()
        ok, msg = validate_profile(client, version, device, ua, device_id)
        if not ok:
            raise ValueError(msg)
        changes.update({"client": client, "version": version, "device": device, "ua": ua,
                        "device_id": device_id, "template_key": ""})
    if not changes:
        return public_account(row)
    keys = list(changes)
    db.execute("UPDATE emby_accounts SET " + ",".join("%s=?" % k for k in keys) + " WHERE id=?",
               [changes[k] for k in keys] + [account_id])
    db.commit()
    return public_account(db.execute("SELECT * FROM emby_accounts WHERE id=?", (account_id,)).fetchone())


def delete_account(db, account_id: int) -> None:
    db.execute("DELETE FROM emby_jobs WHERE account_id=?", (account_id,))
    db.execute("DELETE FROM emby_accounts WHERE id=?", (account_id,))
    db.commit()


# ---------------------------------------------------------------- 队列
def enqueue(db, account_id: int, kind: str = "manual") -> int:
    row = db.execute("SELECT 1 FROM emby_jobs WHERE account_id=? AND status IN ('waiting','running')",
                     (account_id,)).fetchone()
    if row:
        raise ValueError("该账号已有保活任务在排队或执行")
    cur = db.execute("INSERT INTO emby_jobs(account_id,kind,status,updated_at) VALUES(?,?,'waiting',?)",
                     (account_id, kind if kind in ("manual", "auto", "probe") else "manual", _now()))
    db.commit()
    return int(cur.lastrowid)


def queue_snapshot(db) -> List[dict]:
    rows = db.execute(
        "SELECT j.*, a.name AS account_name FROM emby_jobs j "
        "LEFT JOIN emby_accounts a ON a.id=j.account_id "
        "WHERE j.status IN ('waiting','running') ORDER BY j.id").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["steps"] = _json_list(item.pop("steps_json", "[]"))
        out.append(item)
    return out


def cancel_job(db, job_id: int) -> bool:
    row = db.execute("SELECT status FROM emby_jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row["status"] not in ("waiting", "running"):
        return False
    if row["status"] == "waiting":
        db.execute("DELETE FROM emby_jobs WHERE id=?", (job_id,))       # 还没起跑，直接撤单
        db.commit()
        return True
    db.execute("UPDATE emby_jobs SET status='cancelled', updated_at=? WHERE id=?", (_now(), job_id))
    db.commit()
    return True


def live_snapshot(key: str) -> dict:
    return dict(_LIVE.get(key) or {})


def history(db, account_id: Optional[int] = None, limit: int = 60) -> List[dict]:
    limit = max(1, min(200, _as_int(limit, 60)))
    if account_id:
        rows = db.execute("SELECT * FROM emby_runs WHERE account_id=? ORDER BY id DESC LIMIT ?",
                          (account_id, limit)).fetchall()
    else:
        rows = db.execute("SELECT r.*, a.name AS account_name FROM emby_runs r "
                          "LEFT JOIN emby_accounts a ON a.id=r.account_id ORDER BY r.id DESC LIMIT ?",
                          (limit,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["steps"] = _json_list(item.pop("steps_json", "[]"))
        out.append(item)
    return out


# ---------------------------------------------------------------- 调度
def due_accounts(db) -> List[dict]:
    now = _now()
    rows = db.execute("SELECT * FROM emby_accounts WHERE enabled=1 ORDER BY id").fetchall()
    due = []
    for row in rows:
        if row["retry_at"] and now < row["retry_at"]:
            continue
        if row["circuit_until"] and now < row["circuit_until"]:
            continue
        next_at = row["next_run_at"] or 0
        if next_at and now >= next_at:
            due.append(dict(row))
        elif not next_at and not row["last_run_at"]:
            due.append(dict(row))
    return due


async def cleanup_zombies(db) -> int:
    now = _now()
    rows = db.execute("SELECT * FROM emby_jobs WHERE status='running'").fetchall()
    cleaned = 0
    for row in rows:
        if now - (row["updated_at"] or row["started_at"] or now) <= ZOMBIE_SEC:
            continue
        db.execute("UPDATE emby_jobs SET status='interrupted', updated_at=? WHERE id=?", (now, row["id"]))
        db.commit()
        try:
            await _abort_job(db, row, "执行超时")
        except Exception:
            log.warning("僵尸任务收尾失败 job=%s", row["id"], exc_info=True)
        db.execute("DELETE FROM emby_jobs WHERE id=?", (row["id"],))
        db.commit()
        cleaned += 1
    return cleaned


async def tick(db, key: str, notify=None) -> None:
    """到期入队 + 僵尸清理；真正的启动在 advance()"""
    await cleanup_zombies(db)
    for account in due_accounts(db):
        try:
            enqueue(db, account["id"], "auto")
        except ValueError:
            continue
    await advance(db, key, notify)


async def advance(db, key: str, notify=None) -> None:
    if key in _ADVANCING:
        return
    _ADVANCING.add(key)
    try:
        while True:
            cfg = get_config(db)
            limit = cfg["concurrency"]
            running = db.execute("SELECT account_id FROM emby_jobs WHERE status='running'").fetchall()
            if len(running) >= limit:
                return
            busy = {r["account_id"] for r in running}
            row = db.execute("SELECT * FROM emby_jobs WHERE status='waiting' ORDER BY id").fetchall()
            picked = None
            for item in row:
                if item["account_id"] not in busy:
                    picked = item
                    break
            if not picked:
                return
            db.execute("UPDATE emby_jobs SET status='running', started_at=?, updated_at=? "
                       "WHERE id=? AND status='waiting'", (_now(), _now(), picked["id"]))
            db.commit()
            job_id = int(picked["id"])
            task = asyncio.ensure_future(_run_job(db, key, job_id, notify))
            _TASKS.setdefault(key, {})[job_id] = task
            task.add_done_callback(lambda _t, k=key, j=job_id: _TASKS.get(k, {}).pop(j, None))
    finally:
        _ADVANCING.discard(key)


def cancel_all(key: str) -> None:
    """循环退出时取消本账号所有在跑任务（进程级清理，不做 Stopped）"""
    for task in list(_TASKS.get(key, {}).values()):
        task.cancel()
    _TASKS.pop(key, None)


def _set_live(key: str, **fields) -> None:
    live = _LIVE.setdefault(key, {"running": False, "current": "", "progress": 0,
                                 "steps": [], "item": "", "bytes": 0, "account": "",
                                 "job_id": 0, "started_at": 0})
    live.update(fields)


async def _run_job(db, key: str, job_id: int, notify=None) -> None:
    row = db.execute("SELECT * FROM emby_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return
    account = get_account(db, row["account_id"])
    if not account:
        db.execute("UPDATE emby_jobs SET status='failed', error='账号已被删除' WHERE id=?", (job_id,))
        db.commit()
        return
    cfg = get_config(db)
    _set_live(key, running=True, job_id=job_id, account=account["name"],
              current="准备开始", progress=3, steps=[], item="", bytes=0, started_at=_now())
    try:
        result = await run_keepalive(db, account, job_id, manual=(row["kind"] == "manual"),
                                     probe=(row["kind"] == "probe"),
                                     notify=None, key=key)
    except asyncio.CancelledError:
        raise
    except Exception as error:                     # 兜底：别让异常把队列堵死
        log.warning("观看保号异常 #%s: %s", job_id, type(error).__name__)
        result = {"ok": False, "msg": "执行异常: %s" % type(error).__name__, "steps": [],
                  "reason": "exception", "item": "", "play_sec": 0, "bytes": 0}
    try:
        _finish(db, key, account, job_id, result, cfg, notify)
    finally:
        # 完成后清空实时快照：历史在 emby_runs，保活中心不该一直挂着上一次任务的步骤
        _set_live(key, running=False, current="", progress=0, steps=[], item="",
                  bytes=0, job_id=0, started_at=0)


def _finish(db, key: str, account: dict, job_id: int, result: dict, cfg: dict, notify) -> None:
    now = _now()
    ok = bool(result.get("ok"))
    msg = str(result.get("msg") or "")[:300]
    steps = result.get("steps") or []
    db.execute("INSERT INTO emby_runs(account_id,ok,msg,item,play_sec,bytes,steps_json,finished_at) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (account["id"], 1 if ok else 0, msg, str(result.get("item") or "")[:120],
                _as_int(result.get("play_sec")), _as_int(result.get("bytes")),
                json.dumps(steps, ensure_ascii=False), now))
    if ok:
        next_at = now + int(next_run_offset_ms(account["interval_days"]) / 1000)
        db.execute("UPDATE emby_accounts SET last_run_at=?, last_result='success', last_detail=?, "
                   "next_run_at=?, retry_at=0, fail_count=0, circuit_until=0 WHERE id=?",
                   (now, msg, next_at, account["id"]))
    else:
        fail_count = _as_int(account.get("fail_count")) + 1
        retry_at = now + int(max(0.5, _as_float(cfg.get("retry_hours"), 4)) * 3600)
        circuit_until = 0
        detail = msg
        if fail_count >= 4:
            circuit_until = now + 12 * 3600
            retry_at = 0
            detail = "连续失败 %d 次，已熔断至 %s" % (
                fail_count, time.strftime("%m-%d %H:%M", time.localtime(circuit_until)))
        db.execute("UPDATE emby_accounts SET last_run_at=?, last_result=?, last_detail=?, "
                   "retry_at=?, fail_count=?, circuit_until=?, next_run_at=? WHERE id=?",
                   (now, "circuit_open" if circuit_until else "fail", detail[:300],
                    retry_at, fail_count, circuit_until, circuit_until or account.get("next_run_at") or 0,
                    account["id"]))
    db.execute("DELETE FROM emby_jobs WHERE id=?", (job_id,))
    db.commit()

    mode = account.get("notify_mode") or cfg.get("notify_mode")
    should_notify = mode == "all" or (mode == "fail" and not ok)
    if notify and should_notify and mode != "off":
        try:
            notify("success" if ok else "fail", "观看保号 · %s" % account["name"], msg)
        except Exception:
            log.warning("观看保号通知发送失败", exc_info=True)


# ---------------------------------------------------------------- 执行链路
async def run_keepalive(db, account: dict, job_id: Optional[int] = None,
                        manual: bool = False, probe: bool = False,
                        notify=None, key: str = "") -> dict:
    """执行一次保活/观看；返回 {ok,msg,steps,reason,item,play_sec,bytes}"""
    key = key or str(id(db))
    steps: List[dict] = []
    state = {"item": "", "play_sec": 0, "bytes": 0, "start_ticks": 0, "end_ticks": 0,
             "item_id": "", "media_source_id": "", "play_session_id": "", "cap": 0}

    def step(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"name": name, "ok": bool(ok), "detail": str(detail)[:220]})
        _update_job(db, job_id, steps=steps)
        _set_live(key, current=name, steps=list(steps),
                  progress=min(98, 8 + len(steps) * 9), item=state["item"])

    if httpx is None:
        return {"ok": False, "msg": "缺少 httpx 依赖", "steps": steps, "reason": "exception"}

    profile = profile_of(account)
    base = str(account["base_url"]).rstrip("/")
    headers = build_headers(profile)
    cfg = get_config(db)
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=20.0, pool=10.0)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 trust_env=False) as client:
        # 1) 连通性
        try:
            r = await client.get(base + "/emby/System/Info/Public", headers=headers)
            step("正在打开媒体库", r.status_code == 200, "GET /emby/System/Info/Public → %s" % r.status_code)
            if r.status_code != 200:
                return {"ok": False, "msg": "站点不可达（%s）" % r.status_code, "steps": steps,
                        "reason": "unreachable"}
        except Exception as error:
            step("正在打开媒体库", False, type(error).__name__)
            return {"ok": False, "msg": "站点不可达: %s" % type(error).__name__, "steps": steps,
                    "reason": "unreachable"}

        # 2) 凭据（API Key 优先，其次缓存 token，最后账号密码登录）
        token = str(account.get("api_key") or "").strip()
        user_id = str(account.get("user_id") or "").strip()
        if not token:
            token = str(account.get("access_token") or "").strip()
            if token:
                try:
                    r = await _json_call(client, "GET", _with_key(base + "/emby/Users/Me", token), headers)
                    if r["status"] == 401:
                        token = ""
                except Exception:
                    token = ""
            if not token:
                if not account.get("username"):
                    return {"ok": False, "msg": "未配置 API Key 或账号密码", "steps": steps,
                            "reason": "auth_failed"}
                token, user_id, login_ok, login_detail = await _login(client, base, headers, account)
                step("正在验证账号", login_ok, login_detail)
                if not login_ok:
                    return {"ok": False, "msg": "账号登录失败，未消耗保活间隔", "steps": steps,
                            "reason": "auth_failed"}
                db.execute("UPDATE emby_accounts SET access_token=?, user_id=? WHERE id=?",
                           (token, user_id, account["id"]))
                db.commit()

        if not user_id:
            try:
                r = await _json_call(client, "GET", _with_key(base + "/emby/Users", token), headers)
                items = r["data"] if isinstance(r["data"], list) else []
                if items:
                    user_id = str(items[0].get("Id") or "")
                    db.execute("UPDATE emby_accounts SET user_id=? WHERE id=?", (user_id, account["id"]))
                    db.commit()
            except Exception:
                pass

        if probe or account.get("mode") == "probe":
            step("保活探测", True, "仅探测模式，未执行观看")
            return {"ok": True, "msg": "站点连接正常（探测模式）", "steps": steps,
                    "reason": "", "item": "", "play_sec": 0, "bytes": 0}

        if not user_id:
            return {"ok": False, "msg": "未能获取观看账号 UserId（权限不足或接口异常）",
                    "steps": steps, "reason": "auth_failed"}

        # 3) 取片
        item = await _pick_item(client, base, headers, token, user_id)
        if not item:
            step("正在加载媒体库", False, "媒体库为空或无权访问")
            return {"ok": False, "msg": "媒体库为空或无权访问", "steps": steps, "reason": "no_content"}
        state["item"] = item.get("Name") or "未知条目"
        state["item_id"] = item.get("Id") or ""
        runtime_ticks = _as_int(item.get("RunTimeTicks"))
        step("正在加载媒体库", True, "最新入库：%s" % state["item"])
        step("正在浏览详情页", True, "《%s》 · 片长 %s" % (state["item"], fmt_pos(runtime_ticks)))

        # 4) PlaybackInfo
        info = await _playback_info(client, base, headers, token, user_id, state["item_id"], profile)
        if not info:
            step("正在获取播放地址", False, "PlaybackInfo POST/GET 均失败")
            return {"ok": False, "msg": "无法获取播放链接", "steps": steps, "reason": "session_failed"}
        ms_id = info.get("media_source_id") or ""
        play_session = info.get("play_session_id") or ""
        size_bytes = _as_int(info.get("size"))
        bit_rate = _as_int(info.get("bitrate"))
        cap_mb = min(cfg["cap_mb"], _as_int(account.get("stream_cap_mb"), cfg["cap_mb"]))

        play_sec = play_sec_by_runtime(runtime_ticks)
        start_ticks = int(runtime_ticks * (0.02 + random.random() * 0.08)) if runtime_ticks else \
            (60 + random.randint(0, 240)) * 10000000
        end_ticks = start_ticks + play_sec * 10000000
        if runtime_ticks:
            cap_ticks = int(runtime_ticks * 0.97)
            if start_ticks >= cap_ticks:
                start_ticks = max(0, cap_ticks - min(play_sec, 60) * 10000000)
            if end_ticks > cap_ticks:
                end_ticks = cap_ticks
                play_sec = max(1, int((end_ticks - start_ticks) / 10000000))
        plan, plan_meta = build_plan(start_ticks, end_ticks, play_sec)
        state.update(play_sec=play_sec, start_ticks=start_ticks, end_ticks=end_ticks,
                     media_source_id=ms_id, play_session_id=play_session,
                     cap=cap_bytes(size_bytes, bit_rate, play_sec, cap_mb))
        step("正在获取播放地址", True, "%s · 从 %s 开始播放（本次看 %ss）" % (
            info.get("name") or "默认媒体源", fmt_pos(start_ticks), play_sec))

        # 5) 首段拉流校验：不可播就不上报会话，避免产生虚假观看
        gate_bytes, gate_status = await _pull(client, base, headers, token, state["item_id"],
                                              ms_id or state["item_id"], 0, min(8 * 1024 * 1024, state["cap"]))
        if gate_bytes <= 0:
            step("视频加载中", False, "stream → %s，未取到数据" % gate_status)
            return {"ok": False, "msg": "文件不可播（拉流校验失败），已中止会话上报",
                    "steps": steps, "reason": "not_playable"}
        state["bytes"] = gate_bytes
        step("视频加载中", True, "已缓冲 %.1fMB" % (gate_bytes / 1048576.0))

        # 6) 起播
        body = _session_body(state, user_id, profile, plan[0])
        start_ok = False
        try:
            r = await _json_call(client, "POST", _with_key(base + "/emby/Sessions/Playing", token),
                                 dict(headers, **{"Content-Type": "application/json"}), body)
            start_ok = r["status"] in (200, 204)
            step("开始观看", start_ok, "《%s》 · 从 %s 起播" % (state["item"], fmt_pos(plan[0])))
        except Exception as error:
            step("开始观看", False, type(error).__name__)
        if not start_ok:
            return {"ok": False, "msg": "播放会话建立失败", "steps": steps,
                    "reason": "session_failed", "bytes": state["bytes"]}

        # 起播阶段与进度条动作写进步骤，面板上能看到“网速慢 / 拖进度条”
        if plan_meta.get("buffer_ticks"):
            steps.append({"name": "起播缓冲", "ok": True,
                          "detail": "网络有点慢，前 %d 跳按 45%% 速度加载" % plan_meta["buffer_ticks"]})
        for seek in plan_meta.get("seeks") or []:
            steps.append({"name": "拖动进度条", "ok": True,
                          "detail": "第 %d 跳快进了 %ss" % (seek["tick"], seek["jump"])})

        # 7) 心跳 + 持续拉流（文案按真实播放器走：起播慢、正常观看、卡顿缓冲、拖进度条）
        hb_ok = 0
        hb_fail = 0
        stream_warned = False
        stream_limited = False
        cancelled = False
        buffer_ticks = _as_int(plan_meta.get("buffer_ticks"))
        seek_ticks = {int(s.get("tick") or 0): int(s.get("jump") or 0)
                      for s in (plan_meta.get("seeks") or [])}
        for index, pos in enumerate(plan):
            await asyncio.sleep(HEARTBEAT_SEC)
            if job_id and _job_cancelled(db, job_id):
                cancelled = True
                break
            label = "正常观看中"
            stall = False
            if index < buffer_ticks:
                label = "起播缓冲中，网络有点慢"
            elif index + 1 in seek_ticks:
                label = "拖动进度条 +%ss" % seek_ticks[index + 1]
            if not stream_limited and state["bytes"] < state["cap"]:
                seg = min(SEGMENT_MB * 1024 * 1024, state["cap"] - state["bytes"])
                got, status = await _pull(client, base, headers, token, state["item_id"],
                                          ms_id or state["item_id"], state["bytes"], seg)
                if got > 0:
                    state["bytes"] += got
                elif not stream_warned:
                    stream_warned = True
                    stall = True
                    steps.append({"name": "网络卡顿", "ok": True,
                                  "detail": "拉流中断（HTTP %s），缓冲一下再继续" % status})
                elif index > 2 and random.random() < 0.15:
                    stall = True
                    steps.append({"name": "网络卡顿", "ok": True, "detail": "网络抖动，缓冲一下"})
                if state["bytes"] >= state["cap"]:
                    stream_limited = True
                    steps.append({"name": "拉流达上限", "ok": True,
                                  "detail": "已拉 %.1fMB，停止拉流，心跳继续"
                                            % (state["bytes"] / 1048576.0)})
            if stall and index < len(plan) - 1:
                label = "网络卡顿，正在缓冲…"
                await asyncio.sleep(random.uniform(*STALL_RANGE))
            try:
                r = await _json_call(client, "POST", _with_key(base + "/emby/Sessions/Playing/Progress", token),
                                     dict(headers, **{"Content-Type": "application/json"}),
                                     _session_body(state, user_id, profile, pos))
                if r["status"] in (200, 204):
                    hb_ok += 1
                else:
                    hb_fail += 1
            except Exception:
                hb_fail += 1
            elapsed = (index + 1) * HEARTBEAT_SEC
            _update_job(db, job_id, steps=steps, elapsed=elapsed, bytes_read=state["bytes"])
            _set_live(key,
                      current="%s · 《%s》 %s%s" % (
                          label, state["item"], fmt_pos(pos),
                          " · 已拉流 %.1fMB" % (state["bytes"] / 1048576.0) if state["bytes"] else ""),
                      steps=list(steps), progress=min(96, 50 + (index + 1) * 45 // max(1, len(plan))),
                      item=state["item"], bytes=state["bytes"])
        step("本次观看结束", hb_ok > 0,
             "已看 %ss（心跳 %d/%d）%s" % (play_sec, hb_ok, len(plan),
                                       "，中途被取消" if cancelled else ""))

        # 8) 停止 + 清观看记录（取消/异常也走这里；清记录默认开启，可在账号上关）
        await _stop(client, base, headers, token, state, user_id, profile,
                    step, cancelled, bool(account.get("clean_userdata", 1)))

        # 9) 判定
        if cancelled:
            return {"ok": False, "msg": "任务已被取消（会话已收尾）", "steps": steps,
                    "reason": "cancelled", "item": state["item"], "play_sec": play_sec,
                    "bytes": state["bytes"]}
        if hb_ok == 0:
            return {"ok": False, "msg": "播放心跳全部失败，观看记录未建立", "steps": steps,
                    "reason": "session_failed", "item": state["item"], "play_sec": play_sec,
                    "bytes": state["bytes"]}
        return {"ok": True, "msg": "观看完成（%ss，%d 条心跳，拉流 %.1fMB）" % (
            play_sec, hb_ok, state["bytes"] / 1048576.0), "steps": steps, "reason": "",
            "item": state["item"], "play_sec": play_sec, "bytes": state["bytes"]}


def _session_body(state: dict, user_id: str, profile: dict, pos_ticks: int) -> dict:
    return {
        "ItemId": state["item_id"],
        "MediaSourceId": state["media_source_id"] or state["item_id"],
        "PlaySessionId": state["play_session_id"],
        "UserId": user_id,
        "DeviceId": profile["device_id"],
        "Client": profile["client"],
        "Version": profile["version"],
        "PlayMethod": "DirectStream",
        "CanSeek": True,
        "IsPaused": False,
        "QueueableMediaTypes": ["Video"],
        "PositionTicks": pos_ticks,
        "PlaybackPositionTicks": pos_ticks,
    }


async def _stop(client, base: str, headers: dict, token: str, state: dict, user_id: str,
                profile: dict, step, cancelled: bool, clean: bool) -> None:
    try:
        r = await _json_call(client, "POST", _with_key(base + "/emby/Sessions/Playing/Stopped", token),
                             dict(headers, **{"Content-Type": "application/json"}),
                             _session_body(state, user_id, profile, state["end_ticks"]))
        step("正在退出播放", r["status"] in (200, 204),
             "Stopped → %s%s" % (r["status"], "（任务取消）" if cancelled else ""))
    except Exception as error:
        step("正在退出播放", False, type(error).__name__)
    if not clean:
        return
    # 等 Stopped 异步落库后再清，避免把刚写进去的记录又留下
    await asyncio.sleep(CLEAN_DELAY_SEC)
    try:
        r = await _json_call(client, "POST",
                             _with_key(base + "/emby/Users/%s/Items/%s/UserData"
                                      % (user_id, state["item_id"]), token),
                             dict(headers, **{"Content-Type": "application/json"}),
                             {"Played": False, "PlayCount": 0, "ResumePositionTicks": 0,
                              "PlaybackPositionTicks": 0, "PlayedPercentage": 0})
        step("已清除观看记录", r["status"] in (200, 204),
             "已重置播放状态/播放次数/续播进度（UserData → %s）" % r["status"])
    except Exception as error:
        step("已清除观看记录", False, type(error).__name__)


async def _abort_job(db, job_row, note: str) -> None:
    """自愈/超时：把残留会话补发 Stopped，并尽量清掉观看记录"""
    account = get_account(db, job_row["account_id"])
    if not account:
        return
    profile = profile_of(account)
    base = account["base_url"].rstrip("/")
    headers = build_headers(profile)
    token = str(account.get("api_key") or account.get("access_token") or "")
    state = {"item_id": job_row["item_id"], "media_source_id": job_row["media_source_id"],
             "play_session_id": job_row["play_session_id"], "end_ticks": job_row["end_ticks"]}
    body = _session_body(state, account.get("user_id") or "", profile, state["end_ticks"])
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=20.0),
                                     trust_env=False) as client:
            if not token and account.get("username"):
                token, account["user_id"], _ok, _detail = await _login(client, base, headers, account)
                if not token:
                    return
            await _json_call(client, "POST", _with_key(base + "/emby/Sessions/Playing/Stopped", token),
                             dict(headers, **{"Content-Type": "application/json"}), body)
            if account.get("clean_userdata") and account.get("user_id"):
                await asyncio.sleep(1.2)
                await _json_call(client, "POST",
                                 _with_key(base + "/emby/Users/%s/Items/%s/UserData"
                                          % (account["user_id"], job_row["item_id"]), token),
                                 dict(headers, **{"Content-Type": "application/json"}),
                                 {"Played": False, "PlayCount": 0, "ResumePositionTicks": 0,
                                  "PlaybackPositionTicks": 0, "PlayedPercentage": 0})
    except Exception:
        log.warning("观看保号自愈失败 job=%s", job_row["id"], exc_info=True)


RECOVER_DELAY_RANGE = (300, 600)   # 服务刚起来先等 5~10 分钟，避开重启后的网络抖动


def delay_overdue_accounts(db) -> int:
    """服务重启/升级后，把已经到期的账号往后推 5~10 分钟再跑。

    刚恢复时网络、DNS、上游鉴权都可能还没热，直接跑很容易失败，
    失败一次又要等 retry_hours，反而更慢。延迟会写回 next_run_at，
    所以反复重启也不会把排队越推越远（延迟后就不再是「已到期」）。
    """
    now = _now()
    delayed = 0
    for row in db.execute("SELECT * FROM emby_accounts WHERE enabled=1").fetchall():
        if row["retry_at"] and now < row["retry_at"]:
            continue
        if row["circuit_until"] and now < row["circuit_until"]:
            continue
        next_at = row["next_run_at"] or 0
        if next_at and now >= next_at:
            wait = random.randint(*RECOVER_DELAY_RANGE)
            db.execute("UPDATE emby_accounts SET next_run_at=?, last_detail=? WHERE id=?",
                       (now + wait,
                        "服务刚恢复，延迟 %d 分钟再开始（避开刚启动时的网络波动）" % max(1, wait // 60),
                        row["id"]))
            delayed += 1
    db.commit()
    return delayed


async def startup_recover(db, key: str, notify=None) -> int:
    """进程重启后清理上一次残留的 running 任务，并把到期账号延后 5~10 分钟"""
    rows = db.execute("SELECT * FROM emby_jobs WHERE status='running'").fetchall()
    count = 0
    for row in rows:
        db.execute("UPDATE emby_jobs SET status='interrupted', updated_at=? WHERE id=?",
                   (_now(), row["id"]))
        db.commit()
        try:
            await _abort_job(db, row, "进程重启")
        except Exception:
            pass
        db.execute("INSERT INTO emby_runs(account_id,ok,msg,item,play_sec,bytes,steps_json,finished_at) "
                   "VALUES(?,0,?,?,?,?, '[]', ?)",
                   (row["account_id"], "进程重启，已补发 Stopped 并清理残留会话",
                    row["item_name"], row["play_sec"], row["bytes_read"], _now()))
        db.execute("DELETE FROM emby_jobs WHERE id=?", (row["id"],))
        count += 1
    db.commit()
    delay_overdue_accounts(db)
    return count


# ---------------------------------------------------------------- HTTP 细节
def _with_key(url: str, token: str) -> str:
    if not token:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "api_key=" + token


async def _json_call(client, method: str, url: str, headers: dict, payload=None) -> dict:
    r = await client.request(method, url, headers=headers, json=payload) if payload is not None \
        else await client.request(method, url, headers=headers)
    text = r.text
    data = None
    try:
        data = r.json()
    except Exception:
        data = None
    log.debug("[emby-ka] %s %s → %s", method, redact(url), r.status_code)
    return {"status": r.status_code, "data": data, "text": text}


async def _login(client, base: str, headers: dict, account: dict) -> Tuple[str, str, bool, str]:
    try:
        r = await _json_call(client, "POST", base + "/emby/Users/AuthenticateByName",
                             dict(headers, **{"Content-Type": "application/json"}),
                             {"Username": account.get("username") or "",
                              "Pw": account.get("password") or ""})
        data = r.get("data") or {}
        token = str(data.get("AccessToken") or "")
        user_id = str((data.get("User") or {}).get("Id") or "")
        if token:
            return token, user_id, True, "AuthenticateByName → %s" % r["status"]
        return "", "", False, "AuthenticateByName → %s（账号密码错误或站点拒绝）" % r["status"]
    except Exception as error:
        return "", "", False, "登录异常: %s" % type(error).__name__


async def _pick_item(client, base: str, headers: dict, token: str, user_id: str) -> Optional[dict]:
    url = _with_key(base + "/emby/Items?Recursive=true&IncludeItemTypes=Movie,Episode"
                          "&Limit=1&SortBy=DateCreated&SortOrder=Descending&Fields=RunTimeTicks", token)
    try:
        r = await _json_call(client, "GET", url, headers)
        data = r["data"] or {}
        items = data.get("Items") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if items:
            return items[0]
    except Exception:
        pass
    try:
        r = await _json_call(client, "GET", _with_key(
            base + "/emby/Users/%s/Items/Recents?Limit=1&Fields=RunTimeTicks" % user_id, token), headers)
        data = r["data"] or {}
        items = data.get("Items") if isinstance(data, dict) else []
        return items[0] if items else None
    except Exception:
        return None


async def _playback_info(client, base: str, headers: dict, token: str, user_id: str,
                         item_id: str, profile: dict) -> Optional[dict]:
    payload = {"UserId": user_id, "DeviceId": profile["device_id"], "MediaSources": [],
               "AutoOpenLiveStream": False, "StartTimeTicks": 0}
    data = None
    try:
        r = await _json_call(client, "POST",
                             _with_key(base + "/emby/Items/%s/PlaybackInfo" % item_id, token),
                             dict(headers, **{"Content-Type": "application/json"}), payload)
        data = r.get("data")
    except Exception:
        data = None
    if not data or not (data.get("MediaSources") or data.get("PlaySessionId")):
        try:
            r = await _json_call(client, "GET", _with_key(
                base + "/emby/Items/%s/PlaybackInfo?UserId=%s&AutoOpenLiveStream=true"
                % (item_id, user_id), token), headers)
            data = r.get("data")
        except Exception:
            data = None
    if not data:
        return None
    sources = data.get("MediaSources") or []
    source = sources[0] if sources else {}
    bit_rate = 0
    for stream in (source.get("MediaStreams") or []):
        if str(stream.get("Type") or "").lower() == "video":
            bit_rate = _as_int(stream.get("BitRate")) or bit_rate
            break
    if not bit_rate:
        bit_rate = _as_int(source.get("Bitrate"))
    return {"media_source_id": str(source.get("Id") or ""),
            "play_session_id": str(data.get("PlaySessionId") or ""),
            "name": str(source.get("Name") or ""),
            "size": _as_int(source.get("Size")),
            "bitrate": bit_rate}


async def _pull(client, base: str, headers: dict, token: str, item_id: str, ms_id: str,
                offset: int, size: int) -> Tuple[int, int]:
    """拉一段流（Range 定位），读到量即断；Static=false 失败降级 true"""
    if size <= 0:
        return 0, 0
    for static in ("false", "true"):
        url = _with_key(base + "/emby/Videos/%s/stream?MediaSourceId=%s&Static=%s"
                        % (item_id, ms_id or item_id, static), token)
        try:
            async with client.stream("GET", url, headers=dict(headers, **{
                "Range": "bytes=%d-%d" % (offset, offset + size - 1), "Accept": "*/*"})) as resp:
                if resp.status_code not in (200, 206):
                    continue
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total >= size:
                        break
                return total, resp.status_code
        except Exception:
            continue
    return 0, 0


def _job_cancelled(db, job_id: int) -> bool:
    row = db.execute("SELECT status FROM emby_jobs WHERE id=?", (job_id,)).fetchone()
    return (not row) or row["status"] == "cancelled"


def _update_job(db, job_id: Optional[int], **fields) -> None:
    if not job_id:
        return
    mapping = {"steps": "steps_json", "bytes_read": "bytes_read", "elapsed": "elapsed",
               "item": "item_name", "item_id": "item_id", "media_source_id": "media_source_id",
               "play_session_id": "play_session_id", "start_ticks": "start_ticks",
               "end_ticks": "end_ticks", "play_sec": "play_sec", "cap_bytes": "cap_bytes"}
    pairs = []
    values = []
    for key, value in fields.items():
        column = mapping.get(key)
        if not column:
            continue
        pairs.append("%s=?" % column)
        values.append(json.dumps(value, ensure_ascii=False) if column == "steps_json" else value)
    if not pairs:
        return
    try:
        db.execute("UPDATE emby_jobs SET " + ",".join(pairs) + ", updated_at=? WHERE id=?",
                   values + [_now(), job_id])
        db.commit()
    except Exception:
        pass
