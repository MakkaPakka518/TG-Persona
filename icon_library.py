"""Shared, data-only icon libraries (no main, account, TG or AI imports).

Integrate with app.include_router(build_icon_router(root_get_setting,
    root_set_setting)). Callbacks MUST bind the ROOT/global settings store,
    never the current-account store: get_setting(key, default=None),
    set_setting(key, JSON_string). This module reads/writes ONLY icon_libraries.
Authentication/CSRF belong to the host app. fetch_json(url), if supplied, is a
trusted synchronous test/transport adapter returning dict, JSON bytes or text;
it runs in an executor and replaces the default DNS/network boundary.
Only the default transport guarantees DNS pinning, no proxies or redirects.
No images are fetched, metadata/descriptions are never returned or executed.
"""
import asyncio
from collections import OrderedDict
from functools import partial
import http.client
import ipaddress
import json
import re
import socket
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit, quote
from urllib.request import (HTTPHandler, HTTPSHandler, HTTPRedirectHandler,
                            ProxyHandler, Request, build_opener)
import uuid

from fastapi import APIRouter, Body, HTTPException

SETTING_KEY = "icon_libraries"
BUILTIN_ID = "builtin-tfel"
BUILTIN_URL = "https://emby-icon.vercel.app/TFEL-Emby.json"
MAX_LIBRARIES = 10
MAX_LIBRARY_ICONS = 2000
MAX_TOTAL_ICONS = 5000
MAX_JSON_BYTES = 1024 * 1024
MAX_URL_LENGTH = 2048
MAX_NAME_LENGTH = 80
MAX_ICON_NAME_LENGTH = 128
CACHE_TTL = 30 * 60
HTTP_TIMEOUT = 10


class IconError(ValueError):
    """Controlled, credential-free diagnostic; never echo remote exceptions."""


def _name(value, maximum, allow_empty=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise IconError("名称类型无效或超过长度限制")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise IconError("名称不可包含控制字符")
    value = value.strip()
    if not value and not allow_empty:
        raise IconError("名称不可为空")
    return value


def _public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise IconError("DNS 返回了无效地址")
    if (not address.is_global or address.is_multicast or address.is_reserved or
            (address.version == 4 and any(address in ipaddress.ip_network(block) for block in
                                         ("192.0.0.0/24", "192.88.99.0/24"))) or
            (address.version == 6 and address not in ipaddress.ip_network("2000::/3")) or
            getattr(address, "ipv4_mapped", None) is not None or
            getattr(address, "sixtofour", None) is not None or
            getattr(address, "teredo", None) is not None):
        raise IconError("禁止访问私网、本机、链路本地或保留地址")
    return address


def _url(value):
    if (not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH or
            any(ord(c) < 32 or ord(c) == 127 for c in value) or
            "\\" in value):
        raise IconError("URL 无效或超过 2048 字符")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (parsed.scheme.lower() not in ("http", "https") or not host or
                parsed.username is not None or parsed.password is not None or
                parsed.fragment or "%" in host):
            raise ValueError()
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        if not host or len(host) > 253 or port == 0:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise IconError("只允许不含用户信息/片段的 HTTP(S) URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Reject legacy numeric IPv4 too, including browser/OS interpretations.
        try:
            numeric = socket.inet_aton(host)
        except OSError:
            numeric = None
        if numeric is not None:
            _public_ip(socket.inet_ntoa(numeric))
            raise IconError("IP URL 必须使用标准地址写法")
        if (host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")) or
                not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                        for label in host.split("."))):
            raise IconError("禁止本地或无效主机名")
    else:
        _public_ip(str(address))
    netloc = "[" + host + "]" if ":" in host else host
    if port is not None:
        netloc += ":" + str(port)
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="%=&;/?:@!$'()*+,-._~")
    normalized = urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))
    if len(normalized) > MAX_URL_LENGTH:
        raise IconError("URL 超过 2048 字符")
    return normalized


def _resolve(url):
    parsed = urlsplit(_url(url))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        raise IconError("图标库主机 DNS 解析失败")
    if not addresses:
        raise IconError("图标库主机 DNS 没有可用地址")
    # Reject the WHOLE answer if any record is unsafe (no mixed-answer bypass).
    for family, kind, protocol, canonname, sockaddr in addresses:
        if family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM:
            raise IconError("DNS 返回了不支持的地址")
        _public_ip(sockaddr[0])
    return sorted(addresses, key=lambda item: item[0] != socket.AF_INET)


