"""In-process account contexts, private storage and lifecycle ownership.

No Telegram/network dependency. A ContextVar selects an object, never swaps shared
module globals. All tasks and event callbacks retain the object that created them.
"""
import asyncio
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
import os
import re
import secrets
import shutil
import sqlite3
import time

MAX_ACCOUNTS = 20  # includes the undeletable legacy default
ACCOUNT_ID = re.compile(r"[0-9a-f]{32}\Z")
_current = ContextVar("persona_account", default=None)
_default = None


class AccountError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def valid_account_id(value):
    return value == "default" or bool(isinstance(value, str) and ACCOUNT_ID.fullmatch(value))


def secure_directory(path, create=False):
    path = Path(path)
    # Every existing ancestor is checked; do not accept an intermediate symlink.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise AccountError(400, "账号目录不允许符号链接")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise AccountError(400, "账号目录不存在或无效")
    os.chmod(path, 0o700)
    return path


def secure_files(directory):
    for name in ("config.db", "config.db-wal", "config.db-shm", "config.db-journal",
                 "tg.session", "tg.session-journal", "tg.session-wal", "tg.session-shm", "avatar.jpg"):
        path = directory / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise AccountError(400, "账号文件不允许符号链接或非普通文件")
        if path.exists():
            os.chmod(path, 0o600)


