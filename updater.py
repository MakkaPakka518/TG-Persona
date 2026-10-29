#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TG Persona 在线更新器

被 main.py 调用,提供:
- current_version()       读取本地 VERSION
- fetch_latest()          拉取仓库 main 分支的 VERSION
- start_update()          后台下载最新 tarball、替换文件、装依赖、重启服务
- snapshot()              给前端的状态(当前/最新版本、是否可更新、进度日志)

只依赖标准库;数据目录 data/ 和虚拟环境 venv/ 不会被覆盖。
"""
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = "MakkaPakka518/TG-Persona"
BRANCH = "main"
SERVICE = "tg-persona"
BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
VERSION_FILE = BASE / "VERSION"

RAW_VERSION = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
TARBALL_URL = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{BRANCH}"

SKIP_NAMES = {"data", "venv", ".git", "__pycache__", ".pytest_cache"}

STATE = {
    "running": False,
    "ok": None,
    "current": None,
    "latest": None,
    "available": False,
    "phase": "",
    "log": [],
    "checked_at": 0,
    "started_at": 0,
}


def _log(msg):
    line = time.strftime("%H:%M:%S") + "  " + str(msg)
    STATE["log"].append(line)
    STATE["log"] = STATE["log"][-200:]


def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "tg-persona-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def current_version():
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text(encoding="utf-8").strip()
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


def fetch_latest():
    raw = _http_get(RAW_VERSION, timeout=20).decode("utf-8", "ignore").strip()
    latest = raw.splitlines()[0].strip() if raw else ""
    if not latest:
        raise RuntimeError("远端 VERSION 为空")
    cur = current_version()
    STATE["latest"] = latest
    STATE["current"] = cur
    STATE["available"] = _ver_tuple(latest) > _ver_tuple(cur)
    STATE["checked_at"] = int(time.time())
    return latest


def _pip_install(requirements):
    venv_pip = BASE / "venv" / "bin" / "pip"
    if venv_pip.exists():
        cmd = [str(venv_pip), "install", "-q", "-r", str(requirements)]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)]
    subprocess.run(cmd, check=True, timeout=300)


def _restart():
    def _try_systemd():
        if not shutil.which("systemctl"):
            return False
        try:
            # 脱离当前进程,避免被 uvicorn 退出连带杀掉
            subprocess.Popen(
                ["systemctl", "restart", SERVICE],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False

    if _try_systemd():
        _log("已发出 systemctl restart tg-persona,面板几秒后自动重连")
        return
    _log("没有 systemd,改用进程内重启(execv)")
    time.sleep(1)
    os.execv(sys.executable, [sys.executable, str(BASE / "main.py")])


def _do_update():
    STATE["running"] = True
    STATE["ok"] = None
    STATE["phase"] = "download"
    STATE["log"] = []
    STATE["started_at"] = int(time.time())
    tmpdir = None
    try:
        _log("当前版本 " + current_version())
        _log("下载最新代码 " + TARBALL_URL)
        blob = _http_get(TARBALL_URL, timeout=120)

        STATE["phase"] = "extract"
        tmpdir = Path(tempfile.mkdtemp(prefix="tgp-up-"))
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            tf.extractall(tmpdir)
        roots = [p for p in tmpdir.iterdir() if p.is_dir()]
        if not roots:
            raise RuntimeError("压缩包结构异常")
        src = roots[0]
        latest = (src / "VERSION").read_text().strip() if (src / "VERSION").exists() else current_version()
        _log("压缩包版本 " + latest)

        # 备份将被覆盖的现有文件
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = DATA / ("backup-" + stamp)
        backup.mkdir(parents=True, exist_ok=True)

        STATE["phase"] = "replace"
        for item in src.iterdir():
            if item.name in SKIP_NAMES:
                continue
            dst = BASE / item.name
            if dst.exists():
                if dst.is_dir():
                    shutil.copytree(dst, backup / item.name, dirs_exist_ok=True)
                else:
                    shutil.copy2(dst, backup / item.name)
            if dst.exists() and dst.is_dir():
                shutil.rmtree(dst)
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)
        _log("文件已替换,旧文件备份在 data/" + backup.name)

        STATE["phase"] = "deps"
        req = src / "requirements.txt"
        if req.exists():
            target_req = BASE / "requirements.txt"
            _log("检查并安装依赖")
            _pip_install(target_req)
        else:
            _log("无 requirements.txt,跳过依赖安装")

        STATE["phase"] = "restart"
        STATE["ok"] = True
        STATE["latest"] = latest
        STATE["available"] = False
        _log("更新完成,准备重启")
        _restart()
    except Exception as e:
        STATE["ok"] = False
        STATE["phase"] = "error"
        _log("更新失败:" + repr(e))
    finally:
        STATE["running"] = False
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def start_update(force=False):
    if STATE["running"]:
        return snapshot()
    if not force:
        try:
            fetch_latest()
        except Exception as e:
            _log("检查最新版本失败:" + repr(e))
    import threading
    t = threading.Thread(target=_do_update, daemon=True)
    t.start()
    return snapshot()


def snapshot():
    STATE["current"] = current_version()
    return dict(STATE)
