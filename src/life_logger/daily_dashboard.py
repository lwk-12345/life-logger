"""Daily Life Dashboard & Multi-Modal Digest Generator for Life Logger.

Synthesizes:
- 24h Timeline & Acoustic Scene Breakdown (Office, Transit, Dining, Quiet)
- Physical & Physiological Health (Heart rate, Steps, Sleep stages, Stress spikes)
- Social & Emotion Meter (Self vs Others speech ratio, Happiness index, Laughter count)
- Instant Action Cards (Captured TODOs, Expenses, Ideas)
- AI Daily Highlights & Retrospective Insight
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from life_logger.ambient_anchors import place_from_geo
from life_logger.dictation import DashboardDictationFilter
from life_logger.event_view import build_event_view
from life_logger.health_store import DailyHealthSummary, HealthDataStore
logger = logging.getLogger(__name__)

from life_logger.timeutil import LOCAL_TZ, local_clock, local_date, parse_ts
from life_logger.voice_triggers import QuickCaptureStore
from life_logger.watch_stream import WatchSessionStore


# ---------------------------------------------------------------------------
# Capture provenance projection (read-only, additive).
#
# A dashboard todo can come from a quick capture (no source fields recorded by
# older versions) or from a canonical event (which already has evidence). Keep
# only public, stable references; never leak source paths/tokens or invent IDs.
# ---------------------------------------------------------------------------
_PUBLIC_SOURCE_EVIDENCE_FIELDS = (
    "recording_id",
    "segment_id",
    "source_id",
    "source_label",
    "reported_at",
    "start_offset_seconds",
    "end_offset_seconds",
    "extractor",
    "deepseek_model",
    "source_review_required",
)
_PRIVATE_TEXT = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]+"
    r"|token\s*[=:]\s*\S+"
    r"|\b(?:sk|rk)-[A-Za-z0-9_-]{8,}\b"
    r"|\bghp_[A-Za-z0-9]{12,}\b"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|(?:^|[\s\"'])(?:/Users/|/home/|/var/|/tmp/|/private/|file://)\S*"
    r"|\\\\[A-Za-z0-9_.-]+\\[^\s\"']+"
    r"|[A-Za-z]:[\\/][^\s\"']+)"
)

def _safe_source_text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    if not text:
        return None
    return _PRIVATE_TEXT.sub("[redacted]", text)

def _safe_public_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _PUBLIC_SOURCE_EVIDENCE_FIELDS:
        raw = value.get(key)
        if raw is None or raw == "":
            continue
        if key in {"start_offset_seconds", "end_offset_seconds"}:
            if isinstance(raw, bool):
                continue
            try:
                number = float(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if number == number and number not in (float("inf"), float("-inf")):
                out[key] = number
            continue
        if key == "source_review_required":
            out[key] = bool(raw)
            continue
        text = _safe_source_text(raw)
        if text:
            out[key] = text
    return out

def _source_quote_sha256(ev: dict[str, Any], evidence: dict[str, Any]) -> str | None:
    quote = evidence.get("quote")
    if not isinstance(quote, str) or not quote:
        quote = (ev.get("time") or {}).get("quote") if isinstance(ev.get("time"), dict) else None
    if not isinstance(quote, str) or not quote:
        return None
    normalized = " ".join(quote.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

def _event_capture_provenance(ev: dict[str, Any]) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    raw_evidence = ev.get("evidence") if isinstance(ev.get("evidence"), dict) else {}
    evidence = _safe_public_evidence(raw_evidence)
    event_id = _safe_source_text(ev.get("event_id"))
    provenance = {
        "schema": "capture-provenance-v1",
        "origin": "event",
        "status": "resolved" if (event_id or evidence) else "unknown",
        "reason": None if (event_id or evidence) else "event_source_not_recorded",
        "source_event_id": event_id,
        "source_evidence": evidence,
        "recording_id": evidence.get("recording_id"),
        "segment_id": evidence.get("segment_id"),
        "source_id": evidence.get("source_id"),
        "source_label": evidence.get("source_label"),
        "reported_at": evidence.get("reported_at"),
        "quote_sha256": _source_quote_sha256(ev, raw_evidence),
        # No capture was compared here; do not claim same-utterance identity.
        "same_utterance": None,
        "identity_scope": "event_source_only",
    }
    return event_id, evidence, provenance

_CAPTURE_SOURCE_CONTRACT_FIELDS = (
    "recording_id",
    "session_id",
    "segment_id",
    "start_offset_seconds",
    "end_offset_seconds",
    "absolute_started_at",
    "absolute_ended_at",
    "time_reference",
    "time_unit",
)

def _capture_source_value(key: str, value: Any) -> Any:
    if key in {"start_offset_seconds", "end_offset_seconds"}:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return round(number, 3) if math.isfinite(number) and number >= 0 else None
    text = _safe_source_text(value)
    if text is None or text == "[redacted]":
        return None
    return text

def _normalize_capture_branch(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _CAPTURE_SOURCE_CONTRACT_FIELDS:
        value = _capture_source_value(key, raw.get(key))
        if value is not None:
            out[key] = value
    return out

def _capture_capture_provenance(item: dict[str, Any]) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    existing = item.get("capture_provenance") if isinstance(item.get("capture_provenance"), dict) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    nested_raw = metadata.get("source_provenance") if isinstance(metadata.get("source_provenance"), dict) else {}
    flat_raw = {key: metadata.get(key) for key in _CAPTURE_SOURCE_CONTRACT_FIELDS if metadata.get(key) is not None}
    evidence_raw = (
        item.get("source_evidence")
        if isinstance(item.get("source_evidence"), dict)
        else item.get("evidence")
        if isinstance(item.get("evidence"), dict)
        else {}
    )
    branches = {
        "nested": _normalize_capture_branch(nested_raw),
        "flat_metadata": _normalize_capture_branch(flat_raw),
        "source_evidence": _normalize_capture_branch(evidence_raw),
    }
    branches = {name: values for name, values in branches.items() if values}
    priority = ("nested", "flat_metadata", "source_evidence")
    selected_branch = next((name for name in priority if branches.get(name)), None)
    values: dict[str, Any] = {}
    field_sources: dict[str, str] = {}
    conflicts: list[str] = []
    conflicting_values: dict[str, dict[str, Any]] = {}
    for key in _CAPTURE_SOURCE_CONTRACT_FIELDS:
        present = {name: data[key] for name, data in branches.items() if key in data}
        distinct = {json.dumps(value, sort_keys=True, ensure_ascii=False) for value in present.values()}
        if len(distinct) > 1:
            conflicts.append(key)
            conflicting_values[key] = present
        if present:
            chosen = selected_branch if selected_branch in present else next(
                name for name in priority if name in present
            )
            values[key] = present[chosen]
            field_sources[key] = chosen
    # A legacy top-level projection is a fourth source of truth for IDs.
    existing_recording = _safe_source_text(existing.get("recording_id"))
    existing_segment = _safe_source_text(existing.get("segment_id"))
    existing_session = _safe_source_text(existing.get("session_id"))
    existing_event = _safe_source_text(existing.get("source_event_id"))
    event_id = _safe_source_text(item.get("source_event_id")) or existing_event
    for key, prior in (("recording_id", existing_recording),
                       ("segment_id", existing_segment),
                       ("session_id", existing_session)):
        if prior and values.get(key) and prior != values[key]:
            conflicts.append(key)
            conflicting_values.setdefault(key, {})["existing_projection"] = prior
    if event_id and existing_event and event_id != existing_event:
        conflicts.append("source_event_id")
        conflicting_values.setdefault("source_event_id", {})["existing_projection"] = existing_event
    conflicts = list(dict.fromkeys(conflicts))

    # Preserve the writer's contract facts separately from the projection status.
    original_status = nested_raw.get("status")
    if original_status not in {"complete", "partial", "unknown", "conflict"}:
        original_status = "partial" if branches else "unknown"
    original_missing = nested_raw.get("missing_fields")
    if not isinstance(original_missing, list):
        original_missing = [key for key in _CAPTURE_SOURCE_CONTRACT_FIELDS if key not in values]
    source_contract = {
        "schema": nested_raw.get("schema") or ("legacy-flat" if branches.get("flat_metadata") else None),
        "status": original_status,
        "missing_fields": list(original_missing),
        "time_reference": values.get("time_reference"),
        "time_unit": values.get("time_unit"),
    }

    evidence = _safe_public_evidence(evidence_raw)
    has_identity = bool(
        values.get("recording_id")
        or values.get("segment_id")
        or evidence.get("source_id")
    )
    if conflicts:
        status = "conflict"
        reason = "source_identity_conflict"
    elif has_identity:
        contract_partial = nested_raw.get("status") == "partial"
        status = "partial" if contract_partial else "resolved"
        reason = "source_contract_partial" if contract_partial else None
    elif values.get("session_id"):
        status = "partial"
        reason = "session_only_not_utterance"
    else:
        status = "unknown"
        reason = "source_not_recorded"

    provenance = {
        "schema": "capture-provenance-v1",
        "origin": "capture",
        "status": status,
        "reason": reason,
        "source_event_id": event_id,
        "source_evidence": evidence,
        "recording_id": values.get("recording_id"),
        "segment_id": values.get("segment_id"),
        "source_id": evidence.get("source_id"),
        "source_label": evidence.get("source_label"),
        "reported_at": evidence.get("reported_at"),
        "session_id": values.get("session_id"),
        "start_offset_seconds": values.get("start_offset_seconds"),
        "end_offset_seconds": values.get("end_offset_seconds"),
        "absolute_started_at": values.get("absolute_started_at"),
        "absolute_ended_at": values.get("absolute_ended_at"),
        "time_reference": values.get("time_reference"),
        "time_unit": values.get("time_unit"),
        "source_contract": source_contract,
        "source_branches": branches,
        "selected_branch": selected_branch,
        "field_sources": field_sources,
        "conflicting_values": conflicting_values,
        "quote_sha256": _source_quote_sha256(item, evidence_raw),
        # Existing unknown provenance is not silently promoted to same-utterance.
        "same_utterance": None,
        "identity_scope": "capture_only",
        "conflict_fields": conflicts,
    }
    return event_id, evidence, provenance


def _fresh_segment_place(geo: Any, segment_start: datetime) -> str | None:
    """定位点比这一段旧太多就不算数：手表常年复用旧点，否则人出门了还显示在家。"""
    if not isinstance(geo, dict) or not geo.get("timestamp"):
        return None
    try:
        located_at = datetime.fromisoformat(str(geo["timestamp"]).replace("Z", "+00:00"))
        if abs((segment_start - located_at).total_seconds()) > 15 * 60:
            return None
    except (TypeError, ValueError):
        return None
    return place_from_geo(geo)


def _scene_place(points: list[tuple[int, str]], start_min: int, end_min: int) -> str | None:
    """这段时间里出现最多的地点；没有定位就返回 None，不猜。"""
    counts: dict[str, int] = {}
    for minute, place in points:
        if start_min <= minute < end_min:
            counts[place] = counts.get(place, 0) + 1
    return max(counts, key=counts.get) if counts else None


@dataclass(frozen=True)
class DailyDashboardData:
    date: str
    total_audio_recorded_minutes: float
    scene_breakdown_percentages: dict[str, float]
    timeline_scenes: list[dict[str, Any]]
    social_ratio: dict[str, float]  # self_speech_percent, others_speech_percent；无真实语音时长数据时为空 dict
    emotion_distribution: dict[str, float]  # happy_pct, calm_pct, stressed_pct, neutral_pct；无记录时为空 dict
    laughter_count: int
    health_summary: dict[str, Any]
    captured_todos: list[dict[str, Any]]
    captured_expenses: list[dict[str, Any]]
    captured_ideas: list[dict[str, Any]]
    ai_daily_highlights: str
    updated_at: str
    # Duration fields are appended with defaults so existing callers keep working.
    # total_audio_recorded_minutes is received Watch coverage when available;
    # speech_duration_minutes always records the legacy ASR utterance total.
    audio_duration_basis: str = "asr_speech"
    received_recording_coverage_minutes: float = 0.0
    speech_duration_minutes: float = 0.0
    # 【2026-10-17】动作组（action_group）——把连续的生活动作聚成一个可命名的组，
    # 它是场景轨的「人话版本」：场景说「这是哪类环境」，动作组说「这段在干什么」。
    # 全仓此前 "action_group" 命中 0 次，Web 端也没有任何动作组标签。
    action_groups: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# [2026-10-22 P0] `list_coverage_intervals()` 会把**所有**会话的每个 segment
# 的 state.json 都读一遍（实测 9,778 次 json.loads ≈ 0.3s，且随录音时长线性增长），
# 而瀑布图只需要「某一天」的覆盖分钟数。这里按 watch-sessions 目录指纹做进程内缓存：
# 目录结构/最新 mtime 不变就直接复用，变了（新会话/新分段）才重建。
_COVERAGE_CACHE: dict[str, tuple[tuple, float, list[dict]]] = {}
_COVERAGE_TTL_SECONDS = 60.0


#: 小文件内容摘要的上限：超过这个大小只信 (mtime_ns, size)，不再整份读。
_CONTENT_DIGEST_MAX_BYTES = 8 * 1024 * 1024

# Every use still stats the input; ctime also catches writes with restored mtime.
_DASHBOARD_JSON_CACHE: dict[str, tuple[tuple[int, ...], Any]] = {}
_DASHBOARD_DIGEST_CACHE: dict[str, tuple[tuple[int, ...], str]] = {}
_DASHBOARD_INPUT_CACHE_MAX = 32768
_COVERAGE_MINUTES_CACHE: dict[tuple[str, str], tuple[tuple, float]] = {}
_MANIFEST_DAY_SEGMENTS: dict[str, tuple[Any, tuple, dict]] = {}


def _manifest_day_segments(path: Path, manifest: dict, day: str) -> list:
    """Index unchanged manifest geometry once; state contents remain checked per build."""
    key = str(path)
    zone = (time.tzname, time.timezone)
    cached = _MANIFEST_DAY_SEGMENTS.get(key)
    if cached is not None and cached[0] is manifest and cached[1] == zone:
        return cached[2].get(day, [])
    started_raw = manifest.get("started_at")
    if not started_raw:
        return []
    try:
        started = datetime.fromisoformat(started_raw)
    except Exception:
        return []
    days: dict[str, list] = {}
    fixed_local = timezone(timedelta(hours=8))
    for segment in manifest.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        offset = int(segment.get("start_offset_ms", 0))
        # Keep the original validation/evaluation order, even for non-target dates.
        int(segment.get("duration_ms", 60000))
        moment = started + timedelta(milliseconds=offset)
        local_day = moment.astimezone(fixed_local).date().isoformat()
        days.setdefault(local_day, []).append((segment.get("sequence", 0), moment, segment))
    if len(_MANIFEST_DAY_SEGMENTS) >= _MANIFEST_MEMO_MAX:
        _MANIFEST_DAY_SEGMENTS.pop(next(iter(_MANIFEST_DAY_SEGMENTS)))
    _MANIFEST_DAY_SEGMENTS[key] = (manifest, zone, days)
    return days.get(day, [])


def _read_dashboard_json(path: Path) -> Any:
    stat = path.stat()
    signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
    key = str(path)
    hit = _DASHBOARD_JSON_CACHE.get(key)
    if hit is not None and hit[0] == signature:
        return hit[1]
    value = json.loads(path.read_text(encoding="utf-8"))
    if len(_DASHBOARD_JSON_CACHE) >= _DASHBOARD_INPUT_CACHE_MAX:
        _DASHBOARD_JSON_CACHE.pop(next(iter(_DASHBOARD_JSON_CACHE)))
    _DASHBOARD_JSON_CACHE[key] = (signature, value)
    return value


def _file_content_digest(path: Path) -> tuple[int, int, str, int]:
    """(mtime_ns, size, sha256[:16], 存在标志)。

    [2026-10-29 修复] 原来只用 (mtime_ns, size)：同一纳秒内改两次、或写入方
    保留 mtime（copy/touch -r、rsync -t、某些原子写回放）时，指纹不变 → 读缓存
    把旧结果当新鲜。改成**内容摘要**后这类碰撞不再漏。
    读不到 → (0, 0, "", 0)，绝不伪称「已是最新」。
    """
    try:
        stat = path.stat()
    except OSError:
        return (0, 0, "", 0)
    digest = ""
    if stat.st_size <= _CONTENT_DIGEST_MAX_BYTES:
        try:
            signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
            key = str(path)
            cached = _DASHBOARD_DIGEST_CACHE.get(key)
            if cached is not None and cached[0] == signature:
                digest = cached[1]
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                if len(_DASHBOARD_DIGEST_CACHE) >= _DASHBOARD_INPUT_CACHE_MAX:
                    _DASHBOARD_DIGEST_CACHE.pop(next(iter(_DASHBOARD_DIGEST_CACHE)))
                _DASHBOARD_DIGEST_CACHE[key] = (signature, digest)
        except OSError:
            digest = ""
    return (int(stat.st_mtime_ns), int(stat.st_size), digest, 1)


def _scene_corrections_fingerprint(job_root: Path) -> tuple[int, int, str, int]:
    """scene-corrections.json 的 (mtime_ns, size, sha256[:16], 存在标志)。

    [2026-10-29] 这个文件此前**完全不在任何缓存键里**：人工纠正改了它，
    聚合/覆盖缓存仍当新鲜。
    """
    return _file_content_digest(job_root / "scene-corrections.json")


def _activity_timeline_fingerprint(job_root: Path) -> tuple:
    """activity-timeline/**/*.json（含 *.corrections.json）的内容指纹。

    日视图的「活动/分类/媒体」侧事实来自这些文件；它们此前不在任何 dashboard
    缓存键里。文件数是个位数量级（实测 12 个、每个 < 3MB），逐份摘要成本可接受，
    且覆盖缓存/聚合缓存自身有 60s TTL —— 不会每次请求都重算。
    """
    root = job_root / "activity-timeline"
    items: list[tuple[str, int, int, str, int]] = []
    try:
        # [2026-10-29 返工] pathlib 的 glob 会匹配点文件：`.scheduling-state.json`
        # 每 tick 被重写一次，若纳入指纹 → dashboard 缓存每 tick 必失效。
        # 只取业务数据文件（非点开头）。
        paths = sorted(p for p in root.glob("*/*.json") if not p.name.startswith("."))
    except OSError:
        paths = []
    for path in paths:
        st = _file_content_digest(path)
        # [2026-10-29] 必须带 route 目录名（clef/ vs jev/）：只记 path.name 时，
        # 两个 route 同日期文件**内容互换**后指纹不变 → 跨 route 碰撞、缓存不失效。
        items.append((f"{path.parent.name}/{path.name}", st[0], st[1], st[2], st[3]))
    return (len(items), tuple(items))


def _synthesis_fingerprint(job_root: Path) -> tuple:
    """daily-synthesis/synthesis-*.json 的内容指纹（日报摘要侧事实）。

    [2026-10-29 返工] 此前完全不在任何 dashboard 缓存键里：摘要重建后
    dashboard 的 ai_daily_highlights 只能等 TTL。文件数是个位数量级。
    """
    root = job_root / "daily-synthesis"
    items: list[tuple[str, int, int, str, int]] = []
    try:
        paths = sorted(p for p in root.glob("synthesis-*.json") if not p.name.startswith("."))
    except OSError:
        paths = []
    for path in paths:
        st = _file_content_digest(path)
        items.append((path.name, st[0], st[1], st[2], st[3]))
    return (len(items), tuple(items))


def invalidate_dashboard_caches(*, job_root: Path | None = None) -> int:
    """**写后失效入口**：把 dashboard 的两层缓存整体作废。

    [2026-10-29] 原来只有「4 个写点记得调 invalidate」，dashboard 的
    `_COVERAGE_CACHE` / `_WATCH_AGG_CACHE` 完全靠 60s TTL 兜底。现在：
      * 读侧：缓存键里带**内容摘要**（corrections / activity-timeline），改了就 miss；
      * 写侧：写 corrections / timeline / events / job status 后直接调本函数，
        下一次读立刻重建（不用等 TTL）。
    返回清掉的条目数，便于调用方/测试断言。
    """
    removed = len(_COVERAGE_CACHE) + len(_WATCH_AGG_CACHE)
    _COVERAGE_CACHE.clear()
    _WATCH_AGG_CACHE.clear()
    _WATCH_FP_MEMO.clear()
    _WATCH_SCAN_MEMO.clear()
    _COVERAGE_MINUTES_CACHE.clear()
    if job_root is None:
        _DASHBOARD_JSON_CACHE.clear()
        _DASHBOARD_DIGEST_CACHE.clear()
        _MANIFEST_DAY_SEGMENTS.clear()
    # [2026-10-30 性能] 默认不再 unlink 磁盘缓存：磁盘项自带 fingerprint，
    # 写后内容变了 → 下一次读指纹不匹配会自动重扫并覆盖；内容没变 → 直接复用。
    # 原实现每次都删，导致「写后第一次读」必然全量重扫（实测 5.852s），缓存等于没写。
    # 删除只发生在缓存自身被覆盖时（atomic replace），不在这里做。
    # 逃生开关 LIFE_LOGGER_DISK_INDEX=0 时恢复旧行为（连磁盘一起删）。
    if job_root is not None and not _disk_index_enabled():
        for name in ("watch-coverage-intervals.json", "watch-scene-aggregate.json"):
            try:
                (job_root / "cache" / name).unlink()
            except OSError:
                pass
    return removed


#: [2026-10-30 性能] 磁盘索引开关（默认开）。LIFE_LOGGER_DISK_INDEX=0 退回旧行为：
#: 只在内存 miss（hit is None）时才读磁盘缓存；用于出问题时的显式逃生。
def _disk_index_enabled() -> bool:
    raw = os.environ.get("LIFE_LOGGER_DISK_INDEX", "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


#: [2026-10-30 N1] 可选内容摘要层：LIFE_LOGGER_DISK_INDEX_DIGEST=1 时把每个 state.json
#: 的内容 sha256[:16] 也纳入指纹（实测 12,846 文件 ≈ +1.97s/次，默认关闭）。
#: 默认用 ctime 守卫（写/utime 都会改 ctime，用户态无法回写），见 _watch_tree_fingerprint。
def _hostname() -> str:
    try:
        import socket
        return socket.gethostname()
    except OSError:
        return "unknown"


def _disk_digest_enabled() -> bool:
    raw = os.environ.get("LIFE_LOGGER_DISK_INDEX_DIGEST", "0").strip().lower()
    return raw in {"1", "true", "on", "yes"}


def _watch_content_digest(watch_root: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    for state_file in sorted(watch_root.glob("*/*/formal-asr/segments/*/state.json")):
        try:
            h.update(state_file.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()[:16]


#: 指纹算法版本：结构一旦变化就 +1，避免旧磁盘缓存按错误语义命中。
_WATCH_FP_VERSION = 5  # [2026-10-30] 加入 ctime：同长度改写+utime 复位也会变
#: 同一次 build 内复用指纹（两个缓存各要一次 → 只算一次）。
_WATCH_FP_MEMO: dict[str, tuple[float, tuple]] = {}
_WATCH_FP_MEMO_TTL_SECONDS = 2.0


#: [2026-10-30 增量索引] 一次扫描的结果缓存：同一 build 里指纹与聚合共用，避免扫两遍。
_WATCH_SCAN_MEMO: dict[str, tuple[float, tuple, dict]] = {}
_WATCH_SCAN_TTL_SECONDS = 2.0

#: 分段索引：每个 state.json 的 (size, ctime_ns) → 已解析的分桶贡献。
_SEGMENT_INDEX_NAME = "watch-segment-index-v1.json"
# [Q628 2026-11-02] v2：段贡献新增「no_speech 所在日期」，旧索引无该字段 → 版本号 bump 触发全量重扫。
_SEGMENT_INDEX_VERSION = 2


def _watch_scan(watch_root: Path, *, use_memo: bool = True) -> tuple[tuple, dict]:
    """**一次** os.scandir + stat：返回 (树指纹, {相对路径: (size, ctime_ns, mtime_ns)})。

    指纹结构与 `_watch_tree_fingerprint` 完全一致（版本 4），只是顺带收集文件清单，
    供增量索引判断「哪些 state.json 变了」，避免聚合阶段再 glob + stat 一遍。
    """
    key = str(watch_root)
    now = time.monotonic()
    if use_memo:
        memo = _WATCH_SCAN_MEMO.get(key)
        if memo is not None and now - memo[0] < _WATCH_SCAN_TTL_SECONDS:
            return memo[1], memo[2]

    sessions: list[tuple] = []
    files: dict[str, tuple[int, int, int]] = {}
    try:
        devices = list(os.scandir(watch_root))
    except OSError:
        devices = []
    for device in devices:
        if not device.is_dir(follow_symlinks=False):
            continue
        try:
            session_entries = list(os.scandir(device.path))
        except OSError:
            continue
        for session in session_entries:
            if not session.is_dir(follow_symlinks=False):
                continue
            seg_dir = os.path.join(session.path, "formal-asr", "segments")
            manifest_path = os.path.join(session.path, "manifest.json")
            try:
                seg_stat = os.stat(seg_dir, follow_symlinks=False)
                seg_sig = (int(seg_stat.st_mtime_ns), int(seg_stat.st_size),
                           int(seg_stat.st_ctime_ns))
            except OSError:
                seg_sig = (0, 0, 0)
            try:
                man_stat = os.stat(manifest_path, follow_symlinks=False)
                man_sig = (int(man_stat.st_mtime_ns), int(man_stat.st_size),
                           int(man_stat.st_ctime_ns))
            except OSError:
                man_sig = (0, 0, 0)

            count = 0
            newest = 0
            total = 0
            mtime_sum = 0
            mtime_xor = 0
            ctime_sum = 0
            ctime_xor = 0
            if seg_sig != (0, 0, 0):
                try:
                    segment_dirs = list(os.scandir(seg_dir))
                except OSError:
                    segment_dirs = []
                relative_prefix = f"{device.name}/{session.name}/formal-asr/segments/"
                for segment in segment_dirs:
                    if not segment.is_dir(follow_symlinks=False):
                        continue
                    state_path = segment.path + "/state.json"
                    try:
                        state_stat = os.stat(state_path, follow_symlinks=False)
                    except OSError:
                        continue
                    mtime = int(state_stat.st_mtime_ns)
                    count += 1
                    newest = max(newest, mtime)
                    total += int(state_stat.st_size)
                    mtime_sum = (mtime_sum + mtime) & 0xFFFFFFFFFFFFFFFF
                    mtime_xor ^= mtime
                    ctime = int(state_stat.st_ctime_ns)
                    ctime_sum = (ctime_sum + ctime) & 0xFFFFFFFFFFFFFFFF
                    ctime_xor ^= ctime
                    rel = relative_prefix + segment.name + "/state.json"
                    files[rel] = (int(state_stat.st_size), ctime, mtime)

            sessions.append((session.name, seg_sig[0], seg_sig[1], seg_sig[2],
                             man_sig[0], man_sig[1], man_sig[2], count, newest, total,
                             mtime_sum, mtime_xor, ctime_sum, ctime_xor))

    sessions.sort()
    digest_part = _watch_content_digest(watch_root) if _disk_digest_enabled() else ""
    fingerprint = (_WATCH_FP_VERSION, len(sessions), tuple(sessions), digest_part)
    if use_memo:
        _WATCH_SCAN_MEMO[key] = (now, fingerprint, files)
        _WATCH_FP_MEMO[key] = (now, fingerprint)
    return fingerprint, files


def _watch_tree_fingerprint(watch_root: Path, *, use_memo: bool = True) -> tuple:
    """watch-sessions 的**分层**内容指纹（只 stat 消费者真正读的文件）。

    [2026-10-30] 实体已并入 `_watch_scan()`：一次扫描同时给出指纹与 state.json 清单，
    供增量索引复用（原实现聚合阶段还要再 glob+stat 一遍）。
    """
    return _watch_scan(watch_root, use_memo=use_memo)[0]


def _watch_content_fingerprint(watch_root: Path) -> tuple[int, int, int]:
    """向后兼容包装：返回 (state.json 文件数, 最新 mtime_ns, 总字节)。"""
    fp = _watch_tree_fingerprint(watch_root)
    count = newest = total = 0
    for item in fp[2]:
        count += item[8]
        newest = max(newest, item[9])
        total += item[10]
    return (count, newest, total)


def _fingerprint_from_json(value: Any) -> tuple:
    """把磁盘 JSON 里的指纹（嵌套 list）还原成可比较的 tuple。"""
    if isinstance(value, (list, tuple)):
        return tuple(_fingerprint_from_json(item) for item in value)
    return value


def _coverage_dir_fingerprint(watch_root: Path, *, content_fp: tuple | None = None) -> tuple:
    """录音覆盖缓存指纹：会话目录 + 树内容指纹 + scene-corrections。

    [2026-10-29 性能修复] `content_fp` 可由调用方传入，避免一次 build 里重复计算。
    """
    base = _fingerprint_dirs(watch_root)
    return (base,
            content_fp if content_fp is not None else _watch_tree_fingerprint(watch_root),
            _scene_corrections_fingerprint(watch_root.parent),
            _activity_timeline_fingerprint(watch_root.parent),
            _synthesis_fingerprint(watch_root.parent))


def _cached_coverage_intervals(watch_root: Path, *, content_fp: tuple | None = None) -> list[dict]:
    import time as _time

    key = str(watch_root)
    hit = _COVERAGE_CACHE.get(key)
    now = _time.monotonic()
    # Validate the current-build fingerprint before any cache hit.
    fingerprint = _coverage_dir_fingerprint(watch_root, content_fp=content_fp)
    if hit is not None and hit[0] == fingerprint:
        _COVERAGE_CACHE[key] = (fingerprint, now, hit[2])
        return hit[2]
    # 进程重启后第一次仍要重扫 9k+ 个 state.json（约 1.7s）。把结果按指纹落盘，
    # 重启后第一帧直接读一个小 JSON。指纹不符（有新分段/新会话）才重扫。
    disk = watch_root.parent / "cache" / "watch-coverage-intervals.json"
    if _disk_index_enabled() or hit is None:
        try:
            import json as _json

            payload = _json.loads(disk.read_text(encoding="utf-8"))
            if not payload.get("generated_at") or not payload.get("generator"):
                raise ValueError("disk cache lacks generation metadata")
            if _fingerprint_from_json(payload.get("fingerprint")) == fingerprint:
                intervals = payload.get("intervals") or []
                _COVERAGE_CACHE[key] = (fingerprint, now, intervals)
                return intervals
        except (OSError, ValueError, TypeError):
            pass
    intervals = WatchSessionStore(watch_root).list_coverage_intervals()
    _COVERAGE_CACHE[key] = (fingerprint, now, intervals)
    try:
        # 原实现用固定 `.tmp` 名：两个进程同时写会互相截断，replace 还会把对方的 inode 顶到目标位。
        from life_logger.atomic import write_json_atomic

        write_json_atomic(
            disk,
            {"fingerprint": list(fingerprint), "intervals": intervals,
             "generated_at": datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds"),
             "generator": {"pid": os.getpid(), "host": _hostname()}},
            on_durability_error="report",
        )
    except (OSError, TypeError, ValueError):
        pass  # 缓存写失败不影响主流程（语义保持不变）
    return intervals



_WATCH_AGG_CACHE: dict[str, tuple[tuple[int, ...], dict]] = {}
_WATCH_AGG_TTL_SECONDS = 60.0
#: Watch 聚合的分桶/时区策略版本。**改了按日期分桶的时区规则就要 +1**：
#: 旧磁盘缓存里是 2 元组指纹，带上策略版本后必然 miss，从而强制重算。
_WATCH_AGG_POLICY = 7  # [Q628 2026-11-02] 段贡献新增 per-day no_speech；语义变更 → 版本+1 使旧磁盘缓存必然 miss
_DINING_KW = ("咖啡", "汉堡", "吃饭", "餐", "喝", "酒", "茶", "饿", "买单")
_TRANSIT_KW = ("车", "地铁", "路上", "走路", "走去", "打车", "站")
_OFFICE_KW = ("代码", "电脑", "工作", "需求", "开发", "开会", "会议", "办公", "写代码")


def _fingerprint_dirs(root: Path) -> tuple[int, int]:
    items = list(root.glob("*/*"))
    newest = 0
    for path in items:
        try:
            newest = max(newest, int(path.stat().st_mtime))
        except OSError:
            continue
    return (len(items), newest)


def _segment_index_path(watch_root: Path) -> Path:
    return watch_root.parent / "cache" / _SEGMENT_INDEX_NAME


def _load_segment_index(path: Path) -> dict:
    """自愈：缺失/截断/非 dict/版本不符/内容异常 → 返回空索引（调用方会全量重扫）。"""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(doc, dict) or doc.get("version") != _SEGMENT_INDEX_VERSION:
        return {}
    segments = doc.get("segments")
    return segments if isinstance(segments, dict) else {}


def _save_segment_index(path: Path, segments: dict) -> None:
    """原子落盘；失败只影响下次能否复用，绝不影响本次结果。"""
    try:
        from life_logger.atomic import write_json_atomic

        write_json_atomic(path, {"version": _SEGMENT_INDEX_VERSION, "segments": segments},
                          on_durability_error="report")
    except (OSError, TypeError, ValueError):
        pass


#: manifest 解析缓存：key=(路径, mtime_ns, ctime_ns, size, inode) → (started_at, offsets)
#: [Q697 性能修复] 一个会话往往有几十上百个 no_speech 段；原实现只缓存 offsets，
#: 但 `started_at` 每次仍然整份 read_text + json.loads ⇒ 实测 7,889 次读盘、冷构建 20-25 s。
#: 现在**一次 stat 命中就把整份 manifest 只解析一次**，两样都从缓存取。
_MANIFEST_INFO_MEMO: dict[tuple, tuple[str | None, dict[int, int]]] = {}
_MANIFEST_MEMO_MAX = 1024


def _session_manifest_info(manifest: Path, pass_cache: dict | None = None) -> tuple[str | None, dict[int, int]] | None:
    """读 manifest，返回 (started_at, {sequence: start_offset_ms})；失败返回 None。

    一致性：key 带 mtime/ctime/size/inode —— 同一请求内若磁盘上的 manifest 变了，
    下次取用会重新解析（不会把旧内容用第二次）。
    淘汰策略：超过上限时丢最早的一批（不再整体 clear，避免抖动时反复重读）。
    """
    # [Q697] pass_cache：同一次聚合扫描内，同一会话只 stat 一次（7,833 段 → 73 次）
    if pass_cache is not None:
        hit = pass_cache.get(str(manifest))
        if hit is not None:
            return hit
    try:
        st = manifest.stat()
    except OSError:
        return None
    key = (str(manifest), st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
    cached = _MANIFEST_INFO_MEMO.get(key)
    if cached is not None:
        if pass_cache is not None:
            pass_cache[str(manifest)] = cached
        return cached
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    started_raw = doc.get("started_at")
    started_at = started_raw if isinstance(started_raw, str) and started_raw.strip() else None
    offsets: dict[int, int] = {}
    segments = doc.get("segments")
    if isinstance(segments, list):
        for idx, seg in enumerate(segments):
            if not isinstance(seg, dict):
                continue
            seq = seg.get("sequence")
            seq = seq if isinstance(seq, int) else idx
            try:
                offsets[int(seq)] = int(seg.get("start_offset_ms") or 0)
            except (TypeError, ValueError):
                offsets[int(seq)] = 0
    if len(_MANIFEST_INFO_MEMO) >= _MANIFEST_MEMO_MAX:
        for stale_key in list(_MANIFEST_INFO_MEMO)[: _MANIFEST_MEMO_MAX // 4]:
            _MANIFEST_INFO_MEMO.pop(stale_key, None)
    info = (started_at, offsets)
    _MANIFEST_INFO_MEMO[key] = info
    if pass_cache is not None:
        pass_cache[str(manifest)] = info
    return info


def _session_manifest_offsets(manifest: Path) -> dict[int, int] | None:
    """兼容旧调用：只要 offsets。"""
    info = _session_manifest_info(manifest)
    return info[1] if info is not None else None


def _no_speech_day(state_file: Path, state: dict, pass_cache: dict | None = None) -> str | None:
    """no_speech 段属于**哪一天**（北京日）。

    [Q628] 原来 no_speech 只做全局累加，调用方又把它加进「当天」的场景桶，
    导致任何 fallback 日都变成 home_quiet=100%（三天输出完全相同）。
    这里用「会话 started_at + 段 start_offset_ms」还原真实发生日，
    与 completed 段「按行绝对时间归桶」保持同一口径。
    """
    seg_dir = state_file.parent
    if seg_dir.parent.name != "segments":       # 只认 formal-asr/segments/NNNNNN 形态
        return None
    # .../<session>/formal-asr/segments/<NNNNNN>/state.json
    #        ↑ parents[3]              ↑ parents[1]  ↑ parents[0]
    session_dir = seg_dir.parent.parent.parent
    manifest = session_dir / "manifest.json"
    info = _session_manifest_info(manifest, pass_cache)   # [Q697] 整份 manifest 只解析一次
    if info is None:
        return None
    started_raw, offsets = info
    started = parse_ts(started_raw) if started_raw else None
    if started is None:
        return None
    seq = state.get("sequence")
    if not isinstance(seq, int):
        try:
            seq = int(seg_dir.name)
        except (TypeError, ValueError):
            return None
    offset_ms = offsets.get(seq)
    if offset_ms is None:
        return None
    return local_date(started + timedelta(milliseconds=offset_ms))


def _segment_contribution(state_file: Path, pass_cache: dict | None = None) -> dict:
    """把一个 state.json 解析成**紧凑贡献**（可 JSON 序列化）。

    返回两种形态之一：
      * ``{"n": 1}``                      —— no_speech 计数 +1
      * ``{"d": {day: [dining, transit, office, general, minutes, laughter,
                        self_ms, others_ms, emotion_unknown,
                        [happy, calm, neutral, stressed]]}}``
      * ``{}``                            —— 非 completed（不计入）
    """
    try:
        st = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(st, dict):
        return {}
    status = st.get("status")
    if status == "no_speech":
        # [Q628] 附带「这一天」，供聚合层按天归桶；解析不出日期时只留全局量。
        day = _no_speech_day(state_file, st, pass_cache)
        return {"n": 1, "day": day} if day else {"n": 1}
    if status != "completed":
        return {}
    rows = [r for r in st.get("rows", []) if isinstance(r, dict)]
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        day = local_date(row.get("absolute_started_at"))
        if not day:
            continue
        by_day.setdefault(day, []).append(row)
    out: dict[str, list] = {}
    for day, day_rows in by_day.items():
        slot = [0, 0, 0, 0, 0.0, 0, 0, 0, 0, [0, 0, 0, 0]]
        for r in day_rows:
            if "end_offset_seconds" in r or "start_offset_seconds" in r:
                dur_s = float(r.get("end_offset_seconds") or 0.0) - float(r.get("start_offset_seconds") or 0.0)
                dur_ms = int(max(0.0, dur_s) * 1000)
            else:
                dur_ms = int(r.get("end_offset_ms", 0) - r.get("start_offset_ms", 0))
            if dur_ms > 0:
                slot[4] += dur_ms / 60000.0
            src_label = str(r.get("source_label") or "").lower()
            spk = str(r.get("speaker") or "").lower()
            if src_label == "self" or "self" in spk:
                slot[6] += max(0, dur_ms)
            elif src_label in {"non_self", "unknown"} or spk:
                slot[7] += max(0, dur_ms)
            txt = str(r.get("text", ""))
            raw_emotion = r.get("emotion")
            emo = str(raw_emotion).strip().upper() if raw_emotion is not None else ""
            if "<|LAUGHTER|>" in txt or "哈哈" in txt:
                slot[5] += 1
            if not emo:
                slot[8] += 1
            elif emo in {"HAPPY", "SURPRISED"}:
                slot[9][0] += 1
            elif emo in {"ANGRY", "FEARFUL", "DISGUSTED", "SAD"}:
                slot[9][3] += 1
            else:
                slot[9][1] += 1
                slot[9][2] += 1
        combined = " ".join(str(r.get("text", "")) for r in day_rows)
        if any(k in combined for k in _DINING_KW):
            slot[0] += 1
        elif any(k in combined for k in _TRANSIT_KW):
            slot[1] += 1
        elif any(k in combined for k in _OFFICE_KW):
            slot[2] += 1
        else:
            slot[3] += 1
        out[day] = slot
    return {"d": out} if out else {}


def _watch_scene_aggregate(
    watch_root: Path, *, content_fp: tuple | None = None,
    scan_files: dict[str, tuple[int, int, int]] | None = None,
) -> dict:
    """一次扫描所有 Watch 分段，按「行所在日期」预聚合场景/时长/情绪。

    [2026-10-22 P0] 原来 `build_for_date` 为了取**某一天**的切片，
    会把全部会话的每个 `formal-asr/segments/*/state.json` 都 json 读一遍
    （实测 9,778 次读 + 解析 ≈ 1.0~1.7s，且随录音总量线性增长），
    再看每行日期是否等于目标日。这里改成「扫一次、按日期分桶」，结果按
    目录指纹缓存（内存 + 磁盘），指纹不变就直接复用。
    语义与旧实现逐项对齐：no_speech 现在**同时**给全局量与 per-day 计数
    （[Q628] 场景占比只允许用 per-day 量；全局量仅为兼容保留），
    completed 分段的行按行自身日期归桶，每段每日只记一个场景桶。
    """
    key = str(watch_root)
    now = time.monotonic()
    hit = _WATCH_AGG_CACHE.get(key)
    # Changed state/manifest must appear on the very next build, without a TTL wait.
    fingerprint = (_fingerprint_dirs(watch_root), _WATCH_AGG_POLICY,
                   content_fp if content_fp is not None else _watch_tree_fingerprint(watch_root),
                   _scene_corrections_fingerprint(watch_root.parent),
                   _activity_timeline_fingerprint(watch_root.parent),
                   _synthesis_fingerprint(watch_root.parent))
    if hit is not None and hit[0] == fingerprint:
        _WATCH_AGG_CACHE[key] = (fingerprint, now, hit[2])
        return hit[2]

    disk = watch_root.parent / "cache" / "watch-scene-aggregate.json"
    if _disk_index_enabled() or hit is None:
        try:
            payload = json.loads(disk.read_text(encoding="utf-8"))
            if not payload.get("generated_at") or not payload.get("generator"):
                raise ValueError("disk cache lacks generation metadata")
            if _fingerprint_from_json(payload.get("fingerprint")) == fingerprint:
                _WATCH_AGG_CACHE[key] = (fingerprint, now, payload["aggregate"])
                return payload["aggregate"]
        except (OSError, ValueError, TypeError, KeyError):
            pass

    no_speech = 0
    dates: dict[str, dict[str, Any]] = {}

    def bucket(day: str) -> dict[str, Any]:
        return dates.setdefault(day, {
            "dining": 0, "transit": 0, "office": 0, "general": 0,
            # [Q628] no_speech 也按天归桶：原来只有全局量，导致任何一天都被加上
            # 「全历史 no_speech 段数」，场景占比恒为 home_quiet≈100%。
            "no_speech": 0,
            "minutes": 0.0, "laughter": 0, "self_ms": 0, "others_ms": 0,
            "emotion_unknown": 0, "emotions": {"happy": 0, "calm": 0, "neutral": 0, "stressed": 0},
        })

    # [2026-10-30 增量索引] 用 _watch_scan 已经 stat 过的清单 + 磁盘索引：
    #   命中（size+ctime_ns 未变）→ 直接复用上次解析出的分桶贡献；
    #   未命中 → 只读这一个 state.json；索引缺失/损坏/落后 → 自然的全量重扫。
    if scan_files is None:
        _fingerprint_now, files = _watch_scan(watch_root, use_memo=False)
    else:
        files = scan_files
    _pass_manifest_cache: dict[str, tuple] = {}   # [Q697] 同一次聚合内 manifest 只 stat 一次
    index_path = _segment_index_path(watch_root)
    old_index = _load_segment_index(index_path)
    new_index: dict[str, dict] = {}
    dirty = False
    manifest_signatures: dict[str, list[int]] = {}

    def manifest_signature_for(rel: str) -> list[int]:
        manifest = (watch_root / rel).parents[3] / "manifest.json"
        key = str(manifest)
        if key not in manifest_signatures:
            try:
                stat = manifest.stat()
                manifest_signatures[key] = [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino]
            except OSError:
                manifest_signatures[key] = []
        return manifest_signatures[key]

    for rel, (size, ctime, _mtime) in files.items():
        cached = old_index.get(rel)
        reusable = (isinstance(cached, dict) and cached.get("s") == size
                    and cached.get("c") == ctime and isinstance(cached.get("b"), dict))
        manifest_signature = None
        if reusable and cached["b"].get("n"):
            # no_speech's day depends on manifest start/offsets, not just state.json.
            manifest_signature = manifest_signature_for(rel)
            reusable = cached.get("m") == manifest_signature
        if reusable:
            contribution = cached["b"]
        else:
            contribution = _segment_contribution(watch_root / rel, _pass_manifest_cache)
            dirty = True
        entry = {"s": size, "c": ctime, "b": contribution}
        if contribution.get("n"):
            if manifest_signature is None:
                manifest_signature = manifest_signature_for(rel)
            entry["m"] = manifest_signature
        new_index[rel] = entry
        if contribution.get("n"):
            no_speech += 1                     # 全局量：保留给兼容消费方
            ns_day = contribution.get("day")
            if isinstance(ns_day, str) and ns_day:
                bucket(ns_day)["no_speech"] += 1   # [Q628] 真正被场景占比使用的量
        for day, vals in (contribution.get("d") or {}).items():
            slot_bucket = bucket(day)
            slot_bucket["dining"] += int(vals[0])
            slot_bucket["transit"] += int(vals[1])
            slot_bucket["office"] += int(vals[2])
            slot_bucket["general"] += int(vals[3])
            slot_bucket["minutes"] += float(vals[4])
            slot_bucket["laughter"] += int(vals[5])
            slot_bucket["self_ms"] += int(vals[6])
            slot_bucket["others_ms"] += int(vals[7])
            slot_bucket["emotion_unknown"] += int(vals[8])
            emos = vals[9] if isinstance(vals[9], list) and len(vals[9]) == 4 else [0, 0, 0, 0]
            slot_bucket["emotions"]["happy"] += int(emos[0])
            slot_bucket["emotions"]["calm"] += int(emos[1])
            slot_bucket["emotions"]["neutral"] += int(emos[2])
            slot_bucket["emotions"]["stressed"] += int(emos[3])
    if dirty or len(new_index) != len(old_index):
        _save_segment_index(index_path, new_index)

    aggregate = {"no_speech": no_speech, "dates": dates}
    _publish_watch_aggregate(watch_root, fingerprint, aggregate)
    return aggregate


# [2026-10-29] watch 聚合的**单一写入口**：内存 + 磁盘两处写收敛到这里，
# 进程内用锁串行化（ThreadingHTTPServer 的读生产者是多线程，实测同 key 会被并发调用）；
# 跨进程仍靠 atomic.write_json_atomic（tmp+fsync+replace）保证不出现半文件。
_WATCH_AGG_WRITE_LOCK = threading.Lock()


def _publish_watch_aggregate(watch_root: Path, fingerprint: tuple, aggregate: dict) -> None:
    """落盘 + 内存更新的唯一入口；失败只影响缓存，不影响语义。"""
    key = str(watch_root)
    disk = watch_root.parent / "cache" / "watch-scene-aggregate.json"
    with _WATCH_AGG_WRITE_LOCK:
        _WATCH_AGG_CACHE[key] = (fingerprint, time.monotonic(), aggregate)
        try:
            from life_logger.atomic import write_json_atomic

            write_json_atomic(
                disk,
                {"fingerprint": list(fingerprint), "aggregate": aggregate,
                 "generated_at": datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds"),
                 "generator": {"pid": os.getpid(), "host": _hostname()}},
                on_durability_error="report",
            )
        except (OSError, TypeError, ValueError):
            pass  # 缓存写失败不影响主流程（语义保持不变）


class DailyDashboardBuilder:
    """Builds unified multi-modal life dashboard data for a given date."""

    def __init__(
        self,
        job_root: Path,
        *,
        dictation_filter: DashboardDictationFilter | None = None,
        use_scan_cache: bool = False,
    ) -> None:
        self.job_root = job_root
        # 生活看板的 dictation（电脑前说话）过滤器：只影响这份读模型，产物文件不动。
        # 默认 None = 不过滤（保持旧行为，测试与脚本可以直接用 builder）。
        self.dictation_filter = dictation_filter
        self.use_scan_cache = use_scan_cache
        self.health_store = HealthDataStore(self.job_root / "health")
        self.capture_store = QuickCaptureStore(self.job_root / "quick_captures.json")
        self._event_view_cache: dict[str, dict[str, Any]] = {}

    def _hide_event(self, event: Any) -> bool:
        if self.dictation_filter is None or not isinstance(event, dict):
            return False
        return self.dictation_filter.hide_event(event)

    def _dashboard_events(self, date_str: str) -> list[dict[str, Any]]:
        """Return the single canonical event read view for this builder/date."""
        view = self._event_view_cache.get(date_str)
        if view is None:
            view = build_event_view(
                self.job_root,
                date_from=date_str,
                date_to=date_str,
                scope="provisional",
                view="display",
                use_cache=self.use_scan_cache,
            )
            self._event_view_cache[date_str] = view
        return [
            event
            for event in (view.get("events") or [])
            if isinstance(event, dict) and not self._hide_event(event)
        ]

    @staticmethod
    def _event_occurrence(event: Any) -> datetime | None:
        """Return an actual occurrence time only; never substitute reported_at."""
        if not isinstance(event, dict):
            return None
        time_info = event.get("time")
        if not isinstance(time_info, dict):
            return None
        return parse_ts(time_info.get("absolute"))

    def _received_coverage_minutes(self, date_str: str, *, content_fp: tuple | None = None) -> float:
        """Union of received Watch recording intervals clipped to a local day."""
        watch_root = self.job_root / "watch-sessions"
        if not watch_root.is_dir():
            return 0.0
        cache_key = (str(watch_root), date_str)
        if content_fp is None:
            content_fp = _watch_scan(watch_root, use_memo=False)[0]
        cached = _COVERAGE_MINUTES_CACHE.get(cache_key)
        if cached is not None and cached[0] == content_fp:
            return cached[1]
        day_start = parse_ts(date_str)
        if day_start is None:
            return 0.0
        day_start = day_start.astimezone(LOCAL_TZ)
        day_end = day_start + timedelta(days=1)
        try:
            intervals = _cached_coverage_intervals(watch_root, content_fp=content_fp)
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return 0.0
        clipped: list[tuple[datetime, datetime]] = []
        for item in intervals:
            if not isinstance(item, dict):
                continue
            start = parse_ts(item.get("start_at"))
            end = parse_ts(item.get("end_at"))
            if start is None or end is None:
                continue
            start = max(start.astimezone(LOCAL_TZ), day_start)
            end = min(end.astimezone(LOCAL_TZ), day_end)
            if end > start:
                clipped.append((start, end))
        if not clipped:
            _COVERAGE_MINUTES_CACHE[cache_key] = (content_fp, 0.0)
            return 0.0
        clipped.sort(key=lambda pair: pair[0])
        total_seconds = 0.0
        current_start, current_end = clipped[0]
        for start, end in clipped[1:]:
            if start <= current_end:
                current_end = max(current_end, end)
                continue
            total_seconds += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
        total_seconds += (current_end - current_start).total_seconds()
        minutes = total_seconds / 60.0
        if len(_COVERAGE_MINUTES_CACHE) >= _DASHBOARD_INPUT_CACHE_MAX:
            _COVERAGE_MINUTES_CACHE.pop(next(iter(_COVERAGE_MINUTES_CACHE)))
        _COVERAGE_MINUTES_CACHE[cache_key] = (content_fp, minutes)
        return minutes

    def build_for_date(self, date_str: str) -> DailyDashboardData:
        # 1. 查询健康数据
        health: DailyHealthSummary = self.health_store.get_daily_summary(date_str)

        # 2. 查询即时抓取卡片
        day_captures = self.capture_store.filter_by_date(date_str)
        if self.dictation_filter is not None:
            # dictation 窗口里的即时抓取卡片不进看板（产物照旧保留）
            day_captures = self.dictation_filter.filter_captured_items(day_captures)
        projected: list[dict[str, Any]] = []
        for c in day_captures:
            item = {**c, "_origin": "capture"}
            source_event_id, source_evidence, provenance = _capture_capture_provenance(item)
            if source_event_id:
                item["source_event_id"] = source_event_id
            if source_evidence:
                item["source_evidence"] = source_evidence
            item["capture_provenance"] = provenance
            projected.append(item)
        todos = [item for item in projected if item.get("trigger_type") in {"todo", "query"}]
        expenses = [item for item in projected if item.get("trigger_type") == "expense"]
        ideas = [item for item in projected if item.get("trigger_type") == "idea"]

        seen_todo_texts = {str(c.get("extracted_content") or "").strip() for c in todos}
        # 【2026-10-17】即时抓取 vs 结构化便签事件的重复卡。
        # 同一句话会同时产出两样东西：voice_triggers 的 quick capture（原文片段）
        # 与 DeepSeek 的 pinned memo 事件（带标题的精炼版）。看板两个都显示，
        # 于是同一分钟出现两张内容相近的卡（实测 10:01「是什么情况，然后有没有
        # 什么更好的一个方案」+「检查当前情况并寻找更好的方案」）。
        # 策略：结构化事件更可信 → 记下它的分钟，同分钟的 quick capture 让位。
        pinned_minutes: set[str] = set()
        for ev in self._dashboard_events(date_str):
            ev_info = ev.get("evidence") if isinstance(ev.get("evidence"), dict) else {}
            is_pm = bool(ev.get("pinned_memo") or ev_info.get("pinned_memo"))
            is_qd = bool(ev.get("query_dispatch") or ev_info.get("query_dispatch"))
            if not is_pm and not is_qd:
                continue
            # Only an actual occurrence places this card on a minute. An unknown
            # occurrence may still be organised by its reported date in the shared
            # view, but we must not turn reported_at into a fake occurrence.
            occurrence = self._event_occurrence(ev)
            ts = (
                str((ev.get("time") or {}).get("absolute") or "")
                if occurrence is not None
                else ""
            )
            if occurrence is not None and local_date(occurrence) != date_str:
                continue
            title = str(ev.get("title") or "").strip()
            content = title
            for prefix in ("便签：", "查询：", "便签:", "查询:"):
                if content.startswith(prefix):
                    content = content[len(prefix):].strip()
            if not content or content in seen_todo_texts:
                continue
            seen_todo_texts.add(content)
            if occurrence is not None:
                pinned_minutes.add(
                    f"{local_date(occurrence)} {local_clock(occurrence)}"
                )
            source_event_id, source_evidence, provenance = _event_capture_provenance(ev)
            item = {
                "_origin": "event",
                "trigger_type": "query" if is_qd and not is_pm else "todo",
                "raw_text": str(ev_info.get("quote") or title),
                "extracted_content": content,
                "captured_at": ts or None,
                "confidence": float(ev.get("confidence") or 0.95),
                "source_event_id": source_event_id,
                "source_evidence": source_evidence,
                "capture_provenance": provenance,
            }
            todos.append(item)

        # 【2026-10-17】结构化便签事件优先：同分钟内让 quick capture 让位，
        # 避免一句话在看板上出两张卡（原文碎片 + 精炼标题）。
        if pinned_minutes:
            deduped: list[dict[str, Any]] = []
            for item in todos:
                if item.get("_origin") == "capture":
                    raw_ts = str(item.get("captured_at") or "")
                    clock = local_clock(raw_ts)
                    day = local_date(raw_ts)
                    minute_key = f"{day} {clock}" if day and clock else None
                    if minute_key and minute_key in pinned_minutes:
                        continue
                deduped.append(item)
            todos = deduped

        # 3. 汇总音频与场景数据（扫描对应日期的 job 产物）
        scene_counts: dict[str, int] = {
            "office": 0,
            "transit": 0,
            "dining": 0,
            "home_quiet": 0,
            "general": 0,
        }
        total_minutes = 0.0
        laughter_count = 0
        self_speech_ms = 0
        others_speech_ms = 0

        jobs_dir = self.job_root / "jobs"
        emotion_counts: dict[str, int] = {"happy": 0, "calm": 0, "stressed": 0, "neutral": 0}
        # 【2026-10-14】缺 emotion 的记录单独计数，不混进情绪分布（否则「无数据」会被
        # 伪装成 calm/neutral 的分布）。见下方计数分支。
        emotion_unknown_count = 0
        seen_segment_keys: set[str] = set()

        def _ingest_records(records: list[Any]) -> None:
            nonlocal total_minutes, laughter_count, self_speech_ms, others_speech_ms
            nonlocal emotion_unknown_count
            for r in records:
                if not isinstance(r, dict):
                    continue
                seg_key = f"{r.get('recording_id', '')}:{r.get('segment_id', '')}:{r.get('absolute_started_at', '')}"
                if seg_key != "::" and seg_key in seen_segment_keys:
                    continue
                if seg_key != "::":
                    seen_segment_keys.add(seg_key)
                if "end_offset_seconds" in r or "start_offset_seconds" in r:
                    dur_s = float(r.get("end_offset_seconds") or 0.0) - float(r.get("start_offset_seconds") or 0.0)
                    dur_ms = int(max(0.0, dur_s) * 1000)
                else:
                    dur_ms = int(r.get("end_offset_ms", 0) - r.get("start_offset_ms", 0))
                if dur_ms > 0:
                    total_minutes += dur_ms / 60000.0
                src_label = str(r.get("source_label") or "").lower()
                spk = str(r.get("speaker") or "").lower()
                if src_label == "self" or "self" in spk:
                    self_speech_ms += max(0, dur_ms)
                elif src_label in {"non_self", "unknown"} or spk:
                    others_speech_ms += max(0, dur_ms)

                txt = str(r.get("text", ""))
                # 【2026-10-14 修】原来 `emo = str(r.get("emotion") or "NEUTRAL")` ——
                # 把「缺 emotion」也当成显式 NEUTRAL，于是所有缺失记录都落进下面 else 分支
                # 同时累加 calm 与 neutral。实测 MOSS 的 219 条 rows 全部 emotion=None，
                # 结果恒显示「calm 50% / neutral 50%」的**假分布**（且 total 被算成 438，
                # 是真实记录数的 2 倍）。与 social_ratio 同一原则：宁可不显示，也不显示编造的。
                # 注意**不要**动显式 NEUTRAL 的行为：既有测试
                # tests/test_daily_dashboard_no_fake_ratios.py::test_real_records_still_compute_ratios
                # 锁定「显式 NEUTRAL 同时计入 calm 与 neutral」（happy=1/3=33.3%），那是有意设计。
                raw_emotion = r.get("emotion")
                emo = str(raw_emotion).strip().upper() if raw_emotion is not None else ""
                if "<|LAUGHTER|>" in txt or "哈哈" in txt or emo == "HAPPY":
                    if "<|LAUGHTER|>" in txt or "哈哈" in txt:
                        laughter_count += 1
                if not emo:
                    # 缺情绪标签：独立计数，不参与情绪分布
                    emotion_unknown_count += 1
                elif emo in {"HAPPY", "SURPRISED"}:
                    emotion_counts["happy"] += 1
                elif emo in {"ANGRY", "FEARFUL", "DISGUSTED", "SAD"}:
                    emotion_counts["stressed"] += 1
                else:
                    # 显式 NEUTRAL / CALM 及未知取值：保持原有「双计」语义不变
                    emotion_counts["calm"] += 1
                    emotion_counts["neutral"] += 1

        if jobs_dir.exists():
            for job_dir in jobs_dir.glob("job-*"):
                for target_date_dir in job_dir.glob(f"files/*/result/{date_str}"):
                    file_root = target_date_dir.parent.parent
                    attr_file = file_root / "attribution" / "source-attribution.json"
                    asr_file = file_root / "asr" / "transcript.json"
                    legacy_t_file = target_date_dir / "transcript.json"
                    for candidate_file in (attr_file, asr_file, legacy_t_file):
                        if candidate_file.exists():
                            try:
                                payload = _read_dashboard_json(candidate_file)
                                records = payload.get("records", []) if isinstance(payload, dict) else payload
                                if isinstance(records, list):
                                    _ingest_records(records)
                                    break
                            except Exception:
                                pass

                    for ac_file in (
                        file_root / "result" / "audio-classification.json",
                        file_root / "attribution" / "audio-classification.json",
                        target_date_dir.parent / "audio-classification.json",
                    ):
                        if ac_file.exists():
                            try:
                                ac_data = _read_dashboard_json(ac_file)
                                windows = ac_data.get("windows", [])
                                for w in windows:
                                    cls = str(w.get("primary_class", "general"))
                                    if cls in scene_counts:
                                        scene_counts[cls] += 1
                                    elif cls in {"speech", "conversation"}:
                                        scene_counts["office"] += 1
                                    elif cls == "silence":
                                        scene_counts["home_quiet"] += 1
                                    elif cls == "noise":
                                        scene_counts["transit"] += 1
                                    else:
                                        scene_counts["general"] += 1
                                break
                            except Exception:
                                pass

        # 补充当日 Watch 切片与声学场景（[2026-10-22 P0] 改为「扫一次 → 按日期分桶」
        # 的聚合缓存，避免每天把 9,778 个分段 state.json 全量重读）
        watch_root = self.job_root / "watch-sessions"
        watch_content_fp = None
        if watch_root.exists():
            # One fresh scan per build; share its files instead of rescanning or
            # relying on the two-second cross-request memo for freshness.
            watch_content_fp, watch_files = _watch_scan(watch_root, use_memo=False)
            try:
                agg = _watch_scene_aggregate(watch_root, content_fp=watch_content_fp, scan_files=watch_files)
            except (OSError, ValueError, TypeError, KeyError):
                agg = {"no_speech": 0, "dates": {}}
            slot = (agg.get("dates") or {}).get(date_str) or {}
            # [Q628 P0] 只加**当天**的 no_speech。原来这里是
            #   scene_counts["home_quiet"] += int(agg.get("no_speech") or 0)
            # 而 agg["no_speech"] 是跨所有日期的累加量 ⇒ fallback 日恒为 100% 居家安静，
            # 实测 2026-10-30 / 10-31 / 11-02 三天输出完全相同。
            scene_counts["home_quiet"] += int(slot.get("no_speech") or 0)
            for bucket_name in ("dining", "transit", "office", "general"):
                scene_counts[bucket_name] += int(slot.get(bucket_name) or 0)
            if slot:
                total_minutes += float(slot.get("minutes") or 0.0)
                laughter_count += int(slot.get("laughter") or 0)
                self_speech_ms += int(slot.get("self_ms") or 0)
                others_speech_ms += int(slot.get("others_ms") or 0)
                emotion_unknown_count += int(slot.get("emotion_unknown") or 0)
                for emo_key, emo_val in (slot.get("emotions") or {}).items():
                    if emo_key in emotion_counts:
                        emotion_counts[emo_key] += int(emo_val or 0)

        # 场景百分比规范化（优先使用基于多模态聚类的真实场景跨度时长）
        timeline_scenes = self.build_timeline_scenes(date_str)
        if timeline_scenes:
            scene_durations: dict[str, int] = {}
            for s in timeline_scenes:
                sc = s.get("scene") or "general"
                dur = int(s.get("duration_minutes") or 1)
                scene_durations[sc] = scene_durations.get(sc, 0) + dur
            total_dur = sum(scene_durations.values()) or 1
            scene_pcts = {
                k: round(v / total_dur * 100.0, 1)
                for k, v in scene_durations.items()
            }
        else:
            total_scene_windows = sum(scene_counts.values()) or 1
            scene_pcts = {
                k: round(v / total_scene_windows * 100.0, 1)
                for k, v in scene_counts.items()
            }

        # 社交倾听 vs 发言比例：没有真实语音时长数据时返回空，不显示任何默认比例
        total_speech_ms = self_speech_ms + others_speech_ms
        if total_speech_ms > 0:
            social_ratio = {
                "self_speech_percent": round(self_speech_ms / total_speech_ms * 100.0, 1),
                "others_speech_percent": round(others_speech_ms / total_speech_ms * 100.0, 1),
            }
        else:
            social_ratio = {}

        total_emo = sum(emotion_counts.values())
        if total_emo > 0:
            emotion_dist = {
                "happy": round(emotion_counts["happy"] / total_emo * 100.0, 1),
                "calm": round(emotion_counts["calm"] / total_emo * 100.0, 1),
                "stressed": round(
                    max(
                        emotion_counts["stressed"] / total_emo * 100.0,
                        min(30.0, health.stress_spikes_count * 5.0),
                    ),
                    1,
                ),
                "neutral": round(emotion_counts["neutral"] / total_emo * 100.0, 1),
            }
        else:
            # 没有任何真实情绪标签记录时返回空分布（不编造默认百分比）
            emotion_dist = {}

        speech_minutes = round(total_minutes, 1)
        coverage_minutes = round(self._received_coverage_minutes(date_str, content_fp=watch_content_fp), 1)
        if coverage_minutes > 0:
            total_audio_minutes = coverage_minutes
            audio_duration_basis = "received_recording_coverage"
            duration_text = (
                f"已接收录音覆盖约 {coverage_minutes:.1f} 分钟"
                f"（其中 ASR 语音 {speech_minutes:.1f} 分钟）"
            )
        else:
            total_audio_minutes = speech_minutes
            audio_duration_basis = "asr_speech"
            duration_text = (
                f"ASR 语音时长约 {speech_minutes:.1f} 分钟"
                "（没有已接收录音覆盖数据，不代表整段录音时长）"
            )

        # AI 亮点生成。即时抓取消费仍是未确认候选，不能汇总成财务真值。
        highlights = (
            f"📅 【{date_str} 生活简报】\n"
            f"• {duration_text}，笑声记录 {laughter_count} 次。\n"
            f"• 场景分布：办公室/专注 {scene_pcts.get('office', 0)}%，居家安静 {scene_pcts.get('home_quiet', 0)}%，通勤 {scene_pcts.get('transit', 0)}%。\n"
            f"• 生理指标：平均心率 {health.avg_heart_rate} bpm，总步数 {health.total_steps} 步，睡眠时长 {health.sleep_minutes // 60} 小时 {health.sleep_minutes % 60} 分钟。\n"
            f"• 即时抓取：待办 {len(todos)} 项，记账候选 {len(expenses)} 条（未确认，不计入财务真值），灵感想法 {len(ideas)} 条。"
            + (
                f"\\n• 情绪：{emotion_unknown_count} 条记录无情绪标签（当前 ASR 不产情绪），故不给出情绪分布。"
                if emotion_unknown_count and not emotion_dist
                else ""
            )
        )

        action_groups = self.build_action_groups(date_str, timeline_scenes)
        return DailyDashboardData(
            date=date_str,
            action_groups=action_groups,
            total_audio_recorded_minutes=total_audio_minutes,
            scene_breakdown_percentages=scene_pcts,
            timeline_scenes=timeline_scenes,
            social_ratio=social_ratio,
            emotion_distribution=emotion_dist,
            laughter_count=laughter_count,
            health_summary=health.to_dict(),
            captured_todos=todos,
            captured_expenses=expenses,
            captured_ideas=ideas,
            ai_daily_highlights=highlights,
            updated_at=datetime.now(tz=LOCAL_TZ).isoformat(),
            audio_duration_basis=audio_duration_basis,
            received_recording_coverage_minutes=coverage_minutes,
            speech_duration_minutes=speech_minutes,
        )

    # 【2026-10-17 新增】动作组（action_group）
    #
    # 为什么需要这一层：场景轨（timeline_scenes）回答的是「这是哪类环境」——
    # 居家/就餐/通勤，本质是**分类**；而人回看一天时问的是「这段时间我在干什么」。
    # 两者数据相同、语义不同：一个吃饭的片段既可能落在「就餐」也可能落在「外出」，
    # 而它作为一个**动作**应该叫「在快餐店点烧鸭饭吃」。
    #
    # 动作组 = 场景段落 + 该段落内的高价值事件 + 金额/时长，且带稳定 ID 供前后端引用。
    # 全仓此前 grep "action_group" 命中 0 次，Web 端也没有任何动作组标签。
    _ACTION_NAME_BY_SCENE = {
        "dining": "进餐",
        "errand": "外出办事",
        "transit": "通勤移动",
        "gym": "运动训练",
        "office": "工作专注",
        "media": "影音娱乐",
        "conversation": "交谈",
        "quiet": "安静独处",
        "home_quiet": "居家休息",
        "general": "日常活动",
    }

    def build_action_groups(
        self, date_str: str, scenes: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """把场景段落升级为可命名的「动作组」（带事件摘要与金额）。"""
        spans = list(scenes if scenes is not None else self.build_timeline_scenes(date_str))
        if not spans:
            return []

        # 事件按小时粗索引后精确过滤，避免 O(组×事件) 扫全表
        events_by_date: list[dict[str, Any]] = []
        for ev_file in list((self.job_root / "jobs").glob(f"job-*/files/*/result/{date_str}/events.json")):
            try:
                payload = _read_dashboard_json(ev_file)
            except Exception:
                continue
            if isinstance(payload, list):
                # Correction joins may append nested provenance; cached source JSON
                # must remain immutable across builds.
                events_by_date.extend(copy.deepcopy(e) for e in payload if isinstance(e, dict))
        # [2026-10-30] 动作组此前直读 events.json 原文，不经过任何纠正 →
        # 人工纠正后 /api/dashboard/daily 仍显示旧标题。这里读时套用 job 纠正（按 event_id）。
        try:
            from life_logger.event_corrections_join import apply_to_events

            events_by_date, corrections_status, corrections_errors = apply_to_events(
                events_by_date, self.job_root, date_str)
        except Exception as corr_exc:  # noqa: BLE001
            logger.warning("event corrections join failed (dashboard %s): %s", date_str, corr_exc)
            corrections_status, corrections_errors = "unknown", [f"{type(corr_exc).__name__}: {corr_exc}"]
        if corrections_status != "ok":
            # [P2-2] 这里原来赋值后从不使用 ⇒ 纠正文件损坏时 dashboard 静默给旧标题。
            logger.warning(
                "event corrections 状态=%s（dashboard %s）：%s；标题可能未应用纠正",
                corrections_status, date_str, "; ".join(corrections_errors or []) or "无细节",
            )
        for ev_file in (self.job_root / "watch-sessions").glob("*/*/events.json"):
            try:
                payload = _read_dashboard_json(ev_file)
            except Exception:
                continue
            if isinstance(payload, list):
                events_by_date.extend(e for e in payload if isinstance(e, dict))

        # 同一事件会同时出现在 job 产物与 watch-sessions 里（正式链 + 实时链各一份），
        # 不去重就会把金额加两遍（实测动作组金额 ¥14.80 = 1.80+13.00）。
        deduped_by_id: dict[str, dict[str, Any]] = {}
        for ev in events_by_date:
            key = str(ev.get("event_id") or "")
            if not key:
                key = f"{ev.get('title')}|{(ev.get('time') or {}).get('absolute')}"
            deduped_by_id.setdefault(key, ev)
        events_by_date = list(deduped_by_id.values())

        groups: list[dict[str, Any]] = []
        for index, span in enumerate(spans):
            start_minute = int(span.get("start_minute") or 0)
            end_minute = int(span.get("end_minute") or 0)
            scene = str(span.get("scene") or "general")
            bucket: list[dict[str, Any]] = []
            for ev in events_by_date:
                if self._hide_event(ev):
                    continue
                raw = str(
                    (ev.get("time") or {}).get("absolute")
                    or (ev.get("evidence") or {}).get("reported_at")
                    or ""
                )
                parsed = parse_ts(raw)
                if parsed is None:
                    continue
                local = parsed.astimezone(LOCAL_TZ)
                if local.strftime("%Y-%m-%d") != date_str:
                    continue
                minute = local.hour * 60 + local.minute
                if start_minute <= minute < end_minute:
                    bucket.append(ev)

            amounts: list[float] = []
            for ev in bucket:
                try:
                    value = ((ev.get("data") or {}).get("expense") or {}).get("amount")
                    if value is not None:
                        amounts.append(float(value))
                except (TypeError, ValueError):
                    continue

            headline = ""
            for ev in bucket:
                title = str(ev.get("title") or "").strip()
                if title:
                    headline = title
                    break

            action = self._ACTION_NAME_BY_SCENE.get(scene, "日常活动")
            # 【2026-10-18】「动作名」必须有证据支撑。
            # 实测 10-17：15:34–16:03 被标成「进餐 29 分钟」，可那段时间**一条饮食/消费
            # 事件都没有** —— 场景分类只说明「环境像就餐（关键词/声学）」，不等于「真在吃饭」。
            # 所以：无事件佐证的动作类场景一律降级标注，避免把推测写成事实。
            ASSERTS_ACTIVITY = {"dining", "errand", "gym", "office", "media", "conversation"}
            evidence_basis = "events" if bucket else "scene_only"
            if not bucket and scene in ASSERTS_ACTIVITY:
                label = f"{action}（无事件佐证）"
            else:
                label = f"{action}·{headline}" if headline else action
            groups.append({
                "action_group_id": f"ag_{date_str.replace('-', '')}_{index:02d}",
                "date": date_str,
                "scene": scene,
                "action": action,
                "label": label[:80],
                "evidence": evidence_basis,
                "headline": headline[:80],
                "start_minute": start_minute,
                "end_minute": end_minute,
                "start_time": str(span.get("start_time") or f"{start_minute // 60:02d}:{start_minute % 60:02d}"),
                "end_time": str(span.get("end_time") or f"{end_minute // 60:02d}:{end_minute % 60:02d}"),
                "duration_minutes": max(0, end_minute - start_minute),
                "event_count": len(bucket),
                "expense_total_cny": round(sum(amounts), 2),
                "event_ids": [
                    str(ev.get("event_id") or "") for ev in bucket if ev.get("event_id")
                ][:20],
                # [P2-2] 让调用方/审计能看到纠正是否真的应用（ok/none/unknown）
                "corrections_status": corrections_status,
            })
        return groups

    # 【2026-10-17 新增】把分钟级碎片场景合并成「有意义的活动段落」。
    #
    # 背景：场景标签是按 60 秒录音片段独立判的（关键词 + Wi-Fi 锚点），
    # 判错一个片段就会在瀑布图上多出一个小色块。实测 10-17 一天被切成了
    # 35 个 1~3 分钟的碎块，包括「791→818 居家 / 819→820 工作 / 822→826 居家 /
    # 829→843 工作」这种反复横跳，读的人根本看不出今天干了什么。
    #
    # 做法：
    #   1) 先按「连续录制区段」分组（间隔 ≤2 分钟视为同一段录制），保留断档空洞；
    #   2) 每个区段内做滑动窗口多数投票，把 1~2 分钟的抖动抹平；
    #   3) 游程编码成块，再把短于 min_minutes 的块并到邻居；
    #   4) 最后把真实事件标题回填成块内细节（detail），保证「块里能看出干了啥」。
    @staticmethod
    def _consolidate_scene_episodes(
        spans: list[dict[str, Any]],
        *,
        min_minutes: int = 10,
        smooth_window: int = 6,
        gap_tolerance: int = 2,
    ) -> list[dict[str, Any]]:
        seg_spans = [s for s in spans if s.get("_source") == "segment"]
        ev_spans = [s for s in spans if s.get("_source") != "segment"]
        if not seg_spans:
            return [
                {"start_min": s["start_min"], "end_min": s["end_min"],
                 "scene": s["scene"], "detail": s.get("detail", "")}
                for s in sorted(ev_spans, key=lambda x: x["start_min"])
            ]

        # 1) 连续录制区段
        blocks: list[list[int]] = []
        for s in sorted(seg_spans, key=lambda x: x["start_min"]):
            st, en = int(s["start_min"]), max(int(s["end_min"]), int(s["start_min"]) + 1)
            if blocks and st <= blocks[-1][1] + gap_tolerance:
                blocks[-1][1] = max(blocks[-1][1], en)
            else:
                blocks.append([st, en])

        # 2) 每分钟场景表（事件锚点优先级最高）
        minute_scene: dict[int, str] = {}
        for s in seg_spans:
            st = int(s["start_min"])
            en = max(int(s["end_min"]), st + 1)
            for m in range(st, en):
                minute_scene[m] = s["scene"]

        # 事件锚点：真实发生的事（吃饭/健身/通勤/工作）必须压过片段噪声，
        # 否则「14:56 购买烧鸭饭」会被周围 60 秒片段投票洗成「安静」。
        sticky: set[int] = set()
        for ev in ev_spans:
            ev_st = int(ev["start_min"])
            ev_en = max(ev_st + 1, int(ev["end_min"]))
            for m in range(ev_st, ev_en):
                minute_scene[m] = ev["scene"]
                sticky.add(m)

        episodes: list[dict[str, Any]] = []
        for block_idx, (b_start, b_end) in enumerate(blocks):
            minutes = [m for m in range(b_start, b_end) if m in minute_scene]
            if not minutes:
                continue

            # 3) 滑动窗口多数投票
            smoothed: dict[int, str] = {}
            for m in minutes:
                if m in sticky:
                    smoothed[m] = minute_scene[m]
                    continue
                counts: dict[str, int] = {}
                for k in range(m - smooth_window, m + smooth_window + 1):
                    sc = minute_scene.get(k)
                    if sc:
                        counts[sc] = counts.get(sc, 0) + 1
                if counts:
                    smoothed[m] = max(counts, key=lambda k: (counts[k], k != "general"))

            # 4) 游程编码（标记含事件锚点的游程，锁定不被吸收）
            runs: list[dict[str, Any]] = []
            for m in minutes:
                sc = smoothed.get(m)
                if not sc:
                    continue
                is_sticky = m in sticky
                if runs and runs[-1]["scene"] == sc and m == runs[-1]["end_min"]:
                    runs[-1]["end_min"] = m + 1
                    runs[-1]["_sticky"] = runs[-1].get("_sticky") or is_sticky
                else:
                    runs.append({
                        "start_min": m, "end_min": m + 1, "scene": sc,
                        "detail": "", "_sticky": is_sticky,
                    })
            # 5) 吸收短块：短于最小长度的块并入前一块（头部则并入后一块）；
            #    但带事件锚点的块不吸（否则“吃了碗烧鸭饭”会被 60 秒噪声吐掉）
            merged: list[dict[str, Any]] = []
            for r in runs:
                short = (r["end_min"] - r["start_min"]) < min_minutes
                if merged and short and not r.get("_sticky"):
                    merged[-1]["end_min"] = r["end_min"]
                elif merged and merged[-1]["scene"] == r["scene"]:
                    merged[-1]["end_min"] = r["end_min"]
                else:
                    merged.append(r)
            while (
                len(merged) > 1
                and (merged[0]["end_min"] - merged[0]["start_min"]) < min_minutes
                and not merged[0].get("_sticky")
            ):
                merged[1]["start_min"] = merged[0]["start_min"]
                merged.pop(0)
            for m in merged:
                m["_block"] = block_idx
            episodes.extend(merged)

        # 5.5) 「无数据」不构成一个活动：quiet / general 只是没有可用信号，
        #      不能跟「吃饭」「聊天」抢地盘，否则一段电脑前工作会被切成一堆黑洞。
        #      注意：只在**同一个连续录制区段**内吸收；两段录制之间的空洞是真
        #      「没录音」（比如睡觉），绝不能拿去填成活动。
        weak_scenes = {"quiet", "general"}
        idx = 0
        while idx < len(episodes):
            ep = episodes[idx]
            same_block = lambda j: 0 <= j < len(episodes) and episodes[j].get("_block") == ep.get("_block")
            if ep["scene"] in weak_scenes and not ep.get("_sticky"):
                prev_ok, next_ok = same_block(idx - 1), same_block(idx + 1)
                if prev_ok and next_ok and (ep["end_min"] - ep["start_min"]) <= 20:
                    mid = (ep["start_min"] + ep["end_min"]) // 2
                    episodes[idx - 1]["end_min"] = max(episodes[idx - 1]["end_min"], mid)
                    episodes[idx + 1]["start_min"] = min(episodes[idx + 1]["start_min"], mid)
                    episodes.pop(idx)
                    continue
                if next_ok and (not prev_ok or prev_ok) and (ep["end_min"] - ep["start_min"]) <= 20:
                    # 区段开头的弱块：归给紧随其后的实场景
                    episodes[idx + 1]["start_min"] = ep["start_min"]
                    episodes.pop(idx)
                    continue
                if prev_ok and (ep["end_min"] - ep["start_min"]) <= 20:
                    # 区段结尾的弱块：归给前一个实场景
                    episodes[idx - 1]["end_min"] = ep["end_min"]
                    episodes.pop(idx)
                    continue
            idx += 1

        # 6) 真实事件回填细节（块里要能看出「干了啥」）
        for ev in ev_spans:
            ev_st, ev_en = int(ev["start_min"]), int(ev["end_min"])
            hit = None
            for ep in episodes:
                if ep["start_min"] <= ev_st < ep["end_min"] or ep["start_min"] < ev_en <= ep["end_min"]:
                    hit = ep
                    break
            if hit is not None:
                title = str(ev.get("detail") or "").strip()
                if title:
                    existing = [x for x in str(hit.get("detail") or "").split(" · ") if x]
                    if title not in existing:
                        existing.append(title)
                    # 一块里最多带 3 件事，多了就成流水账了（用户要的是精简）
                    hit["detail"] = " · ".join(existing[:3])
                continue
            # 落在未录制空洞里的事件单独成块，但不要虚构成超大跨度
            episodes.append({
                "start_min": ev_st,
                "end_min": max(ev_st + 10, min(ev_en, ev_st + 30)),
                "scene": ev["scene"],
                "detail": ev.get("detail", ""),
            })

        episodes.sort(key=lambda s: s["start_min"])

        # 5.7) 收尾：同一活动跨过短短录音断层就合成一块；过短的孤立碎块并入邻居
        merged2: list[dict[str, Any]] = []
        for ep in episodes:
            if merged2 and merged2[-1]["scene"] == ep["scene"] and ep["start_min"] - merged2[-1]["end_min"] <= 5:
                merged2[-1]["end_min"] = max(merged2[-1]["end_min"], ep["end_min"])
                if not merged2[-1].get("detail") and ep.get("detail"):
                    merged2[-1]["detail"] = ep["detail"]
                continue
            merged2.append(ep)

        final: list[dict[str, Any]] = []
        for ep in merged2:
            dur = ep["end_min"] - ep["start_min"]
            # 吸收短块时必须同时要求「时间上紧邻」：第一版只看 dur<5，
            # 结果把凌晨 00:00–00:27 和中午 12:35–12:39 两个不相干的安静块
            # 错并成「00:00 → 12:39」，凭空造出一整上午。
            if (
                final
                and dur < 5
                and not ep.get("_sticky")
                and ep["start_min"] - final[-1]["end_min"] <= 10
            ):
                final[-1]["end_min"] = max(final[-1]["end_min"], ep["end_min"])
                continue
            if dur < 5 and not ep.get("_sticky"):
                # 孤立一闪而过的碎块（实测 14:33→14:34 一分钟）不单独上瀑布图。
                continue
            final.append(ep)
        episodes = final

        return episodes

    def _apply_user_scene_corrections(
        self, results: list[dict[str, Any]], date_str: str
    ) -> list[dict[str, Any]]:
        """Apply explicit user scene corrections (scene-corrections.json).

        Deterministic slicing only: each rule names one date, one scene
        and one exact [start_time, end_time) interval the user negated.
        Spans overlapping the interval are cut by intersection; parts
        outside it are preserved as copies, the input list is never
        mutated, and other scenes/dates are untouched. Missing or
        unreadable correction data means unknown: results are returned
        unchanged rather than guessed. This never infers a scene from
        activity words — it only removes what the user explicitly
        negated (2026-10-26: gym 20:51-22:32, user stated they had
        already returned home in the 19:xx hour; the approximate
        wording is kept as-is and never converted into precise times).
        """
        corr_path = self.job_root / "scene-corrections.json"
        if not corr_path.is_file():
            return results
        try:
            payload = json.loads(corr_path.read_text(encoding="utf-8"))
        except Exception:
            return results
        rules = payload.get(date_str) if isinstance(payload, dict) else None
        if not isinstance(rules, list) or not rules:
            return results
        current = results
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            revoke_scene = rule.get("revoke_scene")
            try:
                start_parts = str(rule.get("start_time", "")).split(":")
                end_parts = str(rule.get("end_time", "")).split(":")
                r_start = int(start_parts[0]) * 60 + int(start_parts[1])
                r_end = int(end_parts[0]) * 60 + int(end_parts[1])
            except Exception:
                continue
            if not revoke_scene or r_end <= r_start:
                continue
            sliced: list[dict[str, Any]] = []
            for span in current:
                if span.get("scene") != revoke_scene:
                    sliced.append(span)
                    continue
                s_start = int(span.get("start_minute", 0))
                s_end = int(span.get("end_minute", 0))
                overlap_start = max(s_start, r_start)
                overlap_end = min(s_end, r_end)
                if overlap_start >= overlap_end:
                    sliced.append(span)
                    continue
                if s_start < overlap_start:
                    left = dict(span)
                    left["end_minute"] = overlap_start
                    left["end_time"] = f"{overlap_start // 60:02d}:{overlap_start % 60:02d}"
                    left["duration_minutes"] = overlap_start - s_start
                    sliced.append(left)
                if overlap_end < s_end:
                    right = dict(span)
                    right["start_minute"] = overlap_end
                    right["start_time"] = f"{overlap_end // 60:02d}:{overlap_end % 60:02d}"
                    right["duration_minutes"] = s_end - overlap_end
                    sliced.append(right)
            current = sliced
        return current

    def build_timeline_scenes(self, date_str: str) -> list[dict[str, Any]]:
        """Construct continuous, high-fidelity ambient scene spans for a given date."""
        bj_tz = timezone(timedelta(hours=8))
        raw_spans: list[dict[str, Any]] = []
        place_points: list[tuple[int, str]] = []
        events = self._dashboard_events(date_str)
        event_overrides = []
        for ev in events:
            occurrence = self._event_occurrence(ev)
            if occurrence is None:
                continue
            local = occurrence.astimezone(bj_tz)
            if local_date(local) == date_str:
                event_overrides.append((local.hour * 60 + local.minute,
                                        (ev.get("categories") or [""])[0],
                                        ev.get("activity_type", ""), ev.get("title", "")))

        # 1. 扫描活跃与历史 Watch 会话
        watch_root = self.job_root / "watch-sessions"
        if watch_root.is_dir():
            for mpath in watch_root.glob("*/*/manifest.json"):
                sess_dir = mpath.parent
                try:
                    m = _read_dashboard_json(mpath)
                except Exception:
                    continue
                asr_dir = sess_dir / "formal-asr" / "segments"
                for seq, seg_start, seg in _manifest_day_segments(mpath, m, date_str):
                    dur_ms = int(seg.get("duration_ms", 60000))
                    seg_start_bj = seg_start.astimezone(bj_tz)

                    start_min = seg_start_bj.hour * 60 + seg_start_bj.minute
                    end_min = min(1440, start_min + max(1, dur_ms // 60000))

                    scene = "general"
                    scene_detail = ""

                    st_path = asr_dir / f"{seq:06d}" / "state.json" if asr_dir.exists() else None
                    if st_path and st_path.is_file():
                        try:
                            st = _read_dashboard_json(st_path)
                            status = st.get("status")
                            if status == "no_speech":
                                # 【2026-10-17】原来 silence 一律判 home_quiet（＝「居家」），
                                # 等于凭空声称用户在家 —— 而用户下午其实在外面吃饭、买东西。
                                # 安静只能说明「没说话」，不能说明在哪。改用中性的 quiet。
                                scene = "quiet"
                            elif status == "completed":
                                rows_st = st.get("rows", []) or []
                                txt = " ".join(str(r.get("text", "")) for r in rows_st)
                                # 【2026-10-17】区分「自己在说话」与「背景在放音」。
                                # 实测 audio_content_class 不可靠：用户本人对话也会被标 stream（实测 12:47
                                # 亲口说话的片段也是 stream 0.59），拿它当主信号会把整条时间线说成“看电视”。
                                # 改用声纹归属：self/near 占比高 → 本人在说话；占比极低且有大段文字 → 背景影音。
                                self_votes = sum(
                                    1
                                    for r in rows_st
                                    if str(r.get("source_label") or "") == "self"
                                )
                                self_ratio = self_votes / max(1, len(rows_st))
                                if rows_st and self_ratio < 0.2 and len(txt) > 40:
                                    scene = "media"
                                    scene_detail = "影音播放（短剧 / 直播 / 游戏）"
                                elif any(k in txt for k in ("地铁", "车", "车站", "路上", "打车", "转线", "公交", "高铁")):
                                    scene = "transit"
                                    scene_detail = "地铁/交通"
                                elif any(k in txt for k in ("商场", "店", "咖啡", "汉堡", "吃饭", "餐", "酒", "茶", "买单", "逛")):
                                    scene = "dining"
                                    scene_detail = "就餐"
                                elif any(k in txt for k in ("健身", "跑步", "练腿", "胸部", "有氧", "运动", "累了")):
                                    scene = "gym"
                                    scene_detail = "健身/运动"
                                elif any(k in txt for k in ("代码", "电脑", "工作", "需求", "开发", "开会", "会议", "办公", "写代码")):
                                    scene = "office"
                                    scene_detail = "工作/沟通"
                                else:
                                    scene = "conversation"
                        except Exception:
                            pass

                    # 地点单独记，不改声学场景：在家也可能在看视频或开会。
                    place = _fresh_segment_place(seg.get("location"), seg_start)
                    if place:
                        place_points.append((start_min, place))

                    # 关联结构化事件（共享视图已做过 canonical/日期/分支/去重）。
                    # unknown occurrence 不参与定位，reported_at 不能冒充发生时间。
                    for ev_min, cat, act, title in event_overrides:
                        if abs(ev_min - start_min) <= 45:
                            if cat in ("food",) or act in ("meal", "drink"):
                                scene = "dining"
                                scene_detail = title
                            elif cat in ("exercise",) or act in ("cardio", "workout"):
                                scene = "gym"
                                scene_detail = title
                            elif cat in ("commute",):
                                scene = "transit"
                                scene_detail = title

                    raw_spans.append({
                        "start_min": start_min,
                        "end_min": end_min,
                        "scene": scene,
                        "detail": scene_detail,
                        "_source": "segment",
                    })

        # 2. 共享事件视图作为「块内细节锚点」。正式事件以 daily_summary 为准，
        #    跨日时按 occurrence 归到实际日期；同一事件的 live/formal 重复只留一个锚点。
        seen_event_anchors: set[tuple[int, str, str]] = set()
        for ev in events:
            ev_dt = self._event_occurrence(ev)
            if ev_dt is None:
                # 发生时间未知：可以进入事件的 reported-date 视图，但不能凭空定位。
                continue
            ev_local = ev_dt.astimezone(bj_tz)
            if local_date(ev_local) != date_str:
                continue
            ev_min = ev_local.hour * 60 + ev_local.minute
            cat = (ev.get("categories") or [""])[0]
            act = ev.get("activity_type", "")
            if cat in ("food",) or act in ("meal", "drink"):
                sc = "dining"
            elif cat in ("exercise",) or act in ("cardio", "workout"):
                sc = "gym"
            elif cat in ("commute",):
                sc = "transit"
            elif cat in ("expense",):
                # 线下 POS / 口头订单：买冰杯、买烟这类「在店里办事」
                sc = "errand"
            elif cat in ("work", "work_communication", "office"):
                sc = "office"
            else:
                continue
            detail = str(ev.get("title") or "")
            anchor_key = (ev_min, sc, detail)
            if anchor_key in seen_event_anchors:
                continue
            seen_event_anchors.add(anchor_key)
            raw_spans.append({
                "start_min": max(0, ev_min - 10),
                "end_min": min(1440, ev_min + 15),
                "scene": sc,
                "detail": detail,
                "_source": "event",
            })

        if not raw_spans:
            return []

        raw_spans.sort(key=lambda s: s["start_min"])

        # 【2026-10-17 重构】原来按 1 分钟粒度的 seg 逐一打标签，直接把 276 个
        # 60 秒片段变成 35 个 1~3 分钟的碎块（791→818 / 819→820 / 822→826 / 829→843…），
        # 用户看到的就是「一堆小方块，不知道今天到底干了啥」。
        # 现在改为「活动段落（episode）」：先按连续录制区段做滑动窗口多数投票把
        # 噪声抹平，再做游程编码 + 短段吸收，产出少量长块；事件标题只作为块内细节。
        clusters = self._consolidate_scene_episodes(raw_spans)

        label_meta = {
            # 【2026-10-17】原有 home_quiet 只在 Wi-Fi 锚点确实报 Home 时才用。
            # 新增加的 quiet/media/conversation 三个中性场景，避免凭空声称“在家”。
            "home_quiet": ("🏠", "居家安静 / 休息", "#10b981"),
            "quiet": ("🌙", "安静少言（无对话）", "#7c8b86"),
            "media": ("📺", "影音娱乐（剧 / 直播）", "#b0567a"),
            "conversation": ("🗣️", "聊天 / 交流", "#5c7f8a"),
            "errand": ("🛒", "外出 / 采购办事", "#c98a3a"),
            # 【2026-10-14 修】原来是 ("🛍️", "商场 / 就餐 / 咖啡") —— 但该场景是靠
            # 文本关键词（吃饭/餐/咖啡…）判的，**推断不出地点**。实测用户凌晨在家吃乌冬面
            # 被判成 dining，看板就显示「商场 / 就餐 / 咖啡」+ 购物袋图标 —— 与事实相反，
            # 属于「编造地点」。改为地点中性的「就餐 / 用餐」+ 餐具图标。
            "dining": ("🍜", "就餐 / 用餐", "#f59e0b"),
            "transit": ("🚇", "地铁 / 通勤", "#8b5cf6"),
            "gym": ("🏋️", "健身房 / 有氧运动", "#ef4444"),
            "office": ("💻", "办公室 / 专注", "#3b82f6"),
            "general": ("🌐", "日常环境", "#6b7280"),
        }

        # 【2026-10-17】旧的两轮「按 20/15 分钟间隙聚类 + 二次平滑」已由
        # _consolidate_scene_episodes() 统一接管，避免两套平滑规则相互抵消。
        smoothed: list[dict[str, Any]] = [c for c in clusters]

        results: list[dict[str, Any]] = []
        for c in smoothed:
            dur = c["end_min"] - c["start_min"]
            icon, name, color = label_meta.get(c["scene"], ("🌐", c["scene"], "#6b7280"))
            start_str = f"{c['start_min'] // 60:02d}:{c['start_min'] % 60:02d}"
            end_str = f"{c['end_min'] // 60:02d}:{c['end_min'] % 60:02d}"
            results.append({
                "start_minute": c["start_min"],
                "end_minute": c["end_min"],
                "start_time": start_str,
                "end_time": end_str,
                "duration_minutes": dur,
                "scene": c["scene"],
                "label": name,
                "icon": icon,
                "color": color,
                "detail": c.get("detail", ""),
                "place": _scene_place(place_points, c["start_min"], c["end_min"]),
            })

        if self.dictation_filter is not None:
            # 整段都落在 dictation（电脑前说话）窗口里的场景在看板上隐藏 ——
            # 否则场景轴上还会出现「设计沟通」这类内容。产物文件不动。
            results = self.dictation_filter.filter_scenes(results, date=date_str)
        return self._apply_user_scene_corrections(results, date_str)
