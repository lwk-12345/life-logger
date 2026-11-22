"""Shared canonical read view for Life Logger events.

The formal pipeline writes ``daily_summary.json`` artifacts. The Watch live
pipeline writes ``watch-sessions/*/*/events.json``. Before this module existed
the statistics reader filtered summaries by their artifact directory name and
the daily brief merged live events by event_id only. Both behaviours were
wrong for cross-midnight recordings and for the same utterance appearing once
in the formal pipeline and once in the live pipeline with two different ids.

This module is deliberately read-only. It never changes artifact files, source
ids, event ids, review overlays or decision branches. Duplicates are
represented as a canonical event plus provenance/aliases; the original event
payload is preserved inside the alias.

Date rules (Asia/Shanghai):

* A valid occurrence timestamp wins. It is never replaced by
  ``recording_start + relative_seconds``.
* If occurrence is unknown, the local ``reported_at`` date is used to organise
  the undated fact and is explicitly marked ``date_basis="reported_local"``.
* If no reported timestamp exists either, a summary directory date may be used
  as a last-resort artifact fallback and is marked ``artifact_directory``.
  The occurrence date remains unknown.
* A summary's directory date is never used to exclude an event before its
  actual occurrence/reported date is inspected.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from life_logger.jsonio import glob_json, read_json, read_json_text, resolve_readable_json_path

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = "event-view-v1"
SUPPORTED_SCOPES = ("settled", "provisional")
SUPPORTED_VIEWS = ("display", "all")

# Financial fields are intentionally not filled from the live branch when the
# preferred formal branch left them empty. Doing so would bypass the formal
# evidence/amount validation. The live value is still preserved in the alias
# and in ``conflicts`` when both branches disagree.
_FINANCIAL_KEYS = {
    "amount",
    "net_amount",
    "currency",
    "proposed_expense_amount",
    "balance",
    "paid_amount",
    "total_amount",
    "refund_amount",
    "amount_cny",
}

_QUANTITY_UNIT_PATTERN = re.compile(
    r"(?:[一二两三四五六七八九十百千万零〇0-9]+"
    r"(?:包|瓶|杯|盒|袋|个|份|块|斤|公斤|千克|克|kg|g|ml|毫升|升|支|条|罐|桶|"
    r"碗|盘|只|件|箱|串|颗|粒|片|张|本|双|套|元|钱))",
    re.IGNORECASE,
)


def _is_financial_key(key: object) -> bool:
    name = str(key or "").casefold()
    return (
        name in _FINANCIAL_KEYS
        or name.startswith("amount")
        or name.endswith("_amount")
    )


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


SENTINEL_DATE = "2025-01-01"


def _is_sentinel_datetime(value: object) -> bool:
    """The session aggregator historically used 2025-01-01 as a default date."""
    parsed = _parse_datetime(value)
    return parsed is not None and parsed.date().isoformat() == SENTINEL_DATE


def _validate_date(value: str, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(f"{label}格式无效") from error
    return parsed.date().isoformat()


def _normalized_quote(event: Mapping[str, Any]) -> str:
    evidence = event.get("evidence")
    quote = evidence.get("quote") if isinstance(evidence, Mapping) else ""
    return "".join(
        str(char).casefold() for char in str(quote or "") if str(char).isalnum()
    )


def _reported_datetime(event: Mapping[str, Any]) -> datetime | None:
    evidence = event.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    for key in ("reported_at", "reported_at_local"):
        if _is_sentinel_datetime(evidence.get(key)):
            continue
        parsed = _parse_datetime(evidence.get(key))
        if parsed is not None:
            return parsed
    return None


def _occurrence_datetime(event: Mapping[str, Any]) -> datetime | None:
    time_info = event.get("time")
    evidence = event.get("evidence")
    time_info = time_info if isinstance(time_info, Mapping) else {}
    evidence = evidence if isinstance(evidence, Mapping) else {}
    for value in (
        time_info.get("absolute"),
        evidence.get("occurrence_time_evidence"),
    ):
        if _is_sentinel_datetime(value):
            continue
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed

    # ``local``/``absolute_local`` are derived fields.  They are useful for
    # genuinely local-only legacy events, but only when the artifact states a
    # known precision.  An unknown-precision row whose local string came from
    # reported_at must stay an unknown occurrence.
    precision = str(time_info.get("precision") or "").strip().casefold()
    if precision in {"exact", "inferred", "relative"}:
        for value in (time_info.get("absolute_local"), time_info.get("local")):
            if _is_sentinel_datetime(value):
                continue
            parsed = _parse_datetime(value)
            if parsed is not None:
                return parsed
    return None


def _redact_sentinel_values(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """把审计映射里的 2025-01-01 sentinel 值换成 None（键保留，便于排查）。"""
    return {
        key: (None if _is_sentinel_datetime(value) else value)
        for key, value in mapping.items()
    }


def _capture_statement(event: Mapping[str, Any]) -> bool:
    """原话/标题是否为「记录一下…」这类显式记录指令。"""
    evidence = event.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    text = f"{event.get('title') or ''} {evidence.get('quote') or ''}"
    return bool(CAPTURE_STATEMENT_RE.search(text))


def _capture_time_masquerade(event: Mapping[str, Any]) -> datetime | None:
    """返回"伪装成发生时刻的捕获时刻"，否则 None。

    契约：`precision="unknown"` 表示抽取器**没有**确定发生时间；此时 `time.absolute`
    若恰好等于 `evidence.reported_at`，说明它只是音频/入库时刻，**不是发生时刻**。
    2026-10-24 起不再奉行"precision=unknown 但 absolute 存在 ⇒ 已定位"。
    """
    time_info = event.get("time")
    time_info = time_info if isinstance(time_info, Mapping) else {}
    evidence = event.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    if str(time_info.get("precision") or "").strip().lower() != "unknown":
        return None
    absolute = _parse_datetime(time_info.get("absolute"))
    reported = _reported_datetime(event)
    if absolute is None or reported is None:
        return None
    if abs((absolute - reported).total_seconds()) > CAPTURE_TIME_TOLERANCE_SECONDS:
        return None
    return absolute


def _display_label(moment: datetime, basis: str) -> str:
    clock = moment.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
    if basis == "capture_statement":
        return f"约{clock}（按讲述时间）"
    if basis == "inferred_occurrence":
        return f"{clock}（按段转录时间）"
    return clock


def _event_time_basis(
    event: Mapping[str, Any],
    *,
    occurrence: datetime | None,
    reported: datetime | None,
    directory_date: str | None,
) -> str:
    time_info = event.get("time")
    time_info = time_info if isinstance(time_info, Mapping) else {}
    if str(time_info.get("display_basis") or "") == "capture_statement":
        return "capture_statement"
    if occurrence is not None:
        if str(time_info.get("occurrence_source") or "") == "segment_transcript":
            return "inferred_occurrence"
        return "exact_occurrence"
    if reported is not None:
        return "reported_local"
    if directory_date:
        return "artifact_directory"
    return "unknown"


def _apply_time_contract(
    event: dict[str, Any],
    occurrence: datetime | None,
    info: dict[str, Any],
) -> None:
    """Regenerate derived local clock fields from the authoritative occurrence.

    Correcting only ``time.absolute`` must not leave an old ``local``/``clock``/
    ``date`` next to the new absolute.  Conversely, when occurrence is unknown,
    stale derived occurrence fields must be removed so they cannot resurrect an
    old day.  ``evidence.reported_at_local`` is regenerated separately from
    ``reported_at``.
    """

    time_info = event.get("time")
    time_info = time_info if isinstance(time_info, dict) else {}
    event["time"] = time_info
    derived_keys = ("absolute_local", "local", "clock", "date", "timezone")
    previous = {
        key: time_info.get(key)
        for key in derived_keys
        if key in time_info
    }
    if occurrence is not None:
        time_info["absolute_local"] = occurrence.isoformat()
        time_info["local"] = occurrence.isoformat()
        time_info["clock"] = occurrence.strftime("%H:%M")
        time_info["date"] = occurrence.date().isoformat()
        time_info["timezone"] = "Asia/Shanghai"
        info["time_contract"] = "occurrence"
    else:
        removed = {
            key: time_info.pop(key)
            for key in ("absolute_local", "local", "clock", "date")
            if key in time_info
        }
        if _is_sentinel_datetime(time_info.get("absolute")):
            removed["absolute"] = time_info.get("absolute")
            time_info["absolute"] = None
            time_info["precision"] = "unknown"
        if _is_sentinel_datetime(time_info.get("absolute_ended_at")):
            removed["absolute_ended_at"] = time_info.get("absolute_ended_at")
            time_info["absolute_ended_at"] = None
        if _is_sentinel_datetime(time_info.get("absolute_ended_at_local")):
            removed["absolute_ended_at_local"] = time_info.get("absolute_ended_at_local")
            time_info["absolute_ended_at_local"] = None
        if removed:
            info["removed_derived_occurrence_fields"] = _redact_sentinel_values(removed)
            if any(_is_sentinel_datetime(v) for v in removed.values()):
                info["sentinel_values_dropped"] = True
        info["time_contract"] = "unknown"
    if previous and previous != {
        key: time_info.get(key) for key in previous
    }:
        info["previous_derived_time_fields"] = _redact_sentinel_values(previous)

    evidence = event.get("evidence")
    if isinstance(evidence, Mapping):
        evidence_copy = dict(evidence)
        event["evidence"] = evidence_copy
        # 【2026-10-23 返工·A】sentinel 的 reported_at 也必须清零。
        # 只清 time.* 是不够的：`/api/daily` 仍会把 evidence.reported_at=2025-01-01 发出去，
        # 而前端 `web/app.js:3943 journalEventTimestamp()` 的 fallback 恰好会读它
        # （time.absolute 为 null 时），日志页就会显示 2025 年。
        dropped: list[str] = []
        for key in ("reported_at", "reported_at_local"):
            if _is_sentinel_datetime(evidence_copy.get(key)):
                evidence_copy.pop(key, None)
                dropped.append(key)
        if dropped:
            evidence_copy["reported_at_dropped"] = "sentinel"
            info["dropped_sentinel_reported_fields"] = dropped
        reported = _reported_datetime(event)
        if reported is not None:
            evidence_copy["reported_at_local"] = reported.isoformat()


def _relative_seconds(event: Mapping[str, Any]) -> float | None:
    time_info = event.get("time")
    evidence = event.get("evidence")
    raw: object | None = None
    if isinstance(time_info, Mapping):
        raw = time_info.get("relative_seconds")
    if raw is None and isinstance(evidence, Mapping):
        raw = evidence.get("start_offset_seconds")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _candidate_from_offset(
    event: Mapping[str, Any], source_start: datetime | None
) -> datetime | None:
    if source_start is None:
        return None
    seconds = _relative_seconds(event)
    if seconds is None:
        return None
    return source_start + timedelta(seconds=seconds)


def _safe_to_repair_missing_time(event: Mapping[str, Any]) -> bool:
    """Do not invent an occurrence for an explicitly recalled/past event."""

    evidence = event.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    time_info = event.get("time")
    time_info = time_info if isinstance(time_info, Mapping) else {}
    if event.get("correction_status") and time_info.get("absolute") in (None, ""):
        return False
    if _parse_datetime(evidence.get("occurrence_time_evidence")) is not None:
        return False
    relation = str(evidence.get("temporal_relation") or "").strip().casefold()
    # [2026-10-23 P0] explicit unknown / past / future must never be repaired
    # from recording offset. Only at_speech/at_event (or absent legacy relation)
    # may use the source-start fallback.
    if relation in {"unknown", "past", "future", "recalled", "historical", "before"}:
        return False
    return True


def _legacy_time_is_invalid(
    event: Mapping[str, Any],
    occurrence: datetime,
    candidate: datetime | None,
) -> bool:
    """Detect the old buggy absolute timestamp without touching valid past time.

    Legacy artifacts sometimes carried an absolute that was the recording end
    time or a UTC value saved as if it were local, while the event only had a
    speech offset. There is no reported_at/occurrence evidence or temporal
    relation in those rows. Only repair when the absolute and the source-start
    derived candidate land on different local dates; otherwise keep the
    original occurrence.
    """

    if candidate is None:
        return False
    evidence = event.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    if _reported_datetime(event) is not None:
        return False
    if _parse_datetime(evidence.get("occurrence_time_evidence")) is not None:
        return False
    if str(evidence.get("temporal_relation") or "").strip():
        return False
    return occurrence.date() != candidate.date()


#: 「记录指令」原话：用户明确要求记录/备忘某件事。这类事件的讲述时刻可以按"近似发生时间"展示，
#: 但必须标注为「按讲述时间」，不得冒充精确时刻（2026-10-24 时间语义契约）。
CAPTURE_STATEMENT_RE = re.compile(
    r"记录一下|记录这|记录当前|记录今天|记一下|记下|记个|帮我记|备忘|便签|记一笔"
)

#: 捕获时刻与 reported_at 允许偏差：超过就不认为是同一次发声。
CAPTURE_TIME_TOLERANCE_SECONDS = 2.0

#: 事件时间依据（event_time_basis）取值域。
EVENT_TIME_BASES: tuple[str, ...] = (
    "exact_occurrence",     # 有真实发生时刻证据（exact/minute/…）
    "inferred_occurrence",  # 由段转录行等证据推断出的发生时刻
    "capture_statement",    # 记录指令：展示的是"讲述/捕获时间"，只能标"约"
    "reported_local",       # 只有讲述时间，发生时刻未知
    "artifact_directory",   # 只有产物目录日期
    "unknown",              # 什么都没有
)

#: segment 转录行时间与 reported_at 的允许偏差（秒）。超过就当"不是同一次发声"，保持 unknown。
SEGMENT_OCCURRENCE_TOLERANCE_SECONDS = 2.0


def _segment_row_evidence(
    web_root: Path | None, evidence: Mapping[str, Any]
) -> tuple[datetime | None, dict[str, Any] | None]:
    """按 `evidence.segment_id` 找**同 id 的转录行**，返回 (发生时间, 证据)。

    规则（2026-10-24 时间未定位审计 B 类）：
      * 只认 `formal-asr/segments/<目录>/state.json` 里 `segment_id` **完全相同**的行；
      * 该行 `absolute_started_at` 与 `evidence.reported_at` 必须相差 ≤2 秒；
      * 段目录不唯一 / 行不唯一 / 找不到 / 超差 → 一律返回 None（调用方保持 unknown）。
    """
    if web_root is None:
        return None, None
    segment_id = str(evidence.get("segment_id") or "").strip()
    if not segment_id:
        return None, None
    parts = segment_id.split("_")
    if len(parts) < 3:
        return None, None
    segment_dir = parts[-2]          # watch_000220_000001 → 000220
    session = str(evidence.get("recording_id") or "").replace("watch_", "")
    base = Path(web_root) / "watch-sessions"
    if session:
        pattern = f"*/{session}/formal-asr/segments/{segment_dir}/state.json"
    else:
        pattern = f"*/*/formal-asr/segments/{segment_dir}/state.json"
    candidates = glob_json(base, pattern)
    if len(candidates) != 1:
        return None, {
            "source": "segment_transcript",
            "status": "ambiguous" if candidates else "missing",
            "candidate_files": len(candidates),
        }
    state = _read_json_cached(candidates[0])
    rows = [
        row for row in ((state or {}).get("rows") or [])
        if isinstance(row, Mapping) and str(row.get("segment_id") or "") == segment_id
    ]
    if len(rows) != 1:
        return None, {
            "source": "segment_transcript",
            "status": "row_ambiguous" if rows else "row_missing",
            "rows": len(rows),
        }
    row_started = _parse_datetime(rows[0].get("absolute_started_at"))
    reported = _reported_datetime({"evidence": dict(evidence)})
    if row_started is None:
        return None, {"source": "segment_transcript", "status": "row_time_missing"}
    if reported is None:
        return None, {"source": "segment_transcript", "status": "reported_at_missing"}
    delta = abs((row_started - reported).total_seconds())
    if delta > SEGMENT_OCCURRENCE_TOLERANCE_SECONDS:
        return None, {
            "source": "segment_transcript",
            "status": "out_of_tolerance",
            "delta_seconds": round(delta, 3),
        }
    return row_started, {
        "source": "segment_transcript",
        "status": "matched",
        "segment_id": segment_id,
        "segment_started_at": row_started.isoformat(),
        "delta_seconds": round(delta, 3),
        "state_file": str(candidates[0]),
    }


def _prepare_event(
    event: Mapping[str, Any],
    source_start: datetime | None,
    *,
    web_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a copy of *event* plus a description of any safe time repair.

    Valid occurrence timestamps are preserved verbatim. The only repairs are:

    * a missing/unparseable occurrence with a safe offset and source start;
    * a legacy absolute that is on a different local date from the source
      start plus offset and carries no explicit reported/occurrence metadata.
    """

    repaired = dict(event)
    time_info = (
        dict(event.get("time")) if isinstance(event.get("time"), Mapping) else {}
    )
    repaired["time"] = time_info
    evidence = event.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    info: dict[str, Any] = {
        "repaired": False,
        "basis": None,
        # 【返工·A】审计字段同样不留 sentinel 原值：'2025-01-01' 只要出现在响应里，
        # 就会被前端/日志/脚本当成真实日期读走。这里改成 None 并标注来源。
        "original_absolute": (
            None if _is_sentinel_datetime(time_info.get("absolute")) else time_info.get("absolute")
        ),
        "relative_seconds": _relative_seconds(event),
    }

    occurrence = _occurrence_datetime(event)
    candidate = _candidate_from_offset(event, source_start)

    # 【2026-10-24 时间语义】precision=unknown 却有 absolute（且等于 reported_at）⇒
    # 那是捕获时刻，不是发生时刻。记录指令可以按"近似"展示，普通事件一律回到 unknown。
    masquerade = _capture_time_masquerade(event)
    if masquerade is not None:
        if _capture_statement(event):
            time_info["precision"] = "approximate"
            time_info["display_basis"] = "capture_statement"
            time_info["display_label"] = _display_label(masquerade, "capture_statement")
            info.update(
                {
                    "repaired": False,
                    "basis": "capture_statement",
                    "capture_statement_time": masquerade.isoformat(),
                    "time_semantics": "capture_time_as_approximate_occurrence",
                }
            )
            _apply_time_contract(repaired, masquerade, info)
            return repaired, info
        time_info["capture_time"] = masquerade.isoformat()
        time_info["absolute"] = None
        time_info["precision"] = "unknown"
        time_info.pop("absolute_ended_at", None)
        info.update(
            {
                "repaired": False,
                "basis": "capture_time_not_occurrence",
                "capture_time": masquerade.isoformat(),
            }
        )
        _apply_time_contract(repaired, None, info)
        return repaired, info

    if occurrence is None:
        # 【2026-10-24】B 类严格回填：只在「同 segment id 的转录行」与 reported_at 相差 ≤2s 时
        # 才认这个时间；其余（段缺失/行歧义/超差）保持 unknown，绝不按偏移硬凑。
        # A 类 session-aggregator（无 segment_id）在这里天然不走这条路径。
        segment_time, segment_evidence = _segment_row_evidence(web_root, evidence)
        if segment_evidence is not None:
            evidence_copy = dict(evidence)
            evidence_copy["occurrence_time_evidence"] = segment_evidence
            repaired["evidence"] = evidence_copy
            info["segment_occurrence_evidence"] = segment_evidence
        if segment_time is not None:
            time_info["absolute"] = segment_time.isoformat()
            time_info["precision"] = "inferred"
            time_info["occurrence_source"] = "segment_transcript"
            occurrence = segment_time
            info.update(
                {
                    "repaired": True,
                    "basis": "segment_transcript_same_id",
                    "repaired_absolute": segment_time.isoformat(),
                }
            )
            _apply_time_contract(repaired, occurrence, info)
            return repaired, info
        if str(evidence.get("segment_id") or "").strip():
            # 【返工 2026-10-24】该事件本来就有段级证据，取不到（缺失/歧义/超 2s）就是"不知道"，
            # 绝不允许再用 source_start+offset 猜一个时刻 —— 否则等于伪造精确时间。
            _apply_time_contract(repaired, None, info)
            return repaired, info
        if candidate is not None and _safe_to_repair_missing_time(event):
            time_info["repaired_absolute"] = candidate.isoformat()
            if time_info.get("absolute") not in (None, ""):
                time_info["repaired_from_absolute"] = time_info.get("absolute")
            time_info["absolute"] = candidate.isoformat()
            info.update(
                {
                    "repaired": True,
                    "basis": "source_start_plus_offset",
                    "repaired_absolute": candidate.isoformat(),
                }
            )
        occurrence = _occurrence_datetime(repaired)
        _apply_time_contract(repaired, occurrence, info)
        return repaired, info

    if _legacy_time_is_invalid(event, occurrence, candidate) and candidate is not None:
        time_info["repaired_from_absolute"] = time_info.get("absolute")
        time_info["absolute"] = candidate.isoformat()
        time_info["repaired_absolute"] = candidate.isoformat()
        occurrence = candidate
        info.update(
            {
                "repaired": True,
                "basis": "legacy_absolute_replaced_with_source_offset",
                "repaired_absolute": candidate.isoformat(),
            }
        )
    _apply_time_contract(repaired, occurrence, info)
    return repaired, info


