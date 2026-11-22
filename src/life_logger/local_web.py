"""Small localhost-only web UI and durable job store for Life Logger.

S7 之后本模块主要保留：常量、少量仍被外部懒加载的助手、门面转发。
未使用的导入是给 ``web_*`` / ``scripts`` 的 ``from life_logger.local_web import X``
兼容再导出用的，故整文件豁免 F401。
"""

# ruff: noqa: F401

# 源码扫描兼容说明（tests/test_openclaw_task_status_route.py 直接在 local_web.py 里找
# 这些标记；实现已随 _Handler 搬到 web_http.py，这里保留一份可检索的指针）：
#   path == "/api/openclaw/tasks/status"
#   task_key 必须匹配 oc_task_[\w-]+；status 仅接受 "dismissed" / "completed"
#   校验失败返回 HTTPStatus.CONFLICT；状态写入调用 patch_task_status
#   path == "/api/openclaw/reconciliation-callback"

from __future__ import annotations

import copy
import gc
import gzip
import hashlib
import io
import json
import logging
import math
import mimetypes
mimetypes.add_type("application/x-apple-shortcut", ".shortcut")
import re
import shutil
import socket
import subprocess
import sys
import types
import uuid
import zipfile
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

from life_logger.timeutil import LOCAL_TZ, parse_ts

from life_logger.daily_brief import (
    AGENT_MIN_CONFIDENCE,
    build_daily_brief,
    default_outbox_dir,
    deliver_daily_brief,
    external_agent_brief,
    outbound_options,
)
from life_logger.dictation import (
    DashboardDictationFilter,
    build_dashboard_dictation_filter,
)
from life_logger.doctor import run_doctor
from life_logger.domain import ALLOWED_CATEGORIES, TranscriptSegment
from life_logger.extract import RuleBasedExtractor
from life_logger.ingest import AUDIO_EXTENSIONS
from life_logger.local_habits import (
    HabitNotFoundError,
    apply_habit_captures,
)
from life_logger.passport_stream import list_passport_sessions, start_passport_receiver  # noqa: F401
from life_logger.passport_sync import (
    CARD_SCHEMA_VERSION,
    deck_to_packets_hex,
    extract_passport_cards,
)
from life_logger.patterns import build_pattern_observations
from life_logger.segment import split_utterances
from life_logger.statistics import build_range_statistics
from life_logger.timecode import infer_start_time
from life_logger.model_settings import (
    VALID_STAGES,
    check_model_connectivity,
    get_global_deepseek_tiers,
    load_raw_config,
    resolve_stage_tiers,
    update_stage_config,
)
from . import web_support as _support
from . import web_auth as _auth
from . import watch_upload as _watch
from . import job_store as _jobs
from . import web_data_views as _views
from . import web_startup as _startup
from . import web_http as _http
from life_logger.watch_stream import (
    MAX_WATCH_SEGMENT_BYTES,
    WATCH_CAPTURE_SOURCE,
    IncompleteWatchSessionError,
    WatchSessionChecksumMismatchError,
    WatchSessionConflictError,
    WatchSessionError,
    WatchSessionNotFoundError,
    WatchSessionProcessingError,
)
from life_logger.web_common import (  # noqa: F401
    DEFAULT_PREFERENCES,
    DEGRADABLE_SEVERITY,
    FATAL_SEVERITY,
    PROJECT_ROOT,
    STAGE_ARTIFACTS,
    STAGE_SEVERITY,
    SUPPORTED_ASR_MODELS,
    SUPPORTED_SPEAKER_MODELS,
    _BACKUP_MAX_BYTES,
    _ERROR_LINE_PATTERN,
    _EXPENSE_RANGE_CACHE_DIRNAME,
    _MAX_STAGE_ERROR_TEXT,
    _READ_CONTRACT_SCHEMA,
    _SEARCH_CACHE_MAX_ENTRIES,
    _SEARCH_CATEGORY_ALIASES,
    _SEARCH_EVENTS_CACHE,
    _SEARCH_SOURCE_MAP_CACHE,
    _STAGE_LOG_TAIL_BYTES,
    _STAGE_SUCCESS_PATTERN,
    _TRACEBACK_MARKER,
    _atomic_json,
    _backup_previous,
    _missing_stage_artifact,
    _read_contract,
    _stage_error,
    _stage_log_text,
    _success_evidence_marker,
    _timestamp_key,
    logger,
    stage_severity,
)
from life_logger.web_preferences import (  # noqa: F401
    _desired_mobile_bind,
    _normalize_preferences,
    _normalize_trusted_network_bind,
    _read_preferences,
    _write_preferences,
)
from life_logger.web_openclaw import (  # noqa: F401
    _openclaw_task_items,
    patch_task_status,
    _openclaw_task_read_metadata,
)
from life_logger.web_search import (  # noqa: F401
    _canonical_event_import_source,
    _canonical_event_link,
    _canonical_event_result,
    _canonical_event_search,
    _canonical_search_category,
    _normalized_search_event_text,
    _search_date,
    _search_event_ngrams,
    _search_events_cached,
    _search_result_seconds,
    _search_source_map_cached,
    _semantic_search_event_duplicate,
)
from life_logger.web_health import (  # noqa: F401
    _device_offline_summary,
    _failure_evidence,
    _synthesis_staleness,
    build_daily_health,
    collect_stage_errors,
)
from life_logger.web_expense_range import (  # noqa: F401
    _expense_day_index,
    _expense_range_signature,
    build_expense_range,
)
from life_logger.web_job_helpers import (  # noqa: F401
    _safe_relative_path,
    _stream_to_file,
)
from life_logger.web_routes_watch import _WatchRoutesMixin
from life_logger.web_routes_events import _EventRoutesMixin
from life_logger.web_routes_settings import _SettingsRoutesMixin
from life_logger.web_routes_debug import _DebugRoutesMixin


