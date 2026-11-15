"""Account-local durable execution, memory history and AI measurements."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import math
import time
import uuid
import zlib

execution = ContextVar("persona_execution", default=None)
memory_change = ContextVar("persona_memory_change", default={})


def init(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS execution_runs (
        id TEXT PRIMARY KEY, task_id INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
        chat_id TEXT NOT NULL DEFAULT '', started REAL NOT NULL, finished REAL,
        status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}');
    CREATE INDEX IF NOT EXISTS execution_runs_task ON execution_runs(task_id,started DESC);
    CREATE TABLE IF NOT EXISTS execution_events (
        id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, ts REAL NOT NULL,
        stage TEXT NOT NULL, detail TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS execution_events_run ON execution_events(run_id,id);
    CREATE TABLE IF NOT EXISTS pending_deletions (
        chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, due REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(chat_id,message_id));
    CREATE TABLE IF NOT EXISTS reply_deliveries (
        chat_id TEXT NOT NULL, digest TEXT NOT NULL, ts REAL NOT NULL,
        status TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(chat_id,digest));
    CREATE TABLE IF NOT EXISTS workflow_checkpoints (
        task_id INTEGER PRIMARY KEY, signature TEXT NOT NULL, step INTEGER NOT NULL,
        state TEXT NOT NULL, context TEXT NOT NULL, updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS memory_versions (
        id INTEGER PRIMARY KEY, file TEXT NOT NULL, created REAL NOT NULL, reason TEXT NOT NULL,
        digest TEXT NOT NULL, content BLOB NOT NULL, source TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS memory_versions_file ON memory_versions(file,id DESC);
    CREATE TABLE IF NOT EXISTS memory_conflicts (
        id INTEGER PRIMARY KEY, created REAL NOT NULL, old_body TEXT NOT NULL,
        new_body TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
    CREATE TABLE IF NOT EXISTS replay_samples (
        id INTEGER PRIMARY KEY, created REAL NOT NULL, task_id INTEGER NOT NULL,
        name TEXT NOT NULL, config TEXT NOT NULL, snapshot TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS replay_results (
        id INTEGER PRIMARY KEY, sample_id INTEGER NOT NULL, created REAL NOT NULL,
        config TEXT NOT NULL, result TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ai_measurements (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, provider_id TEXT NOT NULL, model TEXT NOT NULL,
        feature TEXT NOT NULL, task_id INTEGER NOT NULL, chat_id TEXT NOT NULL,
        task_kind TEXT NOT NULL DEFAULT 'automation',
        elapsed_ms INTEGER NOT NULL, ok INTEGER NOT NULL, input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL, cached_tokens INTEGER NOT NULL,
        cost REAL, currency TEXT NOT NULL, usage_known INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS ai_measurements_time ON ai_measurements(ts,provider_id);
    CREATE TABLE IF NOT EXISTS provider_profiles (
        provider_id TEXT PRIMARY KEY, profile TEXT NOT NULL, updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS summary_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
        groups TEXT NOT NULL, at TEXT NOT NULL, hours INTEGER NOT NULL,
        message_limit INTEGER NOT NULL, topics TEXT NOT NULL, provider_ids TEXT NOT NULL,
        destination TEXT NOT NULL DEFAULT 'me', updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS summary_runs (
        job_id INTEGER NOT NULL, day TEXT NOT NULL, chat_id TEXT NOT NULL,
        status TEXT NOT NULL, updated REAL NOT NULL, digest TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '', PRIMARY KEY(job_id,day,chat_id));
    CREATE TRIGGER IF NOT EXISTS persona_task_deleted AFTER DELETE ON automations BEGIN
        DELETE FROM workflow_checkpoints WHERE task_id=OLD.id;
        DELETE FROM replay_results WHERE sample_id IN (SELECT id FROM replay_samples WHERE task_id=OLD.id);
        DELETE FROM replay_samples WHERE task_id=OLD.id;
    END;
    """)
    if "task_kind" not in {row[1] for row in db.execute("PRAGMA table_info(ai_measurements)")}:
        db.execute("ALTER TABLE ai_measurements ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'automation'")
    db.execute("UPDATE execution_runs SET status='interrupted',finished=? WHERE status IN ('running','queued')", (time.time(),))
    db.execute("UPDATE summary_runs SET status='unknown',error='发送时服务中断，请核对收藏夹' WHERE status='sending'")
    db.execute("UPDATE summary_runs SET status='failed',error='生成时服务中断，可手动重试' WHERE status='running'")
    db.commit()


