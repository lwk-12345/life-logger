"""Daily brief aggregation and outbound delivery hooks."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from life_logger.jsonio import glob_json, read_json
from life_logger.timeutil import LOCAL_TZ

BRIEF_CATEGORIES = (
    "routine",
    "food",
    "expense",
    "exercise",
    "work_communication",
    "idea",
    "todo",
)

AGENT_BRIEF_SCHEMA = "agent-brief-v1"
AGENT_MIN_CONFIDENCE = 0.90
DEFAULT_OUTBOX_RELATIVE = Path("outbox") / "daily-brief"


def _load_json(path: Path) -> Any:
    # [2026-11-05] 中间 JSON 可能是 `x.json.gz`：未压缩优先，缺了才解压读。
    return read_json(path)


def _daily_summary_paths(job_root: Path, date: str) -> list[Path]:
    root = Path(job_root)
    paths = [
        path
        for path in glob_json(root, "jobs/job-*/files/*/result/*/daily_summary.json")
        if path.parent.name == date
    ]
    return sorted(paths)


def _task_states(job_root: Path) -> list[dict[str, Any]]:
    path = Path(job_root) / "task-states.json"
    if not path.is_file():
        return []
    try:
        payload = _load_json(path)
    except (OSError, json.JSONDecodeError):
        return []
    tasks = payload.get("tasks", {}) if isinstance(payload, dict) else {}
    if not isinstance(tasks, dict):
        return []
    return [item for item in tasks.values() if isinstance(item, dict)]


def _event_key(event: Mapping[str, Any]) -> str:
    event_id = str(event.get("event_id") or "").strip()
    if event_id:
        return event_id
    time = event.get("time") if isinstance(event.get("time"), dict) else {}
    return "|".join(
        (
            str(event.get("title") or ""),
            str(time.get("absolute") or time.get("relative_seconds") or ""),
            ",".join(str(item) for item in event.get("categories", [])),
        )
    )


def _category_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for category in BRIEF_CATEGORIES:
        matches = [
            event
            for event in events
            if category in [str(item) for item in event.get("categories", [])]
        ]
        result[category] = {
            "count": len(matches),
            "highlights": [
                str(event.get("title") or "").strip()
                for event in matches[:5]
                if str(event.get("title") or "").strip()
            ],
        }
    return result


def _todo_items(
    events: Sequence[Mapping[str, Any]],
    task_states: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    state_by_event = {
        str(item.get("event_id")): item
        for item in task_states
        if item.get("event_id")
    }
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        categories = [str(item) for item in event.get("categories", [])]
        if "todo" not in categories:
            continue
        event_id = str(event.get("event_id") or "")
        state = state_by_event.get(event_id, {})
        status = str(state.get("status") or "pending")
        if status in {"completed", "dismissed"}:
            continue
        key = event_id or _event_key(event)
        seen.add(key)
        items.append(
            {
                "task_key": str(state.get("task_key") or key),
                "event_id": event_id or None,
                "title": str(
                    state.get("title") or event.get("title") or "未命名待办"
                ),
                "status": status,
                "source": "event",
            }
        )
    for state in task_states:
        status = str(state.get("status") or "pending")
        if status in {"completed", "dismissed"}:
            continue
        event_id = str(state.get("event_id") or "")
        key = event_id or str(state.get("task_key") or "")
        if not key or key in seen:
            continue
        title = str(state.get("title") or "").strip()
        if not title:
            continue
        seen.add(key)
        items.append(
            {
                "task_key": str(state.get("task_key") or key),
                "event_id": event_id or None,
                "title": title,
                "status": status,
                "source": "task_state",
            }
        )
    return items


def _has_activity_products(job_root: Path, date: str) -> bool:
    """当天有没有活动层产物（`activity-timeline/jev/<date>.json`）。

    [2026-11-07 唯一事实来源] 只有真有活动产物时才切到按天账本，
    临时目录/纯内存的旧用法行为完全不变。
    """
    from life_logger.day_facts import data_root

    try:
        return (data_root(Path(job_root)) / "activity-timeline" / "jev" / f"{date}.json").is_file()
    except OSError:
        return False


def brief_expense_ledger(events: Sequence[Mapping[str, Any]], date: str, job_root: Path | str) -> dict[str, Any]:
    """daily-brief 用的消费账本：与 `/api/daily` 的 `expense_ledger` 同源。

    有当天活动产物 → `day_expense_ledger(events, date, root=job_root)`（含活动层消费）；
    否则 → 原来的 `event_only_expense_ledger(events)`（向后兼容，不读磁盘活动文件）。
    """
    from life_logger.day_facts import day_expense_ledger, event_only_expense_ledger

    if date and _has_activity_products(Path(job_root), date):
        return day_expense_ledger(list(events), date, root=job_root)
    return event_only_expense_ledger(list(events))


def build_daily_brief(
    job_root: Path,
    *,
    date: str,
    task_states: Sequence[Mapping[str, Any]] | None = None,
    scope: str = "provisional",
    view: str = "display",
    use_scan_cache: bool = False,
) -> dict[str, Any]:
    """Aggregate generated daily artifacts into one external-agent friendly brief.

    【2026-10-17】补齐读契约 ``scope`` / ``view``。
    为什么：``local_web.py`` 已经在所有读接口上传了这两个参数（`/api/daily`、
    `/api/daily-brief`、`/api/stats`），``event_view.build_event_view`` 也早已实现
    「settled=只认正式产物 / provisional=正式 + Watch 实时」与
    「display=优先 display_timeline / all=原始 timeline」。
    唯独本函数签名没跟上，导致调用直接 ``TypeError`` —— 接口层只能静默降级，
    结果是「切了 scope 但数字没变」这种更隐蔽的错误。

    现在：参数照收、值照校验（非法 scope/view 必须显式报错，不能假装接受），
    并且事件表统一走 ``event_view``（全仓唯一权威读层），这样
    `/api/daily`、`/api/daily-brief`、`/api/stats` 三处口径必然一致。
    """

    try:
        datetime.fromisoformat(date)
    except ValueError as error:
        raise ValueError("date must use YYYY-MM-DD") from error

    scope, view = _validate_read_contract(scope, view)

    from life_logger.activity_cards import build_activity_cards
    from life_logger.activity_links import build_activity_links
    from life_logger.activity_presentation import (
        PURCHASE_SCHEMA,
        build_activity_presentation,
        build_purchase_session_summaries,
    )
    from life_logger.event_view import build_event_view

    payload = build_event_view(
        Path(job_root), date_from=date, date_to=date, scope=scope, view=view,
        use_cache=use_scan_cache,
    )
    events = [
        dict(event)
        for event in payload.get("events", [])
        if isinstance(event, Mapping)
    ]
    metadata = payload.get("metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    sources = metadata.get("sources", {})
    sources = sources if isinstance(sources, Mapping) else {}
    source_paths = [
        str(path) for path in sources.get("formal_summaries", []) if path
    ]
    manual_source_paths = [
        str(path) for path in sources.get("manual_event_files", []) if path
    ]
    live_sessions = sources.get("live_sessions", [])
    if not events and not source_paths and not manual_source_paths and not live_sessions:
        raise ValueError(f"没有找到 {date} 的 daily_summary.json")

    timezone_name = LOCAL_TZ.key
    tasks = list(task_states) if task_states is not None else _task_states(job_root)
    # Sidecar activity facts. This is a pure read projection: events remain the
    # canonical ordered list and the ledger is reference-only.
    activity_cards = build_activity_cards(
        events,
        ledger=brief_expense_ledger(events, date, job_root),
    ).get("cards", [])
    # Sidecar 显式关系边：只读引用字段，不改 events / 金额 / 账本。
    activity_links = build_activity_links(events)
    # 纯展示投影：只读 cards/links，events / cards / links / ledger 原样返回。
    activity_presentation = build_activity_presentation(activity_cards, activity_links)
    # 跨商品购买摘要（只读投影）：显式追加关系才并同一会话；交易逐笔保留、不求和、不判支付。
    purchase_projection = build_purchase_session_summaries(events)
    return {
        "schema_version": "daily-brief-v1",
        "date": date,
        "timezone": timezone_name,
        "generated_at": datetime.now(tz=LOCAL_TZ).isoformat(),
        "event_count": len(events),
        "category_summary": _category_summary(events),
        "todos": _todo_items(events, tasks),
        "warnings": list(dict.fromkeys(metadata.get("warnings", []))),
        "events": events,
        "activity_cards": activity_cards,
        "activity_links": activity_links,
        "activity_presentation": activity_presentation,
        "purchase_sessions": purchase_projection.get("sessions", []),
        "purchase_session_contract": {
            "schema": PURCHASE_SCHEMA,
            "link_evidence": purchase_projection.get("contract", {}).get("link_evidence"),
            "only_explicit_addon": True,
            "transactions": "逐笔引用，不求和、不去重",
            "state": "unknown 表示不判断是否已付款",
            "pending_candidates": "缺 canonical 商品时只报候选，不生成记录",
        },
        "purchase_session_pending": purchase_projection.get("pending_candidates", []),
        "purchase_session_pending_broadcast": purchase_projection.get("pending_broadcast_addons", []),
        "source_daily_summaries": source_paths,
        "source_manual_event_files": manual_source_paths,
        "accounting_scope": scope,
        "scope": scope,
        "view": view,
        "counts": metadata.get("counts", {}),
        "selected_counts": metadata.get("selected_counts", {}),
        "event_view": metadata,
    }


def _validate_read_contract(scope: str, view: str) -> tuple[str, str]:
    """校验读契约。非法值必须显式报错 —— 静默接受会让调用方以为筛选生效了。"""
    from life_logger.event_view import SUPPORTED_SCOPES, SUPPORTED_VIEWS

    scope_value = str(scope or "provisional")
    view_value = str(view or "display")
    if scope_value not in SUPPORTED_SCOPES:
        raise ValueError(f"不支持的 scope: {scope_value}")
    if view_value not in SUPPORTED_VIEWS:
        raise ValueError(f"不支持的 view: {view_value}")
    return scope_value, view_value


def default_outbox_dir(job_root: Path) -> Path:
    """Return the single canonical local outbox for external Agent consumption."""

    return Path(job_root).expanduser().resolve() / DEFAULT_OUTBOX_RELATIVE


def _safe_point(event: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        confidence = float(event.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < AGENT_MIN_CONFIDENCE:
        return None

    raw_time = event.get("time") if isinstance(event.get("time"), dict) else {}
    time_payload = {
        key: raw_time.get(key)
        for key in ("absolute", "relative_seconds", "precision")
        if raw_time.get(key) is not None
    }
    categories = [
        str(item)
        for item in event.get("categories", [])
        if str(item) in BRIEF_CATEGORIES
    ]
    raw_data = event.get("data") if isinstance(event.get("data"), dict) else {}
    facts = {
        category: raw_data.get(category)
        for category in categories
        if category in raw_data and isinstance(raw_data.get(category), dict)
    }
    return {
        "event_id": str(event.get("event_id") or "") or None,
        "categories": categories,
        "activity_type": str(event.get("activity_type") or "") or None,
        "title": str(event.get("title") or "").strip(),
        "time": time_payload,
        "facts": facts,
        "confidence": confidence,
    }


def _searchable_summary(
    points: Sequence[Mapping[str, Any]],
    todos: Sequence[Mapping[str, Any]],
    warnings: Sequence[str],
) -> str:
    values: list[str] = []
    for item in points:
        title = str(item.get("title") or "").strip()
        if title:
            values.append(title)
    for item in todos:
        title = str(item.get("title") or "").strip()
        if title:
            values.append(title)
    values.extend(str(item).strip() for item in warnings if str(item).strip())
    return "；".join(dict.fromkeys(values))[:4000]


def external_agent_brief(brief: Mapping[str, Any]) -> dict[str, Any]:
    """Project an internal brief onto the only supported external payload contract."""

    if brief.get("schema_version") == AGENT_BRIEF_SCHEMA:
        return dict(brief)

    points = [
        point
        for raw in brief.get("events", [])
        if isinstance(raw, dict) and (point := _safe_point(raw)) is not None
    ]
    point_ids = {
        str(item.get("event_id"))
        for item in points
        if item.get("event_id")
    }
    todos: list[dict[str, Any]] = []
    for raw in brief.get("todos", []):
        if not isinstance(raw, dict):
            continue
        event_id = str(raw.get("event_id") or "")
        if event_id and event_id not in point_ids:
            continue
        todos.append(
            {
                "task_key": str(raw.get("task_key") or "") or None,
                "event_id": event_id or None,
                "title": str(raw.get("title") or "").strip(),
                "status": str(raw.get("status") or "pending"),
            }
        )
    warnings = [
        str(item)
        for item in brief.get("warnings", [])
        if str(item).strip()
    ]
    return {
        "schema_version": AGENT_BRIEF_SCHEMA,
        "date": str(brief.get("date") or ""),
        "timezone": LOCAL_TZ.key,
        "generated_at": brief.get("generated_at"),
        "event_count": len(points),
        "category_summary": _category_summary(points),
        "todos": todos,
        "warnings": list(dict.fromkeys(warnings)),
        "searchable_summary": _searchable_summary(points, todos, warnings),
        "high_confidence_points": points,
    }


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp, path)
    path.chmod(0o600)


def deliver_daily_brief(
    brief: Mapping[str, Any],
    *,
    outbox_dir: Path | None = None,
    webhook_url: str | None = None,
    opener=urlopen,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Write the allowlisted Agent brief to outbox and optionally POST it."""

    outbound = external_agent_brief(brief)
    result: dict[str, Any] = {
        "outbox_path": None,
        "webhook": None,
    }
    if outbox_dir is not None:
        date = str(outbound.get("date") or "undated")
        target = Path(outbox_dir).expanduser().resolve() / (
            f"life-logger-daily-brief-{date}.json"
        )
        _atomic_json(target, outbound)
        result["outbox_path"] = str(target)

    url = str(webhook_url or "").strip()
    if url:
        request = Request(
            url,
            data=json.dumps(outbound, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "LifeLogger/0.1",
            },
            method="POST",
        )
        try:
            with opener(request, timeout=timeout) as response:
                status = int(getattr(response, "status", response.getcode()))
                result["webhook"] = {
                    "ok": 200 <= status < 300,
                    "status": status,
                    "error": None,
                }
        except HTTPError as error:
            result["webhook"] = {
                "ok": False,
                "status": int(error.code),
                "error": str(error),
            }
        except (OSError, URLError) as error:
            result["webhook"] = {
                "ok": False,
                "status": None,
                "error": str(error),
            }
    return result


def outbound_options(config: Mapping[str, Any]) -> tuple[Path | None, str | None]:
    raw = config.get("daily_brief", {})
    settings = raw if isinstance(raw, dict) else {}
    outbox = str(settings.get("outbox_dir") or "").strip()
    webhook = str(settings.get("webhook_url") or "").strip()
    return (Path(outbox).expanduser() if outbox else None, webhook or None)