WEB_ROOT = PROJECT_ROOT / "web"
RESTART_INTERRUPTED_ERROR = (
    "服务器重启或进程中断；任务会在服务恢复后自动从断点继续。"
)
SERVER_BUILD_ID = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]





#: 版本角标脚本（服务端注入，动态填充运行中进程的版本信息）。
# 为什么用内联 JS 而不是加一个 web/ 文件：避免与并发改前端的会话冲突，
# 也保证角标内容永远等于「正在跑的进程」而不是磁盘上某个静态文件。












# [2026-10-23 W7b] capture_funnel 冷算实测 1.0~1.6s，是 /api/daily 冷路径最大项。
# 它只用于「说了多少句」的展示，不影响金额/事件/台账，所以允许先返回 pending，
# 后台算完写进程内缓存；下一次请求就是准值。可用 LIFE_LOGGER_SYNC_FUNNEL=1 强制同步。


































def _parse_client_time(value: str | None, timezone_name: str) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("开始时间格式无效") from error
    # Manual wall times use Beijing; an external offset retains its instant.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.isoformat()












# 【2026-10-15】两套状态集历史上从未对齐（任务本 2026-09-21 定型、
# 提醒本 2026-09-22 定型），导致 /api/tasks/status 双写时 reminders 侧抛
# ValueError("状态无效")，而 task state 已写入 → 两个存储长期不一致。
# 这里加一层**显式双向翻译**：不改任何一侧的语义，只在跨界时换词。







#: 转写产物解析缓存：(路径 → (mtime_ns, size, payload))。
#
# 为什么需要（2026-10-19 A0 派单 P-9）：
# `_jobs.JobStore.search()` 每次请求都会把 400+ 个 transcript.json 全部读盘并 json 解析，
# 实测占搜索耗时的 80%+（cProfile: 415 次 read+json.loads ≈ 0.3s，且是在 _jobs.JobStore 锁内）。
# 这些文件一旦写入就不会再变，按 (mtime, size) 缓存解析结果即可。


#: 搜索用的「job 状态列表」缓存：避免每次搜索都读 170+ 个 status.json 并持锁。
_SEARCH_STATES_CACHE: dict[str, list[dict[str, Any]]] = {}

#: 单个 job 的「发言条」索引缓存：把 transcript 拍平成可搜索项，只建一次。


def _cached_job_states(store: "_jobs.JobStore") -> list[dict[str, Any]]:
    """缓存 _jobs.JobStore 的状态列表（按 status.json 数量 + 最新 mtime 失效）。

    注意：这里**刻意不取 _jobs.JobStore 锁**——搜索是只读场景，若与后台写任务抢锁，
    会出现「搜索卡住几十秒」（2026-10-19 P-9 的现场）。
    """
    try:
        paths = list(store.jobs_dir.glob("job-*/status.json"))
    except OSError:
        return []
    newest = 0
    for path in paths:
        try:
            newest = max(newest, path.stat().st_mtime_ns)
        except OSError:
            continue
    key = f"{store.root}|{len(paths)}|{newest}"
    cached = _SEARCH_STATES_CACHE.get(key)
    if cached is not None:
        return cached
    states: list[dict[str, Any]] = []
    for path in paths:
        try:
            states.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    states.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    if len(_SEARCH_STATES_CACHE) >= _SEARCH_CACHE_MAX_ENTRIES:
        _SEARCH_STATES_CACHE.clear()
    _SEARCH_STATES_CACHE[key] = states
    return states






#: ---- gzip 响应压缩（2026-10-19 A0 派单 P-13）--------------------------------
# 背景：`/app.js` 本地 419,521 B 未压缩；经 Cloudflare 隧道同一文件 116,239 B ——
# 说明压缩能力存在，只是**本地服务端没开**，远程访问每次都在传原始体积。
#
# 规则：
#   · 仅对文本类 MIME（js/css/html/json/svg/md/xml…）压缩；
#   · 仅当客户端 `Accept-Encoding` 明确接受 gzip（q>0）；
#   · < 1KB 不压缩（头开销大于收益）；压缩后反而更大也不压；
#   · 二进制/已压缩格式（png/jpg/wav/mp3/m4a/zip/gz）一律跳过；
#   · 结果只做**内存**缓存（按内容 sha1），不落盘。