def json_text(value):
    """Bound individual values without ever truncating serialized JSON."""
    remaining = 60000
    def clean(item, depth=0):
        nonlocal remaining
        if remaining <= 0:
            return "[内容已省略]"
        remaining -= 16
        if depth > 8:
            return "[层级过深]"
        if isinstance(item, dict):
            return {str(k)[:100]: clean(v, depth + 1) for k, v in list(item.items())[:80]}
        if isinstance(item, (list, tuple)):
            return [clean(v, depth + 1) for v in item[:100]]
        if isinstance(item, str):
            result = item[:min(12000, max(0, remaining))]
            remaining -= len(result)
            return result
        if isinstance(item, float) and not math.isfinite(item):
            return None
        return item if item is None or isinstance(item, (int, float, bool)) else str(item)[:1000]
    return json.dumps(clean(value), ensure_ascii=False, allow_nan=False)


def start_run(db, task_id=0, kind="custom", name="", chat_id="", status="running"):
    run = uuid.uuid4().hex
    db.execute("INSERT INTO execution_runs(id,task_id,kind,name,chat_id,started,status) VALUES(?,?,?,?,?,?,?)",
               (run, task_id, kind, name, str(chat_id), time.time(), status))
    db.commit()
    return run


def event(db, stage, detail=None, run_id=None):
    active = execution.get() or {}
    run_id = run_id or active.get("run_id")
    if not run_id:
        return
    db.execute("INSERT INTO execution_events(run_id,ts,stage,detail) VALUES(?,?,?,?)",
               (run_id, time.time(), stage, json_text(detail or {})))
    db.commit()


def finish_run(db, run_id, status, detail=None):
    db.execute("UPDATE execution_runs SET status=?,finished=?,detail=? WHERE id=?",
               (status, time.time(), json_text(detail or {}), run_id))
    event(db, status, detail, run_id)


@contextmanager
def scope(**values):
    token = execution.set({**(execution.get() or {}), **values})
    try:
        yield
    finally:
        execution.reset(token)


