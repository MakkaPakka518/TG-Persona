"""Offline updater tests: temporary trees; no real HTTP, pip, restart or signals."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import updater as u

SHA = "a" * 40
SHA2 = "b" * 40
DEFAULT = {
    "running": False, "ok": None, "current": None, "latest": None,
    "available": False, "phase": "", "log": [], "checked_at": 0,
    "started_at": 0,
}


def archive(files=None, specials=()):
    files = files if files is not None else {
        "root/main.py": b"x = 2\n", "root/updater.py": b"x = 2\n",
        "root/requirements.txt": b"example>=1\n", "root/VERSION": b"1.1.0\n",
        "root/web/index.html": b"new panel",
        "root/new.txt": b"introduced", "root/newdir/child": b"introduced",
        "root/data/keep": b"evil", "root/venv/keep": b"evil",
        "root/.venv/keep": b"evil", "root/.env": b"evil",
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
        for item in specials:
            tf.addfile(item, io.BytesIO(b"x" * item.size) if item.isfile() else None)
    return out.getvalue()


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "app"
        self.base.mkdir()
        for name in ("data", "venv", ".venv", "web", ".git"):
            (self.base / name).mkdir()
            (self.base / name / "keep").write_text("old " + name)
        for name, body in {"VERSION": "1.0.0\n", "main.py": "x = 1\n",
                           "updater.py": "x = 1\n", "requirements.txt": "oldreq\n",
                           "web/index.html": "old panel", ".env": "private"}.items():
            (self.base / name).write_text(body)
        for name, value in {"BASE": self.base, "DATA": self.base / "data",
                            "VERSION_FILE": self.base / "VERSION", "STATE": dict(DEFAULT, log=[])}.items():
            p = patch.object(u, name, value)
            p.start()
            self.addCleanup(p.stop)
        # Tripwires: any accidental real network/command/signal fails the test.
        for target in ("urllib.request.urlopen", "subprocess.run", "subprocess.Popen", "os.kill"):
            p = patch("updater." + target,
                      side_effect=AssertionError("unmocked " + target))
            p.start()
            self.addCleanup(p.stop)

    def extract(self, blob=None):
        dst = Path(self.temp.name) / "extract"
        dst.mkdir(exist_ok=True)
        return u._safe_extract(blob or archive(), dst)

    def transaction(self):
        src = self.extract()
        txdir = self.base / "data" / "update-test"
        txdir.mkdir(mode=0o700)
        u._backup(src, txdir)
        plan = {"mode": "systemd", "python": "/original/python",
                "active_pid": 10, "identity": "old", "health_url": "http://127.0.0.1:9123/"}
        u._atomic_json(txdir / "plan.json", plan)
        tx = {"base": str(self.base), "source": str(src), "version": "1.1.0", "commit": SHA}
        u._atomic_json(txdir / "transaction.json", tx)
        u._state(running=True, ok=None, transaction=txdir.name, latest="1.1.0")
        return txdir, tx

    def assert_old(self):
        self.assertEqual(u.current_version(), "1.0.0")
        self.assertEqual((self.base / "main.py").read_text(), "x = 1\n")
        self.assertEqual((self.base / "web/index.html").read_text(), "old panel")
        self.assertEqual((self.base / "web/keep").read_text(), "old web")
        self.assertFalse((self.base / "new.txt").exists())
        self.assertFalse((self.base / "newdir").exists())
        self.assert_protected()

    def assert_protected(self):
        for name in ("data", "venv", ".venv", ".git"):
            self.assertEqual((self.base / name / "keep").read_text(), "old " + name)
        self.assertEqual((self.base / ".env").read_text(), "private")

    def test_current_version_missing_and_tuple(self):
        (self.base / "VERSION").unlink()
        self.assertEqual(u.current_version(), "0.0.0")
        self.assertEqual(u._ver_tuple("1.2.3-beta"), (1, 2, 3))

    def test_resolve_main_pins_raw_version(self):
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            return json.dumps({"sha": SHA}).encode() if "api.github" in url else b"1.1.0\n"
        with patch.object(u, "_http_get", side_effect=get):
            self.assertEqual(u.fetch_latest(), "1.1.0")
        self.assertTrue(calls[0].endswith("/commits/main"))
        self.assertIn("/" + SHA + "/VERSION", calls[1])
        self.assertEqual(u.snapshot()["latest_commit"], SHA)
        self.assertTrue(u.snapshot()["available"])

    def test_bad_remote_commit_and_versions(self):
        for sha in ("main", "../evil", "short", "g" * 40):
            with self.subTest(sha=sha), patch.object(u, "_http_get", return_value=json.dumps({"sha": sha}).encode()):
                with self.assertRaises(RuntimeError):
                    u._resolve_main()
        for version in (b"", b"1.2", b"1.2.3\nextra", b"nonsense"):
            with self.subTest(version=version), self.assertRaises(RuntimeError):
                u._valid_version(version)

    def test_check_does_not_change_active_commit(self):
        u._state(running=True, latest="1.1.0", latest_commit=SHA)
        with patch.object(u, "_resolve_main", return_value=(SHA2, "1.2.0")):
            self.assertEqual(u.fetch_latest(), "1.2.0")
        self.assertEqual(u.snapshot()["latest_commit"], SHA)
        self.assertEqual(u.snapshot()["latest"], "1.1.0")

    def test_snapshot_persists_and_log_is_detached(self):
        u._state(running=True, phase="health", ok=None)
        u._log("persistent")
        u.STATE.clear()
        u.STATE.update(DEFAULT)
        snap = u.snapshot()
        self.assertTrue(snap["running"])
        self.assertEqual(snap["phase"], "health")
        snap["log"].clear()
        self.assertEqual(len(u.snapshot()["log"]), 1)

    def test_log_bound(self):
        for number in range(205):
            u._log(str(number))
        self.assertEqual(len(u.snapshot()["log"]), 200)

    def test_http_limit_and_timeout(self):
        response = Mock()
        response.read.return_value = b"12345"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("updater.urllib.request.urlopen", return_value=response) as request:
            with self.assertRaises(RuntimeError):
                u._http_get("https://example.test", timeout=7, limit=4)
            self.assertEqual(request.call_args.kwargs["timeout"], 7)
            response.read.assert_called_once_with(5)

    def test_safe_archive_and_validation(self):
        src = self.extract()
        u._validate_source(src, "1.1.0")
        self.assertEqual((src / "main.py").read_bytes(), b"x = 2\n")

    def test_archive_rejects_traversal_absolute_and_backslash(self):
        for name in ("../escape", "root/../../escape", "/absolute", "root/..", "root\\escape"):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self.extract(archive({name: b"x"}))
        self.assertFalse((Path(self.temp.name) / "escape").exists())

    def test_archive_rejects_symlink_hardlink_device_fifo(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            member = tarfile.TarInfo("root/link")
            member.type = kind
            member.linkname = "../../escape"
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                self.extract(archive({"root/main.py": b"x"}, [member]))

    def test_archive_duplicate_multiple_roots_empty_corrupt(self):
        duplicate = tarfile.TarInfo("root/file")
        for blob in (archive({"root/file": b"x"}, [duplicate]),
                     archive({"root/file": b"x", "other/file": b"x"}),
                     archive({}), b"not gzip"):
            with self.subTest(), self.assertRaises((RuntimeError, tarfile.TarError)):
                self.extract(blob)

    def test_archive_limit_and_modes(self):
        with patch.object(u, "MAX_EXTRACTED", 1), self.assertRaises(RuntimeError):
            self.extract(archive({"root/file": b"xx"}))
        member = tarfile.TarInfo("root/executable")
        member.mode, member.size = 0o6777, 1
        src = self.extract(archive({}, [member]))
        self.assertEqual((src / "executable").stat().st_mode & 0o7777, 0o755)

    def test_missing_critical_syntax_and_version_mismatch(self):
        src = self.extract()
        with self.assertRaises(RuntimeError):
            u._validate_source(src, "1.2.0")
        (src / "main.py").write_text("def broken(:\n")
        with self.assertRaises(SyntaxError):
            u._validate_source(src, "1.1.0")
        (src / "main.py").unlink()
        with self.assertRaises(RuntimeError):
            u._validate_source(src, "1.1.0")

    def test_compile_does_not_import_code(self):
        src = self.extract()
        marker = Path(self.temp.name) / "executed"
        (src / "main.py").write_text(f"open({str(marker)!r}, 'w').write('bad')\n")
        u._validate_source(src, "1.1.0")
        self.assertFalse(marker.exists())

    def test_complete_manifest_restore_and_new_file_cleanup(self):
        txdir, tx = self.transaction()
        entries = json.loads((txdir / "manifest.json").read_text())
        u._replace(Path(tx["source"]), entries)
        self.assertEqual(u.current_version(), "1.1.0")
        self.assertTrue((self.base / "newdir/child").exists())
        self.assert_protected()
        u._rollback(txdir)
        self.assert_old()
        u._rollback(txdir)  # idempotent
        self.assert_old()

    def test_backup_rejects_local_target_symlink(self):
        src = self.extract()
        outside = Path(self.temp.name) / "outside"
        outside.write_text("safe")
        (self.base / "main.py").unlink()
        (self.base / "main.py").symlink_to(outside)
        txdir = Path(self.temp.name) / "backup-test"
        txdir.mkdir()
        with self.assertRaises(RuntimeError):
            u._backup(src, txdir)
        self.assertEqual(outside.read_text(), "safe")

    def test_backup_preserves_nested_local_symlink(self):
        outside = Path(self.temp.name) / "outside"
        outside.write_text("safe")
        (self.base / "web/local-link").symlink_to(outside)
        txdir, tx = self.transaction()
        u._replace(Path(tx["source"]), json.loads((txdir / "manifest.json").read_text()))
        u._rollback(txdir)
        self.assertTrue((self.base / "web/local-link").is_symlink())
        self.assertEqual(outside.read_text(), "safe")

    def test_pip_failure_restores_code_without_dependency_claim(self):
        txdir, tx = self.transaction()
        with patch.object(u, "_pip_install", side_effect=subprocess.CalledProcessError(1, "pip")), \
                patch.object(u, "_replace") as replace, patch.object(u, "_restart") as restart:
            u._run_transaction(txdir, tx)
        self.assert_old()
        replace.assert_not_called()
        restart.assert_not_called()
        state = u.snapshot()
        self.assertFalse(state["ok"])
        self.assertFalse(state["running"])
        self.assertTrue(state["code_rollback"])
        self.assertFalse(state["dependency_rollback"])
        self.assertIn("依赖环境未完整回滚", "\n".join(state["log"]))

    def test_partial_replace_failure_restores_entire_tree(self):
        txdir, tx = self.transaction()
        def partial(src, entries):
            (self.base / "VERSION").write_text("1.1.0")
            (self.base / "new.txt").write_text("new")
            u._remove(self.base / "web")
            raise OSError("disk error")
        with patch.object(u, "_pip_install"), patch.object(u, "_replace", side_effect=partial), \
                patch.object(u, "_restart") as restart:
            u._run_transaction(txdir, tx)
        self.assert_old()
        restart.assert_not_called()
        self.assertEqual(u.snapshot()["phase"], "rolled_back")

    def test_success_is_not_set_until_restart_and_health(self):
        txdir, tx = self.transaction()
        phases = []
        def inspect(plan, *args):
            snap = u.snapshot()
            self.assertTrue(snap["running"])
            self.assertIsNone(snap["ok"])
            phases.append(snap["phase"])
        with patch.object(u, "_pip_install") as pip, \
                patch.object(u, "_restart", side_effect=inspect), \
                patch.object(u, "_health_check", side_effect=inspect):
            u._run_transaction(txdir, tx)
        pip.assert_called_once_with(Path(tx["source"]) / "requirements.txt", python="/original/python")
        self.assertEqual(phases, ["restart", "health"])
        snap = u.snapshot()
        self.assertTrue(snap["ok"])
        self.assertFalse(snap["running"])
        self.assertEqual(snap["phase"], "done")
        self.assertEqual(snap["current"], "1.1.0")
        self.assertFalse(snap["available"])
        self.assert_protected()

    def test_restart_failure_rolls_back_then_checks_old_service(self):
        txdir, tx = self.transaction()
        with patch.object(u, "_pip_install"), \
                patch.object(u, "_restart", side_effect=[RuntimeError("restart failed"), None]) as restart, \
                patch.object(u, "_health_check") as health:
            u._run_transaction(txdir, tx)
        self.assert_old()
        self.assertEqual(restart.call_count, 2)
        health.assert_called_once()
        self.assertTrue(u.snapshot()["rollback_health"])
        self.assertFalse(u.snapshot()["ok"])

    def test_health_failure_rolls_back_then_checks_old_service(self):
        txdir, tx = self.transaction()
        def inspect_old(plan):
            self.assert_old()
        with patch.object(u, "_pip_install"), patch.object(u, "_restart") as restart, \
                patch.object(u, "_health_check", side_effect=[RuntimeError("unhealthy"), None]) as health:
            u._run_transaction(txdir, tx)
        self.assert_old()
        self.assertEqual(restart.call_count, 2)
        self.assertEqual(health.call_count, 2)
        self.assertTrue(u.snapshot()["rollback_health"])
        self.assertEqual(u.snapshot()["phase"], "rolled_back")

    def test_rollback_restart_failure_is_not_success(self):
        txdir, tx = self.transaction()
        with patch.object(u, "_pip_install"), patch.object(u, "_restart"), \
                patch.object(u, "_health_check", side_effect=RuntimeError("unhealthy")):
            u._run_transaction(txdir, tx)
        self.assert_old()
        self.assertFalse(u.snapshot()["rollback_health"])
        self.assertEqual(u.snapshot()["phase"], "error")
        self.assertFalse(u.snapshot()["ok"])

    def test_file_rollback_failure_is_visible(self):
        txdir, tx = self.transaction()
        with patch.object(u, "_pip_install", side_effect=RuntimeError("pip")), \
                patch.object(u, "_rollback", side_effect=OSError("restore failed")):
            u._run_transaction(txdir, tx)
        self.assertFalse(u.snapshot()["code_rollback"])
        self.assertFalse(u.snapshot()["ok"])
        self.assertEqual(u.snapshot()["phase"], "error")

    def test_invalid_manifest_never_touches_protected_data(self):
        txdir, tx = self.transaction()
        for name in ("../outside", "data", "venv", ".env", ".", ".."):
            u._atomic_json(txdir / "manifest.json", [{"name": name, "existed": False}])
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                u._rollback(txdir)
        self.assert_protected()

    def test_pip_uses_original_interpreter_and_checks_exit(self):
        with patch("updater.subprocess.run", return_value=Mock(returncode=0)) as run:
            u._pip_install(Path("candidate/requirements.txt"), python="/active/python")
        self.assertEqual(run.call_args.args[0][:3], ["/active/python", "-m", "pip"])
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["timeout"], 300)

    def test_lock_excludes_threads_before_worker_runs(self):
        created = []
        class DeferredThread:
            def __init__(self, **kwargs):
                created.append(kwargs)
            def start(self):
                pass
        # Use the real threading class for callers, mock only updater's factory.
        real_thread = threading.Thread
        with patch.object(u.threading, "Thread", DeferredThread):
            callers = [real_thread(target=u.start_update) for _ in range(16)]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join()
            self.assertEqual(len(created), 1)
            fd = created[0]["args"][0]
            self.assertIsNone(u._acquire_lock())
            u._release_lock(fd)
            # Persisted running also excludes callers in the helper handoff gap.
            u.start_update()
            self.assertEqual(len(created), 1)

    def test_thread_start_failure_releases_lock(self):
        with patch.object(u.threading.Thread, "start", side_effect=RuntimeError("thread")):
            with self.assertRaises(RuntimeError):
                u.start_update()
        fd = u._acquire_lock()
        self.assertIsNotNone(fd)
        u._release_lock(fd)
        self.assertFalse(u.snapshot()["running"])

    def test_prepare_downloads_same_commit_after_main_moves(self):
        u._state(latest="1.0.5", latest_commit=SHA2)
        downloaded = []
        def get(url, **kwargs):
            downloaded.append(url)
            return archive()
        def handoff(txdir, plan):
            self.assert_old()  # no code/pip mutation in the parent
            self.assertEqual((txdir / "helper.py").read_bytes(), Path(u.__file__).read_bytes())
            self.assertEqual((txdir / "environment.json").stat().st_mode & 0o777, 0o600)
        fd = u._acquire_lock()
        u._state(running=True, ok=None)
        with patch.object(u, "_resolve_main", return_value=(SHA, "1.1.0")), \
                patch.object(u, "_restart_plan", return_value={"mode": "systemd", "python": "python"}), \
                patch.object(u, "_http_get", side_effect=get), \
                patch.object(u, "_launch_helper", side_effect=handoff), \
                patch.object(u, "_pip_install") as pip:
            u._do_update(fd)
        self.assertEqual(downloaded, [f"https://codeload.github.com/{u.REPO}/tar.gz/{SHA}"])
        self.assertTrue(u.snapshot()["running"])
        self.assertIsNone(u.snapshot()["ok"])
        self.assertEqual(u.snapshot()["latest_commit"], SHA)
        pip.assert_not_called()
        self.assert_old()

    def test_prepare_failures_do_not_modify_application(self):
        for stage in ("resolve", "plan", "download", "validate", "backup", "handoff"):
            with self.subTest(stage=stage):
                u._state(running=True, ok=None)
                fd = u._acquire_lock()
                with patch.object(u, "_resolve_main", side_effect=RuntimeError("network") if stage == "resolve" else None,
                                  return_value=(SHA, "1.1.0")), \
                        patch.object(u, "_restart_plan", side_effect=RuntimeError("permission") if stage == "plan" else None,
                                     return_value={"mode": "systemd", "python": "python"}), \
                        patch.object(u, "_http_get", side_effect=RuntimeError("download") if stage == "download" else None,
                                     return_value=archive()), \
                        patch.object(u, "_validate_source", side_effect=RuntimeError("validation") if stage == "validate" else None), \
                        patch.object(u, "_backup", side_effect=RuntimeError("backup") if stage == "backup" else None), \
                        patch.object(u, "_launch_helper", side_effect=RuntimeError("launch") if stage == "handoff" else None):
                    u._do_update(fd, force=True)
                self.assert_old()
                self.assertFalse(u.snapshot()["running"])
                self.assertFalse(u.snapshot()["ok"])
                free_fd = u._acquire_lock()
                self.assertIsNotNone(free_fd)
                u._release_lock(free_fd)

    def test_no_new_version_unless_force(self):
        fd = u._acquire_lock()
        u._state(running=True)
        with patch.object(u, "_resolve_main", return_value=(SHA, "1.0.0")), \
                patch.object(u, "_restart_plan") as plan:
            u._do_update(fd, force=False)
        plan.assert_not_called()
        self.assertIsNone(u.snapshot()["ok"])
        self.assertFalse(u.snapshot()["running"])
        self.assertEqual(u.snapshot()["phase"], "idle")

    def test_launch_systemd_is_separate_checked_transient_unit(self):
        txdir, tx = self.transaction()
        u._atomic_json(txdir / "ready.json", {"pid": 123, "identity": "start"})
        with patch("updater.subprocess.run", return_value=Mock(returncode=0)) as run, \
                patch.object(u, "_process_identity", return_value="start"):
            u._launch_helper(txdir, {"mode": "systemd", "python": "/python"})
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "systemd-run")
        self.assertIn("--property=Type=exec", cmd)
        self.assertNotIn("systemctl", cmd)
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertIn(str(txdir / "helper.py"), cmd)

    def test_launch_direct_waits_for_readiness_not_popen(self):
        txdir, tx = self.transaction()
        with patch("updater.subprocess.Popen", return_value=Mock(pid=123)) as popen, \
                patch.object(u.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaises(RuntimeError):
                u._launch_helper(txdir, {"mode": "direct", "python": "/python"})
        self.assertTrue((txdir / "cancelled.json").exists())
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertTrue(popen.call_args.kwargs["close_fds"])

    def test_systemd_launch_failure_is_propagated(self):
        txdir, tx = self.transaction()
        with patch("updater.subprocess.run", side_effect=subprocess.CalledProcessError(1, "systemd-run")):
            with self.assertRaises(subprocess.CalledProcessError):
                u._launch_helper(txdir, {"mode": "systemd", "python": "/python"})

    def test_helper_mode_runs_under_lock_and_cleans_environment(self):
        txdir, tx = self.transaction()
        u._atomic_json(txdir / "environment.json", {"PORT": "9123"})
        def run(dirpath, transaction):
            self.assertIsNone(u._acquire_lock())
            self.assertEqual(os.environ["PORT"], "9123")
            self.assertTrue((txdir / "ready.json").exists())
        with patch.dict(os.environ, {}, clear=False), patch.object(u, "_run_transaction", side_effect=run) as runner:
            u._helper_main(txdir)
        runner.assert_called_once()
        self.assertFalse((txdir / "environment.json").exists())
        self.assertTrue((txdir / "backup").is_dir())
        fd = u._acquire_lock()
        self.assertIsNotNone(fd)
        u._release_lock(fd)

    def test_cancelled_late_helper_cannot_mutate(self):
        txdir, tx = self.transaction()
        u._atomic_json(txdir / "environment.json", {})
        u._atomic_json(txdir / "cancelled.json", {"cancelled": True})
        with patch.dict(os.environ, {}, clear=False), patch.object(u, "_run_transaction") as runner:
            u._helper_main(txdir)
        runner.assert_not_called()
        self.assert_old()

    def mock_plan(self, systemd=False, argv=None, cgroup="0::/\n", port="9123", healthy=True):
        original_bytes, original_text, original_is_dir = Path.read_bytes, Path.read_text, Path.is_dir
        def read_bytes(path):
            if str(path).endswith("/cmdline"):
                return b"\0".join(os.fsencode(arg) for arg in (argv or ["/python", "main.py"])) + b"\0"
            return original_bytes(path)
        def read_text(path, *args, **kwargs):
            return cgroup if str(path).endswith("/cgroup") else original_text(path, *args, **kwargs)
        def is_dir(path):
            return systemd if str(path) == "/run/systemd/system" else original_is_dir(path)
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(Path, "read_bytes", read_bytes))
        stack.enter_context(patch.object(Path, "read_text", read_text))
        stack.enter_context(patch.object(Path, "is_dir", is_dir))
        stack.enter_context(patch.object(u, "_process_identity", return_value="start"))
        stack.enter_context(patch.object(u, "_healthy", return_value=healthy))
        stack.enter_context(patch.object(u, "_service_pid", return_value=os.getpid()))
        stack.enter_context(patch.object(u.shutil, "which", return_value="/mock/bin"))
        stack.enter_context(patch.dict(os.environ, {"PORT": port}, clear=True))
        return stack

    def test_direct_plan_keeps_port_env_and_uvicorn_argv(self):
        with self.mock_plan(port="9123"):
            plan = u._restart_plan()
        self.assertEqual(plan["mode"], "direct")
        self.assertEqual(plan["health_url"], "http://127.0.0.1:9123/")
        argv = ["/venv/python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port=9234"]
        with self.mock_plan(argv=argv, port="9123"):
            plan = u._restart_plan()
        self.assertEqual(plan["argv"], argv)
        self.assertEqual(plan["health_url"], "http://127.0.0.1:9234/")

    def test_systemd_plan_preserves_command_port(self):
        argv = ["/venv/python", "-m", "uvicorn", "main:app", "--port", "9345"]
        with self.mock_plan(systemd=True, argv=argv):
            plan = u._restart_plan()
        self.assertEqual(plan["mode"], "systemd")
        self.assertEqual(plan["health_url"], "http://127.0.0.1:9345/")

    def test_plan_fails_closed_for_managed_cgroup_or_unknown_argv(self):
        for options in ({"cgroup": "0::/system.slice/custom.service\n"},
                        {"cgroup": "0::/user.slice/app.scope\n"},
                        {"argv": ["/python", "other.py"]},
                        {"argv": ["/python", "main.py", "--reload"]},
                        {"argv": ["/python", "main.py", "--workers=2"]},
                        {"port": "invalid"}, {"port": "70000"}, {"healthy": False}):
            with self.subTest(options=options), self.mock_plan(**options), self.assertRaises(RuntimeError):
                u._restart_plan()

    def test_systemd_missing_tools_or_wrong_service_pid(self):
        with self.mock_plan(systemd=True), patch.object(u.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError):
                u._restart_plan()
        with self.mock_plan(systemd=True), patch.object(u, "_service_pid", return_value=1):
            with self.assertRaises(RuntimeError):
                u._restart_plan()

    def test_health_url_override_is_loopback_only(self):
        with self.mock_plan(), patch.dict(os.environ, {"TG_PERSONA_HEALTH_URL": "http://localhost:9999/"}):
            self.assertEqual(u._restart_plan()["health_url"], "http://localhost:9999/")
        with self.mock_plan(), patch.dict(os.environ, {"TG_PERSONA_HEALTH_URL": "https://remote.test/"}):
            with self.assertRaises(RuntimeError):
                u._restart_plan()

    def test_restart_checks_systemctl_result_and_changed_pid(self):
        txdir, tx = self.transaction()
        plan = json.loads((txdir / "plan.json").read_text())
        for failure in (subprocess.CalledProcessError(1, "systemctl"), subprocess.TimeoutExpired("systemctl", 45)):
            with self.subTest(failure=failure), patch("updater.subprocess.run", side_effect=failure):
                with self.assertRaises(type(failure)):
                    u._restart(plan, txdir)
        with patch("updater.subprocess.run", return_value=Mock(returncode=0)) as run, \
                patch.object(u, "_service_pid", return_value=10):
            with self.assertRaises(RuntimeError):
                u._restart(plan, txdir)
            self.assertTrue(run.call_args.kwargs["check"])
        with patch("updater.subprocess.run", return_value=Mock(returncode=0)), \
                patch.object(u, "_service_pid", return_value=11), \
                patch.object(u, "_process_identity", return_value="new"):
            u._restart(plan, txdir)
        self.assertEqual(plan["active_pid"], 11)

    def test_direct_restart_keeps_argv_and_refuses_busy_port(self):
        txdir, tx = self.transaction()
        argv = ["/python", "main.py"]
        plan = {"mode": "direct", "argv": argv, "cwd": str(self.base),
                "active_pid": 10, "identity": "old", "health_url": "http://127.0.0.1:9123/"}
        with patch.object(u, "_stop_process"), patch.object(u, "_healthy", return_value=True):
            with self.assertRaises(RuntimeError):
                u._restart(plan, txdir)
        with patch.object(u, "_stop_process"), patch.object(u, "_healthy", return_value=False), \
                patch("updater.subprocess.Popen", return_value=Mock(pid=11)) as popen, \
                patch.object(u, "_process_identity", return_value="new"):
            u._restart(plan, txdir)
        self.assertEqual(popen.call_args.args[0], argv)
        self.assertEqual(popen.call_args.kwargs["cwd"], str(self.base))
        self.assertEqual(plan["active_pid"], 11)

    def test_health_requires_stable_process_and_three_http_successes(self):
        plan = {"mode": "systemd", "active_pid": 11, "identity": "new", "health_url": "http://localhost:9123/"}
        with patch.object(u, "_process_identity", return_value="new"), \
                patch.object(u, "_service_pid", return_value=11), \
                patch("updater.subprocess.run", return_value=Mock(returncode=0)), \
                patch.object(u, "_healthy", side_effect=[True, False, True, True, True]) as health, \
                patch.object(u.time, "sleep"):
            u._health_check(plan)
        self.assertEqual(health.call_count, 5)
        with patch.object(u, "_process_identity", return_value=None):
            with self.assertRaises(RuntimeError):
                u._health_check(plan)
        with patch.object(u.time, "monotonic", side_effect=[0, 61]):
            with self.assertRaises(RuntimeError):
                u._health_check(plan)

    def test_health_rejects_inactive_systemd_even_with_http_200(self):
        plan = {"mode": "systemd", "active_pid": 11, "identity": "new", "health_url": "http://localhost:9123/"}
        with patch.object(u, "_process_identity", return_value="new"), \
                patch("updater.subprocess.run", return_value=Mock(returncode=1)), \
                patch.object(u, "_healthy", return_value=True), \
                patch.object(u.time, "monotonic", side_effect=[0, 0, 61]), \
                patch.object(u.time, "sleep"):
            with self.assertRaises(RuntimeError):
                u._health_check(plan)

    def test_stop_process_never_signals_reused_pid(self):
        plan = {"active_pid": 10, "identity": "old"}
        with patch.object(u, "_process_identity", return_value="different"), \
                patch("updater.os.kill") as kill:
            u._stop_process(plan)
        kill.assert_not_called()
        with patch.object(u, "_process_identity", side_effect=["old", None, None]), \
                patch("updater.os.kill") as kill:
            u._stop_process(plan)
        kill.assert_called_once_with(10, signal.SIGTERM)


    def test_stale_preparation_parent_died_allows_new_main_attempt(self):
        u._state(running=True, phase="download", owner_pid=99999,
                 owner_identity="dead", transaction=None)
        with patch.object(u, "_process_identity", return_value=None), \
                patch.object(u.threading, "Thread") as factory:
            u.start_update()
        self.assertIs(factory.call_args.kwargs["target"], u._do_update)
        u._release_lock(factory.call_args.kwargs["args"][0])
        self.assert_old()

    def test_stale_preparation_thread_died_in_live_parent(self):
        u._state(running=True, phase="check", owner_pid=os.getpid(),
                 owner_identity="same", owner_tid=99999999)
        with patch.object(u, "_process_identity", return_value="same"), \
                patch.object(u.threading, "Thread") as factory:
            u.start_update()
        self.assertEqual(factory.call_count, 1)
        u._release_lock(factory.call_args.kwargs["args"][0])

    def test_handoff_free_lock_with_live_helper_is_not_stale(self):
        txdir, tx = self.transaction()
        u._state(phase="handoff", owner_pid=100, owner_identity="dead-parent")
        u._atomic_json(txdir / "ready.json", {"pid": 200, "identity": "live-helper"})
        def identity(pid):
            return "live-helper" if pid == 200 else None
        with patch.object(u, "_process_identity", side_effect=identity), \
                patch.object(u.threading, "Thread") as factory:
            result = u.start_update()
        factory.assert_not_called()
        self.assertTrue(result["running"])
        fd = u._acquire_lock()
        self.assertIsNotNone(fd)
        u._release_lock(fd)

    def test_dead_or_reused_helper_requires_recovery_not_download(self):
        txdir, tx = self.transaction()
        u._state(phase="health", owner_pid=200, owner_identity="old-helper")
        u._atomic_json(txdir / "ready.json", {"pid": 200, "identity": "old-helper"})
        u._atomic_json(txdir / "mutation.json", {"started": True})
        with patch.object(u, "_process_identity", return_value="reused-pid"), \
                patch.object(u.threading, "Thread") as factory:
            result = u.start_update(force=True)
        self.assertIs(factory.call_args.kwargs["target"], u._do_recovery)
        self.assertEqual(factory.call_args.kwargs["args"][1], txdir)
        self.assertTrue(result["running"])
        u._release_lock(factory.call_args.kwargs["args"][0])

    def test_dead_pre_mutation_helper_does_not_require_file_restore(self):
        txdir, tx = self.transaction()
        u._state(phase="handoff", owner_pid=200, owner_identity="old-helper")
        with patch.object(u, "_process_identity", return_value=None), \
                patch.object(u.threading, "Thread") as factory:
            u.start_update()
        self.assertIs(factory.call_args.kwargs["target"], u._do_update)
        self.assertTrue((txdir / "cancelled.json").exists())
        u._release_lock(factory.call_args.kwargs["args"][0])

    def test_stale_recovery_restores_before_restart_without_pip(self):
        txdir, tx = self.transaction()
        u._replace(Path(tx["source"]), json.loads((txdir / "manifest.json").read_text()))
        def check(plan, *args):
            self.assert_old()
            self.assertTrue(u.snapshot()["running"])
            self.assertIsNone(u.snapshot()["ok"])
        with patch.object(u, "_restart", side_effect=check) as restart, \
                patch.object(u, "_health_check", side_effect=check), \
                patch.object(u, "_pip_install") as pip:
            u._recover_transaction(txdir, tx)
        restart.assert_called_once()
        pip.assert_not_called()
        self.assert_old()
        self.assertFalse(u.snapshot()["running"])
        self.assertFalse(u.snapshot()["ok"])
        self.assertTrue(u.snapshot()["rollback_health"])
        self.assertFalse(u.snapshot()["dependency_rollback"])

    def test_recovery_failure_is_retryable_without_new_download(self):
        txdir, tx = self.transaction()
        with patch.object(u, "_rollback", side_effect=OSError("disk")):
            u._recover_transaction(txdir, tx)
        self.assertEqual(u.snapshot()["phase"], "recovery_required")
        with patch.object(u.threading, "Thread") as factory:
            u.start_update(force=True)
        self.assertIs(factory.call_args.kwargs["target"], u._do_recovery)
        u._release_lock(factory.call_args.kwargs["args"][0])

    def test_stale_recovery_missing_manifest_fails_closed(self):
        txdir, tx = self.transaction()
        (txdir / "manifest.json").unlink()
        u._state(phase="replace", owner_pid=200, owner_identity="dead")
        with patch.object(u, "_process_identity", return_value=None), \
                patch.object(u.threading, "Thread") as factory:
            result = u.start_update()
        factory.assert_not_called()
        self.assertEqual(result["phase"], "recovery_required")
        self.assertFalse(result["running"])
        self.assertFalse(result["ok"])

    def test_recovery_handoff_refreshes_ready_and_uses_current_plan(self):
        txdir, tx = self.transaction()
        u._atomic_json(txdir / "ready.json", {"pid": 1, "identity": "stale"})
        u._atomic_json(txdir / "cancelled.json", {"cancelled": True})
        plan = {"mode": "systemd", "python": "/current/python"}
        def launch(dirpath, current_plan):
            self.assertFalse((txdir / "ready.json").exists())
            self.assertFalse((txdir / "cancelled.json").exists())
            self.assertEqual(current_plan, plan)
            self.assertTrue(json.loads((txdir / "transaction.json").read_text())["recovery"])
        fd = u._acquire_lock()
        with patch.object(u, "_restart_plan", return_value=plan), \
                patch.object(u, "_launch_helper", side_effect=launch), \
                patch.object(u, "_resolve_main") as resolve:
            u._do_recovery(fd, txdir)
        resolve.assert_not_called()
        self.assertTrue(u.snapshot()["running"])
        self.assert_old()

    def test_helper_dispatches_recovery_mode(self):
        txdir, tx = self.transaction()
        tx["recovery"] = True
        u._atomic_json(txdir / "transaction.json", tx)
        u._atomic_json(txdir / "environment.json", {})
        with patch.dict(os.environ, {}, clear=False), \
                patch.object(u, "_recover_transaction") as recovery, \
                patch.object(u, "_run_transaction") as update:
            u._helper_main(txdir)
        recovery.assert_called_once()
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