def repair_event_time(
    event: Mapping[str, Any], source_start: datetime | None
) -> dict[str, Any]:
    """Compatibility helper used by older read-only diagnostics."""

    repaired, _info = _prepare_event(event, source_start)
    return repaired


def _temporal_fields(
    event: Mapping[str, Any], directory_date: str | None
) -> dict[str, Any]:
    occurrence = _occurrence_datetime(event)
    reported = _reported_datetime(event)
    if occurrence is not None:
        effective_date = occurrence.date().isoformat()
        date_basis = "occurrence"
    elif reported is not None:
        effective_date = reported.date().isoformat()
        date_basis = "reported_local"
    elif directory_date:
        effective_date = directory_date
        date_basis = "artifact_directory"
    else:
        effective_date = None
        date_basis = "unknown"
    time_basis = _event_time_basis(
        event, occurrence=occurrence, reported=reported, directory_date=directory_date
    )
    display_time = None
    if occurrence is not None and time_basis in (
        "exact_occurrence",
        "inferred_occurrence",
        "capture_statement",
    ):
        display_time = {
            "value": occurrence.isoformat(),
            "precision": {
                "capture_statement": "approximate",
                "inferred_occurrence": "inferred",
            }.get(time_basis, "exact"),
            "label": _display_label(occurrence, time_basis),
            "basis": time_basis,
        }
    return {
        "occurred_at": occurrence.isoformat() if occurrence is not None else None,
        "occurrence_date": occurrence.date().isoformat()
        if occurrence is not None
        else None,
        "reported_at": reported.isoformat() if reported is not None else None,
        "reported_date": reported.date().isoformat() if reported is not None else None,
        "effective_date": effective_date,
        "date_basis": date_basis,
        "date_is_unknown": occurrence is None,
        # 【2026-10-24】明确展示依据：exact_occurrence / inferred_occurrence /
        # capture_statement（按讲述时间，只能"约"）/ reported_local / artifact_directory / unknown
        "event_time_basis": time_basis,
        "display_time": display_time,
    }