def digest(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def remember(db, file, content, reason="write", source=None):
    previous = db.execute("SELECT digest FROM memory_versions WHERE file=? ORDER BY id DESC LIMIT 1", (file,)).fetchone()
    hashed = digest(content)
    if previous and previous[0] == hashed:
        return
    change = memory_change.get()
    db.execute("INSERT INTO memory_versions(file,created,reason,digest,content,source) VALUES(?,?,?,?,?,?)",
               (file, time.time(), change.get("reason", reason), hashed,
                zlib.compress(content.encode("utf-8")), json_text(source or change.get("source", {}))))
    db.execute("DELETE FROM memory_versions WHERE file=? AND id NOT IN "
               "(SELECT id FROM memory_versions WHERE file=? ORDER BY id DESC LIMIT 30)", (file, file))
    db.commit()


def conflict(db, old, new, source):
    if not db.execute("SELECT 1 FROM memory_conflicts WHERE old_body=? AND new_body=? AND status='pending'", (old, new)).fetchone():
        db.execute("INSERT INTO memory_conflicts(created,old_body,new_body,source) VALUES(?,?,?,?)",
                   (time.time(), old, new, json_text(source)))
        db.execute("DELETE FROM memory_conflicts WHERE id NOT IN (SELECT id FROM memory_conflicts ORDER BY id DESC LIMIT 500)")
        db.commit()


def reserve_reply(db, chat_id, text, seconds=60):
    key, now = digest(text), time.time()
    db.execute("DELETE FROM reply_deliveries WHERE ts<?", (now - 86400,))
    row = db.execute("SELECT ts,status FROM reply_deliveries WHERE chat_id=? AND digest=?", (str(chat_id), key)).fetchone()
    if row and row[0] >= now - seconds and row[1] != "failed":
        db.commit()
        return False
    db.execute("INSERT INTO reply_deliveries VALUES(?,?,?,'pending',0) ON CONFLICT(chat_id,digest) "
               "DO UPDATE SET ts=excluded.ts,status='pending',sent=0", (str(chat_id), key, now))
    db.commit()
    return True


def finish_reply(db, chat_id, text, status, sent):
    db.execute("UPDATE reply_deliveries SET status=?,sent=? WHERE chat_id=? AND digest=?",
               (status, sent, str(chat_id), digest(text)))
    db.commit()


def save_checkpoint(db, task_id, signature, step, state, context, credential=None):
    # Credentials are reloaded from their existing store, never copied into history.
    def hide(value, field=""):
        if isinstance(value, dict):
            return {(hide(key) if field in ("watermarks", "baselines", "reply_ids") else key): hide(item, key)
                    for key, item in value.items() if key != "credential"}
        if isinstance(value, (list, tuple)):
            return [hide(item) for item in value]
        if isinstance(value, str):
            for key, secret in sorted((credential or {}).items(), key=lambda pair: len(pair[1]), reverse=True):
                if secret:
                    value = value.replace(secret, "{stored-credential:" + key + "}")
        return value
    db.execute("INSERT OR REPLACE INTO workflow_checkpoints VALUES(?,?,?,?,?,?)",
               (task_id, signature, step, state, json.dumps(hide(context), ensure_ascii=False), time.time()))
    db.commit()


def load_checkpoint(db, task_id, credential=None):
    row = db.execute("SELECT * FROM workflow_checkpoints WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return None
    def restore(value):
        if isinstance(value, dict):
            return {restore(key): restore(item) for key, item in value.items()}
        if isinstance(value, list):
            return [restore(item) for item in value]
        if isinstance(value, str):
            for key, secret in (credential or {}).items():
                value = value.replace("{stored-credential:" + key + "}", secret)
        return value
    return {**dict(row), "context": restore(json.loads(row["context"]))}


def profile(db, provider_id):
    row = db.execute("SELECT profile FROM provider_profiles WHERE provider_id=?", (provider_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def measure(db, cfg, feature, elapsed_ms, ok, usage, pricing):
    usage = usage if isinstance(usage, dict) else {}
    def token(name, alternate):
        try:
            return max(0, int(usage.get(name, usage.get(alternate, 0)) or 0))
        except (ValueError, TypeError, OverflowError):
            return 0
    incoming, outgoing = token("prompt_tokens", "input_tokens"), token("completion_tokens", "output_tokens")
    details = usage.get("prompt_tokens_details", usage.get("input_tokens_details", {})) or {}
    try:
        cached = min(incoming, max(0, int(details.get("cached_tokens", 0))))
    except (TypeError, ValueError, OverflowError, AttributeError):
        cached = 0
    known = any(k in usage for k in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens"))
    cost = None
    if known and pricing.get("input_price") is not None and pricing.get("output_price") is not None:
        cache_price = pricing.get("cached_price")
        cache_price = pricing["input_price"] if cache_price is None else cache_price
        cost = ((incoming - cached) * pricing["input_price"] + cached * cache_price + outgoing * pricing["output_price"]) / 1e6
    context = execution.get() or {}
    db.execute("INSERT INTO ai_measurements(ts,provider_id,model,feature,task_id,chat_id,elapsed_ms,ok,"
               "input_tokens,output_tokens,cached_tokens,cost,currency,usage_known,task_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (time.time(), cfg.get("id", ""), cfg.get("model", ""), feature, context.get("task_id", 0),
                str(context.get("chat_id", "")), elapsed_ms, int(ok), incoming, outgoing, cached,
                 cost, pricing.get("currency", "CNY"), int(known), context.get("task_kind", "automation")))
    db.commit()


def cleanup(db, days=30):
    cutoff = time.time() - max(1, days) * 86400
    db.execute("DELETE FROM execution_runs WHERE started<?", (cutoff,))
    db.execute("DELETE FROM execution_events WHERE run_id NOT IN (SELECT id FROM execution_runs)")
    db.execute("DELETE FROM ai_measurements WHERE ts<?", (time.time() - 90 * 86400,))
    db.execute("DELETE FROM summary_runs WHERE updated<?", (time.time() - 90 * 86400,))
    db.execute("DELETE FROM workflow_checkpoints WHERE task_id NOT IN (SELECT id FROM automations)")
    db.commit()
