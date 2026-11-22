"""活动展示投影（activity presentation）· 纯函数 · 只读 cards/links。

2026-10-25：本文件曾被误覆盖（`build_activity_presentation` 丢失，daily_brief ImportError）。
现按**既有测试合同**恢复：`tests/test_activity_presentation.py`、
`test_activity_presentation_inferred.py`、`test_daily_activity_presentation.py`。

职责：
  1. 显式声明（`links.members` + `member_meta` 声明一致）→ 紧凑摘要组；
  2. 没有显式声明时，**只对具体餐饮实体**做"同小时 + 同主体"的推断分组（`inferred_entity_group`），
     泛餐饮词（午饭/午餐/打包…）、非餐饮、跨小时、跨主体一律不推断；
  3. 冲突（声明冲突/上游冲突）只记录不成组，事件与交易全部保留；
  4. 不求和金额、不推断状态、不修改入参。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

SCHEMA = "activity-presentation-v1"

#: 泛餐饮词：出现这些说明没有具体实体，禁止推断分组。
GENERIC_MEAL_TERMS = (
    "午饭", "午餐", "晚饭", "晚餐", "早餐", "早饭", "夜宵", "外卖",
    "吃饭", "用餐", "打包", "正餐", "快餐", "餐饮", "菜品",
)

#: 标题里的修饰/动作词（归一化实体前先剥掉）。
_MODIFIER_WORDS = (
    "购买", "再次点", "又点", "点了一份", "点一份", "点了个", "点了", "点", "买了", "买",
    "一份儿", "一份", "一个", "两个", "明确要求", "要求", "口味要求", "口味调整为", "口味",
    "调整为", "改为", "改成", "加个", "再加", "再来", "还要",
    "微辣", "中辣", "特辣", "不辣", "辣", "少辣", "多辣",
)

FOOD_KINDS = {"meal", "food", "expense", "purchase"}


def _text(value: Any) -> str:
    return str(value or "")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _local_hour(value: Any) -> str | None:
    raw = _text(value).strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        moment = moment.astimezone(ZoneInfo("Asia/Shanghai"))
    except Exception:
        pass
    return moment.strftime("%Y-%m-%dT%H")


def _card_entity(card: Mapping[str, Any]) -> str | None:
    """从卡片里取"具体餐饮实体"（鸭腿 / 咖啡 …）；取不到或只是泛词 → None。"""
    kind = _text(card.get("kind")).strip().lower()
    # kind 为空/"other" 视为**未知**（合成卡常见），允许继续按标题/事实判断实体；
    # 明确是健身/游戏/工作等非餐饮类型才排除。
    if kind and kind not in FOOD_KINDS and kind != "other":
        return None
    candidate = ""
    for fact in card.get("facts") or []:
        if not isinstance(fact, Mapping):
            continue
        path = _text(fact.get("path"))
        if path.endswith("items[0].name") or path.endswith(".item") or path.endswith(".name"):
            value = fact.get("value")
            if isinstance(value, str) and value.strip():
                candidate = value.strip()
                break
    if not candidate:
        candidate = _text(card.get("title"))
    text = re.sub(r"[（(].*?[)）]", "", candidate)          # 去括号补充
    text = re.sub(r"¥\s*\d+(?:\.\d+)?", "", text)
    text = re.sub(r"\d+(?:\.\d+)?\s*元?", "", text)
    for word in _MODIFIER_WORDS:
        text = text.replace(word, "")
    text = re.sub(r"[^\u4e00-\u9fa5A-Za-z]+", "", text)
    if not text or len(text) < 2:
        return None
    if any(term in text for term in GENERIC_MEAL_TERMS):
        return None
    return text[:8]


def _card_actor(card: Mapping[str, Any]) -> str:
    evidence = _mapping(card.get("evidence"))
    label = _text(evidence.get("source_label") or card.get("source_label")).strip()
    return label or "unknown"


def _member_view(card: Mapping[str, Any]) -> dict[str, Any]:
    transactions = [dict(item) for item in (card.get("transactions") or []) if isinstance(item, Mapping)]
    transaction_ids: list[str] = []
    ledger_event_ids: list[str] = []
    for item in transactions:
        txn = _text(item.get("transaction_id")).strip()
        if txn:
            transaction_ids.append(txn)
        ledger = _text(item.get("event_id")).strip()
        if ledger:
            ledger_event_ids.append(ledger)
    facts = [dict(fact) for fact in (card.get("facts") or []) if isinstance(fact, Mapping)]
    return {
        "event_id": _text(card.get("source_event_id")),
        "title": card.get("title"),
        "kind": card.get("kind"),
        "state": _text(card.get("state")) or "unknown",
        "time": dict(_mapping(card.get("time"))),
        "location": card.get("location"),
        "facts": facts,
        "facts_paths": sorted({_text(f.get("path")) for f in facts if f.get("path")}),
        "transactions": transactions,
        "transaction_ids": transaction_ids,
        "ledger_event_ids": ledger_event_ids,
        "unknown_fields": list(card.get("unknown_fields") or []),
    }


def _select_representative_title(members: Sequence[Mapping[str, Any]]) -> str:
    """加权选择活动组代表标题：
    第一优先：带有交易/金额的核心卡片；
    第二优先：明确的点单/购买行为卡（非纯修饰词）；
    第三优先：纯属性/修饰卡兜底。
    """
    if not members:
        return "未命名活动"

    # 第一优先：带交易引用或明确金额符号的卡
    tx_candidates = []
    for m in members:
        title = _text(m.get("title")).strip()
        tx_ids = m.get("transaction_ids") or m.get("ledger_event_ids") or []
        if tx_ids or "¥" in title or re.search(r"\d+(?:\.\d+)?\s*元", title):
            tx_candidates.append(title)
    if tx_candidates:
        return tx_candidates[0]

    # 第二优先：明确动作行为卡，非纯口味修饰
    modifier_keywords = ("口味要求", "口味调整", "要求辣", "要求微辣", "调整为")
    action_candidates = []
    for m in members:
        title = _text(m.get("title")).strip()
        if not title:
            continue
        is_pure_modifier = any(mod in title for mod in modifier_keywords) and not any(kw in title for kw in ("点了", "点一份", "点", "买了", "买"))
        if not is_pure_modifier:
            action_candidates.append(title)
    if action_candidates:
        return action_candidates[0]

    # 第三优先：最末有效标题兜底
    for m in members:
        title = _text(m.get("title")).strip()
        if title:
            return title
    return "未命名活动"


def _summary(members: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    titles = [m["title"] for m in members if m.get("title")]
    transaction_ids: list[str] = []
    for member in members:
        transaction_ids.extend(member["transaction_ids"] or member["ledger_event_ids"])
    states = sorted({_text(m.get("state")) or "unknown" for m in members})
    return {
        "title": _select_representative_title(members),
        "member_count": len(members),
        "titles": titles,
        "transaction_ids": transaction_ids,
        "transaction_count": len(transaction_ids),
        "states": states,
        "amounts_summed": False,
    }


RESULT_SCHEMA = "activity-result-summary-v1"

#: facts 里这些字段只在"有逐字证据"时才写确定值（model_only 只列出来）
_DOMAIN_FACT_KEYS = ("name", "item", "rounds", "weight_kg", "reps", "portion", "flavor", "location", "duration_minutes")


def _fact_is_supported(fact: Mapping[str, Any]) -> bool:
    support = _text(fact.get("support")).strip()
    return support in {"", "quote_verbatim"}


def _result_summary(members: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """显式关联组的规范摘要：状态分开、领域事实有证据才写确定值、金额/时长不合计。"""
    states = [(_text(m.get("state")) or "unknown") for m in members]
    breakdown: dict[str, int] = {}
    for state in states:
        breakdown[state] = breakdown.get(state, 0) + 1
    if "planned" in breakdown and ({"completed", "done", "in_progress"} & set(breakdown)):
        status = "conflict"
        status_note = "计划与实际并存，未统一成完成"
    elif len(breakdown) == 1:
        status = states[0]
        status_note = None
    else:
        status = "mixed"
        status_note = "组内子项状态不一致，按子项展示"

    actions: list[str] = []
    items: list[str] = []
    places: list[str] = []
    durations: list[dict[str, Any]] = []
    model_only: list[dict[str, Any]] = []
    for member in members:
        for fact in member.get("facts") or []:
            if not isinstance(fact, Mapping):
                continue
            key = _text(fact.get("path")).rsplit(".", 1)[-1]
            value = fact.get("value")
            if key not in _DOMAIN_FACT_KEYS or value in (None, ""):
                continue
            if not _fact_is_supported(fact):
                model_only.append({"path": _text(fact.get("path")), "value": value,
                                   "support": fact.get("support"), "event_id": member.get("event_id")})
                continue
            if key == "name":
                actions.append(str(value))
            elif key in {"item", "rounds", "portion", "flavor"}:
                items.append(str(value))
            elif key == "location":
                places.append(str(value))
            elif key == "duration_minutes":
                durations.append({"minutes": float(value), "event_id": member.get("event_id")})

    return {
        "schema": RESULT_SCHEMA,
        "title": (actions[0] if actions else (members[0].get("title") if members else None)),
        "status": status,
        "status_note": status_note,
        "state_breakdown": breakdown,
        "actions": list(dict.fromkeys(actions)),
        "items": list(dict.fromkeys(items)),
        "locations": list(dict.fromkeys(places)),
        "durations": durations,
        "amounts_summed": False,
        "durations_summed": False,
        "model_only_facts": model_only,
        "note": "金额/时长不合计；model_only 事实不作为确定值展示",
    }


def _steps(members: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """长活动：只给摘要 + 每条子项按**真实时间**引用（不改时间来源）。"""
    steps: list[dict[str, Any]] = []
    for member in members:
        time_info = _mapping(member.get("time"))
        steps.append({
            "event_id": member.get("event_id"),
            "time": {
                "occurred_at": time_info.get("occurred_at") or time_info.get("absolute"),
                "reported_at": time_info.get("reported_at"),
                "precision": time_info.get("precision") or "unknown",
            },
            "state": _text(member.get("state")) or "unknown",
            "title": member.get("title"),
        })
    steps.sort(key=lambda item: (item["time"]["occurred_at"] is None, str(item["time"]["occurred_at"] or "")))
    return steps


FRIENDLY_CONFLICT_MESSAGES = {
    "dangling_reference": "相关联的记录尚未在本日找到，已保留原始片段供核对",
    "declaration_conflict": "记录间状态或主体信息不一致，已分别保留原记录供核对",
    "declaration_unverified": "关联依据尚在等待进一步核实",
    "actor_conflict": "涉及不同人员记录，已分别保留供核对",
    "kind_conflict": "属于不同类型的活动分类，已分别保留供核对",
    "upstream_conflict": "来源标记存疑，已保留原始记录供核对",
    "missing_namespace": "引用所属分类范围暂未明确",
    "invalid_ref_shape": "引用编号格式待规范",
}


def _card_time_seconds(card: Mapping[str, Any]) -> float | None:
    time_info = _mapping(card.get("time"))
    abs_time = time_info.get("absolute") or time_info.get("occurred_at")
    if abs_time:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(abs_time).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (TypeError, ValueError):
            pass
    return _offset(card)


def _card_dur(card: Mapping[str, Any]) -> float:
    for fact in card.get("facts") or []:
        if isinstance(fact, Mapping) and fact.get("path") == "time.duration_minutes":
            try:
                v = float(fact.get("value"))
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
    return 1.0


def _is_expense_card(card: Mapping[str, Any]) -> bool:
    """重要消费/支付/订单：必须独立保留，绝不并入工作或媒体主要活动。"""
    if card.get("transactions") or str(card.get("kind") or "").lower() == "expense":
        return True
    if _amount(card) is not None:
        return True
    facts = card.get("facts") or []
    if any(isinstance(f, Mapping) and "expense" in str(f.get("path") or "") for f in facts):
        return True
    title = str(card.get("title") or "")
    if any(k in title for k in ("¥", "付款", "到账", "收款", "点单", "购买", "买单")):
        return True
    return False


def _is_media_card(card: Mapping[str, Any]) -> bool:
    """媒体/视频/直播播放卡片。"""
    if _is_expense_card(card):
        return False
    kind = str(card.get("kind") or "").lower()
    if kind in ("media", "video", "livestream", "broadcast"):
        return True
    actor = _card_actor(card).lower()
    if actor == "media":
        return True
    title = str(card.get("title") or "")
    if any(k in title for k in ("短视频", "视频", "直播", "播放", "教程", "电台", "广播")):
        return True
    return False


def _is_work_card(card: Mapping[str, Any]) -> bool:
    """工作/编程/沟通卡片。"""
    if _is_expense_card(card) or _is_media_card(card):
        return False
    kind = str(card.get("kind") or "").lower()
    if kind in ("work", "coding", "communication"):
        return True
    title = str(card.get("title") or "")
    if any(k in title for k in ("工作", "编程", "开发", "代码", "终端", "会议", "沟通", "汇报", "调试")):
        return True
    return False


def _media_group_title(members: Sequence[Mapping[str, Any]]) -> str:
    """生成客观中性的媒体展示组标题，绝不冒认'本人看/刷视频'。"""
    titles = [str(m.get("title") or "") for m in members if m.get("title")]
    has_live = any("直播" in t for t in titles)
    has_tutorial = any("教程" in t for t in titles)
    
    if has_live:
        prefix = "播放直播"
    elif has_tutorial:
        prefix = "视频教程播放"
    else:
        prefix = "媒体播放"
    
    topics = []
    for t in titles:
        cleaned = re.sub(r'^(?:设备持续播放的|设备播放|播放的直播/短视频内容[：:]\s*|播放的|媒体内容[：:]\s*|观看/复述视频[：:]\s*|复述视频|复述)', '', t).strip()
        cleaned = re.split(r'[：:（(]', cleaned)[0].strip()
        if cleaned and cleaned not in topics and len(cleaned) <= 12:
            topics.append(cleaned)
    if topics:
        return f"{prefix} · {'、'.join(topics[:2])}"
    return prefix


def build_activity_presentation(
    cards: Sequence[Mapping[str, Any]],
    links: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把 cards + links 投影成"可读的活动摘要 + 未分组事实"。纯函数，不改入参。"""
    link_map = _mapping(links)
    members_map = _mapping(link_map.get("members"))
    meta_map = _mapping(link_map.get("member_meta"))

    card_by_id: dict[str, Mapping[str, Any]] = {}
    ordered_card_ids: list[str] = []
    for card in cards or []:
        if not isinstance(card, Mapping):
            continue
        event_id = _text(card.get("source_event_id"))
        if not event_id or event_id in card_by_id:
            continue
        card_by_id[event_id] = card
        ordered_card_ids.append(event_id)

    groups: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    grouped_ids: set[str] = set()

    def add_conflict(activity_ref: str, event_ids: Sequence[str], conflicted: Sequence[str],
                     reason: str, source: str) -> None:
        clean_reason = _text(reason)
        payload = {
            "activity_ref": activity_ref,
            "event_ids": list(dict.fromkeys(event_ids)),
            "conflicted_event_ids": list(dict.fromkeys(conflicted)),
            "reason": clean_reason,
            "user_note": FRIENDLY_CONFLICT_MESSAGES.get(clean_reason, "相关记录待进一步核对，已完整保留"),
            "source": source,
        }
        key = (payload["activity_ref"], payload["reason"], tuple(payload["event_ids"]),
               tuple(payload["conflicted_event_ids"]))
        for existing in conflicts:
            if (existing["activity_ref"], existing["reason"], tuple(existing["event_ids"]),
                    tuple(existing["conflicted_event_ids"])) == key:
                return
        conflicts.append(payload)

    # ── 1) 显式声明的活动组 ──────────────────────────────────────────────
    for activity_ref, raw_ids in members_map.items():
        ref = _text(activity_ref)
        ids = [str(item) for item in (raw_ids or [])]
        unique_ids = list(dict.fromkeys(ids))
        if not ref or not unique_ids:
            continue
        meta = _mapping(meta_map.get(ref))
        conflicted = [str(item) for item in (meta.get("conflicted") or [])]
        if meta and conflicted:
            add_conflict(ref, unique_ids, conflicted, "declaration_conflict", "member_meta")
            continue
        if not meta:
            add_conflict(ref, unique_ids, [], "declaration_unverified", "member_meta")
            continue
        if meta.get("declaration_consistent") is False:
            add_conflict(ref, unique_ids, conflicted or unique_ids, "declaration_conflict", "member_meta")
            continue
        member_cards = [card_by_id[event_id] for event_id in unique_ids if event_id in card_by_id]
        if len(member_cards) < 2:
            continue
        members = [_member_view(card) for card in member_cards]
        groups.append({
            "activity_ref": ref,
            "basis": {
                "kind": "explicit_activity_declaration",
                "label": "显式声明 · 未核实",
                "activity_entity_verified": bool(meta.get("activity_entity_verified")),
            },
            "member_event_ids": [member["event_id"] for member in members],
            "members": members,
            "summary": _summary(members),
            "result_summary": _result_summary(members),
            "steps": _steps(members),
            "compact": True,
        })
        grouped_ids.update(member["event_id"] for member in members)

    # ── 2) 上游冲突（links.conflicts）────────────────────────────────────
    for item in link_map.get("conflicts") or []:
        if not isinstance(item, Mapping):
            continue
        add_conflict(
            _text(item.get("activity_ref")),
            [str(x) for x in (item.get("event_ids") or [])],
            [str(x) for x in (item.get("conflicted_event_ids") or [])],
            _text(item.get("reason")) or "upstream_conflict",
            "links.conflicts",
        )

    # ── 3) 无显式声明时：具体餐饮实体 + 同小时 + 同主体 ───────────────────
    if not groups:
        buckets: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
        for event_id in ordered_card_ids:
            card = card_by_id[event_id]
            entity = _card_entity(card)
            if not entity:
                continue
            hour = _local_hour(_mapping(card.get("time")).get("occurred_at")
                               or _mapping(card.get("time")).get("reported_at"))
            if not hour:
                continue
            buckets.setdefault((entity, hour, _card_actor(card)), []).append(card)
        for (entity, _hour, _actor), bucket in buckets.items():
            if len(bucket) < 2:
                continue
            members = [_member_view(card) for card in bucket]
            groups.append({
                "activity_ref": f"inferred:meal:{entity}",
                "inferred": True,
                "basis": {
                    "kind": "inferred_entity_group",
                    "inferred": True,
                    "verified": False,
                    "activity_entity_verified": False,
                    "entity": entity,
                    "label": "推断分组 · 未核实",
                },
                "member_event_ids": [member["event_id"] for member in members],
                "members": members,
                "summary": _summary(members),
                "result_summary": _result_summary(members),
                "steps": _steps(members),
                "compact": True,
            })
            grouped_ids.update(member["event_id"] for member in members)

    # ── 4) 主要活动与媒体活动组（复用读模型：共存主导突出，重要消费独立保留）─
    # CONTEXT.md: “主要活动：同一时段多种活动并存时，时间轴突出占主导的活动；
    # 长时间看直播夹杂少量工作发言可显示看直播，长时间工作沟通夹杂少量视频声音可显示工作。
    # 媒体活动组：相邻的媒体播放片段在时间轴上合并成一条可展开的展示组；
    # 重要事项与主要活动：购买等重要事项独立保留，不改变整个时段归属；
    # 不强按相似文本合并。”
    remaining_cards = [card_by_id[eid] for eid in ordered_card_ids if eid not in grouped_ids]
    
    # 按时间相邻性分簇（同一小时或前后间距 <= 1800秒）
    clusters: list[list[Mapping[str, Any]]] = []
    curr_cluster: list[Mapping[str, Any]] = []
    prev_t: float | None = None
    prev_h: str | None = None
    for c in remaining_cards:
        # 重要消费卡绝不参与合并，保持独立未分组
        if _is_expense_card(c):
            if curr_cluster:
                clusters.append(curr_cluster)
                curr_cluster = []
                prev_t, prev_h = None, None
            continue
        c_t = _card_time_seconds(c)
        c_h = _local_hour(_mapping(c.get("time")).get("occurred_at") or _mapping(c.get("time")).get("reported_at"))
        is_adjacent = False
        if c_h and prev_h and c_h == prev_h:
            is_adjacent = True
        elif c_t is not None and prev_t is not None and abs(c_t - prev_t) <= 1800.0:
            is_adjacent = True
        
        if is_adjacent and curr_cluster:
            curr_cluster.append(c)
            if c_t is not None: prev_t = c_t
            if c_h: prev_h = c_h
        else:
            if curr_cluster: clusters.append(curr_cluster)
            curr_cluster = [c]
            prev_t = c_t
            prev_h = c_h
    if curr_cluster:
        clusters.append(curr_cluster)

    for cluster in clusters:
        if len(cluster) < 2:
            continue
        c_media = [c for c in cluster if _is_media_card(c)]
        c_work = [c for c in cluster if _is_work_card(c)]
        
        # 4A. 共存时段主导活动（长直播夹短工作 / 长工作夹短媒体）
        if c_media and c_work:
            media_dur = sum(_card_dur(c) for c in c_media)
            work_dur = sum(_card_dur(c) for c in c_work)
            
            # 长直播夹短工作
            if media_dur >= 2 * work_dur or (len(c_media) >= 1 and media_dur >= 15 and work_dur <= 5):
                members = [_member_view(c) for c in (c_media + c_work)]
                g_title = _media_group_title(c_media)
                groups.append({
                    "activity_ref": f"primary_activity:media:{members[0]['event_id']}",
                    "inferred": True,
                    "basis": {
                        "kind": "primary_activity",
                        "dominant": "media",
                        "subordinate": "work",
                        "label": "主要活动 · 直播/媒体",
                        "verified": False,
                        "activity_entity_verified": False,
                    },
                    "member_event_ids": [m["event_id"] for m in members],
                    "members": members,
                    "summary": {
                        "title": g_title,
                        "member_count": len(members),
                        "titles": [m["title"] for m in members if m.get("title")],
                        "transaction_ids": [],
                        "transaction_count": 0,
                        "states": sorted({_text(m.get("state")) or "unknown" for m in members}),
                        "amounts_summed": False,
                    },
                    "result_summary": _result_summary(members),
                    "steps": _steps(members),
                    "compact": True,
                })
                grouped_ids.update(m["event_id"] for m in members)
                continue

            # 长工作夹短媒体
            elif work_dur >= 2 * media_dur or (len(c_work) >= 1 and work_dur >= 15 and media_dur <= 5):
                members = [_member_view(c) for c in (c_work + c_media)]
                g_title = _select_representative_title([_member_view(c) for c in c_work])
                groups.append({
                    "activity_ref": f"primary_activity:work:{members[0]['event_id']}",
                    "inferred": True,
                    "basis": {
                        "kind": "primary_activity",
                        "dominant": "work",
                        "subordinate": "media",
                        "label": "主要活动 · 工作",
                        "verified": False,
                        "activity_entity_verified": False,
                    },
                    "member_event_ids": [m["event_id"] for m in members],
                    "members": members,
                    "summary": {
                        "title": g_title,
                        "member_count": len(members),
                        "titles": [m["title"] for m in members if m.get("title")],
                        "transaction_ids": [],
                        "transaction_count": 0,
                        "states": sorted({_text(m.get("state")) or "unknown" for m in members}),
                        "amounts_summed": False,
                    },
                    "result_summary": _result_summary(members),
                    "steps": _steps(members),
                    "compact": True,
                })
                grouped_ids.update(m["event_id"] for m in members)
                continue

        # 4B. 邻接媒体片段展示组（不删事件，标题客观中性）
        if len(c_media) >= 2 and len(c_media) == len(cluster):
            members = [_member_view(c) for c in c_media]
            g_title = _media_group_title(c_media)
            groups.append({
                "activity_ref": f"media_group:{members[0]['event_id']}",
                "inferred": True,
                "basis": {
                    "kind": "media_group",
                    "label": "媒体活动组",
                    "verified": False,
                    "activity_entity_verified": False,
                },
                "member_event_ids": [m["event_id"] for m in members],
                "members": members,
                "summary": {
                    "title": g_title,
                    "member_count": len(members),
                    "titles": [m["title"] for m in members if m.get("title")],
                    "transaction_ids": [],
                    "transaction_count": 0,
                    "states": sorted({_text(m.get("state")) or "unknown" for m in members}),
                    "amounts_summed": False,
                },
                "result_summary": _result_summary(members),
                "steps": _steps(members),
                "compact": True,
            })
            grouped_ids.update(m["event_id"] for m in members)
            continue

    # ── 5) 未分组 + 交易覆盖 ─────────────────────────────────────────────
    ungrouped_ids = [event_id for event_id in ordered_card_ids if event_id not in grouped_ids]
    retained_transactions: dict[str, list[str]] = {}
    transactions_total = 0
    referenced = 0
    for event_id in ordered_card_ids:
        card = card_by_id[event_id]
        txn_ids = _member_view(card)["transaction_ids"] or _member_view(card)["ledger_event_ids"]
        transactions_total += len(txn_ids)
        if event_id in grouped_ids:
            referenced += len(txn_ids)
        elif txn_ids:
            retained_transactions[event_id] = txn_ids

    return {
        "schema": SCHEMA,
        "groups": groups,
        "conflicts": conflicts,
        "ungrouped_event_ids": ungrouped_ids,
        "retained_ungrouped_transactions": retained_transactions,
        "coverage": {
            "events_total": len(ordered_card_ids),
            "events_grouped": len(grouped_ids),
            "events_ungrouped": len(ungrouped_ids),
            "transactions_total": transactions_total,
            "transactions_referenced_in_groups": referenced,
            "transactions_retained_ungrouped": sum(len(v) for v in retained_transactions.values()),
            "transactions_dropped": 0,
        },
        "contract": {
            "schema": SCHEMA,
            "only_read": "只读 cards/links，不改 events / 金额 / 账本",
            "grouping": "显式声明优先；无声明时仅对具体餐饮实体做同小时同主体推断（标记 inferred）",
            "amounts": "不求和、不计算总额（amounts_summed=false）",
            "states": "不推断状态；沿用卡片状态，缺省 unknown",
            "transactions": "逐笔引用，未分组交易保留在 retained_ungrouped_transactions",
            "conflicts": "声明冲突/上游冲突只记录不成组，事件与交易全部保留",
        },
    }


