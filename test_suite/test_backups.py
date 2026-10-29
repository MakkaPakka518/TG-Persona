"""Offline backup safety tests: fake files only, never the real DATA tree."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import updater as u

DEFAULT = {"running": False, "ok": None, "phase": "", "log": [],
           "transaction": None, "code_rollback": None, "rollback_health": None}
OLD = "backup-20260101-120000"
NEW = "update-" + "a" * 32
LATEST = "update-" + "b" * 32


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "app"
        self.base.mkdir()
        self.data = self.base / "data"
        self.data.mkdir()
        for name, value in (("BASE", self.base), ("DATA", self.data),
                            ("STATE", dict(DEFAULT, log=[]))):
            mock = patch.object(u, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.outside = Path(self.temp.name) / "outside"
        self.outside.mkdir()
        (self.outside / "keep").write_bytes(b"external-safe")
        # Unrelated real-data stand-ins, never eligible for stat/traversal/deletion.
        self.protected = []
        for name in ("database.db", "account.session", ".env", "environment.json",
                     "update-state.json", "update.lock"):
            target = self.data / name
            target.write_bytes(b"{}" if name == "update-state.json" else b"protected")
            self.protected.append((target, target.read_bytes()))
        for name in ("sessions", "database", "venv", ".venv", "other", "backup",
                     "backup-20260101-120000-extra", "update-test", "update-" + "g" * 32,
                     "update-" + "a" * 31, "update-" + "a" * 33):
            target = self.data / name
            target.mkdir()
            (target / "keep").write_bytes(b"protected")
            self.protected.append((target / "keep", b"protected"))
        (self.base / ".env").write_bytes(b"base-protected")
        self.protected.append((self.base / ".env", b"base-protected"))

    def backup(self, name=OLD, size=3, timestamp=100):
        path = self.data / name
        path.mkdir()
        (path / "nested").mkdir()
        (path / "nested" / "file").write_bytes(b"x" * size)
        # 零字节证明文件使 fixture 结构对应真实备份，不影响大小断言。
        if name.startswith("backup-"):
            (path / "main.py").touch()
        else:
            (path / "manifest.json").touch()
            (path / "backup").mkdir()
            (path / "backup" / "main.py").touch()
        os.utime(path, (timestamp, timestamp))
        return path

    def backups(self):
        return [self.backup(OLD, 3, 100), self.backup(NEW, 5, 200),
                self.backup(LATEST, 7, 300)]

    def state(self, **fields):
        u.STATE.update(fields)
        (self.data / "update-state.json").write_text(json.dumps(u.STATE))

    def assert_protected(self, skip_state=False):
        for path, content in self.protected:
            if skip_state and path.name == "update-state.json":
                continue
            self.assertEqual(path.read_bytes(), content, str(path))
        self.assertEqual((self.outside / "keep").read_bytes(), b"external-safe")

    def assert_blocked(self):
        listing = u.list_backups()
        self.assertFalse(listing["cleanup_allowed"])
        self.assertTrue(listing["reason"])
        with self.assertRaises(RuntimeError):
            u.cleanup_backups("all")

    def test_list_counts_sizes_order_and_kind(self):
        self.backups()
        listing = u.list_backups()
        self.assertEqual(listing["count"], 3)
        self.assertEqual(listing["total_bytes"], 15)
        self.assertTrue(listing["cleanup_allowed"])
        self.assertEqual(listing["reason"], "")
        self.assertEqual([item["name"] for item in listing["items"]], [LATEST, NEW, OLD])
        self.assertEqual([item["created_at"] for item in listing["items"]], [300, 200, 100])
        self.assertEqual([item["kind"] for item in listing["items"]],
                         ["transaction", "transaction", "legacy"])
        self.assert_protected()

    def test_keep_latest_default(self):
        self.backups()
        self.assertEqual(u.cleanup_backups(),
                         {"ok": True, "deleted": [NEW, OLD], "freed_bytes": 8, "remaining": 1})
        self.assertTrue((self.data / LATEST).is_dir())
        self.assertFalse((self.data / OLD).exists())
        self.assertFalse((self.data / NEW).exists())
        self.assert_protected()

    def test_all(self):
        self.backups()
        self.assertEqual(u.cleanup_backups("all"),
                         {"ok": True, "deleted": [LATEST, NEW, OLD], "freed_bytes": 15,
                          "remaining": 0})
        self.assertEqual(u.list_backups()["count"], 0)
        self.assert_protected()

    def test_keep_zero_two_and_twenty(self):
        self.backups()
        result = u.cleanup_backups(keep=20)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["remaining"], 3)
        result = u.cleanup_backups(keep=2)
        self.assertEqual(result["deleted"], [OLD])
        self.assertEqual(result["remaining"], 2)
        result = u.cleanup_backups(keep=0)
        self.assertEqual(result["deleted"], [LATEST, NEW])
        self.assertEqual(result["remaining"], 0)
        self.assert_protected()

    def test_empty_and_idempotent(self):
        self.assertEqual(u.cleanup_backups("all"),
                         {"ok": True, "deleted": [], "freed_bytes": 0, "remaining": 0})
        self.backup()
        u.cleanup_backups("all")
        self.assertEqual(u.cleanup_backups("all")["freed_bytes"], 0)

    def test_invalid_arguments_before_lock_or_io(self):
        with patch.object(u, "_open_backup_data", side_effect=AssertionError("unexpected IO")):
            for mode in ("", "ALL", "../all", None, [], {}, 1):
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    u.cleanup_backups(mode=mode)
            for keep in (-1, 21, True, False, 1.0, "1", None, [], {}):
                for mode in ("keep_latest", "all"):
                    with self.subTest(keep=keep, mode=mode), self.assertRaises(ValueError):
                        u.cleanup_backups(mode=mode, keep=keep)

    def test_matching_regular_files_not_backups(self):
        (self.data / NEW).write_bytes(b"not-a-directory")
        self.assertEqual(u.list_backups()["count"], 0)
        u.cleanup_backups("all")
        self.assertEqual((self.data / NEW).read_bytes(), b"not-a-directory")

    def test_invalid_names_and_traversal_never_accepted(self):
        for name in ("../" + NEW, "/" + NEW, NEW + "/child", NEW + "\n",
                     "backup-20260101-120000\n", "update-" + "A" * 32,
                     "backup-abcdefgh-ijklmn", ".", "..", "venv", ".env"):
            with self.subTest(name=name):
                self.assertIsNone(u._backup_kind(name))
        self.backup()
        u.cleanup_backups("all")
        self.assert_protected()

    def test_tie_sort_is_deterministic(self):
        self.backup(NEW, timestamp=100)
        self.backup(LATEST, timestamp=100)
        self.assertEqual(u.cleanup_backups()["deleted"], [NEW])

    def test_running_state_blocks_even_without_lock(self):
        self.backup()
        u.STATE["running"] = True  # Persisted {} must not weaken an in-memory true.
        self.assert_blocked()
        self.assertTrue((self.data / OLD).is_dir())

    def test_persisted_running_is_refreshed(self):
        self.backup()
        (self.data / "update-state.json").write_text(json.dumps({"running": True}))
        self.assertFalse(u.STATE["running"])
        self.assert_blocked()

    def test_all_active_phases_and_recovery_flag_block(self):
        self.backup()
        for phase in ("deps", "replace", "restart", "health", "rollback",
                      "recovery_handoff", "recovery_required"):
            with self.subTest(phase=phase):
                self.state(running=False, phase=phase)
                self.assert_blocked()
        self.state(phase="", recovery_required=True)
        self.assert_blocked()
        self.assertTrue((self.data / OLD).exists())

    def test_unfinished_current_mutation_blocks(self):
        tx = self.backup(NEW)
        (tx / "mutation.json").write_bytes(b"not-read-as-json")
        for phase, fields in (("error", {}), ("idle", {}), ("done", {"ok": False}),
                              ("rolled_back", {"code_rollback": False}),
                              ("rolled_back", {"code_rollback": True, "rollback_health": False})):
            with self.subTest(phase=phase, fields=fields):
                fields = dict({"transaction": NEW, "phase": phase, "ok": None,
                               "code_rollback": None, "rollback_health": None}, **fields)
                self.state(**fields)
                self.assert_blocked()
        self.assertTrue(tx.exists())

    def test_confirmed_success_with_mutation_can_be_deleted(self):
        tx = self.backup(NEW)
        (tx / "mutation.json").write_bytes(b"marker")
        self.state(transaction=NEW, phase="done", ok=True)
        self.assertTrue(u.list_backups()["cleanup_allowed"])
        self.assertEqual(u.cleanup_backups("all")["deleted"], [NEW])
        self.assert_protected(skip_state=True)
        self.assertTrue(u.list_backups()["cleanup_allowed"])

    def test_confirmed_code_rollback_can_be_deleted(self):
        tx = self.backup(NEW)
        (tx / "mutation.json").write_bytes(b"marker")
        for health in (None, True):
            with self.subTest(health=health):
                self.state(transaction=NEW, phase="rolled_back", ok=False,
                           code_rollback=True, rollback_health=health)
                self.assertTrue(u.list_backups()["cleanup_allowed"])
        self.assertEqual(u.cleanup_backups("all")["deleted"], [NEW])

    def test_failed_preparation_without_mutation_is_history(self):
        self.backup(NEW)
        self.state(transaction=NEW, phase="error", ok=False)
        self.assertEqual(u.cleanup_backups("all")["deleted"], [NEW])

    def test_bad_transaction_paths_block_without_traversal(self):
        self.backup()
        for name in ("../outside", "../../outside", "/tmp/outside", NEW + "/child", []):
            with self.subTest(name=name):
                self.state(transaction=name, phase="error")
                self.assert_blocked()
        self.assertTrue((self.data / OLD).exists())
        self.assertEqual((self.outside / "keep").read_bytes(), b"external-safe")

    def test_existing_noncanonical_current_transaction_is_still_protected(self):
        tx = self.data / "update-test"
        (tx / "mutation.json").write_bytes(b"marker")
        self.state(transaction="update-test", phase="error")
        self.assert_blocked()

    def test_update_file_lock_blocks_cleanup_and_listing_permission(self):
        self.backup()
        fd = u._acquire_lock()
        self.assertIsNotNone(fd)
        try:
            self.assert_blocked()
            self.assertTrue((self.data / OLD).exists())
        finally:
            u._release_lock(fd)
        self.assertTrue(u.list_backups()["cleanup_allowed"])
        self.assertEqual(u.cleanup_backups("all")["deleted"], [OLD])

    def test_cleanup_holds_update_lock_during_deletion(self):
        self.backup()
        original = u._delete_backup_dir
        checks = []
        def remove(*args):
            checks.append(u._acquire_lock())
            return original(*args)
        with patch.object(u, "_delete_backup_dir", side_effect=remove):
            u.cleanup_backups("all")
        self.assertTrue(checks)
        self.assertTrue(all(fd is None for fd in checks))

    def test_concurrent_cleanup_is_exclusive(self):
        self.backup()
        entered, finish = threading.Event(), threading.Event()
        original = u._delete_backup_dir
        results, errors = [], []
        def remove(*args):
            entered.set()
            if not finish.wait(5):
                raise AssertionError("test synchronization timed out")
            return original(*args)
        def first():
            try:
                results.append(u.cleanup_backups("all"))
            except BaseException as error:
                errors.append(error)
        with patch.object(u, "_delete_backup_dir", side_effect=remove):
            worker = threading.Thread(target=first)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(u.list_backups()["cleanup_allowed"])
                with self.assertRaises(RuntimeError):
                    u.cleanup_backups("all")
                # start_update must not start a thread while cleanup has its lock.
                with patch.object(u, "snapshot", return_value={"busy": True}), \
                        patch("updater.threading.Thread") as spawn:
                    self.assertEqual(u.start_update(), {"busy": True})
                    spawn.assert_not_called()
            finally:
                finish.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["deleted"], [OLD])

    def test_nested_links_are_sized_but_never_followed(self):
        tx = self.backup()
        links = [tx / "file-link", tx / "dir-link", tx / "dangling", tx / "cycle"]
        links[0].symlink_to(self.outside / "keep")
        links[1].symlink_to(self.outside, target_is_directory=True)
        links[2].symlink_to(self.outside / "missing")
        links[3].symlink_to(tx, target_is_directory=True)
        expected = 3 + sum(path.lstat().st_size for path in links)
        self.assertEqual(u.list_backups()["total_bytes"], expected)
        self.assertEqual(u.cleanup_backups("all")["freed_bytes"], expected)
        self.assertFalse(tx.exists())
        self.assert_protected()

    def test_matching_top_level_symlink_fails_closed_for_entire_plan(self):
        self.backup()
        (self.data / NEW).symlink_to(self.outside, target_is_directory=True)
        self.assert_blocked()
        self.assertTrue((self.data / OLD).exists())
        self.assertTrue((self.data / NEW).is_symlink())
        self.assertEqual(u.list_backups()["count"], 1)
        self.assert_protected()

    def test_data_symlink_is_rejected(self):
        self.backup()
        parked = self.base / "parked-data"
        self.data.rename(parked)
        self.data.symlink_to(self.outside, target_is_directory=True)
        self.assert_blocked()
        self.assertTrue((parked / OLD).exists())
        self.assertFalse((self.outside / "update.lock").exists())
        self.assertEqual((self.outside / "keep").read_bytes(), b"external-safe")

    def test_symlink_swap_after_scan_cannot_delete_external_or_other_backup(self):
        self.backup()
        self.backup(NEW)
        scan = u._scan_backups
        def swap(fd):
            result = scan(fd)
            (self.data / OLD).rename(self.data / "parked-backup")
            (self.data / OLD).symlink_to(self.outside, target_is_directory=True)
            return result
        with patch.object(u, "_scan_backups", side_effect=swap), self.assertRaises(RuntimeError):
            u.cleanup_backups("all")
        self.assertTrue((self.data / NEW).exists())  # All candidates preflight first.
        self.assertTrue((self.data / "parked-backup/nested/file").exists())
        self.assert_protected()

    def test_real_directory_inode_swap_is_rejected(self):
        self.backup()
        scan = u._scan_backups
        def swap(fd):
            result = scan(fd)
            (self.data / OLD).rename(self.data / "parked-backup")
            self.backup(OLD, size=19)
            return result
        with patch.object(u, "_scan_backups", side_effect=swap), self.assertRaises(RuntimeError):
            u.cleanup_backups("all")
        self.assertEqual((self.data / OLD / "nested/file").stat().st_size, 19)
        self.assertTrue((self.data / "parked-backup/nested/file").exists())

    def test_symlink_swap_between_stat_and_open_is_rejected(self):
        self.backup()
        real_open = os.open
        swapped = []
        def open_fd(path, flags, *args, **kwargs):
            if path == OLD and not swapped:
                swapped.append(True)
                (self.data / OLD).rename(self.data / "parked-backup")
                (self.data / OLD).symlink_to(self.outside, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)
        with patch("updater.os.open", side_effect=open_fd):
            self.assert_blocked()
        self.assertTrue(swapped)
        self.assertTrue((self.data / "parked-backup/nested/file").exists())
        self.assert_protected()

    def test_data_swap_after_scan_is_rejected(self):
        self.backup()
        scan = u._scan_backups
        parked = self.base / "parked-data"
        def swap(fd):
            result = scan(fd)
            self.data.rename(parked)
            self.data.symlink_to(self.outside, target_is_directory=True)
            return result
        with patch.object(u, "_scan_backups", side_effect=swap), self.assertRaises(RuntimeError):
            u.cleanup_backups("all")
        self.assertTrue((parked / OLD).exists())
        self.assertEqual((self.outside / "keep").read_bytes(), b"external-safe")

    def test_lock_replacement_after_scan_is_rejected(self):
        self.backup()
        scan = u._scan_backups
        def swap(fd):
            result = scan(fd)
            (self.data / "update.lock").rename(self.data / "parked-lock")
            (self.data / "update.lock").write_bytes(b"new-lock")
            return result
        with patch.object(u, "_scan_backups", side_effect=swap), self.assertRaises(RuntimeError):
            u.cleanup_backups("all")
        self.assertTrue((self.data / OLD).exists())

    def test_unreadable_backup_reports_unknown_size_and_deletes_nothing(self):
        self.backups()
        bad_inode = (self.data / NEW).stat().st_ino
        measure = u._backup_bytes
        def unreadable(fd):
            if os.fstat(fd).st_ino == bad_inode:
                raise PermissionError("fake unreadable")
            return measure(fd)
        with patch.object(u, "_backup_bytes", side_effect=unreadable):
            listing = u.list_backups()
            self.assertEqual(listing["count"], 3)
            self.assertEqual(listing["total_bytes"], 10)
            self.assertIsNone(next(item for item in listing["items"] if item["name"] == NEW)["bytes"])
            self.assertIn("不可读取", listing["reason"])
            self.assertFalse(listing["cleanup_allowed"])
            with self.assertRaises(RuntimeError):
                u.cleanup_backups("all")
        self.assertTrue(all((self.data / name).exists() for name in (OLD, NEW, LATEST)))
        self.assert_protected()

    def test_invalid_or_symlink_state_fails_closed(self):
        self.backup()
        target = self.data / "update-state.json"
        for content in (b"bad-json", b"[]", b"null"):
            with self.subTest(content=content):
                target.write_bytes(content)
                self.assert_blocked()
        target.unlink()
        target.symlink_to(self.outside / "keep")
        with patch("updater.json.load", side_effect=AssertionError("must not read external state")):
            self.assert_blocked()
        self.assertTrue((self.data / OLD).exists())
        self.assertEqual((self.outside / "keep").read_bytes(), b"external-safe")

    def test_symlink_lock_fails_closed(self):
        self.backup()
        target = self.data / "update.lock"
        target.unlink()
        target.symlink_to(self.outside / "keep")
        self.assert_blocked()
        self.assertTrue((self.data / OLD).exists())
        self.assertEqual((self.outside / "keep").read_bytes(), b"external-safe")

    def test_listing_and_deletion_do_not_open_backup_file_contents(self):
        self.backups()
        real_open = os.open
        opened = []
        def checked_open(path, flags, *args, **kwargs):
            opened.append(path)
            if path not in (str(self.data), "update.lock", "update-state.json"):
                self.assertTrue(flags & os.O_DIRECTORY, "opened backup file: " + str(path))
                self.assertTrue(flags & os.O_NOFOLLOW)
            return real_open(path, flags, *args, **kwargs)
        with patch("updater.os.open", side_effect=checked_open), \
                patch.object(Path, "read_text", side_effect=AssertionError("no Path read")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("no Path read")):
            self.assertEqual(u.list_backups()["total_bytes"], 15)
            self.assertEqual(u.cleanup_backups("all")["freed_bytes"], 15)
        self.assertNotIn("file", opened)
        self.assert_protected()

    def test_unrelated_directories_not_scanned(self):
        self.backup()
        permitted = {self.data.stat().st_ino, (self.data / OLD).stat().st_ino,
                     (self.data / OLD / "nested").stat().st_ino}
        scan = os.scandir
        def checked_scan(fd):
            self.assertIsInstance(fd, int)
            self.assertIn(os.fstat(fd).st_ino, permitted)
            return scan(fd)
        with patch("updater.os.scandir", side_effect=checked_scan):
            u.list_backups()
            u.cleanup_backups("all")
        self.assert_protected()

    def test_error_releases_update_lock(self):
        self.backup()
        with patch.object(u, "_delete_backup_dir", side_effect=PermissionError("fake")):
            with self.assertRaises(RuntimeError):
                u.cleanup_backups("all")
        fd = u._acquire_lock()
        self.assertIsNotNone(fd)
        u._release_lock(fd)
        self.assertTrue((self.data / OLD).exists())

    def test_state_change_after_scan_is_checked_again(self):
        self.backup()
        scan = u._scan_backups
        def change_state(fd):
            result = scan(fd)
            self.state(running=True)
            return result
        with patch.object(u, "_scan_backups", side_effect=change_state), self.assertRaises(RuntimeError):
            u.cleanup_backups("all")
        self.assertTrue((self.data / OLD).exists())

    def test_missing_data_list_is_read_only_and_cleanup_empty(self):
        absent = self.base / "absent-data"
        with patch.object(u, "DATA", absent):
            self.assertEqual(u.list_backups(), {"items": [], "count": 0, "total_bytes": 0,
                                               "cleanup_allowed": True, "reason": ""})
            self.assertFalse(absent.exists())
            self.assertEqual(u.cleanup_backups("all")["remaining"], 0)
            self.assertTrue((absent / "update.lock").is_file())


    def test_top_symlink_swap_after_delete_open_stops_before_unlink(self):
        tx = self.backup()
        inode = tx.stat().st_ino
        real_scan, real_delete = os.scandir, u._delete_backup_dir
        deleting, swapped = [], []
        def remove(*args):
            deleting.append(True)
            return real_delete(*args)
        def swap(fd):
            if deleting and not swapped and os.fstat(fd).st_ino == inode:
                swapped.append(True)
                tx.rename(self.data / "parked-backup")
                tx.symlink_to(self.outside, target_is_directory=True)
            return real_scan(fd)
        with patch.object(u, "_delete_backup_dir", side_effect=remove), \
                patch("updater.os.scandir", side_effect=swap), self.assertRaises(RuntimeError):
            u.cleanup_backups("all")
        self.assertTrue(swapped)
        self.assertTrue((self.data / "parked-backup/nested/file").exists())
        self.assert_protected()

    def test_internal_symlink_swap_during_delete_never_follows_external(self):
        tx = self.backup()
        real_delete = u._delete_backup_dir
        swapped = []
        def swap(parent_fd, name, expected, *args):
            if name == "nested" and not swapped:
                swapped.append(True)
                (tx / "nested").rename(tx / "parked-nested")
                (tx / "nested").symlink_to(self.outside, target_is_directory=True)
            return real_delete(parent_fd, name, expected, *args)
        with patch.object(u, "_delete_backup_dir", side_effect=swap), self.assertRaises(RuntimeError):
            u.cleanup_backups("all")
        self.assertTrue(swapped)
        self.assertTrue((tx / "parked-nested/file").exists())
        self.assert_protected()


    def test_keep_latest_preserves_real_backup_not_failed_preparation(self):
        self.backup(OLD, 5, 100)
        failed = self.data / LATEST
        failed.mkdir()
        (failed / "cancelled.json").write_bytes(b"cancelled")
        os.utime(failed, (300, 300))
        listing = u.list_backups()
        self.assertFalse(listing["items"][0]["restorable"])
        result = u.cleanup_backups()
        self.assertEqual(result["deleted"], [LATEST])
        self.assertTrue((self.data / OLD).is_dir())
        self.assertEqual(result["remaining"], 1)
        self.assert_protected()


    def test_helper_holds_lock_until_temporary_source_cleanup_finishes(self):
        tx = self.backup(NEW, 3, 200)
        (tx / "source").mkdir()
        (tx / "transaction.json").write_text(json.dumps({"base": str(self.base)}))
        (tx / "environment.json").write_text(json.dumps({"FIXTURE": "yes"}))
        self.state(running=True, phase="handoff", transaction=NEW)
        real_rmtree = u.shutil.rmtree
        checked = []
        def complete(*args):
            u._state(running=False, ok=True, phase="done")
        def cleanup_source(path, *args, **kwargs):
            if Path(path) == tx / "source":
                with self.assertRaises(RuntimeError):
                    u.cleanup_backups("all")
                checked.append(True)
            return real_rmtree(path, *args, **kwargs)
        with patch.dict(os.environ, {"FIXTURE": "yes"}, clear=True), \
                patch.object(u, "VERSION_FILE", self.base / "VERSION"), \
                patch.object(u, "_run_transaction", side_effect=complete), \
                patch.object(u.shutil, "rmtree", side_effect=cleanup_source):
            u._helper_main(tx)
        self.assertEqual(checked, [True])
        self.assertTrue(tx.exists())
        self.assertFalse((tx / "environment.json").exists())
        self.assertFalse((tx / "source").exists())
        self.assert_protected(skip_state=True)


if __name__ == "__main__":
    unittest.main()