def _connector(addresses):
    def connect(address, timeout=HTTP_TIMEOUT, source_address=None):
        # Never resolve the hostname again: connect directly to a checked IP.
        family, kind, protocol, canonname, sockaddr = addresses[0]
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except BaseException:
            sock.close()
            raise
    return connect


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, addresses, **kwargs):
        super().__init__(host, **kwargs)
        self._create_connection = _connector(addresses)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, addresses, **kwargs):
        super().__init__(host, **kwargs)
        # Keep original host for TLS SNI and certificate/hostname validation.
        self._create_connection = _connector(addresses)


class _HTTPHandler(HTTPHandler):
    def __init__(self, addresses):
        super().__init__()
        self.addresses = addresses

    def http_open(self, req):
        return self.do_open(partial(_PinnedHTTPConnection, addresses=self.addresses), req)


class _HTTPSHandler(HTTPSHandler):
    def __init__(self, addresses):
        super().__init__()
        self.addresses = addresses

    def https_open(self, req):
        options = {"context": self._context}
        if hasattr(self, "_check_hostname"):  # Python 3.8-3.11 only
            options["check_hostname"] = self._check_hostname
        return self.do_open(partial(_PinnedHTTPSConnection, addresses=self.addresses), req, **options)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise IconError("不支持重定向，请使用最终 JSON 地址")


def urlopen(request, timeout, addresses):
    """Per-request stdlib opener; deliberately never install a global opener."""
    opener = build_opener(ProxyHandler({}), _HTTPHandler(addresses),
                          _HTTPSHandler(addresses), _NoRedirect())
    return opener.open(request, timeout=timeout)


def _fetch_json(url):
    url = _url(url)
    addresses = _resolve(url)
    request = Request(url, headers={"Accept": "application/json", "Accept-Encoding": "identity",
                                    "User-Agent": "TG-Persona-IconLibrary/1"}, method="GET")
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT, addresses=addresses) as response:
            if 300 <= response.getcode() < 400 or response.geturl() != url:
                raise IconError("不支持重定向，请使用最终 JSON 地址")
            if response.getcode() != 200:
                raise IconError("图标库 HTTP 请求失败")
            headers = response.headers
            if "text/html" in headers.get("Content-Type", "").lower():
                raise IconError("图标库返回 HTML，不是 JSON")
            if headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise IconError("图标库需返回未压缩 JSON")
            length = headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) < 0 or int(length) > MAX_JSON_BYTES:
                        raise IconError("图标库 JSON 超过 1MB")
                except ValueError as exc:
                    if isinstance(exc, IconError):
                        raise
                    raise IconError("图标库 Content-Length 无效")
            return response.read(MAX_JSON_BYTES + 1)
    except IconError:
        raise
    except HTTPError as exc:
        exc.close()
        if 300 <= exc.code < 400:
            raise IconError("不支持重定向，请使用最终 JSON 地址")
        raise IconError("图标库 HTTP 请求失败")
    except (URLError, OSError, ValueError):
        raise IconError("图标库下载失败或超时")


def _parse_icons(data):
    try:
        if isinstance(data, (dict, list)):
            raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        elif isinstance(data, str):
            raw = data.encode("utf-8")
        elif isinstance(data, bytes):
            raw = data
        else:
            raise IconError("图标库必须是 JSON 对象")
        if len(raw) > MAX_JSON_BYTES:
            raise IconError("图标库 JSON 超过 1MB")
        def invalid_constant(value):
            raise ValueError()
        document = json.loads(raw.decode("utf-8-sig"), parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError, TypeError) as exc:
        if isinstance(exc, IconError):
            raise
        raise IconError("图标库不是有效 JSON")
    if not isinstance(document, dict):
        raise IconError("图标库必须是 JSON 对象")
    # Only icons are extracted: external name/description/commands are ignored.
    source = document.get("icons", document)
    if isinstance(source, dict):
        source = [{key: value} for key, value in source.items()]
    if not isinstance(source, list):
        raise IconError("图标库 icons 必须是数组或名称映射")
    if len(source) > MAX_LIBRARY_ICONS:
        raise IconError("单个图标库最多 2000 个图标")
    icons = []
    for item in source:
        if not isinstance(item, dict):
            raise IconError("图标条目必须是名称与 URL 对象")
        if "name" in item and "url" in item:
            name, url = item["name"], item["url"]
        elif len(item) == 1:
            name, url = next(iter(item.items()))
        else:
            raise IconError("图标条目缺少名称或 URL")
        icons.append({"name": _name(name, MAX_ICON_NAME_LENGTH), "url": _url(url)})
    return icons