def _summary_source_start(path: Path) -> datetime | None:
    try:
        file_id = path.parents[2].name
        status_path = path.parents[4] / "status.json"
    except IndexError:
        return None
    if not resolve_readable_json_path(status_path).is_file():
        return None
    try:
        status = read_json(status_path)
    except (OSError, json.JSONDecodeError):
        return None
    files = status.get("files") if isinstance(status, Mapping) else None
    if not isinstance(files, list):
        return None
    source = next(
        (
            item
            for item in files
            if isinstance(item, Mapping)
            and str(item.get("file_id") or "") == file_id
        ),
        None,
    )
    if source is None:
        return None
    from life_logger.timecode import parse_timestamp_from_name

    for key in ("original_name", "relative_path"):
        name = str(source.get(key) or "").strip()
        if not name:
            continue
        parsed = parse_timestamp_from_name(Path(name))
        if parsed is not None:
            return parsed.replace(tzinfo=SHANGHAI)
    for key in ("effective_start_at", "inferred_start_at"):
        parsed = _parse_datetime(source.get(key))
        if parsed is not None:
            return parsed
    return None


def _load_job_corrections(job_dir: Path) -> dict[str, dict[str, Any]]:
    path = job_dir / "event-corrections.json"
    if not resolve_readable_json_path(path).is_file():
        return {}
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return {}
    corrections = payload.get("corrections") if isinstance(payload, Mapping) else None
    if not isinstance(corrections, Mapping):
        return {}
    return {
        str(key): dict(value)
        for key, value in corrections.items()
        if isinstance(value, Mapping)
    }


def _verified_event_artifact(
    job_dir: Path | None, artifact_path: object
) -> tuple[str, Path] | None:
    """Return (job-relative path, absolute path) only for a real sibling events.json."""

    if job_dir is None:
        return None
    relative = str(artifact_path or "").strip().replace("\\", "/")
    if not relative or not relative.endswith("events.json"):
        return None
    job_resolved = job_dir.resolve()
    target = (job_resolved / relative).resolve()
    if target == job_resolved or job_resolved not in target.parents:
        return None
    if not target.is_file() or target.name != "events.json":
        return None
    return target.relative_to(job_resolved).as_posix(), target


def _summary_event_artifact(path: Path, job_dir: Path | None) -> tuple[str, Path] | None:
    return _verified_event_artifact(
        job_dir, path.with_name("events.json")
    )


#: 首页“记一笔”写的是 `<date>/events.json`，没有 result 这一层，没说时间就会丢。
#: 只从修复当天起生效：更早被丢的条目不自动冒出来，以免改动已冻结的历史账。
_HOME_INPUT_DATE_FROM = "2026-11-05"


def _directory_date_from_artifact_path(
    artifact_path: str | None,
) -> str | None:
    if not artifact_path:
        return None
    parts = Path(artifact_path).parts
    if "result" in parts:
        result_index = parts.index("result")
        if result_index + 1 >= len(parts):
            return None
        candidate = parts[result_index + 1]
    elif parts and parts[0] >= _HOME_INPUT_DATE_FROM:
        candidate = parts[0]
    else:
        return None
    try:
        return datetime.strptime(candidate, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _apply_correction(
    event: Mapping[str, Any], correction: Mapping[str, Any]
) -> tuple[dict[str, Any], bool]:
    corrected = dict(event)
    changes = correction.get("changes")
    changes = changes if isinstance(changes, Mapping) else {}
    for key in ("title", "categories", "activity_type", "data"):
        if key in changes:
            corrected[key] = copy.deepcopy(changes[key])
    if isinstance(changes.get("time"), Mapping):
        corrected["time"] = {
            **(dict(event.get("time")) if isinstance(event.get("time"), Mapping) else {}),
            **copy.deepcopy(dict(changes["time"])),
        }
    if "note" in changes:
        evidence = (
            dict(corrected.get("evidence"))
            if isinstance(corrected.get("evidence"), Mapping)
            else {}
        )
        evidence["correction_note"] = changes["note"]
        corrected["evidence"] = evidence
    rejected = bool(correction.get("rejected"))
    corrected["correction_status"] = "rejected" if rejected else "corrected"
    corrected["corrected_at"] = correction.get("updated_at")
    corrected["correction_provenance"] = {
        "job_id": correction.get("job_id"),
        "artifact_path": correction.get("artifact_path"),
        "event_id": correction.get("event_id"),
        "updated_at": correction.get("updated_at"),
        "rejected": rejected,
        "note": changes.get("note"),
    }
    if rejected:
        corrected["review_status"] = "rejected"
    else:
        corrected["review_status"] = "confirmed"
    return corrected, rejected


def _manifest_source_start(manifest: Mapping[str, Any]) -> datetime | None:
    segments = manifest.get("segments")
    if isinstance(segments, list):
        for segment in sorted(
            (item for item in segments if isinstance(item, Mapping)),
            key=lambda item: int(item.get("sequence") or 0),
        ):
            for key in ("started_at_local", "started_at"):
                parsed = _parse_datetime(segment.get(key))
                if parsed is not None:
                    return parsed
    for key in ("started_at_local", "started_at"):
        parsed = _parse_datetime(manifest.get(key))
        if parsed is not None:
            return parsed
    return None


def _manifest_window(
    manifest: Mapping[str, Any],
) -> tuple[datetime | None, datetime | None]:
    start = _manifest_source_start(manifest)
    end = _parse_datetime(manifest.get("ended_at"))
    segments = manifest.get("segments")
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, Mapping):
                continue
            segment_start = _parse_datetime(segment.get("started_at_local"))
            if segment_start is None:
                continue
            try:
                duration_ms = float(segment.get("duration_ms") or 0)
            except (TypeError, ValueError):
                duration_ms = 0.0
            candidate_end = segment_start + timedelta(milliseconds=duration_ms)
            if end is None or candidate_end > end:
                end = candidate_end
    return start, end


def _content_identity(kind: str, event: Mapping[str, Any], source: str = "") -> str:
    evidence = event.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    time_info = event.get("time")
    time_info = time_info if isinstance(time_info, Mapping) else {}
    payload = "|".join(
        (
            str(event.get("title") or ""),
            str(evidence.get("quote") or ""),
            str(time_info.get("absolute") or evidence.get("reported_at") or ""),
            str(time_info.get("relative_seconds") or ""),
            ",".join(str(item) for item in event.get("categories") or []),
            source,
        )
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]
    return f"{kind}-content:{digest}"


def _event_key(kind: str, event: Mapping[str, Any], source: str = "") -> str:
    event_id = str(event.get("event_id") or "").strip()
    if event_id:
        return f"{kind}:{event_id}"
    return _content_identity(kind, event, source)


def _normalize_identity_value(value: Any) -> str:
    return "".join(
        str(char).casefold() for char in str(value or "") if str(char).isalnum()
    )


def _item_identity(value: Any) -> str:
    """Normalize quantity/packaging noise without changing the fact identity.

    "桂圆一包" and "桂圆" are the same item; "桂圆" and "柠檬茶" are not.
    Amount is never part of this identity.
    """

    normalized = _normalize_identity_value(value)
    if not normalized:
        return ""
    stripped = _QUANTITY_UNIT_PATTERN.sub("", normalized)
    for prefix in ("购买", "买了", "购入", "便签"):
        if stripped.startswith(prefix) and len(stripped) > len(prefix):
            stripped = stripped[len(prefix) :]
    return stripped or normalized


def _entity_discriminator(event: Mapping[str, Any]) -> str:
    """Evidence-based fact discriminator, independent of pipeline event ids.

    The same utterance can yield multiple distinct facts (for example a bunch
    of longans and a lemon tea in one sentence).  Item/description text is the
    strongest non-financial discriminator available at read time.  Amount is
    deliberately not required here: a missing amount must not become identity,
    and a model-corrected amount must not create a second entity.
    """

    data = event.get("data")
    if isinstance(data, Mapping):
        for category in ("expense", "food"):
            payload = data.get(category)
            if not isinstance(payload, Mapping):
                continue
            for key in ("item", "merchant", "detail", "type"):
                value = _item_identity(payload.get(key))
                if value:
                    return value
    title = _item_identity(event.get("title"))
    if title:
        return title
    return ",".join(
        sorted(_normalize_identity_value(item) for item in event.get("categories") or [])
    )