def open_database(path):
    secure_directory(Path(path).parent)
    secure_files(Path(path).parent)
    db = sqlite3.connect(path, check_same_thread=False, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        os.chmod(path, 0o600)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=8000")
    except BaseException:
        db.close()
        raise
    return db


@dataclass(eq=False)
class AccountRuntime:
    id: str
    label: str
    directory: Path
    db: sqlite3.Connection
    created_at: int
    client: object = None
    tg_phone: str = ""
    tg_state: dict = field(default_factory=lambda: {"phase": "idle", "phone": "", "api_ok": False, "me": None, "err": None})
    hits_count: dict = field(default_factory=lambda: {"total": 0})
    cache: dict = field(default_factory=lambda: {"at": 0.0, "watched": set(), "rules": []})
    dialog_cache: dict = field(default_factory=lambda: {"ts": 0.0, "groups": []})
    running: dict = field(default_factory=lambda: {"paused": False})
    last_hit_ts: dict = field(default_factory=dict)
    flock_buf: dict = field(default_factory=dict)
    auto_last_ts: dict = field(default_factory=dict)
    signin_plans: dict = field(default_factory=dict)
    poll_last: dict = field(default_factory=dict)
    auto_tasks: dict = field(default_factory=dict)
    # Queue reservations live in auto_tasks for backwards-compatible guards;
    # auto_workers contains only attempts that are actually consuming a slot.
    auto_pending: list = field(default_factory=list)
    auto_queued: set = field(default_factory=set)
    auto_workers: dict = field(default_factory=dict)
    auto_stop_requested: set = field(default_factory=set)
    auto_queue_task: object = None
    auto_queue_wakeup: object = None
    watcher_task: object = None
    signin_task: object = None
    poll_task: object = None
    keepalive_task: object = None
    housekeeping_task: object = None
    health_task: object = None
    session_alerted: bool = False
    handler_registered: bool = False
    closing: bool = False
    stopping: bool = False
    closed: bool = False
    storage_error: str = ""
    tasks: set = field(default_factory=set)
    _lifecycle_lock: object = None
    _requests_idle: object = None
    request_count: int = 0
    lock_owner: object = None

    @property
    def lifecycle_lock(self):
        # Python 3.8 binds asyncio primitives to their creation loop. Defer until
        # the first running request/startup task, not module import/uvicorn setup.
        if self._lifecycle_lock is None:
            self._lifecycle_lock = asyncio.Lock()
        return self._lifecycle_lock

    @property
    def requests_idle(self):
        if self._requests_idle is None:
            self._requests_idle = asyncio.Event()
            self._requests_idle.set()
        return self._requests_idle

    @property
    def session_path(self):
        return str(self.directory / "tg")

    @property
    def avatar_file(self):
        return self.directory / "avatar.jpg"

    def check_live(self):
        if self.closing or self.closed or self.stopping:
            raise AccountError(409, "账号正在退出或删除")
        if self.storage_error:
            raise AccountError(409, self.storage_error)
        secure_directory(self.directory)
        secure_files(self.directory)

    @asynccontextmanager
    async def request_lease(self):
        self.check_live()
        self.request_count += 1
        self.requests_idle.clear()
        try:
            with use_runtime(self):
                yield self
        finally:
            self.request_count -= 1
            if not self.request_count:
                self.requests_idle.set()

    @asynccontextmanager
    async def lifecycle(self, allow_closing=False):
        # Reentrant only for the same asyncio task, not inherited ContextVars.
        task = asyncio.current_task()
        if self.lock_owner is task:
            if not allow_closing:
                self.check_live()
            yield
            return
        async with self.lifecycle_lock:
            if not allow_closing:
                self.check_live()
            self.lock_owner = task
            try:
                with use_runtime(self):
                    yield
            finally:
                self.lock_owner = None

    def spawn(self, coroutine):
        try:
            self.check_live()
        except BaseException:
            coroutine.close()
            raise
        with use_runtime(self):
            task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def bind_event(self, callback, owner_client):
        async def bound(event):
            if self.closing or self.stopping or self.closed or self.client is not owner_client:
                return
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                with use_runtime(self):
                    await callback(event)
            finally:
                self.tasks.discard(task)
        return bound

    def clear_auto_queue(self):
        """Drop queue reservations after owned dispatcher/workers are stopped."""
        self.auto_stop_requested.update(self.auto_tasks)
        self.auto_pending[:] = []
        self.auto_queued.clear()
        self.auto_workers.clear()
        for completion in list(self.auto_tasks.values()):
            if completion is not None and not completion.done():
                completion.cancel()
        self.auto_tasks.clear()
        self.auto_stop_requested.clear()
        self.auto_queue_task = None
        if self.auto_queue_wakeup is not None:
            self.auto_queue_wakeup.set()

    async def cancel_tasks(self):
        """Cancel/await owned writers without disconnecting any account yet."""
        current = asyncio.current_task()
        pending = [task for task in self.tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def stop(self, logout=False, cancel_tasks=True, secure_storage=True):
        """Caller owns lifecycle lock; cancel writers before disconnect/DB close."""
        self.stopping = True
        try:
            if cancel_tasks:
                await self.cancel_tasks()
            old = self.client
            if old is not None:
                if logout:
                    try:
                        await old.log_out()
                    except Exception:
                        pass
                try:
                    await old.disconnect()
                except Exception:
                    pass
            self.client = None
            self.handler_registered = False
            self.clear_auto_queue()
            for name in ("watcher_task", "signin_task", "poll_task", "keepalive_task", "housekeeping_task", "health_task"):
                setattr(self, name, None)
            self.tg_phone = ""
            self.tg_state.update(phase="idle", phone="", me=None, err=None, health_error=None)
            self.dialog_cache.update(ts=0.0, groups=[])
            self.last_hit_ts.clear()
            self.auto_last_ts.clear()
            self.poll_last.clear()
            self.signin_plans.clear()
            self.flock_buf.clear()
            self.session_alerted = False
            if secure_storage and not self.storage_error:
                secure_directory(self.directory)
                secure_files(self.directory)
        finally:
            self.stopping = False


def get_runtime():
    runtime = _current.get() or _default
    if runtime is None or runtime.closed:
        raise RuntimeError("Account runtime is not available")
    return runtime


@contextmanager
def use_runtime(runtime):
    token = _current.set(runtime)
    try:
        yield runtime
    finally:
        _current.reset(token)


def account_locked(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        async with get_runtime().lifecycle():
            return await function(*args, **kwargs)
    return wrapped


class AccountRegistry:
    def __init__(self, data, root_db, initialize):
        global _default
        self.data = secure_directory(data)
        self.profiles = secure_directory(self.data / "accounts", create=True)
        self.root_db = root_db
        self.initialize = initialize
        self.accounts = {}
        self._lock = None
        self.shutting_down = False
        root_db.execute("CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, label TEXT NOT NULL, created_at INTEGER NOT NULL)")
        root_db.execute("INSERT OR IGNORE INTO accounts VALUES ('default', '默认账号', ?)", (int(time.time()),))
        root_db.commit()
        for row in root_db.execute("SELECT id,label,created_at FROM accounts ORDER BY created_at,id").fetchall():
            aid = row["id"]
            if not valid_account_id(aid):
                # Do not derive a path from a corrupt registry ID. Valid profiles
                # and the legacy root must remain available for panel recovery.
                continue
            directory = self.data if aid == "default" else self.profiles / aid
            db, problem = root_db if aid == "default" else None, ""
            if aid != "default":
                try:
                    secure_directory(directory)
                    if not (directory / "config.db").is_file():
                        raise AccountError(400, "账号数据库缺失")
                    db = open_database(directory / "config.db")
                    initialize(db)
                except (AccountError, OSError, sqlite3.Error) as error:
                    if db is not None:
                        db.close()
                    db = None
                    problem = "账号存储损坏或缺失，可安全删除，或停止服务后恢复该账号目录 (%s)" % type(error).__name__
            runtime = AccountRuntime(aid, row["label"], directory, db, row["created_at"], storage_error=problem)
            if problem:
                runtime.tg_state.update(phase="error", err=problem)
            self.accounts[aid] = runtime
        _default = self.accounts["default"]

    @property
    def lock(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def select(self, value=None, allow_broken=False):
        aid = "default" if value is None else value
        if not valid_account_id(aid):
            raise AccountError(400, "X-Account-ID 无效")
        if self.shutting_down:
            raise AccountError(409, "应用正在退出")
        runtime = self.accounts.get(aid)
        if runtime is None:
            raise AccountError(404, "账号不存在")
        if allow_broken:
            if runtime.closing or runtime.closed or runtime.stopping:
                raise AccountError(409, "账号正在退出或删除")
        else:
            runtime.check_live()
        return runtime

    @staticmethod
    def label(value):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
            raise AccountError(400, "账号名称须为 1–80 个字符")
        return value.strip()

    async def create(self, label):
        label = self.label(label)
        async with self.lock:
            if self.shutting_down:
                raise AccountError(409, "应用正在退出")
            if len(self.accounts) >= MAX_ACCOUNTS:
                raise AccountError(400, "最多支持 20 个账号")
            aid = secrets.token_hex(16)
            directory = self.profiles / aid
            # Never reuse a pre-existing directory, even if it is unregistered.
            secure_directory(self.profiles)
            directory.mkdir(mode=0o700, exist_ok=False)
            db = None
            try:
                db = open_database(directory / "config.db")
                self.initialize(db)
                runtime = AccountRuntime(aid, label, directory, db, int(time.time()))
                self.root_db.execute("INSERT INTO accounts VALUES (?,?,?)", (aid, label, runtime.created_at))
                self.root_db.commit()
                self.accounts[aid] = runtime
                return runtime
            except BaseException:
                self.root_db.rollback()
                if db is not None:
                    db.close()
                shutil.rmtree(directory)
                raise

    async def rename(self, aid, label):
        label = self.label(label)
        async with self.lock:
            runtime = self.select(aid, allow_broken=True)
            self.root_db.execute("UPDATE accounts SET label=? WHERE id=?", (label, aid))
            self.root_db.commit()
            runtime.label = label

    async def delete(self, aid):
        if aid == "default":
            raise AccountError(400, "默认账号不可删除,可退出 TG")
        async with self.lock:
            runtime = self.select(aid, allow_broken=True)
            secure_directory(self.profiles)  # never follow an unsafe parent
            runtime.closing = True
        try:
            await runtime.requests_idle.wait()
            async with runtime.lifecycle(allow_closing=True):
                await runtime.stop(secure_storage=False)
                # Unregister atomically BEFORE closing/removing files. A crash or
                # cleanup failure can leave an orphan, never a dangling live row.
                async with self.lock:
                    self.root_db.execute("DELETE FROM accounts WHERE id=?", (aid,))
                    self.root_db.commit()
                    del self.accounts[aid]
                if runtime.db is not None:
                    runtime.db.close()
                runtime.closed = True
                path = self.profiles / aid  # validated registry ID, not a user path
                try:
                    secure_directory(self.profiles)
                    if path.is_symlink():
                        path.unlink()  # remove the profile link itself, not its target
                    elif path.exists():
                        if not path.is_dir():
                            raise AccountError(400, "账号路径不是目录")
                        shutil.rmtree(path)  # does not follow child symlinks
                except (OSError, AccountError):
                    return {"ok": True, "warning": "账号已注销，残留未清理", "residual": True}
                return {"ok": True}
        except BaseException:
            if not runtime.closed:
                runtime.closing = False
                self.root_db.rollback()
            raise

    async def shutdown(self):
        self.shutting_down = True
        runtimes = list(self.accounts.values())
        for runtime in runtimes:
            runtime.closing = True
        await asyncio.gather(*(runtime.requests_idle.wait() for runtime in runtimes))
        for runtime in runtimes:
            runtime.stopping = True
        # Global staging matters: a cancelled task in B may still write/read root
        # settings while A finishes. No client/DB is closed until all writers end.
        await asyncio.gather(*(runtime.cancel_tasks() for runtime in runtimes))
        async def disconnect(runtime):
            async with runtime.lifecycle(allow_closing=True):
                await runtime.stop(cancel_tasks=False)
        try:
            results = await asyncio.gather(*(disconnect(runtime) for runtime in runtimes), return_exceptions=True)
        finally:
            for runtime in runtimes:
                if runtime.db is not None and runtime.db is not self.root_db:
                    runtime.db.close()
                runtime.closed = True
            self.root_db.close()
        for result in results:
            if isinstance(result, BaseException):
                raise result
