"""共享读写支持：工具/常量、读缓存代数、闸门异常、缓存与响应助手。

从 ``local_web.py`` 原样搬出（S1，2026-11-07），逻辑不改。
各模块统一 ``from . import web_support as _support``，保证
``monkeypatch.setattr(local_web, "<name>", fake)`` 能影响实现。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from life_logger.timeutil import LOCAL_TZ
from life_logger.web_common import logger

def _env_flag(name: str) -> bool:
    """W7b 诊断开关：默认全关，线上行为不变。"""
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}

def _version_badge_enabled(config_path: Path) -> bool:
    """是否在首页注入版本角标（`local-config.json` → `web_version_badge_enabled`）。

    默认开启；如需关闭，在配置里写 false，无需改代码。
    """
    try:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    value = payload.get("web_version_badge_enabled", True)
    return bool(value)

def _now() -> float:
    return time.time()

def _iso(timestamp: float | None = None) -> str:
    value = _now() if timestamp is None else timestamp
    return datetime.fromtimestamp(value, tz=LOCAL_TZ).isoformat()

def _mark_stale_read(value, note):
    """Mark a copied snapshot without changing the cached original."""
    if isinstance(value, dict):
        value["read_status"] = "stale_busy"
        value["read_note"] = note
    elif value is not None and hasattr(value, "to_dict"):
        object.__setattr__(value, "_read_status", "stale_busy")
        object.__setattr__(value, "_read_note", note)
    return value

class ControlSnapshotBusy(RuntimeError):
    """Another request owns this control key's single-flight refresh."""

    def __init__(self, key: str):
        super().__init__(f"control snapshot refresh busy for {key}")
        self.key = key

class ReadProducerBusy(RuntimeError):
    """Raised when the global read-producer budget is exhausted.

    ``stale`` carries a previously cached snapshot when one exists; routes can
    return it with a busy marker instead of hanging or returning a hard error.
    """

    def __init__(
        self,
        key: str,
        timeout: float,
        stale: Any | None = None,
        *,
        background: bool = False,
    ):
        super().__init__(f"read producer busy for {key} after {timeout:g}s")
        self.key = key
        self.timeout = timeout
        self.stale = stale
        self.background = background
        self.retry_after = max(1, int(timeout))

    @property
    def retry_after_ms(self) -> int:
        """同一个等待时间的毫秒表示（下限 250ms）。

        [2026-10-29] 前端只读 `retry_after_ms`（与 202 响应里的同名字段对齐）；
        只给秒会让 `Number(undefined)` → NaN → 兜底 800ms，服务端建议的 4s 被忽略。
        这里集中换算，避免每个 202/503 构造点各自算单位。
        """
        return max(250, int(self.retry_after) * 1000)

def _nonnegative_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default

#: 读缓存代数：任何一次写失效都会 +1，`cached_read` 把代数并进 key，
#: 于是所有 server 实例的旧结果缓存**立刻不可命中**（不需要逐实例清理）。
_READ_CACHE_EPOCH = 0
#: 按 root 的读快照代数：不同 store 根的失效互不影响（多实例/多库隔离）。
#: 只有 root=None 时才回落到全局 `_READ_CACHE_EPOCH`。
_READ_CACHE_EPOCHS: dict[str, int] = {}
_READ_CACHE_EPOCH_LOCK = threading.Lock()

def _read_epoch_key(root: Path | str | None) -> str | None:
    if root is None:
        return None
    try:
        return os.path.realpath(str(root))
    except OSError:
        return str(root)


def read_cache_epoch(root: Path | str | None) -> int:
    """当前 root 的读快照代数（用于拼缓存 key）。"""
    key = _read_epoch_key(root)
    if key is None:
        return _READ_CACHE_EPOCH
    with _READ_CACHE_EPOCH_LOCK:
        return _READ_CACHE_EPOCHS.get(key, 0)