def _canonical_entity_key(event: Mapping[str, Any]) -> str | None:
    """Stable entity key that survives live -> formal artifact arrival."""

    quote = _normalized_quote(event)
    timestamp = _deadline_timestamp(event)
    if not quote or timestamp is None:
        return None
    discriminator = _entity_discriminator(event)
    payload = "|".join((quote, format(round(timestamp, 3), ".3f"), discriminator))
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]
    return f"entity:{digest}"


def _deadline_timestamp(event: Mapping[str, Any]) -> float | None:
    """Timestamp used only to decide whether two artifacts describe the same
    utterance. Prefer explicit reported_at because formal artifacts may have no
    occurrence absolute for an undated fact."""

    for parsed in (_reported_datetime(event), _occurrence_datetime(event)):
        if parsed is not None:
            return parsed.timestamp()
    return None


def _dedup_key(event: Mapping[str, Any]) -> tuple[str, float] | None:
    quote = _normalized_quote(event)
    timestamp = _deadline_timestamp(event)
    if not quote or timestamp is None:
        return None
    return quote, round(timestamp, 3)


def _copy_non_financial(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _copy_non_financial(item)
            for key, item in value.items()
            if not _is_financial_key(key)
        }
    if isinstance(value, list):
        return [_copy_non_financial(item) for item in value]
    return copy.deepcopy(value)


def _merge_missing_non_financial(
    target: dict[str, Any], source: Mapping[str, Any], prefix: str = ""
) -> list[str]:
    """Recursively fill only missing/non-null fields from *source*.

    Explicit ``0``/``False`` values in the preferred target are real values and
    are never replaced.  Financial fields are never copied at any depth.
    """

    filled: list[str] = []
    for key, source_value in source.items():
        if _is_financial_key(key):
            continue
        path = f"{prefix}.{key}" if prefix else str(key)
        current = target.get(key)
        if current is None or current == "":
            if source_value is None or source_value == "":
                continue
            target[key] = copy.deepcopy(source_value)
            filled.append(path)
            continue
        if isinstance(current, dict) and isinstance(source_value, Mapping):
            filled.extend(
                _merge_missing_non_financial(current, source_value, path)
            )
    return filled


def _event_amount(event: Mapping[str, Any]) -> float | None:
    data = event.get("data")
    if not isinstance(data, Mapping):
        return None
    expense = data.get("expense")
    if not isinstance(expense, Mapping):
        return None
    amount = expense.get("amount")
    if isinstance(amount, (int, float)):
        return float(amount)
    return None


def _mapping(val: Any) -> Mapping[str, Any]:
    return val if isinstance(val, Mapping) else {}


def _text(val: Any) -> str:
    return str(val or "")


_PURCHASE_NEGATION_RE = re.compile(
    r"(?:没买|没花钱|没付钱|不买了|不买|不想买|算了|不要了|退款|未消费|放弃购买)"
)
_NON_FACT_STATES_SET = frozenset({
    "negated",
    "cancelled",
    "canceled",
    "rejected",
    "aborted",
    "hypothetical",
    "plan",
    "planned",
    "question",
})
_CN_NUM_DIGITS = {
    0: "零", 1: "一", 2: "二", 3: "三", 4: "四", 5: "五",
    6: "六", 7: "七", 8: "八", 9: "九", 10: "十"
}


def _cn_int_forms(num: int) -> list[str]:
    if num <= 0 or num >= 10000:
        return []
    if num == 2:
        return ["二", "两"]
    if num <= 10:
        return [_CN_NUM_DIGITS[num]]
    if num < 20:
        return [f"十{_CN_NUM_DIGITS[num % 10]}"]
    if num < 100:
        tens, ones = divmod(num, 10)
        s = ("" if tens == 1 else _CN_NUM_DIGITS[tens]) + "十" + (_CN_NUM_DIGITS[ones] if ones else "")
        return [s]
    if num < 1000 and num % 100 == 0:
        return [f"{_CN_NUM_DIGITS[num // 100]}百"]
    return []


def _quote_has_verbatim_amount(quote: str, amount: float) -> bool:
    if not quote or amount <= 0:
        return False
    int_val = int(amount) if amount.is_integer() else None
    str_val = str(int_val if int_val is not None else amount)
    if re.search(rf"(?<![\d.]){re.escape(str_val)}(?![\d.])", quote):
        return True
    if int_val is not None:
        for cn in _cn_int_forms(int_val):
            if cn in quote:
                return True
    return False


FINANCIAL_ENRICHMENT_ENABLED = False


def _can_enrich_financial_evidence(
    formal_event: Mapping[str, Any],
    live_event: Mapping[str, Any],
    live_amount: float,
) -> tuple[bool, str | None]:
    formal_evidence = _mapping(formal_event.get("evidence"))
    live_evidence = _mapping(live_event.get("evidence"))

    # 1. 否决门：formal 显式否定 / 撤回 / 取消
    if (
        formal_event.get("review_status") == "rejected"
        or formal_event.get("correction_status") == "rejected"
    ):
        return False, "formal_rejected"

    formal_text = f"{formal_event.get('title') or ''} {formal_evidence.get('quote') or ''}"
    if _PURCHASE_NEGATION_RE.search(formal_text):
        return False, "formal_explicit_negation"

    for s in (formal_event.get("event_state"), formal_evidence.get("event_state")):
        if str(s or "").strip().lower() in _NON_FACT_STATES_SET:
            return False, f"formal_state_negated:{s}"

    for c in (formal_event.get("claim_type"), formal_evidence.get("claim_type")):
        if str(c or "").strip().lower() in _NON_FACT_STATES_SET:
            return False, f"formal_claim_negated:{c}"

    # 2. 否决门：说话人 / POS 播报
    live_activity = str(live_event.get("activity_type") or "").strip().lower()
    formal_activity = str(formal_event.get("activity_type") or "").strip().lower()
    if live_activity == "pos_payment_broadcast" or str(live_evidence.get("title_source") or "") == "pos_broadcast":
        return False, "live_is_pos_broadcast"

    live_spk = str(live_evidence.get("source_label") or "").strip().lower()
    if live_spk not in {"self"}:
        return False, f"live_speaker_not_self:{live_spk}"

    formal_spk = str(formal_evidence.get("source_label") or "").strip().lower()
    if formal_spk and formal_spk not in {"self"}:
        return False, f"formal_speaker_not_self:{formal_spk}"

    # 3. 否决门：模型-only 金额不可补真值
    contract = live_evidence.get("expense_model_contract")
    if isinstance(contract, Mapping):
        status = str(contract.get("status") or "").strip().lower()
        if status == "rejected" or contract.get("reason") in {"amount_not_verbatim", "amount_missing"}:
            return False, "live_expense_contract_rejected"

    quote = _text(live_evidence.get("quote"))
    if not _quote_has_verbatim_amount(quote, live_amount):
        return False, "model_only_amount_without_quote_evidence"

    # 4. 否决门：交易类型冲突与实体屏障
    non_purchase = {"withdrawal", "refund", "topup", "account_transfer"}
    if (formal_activity in non_purchase or live_activity in non_purchase) and formal_activity != live_activity:
        return False, f"activity_type_conflict:{formal_activity}_vs_{live_activity}"

    for key, ns in [("order_id", "order"), ("transaction_id", "transaction"), ("activity_id", "activity")]:
        val_f = formal_event.get(key) or formal_evidence.get(key)
        val_l = live_event.get(key) or live_evidence.get(key)
        if val_f and val_l and str(val_f).strip() != str(val_l).strip():
            return False, f"{ns}_id_conflict"

    # 5. 强身份门禁：完整双侧源物理坐标一致
    from life_logger.exact_projection_identity import extract_source_coordinates

    coord_f = extract_source_coordinates(formal_event)
    coord_l = extract_source_coordinates(live_event)
    if not coord_f["recording_id"] or not coord_l["recording_id"]:
        return False, "recording_id_missing_coordinates"
    if not coord_f["segment_id"] or not coord_l["segment_id"]:
        return False, "segment_id_missing_coordinates"
    if coord_f["recording_id"] != coord_l["recording_id"]:
        return False, f"physical_source_recording_distinct:{coord_f['recording_id']}_vs_{coord_l['recording_id']}"
    if coord_f["segment_id"] != coord_l["segment_id"]:
        return False, f"physical_source_segment_distinct:{coord_f['segment_id']}_vs_{coord_l['segment_id']}"

    # 6. 实体判别名冲突
    formal_item = _entity_discriminator(formal_event)
    live_item = _entity_discriminator(live_event)
    if formal_item and live_item and formal_item != live_item:
        return False, f"entity_discriminator_conflict:{formal_item}_vs_{live_item}"

    # 7. 时间戳与原话一致性
    formal_ts = _deadline_timestamp(formal_event)
    live_ts = _deadline_timestamp(live_event)
    if formal_ts is None or live_ts is None or abs(formal_ts - live_ts) > 0.5:
        return False, "timestamp_mismatch"

    formal_quote = _normalize_identity_value(
        formal_evidence.get("quote") or formal_event.get("title")
    )
    live_quote = _normalize_identity_value(
        live_evidence.get("quote") or live_event.get("title")
    )
    if not formal_quote or not live_quote or (
        formal_quote != live_quote
        and formal_quote not in live_quote
        and live_quote not in formal_quote
    ):
        return False, "quote_mismatch"

    return True, None


