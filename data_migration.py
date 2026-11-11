"""Encrypted full-data migration packages and startup-safe restore transactions."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
import uuid

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"TGPMIGRATION1\0"
MAX_PACKAGE = 512 * 1024 * 1024
MAX_EXTRACTED = 1024 * 1024 * 1024
MAX_FILES = 128
ACCOUNT_ID = re.compile(r"[0-9a-f]{32}")
ROOT_FILES = {"config.db", "tg.session", "avatar.jpg"}
ACCOUNT_FILES = {"config.db", "tg.session", "avatar.jpg"}


def _key(password, salt):
    if not isinstance(password, str) or len(password) < 8 or len(password) > 256:
        raise ValueError("备份密码须为 8-256 位")
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64*1024*1024)


def _atomic_json(path, data):
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush();os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try: tmp.unlink()
        except FileNotFoundError: pass


def _sqlite_snapshot(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    src = sqlite3.connect(str(source), timeout=10)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("数据库快照校验失败")
    finally:
        dst.close();src.close()
    os.chmod(target, 0o600)


def _copy_optional(source, target):
    if not source.is_file() or source.is_symlink():
        return False
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(source, target);os.chmod(target, 0o600)
    return True


def _snapshot_file(source, target):
    if source.name.endswith(".db") or source.name.endswith(".session"):
        _sqlite_snapshot(source, target)
    else:
        _copy_optional(source, target)


def create_package(base, data, account_ids, version, password):
    work = Path(tempfile.mkdtemp(prefix=".migration-export-", dir=base))
    os.chmod(work, 0o700)
    try:
        snapshot = work / "snapshot" / "data"
        for name in ROOT_FILES:
            source = data / name
            if source.is_file(): _snapshot_file(source, snapshot / name)
        for account_id in account_ids:
            if not ACCOUNT_ID.fullmatch(account_id):
                raise ValueError("账号注册表包含无效 ID")
            directory = data / "accounts" / account_id
            if not (directory / "config.db").is_file():
                raise ValueError("账号数据不完整，无法导出")
            for name in ACCOUNT_FILES:
                source = directory / name
                if source.is_file(): _snapshot_file(source, snapshot / "accounts" / account_id / name)
        files = []
        for path in sorted(snapshot.rglob("*")):
            if path.is_file():
                rel = "data/" + path.relative_to(snapshot).as_posix()
                body = path.read_bytes()
                files.append({"path": rel, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        manifest = {"format": "tg-persona-machine-backup", "schema_version": 1,
                    "app_version": version, "created_at": int(time.time()), "files": files,
                    "accounts": len(account_ids) + 1}
        manifest_path = work / "snapshot" / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        tar_path = work / "payload.tar.gz"
        with tarfile.open(tar_path, "w:gz") as archive:
            archive.add(manifest_path, arcname="manifest.json", recursive=False)
            archive.add(snapshot, arcname="data", recursive=True)
        salt, nonce = os.urandom(16), os.urandom(12)
        package = work / ("tg-persona-migration-" + time.strftime("%Y%m%d-%H%M%S") + ".tgp")
        encryptor=Cipher(algorithms.AES(_key(password,salt)),modes.GCM(nonce)).encryptor();encryptor.authenticate_additional_data(MAGIC)
        with open(tar_path,"rb") as source,open(package,"xb") as target:
            target.write(MAGIC+salt+nonce)
            while True:
                chunk=source.read(1024*1024)
                if not chunk: break
                target.write(encryptor.update(chunk))
            target.write(encryptor.finalize());target.write(encryptor.tag)
        os.chmod(package, 0o600)
        if package.stat().st_size > MAX_PACKAGE:
            raise ValueError("迁移数据超过 512MB，请先清理历史日志或头像后重试")
        return package, work
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def _allowed(path):
    if path in ("manifest.json", "data/config.db", "data/tg.session", "data/avatar.jpg"):
        return True
    parts = path.split("/")
    return len(parts) == 4 and parts[:2] == ["data", "accounts"] and ACCOUNT_ID.fullmatch(parts[2]) and parts[3] in ACCOUNT_FILES


def stage_package(base, package_path, password, current_version):
    package_path = Path(package_path)
    package_size=package_path.stat().st_size
    if package_size > MAX_PACKAGE or package_size < len(MAGIC) + 44:
        raise ValueError("备份格式无效或超过 512MB")
    offset=len(MAGIC)
    with open(package_path,"rb") as source:
        if source.read(offset)!=MAGIC: raise ValueError("备份格式无效或超过 512MB")
        salt=source.read(16);nonce=source.read(12);source.seek(-16,os.SEEK_END);tag=source.read(16)
    try:
        decryptor=Cipher(algorithms.AES(_key(password,salt)),modes.GCM(nonce,tag)).decryptor();decryptor.authenticate_additional_data(MAGIC)
        cipher_size=package_size-offset-28-16
        restore_id = uuid.uuid4().hex
        work = Path(tempfile.mkdtemp(prefix=".migration-stage-" + restore_id + "-", dir=base));os.chmod(work,0o700)
        tar_path=work/"payload.tar.gz"
        with open(package_path,"rb") as source,open(tar_path,"xb") as target:
            source.seek(offset+28);remaining=cipher_size
            while remaining:
                chunk=source.read(min(1024*1024,remaining))
                if not chunk: raise ValueError("备份文件已截断")
                target.write(decryptor.update(chunk));remaining-=len(chunk)
            target.write(decryptor.finalize())
    except Exception:
        if 'work' in locals(): shutil.rmtree(work,ignore_errors=True)
        raise ValueError("备份密码错误或文件已损坏") from None
    try:
        root=work/"new";root.mkdir(mode=0o700)
        with tarfile.open(tar_path,"r:gz") as archive:
            total_size=0
            for count,member in enumerate(archive,1):
                if count>MAX_FILES: raise ValueError("备份文件数量超限")
                name=member.name.rstrip("/")
                if member.isdir():
                    if name not in ("data","data/accounts") and not (name.startswith("data/accounts/") and ACCOUNT_ID.fullmatch(name.split("/")[-1])): raise ValueError("备份目录无效")
                    continue
                if not member.isfile() or not _allowed(name): raise ValueError("备份包含不允许的文件")
                total_size+=member.size
                if member.size<0 or member.size>MAX_PACKAGE or total_size>MAX_EXTRACTED: raise ValueError("备份解压大小超过限制")
                target=root/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                source=archive.extractfile(member)
                if source is None: raise ValueError("备份文件无法读取")
                data=source.read(MAX_PACKAGE+1)
                if len(data)>MAX_PACKAGE: raise ValueError("备份文件过大")
                target.write_bytes(data);os.chmod(target,0o600)
        manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format")!="tg-persona-machine-backup" or manifest.get("schema_version")!=1: raise ValueError("备份清单不兼容")
        def version_tuple(value):
            match=re.fullmatch(r"(\d+)\.(\d+)\.(\d+)",str(value or ""))
            return tuple(map(int,match.groups())) if match else None
        source_version=version_tuple(manifest.get("app_version"));target_version=version_tuple(current_version)
        if not source_version or not target_version or source_version>target_version: raise ValueError("备份版本高于或不兼容当前面板，请先更新代码")
        expected={item["path"]:(item["bytes"],item["sha256"]) for item in manifest.get("files",[]) if isinstance(item,dict)}
        actual={}
        for path in (root/"data").rglob("*"):
            if path.is_file():
                data=path.read_bytes();actual["data/"+path.relative_to(root/"data").as_posix()]=(len(data),hashlib.sha256(data).hexdigest())
        if actual!=expected: raise ValueError("备份文件与清单不一致")
        root_db=root/"data/config.db"
        if not root_db.is_file(): raise ValueError("备份缺少根配置数据库")
        for path in (root/"data").rglob("*"):
            if path.is_file() and path.name.endswith((".db",".session")):
                database=sqlite3.connect(path)
                try:
                    if database.execute("PRAGMA quick_check").fetchone()[0]!="ok": raise ValueError("备份数据库损坏")
                finally: database.close()
        db=sqlite3.connect(root_db)
        try:
            if db.execute("PRAGMA quick_check").fetchone()[0]!="ok": raise ValueError("根数据库损坏")
            ids=[row[0] for row in db.execute("SELECT id FROM accounts WHERE id!='default'").fetchall()]
        finally: db.close()
        for account_id in ids:
            if not ACCOUNT_ID.fullmatch(account_id) or not (root/"data/accounts"/account_id/"config.db").is_file(): raise ValueError("账号注册表与文件不一致")
        return restore_id,{"id":restore_id,"source_version":manifest.get("app_version",""),"accounts":manifest.get("accounts",len(ids)+1),"files":len(actual),"bytes":sum(size for size,_ in actual.values())},work
    except Exception as error:
        shutil.rmtree(work,ignore_errors=True)
        if isinstance(error,ValueError): raise
        raise ValueError("备份格式无效或文件已损坏") from None


def arm_restore(base, restore_id, work):
    base=Path(base);marker=base/".migration-restore.json";work=Path(work)
    if marker.exists(): raise RuntimeError("已有迁移恢复等待执行")
    if not work.is_dir() or not work.name.startswith(".migration-stage-"+restore_id+"-"):
        raise RuntimeError("迁移暂存数据无效")
    if (base/(".data-pre-restore-"+restore_id)).exists() or (base/(".data-failed-"+restore_id)).exists():
        raise RuntimeError("迁移恢复目录冲突")
    _atomic_json(marker,{"phase":"pending","id":restore_id,"stage":work.name})


def prepare_startup_restore(base):
    base=Path(base);marker=base/".migration-restore.json"
    if not marker.is_file(): return "none"
    state=json.loads(marker.read_text(encoding="utf-8"));rid=state.get("id","");stage_name=state.get("stage","")
    if not re.fullmatch(r"[0-9a-f]{32}",rid): raise RuntimeError("迁移事务标记损坏")
    if not isinstance(stage_name,str) or Path(stage_name).name!=stage_name or not stage_name.startswith(".migration-stage-"+rid+"-"):
        raise RuntimeError("迁移事务标记损坏")
    phase=state.get("phase");old=base/(".data-pre-restore-"+rid);failed=base/(".data-failed-"+rid)
    data=base/"data";work=base/stage_name;stage=work/"new"/"data"
    if phase in ("committed","rolled_back"):
        shutil.rmtree(old if phase=="committed" else failed,ignore_errors=True);shutil.rmtree(work,ignore_errors=True);marker.unlink()
        return phase
    if phase=="pending":
        if old.exists() or failed.exists() or not stage.is_dir() or not data.is_dir(): raise RuntimeError("迁移暂存数据无效")
        _atomic_json(marker,{"phase":"swapping","id":rid,"stage":stage_name});phase="swapping"
    if phase=="swapping":
        if not old.exists():
            if not data.is_dir() or not stage.is_dir(): raise RuntimeError("迁移换入状态损坏")
            os.replace(data,old)
        if stage.exists():
            if data.exists(): raise RuntimeError("迁移换入目录冲突")
            os.replace(stage,data)
        elif not data.is_dir(): raise RuntimeError("迁移换入数据缺失")
        _atomic_json(marker,{"phase":"booting","id":rid,"stage":stage_name});return "booting"
    if phase=="booting":
        _atomic_json(marker,{"phase":"rolling_back","id":rid,"stage":stage_name});phase="rolling_back"
    if phase=="rolling_back":
        if old.exists():
            if data.exists():
                if failed.exists(): raise RuntimeError("迁移回滚目录冲突")
                os.replace(data,failed)
            elif not failed.exists(): raise RuntimeError("迁移回滚数据缺失")
            os.replace(old,data)
        elif not data.is_dir() or not failed.exists(): raise RuntimeError("迁移回滚状态损坏")
        _atomic_json(marker,{"phase":"rolled_back","id":rid,"stage":stage_name});return "rolled_back"
    raise RuntimeError("迁移事务标记损坏")


def finish_startup_restore(base):
    base=Path(base);marker=base/".migration-restore.json"
    if not marker.is_file(): return
    state=json.loads(marker.read_text(encoding="utf-8"))
    if state.get("phase")=="rolled_back":
        prepare_startup_restore(base);return
    if state.get("phase")!="booting": return
    rid=state["id"];_atomic_json(marker,{"phase":"committed","id":rid,"stage":state["stage"]})
    shutil.rmtree(base/(".data-pre-restore-"+rid),ignore_errors=True);shutil.rmtree(base/state["stage"],ignore_errors=True);marker.unlink()
