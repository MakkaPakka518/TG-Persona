"""Account-local task/configuration API; deliberately imports no main/TG/AI code.

Integration: app.include_router(build_task_router(get_account_db, AutoIn,
    _valid_auto, _auto_vals, _AUTO_COLS, invalidate_cache,
    _reset_auto_schedule, lambda: set(_auto_running), account_info))
All callbacks are synchronous and resolve the *current request account*.
get_db() returns one sqlite3.Connection with no outstanding transaction;
row_factory need not be configured. auto_columns is a comma-separated string
or a zero-argument getter returning that string (evaluated once per request).
auto_values(model) returns exactly that column order, without extra values.
validate_auto(model) raises HTTPException/ValueError on invalid configuration.
reset_schedule(aid, clear_success=False) must also safely forget DELETED ids;
it must not start tasks. invalidate_cache() only invalidates the current cache.
Schedule/cache callbacks run after the configuration transaction commits.
Callbacks should not raise; failures are reported as warnings, not as a false
rollback. Authentication/CSRF/account selection remain the host app's job.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import re
import sqlite3
import time
from typing import Any, Literal, List, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError
from custom_workflow import canonical_steps, canonical_trigger, parse_steps, uses_credentials

MAX_BYTES = 2 * 1024 * 1024
SAFE_COLUMNS = tuple((
    "type,name,target,command,win_start,win_end,click_btn,btn_text,btn_emoji,click_times,click_interval,"
    "success_kw,fail_kw,points_pat,is_chat,retries,signin_captcha,groups,keywords,action,reply_text,"
    "poll_interval,custom_steps,custom_trigger,chain_retry,cooldown,enabled,command_delay,button_delay,refresh_buttons,icon_url,result_delay,result_timeout,scheduled_at,history_first,"
    "link_mode,link_limit,link_wait,existing_kw,success_reply_enabled,success_reply_text,success_reply_delay,announcement_kw,announcement_button,ai_provider_ids,signin_recognition_mode,signin_image_prompt,signin_image_action,ai_flock_enabled,ai_flock_minutes,ai_flock_prompt,ai_flock_provider_ids,ai_flock_reaction,ai_flock_context_messages,ai_flock_message_chars,ai_flock_skill_chars,ai_flock_max_tokens"
).split(","))
# Credentials are stored per-account in settings, never in task rows/exports.
INTEGER_LIMITS = {
    "click_btn": (0, 1), "is_chat": (0, 1), "signin_captcha": (0, 1),
    "enabled": (0, 1), "refresh_buttons": (0, 1), "click_times": (1, 20),
    "click_interval": (50, 3000), "retries": (0, 5), "chain_retry": (1, 5),
    "cooldown": (0, 86400), "poll_interval": (0, 90 * 86400), "result_timeout": (1, 300),
    "history_first": (0, 1), "link_mode": (0, 1), "link_limit": (2, 4), "link_wait": (2, 15),
    "success_reply_enabled": (0, 1), "success_reply_delay": (0, 300),
    "ai_flock_enabled": (0, 1), "ai_flock_minutes": (1, 60),
    "ai_flock_context_messages": (1, 50), "ai_flock_message_chars": (40, 2000),
    "ai_flock_skill_chars": (0, 200000), "ai_flock_max_tokens": (50, 8192),
}
# Only effective fields are applied; custom windows/delays are NOT scheduled.
APPLICABILITY = {
    **{k: {"signin"} for k in ("win_start", "win_end", "command_delay", "button_delay", "result_delay",
                               "refresh_buttons", "click_btn", "btn_text", "btn_emoji",
                               "click_times", "click_interval")},
    "result_timeout": {"signin", "scheduled_register", "link_register", "button_register", "code_register"},
    "scheduled_at": {"scheduled_register"},
    "history_first": {"scheduled_register"},
    "poll_interval": {"keepalive", "grabcodes"},
    "cooldown": {"keepalive", "grabcodes", "redpacket"},
    "retries": {"signin", "custom", "keepalive"}, "chain_retry": {"custom"},
    "link_mode": {"link_register", "code_register"}, "link_limit": {"link_register", "code_register"}, "link_wait": {"link_register", "code_register"},
    "existing_kw": {"link_register", "button_register", "code_register"}, "success_reply_enabled": {"link_register", "button_register", "code_register"},
    "success_reply_text": {"link_register", "button_register", "code_register"}, "success_reply_delay": {"link_register", "button_register", "code_register"},
    "ai_provider_ids": {"signin"},
    "ai_flock_provider_ids": {"link_register", "button_register", "code_register"},
    "ai_flock_reaction": {"link_register", "button_register", "code_register"},
    **{key: {"link_register", "button_register", "code_register"} for key in
       ("ai_flock_context_messages", "ai_flock_message_chars", "ai_flock_skill_chars", "ai_flock_max_tokens")},
}
RULE_COLUMNS = ("keywords", "regex", "action", "value", "reaction", "groups", "enabled")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BatchRequest(StrictModel):
    ids: List[StrictInt] = Field(min_length=1, max_length=200)
    action: Literal["update", "enable", "disable", "delete"]
    patch: Dict[str, Any] = Field(default_factory=dict)
    preview: StrictBool = False


class ExportRequest(StrictModel):
    ids: List[StrictInt] = Field(min_length=1, max_length=500)


class AccountMetadata(StrictModel):
    id: StrictStr = Field(min_length=1, max_length=128)
    label: StrictStr = Field(max_length=512)


class RuleConfig(StrictModel):
    keywords: StrictStr = Field(min_length=1, max_length=16384)
    regex: StrictInt = Field(default=0, ge=0, le=1)
    action: Literal["reply", "react", "both"] = "reply"
    value: StrictStr = Field(default="", max_length=16384)
    reaction: StrictStr = Field(default="", max_length=128)
    groups: StrictStr = Field(default="", max_length=16384)
    enabled: StrictInt = Field(default=1, ge=0, le=1)


class GroupConfig(StrictModel):
    chat_id: StrictStr = Field(min_length=1, max_length=256)
    name: StrictStr = Field(default="", max_length=512)


class BackupConfig(StrictModel):
    format: Literal["tg-persona-config"]
    schema_version: StrictInt = Field(ge=1, le=1)
    exported_at: Optional[StrictStr] = Field(default=None, max_length=128)
    account: Optional[AccountMetadata] = None
    automations: List[Dict[str, Any]] = Field(max_length=500)
    rules: List[RuleConfig] = Field(default_factory=list, max_length=500)
    watched_groups: List[GroupConfig] = Field(default_factory=list, max_length=2000)


class ImportRequest(StrictModel):
    data: BackupConfig
    mode: Literal["merge", "replace"] = "merge"
    preview: StrictBool = True
    include_rules: StrictBool = True
    include_groups: StrictBool = True
    activate: StrictBool = False


def _bad(message):
    raise HTTPException(422, message)


def _ids(ids):
    if any(i <= 0 for i in ids) or len(set(ids)) != len(ids):
        _bad("ids 必须是互不重复的正整数")
    return ids


def _regex(value):
    if len(value) > 1024:
        _bad("正则表达式最长 1024 字符")
    try:
        re.compile(value)
    except re.error:
        _bad("正则表达式无效")


def _check_fields(data, allowed):
    if set(data) - set(allowed):
        _bad("包含不支持的任务字段（凭据、ID 和执行状态禁止导入/修改）")
    result = dict(data)
    for key, value in result.items():
        if key in INTEGER_LIMITS:
            lo, hi = INTEGER_LIMITS[key]
            if type(value) is not int or not lo <= value <= hi:
                _bad(f"{key} 必须为 {lo}-{hi} 的整数")
        elif key in ("command_delay", "button_delay", "result_delay"):
            if type(value) not in (int, float) or not 0 <= value <= 120 or not math.isfinite(value):
                _bad(f"{key} 必须为 0-120 的有限秒数")
            result[key] = float(value)
        else:
            maximum = 262144 if key in ("custom_steps", "custom_trigger") else 16384
            if not isinstance(value, str) or len(value) > maximum:
                _bad(f"{key} 必须为字符串且不超过 {maximum} 字符")
            if key == "type" and value not in ("signin", "scheduled_register", "grabcodes", "redpacket", "custom", "keepalive", "link_register", "button_register", "code_register"):
                _bad("未知任务类型")
            if key == "action" and value not in ("reply", "send"):
                _bad("任务 action 必须为 reply/send")
            if key == "signin_recognition_mode" and value not in ("", "none", "captcha", "image"):
                _bad("图片处理模式无效")
            if key == "signin_image_action" and value not in ("reply", "click"):
                _bad("图片识别结果动作无效")
            if key in ("win_start", "win_end") and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
                _bad("时间窗口格式需为 HH:MM")
            if key == "scheduled_at" and value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d", value):
                _bad("定时执行时间格式需为 YYYY-MM-DDTHH:MM:SS")
            if key == "points_pat" and value:
                _regex(value)
            if key == "custom_steps" and value.strip():
                try:
                    result[key] = canonical_steps(value)
                except ValueError:
                    _bad("自定义工作流步骤配置无效")
            if key == "custom_trigger" and value.strip():
                try:
                    result[key] = canonical_trigger(value)
                except ValueError:
                    _bad("自定义工作流触发条件无效")
    return result


def _rows(db, query, args=()):
    cur = db.execute(query, args)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, tuple(row))) for row in cur.fetchall()]


@contextmanager
def _transaction(db):
    if db.in_transaction:
        raise HTTPException(409, "数据库已有未提交事务，未修改配置")
    try:
        db.execute("BEGIN IMMEDIATE")
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _fingerprint(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_task_router(get_db, auto_model, validate_auto, auto_values, auto_columns,
                      invalidate_cache, reset_schedule, running_ids, account_info=None,
                      reconcile_groups=None) -> APIRouter:
    router = APIRouter()

    def columns():
        text = auto_columns() if callable(auto_columns) else auto_columns
        if not isinstance(text, str):
            raise ValueError("auto_columns must be a comma-separated string or getter -> str")
        cols = tuple(c.strip() for c in text.split(","))
        fields = set(auto_model.model_fields)
        if (not cols or len(set(cols)) != len(cols) or set(cols) - set(SAFE_COLUMNS)
                or set(cols) != fields):
            raise ValueError("auto_columns must exactly match AutoIn fields and the safe allowlist")
        return cols

    columns()  # Fail closed on an unsafe integration before registering endpoints.

    def metadata():
        return AccountMetadata.model_validate(account_info() if account_info else
                                               {"id": "default", "label": "默认账号"}).model_dump()

    def prepare(data, cols):
        checked = _check_fields(data, cols)
        try:
            model = auto_model.model_validate(checked)
            validate_auto(model)
        except HTTPException:
            raise
        except (ValidationError, ValueError, TypeError):
            _bad("任务配置校验失败")
        values = list(auto_values(model))
        if len(values) != len(cols):
            raise ValueError("auto_values(model) must match auto_columns without extras")
        # Validate normalized callback output, too, and canonicalize legacy step JSON.
        return _check_fields(dict(zip(cols, values)), cols)

    def safe_row(row, cols):
        defaults = auto_model().model_dump()
        return {c: row[c] if row.get(c) is not None else defaults[c] for c in cols}

    def selected(db, ids):
        _ids(ids)
        rows = _rows(db, f"SELECT * FROM automations WHERE id IN ({','.join('?' for _ in ids)})", ids)
        by_id = {r["id"]: r for r in rows}
        missing = [aid for aid in ids if aid not in by_id]
        if missing:
            raise HTTPException(404, {"message": "当前账号任务不存在", "ids": missing})
        return [by_id[aid] for aid in ids]

    def unlocked(ids):
        locked = sorted(set(ids) & set(running_ids()))
        if locked:
            raise HTTPException(409, {"message": "任务正在执行，未修改配置", "ids": locked})

    def after_commit(ids, warnings, clear_success_ids=()):
        clear_success_ids = set(clear_success_ids)
        for aid in dict.fromkeys(ids):
            try:
                reset_schedule(aid, clear_success=aid in clear_success_ids)
            except Exception:
                warnings.append(f"任务 {aid} 配置已保存，但计划重置失败；请刷新计划")
        try:
            invalidate_cache()
        except Exception:
            warnings.append("配置已保存，但当前账号缓存失效失败；请刷新缓存")

    @router.post("/api/autos/batch")
    async def batch(body: BatchRequest):
        db, cols = get_db(), columns()
        rows = selected(db, body.ids)
        unlocked(body.ids)
        if body.action == "update":
            if not body.patch:
                _bad("update 必须提供非空 patch")
            patch = _check_fields(body.patch, set(APPLICABILITY) & set(cols))
        else:
            if body.patch:
                _bad("enable/disable/delete 不接受 patch")
            patch = {}
        updated, skipped, candidates = [], [], []
        for row in rows:
            if body.action == "enable" and row["type"] == "custom":
                try:
                    needs_credential = (uses_credentials(parse_steps(row["custom_steps"]))
                                        or "{credential." in str(row.get("target") or ""))
                except ValueError:
                    needs_credential = True
                if needs_credential:
                    skipped.append({"id": row["id"], "name": row["name"], "type": row["type"],
                                    "reason": "含账号凭据变量，请保存凭据后单独启用"})
                    continue
            applicable = {k: v for k, v in patch.items() if row["type"] in APPLICABILITY[k]}
            ignored = [k for k in patch if k not in applicable]
            if ignored:
                skipped.append({"id": row["id"], "name": row["name"], "type": row["type"],
                                "fields": ignored, "reason": "字段对该任务类型不生效，未应用"})
            if body.action == "update" and not applicable:
                continue
            if body.action in ("enable", "disable"):
                applicable = {"enabled": int(body.action == "enable")}
            if body.action != "delete":
                data = prepare({**safe_row(row, cols), **applicable}, cols)
                applicable = {k: data[k] for k in applicable}
            candidates.append((row["id"], applicable))
            updated.append({"id": row["id"], "name": row["name"], "type": row["type"], "patch": applicable})
        warnings = []
        if not body.preview and candidates:
            try:
                with _transaction(db):
                    unlocked(body.ids)
                    for aid, changes in candidates:
                        if body.action == "delete":
                            db.execute("DELETE FROM automations WHERE id=?", (aid,))
                            db.execute("DELETE FROM settings WHERE key=?", ("link_cred_" + str(aid),))
                            db.execute("DELETE FROM registration_events WHERE task_id=?", (aid,))
                            db.execute("DELETE FROM ai_flock_sessions WHERE source_task_id=?", (aid,))
                        else:
                            # Field identifiers originate solely from APPLICABILITY/enabled.
                            db.execute("UPDATE automations SET " + ",".join(k + "=?" for k in changes) + " WHERE id=?",
                                       [*changes.values(), aid])
            except sqlite3.Error:
                raise HTTPException(409, "数据库写入冲突，批量配置已全部回滚")
            changed_custom = [aid for aid, changes in candidates
                              if set(changes) & {"custom_steps", "custom_trigger"}]
            after_commit([a for a, _ in candidates], warnings, changed_custom)
        return {"ok": True, "preview": body.preview, "action": body.action, "updated": updated,
                "updated_count": len(updated),
                "deleted_count": len(updated) if body.action == "delete" and not body.preview else 0,
                "skipped": skipped, "counts": {"selected": len(rows), "updated": len(updated),
                                               "skipped": len(skipped)}, "warnings": warnings}

    def export(db, cols, ids=None):
        rows = selected(db, ids) if ids is not None else _rows(db, "SELECT * FROM automations ORDER BY id")
        tasks = [_check_fields(safe_row(row, cols), cols) for row in rows]
        rules = [{k: r[k] for k in RULE_COLUMNS} for r in _rows(db, "SELECT * FROM rules ORDER BY id")]
        groups = _rows(db, "SELECT chat_id,name FROM watched_groups ORDER BY chat_id")
        return {"format": "tg-persona-config", "schema_version": 1,
                "exported_at": datetime.now(timezone.utc).isoformat(), "account": metadata(),
                "automations": tasks, "rules": rules, "watched_groups": groups}

    @router.get("/api/tasks/export")
    async def export_all():
        return export(get_db(), columns())

    @router.post("/api/tasks/export")
    async def export_selected(body: ExportRequest):
        return export(get_db(), columns(), body.ids)

    async def read_import(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "导入请求最大 2MB")
            raw.extend(chunk)

        def no_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        try:
            obj = json.loads(raw, object_pairs_hook=no_duplicates,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            return ImportRequest.model_validate(obj)
        except (ValueError, TypeError, RecursionError):
            # Never echo rejected input: it may contain credentials.
            _bad("导入 JSON 格式、字段或数量无效")

    @router.post("/api/tasks/import")
    async def import_config(body: ImportRequest = Depends(read_import)):
        db, cols = get_db(), columns()
        data = body.data
        warnings = []
        source = data.account.model_dump() if data.account else None
        if source and source["id"] != metadata()["id"]:
            warnings.append("来源账号与当前账号不同；仅导入当前账号，不切换账号")
        if data.exported_at is not None:
            try:
                datetime.fromisoformat(data.exported_at.replace("Z", "+00:00"))
            except ValueError:
                _bad("exported_at 必须为 ISO 8601 时间")
        tasks = []
        for task in data.automations:
            if body.activate:
                tasks.append(prepare(task, cols))
                continue
            requested_enabled = task.get("enabled", auto_model().enabled)
            if type(requested_enabled) is not int or requested_enabled not in (0, 1):
                _bad("enabled 必须为 0-1 的整数")
            normalized = prepare({**task, "enabled": 0, "ai_flock_enabled": 0}, cols)
            normalized["enabled"] = requested_enabled
            normalized["ai_flock_enabled"] = 0
            tasks.append(normalized)
        rules = []
        for rule in data.rules:
            item = rule.model_dump()
            for key in ("keywords", "value", "reaction", "groups"):
                item[key] = item[key].strip()
            if not item["keywords"] or not item["value"]:
                _bad("规则关键词和回复/表情内容不能为空")
            if item["regex"]:
                _regex(item["keywords"])
            if item["action"] == "react" and len(item["value"].replace("\ufe0f", "")) > 8:
                _bad("react 规则只能填写一个 emoji")
            rules.append(item)
        groups = [{"chat_id": g.chat_id.strip(), "name": g.name.strip()} for g in data.watched_groups]
        if any(not g["chat_id"] for g in groups):
            _bad("chat_id 不能为空")
        if len({g["chat_id"] for g in groups}) != len(groups):
            _bad("导入 watched_groups 的 chat_id 不得重复")
        if not body.include_rules and rules:
            warnings.append("未勾选规则：规则已校验但不会修改")
        if not body.include_groups and groups:
            warnings.append("未勾选监听群：监听群已校验但不会修改")
        old = _rows(db, "SELECT * FROM automations ORDER BY id")
        if body.mode == "replace":
            # Any reserved id blocks replacement, even if its row disappeared.
            unlocked(set(running_ids()) | {r["id"] for r in old})
        seen_tasks = {_fingerprint(_check_fields(safe_row(r, cols), cols)) for r in old} if body.mode == "merge" else set()
        seen_rules = {_fingerprint({k: r[k] for k in RULE_COLUMNS}) for r in _rows(db, "SELECT * FROM rules")} if body.mode == "merge" else set()
        seen_groups = {g["chat_id"] for g in _rows(db, "SELECT chat_id FROM watched_groups")} if body.mode == "merge" else set()
        if not body.activate and (tasks or (body.include_rules and rules)):
            warnings.append("安全默认：新任务和新规则将禁用，导入后待手动启用；activate:true 可恢复备份启用状态")
        summary, pending_tasks, pending_rules, pending_groups = [], [], [], []
        for task in tasks:
            fingerprint = _fingerprint(task)
            saved = task if body.activate else {**task, "enabled": 0}
            if (saved["type"] == "custom" and saved["enabled"] and
                    (uses_credentials(parse_steps(saved["custom_steps"]))
                     or "{credential." in saved["target"])):
                saved = {**saved, "enabled": 0}
                warnings.append(f"自定义任务 {saved['name']} 使用账号凭据变量；凭据不随配置导入，已保持停用")
            saved_fingerprint = _fingerprint(saved)
            # Compare original configuration BEFORE applying safe disabled defaults;
            # also recognize a previous disabled import of this exact backup.
            duplicate = fingerprint in seen_tasks or saved_fingerprint in seen_tasks
            summary.append({"name": task["name"], "type": task["type"], "enabled": saved["enabled"],
                            "status": "skipped_duplicate" if duplicate else "add"})
            if not duplicate:
                pending_tasks.append(saved)
                seen_tasks.update((fingerprint, saved_fingerprint))
        for rule in rules if body.include_rules else []:
            fingerprint = _fingerprint(rule)
            saved = rule if body.activate else {**rule, "enabled": 0}
            saved_fingerprint = _fingerprint(saved)
            if fingerprint not in seen_rules and saved_fingerprint not in seen_rules:
                pending_rules.append(saved)
                seen_rules.update((fingerprint, saved_fingerprint))
        for group in groups if body.include_groups else []:
            if group["chat_id"] not in seen_groups:
                pending_groups.append(group)
                seen_groups.add(group["chat_id"])
            else:
                warnings.append(f"监听群 {group['chat_id']} 已存在，保留当前名称")
        changes = {"tasks": {"add": len(pending_tasks), "skip": len(tasks) - len(pending_tasks),
                            "remove": len(old) if body.mode == "replace" else 0},
                  "rules": {"add": len(pending_rules), "skip": len(rules) - len(pending_rules),
                            "remove": len(_rows(db, "SELECT id FROM rules")) if body.mode == "replace" and body.include_rules else 0},
                  "watched_groups": {"add": len(pending_groups), "skip": len(groups) - len(pending_groups),
                                     "remove": len(_rows(db, "SELECT chat_id FROM watched_groups")) if body.mode == "replace" and body.include_groups else 0}}
        new_ids = []
        if not body.preview:
            try:
                with _transaction(db):
                    if body.mode == "replace":
                        unlocked(set(running_ids()) | {r["id"] for r in old})
                        db.execute("DELETE FROM automations")
                        db.execute("DELETE FROM registration_events")
                        db.execute("DELETE FROM ai_flock_sessions")
                        db.execute("DELETE FROM settings WHERE key LIKE 'link_cred_%'")
                        if body.include_rules:
                            db.execute("DELETE FROM rules")
                        if body.include_groups:
                            db.execute("DELETE FROM watched_groups")
                    now = int(time.time())
                    for task in pending_tasks:
                        cur = db.execute(f"INSERT INTO automations({','.join(cols)},created_at) VALUES({','.join('?' for _ in range(len(cols) + 1))})",
                                         [*(task[c] for c in cols), now])
                        new_ids.append(cur.lastrowid)
                    for rule in pending_rules:
                        db.execute(f"INSERT INTO rules({','.join(RULE_COLUMNS)},created_at) VALUES(?,?,?,?,?,?,?,?)",
                                   [*(rule[c] for c in RULE_COLUMNS), now])
                    for group in pending_groups:
                        db.execute("INSERT INTO watched_groups(chat_id,name,added_at) VALUES(?,?,?)",
                                   (group["chat_id"], group["name"], now))
                    if reconcile_groups is not None and body.mode == "replace" and body.include_groups:
                        reconcile_groups(db)
            except sqlite3.Error:
                raise HTTPException(409, "数据库写入冲突，导入已全部回滚")
            after_commit(([r["id"] for r in old] if body.mode == "replace" else []) + new_ids, warnings)
        counts = {"tasks": len(pending_tasks), "rules": len(pending_rules), "groups": len(pending_groups),
                  "duplicates": len(tasks) - len(pending_tasks)
                                + (len(rules) - len(pending_rules) if body.include_rules else 0)
                                + (len(groups) - len(pending_groups) if body.include_groups else 0)}
        added = {key: (0 if body.preview else counts[key]) for key in ("tasks", "rules", "groups")}
        skipped = {"tasks": changes["tasks"]["skip"], "rules": changes["rules"]["skip"],
                   "groups": changes["watched_groups"]["skip"]}
        return {"ok": True, "preview": body.preview, "mode": body.mode, "activate": body.activate,
                "counts": counts, "changes": changes, "added": added, "skipped": skipped,
                "warnings": warnings, "tasks": summary, "ids": new_ids}

    return router