def _enrich_missing_non_financial(
    formal_event: dict[str, Any], live_event: Mapping[str, Any]
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    formal_data = formal_event.get("data")
    if not isinstance(formal_data, Mapping):
        return conflicts
    live_data = live_event.get("data")
    if not isinstance(live_data, Mapping):
        return conflicts
    filled_fields: list[str] = []
    for category, live_payload in live_data.items():
        if not isinstance(live_payload, Mapping):
            continue
        formal_payload = formal_data.get(category)
        if not isinstance(formal_payload, Mapping):
            formal_data[category] = _copy_non_financial(live_payload)
            filled_fields.append(str(category))
            continue
        filled_fields.extend(
            _merge_missing_non_financial(
                formal_payload, live_payload, prefix=str(category)
            )
        )
    if filled_fields:
        formal_event.setdefault("enrichment_provenance", []).append(
            {
                "source_kind": "live",
                "event_id": live_event.get("event_id"),
                "fields": filled_fields,
            }
        )

    formal_amount = _event_amount(formal_event)
    live_amount = _event_amount(live_event)
    if (
        formal_amount is not None
        and live_amount is not None
        and formal_amount != live_amount
    ):
        conflicts.append(
            {
                "field": "expense.amount",
                "formal": formal_amount,
                "live": live_amount,
                "resolution": "formal_preferred; live value kept on alias",
            }
        )
    elif (
        formal_amount is None
        and live_amount is not None
        and FINANCIAL_ENRICHMENT_ENABLED
    ):
        can_enrich, reason = _can_enrich_financial_evidence(
            formal_event, live_event, live_amount
        )
        if can_enrich:
            if not isinstance(formal_data.get("expense"), dict):
                formal_data["expense"] = (
                    dict(formal_data.get("expense"))
                    if isinstance(formal_data.get("expense"), Mapping)
                    else {}
                )
            formal_data["expense"]["amount"] = live_amount
            live_data_expense = (
                live_data.get("expense")
                if isinstance(live_data.get("expense"), Mapping)
                else {}
            )
            currency = live_data_expense.get("currency") or "CNY"
            formal_data["expense"]["currency"] = currency
            for k in (
                "amount_evidence",
                "transaction_evidence",
                "payment_channel",
                "item",
            ):
                val = live_data_expense.get(k)
                if val is not None and not formal_data["expense"].get(k):
                    formal_data["expense"][k] = copy.deepcopy(val)
            financial_prov = {
                "source_kind": "live",
                "event_id": live_event.get("event_id"),
                "fields": ["expense.amount", "expense.currency"],
                "financial": True,
                "amount": live_amount,
                "currency": currency,
                "resolution": "formal_omitted_field_live_evidence_preserved",
            }
            prov_list = formal_event.setdefault("enrichment_provenance", [])
            prov_list.insert(0, financial_prov)
        else:
            conflicts.append(
                {
                    "field": "expense.amount",
                    "formal": None,
                    "live": live_amount,
                    "resolution": "formal_preferred; live value kept on alias",
                    "conflict_reason": reason,
                }
            )
    formal_data_expense = formal_data.get("expense")
    live_data_expense = live_data.get("expense")
    if isinstance(formal_data_expense, Mapping) and isinstance(
        live_data_expense, Mapping
    ):
        formal_currency = formal_data_expense.get("currency")
        live_currency = live_data_expense.get("currency")
        if formal_currency and live_currency and formal_currency != live_currency:
            conflicts.append(
                {
                    "field": "expense.currency",
                    "formal": formal_currency,
                    "live": live_currency,
                    "resolution": "formal_preferred; live value kept on alias",
                }
            )
    formal_categories = tuple(
        sorted(str(item) for item in formal_event.get("categories") or [])
    )
    live_categories = tuple(
        sorted(str(item) for item in live_event.get("categories") or [])
    )
    if formal_categories != live_categories:
        conflicts.append(
            {
                "field": "categories",
                "formal": list(formal_categories),
                "live": list(live_categories),
                "resolution": "formal_preferred; live categories kept on alias",
            }
        )
    return conflicts


def _alias_payload(live_event: Mapping[str, Any], match_basis: str) -> dict[str, Any]:
    event_payload = {
        key: copy.deepcopy(value)
        for key, value in live_event.items()
        if key
        not in {
            "aliases",
            "conflicts",
            "provenance",
            "canonical_event_id",
        }
    }
    return {
        "event_id": live_event.get("event_id"),
        "event_key": live_event.get("event_key"),
        "source_kind": "live",
        "source_artifact": live_event.get("source_artifact"),
        "source_session": live_event.get("source_session"),
        "source_device": live_event.get("source_device"),
        "match_basis": match_basis,
        "reported_at": live_event.get("reported_at"),
        "occurred_at": live_event.get("occurred_at"),
        "review_status": live_event.get("review_status"),
        "event": event_payload,
    }


def _select_formal_match(
    live_event: Mapping[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Choose the best formal peer for a live event (used by pair scoring)."""

    pair = _best_pair(candidates, [live_event])
    if pair is None:
        return sorted(candidates, key=_sort_key)[0], "exact_quote_and_time"
    formal, _live, basis, _score = pair
    return formal, basis


def _pair_score(
    formal: Mapping[str, Any], live: Mapping[str, Any]
) -> tuple[int, str]:
    formal_jobs = set(formal.get("source_job_ids") or [])
    live_jobs = set(live.get("source_job_ids") or [])
    lineage = bool(formal_jobs and live_jobs and formal_jobs & live_jobs)
    formal_categories = set(str(item) for item in formal.get("categories") or [])
    live_categories = set(str(item) for item in live.get("categories") or [])
    formal_item = _entity_discriminator(formal)
    live_item = _entity_discriminator(live)
    formal_amount = _event_amount(formal)
    live_amount = _event_amount(live)

    score = 0
    if lineage:
        score += 100
    if formal_item and live_item and formal_item == live_item:
        score += 50
    if formal_categories and live_categories and formal_categories & live_categories:
        score += 25
    if (
        formal_amount is not None
        and live_amount is not None
        and formal_amount == live_amount
    ):
        score += 15
    title_equal = _normalize_identity_value(formal.get("title")) == _normalize_identity_value(
        live.get("title")
    )
    if title_equal:
        score += 10

    if lineage:
        return score, "manifest_completion_job"
    live_timestamp = _deadline_timestamp(live)
    session_start = live.get("_session_start_epoch")
    session_end = live.get("_session_end_epoch")
    if (
        live_timestamp is not None
        and isinstance(session_start, (int, float))
        and isinstance(session_end, (int, float))
        and session_start - 300 <= live_timestamp <= session_end + 300
    ):
        return score, "session_time_window"
    return score, "exact_quote_and_time"


def _best_pair(
    formal_candidates: list[dict[str, Any]],
    live_candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], str, int] | None:
    best: tuple[dict[str, Any], dict[str, Any], str, int] | None = None
    for formal in formal_candidates:
        for live in live_candidates:
            score, basis = _pair_score(formal, live)
            candidate = (formal, live, basis, score)
            if best is None or candidate[3] > best[3]:
                best = candidate
    return best


def _assign_one_to_one(
    formal_candidates: list[dict[str, Any]],
    live_candidates: list[dict[str, Any]],
) -> tuple[dict[str, list[tuple[dict[str, Any], str]]], set[str]]:
    """Assign live events to distinct formal events within one quote/time group."""

    pairs: list[tuple[int, int, int, str, dict[str, Any], dict[str, Any]]] = []
    for formal_index, formal in enumerate(formal_candidates):
        for live_index, live in enumerate(live_candidates):
            score, basis = _pair_score(formal, live)
            pairs.append((score, -formal_index, -live_index, basis, formal, live))
    pairs.sort(key=lambda item: item[:3], reverse=True)

    assignments: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    used_formal: set[int] = set()
    used_live: set[int] = set()
    for _score, neg_formal, neg_live, basis, formal, live in pairs:
        formal_index = -neg_formal
        live_index = -neg_live
        if formal_index in used_formal or live_index in used_live:
            continue
        used_formal.add(formal_index)
        used_live.add(live_index)
        assignments[str(formal["pipeline_event_key"])].append((live, basis))
    unmatched_live = {
        str(live["pipeline_event_key"])
        for index, live in enumerate(live_candidates)
        if index not in used_live
    }
    return assignments, unmatched_live



def _sort_key(event: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(
            event.get("occurred_at")
            or event.get("reported_at")
            or event.get("effective_date")
            or ""
        ),
        str(event.get("event_key") or ""),
        str(event.get("event_id") or ""),
    )


def _date_basis_counts(events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    return dict(
        Counter(str(event.get("date_basis") or "unknown") for event in events)
    )


def _source_kind_counts(events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    return dict(
        Counter(
            str(
                event.get("canonical_source_kind")
                or event.get("source_kind")
                or "unknown"
            )
            for event in events
        )
    )


def _visible_live_event(event: Mapping[str, Any]) -> bool:
    """Keep live events but drop the raw question-candidate lane.

    This mirrors the conservative part of the existing web read layer without
    importing the web module (and without creating a circular dependency). The
    raw event remains in the artifact; only the canonical read view drops the
    clearly non-fact question candidates.
    """

    evidence = event.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    if str(event.get("source_type") or "") == "manual":
        return True
    if str(event.get("review_status") or "") == "confirmed":
        return True
    return str(evidence.get("candidate_type") or "") != "question_candidate"


def _add_view_fields(
    prepared: dict[str, Any],
    *,
    source_kind: str,
    artifact: Path,
    directory_date: str | None,
    event_key: str,
    repair_info: Mapping[str, Any],
    job_id: str | None = None,
    file_id: str | None = None,
    source_job_ids: Iterable[str] = (),
    device: str | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    temporal = _temporal_fields(prepared, directory_date)
    prepared.update(temporal)
    prepared["source_kind"] = source_kind
    prepared["canonical_source_kind"] = source_kind
    prepared["source_artifact"] = str(artifact.resolve())
    prepared["source_directory_date"] = directory_date
    prepared["source_job_id"] = job_id
    prepared["source_job_ids"] = sorted(
        {
            str(item)
            for item in source_job_ids
            if item
        }
        | ({str(job_id)} if job_id else set())
    )
    prepared["source_file_id"] = file_id
    prepared["source_device"] = device
    prepared["source_session"] = session
    prepared["pipeline_event_key"] = event_key
    prepared["event_key"] = _canonical_entity_key(prepared) or event_key
    prepared["canonical_event_id"] = str(prepared.get("event_id") or prepared["event_key"])
    prepared["time_repair"] = dict(repair_info)
    prepared["aliases"] = []
    prepared["conflicts"] = []
    prepared["provenance"] = [
        {
            "source_kind": source_kind,
            "event_id": prepared.get("event_id"),
            "event_key": prepared["event_key"],
            "pipeline_event_key": prepared["pipeline_event_key"],
            "artifact": prepared["source_artifact"],
            "job_id": job_id,
            "file_id": file_id,
            "device": device,
            "session": session,
            "directory_date": directory_date,
            "model_branch": "root",
            "review_status": prepared.get("review_status"),
        }
    ]
    return prepared


def _rejected_tombstone(
    prepared: Mapping[str, Any],
    artifact_path: str | None,
) -> dict[str, Any] | None:
    key = _dedup_key(prepared)
    if key is None:
        return None
    return {
        "dedup_key": key,
        "event_id": prepared.get("event_id"),
        "source_job_ids": list(prepared.get("source_job_ids") or []),
        "artifact_path": artifact_path,
        "title": prepared.get("title"),
    }


#: 扫描结果进程内缓存：TTL 短（10s），只缓存**加载结果**，不改变任何语义。
#: 目的：用户切日期时前端会并发打多个 /api/daily；原来每个日期都全量重扫
#: 220 个 summary + 41 个 session，在 ASR 满载时把 0.2s 放大到 4s+。
_SCAN_CACHE_TTL_SECONDS = 10.0
_SCAN_CACHE_MAX_ENTRIES = 64
_SCAN_CACHE: dict[tuple, tuple[float, dict]] = {}
_SCAN_CACHE_LOCK = threading.Lock()
_SCAN_INFLIGHT: dict[tuple, threading.Event] = {}
#: 失效代数：每次 invalidate_scan_cache() +1。
#: producer 在开始扫描时记下代数，写回缓存前比对 —— 若期间发生过失效，
#: 说明它扫的是旧文件，结果直接丢弃（下一个请求重扫），不会把陈旧数据写回。
_SCAN_GENERATION = 0

#: 跨进程失效代际文件（写在每个 store root 下）。
#: 为什么需要：Web 读进程与 processing_worker / Watch ASR 子进程 / CLI 是**不同进程**，
#: 进程内的 `invalidate_scan_cache()` 只能清自己；原来靠 10s TTL 兜底，写完后
#: 最多 10s 看不到新事件。这里用一个极小的代际文件做跨进程协议：
#:   * 写进程改完文件 → `bump_scan_epoch(root)`（原子 os.replace，内容是新 token）；
#:   * 读进程每次 `build_event_view(use_cache=True)` 前读一次该文件（几微秒）；
#:     token 与上次不同 → 立即清本进程缓存并 +1 代数（在途扫描结果也不再写回）。
#: 不依赖 mtime 精度、不依赖固定 TTL，也不需要 IPC/端口。
_EPOCH_FILENAME = ".event-cache-epoch"
_EPOCH_TOKENS: dict[str, str | None] = {}
_EPOCH_LOCK = threading.Lock()


def _root_key(root: Path | str | None) -> str:
    """缓存与失效统一用 realpath，避免 /tmp -> /private/tmp 两侧不一致。"""
    if root is None:
        return ""
    try:
        return os.path.realpath(str(root))
    except OSError:
        return str(root)


def _epoch_path(root: Path | str) -> Path:
    return Path(_root_key(root)) / _EPOCH_FILENAME


def read_scan_epoch(root: Path | str) -> str | None:
    """读当前 root 的跨进程代际 token（文件不存在 = None）。"""
    try:
        token = _epoch_path(root).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token or None


def _write_epoch_token(root: Path | str) -> str | None:
    """原子写代际 token（只负责通知，不碰本进程缓存）。失败返回 None。"""
    token = f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    path = _epoch_path(root)
    try:
        # 原实现 tmp 名含 pid（同进程多线程撞名）且没有 fsync：掉电后其它进程不会失效。
        from life_logger.atomic import write_bytes_atomic

        write_bytes_atomic(path, (token + "\n").encode("utf-8"), on_durability_error="report")
    except OSError:
        return None
    with _EPOCH_LOCK:
        _EPOCH_TOKENS[_root_key(root)] = token
    return token


def bump_scan_epoch(root: Path | str, reason: str = "") -> str | None:
    """**写进程**在产物落盘提交后调用：通知其它进程 + 清本进程缓存。

    返回新 token；写失败（只读目录等）返回 None，绝不抛给业务写路径。
    """
    token = _write_epoch_token(root)
    _apply_invalidation(_root_key(root))
    return token


def sync_scan_epoch(root: Path | str) -> bool:
    """**读进程**在扫描前调用：别的进程 bump 过就立即失效本进程缓存。

    返回 True 表示这次调用发现了变化并做了失效。
    """
    key = _root_key(root)
    token = read_scan_epoch(root)
    with _EPOCH_LOCK:
        seen = key in _EPOCH_TOKENS
        known = _EPOCH_TOKENS.get(key)
    if seen and known == token:
        return False
    with _EPOCH_LOCK:
        _EPOCH_TOKENS[key] = token
    _apply_invalidation(key)
    return True


def _apply_invalidation(root_key: str | None) -> int:
    """清缓存 + 代数 +1。root_key=None 表示全清；否则按 realpath 精确匹配。"""
    global _SCAN_GENERATION
    removed = 0
    with _SCAN_CACHE_LOCK:
        if root_key is None:
            removed = len(_SCAN_CACHE)
            _SCAN_CACHE.clear()
        else:
            for key in list(_SCAN_CACHE):
                if len(key) > 1 and _root_key(key[1]) == root_key:
                    _SCAN_CACHE.pop(key, None)
                    removed += 1
        _SCAN_GENERATION += 1
    return removed


def invalidate_scan_cache(root: Path | str | None = None) -> int:
    """显式失效扫描缓存（写路径必须调用）。

    - 同进程：清掉本进程条目并 +1 代数（在途产品结果不会被写回）。
    - 跨进程：**同时**原子写一个代际 token，别的读进程下一次扫描前会自行失效
      （见 `bump_scan_epoch` / `sync_scan_epoch`）。写失败不影响清理结果。
    - `root=None` 清全部条目（不写 token：调用方没指定 root）。
    - 路径统一 `realpath`，符号链接与真实路径混用也能清对。

    返回被清除的缓存条目数（不含其它进程的）。
    """
    if root is None:
        return _apply_invalidation(None)
    removed = _apply_invalidation(_root_key(root))
    _write_epoch_token(root)
    return removed


def _cached_scan(key: tuple, producer):
    """带 single-flight 的短 TTL 缓存：同一 key 并发只扫一次。"""
    now = time.monotonic()
    with _SCAN_CACHE_LOCK:
        hit = _SCAN_CACHE.get(key)
        if hit is not None and now - hit[0] <= _SCAN_CACHE_TTL_SECONDS:
            return hit[1]
        flight = _SCAN_INFLIGHT.get(key)
        owner = flight is None
        if owner:
            flight = threading.Event()
            _SCAN_INFLIGHT[key] = flight
    if not owner:
        flight.wait(timeout=30.0)
        with _SCAN_CACHE_LOCK:
            hit = _SCAN_CACHE.get(key)
        if hit is not None:
            return hit[1]
        return producer()
    generation = _SCAN_GENERATION
    try:
        value = producer()
        with _SCAN_CACHE_LOCK:
            if generation != _SCAN_GENERATION:
                # 扫描期间发生过写失效 → 这份结果是旧文件的，丢弃不缓存
                return value
            _SCAN_CACHE[key] = (time.monotonic(), value)
            if len(_SCAN_CACHE) > _SCAN_CACHE_MAX_ENTRIES:
                oldest = min(_SCAN_CACHE, key=lambda k: _SCAN_CACHE[k][0])
                _SCAN_CACHE.pop(oldest, None)
        return value
    finally:
        with _SCAN_CACHE_LOCK:
            _SCAN_INFLIGHT.pop(key, None)
        flight.set()


#: 单文件级 JSON 缓存：键 =（路径, mtime_ns, size）。
#: 语义安全：文件一改 key 就变，绝不会读到旧内容；命中时省掉 read_text + json.loads。
#: 注意：返回的是**同一个对象**，调用方不得原地修改 payload（现有加载器只读）。
_JSON_CACHE_TTL_SECONDS = 3600.0
_JSON_CACHE_MAX_ENTRIES = 8192
_JSON_CACHE: dict[tuple[str, int, int], tuple[float, Any]] = {}
_JSON_CACHE_LOCK = threading.Lock()


def _read_json_cached(path: Path) -> Any:
    # [2026-11-05] 中间 JSON 可能被压成 `x.json.gz`：未压缩优先，缺了才读压缩。
    # 缓存键必须用**实际读取的那个文件**（压缩文件的 mtime/size 与未压缩不同）。
    target = resolve_readable_json_path(path)
    stat = target.stat()
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    now = time.monotonic()
    with _JSON_CACHE_LOCK:
        hit = _JSON_CACHE.get(key)
        if hit is not None and now - hit[0] <= _JSON_CACHE_TTL_SECONDS:
            return hit[1]
    value = json.loads(read_json_text(target))
    with _JSON_CACHE_LOCK:
        _JSON_CACHE[key] = (time.monotonic(), value)
        if len(_JSON_CACHE) > _JSON_CACHE_MAX_ENTRIES:
            oldest = min(_JSON_CACHE, key=lambda item: _JSON_CACHE[item][0])
            _JSON_CACHE.pop(oldest, None)
    return value


def _load_formal_events(
    web_root: Path,
    *,
    start: str,
    end: str,
    view: str,
) -> dict[str, Any]:
    formal: dict[str, dict[str, Any]] = {}
    rejected_tombstones: list[dict[str, Any]] = []
    raw_count = 0
    summaries: list[Path] = []
    contributing: set[Path] = set()
    warnings_by_path: dict[Path, list[str]] = {}
    empty_display_summaries = 0
    fallback_summaries = 0
    display_summaries = 0

    for path in glob_json(web_root, "jobs/job-*/files/*/result/*/daily_summary.json"):
        if "decision-variants" in path.parts or "comparison-history" in path.parts:
            continue
        summaries.append(path)
        directory_date = path.parent.name
        try:
            payload = _read_json_cached(path)
        except (OSError, json.JSONDecodeError):
            warnings_by_path[path] = [f"无法读取 {path}"]
            continue
        if not isinstance(payload, Mapping):
            warnings_by_path[path] = [f"{path} 不是 JSON 对象"]
            continue
        raw_warnings = payload.get("warnings", [])
        if isinstance(raw_warnings, list):
            warnings_by_path[path] = [
                str(item) for item in raw_warnings if str(item).strip()
            ]

        if view == "display":
            display_timeline = payload.get("display_timeline")
            if isinstance(display_timeline, list):
                selected = display_timeline
                display_summaries += 1
                if not display_timeline:
                    empty_display_summaries += 1
            else:
                selected = payload.get("timeline", [])
                fallback_summaries += 1
        else:
            selected = payload.get("timeline", [])
        if not isinstance(selected, list):
            continue

        source_start = _summary_source_start(path)
        job_dir = path.parents[4] if len(path.parents) > 4 else None
        job_id = job_dir.name if job_dir is not None else None
        file_id = path.parents[2].name if len(path.parents) > 2 else None
        artifact_info = _summary_event_artifact(path, job_dir)
        artifact_path = artifact_info[0] if artifact_info else None
        corrections = _load_job_corrections(job_dir) if job_dir is not None else {}

        def corrected_event(raw: Mapping[str, Any]):
            event_id = str(raw.get("event_id") or "")
            correction = (
                corrections.get(f"{artifact_path}::{event_id}")
                if artifact_path and event_id
                else None
            )
            if isinstance(correction, Mapping):
                return _apply_correction(raw, correction)
            return dict(raw), False

        selected_ids: set[str] = set()
        for raw in selected:
            if not isinstance(raw, Mapping):
                continue
            selected_ids.add(str(raw.get("event_id") or ""))
            corrected, rejected = corrected_event(raw)
            prepared, repair_info = _prepare_event(corrected, source_start, web_root=web_root)
            prepared = _add_view_fields(
                prepared,
                source_kind="formal",
                artifact=path,
                directory_date=directory_date,
                event_key=_event_key("formal", prepared),
                repair_info=repair_info,
                job_id=job_id,
                file_id=file_id,
            )
            prepared["source_summary_artifact"] = str(path.resolve())
            if artifact_info is not None:
                prepared["source_event_artifact"] = str(artifact_info[1].resolve())
                prepared["artifact_path"] = artifact_info[0]
            else:
                prepared["source_event_artifact"] = None
                prepared["artifact_path"] = None
            if rejected:
                tombstone = _rejected_tombstone(prepared, artifact_path)
                if (
                    tombstone is not None
                    and prepared["effective_date"] is not None
                    and start <= prepared["effective_date"] <= end
                ):
                    rejected_tombstones.append(tombstone)
                continue
            if prepared["effective_date"] is None:
                continue
            if not start <= prepared["effective_date"] <= end:
                continue
            raw_count += 1
            contributing.add(path)
            formal.setdefault(prepared["pipeline_event_key"], prepared)

        # A rejected event may live outside display_timeline.  Keep its
        # tombstone so the matching live twin cannot resurrect it.
        full_timeline = payload.get("timeline")
        if view == "display" and isinstance(full_timeline, list):
            for raw in full_timeline:
                if not isinstance(raw, Mapping):
                    continue
                event_id = str(raw.get("event_id") or "")
                if event_id and event_id in selected_ids:
                    continue
                corrected, rejected = corrected_event(raw)
                if not rejected:
                    continue
                prepared, repair_info = _prepare_event(corrected, source_start, web_root=web_root)
                prepared = _add_view_fields(
                    prepared,
                    source_kind="formal",
                    artifact=path,
                    directory_date=directory_date,
                    event_key=_event_key("formal", prepared),
                    repair_info=repair_info,
                    job_id=job_id,
                    file_id=file_id,
                )
                prepared["source_summary_artifact"] = str(path.resolve())
                prepared["source_event_artifact"] = (
                    str(artifact_info[1].resolve()) if artifact_info else None
                )
                prepared["artifact_path"] = artifact_path
                tombstone = _rejected_tombstone(prepared, artifact_path)
                if (
                    tombstone is not None
                    and prepared["effective_date"] is not None
                    and start <= prepared["effective_date"] <= end
                ):
                    rejected_tombstones.append(tombstone)

    in_range_summaries = set()
    for path in summaries:
        directory_date = path.parent.name
        if start <= directory_date <= end:
            in_range_summaries.add(path)
    source_paths = sorted(contributing | in_range_summaries)
    warnings: list[str] = []
    for path in source_paths:
        warnings.extend(warnings_by_path.get(path, []))
    return {
        "events": formal,
        "rejected_tombstones": rejected_tombstones,
        "raw_count": raw_count,
        "summaries": summaries,
        "source_paths": source_paths,
        "warnings": warnings,
        "empty_display_summaries": empty_display_summaries,
        "fallback_summaries": fallback_summaries,
        "display_summaries": display_summaries,
    }


def _load_manual_events(
    web_root: Path,
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    events: dict[str, dict[str, Any]] = {}
    rejected_tombstones: list[dict[str, Any]] = []
    raw_count = 0
    source_paths: list[Path] = []

    for job_dir in sorted(web_root.glob("jobs/job-*")):
        if not job_dir.is_dir():
            continue
        corrections = _load_job_corrections(job_dir)
        for filename in ("manual-events.json", "review-derived-events.json"):
            path = resolve_readable_json_path(job_dir / filename)
            if not path.is_file():
                continue
            source_paths.append(path)
            try:
                payload = read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, Mapping):
                entries = payload.get("events", [])
            elif isinstance(payload, list):
                entries = payload
            else:
                continue
            if not isinstance(entries, list):
                continue
            for raw in entries:
                if not isinstance(raw, Mapping):
                    continue
                raw_count += 1
                raw_artifact_path = str(raw.get("artifact_path") or "").strip()
                artifact_info = _verified_event_artifact(job_dir, raw_artifact_path)
                artifact_path = artifact_info[0] if artifact_info else None
                event_id = str(raw.get("event_id") or "")
                correction = (
                    corrections.get(f"{artifact_path}::{event_id}")
                    if artifact_path and event_id
                    else None
                )
                if isinstance(correction, Mapping):
                    corrected, rejected = _apply_correction(raw, correction)
                else:
                    corrected, rejected = dict(raw), False
                # restored_date：用户在“需要我判断”里明确恢复的旧记录。
                directory_date = raw.get("restored_date") or _directory_date_from_artifact_path(
                    artifact_path or raw_artifact_path
                )
                prepared, repair_info = _prepare_event(corrected, None, web_root=web_root)
                artifact = artifact_info[1] if artifact_info else path
                prepared = _add_view_fields(
                    prepared,
                    source_kind="manual",
                    artifact=artifact,
                    directory_date=directory_date,
                    event_key=_event_key("manual", prepared),
                    repair_info=repair_info,
                    job_id=job_dir.name,
                )
                prepared["source_summary_artifact"] = None
                prepared["source_event_artifact"] = (
                    str(artifact_info[1].resolve()) if artifact_info else None
                )
                prepared["artifact_path"] = artifact_path
                prepared["provenance"][0]["model_branch"] = "manual"
                if rejected:
                    tombstone = _rejected_tombstone(prepared, artifact_path)
                    if (
                        tombstone is not None
                        and prepared["effective_date"] is not None
                        and start <= prepared["effective_date"] <= end
                    ):
                        rejected_tombstones.append(tombstone)
                    continue
                if prepared["effective_date"] is None:
                    continue
                if not start <= prepared["effective_date"] <= end:
                    continue
                events.setdefault(prepared["pipeline_event_key"], prepared)

    return {
        "events": events,
        "rejected_tombstones": rejected_tombstones,
        "raw_count": raw_count,
        "source_paths": source_paths,
    }


def _load_live_events(
    web_root: Path,
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    live: dict[str, dict[str, Any]] = {}
    raw_count = 0
    visible_raw_count = 0
    filtered_count = 0
    sessions_scanned = 0
    contributing_sessions: set[tuple[str, str]] = set()
    session_sources: dict[tuple[str, str], dict[str, Any]] = {}

    for path in glob_json(web_root, "watch-sessions/*/*/events.json"):
        sessions_scanned += 1
        device = path.parts[-3]
        session = path.parts[-2]
        manifest: Mapping[str, Any] = {}
        manifest_path = path.parent / "manifest.json"
        if manifest_path.is_file():
            try:
                payload = _read_json_cached(manifest_path)
                if isinstance(payload, Mapping):
                    manifest = payload
            except (OSError, json.JSONDecodeError):
                manifest = {}
        job_ids = [
            str(value)
            for value in (
                manifest.get("job_id"),
                manifest.get("completion_job_id"),
            )
            if value
        ]
        source_start = _manifest_source_start(manifest)
        window_start, window_end = _manifest_window(manifest)
        session_sources[(device, session)] = {
            "device": device,
            "session": session,
            "artifact": str(path.resolve()),
            "manifest": str(manifest_path.resolve()),
            "job_ids": job_ids,
            "started_at": window_start.isoformat() if window_start else None,
            "ended_at": window_end.isoformat() if window_end else None,
        }
        try:
            payload = _read_json_cached(path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue
        for raw in payload:
            if not isinstance(raw, Mapping):
                continue
            raw_count += 1
            if not _visible_live_event(raw):
                filtered_count += 1
                continue
            visible_raw_count += 1
            prepared, repair_info = _prepare_event(raw, source_start, web_root=web_root)
            key = _event_key("live", prepared, source=f"{device}/{session}")
            prepared = _add_view_fields(
                prepared,
                source_kind="live",
                artifact=path,
                # Session-aggregator rows have no per-event offset; if their
                # reported/occurrence time is the 2025 sentinel, organize by
                # the real session window instead of inventing 2025-01-01.
                directory_date=(
                    window_start.date().isoformat() if window_start is not None else None
                ),
                event_key=key,
                repair_info=repair_info,
                source_job_ids=job_ids,
                device=device,
                session=session,
            )
            prepared["_session_start_epoch"] = (
                window_start.timestamp() if window_start is not None else None
            )
            prepared["_session_end_epoch"] = (
                window_end.timestamp() if window_end is not None else None
            )
            if prepared["effective_date"] is None:
                continue
            if not start <= prepared["effective_date"] <= end:
                continue
            contributing_sessions.add((device, session))
            live.setdefault(prepared["pipeline_event_key"], prepared)

    live_sources = [
        session_sources[key]
        for key in sorted(contributing_sessions)
        if key in session_sources
    ]
    return {
        "events": live,
        "raw_count": raw_count,
        "visible_raw_count": visible_raw_count,
        "filtered_count": filtered_count,
        "sessions_scanned": sessions_scanned,
        "sources": live_sources,
    }


def _variant_artifact_count(web_root: Path) -> int:
    count = 0
    for path in web_root.glob(
        "jobs/job-*/files/*/result/decision-variants/*/events.json"
    ):
        if path.is_file():
            count += 1
    return count


def build_event_view(
    web_root: Path,
    *,
    date_from: str,
    date_to: str,
    scope: str = "provisional",
    view: str = "display",
    use_cache: bool = False,
) -> dict[str, Any]:
    """Return a canonical, date-bounded read view of formal and live events.

    ``scope="settled"`` returns formal pipeline artifacts only. The name does
    not mean human-confirmed: these are model outputs that passed the formal
    pipeline, not audited financial truth.

    ``scope="provisional"`` returns the formal branch plus live Watch events,
    with exact cross-source duplicates represented as one canonical event and
    provenance aliases. Original event ids are never changed.

    ``view="display"`` honours ``display_timeline`` when present. An empty
    ``display_timeline`` is a legitimate result and is not replaced by the raw
    timeline. ``view="all"`` uses the raw ``timeline`` field.
    """

    start = _validate_date(date_from, "开始日期")
    end = _validate_date(date_to, "结束日期")
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    if scope not in SUPPORTED_SCOPES:
        raise ValueError(f"不支持的 scope: {scope}")
    if view not in SUPPORTED_VIEWS:
        raise ValueError(f"不支持的 view: {view}")

    root = Path(web_root)
    # 跨进程代际：别的进程（worker/ASR/CLI）写完 bump 过 token 就立即失效，
    # 不再等 10s TTL。仅缓存路径需要（无缓存时本来就读最新文件）。
    if use_cache:
        sync_scan_epoch(root)
    # use_cache 只应由「只读的 Web 接口」打开：它会缓存 10s 的扫描结果，
    # 写完立刻回读的场景（测试、批处理）必须保持 False，否则会读到旧快照。
    _scan = _cached_scan if use_cache else (lambda _key, producer: producer())
    # 短 TTL + single-flight：切日期时前端会并发打多个日接口，同一窗口只扫一次。
    generated_result = _scan(
        ("formal", _root_key(root), start, end, view),
        lambda: _load_formal_events(root, start=start, end=end, view=view),
    )
    manual_result = _scan(
        ("manual", _root_key(root), start, end),
        lambda: _load_manual_events(root, start=start, end=end),
    )
    formal_result = dict(generated_result)
    formal_events_map = dict(generated_result["events"])
    formal_events_map.update(manual_result["events"])
    formal_result["events"] = formal_events_map
    formal_result["rejected_tombstones"] = [
        *generated_result.get("rejected_tombstones", []),
        *manual_result.get("rejected_tombstones", []),
    ]
    live_result = _scan(
        ("live", _root_key(root), start, end),
        lambda: _load_live_events(root, start=start, end=end),
    )

    formal_events = sorted(formal_result["events"].values(), key=_sort_key)
    live_events = sorted(live_result["events"].values(), key=_sort_key)
    rejected_index: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    for tombstone in formal_result["rejected_tombstones"]:
        key = tombstone.get("dedup_key")
        if isinstance(key, tuple) and len(key) == 2:
            rejected_index[key].append(tombstone)

    formal_index: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    for event in formal_events:
        key = _dedup_key(event)
        if key is not None:
            formal_index[key].append(event)

    matched_by_formal: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    unmatched_live: list[dict[str, Any]] = []
    live_by_dedup: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    live_by_pipeline = {
        str(event["pipeline_event_key"]): event for event in live_events
    }
    for live_event in live_events:
        key = _dedup_key(live_event)
        if key is not None:
            live_by_dedup[key].append(live_event)
        else:
            unmatched_live.append(live_event)
    rejected_live_suppressed_count = 0
    for key, live_group in live_by_dedup.items():
        tombstones = rejected_index.get(key, [])
        if tombstones:
            kept_live: list[dict[str, Any]] = []
            for live_event in live_group:
                live_jobs = set(live_event.get("source_job_ids") or [])
                suppressed = False
                for tombstone in tombstones:
                    tombstone_jobs = set(tombstone.get("source_job_ids") or [])
                    if live_jobs and tombstone_jobs and not (live_jobs & tombstone_jobs):
                        continue
                    suppressed = True
                    break
                if suppressed:
                    rejected_live_suppressed_count += 1
                else:
                    kept_live.append(live_event)
            live_group = kept_live
        formal_group = formal_index.get(key, [])
        if not live_group:
            continue
        if not formal_group:
            unmatched_live.extend(live_group)
            continue
        assignments, unmatched_keys = _assign_one_to_one(formal_group, live_group)
        for formal_key, pairs in assignments.items():
            matched_by_formal[formal_key].extend(pairs)
        unmatched_live.extend(
            live_by_pipeline[pipeline_key]
            for pipeline_key in unmatched_keys
            if pipeline_key in live_by_pipeline
        )
    cross_source_duplicate_count = sum(
        len(pairs) for pairs in matched_by_formal.values()
    )

    provisional: list[dict[str, Any]] = []
    for formal_event in formal_events:
        matches = matched_by_formal.get(
            str(formal_event["pipeline_event_key"]), []
        )
        if not matches:
            provisional.append(copy.deepcopy(formal_event))
            continue
        canonical = copy.deepcopy(formal_event)
        for live_event, basis in matches:
            alias = _alias_payload(live_event, basis)
            canonical["aliases"].append(alias)
            canonical["provenance"].append(
                {
                    "source_kind": "live",
                    "event_id": live_event.get("event_id"),
                    "event_key": live_event.get("event_key"),
                    "artifact": live_event.get("source_artifact"),
                    "device": live_event.get("source_device"),
                    "session": live_event.get("source_session"),
                    "job_ids": list(live_event.get("source_job_ids") or []),
                    "match_basis": basis,
                    "review_status": live_event.get("review_status"),
                }
            )
            canonical["conflicts"].extend(
                _enrich_missing_non_financial(canonical, live_event)
            )
        canonical["review_provenance"] = [
            {
                "source_kind": "formal",
                "event_id": formal_event.get("event_id"),
                "review_status": formal_event.get("review_status"),
            }
        ] + [
            {
                "source_kind": "live",
                "event_id": live_event.get("event_id"),
                "review_status": live_event.get("review_status"),
            }
            for live_event, _basis in matches
        ]
        provisional.append(canonical)
    provisional.extend(copy.deepcopy(event) for event in unmatched_live)
    provisional.sort(key=_sort_key)

    selected = formal_events if scope == "settled" else provisional
    selected = [copy.deepcopy(event) for event in selected]
    for event in selected:
        event.pop("_session_start_epoch", None)
        event.pop("_session_end_epoch", None)

    # [2026-10-22] 被 `merge_duplicate_expense_events` 合并掉的分段事件带
    # `suppressed=True` + `merged_into=<id>`：文件里保留（零数据丢失），
    # 读层不展示，避免同一笔消费在页面上重复成多张卡。
    suppressed_count = sum(1 for event in selected if event.get("suppressed"))
    if suppressed_count:
        selected = [event for event in selected if not event.get("suppressed")]

    dedup_metadata = {
        "cross_source_group_count": len(matched_by_formal),
        "cross_source_duplicate_count": cross_source_duplicate_count,
        "unmatched_live_count": len(unmatched_live),
        "formal_event_count": len(formal_events),
        "live_event_count": len(live_events),
        "raw_formal_event_count": formal_result["raw_count"],
        "raw_live_event_count": live_result["raw_count"],
        "visible_live_event_count": live_result["visible_raw_count"],
        "filtered_live_event_count": live_result["filtered_count"],
        "rejected_tombstone_count": len(formal_result["rejected_tombstones"]),
        "rejected_live_suppressed_count": rejected_live_suppressed_count,
        "merged_duplicate_suppressed_count": suppressed_count,
        "manual_event_count": len(manual_result["events"]),
        "raw_manual_event_count": manual_result["raw_count"],
    }

    view_info = {
        "requested": view,
        "display_timeline_summary_count": formal_result["display_summaries"],
        "empty_display_summary_count": formal_result["empty_display_summaries"],
        "timeline_fallback_summary_count": formal_result["fallback_summaries"],
        "empty_display_timeline_is_authoritative": True,
    }

    provisional_source_counts = Counter(
        "formal" if str(event.get("canonical_source_kind")) == "formal" else "live"
        for event in provisional
    )
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "date_from": start,
        "date_to": end,
        "timezone": "Asia/Shanghai",
        "scope": scope,
        "view": view,
        "formal_summary_count": len(formal_result["summaries"]),
        "formal_summary_in_range_count": len(formal_result["source_paths"]),
        "live_session_count": live_result["sessions_scanned"],
        # Top-level alias used by API consumers that do not know whether the
        # request selected settled/provisional/all; always mirrors the actual
        # selected events, never a synthetic occurrence count.
        "date_basis_counts": _date_basis_counts(selected),
        "counts": {
            "all": {
                "event_count": len(selected),
                "date_basis_counts": _date_basis_counts(selected),
                "source_kind_counts": _source_kind_counts(selected),
            },
            "settled": {
                "event_count": len(formal_events),
                "raw_event_count": formal_result["raw_count"],
                "date_basis_counts": _date_basis_counts(formal_events),
                "source_kind_counts": {"formal": len(formal_events)},
            },
            "provisional": {
                "event_count": len(provisional),
                "raw_event_count": formal_result["raw_count"]
                + live_result["visible_raw_count"],
                "date_basis_counts": _date_basis_counts(provisional),
                "source_kind_counts": dict(provisional_source_counts),
                "provenance_source_kind_counts": {
                    "formal": len(formal_events),
                    "live": len(live_events),
                },
            },
            "raw": {
                "formal_event_count": formal_result["raw_count"],
                "live_event_count": live_result["raw_count"],
                "live_event_count_before_candidate_filter": live_result["raw_count"],
            },
        },
        "selected_counts": {
            "event_count": len(selected),
            "date_basis_counts": _date_basis_counts(selected),
            "source_kind_counts": _source_kind_counts(selected),
        },
        "dedup": dedup_metadata,
        "view_info": view_info,
        "model_branches": {
            "root_only": True,
            "excluded_variant_artifacts": _variant_artifact_count(root),
            "note": "Jev and Clef variant directories are never merged or counted here.",
        },
        "sources": {
            "formal_summaries": [
                str(path.resolve()) for path in formal_result["source_paths"]
            ],
            "manual_event_files": [
                str(path.resolve()) for path in manual_result["source_paths"]
            ],
            "live_sessions": live_result["sources"],
        },
        "warnings": list(dict.fromkeys(formal_result["warnings"])),
        "scope_semantics": {
            "settled": "formal pipeline artifacts only; not human-confirmed financial truth",
            "provisional": "formal branch plus live Watch events; exact duplicates carry provenance aliases",
        },
    }

    from life_logger.exact_projection_identity import build_projection_identity_audit

    identity_audit = build_projection_identity_audit(selected)
    metadata["projection_identity_audit"] = identity_audit

    return {
        "schema_version": SCHEMA_VERSION,
        "date_from": start,
        "date_to": end,
        "timezone": "Asia/Shanghai",
        "scope": scope,
        "view": view,
        "events": selected,
        "metadata": metadata,
        "projection_identity_audit": identity_audit,
    }


__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_SCOPES",
    "SUPPORTED_VIEWS",
    "build_event_view",
    "repair_event_time",
]
