"""Offline module tests: temporary per-account SQLite + fake callbacks only.

Only AutoIn/_valid_auto/_auto_vals/_AUTO_COLS AST nodes are loaded from source;
main is NEVER imported or initialized. No real data, credentials, TG or AI.
Run: python3 -m unittest test_suite.test_task_management -v
"""
import ast
from contextvars import ContextVar
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from task_management import build_task_router, MAX_BYTES, RULE_COLUMNS


def load_model_callbacks():
    def win_minutes(value):
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            return None
        hour, minute = map(int, value.split(":"))
        return hour * 60 + minute
    source = ast.parse((ROOT / "main.py").read_text())
    wanted = {"AutoIn", "_valid_auto", "_auto_vals"}
    nodes = [node for node in source.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in wanted]
    nodes += [node for node in source.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "_AUTO_COLS" for target in node.targets)]
    namespace = {"BaseModel": BaseModel, "Field": Field, "HTTPException": HTTPException,
                 "math": math, "json": json, "re": re, "_win_minutes": win_minutes}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<safe-model-fixture>", "exec"), namespace)
    return namespace


FIXTURE = load_model_callbacks()
Auto = FIXTURE["AutoIn"]
COLS = FIXTURE["_AUTO_COLS"]
CURRENT = ContextVar("task_test_account", default="default")


class TaskManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tg-task-management-")
        self.dbs, self.running = {}, {"default": set(), "other": set()}
        self.resets, self.invalidations, self.reads, self.validated = [], [], [], []
        for account in ("default", "other"):
            path = Path(self.tmp.name) / ("root.sqlite3" if account == "default" else "accounts/other/account.sqlite3")
            path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(path, check_same_thread=False)
            fields = ",".join(c + (" REAL" if c in ("command_delay", "button_delay", "result_delay") else
                                   " INTEGER" if isinstance(getattr(Auto(), c), int) else " TEXT")
                              for c in COLS.split(","))
            db.executescript("CREATE TABLE automations(id INTEGER PRIMARY KEY AUTOINCREMENT," + fields + ","
                             "last_at TEXT DEFAULT '',last_attempt INTEGER DEFAULT 0,retry_count INTEGER DEFAULT 0,"
                             "retry_day TEXT DEFAULT '',next_run_at INTEGER DEFAULT 0,last_status TEXT DEFAULT '',created_at INTEGER);"
                             "CREATE TABLE rules(id INTEGER PRIMARY KEY AUTOINCREMENT,keywords TEXT,regex INTEGER,"
                             "action TEXT,value TEXT,reaction TEXT,groups TEXT,enabled INTEGER,created_at INTEGER);"
                             "CREATE TABLE watched_groups(chat_id TEXT PRIMARY KEY,name TEXT,added_at INTEGER);"
                             "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);"
                             "CREATE TABLE hits(id INTEGER PRIMARY KEY,content TEXT);")
            db.execute("INSERT INTO settings VALUES(?,?)", ("api_key", "FAKE-DO-NOT-EXPORT"))
            db.execute("INSERT INTO hits VALUES(?,?)", (1, "FAKE-HISTORY"))
            db.commit()
            self.dbs[account] = db
        self.app = FastAPI()

        @self.app.middleware("http")
        async def choose_account(request, call_next):
            token = CURRENT.set(request.headers.get("x-test-account", "default"))
            try:
                return await call_next(request)
            finally:
                CURRENT.reset(token)

        self.router = build_task_router(self.get_db, Auto, self.validate, FIXTURE["_auto_vals"],
                                        lambda: COLS, self.invalidate, self.reset,
                                        lambda: self.running[CURRENT.get()],
                                        lambda: {"id": CURRENT.get(), "label": "Fixture " + CURRENT.get()})
        self.app.include_router(self.router)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        for db in self.dbs.values():
            db.close()
        self.tmp.cleanup()

    def get_db(self):
        self.reads.append(CURRENT.get())
        return self.dbs[CURRENT.get()]

    def validate(self, model):
        self.validated.append(model.name)
        FIXTURE["_valid_auto"](model)

    def reset(self, aid, clear_success=False):
        account = CURRENT.get()
        self.resets.append((account, aid, clear_success))
        # Deleted ids safely clear memory and do not index a nonexistent row.
        db = self.dbs[account]
        db.execute("UPDATE automations SET last_attempt=0,retry_count=0,retry_day='',next_run_at=0,last_status='' WHERE id=?", (aid,))
        if clear_success:
            db.execute("UPDATE automations SET last_at='' WHERE id=?", (aid,))
        db.commit()

    def invalidate(self):
        self.invalidations.append(CURRENT.get())

    def add(self, account="default", **patch):
        body = {"name": "fixture", "target": "@fake", "command": "/sign", **patch}
        model = Auto(**body)
        values = FIXTURE["_auto_vals"](model)
        db = self.dbs[account]
        cur = db.execute(f"INSERT INTO automations({COLS},created_at) VALUES({','.join('?' for _ in range(len(values) + 1))})", [*values, 123])
        db.commit()
        return cur.lastrowid

    def row(self, aid, account="default"):
        cursor = self.dbs[account].execute("SELECT * FROM automations WHERE id=?", (aid,))
        row = cursor.fetchone()
        return dict(zip([c[0] for c in cursor.description], row)) if row else None

    def backup(self, tasks=None, **extra):
        return {"format": "tg-persona-config", "schema_version": 1,
                "automations": tasks if tasks is not None else [{"name": "imported", "target": "@fake", "command": "/sign"}],
                "rules": [{"keywords": "open", "value": "hello", "groups": "-100fake"}],
                "watched_groups": [{"chat_id": "-100fake", "name": "Fixture group"}], **extra}

    def imp(self, data=None, account="default", **options):
        return self.client.post("/api/tasks/import", json={"data": data if data is not None else self.backup(), **options},
                                headers={"x-test-account": account})

    def batch(self, ids, action="update", patch=None, account="default", **extra):
        return self.client.post("/api/autos/batch", json={"ids": ids, "action": action, "patch": patch or {}, **extra},
                                headers={"x-test-account": account})

    def snapshot(self, account="default"):
        return tuple(tuple(self.dbs[account].execute("SELECT * FROM " + table + " ORDER BY 1"))
                     for table in ("automations", "rules", "watched_groups", "settings", "hits"))

    def test_batch_preview_is_read_only(self):
        aid = self.add()
        before = self.snapshot()
        response = self.batch([aid], patch={"win_start": "09:00", "command_delay": 0}, preview=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["updated"][0]["patch"], {"win_start": "09:00", "command_delay": 0})
        self.assertEqual(before, self.snapshot())
        self.assertEqual((self.resets, self.invalidations), ([], []))

    def test_batch_default_apply_preserves_success(self):
        aid = self.add()
        db = self.dbs["default"]
        db.execute("UPDATE automations SET last_at='2026-10-30',retry_count=3,last_status='failed' WHERE id=?", (aid,))
        db.commit()
        response = self.batch([aid], patch={"win_start": "09:00", "command_delay": 0, "button_delay": 3.5, "refresh_buttons": 0})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["preview"])
        self.assertEqual(self.row(aid)["last_at"], "2026-10-30")
        self.assertEqual(self.row(aid)["win_start"], "09:00")
        self.assertEqual(self.row(aid)["retry_count"], 0)
        self.assertEqual(self.resets, [("default", aid, False)])
        self.assertEqual(self.invalidations, ["default"])

    def test_batch_validates_every_candidate_before_write(self):
        first = self.add(name="early", win_start="06:00", win_end="08:00")
        second = self.add(name="late", win_start="09:00", win_end="12:00")
        before = self.snapshot()
        response = self.batch([first, second], patch={"win_end": "08:30"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.validated, ["early", "late"])
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.resets)

    def test_batch_sql_error_rolls_back_all_rows(self):
        first, second = self.add(name="one"), self.add(name="two")
        self.dbs["default"].execute(f"CREATE TRIGGER block_second BEFORE UPDATE ON automations WHEN OLD.id={second} BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        self.dbs["default"].commit()
        before = self.snapshot()
        response = self.batch([first, second], patch={"win_start": "09:00"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.resets)

    def test_mixed_types_skip_ineffective_fields(self):
        signin = self.add()
        custom = self.add(type="custom", custom_steps='[{"t":"send","v":"/start"}]')
        keepalive = self.add(type="keepalive", reply_text="hello", poll_interval=120)
        grabcodes = self.add(type="grabcodes", reply_text="{code}", poll_interval=20)
        response = self.batch([signin, custom, keepalive, grabcodes], patch={"win_start": "09:00", "poll_interval": 300, "button_delay": 0})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["counts"]["updated"], 3)
        self.assertEqual(len(response.json()["skipped"]), 4)
        self.assertEqual(self.row(custom)["win_start"], "08:00")
        self.assertEqual(self.row(custom)["button_delay"], 2)
        self.assertEqual(self.row(keepalive)["poll_interval"], 300)
        self.assertEqual(self.row(grabcodes)["poll_interval"], 300)
        self.assertNotIn(("default", custom, False), self.resets)

    def test_all_inapplicable_is_noop(self):
        aid = self.add(type="custom", custom_steps='[{"t":"send","v":"hi"}]')
        before = self.snapshot()
        response = self.batch([aid], patch={"win_start": "09:00"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated"], [])
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.invalidations)

    def test_unknown_and_foreign_ids_are_404(self):
        aid = self.add()
        self.add("other", name="other-only")
        foreign = self.add("other", name="foreign")
        before = self.snapshot()
        for ids in ([aid, 999], [foreign]):
            response = self.batch(ids, action="delete")
            self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(before, self.snapshot())
        self.assertIsNotNone(self.row(foreign, "other"))

    def test_running_locks_every_batch_action_and_preview(self):
        aid = self.add()
        self.running["default"].add(aid)
        for action in ("update", "enable", "disable", "delete"):
            for preview in (True, False):
                response = self.batch([aid], action=action, patch={"win_start": "09:00"} if action == "update" else {}, preview=preview)
                self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNotNone(self.row(aid))
        self.assertFalse(self.resets)

    def test_enable_disable_delete_only_current_account(self):
        aid = self.add(enabled=0)
        self.add("other", enabled=0)
        self.running["other"].add(aid)
        self.assertEqual(self.batch([aid], action="enable").status_code, 200)
        self.assertEqual(self.row(aid)["enabled"], 1)
        self.assertEqual(self.batch([aid], action="disable").status_code, 200)
        self.assertEqual(self.row(aid)["enabled"], 0)
        self.assertEqual(self.batch([aid], action="delete").status_code, 200)
        self.assertIsNone(self.row(aid))
        self.assertEqual(self.row(aid, "other")["enabled"], 0)
        self.assertTrue(all(account == "default" and not clear for account, _, clear in self.resets))

    def test_ids_and_patch_are_strict(self):
        aid = self.add()
        for ids in ([], [0], [-1], [True], ["1"], [aid, aid], list(range(1, 202))):
            self.assertEqual(self.batch(ids, action="delete").status_code, 422)
        for patch in ({}, {"last_at": "x"}, {"enabled": 0}, {"win_start": "24:00"},
                      {"command_delay": True}, {"command_delay": -1}, {"button_delay": 121},
                      {"refresh_buttons": 2}, {"poll_interval": "60"}, {"retries": 6},
                      {"win_start=(SELECT 1)": "09:00"}, {"command_delay": 10 ** 400}):
            self.assertEqual(self.batch([aid], patch=patch).status_code, 422)
        self.assertEqual(self.batch([aid], action="enable", patch={"command_delay": 1}).status_code, 422)
        self.assertEqual(self.batch([aid], patch={"win_start": "09:00"}, preview="false").status_code, 422)

    def test_export_whitelist_and_account_isolation(self):
        aid = self.add(groups="-100fake,@fixture", enabled=1)
        self.add("other", name="NOT-THIS-ACCOUNT")
        db = self.dbs["default"]
        db.execute("UPDATE automations SET last_at='SECRET-STATE',retry_count=4,retry_day='SECRET-DAY',last_status='failed'")
        db.execute("INSERT INTO rules(keywords,regex,action,value,reaction,groups,enabled,created_at) VALUES('x',0,'reply','reply','','-100fake',1,123)")
        db.execute("INSERT INTO watched_groups VALUES('-100fake','Fixture',123)")
        db.commit()
        response = self.client.get("/api/tasks/export")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["account"]["id"], "default")
        self.assertEqual(body["automations"][0]["groups"], "-100fake,@fixture")
        self.assertEqual(body["automations"][0]["enabled"], 1)
        self.assertEqual(set(body["automations"][0]), set(COLS.split(",")))
        self.assertEqual(set(body["rules"][0]), set(RULE_COLUMNS))
        self.assertEqual(set(body["watched_groups"][0]), {"chat_id", "name"})
        for forbidden in ("FAKE-DO-NOT-EXPORT", "FAKE-HISTORY", "SECRET-STATE", "SECRET-DAY", "NOT-THIS-ACCOUNT",
                          '"api_key"', '"api_hash"', '"phone"', '"password"', '"session"', '"last_at"', '"retry_count"'):
            self.assertNotIn(forbidden, response.text)
        self.assertEqual(self.reads, ["default"])

    def test_export_selected_and_invalid_ids(self):
        first, second = self.add(name="one"), self.add(name="two")
        response = self.client.post("/api/tasks/export", json={"ids": [second]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([t["name"] for t in response.json()["automations"]], ["two"])
        self.assertEqual(self.client.post("/api/tasks/export", json={"ids": [first, 999]}).status_code, 404)
        for ids in ([], [True], [first, first]):
            self.assertEqual(self.client.post("/api/tasks/export", json={"ids": ids}).status_code, 422)

    def test_import_defaults_preview_and_disabled(self):
        before = self.snapshot()
        preview = self.imp()
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        self.assertTrue(body["preview"])
        self.assertFalse(body["activate"])
        self.assertEqual(body["counts"], {"tasks": 1, "rules": 1, "groups": 1, "duplicates": 0})
        self.assertEqual(body["tasks"][0]["enabled"], 0)
        self.assertTrue(any("禁用" in warning for warning in body["warnings"]))
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.resets)
        apply = self.imp(preview=False)
        self.assertEqual(apply.status_code, 200, apply.text)
        self.assertEqual(body["counts"], apply.json()["counts"])
        self.assertEqual(apply.json()["added"], {"tasks": 1, "rules": 1, "groups": 1})
        self.assertEqual(self.row(apply.json()["ids"][0])["enabled"], 0)
        self.assertEqual(self.dbs["default"].execute("SELECT enabled FROM rules").fetchone()[0], 0)

    def test_import_activate_explicitly_restores_enabled(self):
        response = self.imp(preview=False, activate=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.row(response.json()["ids"][0])["enabled"], 1)
        self.assertEqual(self.dbs["default"].execute("SELECT enabled FROM rules").fetchone()[0], 1)
        self.assertEqual(self.imp(activate="true").status_code, 422)

    def test_merge_repeated_backup_deduplicates_disabled_import(self):
        backup = self.backup()
        first = self.imp(backup, preview=False)
        self.assertEqual(first.status_code, 200, first.text)
        before = self.snapshot()
        second = self.imp(backup, preview=False)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["counts"], {"tasks": 0, "rules": 0, "groups": 0, "duplicates": 3})
        self.assertEqual(second.json()["ids"], [])
        self.assertEqual(before, self.snapshot())

    def test_merge_exact_current_configuration_is_skipped_before_disabling(self):
        self.add(name="current", enabled=1)
        backup = self.client.get("/api/tasks/export").json()
        response = self.imp(backup, preview=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["counts"]["tasks"], 0)
        self.assertEqual(response.json()["counts"]["duplicates"], 1)
        self.assertEqual(self.row(1)["enabled"], 1)

    def test_merge_duplicate_entries_and_custom_json_are_normalized(self):
        task = {"name": "chain", "type": "custom", "target": "@fake",
                "custom_steps": '[ {"v": "/start", "t": "send"} ]'}
        data = self.backup([task, {**task, "custom_steps": '[{"t":"send","v":"/start"}]'}], rules=[], watched_groups=[])
        response = self.imp(data, preview=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["counts"]["tasks"], 1)
        self.assertEqual(response.json()["counts"]["duplicates"], 1)
        repeat = self.imp(data, preview=False)
        self.assertEqual(repeat.json()["counts"]["tasks"], 0)

    def test_replace_is_account_local_and_allocates_new_ids(self):
        old = self.add(name="old")
        other = self.add("other", name="other")
        self.imp(self.backup([]), preview=False)
        before_other = self.snapshot("other")
        preview = self.imp(mode="replace")
        self.assertEqual(preview.status_code, 200)
        self.assertIsNotNone(self.row(old))
        response = self.imp(mode="replace", preview=False)
        self.assertEqual(response.status_code, 200, response.text)
        new = response.json()["ids"][0]
        self.assertGreater(new, old)
        self.assertIsNone(self.row(old))
        self.assertEqual(self.row(new)["name"], "imported")
        self.assertEqual(before_other, self.snapshot("other"))
        self.assertIsNotNone(self.row(other, "other"))
        self.assertIn(("default", old, False), self.resets)
        self.assertIn(("default", new, False), self.resets)

    def test_replace_running_guard_includes_stale_reservations(self):
        aid = self.add()
        before = self.snapshot()
        for running_id in (aid, 999):
            self.running["default"] = {running_id}
            for preview in (True, False):
                response = self.imp(mode="replace", preview=preview)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(before, self.snapshot())
        self.running["default"] = set()
        self.running["other"] = {aid}
        self.assertEqual(self.imp(mode="replace", preview=False).status_code, 200)

    def test_import_other_account_and_source_metadata_never_selects_database(self):
        self.add(name="default-remains")
        before = self.snapshot()
        backup = self.backup(account={"id": "default", "label": "Not a selector"})
        response = self.imp(backup, account="other", preview=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(any("不同" in warning for warning in response.json()["warnings"]))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.row(response.json()["ids"][0], "other")["name"], "imported")
        self.assertEqual(self.reads, ["other"])
        self.assertEqual(self.invalidations, ["other"])
        exported = self.client.get("/api/tasks/export", headers={"x-test-account": "other"})
        self.assertEqual(exported.json()["account"]["id"], "other")

    def test_import_no_success_retry_or_history_is_inherited(self):
        old = self.add()
        db = self.dbs["default"]
        db.execute("UPDATE automations SET last_at='2026-10-30',last_attempt=123,retry_count=5,retry_day='2026-10-30',next_run_at=999,last_status='failed'")
        db.commit()
        backup = self.client.get("/api/tasks/export").json()
        response = self.imp(backup, mode="replace", preview=False)
        self.assertEqual(response.status_code, 200, response.text)
        row = self.row(response.json()["ids"][0])
        for field in ("last_at", "retry_day", "last_status"):
            self.assertEqual(row[field], "")
        for field in ("last_attempt", "retry_count", "next_run_at"):
            self.assertEqual(row[field], 0)
        self.assertEqual(db.execute("SELECT content FROM hits").fetchone()[0], "FAKE-HISTORY")
        self.assertIsNone(self.row(old))

    def test_include_flags_preserve_rules_and_groups_during_replace(self):
        self.imp(preview=False)
        db = self.dbs["default"]
        before_rules = tuple(db.execute("SELECT * FROM rules"))
        before_groups = tuple(db.execute("SELECT * FROM watched_groups"))
        response = self.imp(self.backup(rules=[], watched_groups=[]), mode="replace", preview=False,
                            include_rules=False, include_groups=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(before_rules, tuple(db.execute("SELECT * FROM rules")))
        self.assertEqual(before_groups, tuple(db.execute("SELECT * FROM watched_groups")))
        malformed = self.backup(rules=[{"keywords": "x", "value": "ok", "api_key": "FAKE"}])
        self.assertEqual(self.imp(malformed, include_rules=False).status_code, 422)

    def test_import_validates_all_tasks_before_any_write(self):
        before = self.snapshot()
        tasks = [{"name": "good", "target": "@fake", "command": "/sign"},
                 {"name": "bad", "target": "@fake", "command": "/sign", "win_end": "07:00"}]
        response = self.imp(self.backup(tasks), preview=False)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.resets)

    def test_replace_failure_restores_tasks_rules_groups(self):
        self.add(name="old")
        self.imp(self.backup([]), preview=False)
        db = self.dbs["default"]
        db.execute("CREATE TRIGGER reject_import BEFORE INSERT ON watched_groups BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        db.commit()
        before = self.snapshot()
        self.resets.clear()
        response = self.imp(mode="replace", preview=False)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.resets)
        self.assertFalse(db.in_transaction)

    def test_bad_root_format_schema_and_unknown_secrets(self):
        cases = [self.backup(format="sqlite-backup"), self.backup(schema_version=2),
                 self.backup(schema_version=True), self.backup(schema_version="1"),
                 self.backup(api_key="FAKE"), self.backup(session="FAKE"),
                 self.backup(account={"id": "other", "label": "x", "phone": "FAKE"}),
                 self.backup(exported_at="yesterday"), {"automations": []}]
        before = self.snapshot()
        for data in cases:
            self.assertEqual(self.imp(data, preview=False).status_code, 422)
        self.assertEqual(before, self.snapshot())
        response = self.client.post("/api/tasks/import", json={"data": self.backup(), "password": "FAKE"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("FAKE", response.text)

    def test_task_fields_reject_state_secret_types_and_sql_names(self):
        base = {"name": "bad", "target": "@fake", "command": "/sign"}
        for patch in ({"id": 1}, {"last_at": "x"}, {"retry_count": 4}, {"next_run_at": 123},
                      {"api_key": "FAKE"}, {"api_hash": "FAKE"}, {"phone": "FAKE"},
                      {"password": "FAKE"}, {"session": "FAKE"}, {"type": "evil"},
                      {"action": "DROP TABLE"}, {"name": 123}, {"enabled": True},
                      {"poll_interval": "60"}, {"win_start": "24:00"}, {"command_delay": -1},
                      {"points_pat": "x" * 1025}, {"points_pat": "["},
                      {"name) VALUES('x'); DROP TABLE rules; --": "fake"}):
            response = self.imp(self.backup([{**base, **patch}]), preview=False)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.snapshot()[0], ())

    def test_rules_and_groups_are_strict_and_regex_is_bounded(self):
        for rule in ({"keywords": "[", "regex": 1, "value": "ok"},
                     {"keywords": "x" * 1025, "regex": 1, "value": "ok"},
                     {"keywords": " ", "value": "ok"}, {"keywords": "x", "value": " "},
                     {"keywords": "x", "value": "ok", "regex": True},
                     {"keywords": "x", "value": "ok", "action": "evil"},
                     {"keywords": "x", "value": "ok", "last_at": "x"}):
            self.assertEqual(self.imp(self.backup([], rules=[rule])).status_code, 422)
        for groups in ([{"chat_id": 123}], [{"chat_id": " "}], [{"chat_id": "x", "id": 1}],
                       [{"chat_id": "x"}, {"chat_id": "x"}]):
            self.assertEqual(self.imp(self.backup([], watched_groups=groups)).status_code, 422)

    def test_custom_legacy_steps_are_configuration_only(self):
        steps = [{"t": "send", "v": "/start"}, {"t": "wait", "v": "1000"},
                 {"t": "click", "v": "next", "any": 1, "ai": False},
                 {"t": "captcha", "v": "4", "send": "/answer {code}"},
                 {"t": "until", "timeout": "30000", "success": "done", "fail": "error"}]
        data = self.backup([{"name": "chain", "type": "custom", "target": "@fake",
                             "custom_steps": json.dumps(steps)}], rules=[], watched_groups=[])
        response = self.imp(data, preview=False)
        self.assertEqual(response.status_code, 200, response.text)
        stored = json.loads(self.row(response.json()["ids"][0])["custom_steps"])
        self.assertEqual([s["t"] for s in stored], ["send", "wait", "click", "captcha", "until"])
        self.assertEqual(stored[1]["v"], 1000)
        self.assertEqual(stored[3]["v"], 4)
        for steps in ([{"type": "send", "text": "/start"}], [{"t": "hack"}],
                      [{"t": "captcha", "api_key": "FAKE"}], [{"t": "wait", "v": -1}],
                      [{"t": "captcha", "v": 17}], [{"t": "click", "ai": "yes"}],
                      [{"t": "until", "timeout": False}], []):
            bad = self.backup([{"name": "bad", "type": "custom", "target": "@fake", "custom_steps": json.dumps(steps)}])
            self.assertEqual(self.imp(bad).status_code, 422)

    def test_size_task_count_json_duplicates_and_nonfinite_are_rejected(self):
        response = self.client.post("/api/tasks/import", content=b" " * (MAX_BYTES + 1), headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 413)
        tasks = [{"name": "x", "target": "@fake", "command": "/sign"}] * 501
        self.assertEqual(self.imp(self.backup(tasks)).status_code, 422)
        for raw in ('{', '[1]', '{"data":{},"data":{}}',
                    '{"data":{"format":"tg-persona-config","schema_version":1,"automations":[{"command_delay":NaN}]}}'):
            self.assertEqual(self.client.post("/api/tasks/import", content=raw).status_code, 422)

    def test_string_column_callback_and_no_account_info_are_supported(self):
        router = build_task_router(self.get_db, Auto, self.validate, FIXTURE["_auto_vals"], COLS,
                                   self.invalidate, self.reset, lambda: set())
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            result = client.get("/api/tasks/export")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["account"]["id"], "default")
        for bad in (COLS + ",api_key", COLS + ",id", "name;DROP TABLE automations"):
            with self.assertRaises(ValueError):
                build_task_router(self.get_db, Auto, self.validate, FIXTURE["_auto_vals"], bad,
                                  self.invalidate, self.reset, lambda: set())

    def test_outstanding_transaction_is_not_committed(self):
        aid = self.add()
        db = self.dbs["default"]
        db.execute("INSERT INTO settings VALUES('pending','fake')")
        response = self.batch([aid], patch={"win_start": "09:00"})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(db.in_transaction)
        self.assertEqual(self.row(aid)["win_start"], "08:00")
        db.rollback()

    def test_callback_failure_returns_explicit_saved_warning(self):
        aid = self.add()
        self.reset = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("fake"))
        app = FastAPI()
        app.include_router(build_task_router(self.get_db, Auto, self.validate, FIXTURE["_auto_vals"], COLS,
                                            self.invalidate, self.reset, lambda: set()))
        with TestClient(app) as client:
            response = client.post("/api/autos/batch", json={"ids": [aid], "action": "disable"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["warnings"])
        self.assertEqual(self.row(aid)["enabled"], 0)


if __name__ == "__main__":
    unittest.main()
