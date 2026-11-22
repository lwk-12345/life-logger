"""一天的事实只从这里读：账本、日报、统计都走这个入口（docs/adr/0005）。

从 2026-11-06 起，当天有活动产物时，四本账只认活动与用户手动/确认事件；
更早的历史及无活动产物的日期保留原口径。

* ``day_expense_ledger``：按切换规则合并事件与活动，网页账本和区间统计在用。
* ``event_only_expense_ledger``：只读事件，日报生成、卡片、统计在用。
"""
from __future__ import annotations

from datetime import date as _date, datetime, timedelta, timezone
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from life_logger.expense_corrections import (
    enrich_ledger_with_edits,
    resolve_confirmed_and_edits,
)
from life_logger.expense_policy import build_expense_ledger
from life_logger.fact_types import has_self_evidence
from life_logger.timeutil import local_date

PROJECT_ROOT = Path(__file__).resolve().parents[2]
#: 生产数据根（output/local-web）。所有 helper 默认值都是它，
#: 但**调用方传了 root 就必须用调用方的**（审计 D1：隔离数据根仍读到生产活动）。
DEFAULT_DATA_ROOT = PROJECT_ROOT / "output" / "local-web"

#: (数据根, 日期) → (mtime, activities)。按根区分，避免不同实例串数据。
_DAY_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]] | None]] = {}

#: 用户 2026-11-04 确认：这天之前的账冻结，从这天起活动为主（P3 生效）。
FACT_CUTOVER_DATE = "2026-10-26"

#: 用户 2026-11-07 拍板：从这天起（含当天有活动层产物时），四本账只认活动层 + 用户手动/复盘录入事件，不再混入单句自动抽取事件。
ACTIVITY_ONLY_CUTOVER_DATE = "2026-11-06"

_USER_EVENT_SOURCES = frozenset({
    "manual", "user", "manual_entry", "quick_input", "nutrition_statement",
    "user_judgment", "judgment_queue", "habit_capture",
})

_REVIEW_FILENAME = "ledger-review.json"
_SHANGHAI = timezone(timedelta(hours=8))


def data_root(root: Path | str | None = None) -> Path:
    """把调用方给的 root 归一成「数据根」（含 activity-timeline/ 的那一层）。

    兼容三种写法：数据根本身、仓库根（含 output/local-web）、None（生产默认）。
    ``None`` 时**按当前 PROJECT_ROOT 现算**，测试 monkeypatch PROJECT_ROOT 才会生效。
    """
    if root is None:
        return Path(PROJECT_ROOT) / "output" / "local-web"
    path = Path(root)
    if (path / "activity-timeline").is_dir():
        return path
    if (path / "output" / "local-web").is_dir():
        return path / "output" / "local-web"
    return path


