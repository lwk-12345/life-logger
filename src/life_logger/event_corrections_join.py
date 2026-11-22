"""把 job 级 `event-corrections.json` 接到读路径（**读时应用，不复制第二份真相**）。

真相源：`jobs/job-*/event-corrections.json`（Web 纠正 UI 写入，key = `<artifact_path>::<event_id>`）。
本模块只做“读 + 应用”，不写盘；活动与事件用**坐标**（source_id/segment_id/row_id）对齐，
不按标题/金额相似度合并（避免误配）。

三种结果显式区分（绝不把“读失败”当成“无纠正”）：
  * ok            —— 读到 N 份纠正，应用后返回
  * none          —— 没有纠正文件（正常）
  * unknown       —— 有文件但读/解析失败（**不改动任何字段**，并在返回体标 unknown）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

CORRECTIONS_GLOB = "jobs/job-*/event-corrections.json"
STATUS_FIELD = "corrections_status"
APPLIED_FIELD = "corrections_applied"


def _coord_keys(evidence: Mapping[str, Any]) -> list[tuple[str, int]]:
    keys: list[tuple[str, int]] = []
    sid = evidence.get("source_id")
    row = evidence.get("row_id")
    if isinstance(sid, str) and sid and isinstance(row, int):
        keys.append((sid, row))
    return keys


def load_event_corrections(job_root: Path, date: str | None = None) -> dict[str, Any]:
    """读所有 job 的 event-corrections（可选按日期过滤 artifact_path）。

    返回 {status, entries: {event_id: {...}}, by_coord: {(source_id,row_id): entry}}。
    多份纠正对同一 event_id：按文件 mtime 升序应用 ⇒ **最后一次为准**。
    """
    files = sorted(Path(job_root).glob(CORRECTIONS_GLOB), key=lambda p: p.stat().st_mtime)
    if not files:
        return {"status": "none", "entries": {}, "by_coord": {}, "files": 0, "errors": []}
    entries: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path.name}: {type(exc).__name__}")
            continue
        corr = doc.get("corrections") if isinstance(doc, Mapping) and isinstance(doc.get("corrections"), Mapping) else (
            doc if isinstance(doc, Mapping) else {})
        for key, value in (corr or {}).items():
            if not isinstance(value, Mapping):
                continue
            artifact, _, event_id = str(key).partition("::")
            if date and artifact and date not in artifact:
                continue                    # 跨日隔离：artifact 路径必须含目标日期
            entry = {"event_id": event_id or str(key), "artifact": artifact, "fields": dict(value),
                     "source_file": str(path)}
            entries[entry["event_id"]] = entry
    # [P1] 只要有一份文件读失败就整体 unknown（**但仍保留已解析的 entries**）：
    # 混合体（1 份合法 + 1 份损坏）如果判 ok，损坏那份会被静默丢弃，用户以为纠正生效了。
    if errors:
        return {"status": "unknown", "entries": entries, "by_coord": {},
                "files": len(files), "errors": errors}
    return {"status": "ok", "entries": entries, "by_coord": {}, "files": len(files), "errors": errors}


def _index_by_coord(index: dict[str, Any], events_by_id: Mapping[str, Mapping[str, Any]]) -> None:
    """按**精确**坐标 (source_id,row_id) 与**精确** quote 建索引。

    quote 只在“唯一命中”时启用：同一 quote 对应多个事件 ⇒ 视为歧义，跳过（fail-closed）。
    """
    quotes: dict[str, list[dict[str, Any]]] = {}
    for eid, entry in index["entries"].items():
        ev = events_by_id.get(eid)
        if not isinstance(ev, Mapping):
            continue
        evidence = ev.get("evidence") or {}
        for k in _coord_keys(evidence):
            index["by_coord"][k] = entry
        q = evidence.get("quote")
        if isinstance(q, str) and q.strip():
            quotes.setdefault(q, []).append(entry)
    for q, entries in quotes.items():
        if len(entries) == 1:
            index.setdefault("by_quote", {})[q] = entries[0]


def load_events_for_date(job_root: Path, date: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in Path(job_root).glob(f"jobs/job-*/files/*/result/{date}/events.json"):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(payload, list):
            for ev in payload:
                if isinstance(ev, Mapping) and ev.get("event_id"):
                    out[str(ev["event_id"])] = dict(ev)
    return out


def apply_to_activities(payload: dict[str, Any], job_root: Path, date: str) -> dict[str, Any]:
    """把纠正应用到 jev activities（item 级坐标匹配；单 item 活动同步标题）。"""
    index = load_event_corrections(job_root, date)
    _index_by_coord(index, load_events_for_date(job_root, date))
    payload[STATUS_FIELD] = index["status"]
    payload["corrections_errors"] = list(index["errors"])
    if index["status"] == "unknown":
        payload["corrections_unknown_reason"] = "; ".join(index["errors"])[:300]
        # unknown 时仍应用已解析的条目（保留可用信息），但状态必须显式 unknown
        payload = _apply_with_index(payload, index)
        return payload
    if index["status"] == "none":
        return payload
    return _apply_with_index(payload, index)


def _apply_with_index(payload: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    # [P2-1] 先统计时间线里每个 quote 命中多少个 item；>1 即歧义，整条跳过（不猜）。
    timeline_hits: dict[str, int] = {}
    for act in payload.get("activities") or []:
        for item in (act.get("items") or []):
            if not isinstance(item, Mapping):
                continue
            for ev in item.get("evidence") or []:
                q = ev.get("quote") if isinstance(ev, Mapping) else None
                if isinstance(q, str) and q.strip():
                    timeline_hits[q] = timeline_hits.get(q, 0) + 1
    ambiguous_skipped = 0
    _amb_seen: set[str] = set()
    applied_total = 0
    for act in payload.get("activities") or []:
        if not isinstance(act, dict):
            continue
        applied: list[dict[str, Any]] = []
        items = act.get("items") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            for ev in item.get("evidence") or []:
                if not isinstance(ev, Mapping):
                    continue
                entry = None
                for k in _coord_keys(ev):
                    entry = index["by_coord"].get(k)
                    if entry:
                        break
                if entry is None:
                    q = ev.get("quote")
                    if isinstance(q, str) and q.strip():
                        if timeline_hits.get(q, 0) > 1:
                            ambiguous_skipped += 1
                            _amb_seen.add(q)
                            continue
                        entry = (index.get("by_quote") or {}).get(q)
                if not entry:
                    continue
                new_title = entry["fields"].get("title") or entry["fields"].get("text")
                if not isinstance(new_title, str) or not new_title.strip():
                    continue
                if item.get("action") == new_title:
                    continue
                item.setdefault("action_original", item.get("action"))
                item["action"] = new_title
                applied.append({"event_id": entry["event_id"], "scope": "item",
                                "field": "action", "source_file": Path(entry["source_file"]).name})
        if applied and len(items) == 1:
            old_title = act.get("title")
            if old_title != items[0].get("action"):
                act.setdefault("title_original", old_title)
                act["title"] = items[0]["action"]
                applied.append({"event_id": applied[0]["event_id"], "scope": "activity",
                                "field": "title"})
        if applied:
            act.setdefault(APPLIED_FIELD, []).extend(applied)
            applied_total += len(applied)
    if applied_total:
        payload["corrections_applied_count"] = applied_total
    if ambiguous_skipped:
        payload["ambiguous_skipped"] = ambiguous_skipped
        payload["ambiguous_quotes"] = sorted(_amb_seen)[:20]
    return payload


def apply_to_events(events: Iterable[Mapping[str, Any]], job_root: Path, date: str) -> tuple[list[dict[str, Any]], str, list[str]]:
    """dashboard action groups 用：直接按 event_id 覆盖事件标题（保留 title_original）。"""
    index = load_event_corrections(job_root, date)
    out = [dict(e) for e in events if isinstance(e, Mapping)]
    if index["status"] != "ok":
        return out, index["status"], list(index["errors"])
    for ev in out:
        entry = index["entries"].get(str(ev.get("event_id") or ""))
        if not entry:
            continue
        new_title = entry["fields"].get("title") or entry["fields"].get("text")
        if isinstance(new_title, str) and new_title.strip() and ev.get("title") != new_title:
            ev.setdefault("title_original", ev.get("title"))
            ev["title"] = new_title
            ev.setdefault(APPLIED_FIELD, []).append(
                {"event_id": entry["event_id"], "scope": "event", "field": "title"})
    return out, index["status"], list(index["errors"])