def bump_read_cache_epoch(root: Path | str | None) -> int:
    """把某个 root 的读快照代数 +1（旧 key 立刻不可命中）。"""
    global _READ_CACHE_EPOCH
    key = _read_epoch_key(root)
    if key is None:
        with _READ_CACHE_EPOCH_LOCK:
            _READ_CACHE_EPOCH += 1
            return _READ_CACHE_EPOCH
    with _READ_CACHE_EPOCH_LOCK:
        _READ_CACHE_EPOCHS[key] = _READ_CACHE_EPOCHS.get(key, 0) + 1
        return _READ_CACHE_EPOCHS[key]

def invalidate_read_caches(root: Path | str | None, reason: str) -> None:
    """写路径统一出口：事件扫描缓存 + 日读结果缓存 一起失效。

    同进程写（事件修正/驳回/人工事件/快速抓取/Watch 正式 ASR）落盘后必须调用，
    下一次 `build_event_view()` / `/api/daily` 就能看到新文件，不靠 sleep、不等 TTL。
    跨进程写（processing_worker、synthesis 脚本、ASR 子进程）在这里管不到，
    仍受扫描缓存 10s TTL 约束（边界见 work/daily-latency-20261024/REPORT.md）。
    """
    try:
        from life_logger.event_view import invalidate_scan_cache

        invalidate_scan_cache(root)
    except Exception as error:  # 缓存失效失败不能影响写成功
        print(f"[life-logger] invalidate_scan_cache 失败({reason}): {error}", flush=True)
    # [2026-10-29 combined 修复] 原来只清事件扫描缓存 + 读缓存 epoch，
    # dashboard 的两层缓存（_COVERAGE_CACHE / _WATCH_AGG_CACHE，60s TTL）
    # 完全没动 → 人工纠正/补录后 /api/dashboard/daily 仍是旧值（用户看到"不刷新"）。
    # 这里复用 daily_dashboard 的既有失效入口（清内存 + 删磁盘 cache 文件）。
    try:
        from life_logger.daily_dashboard import invalidate_dashboard_caches

        invalidate_dashboard_caches(job_root=Path(str(root)))
    except Exception as error:  # 失效失败不能影响写成功
        print(f"[life-logger] invalidate_dashboard_caches 失败({reason}): {error}", flush=True)
    bump_read_cache_epoch(root)
    print(f"[life-logger] read-cache invalidated ({reason})", flush=True)