def load_ledger_review(root: Path | str | None = None) -> dict[str, str]:
    """用户的人工复核决定：{event_id: exclude | pending}。

    文件坏了按没有决定处理，绝不因为复核文件损坏改动金额。删掉一条就撤销。
    """
    path = data_root(root) / _REVIEW_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        decisions = payload["decisions"]
        return {str(key): str(value["action"]) for key, value in decisions.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def load_ledger_confirmed(root: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """用户确认过的金额：{event_id: {amount, label, ...}}。文件坏了按没有处理。"""
    path = data_root(root) / _REVIEW_FILENAME
    try:
        decisions = json.loads(path.read_text(encoding="utf-8"))["decisions"]
        return {
            str(key): {**value, "amount": float(value["amount"]), "label": value.get("label")}
            for key, value in decisions.items()
            if isinstance(value, Mapping) and value.get("action") == "confirm"
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _prepare_confirmed(
    events: Sequence[Mapping[str, Any]] | None,
    activities: Sequence[Mapping[str, Any]] | None,
    *,
    root: Path | str | None = None,
    day: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    confirmed_decisions = load_ledger_confirmed(root) if root is not None else load_ledger_confirmed()
    if not confirmed_decisions:
        return {}, {}, []
    return resolve_confirmed_and_edits(
        confirmed_decisions,
        events,
        activities,
        day=day,
        start=start,
        end=end,
    )


def _finalize_ledger(
    ledger: dict[str, Any],
    matched_edits: Mapping[str, Mapping[str, Any]],
    unmatched_edits: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return enrich_ledger_with_edits(
        ledger,
        matched_edit_by_event_id=matched_edits,
        unmatched_edits=unmatched_edits,
    )


def activities_for_day(day: str, *, root: Path | str | None = None) -> list[dict[str, Any]] | None:
    """读当天活动；读不到返回 None，调用方按“没有活动”处理。

    ``root`` 为调用方数据根（store root）；不传才退回生产默认根。
    """
    base = data_root(root)
    path = base / "activity-timeline" / "jev" / f"{day}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _DAY_CACHE.pop((str(base), day), None)
        return None
    key = (str(base), day)
    cached = _DAY_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    activities = payload.get("activities")
    result = activities if isinstance(activities, list) else None
    _DAY_CACHE[key] = (mtime, result)
    return result


#: 宽区间一次最多算多少天（防止全库 2000–2099 被无界扫）。
MAX_RANGE_DAYS = 3660


def activities_for_range(
    start: str,
    end: str,
    *,
    root: Path | str | None = None,
    max_days: int = MAX_RANGE_DAYS,
    chunk_days: int = 31,
) -> list[dict[str, Any]]:
    """取 [start, end] 内的活动。

    [2026-11-07 D2] 旧实现「区间 ≥31 天直接返回空」，于是「无日期搜索」用的
    2000–2099 区间永远拿不到活动（同一批事件 703.91 vs 逐日 1471.91）。
    现在：
      * 窄区间（≤ ``max_days``）逐日读，按 ``chunk_days`` 分块；
      * 宽区间**直接列目录**再按日期过滤，不做 3.6 万次 stat 之外的浪费，
        也不会因为「太宽」而返回空。
    """
    try:
        begin = _date.fromisoformat(str(start))
        finish = _date.fromisoformat(str(end))
    except ValueError:
        return []
    if begin > finish:
        return []
    out: list[dict[str, Any]] = []
    span = (finish - begin).days + 1
    base = data_root(root) / "activity-timeline" / "jev"
    if span > max_days:
        # 宽区间：列目录，按日期过滤（目录里只有真实存在的日文件）
        try:
            names = sorted(p.stem for p in base.glob("*.json"))
        except OSError:
            return []
        for name in names:
            try:
                day = _date.fromisoformat(name)
            except ValueError:
                continue
            if begin <= day <= finish:
                activities = activities_for_day(name, root=root)
                if activities:
                    out.extend(a for a in activities if isinstance(a, Mapping))
        return out
    cursor = begin
    step = max(1, int(chunk_days))
    while cursor <= finish:
        chunk_end = min(cursor + timedelta(days=step - 1), finish)
        while cursor <= chunk_end:
            activities = activities_for_day(cursor.isoformat(), root=root)
            if activities:
                out.extend(a for a in activities if isinstance(a, Mapping))
            cursor += timedelta(days=1)
    return out


def is_user_or_manual_event(event: Mapping[str, Any]) -> bool:
    """Explicit user provenance, not an automatic extractor's confidence/status alone."""
    if str(event.get("event_id") or "").startswith(("manual_", "nutr_", "habit_", "user_")):
        return True
    if any(str(event.get(key) or "") in _USER_EVENT_SOURCES
           for key in ("capture_source", "source", "origin", "extractor")):
        return True
    if event.get("manual") is True or event.get("is_manual") is True:
        return True
    evidence = event.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    if (str(evidence.get("source_id") or "").startswith(("manual", "nutr_stmt", "judgment"))
            or str(evidence.get("extractor") or "") in {"manual", "user_judgment"}):
        return True
    # save_manual_event / natural_correction / human-reviewed derived events.
    if (str(event.get("source_type") or "") in {"manual", "user_supplement"}
            or str(evidence.get("source") or "") in {
                "user_confirmation", "user_supplement", "user_spoken_supplement", "user_judgment"}):
        return True
    if event.get("review_status") == "confirmed" and (
        event.get("correction_status") == "corrected"
        or (evidence.get("review_source") == "human" and evidence.get("source_decision") == "self")
    ):
        return True
    data = event.get("data")
    if isinstance(data, Mapping):
        for domain in ("food", "diet", "expense"):
            payload = data.get(domain)
            if isinstance(payload, Mapping) and (
                str(payload.get("nutrition_source") or "") in {"user_statement", "user_judgment"}
                or str(payload.get("estimate_basis") or "") in {"user_adopted_candidate", "user_weight"}
            ):
                return True
    return False


def _event_shanghai_day(event: Mapping[str, Any]) -> str | None:
    """Use occurrence time, then the reported-time fallback of the canonical read view."""
    when = event.get("time")
    when = when if isinstance(when, Mapping) else {}
    evidence = event.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    for value in (when.get("absolute"), when.get("absolute_local"), when.get("local"),
                  event.get("reported_at"), evidence.get("reported_at"),
                  evidence.get("reported_at_local"), when.get("date")):
        day = local_date(value)
        if day:
            return day
    return None


def filter_events_for_activity_cutover(
    events: Sequence[Mapping[str, Any]], *, day: str | None,
    activities: Sequence[Mapping[str, Any]] | None,
    cutover_date: str = ACTIVITY_ONLY_CUTOVER_DATE,
) -> list[Mapping[str, Any]]:
    """Retire automatic event inputs only when activity products are available.

    No input is mutated. An undated event is preserved rather than assigned a date.
    Range callers must supply each day's own activities, not another day's product.
    """
    if not activities or (day is not None and day < cutover_date):
        return list(events)
    out = []
    for event in events:
        ev_day = day if day is not None else _event_shanghai_day(event)
        if ev_day and ev_day >= cutover_date and not is_user_or_manual_event(event):
            continue
        out.append(event)
    return out


def day_expense_ledger(events: Sequence[Mapping[str, Any]], day: str, *,
                       root: Path | str | None = None) -> dict[str, Any]:
    activities = activities_for_day(day, root=root) if root is not None else activities_for_day(day)
    events = filter_events_for_activity_cutover(events, day=day, activities=activities)
    review = load_ledger_review(root) if root is not None else load_ledger_review()
    confirmed, matched_edits, unmatched_edits = _prepare_confirmed(
        events, activities, root=root, day=day
    )
    ledger = build_expense_ledger(
        events,
        activities=activities,
        review=review,
        activity_rules_from=FACT_CUTOVER_DATE,
        confirmed=confirmed,
    )
    return _finalize_ledger(ledger, matched_edits, unmatched_edits)


def range_expense_ledger(events: Sequence[Mapping[str, Any]], start: str, end: str, *,
                         root: Path | str | None = None) -> dict[str, Any]:
    activities = activities_for_range(start, end, root=root) if root is not None else activities_for_range(start, end)
    day_products: dict[str, list[dict[str, Any]] | None] = {}
    kept = []
    for event in events:
        ev_day = _event_shanghai_day(event)
        if ev_day and start <= ev_day <= end and ev_day >= ACTIVITY_ONLY_CUTOVER_DATE:
            if ev_day not in day_products:
                day_products[ev_day] = (activities_for_day(ev_day, root=root) if root is not None
                                        else activities_for_day(ev_day))
            if not filter_events_for_activity_cutover([event], day=ev_day, activities=day_products[ev_day]):
                continue
        kept.append(event)
    review = load_ledger_review(root) if root is not None else load_ledger_review()
    confirmed, matched_edits, unmatched_edits = _prepare_confirmed(
        kept, activities, root=root, start=start, end=end
    )
    ledger = build_expense_ledger(
        kept,
        activities=activities,
        review=review,
        activity_rules_from=FACT_CUTOVER_DATE,
        confirmed=confirmed,
    )
    return _finalize_ledger(ledger, matched_edits, unmatched_edits)


def event_only_expense_ledger(events: Sequence[Mapping[str, Any]], *,
                              root: Path | str | None = None) -> dict[str, Any]:
    review = load_ledger_review(root) if root is not None else load_ledger_review()
    confirmed, matched_edits, unmatched_edits = _prepare_confirmed(
        events, None, root=root
    )
    ledger = build_expense_ledger(events, review=review, confirmed=confirmed)
    return _finalize_ledger(ledger, matched_edits, unmatched_edits)


def activity_track_summary(day: str, *, root: Path | str | None = None) -> dict[str, Any] | None:
    """事件轨没有这一天时，只用活动算当天金额；没有产物返回 None，绝不把 0 当结果。"""
    activities = activities_for_day(day, root=root) if root is not None else activities_for_day(day)
    if not activities:
        return None
    try:
        review = load_ledger_review(root) if root is not None else load_ledger_review()
        confirmed, _, _ = _prepare_confirmed([], activities, root=root, day=day)
        ledger = build_expense_ledger([], activities=activities, review=review,
                                      activity_rules_from=FACT_CUTOVER_DATE, confirmed=confirmed)
    except Exception:  # noqa: BLE001 - 补位失败不能拖垮整个区间查询
        return None
    total = round(float(ledger.get("total_amount") or 0.0), 2)
    if total <= 0 and not (ledger.get("transactions") or []):
        return None
    return {"total_cny": total, "event_count": 0, "source": "activity_track"}


def report_activities(activities: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """压缩成日报的模型输入：只留本人说的话，去掉媒体，避免把视频内容当成当天发生的事。"""
    out: list[dict[str, Any]] = []
    for activity in activities or []:
        if not isinstance(activity, Mapping) or str(activity.get("category") or "").startswith("浏览"):
            continue
        items = []
        for item in activity.get("items") or []:
            if not isinstance(item, Mapping) or not has_self_evidence(item):
                continue
            quotes = [str(entry.get("quote") or "")[:80] for entry in item.get("evidence") or []
                      if isinstance(entry, Mapping) and entry.get("source_label") == "self"]
            items.append({"action": item.get("action"), "status": item.get("status"),
                          "amount": item.get("amount"), "currency": item.get("currency"), "quotes": quotes[:2]})
        if items:
            out.append({"time": _shanghai_clock(activity.get("occurrence_start") or activity.get("spoken_at")),
                        "title": activity.get("title"), "category": activity.get("category"), "items": items})
    return out


def _shanghai_clock(value: Any) -> str | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone(_SHANGHAI).strftime("%H:%M")
    except (TypeError, ValueError):
        return None


def _domain_speech_anchor(quote: Any, stamp: Any) -> tuple[str, datetime, datetime] | None:
    """A long verbatim utterance and the precision interval of its speech timestamp."""
    text = str(quote or "").strip()
    if len(text) < 8 or not stamp:
        return None
    raw = str(stamp)
    try:
        start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if start.tzinfo is None:
        return None
    fraction = re.search(r"T\d{2}:\d{2}:\d{2}\.(\d+)", raw)
    resolution = 10 ** (6 - min(len(fraction[1]), 6)) if fraction else 1_000_000
    return text, start, start + timedelta(microseconds=resolution)


# --------------------------------------------------------------------------------------
# 活动 → 饮食事件：结构化克数 + 食物名
# --------------------------------------------------------------------------------------

#: 这些量词/单位表示「重量」，可以直接当 weight_g 用。
_WEIGHT_UNITS = {"克": 1.0, "g": 1.0, "gram": 1.0, "grams": 1.0, "千克": 1000.0,
                 "kg": 1000.0, "公斤": 1000.0, "斤": 500.0, "两": 50.0}
#: 时间词 / 动词 / 数量 —— 从 action 里剥掉之后剩下的才是食物名。
_FOOD_STRIP_RE = re.compile(
    r"(?:今天|明天|昨天|前天|后天|今早|今儿|刚才|刚刚|早上|早晨|上午|中午|下午|晚上|傍晚|夜里|夜宵|"
    r"早饭|早餐|午饭|午餐|晚饭|晚餐|加餐|宵夜)"
    r"|(?:补报|汇报|上报)"
    r"|(?:我|我们|本人)"
    r"|(?:主食为|主食是|主食)"
    r"|(?:吃了|吃|喝了|喝|来了|来|点了|点|干了|干|消灭了|消灭|吞了)"
    r"|(?:大概|大约|约|差不多|左右|一共|总共|共|又|还|再)"
    r"|[一二两三四五六七八九十半几]\s*(?:份|杯|碗|个|袋|包|只|片|根|串|块|把|勺|盒|瓶)"
    r"|\d+(?:\.\d+)?\s*(?:克|g|kg|公斤|千克|斤|两|毫升|ml|ml\.|杯|碗|个|份|袋|包|只|片|根|串|块)"
    r"|\d+(?:\.\d+)?"
)
_FOOD_EDGE_RE = re.compile(r"^[\s，,。.、；;：:！!？?\-—_]+|[\s，,。.、；;：:！!？?\-—_]+$")


def _weight_grams_from_item(item: Mapping[str, Any]) -> float | None:
    """只在**结构化字段**明确是重量时取克数；不从句子另猜。

    * V3 结构化 ``item["food"]["grams"]``（或 ``weight_g``）直接是克数；
    * V2 ``amount + unit`` 且 ``amount_kind`` 为空或 ``weight`` 才算；
    * ``quantity``/``份``/``个`` 这类含糊计数一律不算。
    """
    structured_food = item.get("food")
    if isinstance(structured_food, Mapping):
        for g_key in ("grams", "weight_g"):
            raw_g = structured_food.get(g_key)
            if raw_g is not None and not isinstance(raw_g, bool):
                try:
                    val = float(raw_g)
                except (TypeError, ValueError):
                    val = -1.0
                if math.isfinite(val) and val > 0:
                    return val
    kind = str(item.get("amount_kind") or "").strip().lower()
    if kind and kind not in {"weight", "mass"}:
        return None
    try:
        amount = float(item.get("amount"))
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    unit = str(item.get("unit") or "").strip().lower()
    factor = _WEIGHT_UNITS.get(unit)
    if factor is None:
        return None
    return amount * factor


def food_name_from_action(action: str) -> str:
    """从活动层的 action 里剥掉时间/动词/数量，剩下的名词短语当食物名。

    ``今天早上吃了400克三全猪肉芹菜速冻水饺`` → ``三全猪肉芹菜速冻水饺``
    剥不干净（结果过短或仍含动词）时返回空串，让上游走「未知」，不硬凑。
    """
    text = _FOOD_STRIP_RE.sub(" ", str(action or ""))
    text = _FOOD_EDGE_RE.sub("", re.sub(r"\s+", " ", text))
    if len(normalize_food_name(text)) < 2:
        return ""
    return text


def normalize_food_name(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", str(text or ""))

def domain_events(
    events: Sequence[Mapping[str, Any]], activities: Sequence[Mapping[str, Any]] | None,
    day: str | None, fact_type: str,
) -> list[Mapping[str, Any]]:
    """Read-only domain projection; only explicitly typed, self-supported activity facts.

    Stable event references or the same source/quote identify legacy duplicates.
    Text alone, recording IDs and amounts never establish identity.
    """
    events = filter_events_for_activity_cutover(events, day=day, activities=activities)
    out = list(events)
    if not activities or (day is not None and day < FACT_CUTOVER_DATE):
        return out
    from life_logger.activity_timeline_runtime import activity_lifecycle

    domain = {"food": "food", "exercise": "exercise", "smoke": "smoking"}[fact_type]
    legacy = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        categories = event.get("categories") or []
        categories = [categories] if isinstance(categories, str) else categories
        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        if domain in categories or (domain == "food" and "diet" in categories) or data.get(domain) or (domain == "food" and data.get("diet")):
            legacy.append(event)
    legacy_ids = {str(e["event_id"]) for e in legacy if e.get("event_id")}
    legacy_sources = {(str(ev.get("source_id")), str(ev.get("quote") or "").strip())
                      for e in legacy for ev in [e.get("evidence") or {}] if isinstance(ev, Mapping) and ev.get("source_id") and ev.get("quote")}
    legacy_speech = []
    for event in legacy:
        ev = event.get("evidence") or {}
        if not isinstance(ev, Mapping) or ev.get("source_label") != "self":
            continue
        anchor = _domain_speech_anchor(ev.get("quote"), ev.get("reported_at") or event.get("reported_at"))
        if anchor:
            legacy_speech.append(anchor)
    seen_items, seen_sources = set(), set()
    uncertain = {"planned", "pending", "cancelled", "canceled", "unknown", "uncertain", "conflict",
                 "计划", "计划中", "待确认", "待办", "已取消", "未知", "不确定", "讨论中", "已提出"}
    not_fact = re.compile(r"询问|提问|计划|打算|准备|建议|讨论|他人|别人|转述|想(?:吃|喝|抽|吸|练|去)|(?:没|未|没有)(?:吃|喝|练|抽|吸)")
    for ai, activity in enumerate(activities):
        if not isinstance(activity, Mapping):
            continue
        if not any(isinstance(i, Mapping) and i.get("fact_type") == fact_type for i in activity.get("items") or []):
            continue
        if activity_lifecycle(activity, day) != "live":
            continue
        for ii, item in enumerate(activity.get("items") or []):
            if not isinstance(item, Mapping) or item.get("fact_type") != fact_type:
                continue
            if item.get("item_lifecycle") in {"media", "unknown"} or not has_self_evidence(item):
                continue
            if str(item.get("status") or "").lower() in uncertain:
                continue
            action = str(item.get("action") or "").strip()
            evidence = [e for e in item.get("evidence") or [] if isinstance(e, Mapping) and e.get("source_label") == "self" and e.get("quote")]
            if not action or not evidence or not_fact.search(action):
                continue
            effective_day = day
            if effective_day is None:
                try:
                    stamp = datetime.fromisoformat(str(item.get("spoken_at") or activity.get("spoken_at")).replace("Z", "+00:00"))
                    effective_day = (stamp.replace(tzinfo=_SHANGHAI) if stamp.tzinfo is None else stamp).astimezone(_SHANGHAI).date().isoformat()
                except (ValueError, TypeError):
                    continue
            if effective_day < FACT_CUTOVER_DATE:
                continue
            refs = {str(b.get("source_event_id")) for b in [item, *evidence] if b.get("source_event_id")}
            sources = {(str(e["source_id"]), str(e["quote"]).strip()) for e in evidence if e.get("source_id")}
            if refs & legacy_ids or sources & legacy_sources:
                continue
            # Old/formal and watch pipelines can use different source IDs. Only
            # a unique verbatim speech anchor with compatible stated precision
            # bridges them; never use occurrence time, a recording or text alone.
            anchors = [_domain_speech_anchor(e["quote"], e.get("reported_at") or (item.get("spoken_at") if len(evidence) == 1 else None)) for e in evidence]
            if anchors and all(anchors):
                matches = [old for old in legacy_speech if all(
                    old[0] == anchor[0] and max(old[1], anchor[1]) < min(old[2], anchor[2]) for anchor in anchors
                )]
                if len(matches) == 1:
                    continue
            aid, iid = activity.get("activity_id"), item.get("item_id")
            identity = (aid, iid, action) if aid and iid else None
            source_identity = (tuple(sorted(sources)), action) if sources else None
            if (identity and identity in seen_items) or (source_identity and source_identity in seen_sources):
                continue
            if identity: seen_items.add(identity)
            if source_identity: seen_sources.add(source_identity)
            data = {domain: {}}
            if fact_type == "food":
                data[domain] = _food_payload(
                    item, action, activity_only=effective_day >= ACTIVITY_ONLY_CUTOVER_DATE,
                )
            elif fact_type == "exercise":
                exercise = {"name": action}
                structured = item.get("exercise")
                if isinstance(structured, Mapping) and structured.get("name"):
                    exercise = {key: structured.get(key) for key in
                                ("name", "sets", "reps", "weight_value", "weight_unit", "side", "duration_seconds")
                                if structured.get(key) is not None}
                elif item.get("duration_seconds") is not None:
                    exercise["duration_seconds"] = item["duration_seconds"]
                data[domain] = {"exercises": [exercise]}
            else:
                purchase = re.search(r"买|购买|购入", action) and not re.search(r"抽|吸", action)
                data[domain] = {"action": "purchase" if purchase else "smoke", "count": None}
            provenance = {"origin": "activity", "activity_id": aid, "item_id": iid,
                          "fact_type": fact_type, "status": item.get("status"),
                          "spoken_at": item.get("spoken_at"), "occurred_at": item.get("occurred_at"),
                          "source_ids": sorted({s for s, _ in sources})}
            if fact_type == "smoke": provenance["count"] = None
            if item.get("fact_schema_version") == 3:
                provenance["fact_schema_version"] = 3
            out.append({"event_id": f"activity:{effective_day}:{aid or ai}:{iid or ii}",
                        "title": action, "status": item.get("status"), "categories": [domain],
                        "activity_type": "meal" if fact_type == "food" else domain,
                        "data": data, "time": {"absolute": item.get("occurred_at"), "clock": _shanghai_clock(item.get("occurred_at"))},
                        "evidence": {"quote": "\n".join(str(e["quote"]) for e in evidence)},
                        "_activity_source": provenance,
                        **({"fact_schema_version": 3} if item.get("fact_schema_version") == 3 else {})})
    return out



def _food_payload(item: Mapping[str, Any], action: str, *, activity_only: bool = False) -> dict[str, Any]:
    """活动 item → 饮食事件条目。

    [2026-11-06] 之前把整句 action 当食物名，且丢掉 item 上已有的克数：
    ``{"item": "今天早上吃了400克三全猪肉芹菜速冻水饺"}`` → 查库必失败。
    [2026-11-08] 兼容 V3 结构化 ``item["food"] = {"name", "meal", "grams", "portion"}``，
    不再因 ``item["food"]`` 是 dict 而退回 action 剥词（导致出现「补报 鸡胸肉丸」「补报 主食为生重 米」）。
    """
    payload: dict[str, Any] = {}
    grams = _weight_grams_from_item(item)
    name = ""
    structured_name = False
    structured_food = item.get("food")
    if isinstance(structured_food, Mapping):
        for key in ("name", "item", "food_name", "item_name", "dish"):
            value = structured_food.get(key)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                structured_name = True
                break
        raw_meal = structured_food.get("meal")
        if isinstance(raw_meal, str) and raw_meal.strip():
            payload["meal"] = raw_meal.strip()
        raw_portion = structured_food.get("portion")
        if isinstance(raw_portion, str) and raw_portion.strip():
            payload["portion"] = raw_portion.strip()
        for copy_key in (
            "estimated_calories",
            "nutrients",
            "nutrition_per_100g",
            "estimate_basis",
            "estimate_confidence",
            "nutrition_source",
            "nutrition_source_label",
            "nutrition_matched_name",
            "nutrition_serving_source",
        ):
            if structured_food.get(copy_key) is not None:
                payload[copy_key] = structured_food[copy_key]
    if not name:
        for key in ("food_name", "item_name", "dish", "name", "food"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                structured_name = key in ("food_name", "name")
                break
    if not name:
        name_action = action
        if activity_only:
            # New activity-only days cannot fall back to legacy events' food names.
            # Strip narration and spoken Chinese weight words, without inferring grams.
            name_action = re.sub(r"^(?:称|表示|陈述|补报|汇报)(?=(?:自己|本人|我|今天)?(?:吃|喝|主食))", "", name_action)
            name_action = re.sub(
                r"[零〇一二两三四五六七八九十百千万点半]+\s*(?:千克|公斤|克|斤|两)(?:的)?",
                "", name_action,
            )
        name = food_name_from_action(name_action)
    evidence_quotes = " ".join(
        str(e.get("quote") or "")
        for e in (item.get("evidence") or [])
        if isinstance(e, Mapping)
    )
    raw_hint_blob = f"{name} {payload.get('portion') or ''} {action} {evidence_quotes}"
    if name in {"米饭", "米", "大米", "白米", "白米饭", "大米饭", "生重米", "生米", "生米饭", "米饭（生重）", "米饭(生重)"} and re.search(
        r"生重|生米|生大米", raw_hint_blob
    ):
        name = "米饭（生重）"
        payload["raw_weight"] = True
    payload["item"] = name or action
    if grams is not None:
        payload["weight_g"] = int(round(grams)) if float(grams).is_integer() else grams
        payload["portion"] = f"{grams:g}克"
        if isinstance(structured_food, Mapping) and isinstance(structured_food.get("portion"), str) and structured_food["portion"].strip():
            payload["portion_note"] = structured_food["portion"].strip()
        payload["amount_kind"] = "weight"
    payload["_name_source"] = "structured" if structured_name else "action"
    return payload

def domain_ledgers(
    events: Sequence[Mapping[str, Any]], day: str | None = None, *,
    activities: Sequence[Mapping[str, Any]] | None = None,
    nutrition_options: Mapping[str, Any] | None = None,
    root: Path | str | None = None,
    only: set[str] | frozenset[str] | Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Domain ledgers include explicit activity facts from FACT_CUTOVER_DATE onward."""
    from life_logger.daily_cognitive_session import (
        build_fitness_ledger,
        build_nutrition_ledger,
        build_smoking_ledger,
    )

    if day is not None and day >= FACT_CUTOVER_DATE and activities is None:
        activities = activities_for_day(day, root=root) if root is not None else activities_for_day(day)
    events = filter_events_for_activity_cutover(events, day=day, activities=activities)
    wanted = set(only) if only is not None else {"nutrition", "fitness", "smoking"}
    out: dict[str, dict[str, Any]] = {}
    if "nutrition" in wanted:
        nutrition_kwargs = dict(nutrition_options or {})
        if root is not None and "web_root" not in nutrition_kwargs:
            nutrition_kwargs["web_root"] = Path(root)
        out["nutrition"] = build_nutrition_ledger(events, activities=activities, day=day, **nutrition_kwargs)
    if "fitness" in wanted:
        out["fitness"] = build_fitness_ledger(events, activities=activities, day=day)
    if "smoking" in wanted:
        out["smoking"] = build_smoking_ledger(events, activities=activities, day=day)
    return out