# 购买会话摘要（展示候选，2026-10-25；与上面的活动展示投影共用本模块）

PURCHASE_SCHEMA = "purchase-session-v1"

ADDON_MARKERS = ("加个", "再加", "再来", "还要", "添一个", "又点")
#: 追加点单缺口的商品词（避免把"再加点水/还要赶路/再来一个西瓜"这类非购买句当缺口）
COMMODITY_HINTS = ("饭", "面", "薯条", "烤肠", "鸭", "鸡", "肉", "菜", "汤", "饮", "可乐", "奶茶", "咖啡",
                   "冰", "肠", "串", "饼", "包", "粉", "粥", "蛋", "果", "雪球", "清补凉")
BROADCAST_MARKERS = ("到账", "收款", "收钱", "播报", "收钱吧", "扫码支付", "二维码收款", "微信收款", "支付宝到账")
SESSION_WINDOW_SECONDS = 1800.0     # 同一次购买的上下文上限（30 分钟）
ADJACENT_SECONDS = 180.0            # "相邻上下文"更严的窗口


def _text(value: Any) -> str:
    return str(value or "")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _amount(event: Mapping[str, Any]) -> float | None:
    expense = _mapping(_mapping(event.get("data")).get("expense"))
    try:
        value = float(expense.get("amount"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _item(event: Mapping[str, Any]) -> str:
    data = _mapping(event.get("data"))
    for block in ("expense", "food", "meal"):
        name = _mapping(data.get(block)).get("item") or _mapping(data.get(block)).get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    title = _text(event.get("title"))
    return title.split(" (")[0].removeprefix("购买") or title


def _offset(event: Mapping[str, Any]) -> float | None:
    evidence = _mapping(event.get("evidence"))
    for key in ("start_offset_seconds", "relative_seconds"):
        value = evidence.get(key)
        if value is None:
            value = _mapping(event.get("time")).get(key)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _segment(event: Mapping[str, Any]) -> str:
    return _text(_mapping(event.get("evidence")).get("segment_id"))


def _absolute(event: Mapping[str, Any]) -> Any:
    time_info = _mapping(event.get("time"))
    return time_info.get("absolute") or _mapping(event.get("evidence")).get("reported_at")


def _gap_seconds(base: Mapping[str, Any], other: Mapping[str, Any]) -> float | None:
    """只有**同一 segment**（局部 offset 可比）或**双方都有绝对时间**时才计算间距。

    不同 segment 的局部 offset 不能当全局相邻 —— 否则会把两个无关片段误关联。
    """
    if _segment(base) and _segment(base) == _segment(other):
        a, b = _offset(base), _offset(other)
        return abs(a - b) if a is not None and b is not None else None
    a_abs, b_abs = _absolute(base), _absolute(other)
    if a_abs and b_abs:
        try:
            from datetime import datetime, timezone
            left = datetime.fromisoformat(str(a_abs).replace("Z", "+00:00"))
            right = datetime.fromisoformat(str(b_abs).replace("Z", "+00:00"))
            if left.tzinfo is None:
                left = left.replace(tzinfo=timezone.utc)
            if right.tzinfo is None:
                right = right.replace(tzinfo=timezone.utc)
            return abs((right - left).total_seconds())
        except (TypeError, ValueError):
            return None
    return None


def _recording(event: Mapping[str, Any]) -> str:
    return _text(_mapping(event.get("evidence")).get("recording_id"))


def _quote(event: Mapping[str, Any]) -> str:
    return _text(_mapping(event.get("evidence")).get("quote")) or _text(event.get("title"))


def _is_addon(event: Mapping[str, Any]) -> bool:
    return any(marker in _quote(event) for marker in ADDON_MARKERS)


def _expense_verdict(event: Mapping[str, Any]) -> str:
    """复用 expense_policy 的同源判据：这条事件在账本里算不算"已认可的订单"。

    返回 `counted`（进交易）/ `pending_broadcast`（收款播报待确认）/ `not_expense`。
    会话层不再自己用"播报词表"一刀切 —— 报价证据被账本认可时就按订单处理。
    """
    try:
        from life_logger.expense_policy import build_expense_ledger

        ledger = build_expense_ledger([event])
    except Exception:
        return "unknown"
    event_id = _text(event.get("event_id"))
    if event_id in {_text(t.get("event_id")) for t in (ledger.get("transactions") or [])}:
        return "counted"
    if event_id in {_text(p.get("event_id")) for p in (ledger.get("pending_broadcast") or [])}:
        return "pending_broadcast"

    # 完整 quote 混有播报句时，账本会判 not_expense；但 Q414 合同认可"点单/报价子句"这一独立证据。
    # 这里把**逐字点单子句**作为 quote 再问一次同一条 expense_policy 规则（不新增判据、不放松 POS）：
    related = _mapping(event.get("evidence")).get("related_quotes") or []
    # 证据必须**可定位**：子句自身带 segment_id 或 absolute 才可作为独立证据（缺坐标 → 不采信）
    order_quotes = [
        r.get("quote") for r in related
        if isinstance(r, Mapping)
        and str(r.get("role")) == "order"
        and str(r.get("source_label") or "self") == "self"
        and r.get("quote")
        and (r.get("segment_id") or r.get("absolute"))
    ]
    if order_quotes:
        probe = dict(event)
        probe_evidence = dict(_mapping(event.get("evidence")))
        probe_evidence["quote"] = " ".join(str(q) for q in order_quotes)[:240]
        probe["evidence"] = probe_evidence
        try:
            from life_logger.expense_policy import build_expense_ledger as _ledger

            probe_ledger = _ledger([probe])
        except Exception:
            return "not_expense"
        if event_id in {_text(t.get("event_id")) for t in (probe_ledger.get("transactions") or [])}:
            return "counted"
    return "not_expense"


def _is_broadcast(event: Mapping[str, Any]) -> bool:
    return any(marker in _quote(event) for marker in BROADCAST_MARKERS)


def _session_id(recording: str, first_event_id: str) -> str:
    return "ps_" + hashlib.sha256(f"{recording}|{first_event_id}".encode()).hexdigest()[:12]


def _exact_event_key(event: Mapping[str, Any]) -> str:
    ek = _text(event.get("event_key")).strip()
    if ek:
        return ek
    evidence = _mapping(event.get("evidence"))
    rec = _text(evidence.get("recording_id")).strip()
    seg = _text(evidence.get("segment_id")).strip()
    start = evidence.get("start_offset_seconds")
    end = evidence.get("end_offset_seconds")
    if rec and seg and start is not None and end is not None:
        return f"coord:{rec}:{seg}:{start}:{end}"
    eid = _text(event.get("event_id")).strip()
    return f"event:{eid}" if eid else f"id:{id(event)}"


def _consolidate_exact_projections(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按相同物理证据坐标（event.event_key）合并同一原始事件的双重视角投影（如 expense 与 food）。

    保留全部原始 event_id 于 linked_event_ids 中，挂载金额与品项/分量，
    消除同一物理事件在展示层生成的冗余会话。
    禁止基于 quote 文本或金额暴力去重。
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    order_keys: list[str] = []
    for ev in events:
        key = _exact_event_key(ev)
        if key not in grouped:
            grouped[key] = []
            order_keys.append(key)
        grouped[key].append(ev)

    consolidated: list[dict[str, Any]] = []
    for key in order_keys:
        ev_list = grouped[key]
        if len(ev_list) == 1:
            consolidated.append(dict(ev_list[0]))
            continue
        # 存在完全相同的物理证据坐标（同一原始事件的多维投影）
        # 优先选择具有明确金额/消费载荷的为主实体
        primary = next((e for e in ev_list if _amount(e) is not None), ev_list[0])
        merged = dict(primary)
        merged_data = dict(_mapping(primary.get("data")))
        for other in ev_list:
            if other is primary:
                continue
            for block, val in _mapping(other.get("data")).items():
                if block not in merged_data or not merged_data[block]:
                    merged_data[block] = val
                elif isinstance(merged_data[block], Mapping) and isinstance(val, Mapping):
                    merged_block = dict(merged_data[block])
                    for k, v in val.items():
                        if (k not in merged_block or merged_block[k] is None) and v is not None:
                            merged_block[k] = v
                    merged_data[block] = merged_block
        merged["data"] = merged_data
        linked_ids = []
        for ev in ev_list:
            eid = _text(ev.get("event_id")).strip()
            if eid and eid not in linked_ids:
                linked_ids.append(eid)
        merged["linked_event_ids"] = linked_ids
        consolidated.append(merged)
    return consolidated


def build_purchase_session_summaries(
    events: Iterable[Mapping[str, Any]],
    *,
    transcript_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """把"同一次购买的多个商品"组织成**一张展示摘要**；不做账目合并。"""
    raw_items = [event for event in events if isinstance(event, Mapping)]
    items = _consolidate_exact_projections(raw_items)
    orders: list[Mapping[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    pending_broadcast: list[dict[str, Any]] = []

    for event in items:
        # 未来计划、取消状态、以及非账本认可订单过滤
        status = str(event.get("status") or _mapping(event.get("data")).get("status") or "").lower()
        if status in {"planned", "plan", "planning", "cancelled", "canceled", "aborted", "future"}:
            skipped.append({"event_id": _text(event.get("event_id")), "reason": "future_plan_not_purchase"})
            continue

        verdict = _expense_verdict(event)
        if verdict == "counted":
            # 账本已认可为订单（即使 quote 里混有播报句）→ 正常进入会话候选
            orders.append(event)
            continue
        if _is_broadcast(event) or verdict == "pending_broadcast":
            reason = "broadcast_not_purchase"
            if _is_addon(event):
                pending_broadcast.append({
                    "event_id": _text(event.get("event_id")),
                    "amount": _amount(event),
                    "reason": "addon_quote_mixes_pos_broadcast",
                    "note": "完整 quote 含播报句 → 现行 policy 归 B 档 pending；不并入会话、不生成交易",
                    "quote_sha": hashlib.sha256(_quote(event).encode()).hexdigest()[:12],
                })
            skipped.append({"event_id": _text(event.get("event_id")), "reason": reason})
            continue
        if _amount(event) is None:
            skipped.append({"event_id": _text(event.get("event_id")), "reason": "no_amount"})
            continue
        if verdict == "not_expense":
            skipped.append({"event_id": _text(event.get("event_id")), "reason": "not_an_order"})
            continue
        orders.append(event)

    # 只有"基数订单 + 显式追加"或"同录音相邻上下文"才归入同一会话
    sessions: list[dict[str, Any]] = []
    used: set[int] = set()
    for index, base in enumerate(orders):
        if index in used:
            continue
        members = [base]
        used.add(index)
        base_offset = _offset(base)
        base_rec = _recording(base)
        links: list[dict[str, Any]] = []
        for other_index, other in enumerate(orders):
            if other_index in used or other is base:
                continue
            if _recording(other) != base_rec:
                continue                                  # 跨录音：不关联
            gap = _gap_seconds(base, other)
            if gap is not None and gap > SESSION_WINDOW_SECONDS:
                continue
            # 【2026-10-25 返工】同录音 ≤180s 相邻**不是**活动关联证据（用户明确边界）。
            # 只保留**显式追加关系**，并且要求：锚点可定位（有 offset/segment）、同主体 self、
            # 同录音、在窗口内。同录音但主体未知的一律不当同一个人。
            # 锚点可定位：有段内 offset **或**有绝对时间（二者任一即可定位同一上下文）
            anchor_locatable = base_offset is not None or _absolute(base) is not None
            same_actor = (
                _text(_mapping(base.get("evidence")).get("source_label")).strip().lower() == "self"
                and _text(_mapping(other.get("evidence")).get("source_label")).strip().lower() == "self"
            )
            explicit_addon = (
                _is_addon(other)
                and anchor_locatable
                and same_actor
                and gap is not None
                and gap <= SESSION_WINDOW_SECONDS
            )
            if not explicit_addon:
                skipped.append({
                    "event_id": _text(other.get("event_id")),
                    "reason": "no_addon_link",
                    "note": "同词/同小时/同金额不足以关联",
                })
                continue
            members.append(other)
            used.add(other_index)
            links.append({
                "from_event_id": _text(base.get("event_id")),
                "to_event_id": _text(other.get("event_id")),
                "basis": "explicit_addon",
                "gap_seconds": gap,
            })

        session_items = []
        member_ids: list[str] = []
        for member in members:
            for eid in (member.get("linked_event_ids") or [_text(member.get("event_id"))]):
                if eid and eid not in member_ids:
                    member_ids.append(eid)
            time_info = _mapping(member.get("time"))
            food_info = _mapping(_mapping(member.get("data")).get("food"))
            item_dict = {
                "time": {
                    "occurred_at": time_info.get("absolute"),
                    "reported_at": _mapping(member.get("evidence")).get("reported_at"),
                    "precision": _text(time_info.get("precision")) or "unknown",
                },
                "event_id": _text(member.get("event_id")),
                "item": _item(member),
                "amount": _amount(member),
                "currency": _text(_mapping(_mapping(member.get("data")).get("expense")).get("currency")) or "CNY",
                "quote_sha": hashlib.sha256(_quote(member).encode()).hexdigest()[:12],
                "transaction_refs": [
                    {"namespace": key, "id": _text(_mapping(member.get("data")).get(key))}
                    for key in ("transaction_id", "payment_id", "receipt_id")
                    if _text(_mapping(member.get("data")).get(key))
                ],
                "payment_status": "unknown",       # 不猜"已付款"
            }
            if food_info.get("portion"):
                item_dict["portion"] = str(food_info["portion"])
            if member.get("linked_event_ids"):
                item_dict["linked_event_ids"] = list(member["linked_event_ids"])
            session_items.append(item_dict)
        sessions.append({
            "schema": PURCHASE_SCHEMA,
            "session_id": _session_id(base_rec, _text(base.get("event_id"))),
            "member_ids": member_ids,
            "state": "unknown" if any(item["payment_status"] == "unknown" for item in session_items) else "confirmed",
            "state_note": "不判断是否已付款；交易逐笔引用",
            "recording_id": base_rec,
            "window_seconds": SESSION_WINDOW_SECONDS,
            "items": session_items,
            "addon_links": links,
            # 【返工】不提供"展示合计"，避免被当成正式账本；金额只能逐项列出。
            "amounts_listed_individually": [item["amount"] for item in session_items],
            "not_a_ledger_total": True,
            "status": "candidate",
            "caveats": sorted({
                *(["包含显式追加点单"] if links else []),
                "支付是否完成未确认",
                "不改账、不重算历史",
            }),
        })

    # 转写回放：识别"说了追加但 canonical 里没有对应商品"的缺口（不假称已入账）
    pending: list[dict[str, Any]] = []
    for row in (transcript_rows or []):
        text = _text(_mapping(row).get("text"))
        if not text or not any(marker in text for marker in ADDON_MARKERS):
            continue
        if _text(_mapping(row).get("source_label")).strip().lower() != "self":
            continue
        # 必须是"追加词 + 短距离内跟商品词"（"我加个烤肠"、"再加一份薯条"），
        # 否则"还要赶路""再加点水""巧克力…还要强化…"都不算缺口。
        tight = re.search(
            r"(?:加个|再加|再来|还要|添一个|又点)[^，。！？,]{0,6}(" + "|".join(COMMODITY_HINTS) + r")",
            text,
        )
        if not tight:
            continue
        matched = [event for event in items if _item(event) and _item(event) in text]
        if not matched:
            pending.append({
                "source_id": _text(_mapping(row).get("source_id")),
                "quote": text[:120],
                "recording_id": _text(_mapping(row).get("recording_id")),
                "segment_id": _text(_mapping(row).get("segment_id")),
                "reason": "addon_mentioned_but_no_canonical_item",
                "status": "pending_candidate_not_booked",
            })

    return {
        "schema": PURCHASE_SCHEMA,
        "sessions": sessions,
        "pending_candidates": pending,
        "pending_broadcast_addons": pending_broadcast,
        "skipped": skipped,
        "stats": {
            "orders": len(orders),
            "sessions": len(sessions),
            "multi_item_sessions": sum(1 for s in sessions if len(s["items"]) > 1),
            "pending_candidates": len(pending),
        },
        "contract": {
            "link_evidence": "仅显式追加点单（加个/再加/再来/还要）＋锚点可定位＋同主体 self＋同录音＋窗口内",
            "not_evidence": "同词组/同小时/同金额/**同录音相邻时间**都不构成关联；同录音未知主体不当同一个人",
            "no_total": "不提供合计；金额逐项列出（展示候选 ≠ 正式账本）",
            "transactions": "逐笔保留，不合并、不去重、不求和入账",
            "payment": "不判断是否已付款；播报/收款仅作证据，不因邻近认作本人",
            "missing_item": "canonical 无对应商品时只报 pending_candidate，不假称已入账",
        },
    }