def _hash_path(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size

_TRANSCRIPT_PAYLOAD_CACHE: dict[str, tuple[int, int, Any]] = {}
_TRANSCRIPT_CACHE_MAX_ENTRIES = 4096

def _cached_transcript_payload(path: Path) -> Any:
    key = str(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    cached = _TRANSCRIPT_PAYLOAD_CACHE.get(key)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if len(_TRANSCRIPT_PAYLOAD_CACHE) >= _TRANSCRIPT_CACHE_MAX_ENTRIES:
        _TRANSCRIPT_PAYLOAD_CACHE.clear()
    _TRANSCRIPT_PAYLOAD_CACHE[key] = (stat.st_mtime_ns, stat.st_size, payload)
    return payload

class ResponseHelpersMixin:
    """``_Handler`` 的响应助手（原样搬出，行为不变）。"""

    RESPONSE_WRITE_TIMEOUT_SECONDS = 60.0

    def _write_body(self, body: bytes, *, label: str) -> None:
        if not body:
            return
        connection = self.connection
        expected = len(body)
        sent = 0
        previous_timeout = None
        try:
            previous_timeout = connection.gettimeout()
        except OSError:
            previous_timeout = None
        try:
            connection.settimeout(self.RESPONSE_WRITE_TIMEOUT_SECONDS)   # 阻塞写，但不会永久挂住
            view = memoryview(body)
            while sent < expected:
                try:
                    chunk = connection.send(view[sent:])
                except (BrokenPipeError, ConnectionResetError):
                    logger.info("响应写中断（客户端已断开）%s %s: %d/%d", self.command, label, sent, expected)
                    self.close_connection = True
                    return
                if not chunk:
                    break
                sent += chunk
            if sent != expected:
                self.close_connection = True
                logger.warning("响应未写完 %s %s: %d/%d 字节（Content-Length 已声明）",
                               self.command, label, sent, expected)
        except (TimeoutError, OSError) as exc:
            self.close_connection = True
            logger.warning("响应写超时/失败 %s %s: %d/%d 字节 (%s: %s)",
                           self.command, label, sent, expected, type(exc).__name__, exc)
        finally:
            try:
                connection.settimeout(previous_timeout)
            except OSError:
                pass

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        method = getattr(self, "command", "GET")
        if method in {"POST", "PUT", "PATCH"} and 200 <= int(status) < 300:
            req_path = urlparse(self.path).path
            # 手表心跳（/api/watch-status）与原始音频分段上传（/api/watch-sessions）不改变事件或账本；
            # 真正产生新转写/导入时已由 watch_formal_asr_segment / watch_session_imported 显式失效。
            if not (req_path == "/api/watch-status" or req_path.startswith("/api/watch-sessions")):
                try:
                    invalidate_read_caches(self.app.store.root, f"{method} {req_path}")
                except Exception as error:  # 失效失败不能影响写成功
                    print(f"[life-logger] invalidate 失败: {error}", flush=True)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_prepared_json(body, status)

    def _send_prepared_json(self, body: bytes, status: int = HTTPStatus.OK) -> None:
        """发送已序列化好的 JSON 字节（大响应可在调用方缓存字节，省掉重复序列化）。"""
        try:
            content_type = "application/json; charset=utf-8"
            # gzip_response_body 仍留在 local_web（S1 未搬），函数内取，避免模块级循环导入。
            from life_logger.local_web import gzip_response_body

            packed = gzip_response_body(body, content_type, self.headers.get("Accept-Encoding"))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            if packed is not None:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
                body = packed
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._write_body(body, label=urlparse(self.path).path)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开（刷新页面、切网、watch 重连）属常态，不是服务器错误。
            # 不捕获的话 socketserver 会把 traceback 刷进 stderr（实测累积 800+ 次 / 4MB）。
            self.close_connection = True


_GZIP_MIN_BYTES = 1024

_GZIP_CACHE: dict[str, bytes] = {}

_GZIP_CACHE_MAX_ENTRIES = 64

_GZIP_MIME_EXACT = {
    "application/json",
    "application/javascript",
    "application/x-javascript",
    "application/xml",
    "application/xhtml+xml",
    "image/svg+xml",
    "application/manifest+json",
}

_GZIP_MIME_PREFIXES = ("text/",)

_GZIP_SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
    ".wav", ".mp3", ".m4a", ".aac", ".mp4", ".webm", ".ogg",
    ".zip", ".gz", ".br", ".7z", ".pdf",
)

def _accepts_gzip(header_value: str | None) -> bool:
    """解析 Accept-Encoding，返回客户端是否**明确接受** gzip（q>0）。"""
    for part in str(header_value or "").split(","):
        token = part.strip().lower()
        if not token.startswith("gzip"):
            continue
        if ";" in token:
            for param in token.split(";")[1:]:
                param = param.strip()
                if param.startswith("q="):
                    try:
                        return float(param[2:]) > 0
                    except ValueError:
                        return False
        return True
    return False

def gzip_response_body(body: bytes, content_type: str, accept_encoding: str | None) -> bytes | None:
    """按规则返回 gzip 后的字节；不该压缩时返回 None（调用方原样发送）。"""
    if len(body) < _GZIP_MIN_BYTES:
        return None
    mime = str(content_type or "").split(";")[0].strip().lower()
    if mime not in _GZIP_MIME_EXACT and not mime.startswith(_GZIP_MIME_PREFIXES):
        return None
    if mime.endswith(_GZIP_SKIP_SUFFIXES):
        return None
    if not _accepts_gzip(accept_encoding):
        return None
    key = hashlib.sha1(body).hexdigest()
    cached = _GZIP_CACHE.get(key)
    if cached is not None:
        return cached
    packed = gzip.compress(body, compresslevel=6, mtime=0)
    if len(packed) >= len(body):
        return None
    if len(_GZIP_CACHE) >= _GZIP_CACHE_MAX_ENTRIES:
        _GZIP_CACHE.clear()
    _GZIP_CACHE[key] = packed
    return packed