#: 手表分段上传的读超时（秒）。2026-10-22 P-31：
# 现场 24h 内 51 次 `segment_incomplete` / 1 次 `interrupted`，
# 且服务端 HTTP/1.0 无 socket 超时——半截连接会把工作线程吊死，
# 直到对端 TCP 超时才释放。这里给上传体读取单独设上限，超时按 408 回包。
WATCH_UPLOAD_READ_TIMEOUT_SECONDS = 30.0
























# ────────────────────────────────────────── 门面（S1）
# 搬到 web_support 的名字仍要能从 local_web 读/写：
#   from life_logger.local_web import _now      → 命中 web_support._now
#   monkeypatch.setattr(local_web, "_now", f)   → 真的写进 web_support._now
# 实现代码统一用 `_support.<name>` 调用，所以 patch 会同时影响实现。
_MOVED_NAMES = {
    **{name: _support for name in (
        "_env_flag", "_version_badge_enabled", "_now", "_iso", "_mark_stale_read",
        "_nonnegative_int", "_read_epoch_key", "read_cache_epoch", "bump_read_cache_epoch",
        "invalidate_read_caches", "_hash_path", "_cached_transcript_payload",
        "ReadProducerBusy", "ControlSnapshotBusy", "ResponseHelpersMixin",
        "_READ_CACHE_EPOCH", "_READ_CACHE_EPOCHS", "_READ_CACHE_EPOCH_LOCK",
        "_TRANSCRIPT_PAYLOAD_CACHE", "_TRANSCRIPT_CACHE_MAX_ENTRIES",
    )},
    **{name: _watch for name in (
        "_parse_sleep_header", "_is_retryable_asr_slot_error", "_watch_direct_access_allowed",
        "_watch_upload_key", "_WatchUploadIncomplete", "_WatchUploadTimeout",
        "WatchUploadHelpersMixin",
    )},
    **{name: _auth for name in (
        "_read_secret_settings", "_write_secret_settings", "_ensure_mobile_access_token",
        "_mobile_capture_config", "AuthHelpersMixin",
    )},
    **{name: _http for name in (
        "_Handler", "_VERSION_BADGE_SCRIPT",
    )},
    **{name: _support for name in (
        "gzip_response_body", "_accepts_gzip", "_GZIP_MIN_BYTES", "_GZIP_CACHE",
        "_GZIP_CACHE_MAX_ENTRIES", "_GZIP_MIME_EXACT", "_GZIP_MIME_PREFIXES",
        "_GZIP_SKIP_SUFFIXES",
    )},
    **{name: _startup for name in (
        "LocalWebServer", "create_server", "serve", "_serve_runtime", "API_FEATURES",
        "_watch_last_segment_received", "_start_passport_receiver_for_web",
        "_passport_receiver_options",
    )},
    **{name: _views for name in (
        "_meaningful_event_payload", "apply_file_outcomes", "failed_file_summaries",
        "_model_catalog", "_apply_interlocutor_fields", "_capture_funnel_nonblocking",
        "_reapply_activity_sidecars", "_MEANINGFUL_WORK_EVENT_MARKERS",
        "INTERLOCUTOR_RELATION_MAP",
    )},
    **{name: _jobs for name in (
        "JobStore", "DEFAULT_JOB_ROOT", "apply_task_status", "to_task_status",
        "_job_utterance_items", "to_reminder_status", "TASK_TO_REMINDER_STATUS",
        "REMINDER_TO_TASK_STATUS", "_JOB_UTTERANCE_ITEMS_CACHE",
    )},
}


_MOVED_CLASS_ATTRS = {
    "_Handler": (_http, "_Handler"),
    **{name: (_watch.WatchUploadHelpersMixin, name) for name in (
        "_is_watch_health_probe", "_is_watch_vip_command", "_is_watch_direct_request",
        "_watch_credential_matches",
    )},
    **{name: (_auth.AuthHelpersMixin, name) for name in (
        "_request_token", "_token_matches", "_require_api_auth",
    )},
}


class _SupportFacade(types.ModuleType):
    def __getattr__(self, name):
        module = _MOVED_NAMES.get(name)
        if module is not None:
            return getattr(module, name)
        owner = _MOVED_CLASS_ATTRS.get(name)
        if owner is not None:
            return getattr(*owner)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        module = _MOVED_NAMES.get(name)
        if module is not None:
            setattr(module, name, value)
            return
        owner = _MOVED_CLASS_ATTRS.get(name)
        if owner is not None:
            setattr(owner[0], owner[1], value)
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _SupportFacade
