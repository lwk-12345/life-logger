"""Multi-day pattern *observations* derived from durable daily outputs.

These are evidence-backed observational summaries only — not diagnoses,
advice, or predictions. Every observation carries the source events that
justify it so a human can audit the claim.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from life_logger.statistics import (
    _event_date,
    _event_datetime,
    _event_key,
    _expense_amount,
    _repair_event_time,
    _summary_paths,
    _summary_source_start,
    _validate_date,
)

MIN_PATTERN_SAMPLE_DAYS = 3


def _parse_hour_minute(event: Mapping[str, Any]) -> tuple[int, int] | None:
    parsed = _event_datetime(event)
    if parsed is None:
        return None
    return parsed.hour, parsed.minute


def _minutes_since_midnight(event: Mapping[str, Any]) -> int | None:
    parts = _parse_hour_minute(event)
    if parts is None:
        return None
    hour, minute = parts
    return hour * 60 + minute


def _format_hhmm(total_minutes: int) -> str:
    total_minutes = int(total_minutes) % (24 * 60)
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _is_wake_event(event: Mapping[str, Any]) -> bool:
    if "routine" not in [str(item) for item in event.get("categories", [])]:
        return False
    activity = str(event.get("activity_type") or "").lower()
    if activity in {"waking_up", "wake_up", "wake"}:
        return True
    title = str(event.get("title") or "")
    quote = ""
    evidence = event.get("evidence")
    if isinstance(evidence, dict):
        quote = str(evidence.get("quote") or "")
    blob = title + quote
    return "起床" in blob


def _food_bucket(minutes: int) -> str:
    if minutes < 10 * 60:
        return "morning"
    if minutes < 15 * 60:
        return "midday"
    if minutes < 20 * 60:
        return "evening"
    return "late"


def _evidence_item(event: Mapping[str, Any], day: str) -> dict[str, Any]:
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    time_info = event.get("time") if isinstance(event.get("time"), dict) else {}
    return {
        "event_id": str(event.get("event_id") or ""),
        "date": _event_date(event, day),
        "title": str(event.get("title") or ""),
        "absolute": time_info.get("absolute"),
        "quote": evidence.get("quote"),
        "categories": [str(item) for item in event.get("categories", [])],
    }


def _load_events(
    job_root: Path,
    *,
    start: str,
    end: str,
) -> tuple[dict[str, dict[str, Any]], list[str], set[str]]:
    events: dict[str, dict[str, Any]] = {}
    source_summaries: list[str] = []
    days_with_data: set[str] = set()
    for path in _summary_paths(Path(job_root)):
        day = path.parent.name
        if day < start or day > end:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        timeline = payload.get("timeline")
        if not isinstance(timeline, list):
            continue
        source_summaries.append(str(path.resolve()))
        days_with_data.add(day)
        source_start = _summary_source_start(path)
        for raw in timeline:
            if not isinstance(raw, dict):
                continue
            event = _repair_event_time(raw, source_start)
            if not start <= _event_date(event, day) <= end:
                continue
            events.setdefault(_event_key(event), event)
    return events, source_summaries, days_with_data


def _wake_observation(events: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    wake_events = [event for event in events if _is_wake_event(event)]
    timed = []
    for event in wake_events:
        minutes = _minutes_since_midnight(event)
        if minutes is None:
            continue
        timed.append((minutes, event))
    if not timed:
        return None
    minutes_list = [item[0] for item in timed]
    evidence = [
        _evidence_item(event, _event_date(event, ""))
        for _, event in sorted(timed, key=lambda item: item[0])
    ]
    if len(timed) < 5:
        # 费曼反自欺原则：样本少于 5 次禁止计算中位数伪装规律
        times_str = "、".join(_format_hhmm(m) for m in minutes_list)
        return {
            "kind": "wake_time_range",
            "label": "起床时间记录（观察 · 样本积累中）",
            "statement": (
                f"当前仅记录到 {len(timed)} 次起床时间（{times_str}）。"
                "这是统计观察，样本数量不足 5 次，暂不计算统计中位数与作息诊断。"
            ),
            "metrics": {
                "sample_count": len(timed),
                "earliest": _format_hhmm(min(minutes_list)),
                "latest": _format_hhmm(max(minutes_list)),
                "insufficient_samples": True,
            },
            "evidence": evidence,
        }
    return {
        "kind": "wake_time_range",
        "label": "起床时间范围（观察）",
        "statement": (
            f"在有记录的 {len(timed)} 次起床事件中，"
            f"最早 {_format_hhmm(min(minutes_list))}，"
            f"最晚 {_format_hhmm(max(minutes_list))}，"
            f"中位数 {_format_hhmm(int(median(minutes_list)))}。"
            "这是对已记录事件的统计观察，不是作息诊断。"
        ),
        "metrics": {
            "sample_count": len(timed),
            "earliest": _format_hhmm(min(minutes_list)),
            "latest": _format_hhmm(max(minutes_list)),
            "median": _format_hhmm(int(median(minutes_list))),
        },
        "evidence": evidence,
    }


def _exercise_observation(
    events: Sequence[Mapping[str, Any]],
    days_with_data: set[str],
) -> dict[str, Any] | None:
    exercise_events = [
        event
        for event in events
        if "exercise" in [str(item) for item in event.get("categories", [])]
    ]
    if not exercise_events and not days_with_data:
        return None
    days = sorted(
        {
            _event_date(event, "")
            for event in exercise_events
            if _event_date(event, "")
        }
    )
    day_count = len(days_with_data) or len(days)
    evidence = [
        _evidence_item(event, _event_date(event, "")) for event in exercise_events
    ]
    return {
        "kind": "exercise_frequency",
        "label": "运动频率（观察）",
        "statement": (
            f"在有数据的 {day_count} 天中，有 {len(days)} 天出现运动类事件，"
            f"共 {len(exercise_events)} 条。"
            "这是对已记录事件的统计观察，不是运动建议。"
        ),
        "metrics": {
            "days_with_data": day_count,
            "days_with_exercise": len(days),
            "exercise_event_count": len(exercise_events),
            "exercise_dates": days,
        },
        "evidence": evidence,
    }


def _food_observation(events: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    food_events = [
        event
        for event in events
        if "food" in [str(item) for item in event.get("categories", [])]
    ]
    buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in food_events:
        minutes = _minutes_since_midnight(event)
        if minutes is None:
            continue
        buckets[_food_bucket(minutes)].append(event)
    if not food_events:
        return None
    bucket_counts = {key: len(value) for key, value in sorted(buckets.items())}
    evidence = [
        _evidence_item(event, _event_date(event, "")) for event in food_events
    ]
    dominant = max(bucket_counts, key=bucket_counts.get) if bucket_counts else None
    return {
        "kind": "food_time_windows",
        "label": "饮食时段（观察）",
        "statement": (
            f"共 {len(food_events)} 条饮食相关事件；"
            + (
                f"其中落在 {dominant} 时段的最多（{bucket_counts[dominant]} 条）。"
                if dominant
                else "多数事件缺少绝对时间，未能划分时段。"
            )
            + "这是对已记录事件的统计观察，不是饮食诊断。"
        ),
        "metrics": {
            "food_event_count": len(food_events),
            "bucket_counts": bucket_counts,
            "dominant_bucket": dominant,
        },
        "evidence": evidence,
    }


def _expense_observation(events: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    expense_events = []
    amounts: list[float] = []
    for event in events:
        if "expense" not in [str(item) for item in event.get("categories", [])]:
            continue
        amount = _expense_amount(event)
        expense_events.append(event)
        if amount > 0:
            amounts.append(amount)
    if not expense_events:
        return None
    total = round(sum(amounts), 2)
    evidence = [
        _evidence_item(event, _event_date(event, "")) for event in expense_events
    ]
    metrics: dict[str, Any] = {
        "expense_event_count": len(expense_events),
        "amount_sample_count": len(amounts),
        "total_cny": total,
    }
    if amounts:
        metrics["median_cny"] = round(float(median(amounts)), 2)
        metrics["min_cny"] = round(min(amounts), 2)
        metrics["max_cny"] = round(max(amounts), 2)
    return {
        "kind": "expense_pattern",
        "label": "消费模式（观察）",
        "statement": (
            f"共 {len(expense_events)} 条消费事件"
            + (f"，金额样本 {len(amounts)} 条，合计约 {total} 元" if amounts else "")
            + "。"
            "这是对已记录事件的统计观察，不是消费建议。"
        ),
        "metrics": metrics,
        "evidence": evidence,
    }


def _hhmm_to_minutes(value: str) -> int | None:
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (TypeError, ValueError):
        return None
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        return None
    return hour * 60 + minute


def _alert_candidates(
    observations: Sequence[Mapping[str, Any]],
    *,
    sample_ready: bool,
) -> list[dict[str, Any]]:
    """Turn strong pattern observations into non-diagnostic attention candidates."""

    if not sample_ready:
        return []
    candidates: list[dict[str, Any]] = []
    for observation in observations:
        kind = str(observation.get("kind") or "")
        metrics = observation.get("metrics")
        if not isinstance(metrics, Mapping):
            continue
        evidence = list(observation.get("evidence") or [])
        if kind == "exercise_frequency":
            days_with_data = int(metrics.get("days_with_data") or 0)
            days_with_exercise = int(metrics.get("days_with_exercise") or 0)
            if days_with_data >= MIN_PATTERN_SAMPLE_DAYS and days_with_exercise == 0:
                candidates.append(
                    {
                        "kind": "exercise_record_gap",
                        "label": "运动记录空缺候选（观察）",
                        "statement": (
                            f"在有数据的 {days_with_data} 天中，没有记录到运动类事件。"
                            "这只是记录层面的提醒候选，不代表健康判断。"
                        ),
                        "severity": "info",
                        "source_observation_kind": kind,
                        "evidence": evidence,
                    }
                )
        elif kind == "wake_time_range":
            earliest = _hhmm_to_minutes(str(metrics.get("earliest") or ""))
            latest = _hhmm_to_minutes(str(metrics.get("latest") or ""))
            sample_count = int(metrics.get("sample_count") or 0)
            if (
                earliest is not None
                and latest is not None
                and sample_count >= MIN_PATTERN_SAMPLE_DAYS
                and latest - earliest >= 180
            ):
                candidates.append(
                    {
                        "kind": "wake_time_spread",
                        "label": "起床时间跨度候选（观察）",
                        "statement": (
                            f"已记录的 {sample_count} 次起床时间跨度达到 "
                            f"{latest - earliest} 分钟。"
                            "这是时间记录波动提示，不是作息诊断。"
                        ),
                        "severity": "info",
                        "source_observation_kind": kind,
                        "evidence": evidence,
                    }
                )
        elif kind == "food_time_windows":
            bucket_counts = metrics.get("bucket_counts")
            late_count = (
                int(bucket_counts.get("late") or 0)
                if isinstance(bucket_counts, Mapping)
                else 0
            )
            if late_count >= MIN_PATTERN_SAMPLE_DAYS:
                candidates.append(
                    {
                        "kind": "late_food_records",
                        "label": "较晚饮食记录候选（观察）",
                        "statement": (
                            f"当前范围内有 {late_count} 条 20:00 后的饮食记录。"
                            "这只是记录时段提示，不是饮食诊断或建议。"
                        ),
                        "severity": "info",
                        "source_observation_kind": kind,
                        "evidence": evidence,
                    }
                )
        elif kind == "expense_pattern":
            amount_count = int(metrics.get("amount_sample_count") or 0)
            maximum = float(metrics.get("max_cny") or 0.0)
            median_value = float(metrics.get("median_cny") or 0.0)
            if amount_count >= MIN_PATTERN_SAMPLE_DAYS and median_value > 0:
                if maximum >= median_value * 2:
                    candidates.append(
                        {
                            "kind": "expense_outlier_candidate",
                            "label": "较高单笔消费候选（观察）",
                            "statement": (
                                f"最高单笔记录 ¥{maximum:.2f}，约为本范围金额中位数 "
                                f"¥{median_value:.2f} 的 "
                                f"{maximum / median_value:.1f} 倍。"
                                "这只是金额分布观察，不是消费建议。"
                            ),
                            "severity": "info",
                            "source_observation_kind": kind,
                            "evidence": evidence,
                        }
                    )
    return candidates


def build_pattern_observations(
    job_root: Path,
    *,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    """Build evidence-backed multi-day pattern observations for a date range."""

    start = _validate_date(date_from, "开始日期")
    end = _validate_date(date_to, "结束日期")
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")

    events_map, source_summaries, days_with_data = _load_events(
        Path(job_root), start=start, end=end
    )
    events = list(events_map.values())
    observations: list[dict[str, Any]] = []
    for builder in (
        lambda: _wake_observation(events),
        lambda: _exercise_observation(events, days_with_data),
        lambda: _food_observation(events),
        lambda: _expense_observation(events),
    ):
        item = builder()
        if item is not None:
            observations.append(item)

    sample_ready = len(days_with_data) >= MIN_PATTERN_SAMPLE_DAYS
    alert_candidates = _alert_candidates(observations, sample_ready=sample_ready)
    return {
        "schema_version": "pattern-observations-v1",
        "disclaimer": (
            "下列内容仅为对已记录生活事件的可回溯观察，"
            "不是健康/消费/作息诊断，也不构成建议。"
        ),
        "date_from": start,
        "date_to": end,
        "days_with_data": len(days_with_data),
        "minimum_sample_days": MIN_PATTERN_SAMPLE_DAYS,
        "sample_ready": sample_ready,
        "sample_hint": (
            "样本天数已达到规律观察的最低展示建议。"
            if len(days_with_data) >= MIN_PATTERN_SAMPLE_DAYS
            else f"建议至少积累 {MIN_PATTERN_SAMPLE_DAYS} 个有数据日期后再解读规律；"
            f"当前只有 {len(days_with_data)} 天。"
        ),
        "event_count": len(events),
        "observations": observations,
        "alert_candidates": alert_candidates,
        "source_daily_summaries": source_summaries,
    }