def _builtin():
    return {"id": BUILTIN_ID, "name": "TFEL", "url": BUILTIN_URL,
            "enabled": True, "builtin": True}


def _fields(body, allowed):
    if not isinstance(body, dict) or set(body) - set(allowed):
        raise HTTPException(422, "包含不支持的字段")


def build_icon_router(get_setting: Callable, set_setting: Callable,
                      fetch_json: Optional[Callable] = None) -> APIRouter:
    """Callbacks are synchronous ROOT/global settings accessors (see above).

    Cache TTL is 1800 seconds, including errors. Refresh bypasses TTL for all
    enabled libraries or the selected enabled id; disabled sources never fetch.
    Failed refresh retains cached old icons plus a controlled error. LRU cache
    and aggregated response both hold at most 5000 icons; eviction can cause an
    extra download when configured libraries collectively exceed that budget.
    """
    router = APIRouter()
    lock = None
    def library_lock():
        nonlocal lock
        if lock is None:
            lock = asyncio.Lock()
        return lock
    cache = OrderedDict()
    fetcher = _fetch_json if fetch_json is None else fetch_json

    def load():
        raw = get_setting(SETTING_KEY, None)
        if raw is None or raw == "":
            return [_builtin()]
        try:
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > 64 * 1024:
                raise IconError("无效配置")
            entries = json.loads(raw)
            if not isinstance(entries, list) or len(entries) > MAX_LIBRARIES:
                raise IconError("无效配置")
            libraries, ids, urls = [], set(), set()
            for entry in entries:
                if not isinstance(entry, dict):
                    raise IconError("无效配置")
                identifier = entry.get("id")
                if (not isinstance(identifier, str) or identifier in ids or
                        (identifier != BUILTIN_ID and not re.fullmatch(r"[0-9a-f]{32}", identifier))):
                    raise IconError("无效配置")
                url = _url(entry.get("url"))
                if url in urls or (identifier == BUILTIN_ID and url != BUILTIN_URL):
                    raise IconError("无效配置")
                enabled = entry.get("enabled", True)
                if type(enabled) is not bool:
                    raise IconError("无效配置")
                libraries.append({"id": identifier, "name": _name(entry.get("name"), MAX_NAME_LENGTH),
                                  "url": url, "enabled": enabled, "builtin": identifier == BUILTIN_ID})
                ids.add(identifier)
                urls.add(url)
            if BUILTIN_ID not in ids:
                if BUILTIN_URL in urls or len(libraries) >= MAX_LIBRARIES:
                    raise IconError("无效配置")
                libraries.insert(0, _builtin())
            return libraries
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise HTTPException(500, "全局 icon_libraries 配置无效")

    def save(libraries):
        set_setting(SETTING_KEY, json.dumps(libraries, ensure_ascii=False, separators=(",", ":")))

    def find_library(libraries, identifier):
        for library in libraries:
            if library["id"] == identifier:
                return library
        raise HTTPException(404, "图标库不存在")

    def remember(identifier, entry):
        cache.pop(identifier, None)
        while cache and sum(len(value["icons"]) for value in cache.values()) + len(entry["icons"]) > MAX_TOTAL_ICONS:
            cache.popitem(last=False)
        # Also bound negative-cache entries; deleted libraries are pruned below.
        while len(cache) >= MAX_LIBRARIES:
            cache.popitem(last=False)
        cache[identifier] = entry

    async def fetch_icons(library):
        try:
            data = await asyncio.get_running_loop().run_in_executor(None, fetcher, library["url"])
        except IconError as exc:
            return [], str(exc) if fetch_json is None else "图标库下载失败"
        except Exception:
            return [], "图标库下载失败或超时"
        try:
            return _parse_icons(data), None
        except IconError as exc:
            return [], str(exc)

    async def cached(library, force=False):
        identifier = library["id"]
        old = cache.get(identifier)
        if old and old["url"] != library["url"]:
            old = None
        if old and not force and time.monotonic() - old["at"] < CACHE_TTL:
            cache.move_to_end(identifier)
            return old
        icons, error = await fetch_icons(library)
        entry = {"url": library["url"], "icons": old["icons"] if error and old else icons,
                 "error": error, "at": time.monotonic()}
        remember(identifier, entry)
        return entry

    async def snapshot(libraries, refresh=False, identifier=None):
        valid_ids = {item["id"] for item in libraries}
        for key in list(cache):
            if key not in valid_ids:
                del cache[key]
        result = {"libraries": libraries, "icons": [], "errors": []}
        for library in libraries:
            if not library["enabled"]:
                continue
            entry = await cached(library, refresh and (identifier is None or identifier == library["id"]))
            if entry["error"]:
                result["errors"].append({"library_id": library["id"], "message": entry["error"]})
            remaining = MAX_TOTAL_ICONS - len(result["icons"])
            if len(entry["icons"]) > remaining:
                result["errors"].append({"library_id": library["id"], "message": "合计最多 5000 个图标，超出部分未显示"})
            result["icons"].extend({"name": item["name"], "url": item["url"], "library_id": library["id"]}
                                   for item in entry["icons"][:remaining])
        return result

    @router.get("/api/icon-libraries")
    async def list_libraries():
        async with library_lock():
            return {"libraries": load()}

    @router.get("/api/icons")
    async def list_icons():
        async with library_lock():
            return await snapshot(load())

    @router.post("/api/icon-libraries")
    async def add_library(body: Dict[str, Any] = Body(...)):
        _fields(body, ("name", "url", "enabled"))
        try:
            url = _url(body.get("url"))
            supplied_name = body.get("name", "")
            name = _name("" if supplied_name is None else supplied_name, MAX_NAME_LENGTH, allow_empty=True)
        except IconError as exc:
            raise HTTPException(422, str(exc))
        enabled = body.get("enabled", True)
        if type(enabled) is not bool:
            raise HTTPException(422, "enabled 必须是布尔值")
        async with library_lock():
            libraries = load()
            if len(libraries) >= MAX_LIBRARIES:
                raise HTTPException(409, "共享图标库最多 10 个（含内置）")
            if any(item["url"] == url for item in libraries):
                raise HTTPException(409, "该图标库 URL 已存在")
            library = {"id": uuid.uuid4().hex, "name": name or urlsplit(url).hostname[:MAX_NAME_LENGTH],
                       "url": url, "enabled": enabled, "builtin": False}
            # Validate before persistence, including disabled additions.
            icons, error = await fetch_icons(library)
            if error:
                raise HTTPException(400, error)
            save(libraries + [library])
            remember(library["id"], {"url": url, "icons": icons, "error": None, "at": time.monotonic()})
            return {"ok": True, "library": library}

    @router.put("/api/icon-libraries/{id}")
    async def update_library(id: str, body: Dict[str, Any] = Body(...)):
        identifier = id
        _fields(body, ("name", "enabled"))
        patch = {}
        if "enabled" in body:
            if type(body["enabled"]) is not bool:
                raise HTTPException(422, "enabled 必须是布尔值")
            patch["enabled"] = body["enabled"]
        if "name" in body:
            try:
                patch["name"] = _name(body["name"], MAX_NAME_LENGTH)
            except IconError as exc:
                raise HTTPException(422, str(exc))
        async with library_lock():
            libraries = load()
            find_library(libraries, identifier).update(patch)
            save(libraries)
            return {"ok": True}

    @router.delete("/api/icon-libraries/{id}")
    async def delete_library(id: str):
        identifier = id
        async with library_lock():
            libraries = load()
            find_library(libraries, identifier)
            if identifier == BUILTIN_ID:
                raise HTTPException(403, "内置 TFEL 图标库不可删除，可禁用")
            save([item for item in libraries if item["id"] != identifier])
            cache.pop(identifier, None)
            return {"ok": True}

    @router.post("/api/icons/refresh")
    async def refresh_icons(body: Optional[Dict[str, Any]] = Body(None)):
        body = {} if body is None else body
        _fields(body, ("id",))
        identifier = body.get("id")
        if identifier is not None and not isinstance(identifier, str):
            raise HTTPException(422, "id 必须是图标库 ID 字符串")
        async with library_lock():
            libraries = load()
            if identifier is not None:
                find_library(libraries, identifier)
            return await snapshot(libraries, refresh=True, identifier=identifier)

    return router
