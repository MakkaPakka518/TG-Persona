#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transactional main-branch updater (stdlib only, Linux/Python 3.8+).

A private helper is copied before replacing code. systemd installations use a
separate transient unit, NOT a detached child in the application's cgroup.
Unmanaged processes can use a detached helper and their original argv/PORT;
other managed processes fail closed. No success until restart AND HTTP health.

Dependencies use the application's interpreter, before code replacement. pip
can partially mutate that environment: code/VERSION roll back, dependencies do
NOT. This limitation is reported explicitly in status/logs, not hidden.
"""
import fcntl
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

REPO = "MakkaPakka518/TG-Persona"
BRANCH = "main"
SERVICE = "tg-persona"
BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
VERSION_FILE = BASE / "VERSION"
RAW_VERSION = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
TARBALL_URL = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{BRANCH}"
SKIP_NAMES = {"data", "venv", ".venv", ".git", "__pycache__",
              ".pytest_cache", ".env"}
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXTRACTED = 256 * 1024 * 1024
_STATE_LOCK = threading.RLock()
STATE = {
    "running": False, "ok": None, "current": None, "latest": None,
    "available": False, "phase": "", "log": [], "checked_at": 0,
    "started_at": 0,
}


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".json-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _load_state():
    path = DATA / "update-state.json"
    if path.exists():
        STATE.update(json.loads(path.read_text(encoding="utf-8")))


def _state(**fields):
    with _STATE_LOCK:
        STATE.update(fields)
        _atomic_json(DATA / "update-state.json", STATE)


def _log(msg):
    with _STATE_LOCK:
        STATE["log"] = (STATE["log"] + [time.strftime("%H:%M:%S") + "  " + str(msg)])[-200:]
        _state()


def _http_get(url, timeout=30, limit=MAX_ARCHIVE):
    req = urllib.request.Request(url, headers={"User-Agent": "tg-persona-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        blob = response.read(limit + 1)
    if len(blob) > limit:
        raise RuntimeError("下载内容过大")
    return blob


def current_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "0.0.0"


def _ver_tuple(v):
    parts = []
    for x in str(v).split("."):
        num = ""
        for ch in x:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _valid_version(raw):
    value = raw.decode("utf-8").strip() if isinstance(raw, bytes) else raw.strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", value):
        raise RuntimeError("VERSION 格式无效")
    return value


def _resolve_main():
    # Resolve once; both VERSION and tarball use this exact commit, never HEAD.
    url = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
    commit = json.loads(_http_get(url, timeout=20, limit=1024 * 1024))["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("main commit 无效")
    version = _valid_version(_http_get(
        f"https://raw.githubusercontent.com/{REPO}/{commit}/VERSION",
        timeout=20, limit=4096))
    return commit, version


def fetch_latest():
    commit, latest = _resolve_main()
    with _STATE_LOCK:
        _load_state()
        # A manual check must not change the snapshot an active update uses.
        if not STATE["running"]:
            _state(latest=latest, latest_commit=commit, current=current_version(),
                   available=_ver_tuple(latest) > _ver_tuple(current_version()),
                   checked_at=int(time.time()))
    return latest


def _pip_install(requirements, python=None):
    subprocess.run([python or sys.executable, "-m", "pip", "install", "-q",
                    "-r", str(requirements)], check=True, timeout=300)


def _acquire_lock(blocking=False, data_fd=None):
    if data_fd is None:
        DATA.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(DATA / "update.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    else:
        # Backup operations pin DATA and never follow a substituted lock link.
        fd = os.open("update.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                     0o600, dir_fd=data_fd)
    try:
        if data_fd is not None:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not os.path.samestat(
                    info, os.stat("update.lock", dir_fd=data_fd, follow_symlinks=False)):
                raise RuntimeError("更新锁文件不安全")
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return fd
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise


def _release_lock(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


# Only direct, historical updater directories are eligible. No caller paths.
_BACKUP_NAMES = re.compile(r"(?:backup-[0-9]{8}-[0-9]{6}|update-[0-9a-f]{32})", re.ASCII)
_BACKUP_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _backup_kind(name):
    if not isinstance(name, str) or not _BACKUP_NAMES.fullmatch(name):
        return None
    return "legacy" if name.startswith("backup-") else "transaction"


def _open_backup_data(create=False):
    data = Path(DATA)
    if not data.is_absolute() or ".." in data.parts:
        raise RuntimeError("备份父目录路径不安全")
    try:
        info = data.lstat()
    except FileNotFoundError:
        if not create:
            return None
        data.mkdir(parents=True, exist_ok=True)
        info = data.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("data 必须是实际目录，不能是符号链接")
    fd = os.open(str(data), _BACKUP_DIR_FLAGS)
    try:
        if not os.path.samestat(info, os.fstat(fd)):
            raise RuntimeError("data 目录发生变化，拒绝清理")
        _check_backup_data(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_backup_data(fd):
    info = Path(DATA).lstat()
    if not stat.S_ISDIR(info.st_mode) or not os.path.samestat(info, os.fstat(fd)):
        raise RuntimeError("data 目录发生变化，拒绝清理")


def _check_backup_entry(parent_fd, name, expected):
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or not os.path.samestat(info, expected):
        raise RuntimeError("备份目录发生变化，拒绝清理: " + name)


def _open_backup_dir(parent_fd, name, expected):
    _check_backup_entry(parent_fd, name, expected)
    if expected.st_dev != os.fstat(parent_fd).st_dev:
        raise RuntimeError("备份包含跨文件系统目录，拒绝清理")
    fd = os.open(name, _BACKUP_DIR_FLAGS, dir_fd=parent_fd)
    if not os.path.samestat(expected, os.fstat(fd)):
        os.close(fd)
        raise RuntimeError("备份目录发生变化，拒绝清理: " + name)
    return fd


def _backup_bytes(fd):
    # Logical file/link bytes, not allocated blocks; never open file contents.
    total = 0
    with os.scandir(fd) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = _open_backup_dir(fd, entry.name, info)
                try:
                    total += _backup_bytes(child)
                    _check_backup_entry(fd, entry.name, info)
                finally:
                    os.close(child)
            else:
                total += info.st_size
    return total


def _restorable_backup(directory_fd, kind):
    """保留策略优先保留真正包含代码的备份，不把失败准备目录当作唯一备份。"""
    backup_fd = None
    try:
        if kind == "legacy":
            return stat.S_ISREG(os.stat("main.py", dir_fd=directory_fd, follow_symlinks=False).st_mode)
        manifest = os.stat("manifest.json", dir_fd=directory_fd, follow_symlinks=False)
        backup = os.stat("backup", dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(manifest.st_mode) or not stat.S_ISDIR(backup.st_mode):
            return False
        backup_fd = _open_backup_dir(directory_fd, "backup", backup)
        main = os.stat("main.py", dir_fd=backup_fd, follow_symlinks=False)
        _check_backup_entry(directory_fd, "backup", backup)
        return stat.S_ISREG(main.st_mode)
    except (OSError, RuntimeError):
        return False
    finally:
        if backup_fd is not None:
            os.close(backup_fd)


def _scan_backups(data_fd):
    items, identities, errors = [], {}, []
    if data_fd is None:
        return items, identities, errors
    with os.scandir(data_fd) as entries:
        for entry in entries:
            kind = _backup_kind(entry.name)
            if not kind:
                continue  # Do not even stat unknown trees (sessions/db/venv/etc.).
            item = None
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise RuntimeError("顶层备份是符号链接")
                if not stat.S_ISDIR(info.st_mode):
                    continue
                item = {"name": entry.name, "bytes": None,
                        "created_at": info.st_mtime, "kind": kind}
                items.append(item)
                identities[entry.name] = info
                child = _open_backup_dir(data_fd, entry.name, info)
                try:
                    item["bytes"] = _backup_bytes(child)
                    item["restorable"] = _restorable_backup(child, kind)
                    _check_backup_entry(data_fd, entry.name, info)
                finally:
                    os.close(child)
            except (OSError, RuntimeError, RecursionError):
                # No raw exception paths or backup file contents in the API.
                errors.append("备份不可读取或不安全: " + entry.name)
                if item is not None:
                    item["bytes"] = None
    items.sort(key=lambda item: (item["created_at"], item["name"]), reverse=True)
    return items, identities, errors


def _backup_cleanup_reason(data_fd):
    """Refresh only updater state, using no-follow reads of its control file.

    Completed updates retain mutation.json. That marker alone must NOT prevent
    deleting a confirmed successful update or a completed code rollback.
    """
    running = STATE.get("running", False)
    if data_fd is not None:
        try:
            state_fd = os.open("update-state.json", os.O_RDONLY | os.O_NOFOLLOW |
                               os.O_NONBLOCK, dir_fd=data_fd)
        except FileNotFoundError:
            state_fd = None
        if state_fd is not None:
            with os.fdopen(state_fd, "r", encoding="utf-8") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise RuntimeError("更新状态文件不安全")
                saved = json.load(source)
                if not isinstance(saved, dict):
                    raise RuntimeError("更新状态文件无效")
                if not os.path.samestat(info, os.stat("update-state.json",
                        dir_fd=data_fd, follow_symlinks=False)):
                    raise RuntimeError("更新状态文件发生变化")
                STATE.update(saved)  # Same refresh semantics as _load_state().
    if running or STATE.get("running"):
        STATE["running"] = True  # A stale persisted false cannot weaken this guard.
        return "更新正在运行，禁止清理备份"
    phase = STATE.get("phase")
    if STATE.get("recovery_required") or phase in (
            "deps", "replace", "restart", "health", "rollback",
            "recovery_handoff", "recovery_required"):
        return "存在待恢复的更新事务，禁止清理备份"
    name = STATE.get("transaction")
    if name is None or name == "":
        return ""
    if not isinstance(name, str):
        return "当前事务路径无效，禁止清理备份"
    txdir = _transaction_dir()
    if txdir is None or txdir.parent != Path(DATA):
        return "当前事务路径无效，禁止清理备份"
    if data_fd is None:
        return ""
    try:
        info = os.stat(name, dir_fd=data_fd, follow_symlinks=False)
    except FileNotFoundError:
        return ""
    child = _open_backup_dir(data_fd, name, info)
    try:
        try:
            os.stat("mutation.json", dir_fd=child, follow_symlinks=False)
            mutated = True
        except FileNotFoundError:
            mutated = False
        _check_backup_entry(data_fd, name, info)
    finally:
        os.close(child)
    completed = ((phase == "done" and STATE.get("ok") is True) or
                 (phase == "rolled_back" and STATE.get("code_rollback") is True
                  and STATE.get("rollback_health") is not False))
    if mutated and not completed:
        return "当前事务已修改应用但未完成回滚，禁止清理备份"
    return ""


def _check_backup_lock(data_fd, lock_fd):
    _check_backup_data(data_fd)
    if not os.path.samestat(os.fstat(lock_fd), os.stat(
            "update.lock", dir_fd=data_fd, follow_symlinks=False)):
        raise RuntimeError("更新锁发生变化，拒绝清理")


def _delete_backup_dir(parent_fd, name, expected, guard=None):
    # openat/unlinkat-style traversal works on Python 3.8 too (rmtree's dir_fd
    # argument requires 3.11). Pinned fds prevent symlink swaps escaping DATA.
    if guard is None:
        def guard():
            _check_backup_data(parent_fd)
            _check_backup_entry(parent_fd, name, expected)
    guard()  # Revalidate the original DATA/top-level inode throughout recursion.
    fd = _open_backup_dir(parent_fd, name, expected)
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                guard()
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    _delete_backup_dir(fd, entry.name, info, guard)
                else:
                    _check_backup_entry(parent_fd, name, expected)
                    os.unlink(entry.name, dir_fd=fd)  # Links are unlinked, never followed.
        guard()
        _check_backup_entry(parent_fd, name, expected)
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(fd)


def list_backups():
    """Metadata only. Unknown sizes are None; reason explains incomplete totals."""
    data_fd = lock_fd = None
    items, reason = [], ""
    try:
        with _STATE_LOCK:
            data_fd = _open_backup_data()
            if data_fd is not None:
                lock_fd = _acquire_lock(data_fd=data_fd)
                if lock_fd is None:
                    reason = "更新或备份清理正在运行，禁止清理备份"
                else:
                    _check_backup_lock(data_fd, lock_fd)
            reason = reason or _backup_cleanup_reason(data_fd)
        items, _, errors = _scan_backups(data_fd)
        if data_fd is not None:
            _check_backup_data(data_fd)
        if errors:
            reason = reason or ("；".join(errors) + "；大小合计不完整")
    except (OSError, RuntimeError, ValueError, RecursionError):
        reason = "备份目录或更新状态不可读取或不安全，禁止清理备份"
    finally:
        if lock_fd is not None:
            _release_lock(lock_fd)
        if data_fd is not None:
            os.close(data_fd)
    return {"items": items, "count": len(items),
            "total_bytes": sum(item["bytes"] or 0 for item in items),
            "cleanup_allowed": not bool(reason), "reason": reason}


def cleanup_backups(mode="keep_latest", keep=1):
    """Remove only verified history under the same exclusive lock as updates."""
    if mode not in ("keep_latest", "all"):
        raise ValueError("mode 仅支持 keep_latest 或 all")
    if type(keep) is not int or not 0 <= keep <= 20:
        raise ValueError("keep 必须是 0..20 的整数")
    data_fd = lock_fd = None
    try:
        with _STATE_LOCK:
            data_fd = _open_backup_data(create=True)
            lock_fd = _acquire_lock(data_fd=data_fd)
            if lock_fd is None:
                raise RuntimeError("更新或备份清理正在运行，禁止清理备份")
            _check_backup_lock(data_fd, lock_fd)
            reason = _backup_cleanup_reason(data_fd)
            if reason:
                raise RuntimeError(reason)
        items, identities, errors = _scan_backups(data_fd)
        if errors:
            raise RuntimeError("；".join(errors) + "；未开始清理")
        retained = {item["name"] for item in [entry for entry in items if entry.get("restorable")][:keep]}
        selected = items if mode == "all" else [item for item in items if item["name"] not in retained]
        # Preflight every candidate, not merely the first one, before deleting.
        for item in selected:
            _check_backup_entry(data_fd, item["name"], identities[item["name"]])
        with _STATE_LOCK:
            _check_backup_lock(data_fd, lock_fd)
            reason = _backup_cleanup_reason(data_fd)
            if reason:
                raise RuntimeError(reason)
        deleted, freed = [], 0
        for item in selected:
            _check_backup_lock(data_fd, lock_fd)
            name = item["name"]
            if not _backup_kind(name):
                raise RuntimeError("备份名称无效，拒绝清理")
            _delete_backup_dir(data_fd, name, identities[name])
            deleted.append(name)
            freed += item["bytes"]
        _check_backup_lock(data_fd, lock_fd)
        return {"ok": True, "deleted": deleted, "freed_bytes": freed,
                "remaining": len(items) - len(deleted)}
    except (OSError, ValueError, RecursionError) as error:
        raise RuntimeError("备份目录或更新状态不可读取或发生变化，清理已停止") from error
    finally:
        if lock_fd is not None:
            _release_lock(lock_fd)
        if data_fd is not None:
            os.close(data_fd)


def _safe_extract(blob, destination):
    """Manually extract only regular files/directories; no extractall filters.

    Validate the entire archive before writing anything. Compatible with 3.8;
    rejects links, devices, duplicates, absolute/parent paths and multiple roots.
    """
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        members = []
        seen, roots, total = set(), set(), 0
        for member in archive:
            members.append(member)
            if len(members) > 20000:
                raise RuntimeError("压缩包文件过多")
            name = member.name.rstrip("/")
            path = PurePosixPath(name)
            if (not name or "\\" in name or path.is_absolute()
                    or ".." in path.parts or not path.parts
                    or not (member.isfile() or member.isdir())):
                raise RuntimeError("不安全的压缩包路径或文件类型: " + member.name)
            normalized = str(path)
            if normalized in seen:
                raise RuntimeError("压缩包重复路径: " + normalized)
            seen.add(normalized)
            roots.add(path.parts[0])
            total += member.size
            if total > MAX_EXTRACTED or member.size < 0:
                raise RuntimeError("压缩包解压内容过大")
        if len(roots) != 1:
            raise RuntimeError("压缩包必须只有一个根目录")
        root = next(iter(roots))
        for member in members:
            path = PurePosixPath(member.name.rstrip("/"))
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                # Keep execute bits, never setuid/setgid/world-writable modes.
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    src = destination / root
    if not src.is_dir():
        raise RuntimeError("压缩包根目录无效")
    return src


def _validate_source(src, expected):
    for name in ("main.py", "updater.py", "requirements.txt", "VERSION", "web/index.html"):
        if not (src / name).is_file() or not (src / name).stat().st_size:
            raise RuntimeError("缺少关键文件: " + name)
    if _valid_version((src / "VERSION").read_bytes()) != expected:
        raise RuntimeError("固定 commit 的 VERSION 与压缩包不一致")
    for path in src.rglob("*.py"):
        if path.relative_to(src).parts[0] not in SKIP_NAMES:
            # compile, not import: never execute downloaded code in preflight.
            compile(path.read_bytes(), str(path), "exec", dont_inherit=True)


def _remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _copy(src, dst):
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def _backup(src, txdir):
    entries = []
    backup = txdir / "backup"
    backup.mkdir()
    for item in sorted(src.iterdir()):
        if item.name in SKIP_NAMES:
            continue
        dst = BASE / item.name
        exists = dst.exists() or dst.is_symlink()
        if dst.is_symlink():
            raise RuntimeError("拒绝覆盖本地符号链接: " + item.name)
        entries.append({"name": item.name, "existed": exists})
        if exists:
            _copy(dst, backup / item.name)
    # Entire replaced directories are backed up, including locally added files.
    # New entries are recorded too, so rollback removes every introduced path.
    _atomic_json(txdir / "manifest.json", entries)
    return entries


def _replace(src, entries):
    for entry in entries:
        dst = BASE / entry["name"]
        _remove(dst)
        _copy(src / entry["name"], dst)


def _rollback(txdir):
    entries = json.loads((txdir / "manifest.json").read_text(encoding="utf-8"))
    for entry in entries:
        name = entry["name"]
        if Path(name).name != name or name in SKIP_NAMES or name in (".", ".."):
            raise RuntimeError("备份 manifest 路径无效")
        dst = BASE / name
        _remove(dst)
        if entry["existed"]:
            _copy(txdir / "backup" / name, dst)


def _process_identity(pid):
    try:
        # comm may contain spaces and ')'; fields after the last ')' start at #3.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]  # starttime (#22)
    except (OSError, IndexError):
        return None


def _owner_fields(thread=False):
    pid = os.getpid()
    return {"owner_pid": pid, "owner_identity": _process_identity(pid),
            "owner_tid": threading.get_native_id() if thread else None}


def _owner_alive():
    pid, identity = STATE.get("owner_pid"), STATE.get("owner_identity")
    if not pid or not identity or _process_identity(pid) != identity:
        return False
    tid = STATE.get("owner_tid")
    return not tid or Path(f"/proc/{pid}/task/{tid}").exists()


def _live_helper(txdir):
    if not txdir or not (txdir / "ready.json").exists():
        return False
    ready = json.loads((txdir / "ready.json").read_text(encoding="utf-8"))
    return bool(ready.get("identity") and
                _process_identity(ready["pid"]) == ready["identity"])


def _transaction_dir():
    name = STATE.get("transaction") or ""
    if not re.fullmatch(r"update-[A-Za-z0-9-]+", name):
        return None
    return DATA / name


def _needs_recovery(txdir):
    return bool(txdir and ((txdir / "mutation.json").exists() or STATE["phase"] in
                ("deps", "replace", "restart", "health", "rollback",
                 "recovery_handoff", "recovery_required")))


def _healthy(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tg-persona-updater-health"})
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _service_pid():
    result = subprocess.run(["systemctl", "show", SERVICE, "--property=MainPID",
                             "--value"], check=True, timeout=10,
                            stdout=subprocess.PIPE, text=True)
    return int(result.stdout.strip() or "0")


def _restart_plan():
    pid = os.getpid()
    argv = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    argv = [os.fsdecode(arg) for arg in argv]
    port = os.environ.get("PORT", "8090")
    for index, arg in enumerate(argv):
        if arg == "--port" and index + 1 < len(argv):
            port = argv[index + 1]
        elif arg.startswith("--port="):
            port = arg.split("=", 1)[1]
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise RuntimeError("无法确定原监听端口")
    url = os.environ.get("TG_PERSONA_HEALTH_URL", f"http://127.0.0.1:{port}/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError("健康检查必须使用本机 HTTP 地址")
    plan = {"active_pid": pid, "identity": _process_identity(pid),
            "python": sys.executable, "argv": argv, "cwd": os.getcwd(),
            "health_url": url}
    if Path("/run/systemd/system").is_dir():
        if not shutil.which("systemctl") or not shutil.which("systemd-run"):
            raise RuntimeError("systemd 更新需要 systemctl 和 systemd-run")
        if _service_pid() != pid:
            raise RuntimeError("服务 MainPID 与当前进程不同,不支持多 worker/未知服务")
        plan["mode"] = "systemd"
    else:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text()
        if any(token in cgroup for token in (".service", ".scope")):
            raise RuntimeError("受管理 cgroup 内不能用 detached helper;需要独立监督器")
        if not (any(Path(arg).name == "main.py" for arg in argv)
                or ("-m" in argv and "uvicorn" in argv and "main:app" in argv)):
            raise RuntimeError("无 systemd 仅支持 python main.py 或 python -m uvicorn main:app")
        if "--reload" in argv or "--workers" in argv or any(
                arg.startswith("--workers=") for arg in argv):
            raise RuntimeError("无 systemd 不支持 reload/多 worker")
        plan["mode"] = "direct"
    if not plan["identity"] or not _healthy(url):
        raise RuntimeError("原进程/健康检查不可用,未修改应用")
    return plan


def _stop_process(plan):
    pid, identity = plan["active_pid"], plan["identity"]
    if not identity or _process_identity(pid) != identity:
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while _process_identity(pid) == identity and time.monotonic() < deadline:
        time.sleep(0.2)
    if _process_identity(pid) == identity:
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while _process_identity(pid) == identity and time.monotonic() < deadline:
            time.sleep(0.2)
        if _process_identity(pid) == identity:
            raise RuntimeError("原进程无法停止")


def _restart(plan, txdir):
    """Blocking restart; Popen alone is never considered success."""
    if plan["mode"] == "systemd":
        subprocess.run(["systemctl", "restart", SERVICE], check=True, timeout=45)
        pid = _service_pid()
        if pid <= 0 or pid == plan["active_pid"]:
            raise RuntimeError("systemd 未启动新的服务进程")
    else:
        _stop_process(plan)
        # Reject unrelated listeners/left-over workers on the original port.
        if _healthy(plan["health_url"]):
            raise RuntimeError("旧端口仍被监听,拒绝启动重复实例")
        with (txdir / "application.log").open("ab") as out:
            process = subprocess.Popen(plan["argv"], cwd=plan["cwd"],
                                       start_new_session=True, stdout=out, stderr=out)
        pid = process.pid
    plan["active_pid"] = pid
    plan["identity"] = _process_identity(pid)
    _atomic_json(txdir / "plan.json", plan)
    if not plan["identity"]:
        raise RuntimeError("新进程已退出")


def _health_check(plan, timeout=60):
    deadline, consecutive = time.monotonic() + timeout, 0
    while time.monotonic() < deadline:
        alive = _process_identity(plan["active_pid"]) == plan["identity"]
        if not plan["identity"] or not alive:
            raise RuntimeError("新服务进程退出")
        if plan["mode"] == "systemd":
            result = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE],
                                    timeout=10)
            alive = result.returncode == 0 and _service_pid() == plan["active_pid"]
        consecutive = consecutive + 1 if alive and _healthy(plan["health_url"]) else 0
        if consecutive >= 3:
            return
        time.sleep(1)
    raise RuntimeError("重启后健康检查超时")


def _launch_helper(txdir, plan):
    command = [plan["python"], str(txdir / "helper.py"), "--helper", str(txdir)]
    if plan["mode"] == "systemd":
        command = ["systemd-run", "--quiet", "--collect",
                   "--unit=tg-persona-update-" + txdir.name,
                   "--property=Type=exec", "--property=RuntimeMaxSec=900"] + command
        # The unit is a sibling cgroup; permission/launch failure is pre-mutation.
        subprocess.run(command, check=True, timeout=20)
    else:
        with (txdir / "helper.log").open("ab") as out:
            subprocess.Popen(command, start_new_session=True, close_fds=True,
                             stdout=out, stderr=out)
    # Acknowledge before releasing the parent lock. The helper waits for it.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (txdir / "ready.json").exists():
            ready = json.loads((txdir / "ready.json").read_text())
            if ready["identity"] and _process_identity(ready["pid"]) == ready["identity"]:
                return
        time.sleep(0.1)
    # A late helper must see cancelled before it can acquire our lock.
    _atomic_json(txdir / "cancelled.json", {"cancelled": True})
    raise RuntimeError("独立更新监督器未就绪,未修改应用")


def _run_transaction(txdir, tx):
    restoring, restart_attempted = False, False
    try:
        src = Path(tx["source"])
        plan = json.loads((txdir / "plan.json").read_text(encoding="utf-8"))
        entries = json.loads((txdir / "manifest.json").read_text(encoding="utf-8"))
        _state(phase="deps")
        _log("依赖使用原解释器安装;失败可恢复代码/VERSION,不能完整回滚 pip 依赖")
        # Mark the limitation BEFORE pip, which can fail after partial changes.
        _state(dependency_rollback=False)
        _atomic_json(txdir / "mutation.json", {"started": True})
        restoring = True
        _pip_install(src / "requirements.txt", python=plan["python"])
        _state(phase="replace")
        _replace(src, entries)
        _state(phase="restart")
        restart_attempted = True
        _restart(plan, txdir)
        _state(phase="health")
        _health_check(plan)
        _log("重启返回值、进程和 HTTP 健康检查已通过")
        _state(running=False, ok=True, phase="done", current=current_version(),
               latest=tx["version"], available=False, code_rollback=None,
               rollback_health=None)
    except Exception as error:
        _log("更新失败: " + repr(error))
        rollback_ok, rollback_health = None, None
        if restoring:
            _state(phase="rollback")
            try:
                _rollback(txdir)
                rollback_ok = True
                _log("旧代码/VERSION 已恢复,新增文件已清理;依赖环境未完整回滚")
                if restart_attempted:
                    # Restore first, then independently restart/check the old code.
                    _restart(plan, txdir)
                    _health_check(plan)
                    rollback_health = True
                    _log("旧版本重启和健康检查通过")
            except Exception as rollback_error:
                if rollback_ok:
                    rollback_health = False
                else:
                    rollback_ok = False
                _log("回滚/旧版本重启失败,需人工处理: " + repr(rollback_error))
        _state(running=False, ok=False,
               phase="rolled_back" if rollback_ok and rollback_health is not False else "error",
               current=current_version(),
               available=_ver_tuple(tx["version"]) > _ver_tuple(current_version()),
               code_rollback=rollback_ok, rollback_health=rollback_health)


def _recover_transaction(txdir, tx):
    """A dead helper may have left partial files or an unconfirmed new process."""
    code_ok = False
    try:
        _state(phase="rollback", dependency_rollback=False)
        _log("检测到监督器/准备线程异常退出;先恢复未确认事务,不拉取另一份代码")
        _rollback(txdir)
        code_ok = True
        plan = json.loads((txdir / "plan.json").read_text(encoding="utf-8"))
        _restart(plan, txdir)
        _health_check(plan)
        _log("失主事务旧代码/VERSION 已恢复并通过健康检查;依赖未完整回滚")
        _state(running=False, ok=False, phase="rolled_back", code_rollback=True,
               rollback_health=True, current=current_version(),
               available=_ver_tuple(tx["version"]) > _ver_tuple(current_version()))
    except Exception as error:
        _log("失主事务恢复失败,需人工按 manifest 恢复或再次请求恢复: " + repr(error))
        _state(running=False, ok=False, phase="recovery_required",
               code_rollback=code_ok, rollback_health=False if code_ok else None)


def _do_recovery(fd, txdir):
    handed_off = False
    try:
        _state(**_owner_fields(thread=True))
        tx = json.loads((txdir / "transaction.json").read_text(encoding="utf-8"))
        plan = _restart_plan()  # identify the currently running app, not a reused PID
        tx["recovery"] = True
        _atomic_json(txdir / "transaction.json", tx)
        _atomic_json(txdir / "plan.json", plan)
        _atomic_json(txdir / "environment.json", dict(os.environ))
        shutil.copy2(Path(__file__), txdir / "helper.py")
        for name in ("ready.json", "cancelled.json"):
            (txdir / name).unlink(missing_ok=True)
        _state(phase="recovery_handoff")
        _launch_helper(txdir, plan)
        handed_off = True
    except Exception as error:
        if txdir:
            _atomic_json(txdir / "cancelled.json", {"cancelled": True})
        _log("无法启动失主事务恢复监督器,需人工处理/重试恢复: " + repr(error))
        _state(running=False, ok=False, phase="recovery_required")
    finally:
        try:
            if not handed_off:
                (txdir / "environment.json").unlink(missing_ok=True)
        finally:
            _release_lock(fd)


def _helper_main(txdir):
    global BASE, DATA, VERSION_FILE
    txdir = Path(txdir).resolve()
    tx = json.loads((txdir / "transaction.json").read_text(encoding="utf-8"))
    BASE = Path(tx["base"]).resolve()
    DATA, VERSION_FILE = BASE / "data", BASE / "VERSION"
    if txdir.parent != DATA or not txdir.name.startswith("update-"):
        raise RuntimeError("监督器事务目录无效")
    # Environment may contain secrets; saved with mode 0600 in a 0700 directory.
    env = json.loads((txdir / "environment.json").read_text(encoding="utf-8"))
    os.environ.clear()
    os.environ.update(env)
    _atomic_json(txdir / "ready.json", {"pid": os.getpid(),
                                       "identity": _process_identity(os.getpid())})
    fd = _acquire_lock(blocking=True)
    try:
        if (txdir / "cancelled.json").exists():
            return
        with _STATE_LOCK:
            _load_state()
        if STATE.get("transaction") != txdir.name or not STATE["running"]:
            return
        _state(**_owner_fields())
        if tx.get("recovery"):
            _recover_transaction(txdir, tx)
        else:
            _run_transaction(txdir, tx)
    finally:
        try:
            (txdir / "environment.json").unlink(missing_ok=True)
            shutil.rmtree(txdir / "source", ignore_errors=True)
        finally:
            _release_lock(fd)


def _do_update(fd, force=False):
    txdir, handed_off = None, False
    try:
        _state(**_owner_fields(thread=True))
        commit, version = _resolve_main()
        _state(latest=version, latest_commit=commit, checked_at=int(time.time()),
               available=_ver_tuple(version) > _ver_tuple(current_version()))
        if not force and not STATE["available"]:
            _log("main 没有更高版本;可使用 force 拉取同版本最新 commit")
            _state(running=False, ok=None, phase="idle")
            return
        plan = _restart_plan()  # fail closed before any application mutation
        txdir = DATA / ("update-" + uuid.uuid4().hex)
        txdir.mkdir(mode=0o700)
        _state(phase="download", transaction=txdir.name)
        url = f"https://codeload.github.com/{REPO}/tar.gz/{commit}"
        _log("下载 main 固定快照 " + commit)
        blob = _http_get(url, timeout=120)
        _state(phase="extract")
        staging = txdir / "source"
        staging.mkdir()
        src = _safe_extract(blob, staging)
        _state(phase="validate")
        _validate_source(src, version)
        _state(phase="backup")
        _backup(src, txdir)
        tx = {"base": str(BASE), "source": str(src), "version": version,
              "commit": commit}
        _atomic_json(txdir / "transaction.json", tx)
        _atomic_json(txdir / "plan.json", plan)
        _atomic_json(txdir / "environment.json", dict(os.environ))
        shutil.copy2(Path(__file__), txdir / "helper.py")
        _state(phase="handoff")
        _log("备份 manifest 位于 data/" + txdir.name + "/manifest.json")
        _launch_helper(txdir, plan)
        handed_off = True
        # Helper owns every destructive operation, survives application's restart.
    except Exception as error:
        if txdir:
            _atomic_json(txdir / "cancelled.json", {"cancelled": True})
        _log("更新准备失败(未修改应用): " + repr(error))
        _state(running=False, ok=False, phase="error")
    finally:
        # 交接时及时释放给监督器；未交接的临时目录须清理完才释放。
        try:
            if txdir and not handed_off:
                env = txdir / "environment.json"
                if env.exists():
                    env.unlink()
                shutil.rmtree(txdir / "source", ignore_errors=True)
        finally:
            _release_lock(fd)


def start_update(force=False):
    with _STATE_LOCK:
        fd = _acquire_lock()
        if fd is None:
            return snapshot()
        recovery = False
        try:
            _load_state()
            txdir = _transaction_dir()
            if STATE["running"]:
                # Lock is free, but a live acknowledged helper may be waiting
                # to acquire it. PID+starttime prevents reuse false positives.
                if _owner_alive() or _live_helper(txdir):
                    _release_lock(fd)
                    fd = None
                    return snapshot()
                recovery = _needs_recovery(txdir)
                if not recovery:
                    if txdir and txdir.exists():
                        _atomic_json(txdir / "cancelled.json", {"cancelled": True})
                    _state(running=False, ok=False, phase="error")
                    _log("失主准备事务未修改应用,解除旧状态并允许重新拉 main")
            elif STATE["phase"] == "recovery_required":
                recovery = True  # Never overwrite an unrecovered transaction.
            if recovery:
                if not txdir or not (txdir / "manifest.json").exists():
                    _state(running=False, ok=False, phase="recovery_required")
                    _log("恢复 manifest 缺失,需人工恢复;拒绝继续覆盖代码")
                    _release_lock(fd)
                    fd = None
                    return snapshot()
                _state(running=True, ok=None, phase="recovery_handoff", **_owner_fields())
                target, args = _do_recovery, (fd, txdir)
            else:
                _state(running=True, ok=None, phase="check", log=[],
                       started_at=int(time.time()), transaction=None,
                       dependency_rollback=None, code_rollback=None, rollback_health=None,
                       **_owner_fields())
                target, args = _do_update, (fd, force)
            thread = threading.Thread(target=target, args=args, daemon=True)
            thread.start()
        except Exception:
            if fd is not None:
                _release_lock(fd)
            _state(running=False, ok=False, phase="recovery_required" if recovery else "error")
            raise
        return snapshot()


def snapshot():
    with _STATE_LOCK:
        _load_state()
        result = dict(STATE)
        result["current"] = current_version()
        result["log"] = list(STATE["log"])
        return result


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--helper":
        _helper_main(sys.argv[2])
    else:
        raise SystemExit("Internal use only: updater.py --helper TRANSACTION_DIR")
