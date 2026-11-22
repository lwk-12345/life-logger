"""从 local_web.py 机械搬出的 web_expense_range 部分，行为不变；分组依据见 work/split-prep-localweb-20261104/。"""
from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from typing import Mapping
from zoneinfo import ZoneInfo
import hashlib
import json
from life_logger.web_common import _EXPENSE_RANGE_CACHE_DIRNAME, logger


def _expense_range_signature(root: Path, date_from: str, date_to: str, scope: str, view: str) -> str:
    """源数据指纹：任何相关产物或日报变化都会让区间缓存失效。

    为什么需要：`build_event_view` 一次区间扫描实测约 3s（要读全部 job 产物 +
    watch-session 事件并做跨源去重/别名）。而区间账本本质是"同一本账的累加"，
    结果只随源数据变——用指纹缓存后，稳态响应是毫秒级。
    """
    # 【2026-10-24 F2-S1】输入清单统一由 `day_views.SOURCE_PATTERNS` 提供：
    # 原来这里只列 3 条 glob，漏掉 manual-events / daily_summary / review-derived /
    # review-decisions / broadcast-decisions 等输入，导致区间账本缓存不失效。
    from life_logger.day_views import source_fingerprint

    days: list[str] = []
    try:
        cursor_dt = datetime.fromisoformat(str(date_from))
        end_dt = datetime.fromisoformat(str(date_to))
        while cursor_dt <= end_dt:
            days.append(cursor_dt.date().isoformat())
            cursor_dt += timedelta(days=1)
    except ValueError:  # 非法日期由上层校验，这里退化为全库指纹
        days = []
    fingerprint = source_fingerprint(root, days or None)
    # 指纹只描述"输入内容"，口径参数仍由这里 seed，二者共同决定缓存键。
    seed = f"{date_from}|{date_to}|{scope}|{view}|{fingerprint}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def _expense_day_index(
    root: Path,
    *,
    scope: str,
    view: str,
    event_filter: Any | None,
    index_start: str,
    index_end: str,
) -> dict[str, Any]:
    """构建（或读取）**按天的账本索引**，一次扫描覆盖整个观察窗口。

    为什么不是每次按 from/to 扫：`build_event_view` 一次区间扫描实测 0.8~2.2s，
    而用户随时会换区间。索引按「源数据指纹」缓存到本地文件，
    冷启动付一次扫描成本，之后任何区间都是毫秒级切片。
    """
    from life_logger.event_view import build_event_view
    from life_logger.day_facts import day_expense_ledger

    signature = _expense_range_signature(root, index_start, index_end, scope, view)
    cache_dir = root / _EXPENSE_RANGE_CACHE_DIRNAME
    cache_file = cache_dir / f"expense-day-index-{scope}-{view}-{signature}.json"
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached.get("signature") == signature:
            return cached
    except (OSError, ValueError):
        pass

    payload = build_event_view(root, date_from=index_start, date_to=index_end, scope=scope, view=view)
    events: list[dict[str, Any]] = [
        dict(event) for event in payload.get("events", []) if isinstance(event, Mapping)
    ]
    if event_filter is not None:
        filtered = event_filter(events)
        if isinstance(filtered, list):
            events = [dict(e) for e in filtered if isinstance(e, Mapping)]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        day = str(event.get("effective_date") or "").strip()
        if day:
            grouped.setdefault(day, []).append(event)

    days: dict[str, dict[str, Any]] = {}
    for day, day_events in grouped.items():
        ledger = day_expense_ledger(day_events, day, root=root)
        days[day] = {
            "total_cny": round(float(ledger.get("total_amount") or 0.0), 2),
            "event_count": len(day_events),
        }
    index = {
        "signature": signature,
        "scope": scope,
        "view": view,
        "window": [index_start, index_end],
        "days": days,
        "generated_at": datetime.now(tz=ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    }
    try:
        # 原实现用固定 `.tmp` 名 → 两个进程同时写会互相截断/覆盖。
        from life_logger.atomic import write_json_atomic

        result = write_json_atomic(cache_file, index, on_durability_error="report")
        if result.durability == "unknown":
            logger.warning("已替换但持久性未知（durability unknown）: %s", cache_file)
    except OSError as exc:
        logger.warning("expense day index cache write failed (%s): %s", type(exc).__name__, exc)
    return index


def build_expense_range(
    root: Path | str,
    *,
    date_from: str,
    date_to: str,
    scope: str = "provisional",
    view: str = "display",
    event_filter: Any | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """区间账本聚合：**与 `/api/daily` 的 expense_ledger 完全同源**。

    设计约束（2026-10-19 A0 派单 P-6）：
    1. 只从 `event_view.build_event_view()` 取事件、只从
       `expense_policy.build_expense_ledger()` 算钱 —— 不重跑模型、不按标题重算；
    2. 没有账本依据的天标 `source="missing"`、金额 null，并进 `missing_days`；
       **绝不按 0 混进总额**（缺口必须可见）；
    3. 31 天区间稳态响应 ≤1s：按天索引按源数据指纹缓存，区间查询只是切片。

    与 `/api/stats` 的区别：stats 是"全维度统计"（分类/时段/运动数），
    本接口只回答"区间花了多少钱、每天多少"，避免两套口径。
    """
    from life_logger.day_facts import activity_track_summary

    from datetime import date as _date

    root_path = Path(root)
    try:
        start = _date.fromisoformat(str(date_from))
        end = _date.fromisoformat(str(date_to))
    except ValueError as error:
        raise ValueError("from/to 需要 YYYY-MM-DD") from error
    if start > end:
        raise ValueError("from 不能晚于 to")

    # 索引窗口固定为「今年年初 → 今天」：一次扫描服务所有区间查询。
    today = datetime.now(tz=ZoneInfo("Asia/Shanghai")).date()
    index_start = f"{today.year}-01-01"
    index_end = max(today.isoformat(), date_to)
    if use_cache:
        index = _expense_day_index(
            root_path, scope=scope, view=view, event_filter=event_filter,
            index_start=index_start, index_end=index_end,
        )
    else:
        index = _expense_day_index(
            root_path, scope=scope, view=view, event_filter=event_filter,
            index_start=index_start, index_end=index_end,
        )
        # 强制重算：删掉刚写出的缓存，确保下次调用仍能验证冷路径
        signature = index.get("signature")
        for stale in (root_path / _EXPENSE_RANGE_CACHE_DIRNAME).glob(
            f"expense-day-index-{scope}-{view}-{signature}.json"
        ):
            try:
                stale.unlink()
            except OSError:
                pass

    day_map: dict[str, dict[str, Any]] = index.get("days") or {}
    days: list[dict[str, Any]] = []
    missing_days: list[str] = []
    total_cny = 0.0
    cursor = start
    while cursor <= end:
        key = cursor.isoformat()
        row = day_map.get(key)
        has_synthesis = (root_path / "daily-synthesis" / f"synthesis-{key}.json").is_file()
        if row is None:
            # [2026-11-04 Q809 补位] 索引只收「有事件的日子」（grouped 以事件为键）。
            # 11-01 的 formal 事件轨是 0 条，但活动轨里有「买了一包烟十块 + 一瓶水六块九」——
            # 于是它既不在 day_map、也没有 synthesis → 永远 missing，区间总额里永远看不到这 16.9。
            # 这里按天补一次「活动轨账本」：只在**该日确有活动文件**时才算，算钱仍走同一个
            # build_expense_ledger(events=[], activities=...)，不新增任何金额口径。
            activity_ledger = activity_track_summary(key, root=root)
            if activity_ledger is not None:
                row = activity_ledger
                day_map[key] = activity_ledger
        if row is None and not has_synthesis:
            days.append({"date": key, "total_cny": None, "event_count": None, "source": "missing"})
            missing_days.append(key)
        else:
            amount = round(float((row or {}).get("total_cny") or 0.0), 2)
            total_cny += amount
            days.append({
                "date": key,
                "total_cny": amount,
                "event_count": None if row is None else row.get("event_count"),
                "source": "ledger",
            })
        cursor += timedelta(days=1)

    return {
        "from": date_from,
        "to": date_to,
        "scope": scope,
        "view": view,
        "total_cny": round(total_cny, 2),
        "by_day": days,
        "missing_days": missing_days,
        "days": len(days),
        "source": "event_view+expense_policy",
        "cache_signature": index.get("signature"),
        "generated_at": datetime.now(tz=ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    }
