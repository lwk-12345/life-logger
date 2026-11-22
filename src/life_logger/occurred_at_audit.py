"""occurred_at 产出校验：把「照抄发声时间」挡在写盘之前（2026-11-04 Q822）。

为什么需要
----------
生产 prompt 明写：

    「无法精确算出确切历史钟点时 occurred_at 必须严格填 null，严禁照抄发声时间」

但实测 `activity-timeline/jev/2026-11-02.json`：

    826 个 item，392 个 occurred_at=null，其余 **434 个全部与 spoken_at 秒级完全相同**

真实"发生时刻"与"说话时刻"在秒级完全相等、且 100% 命中，概率上只能是照抄。
回填代码 `activity_window_state.py:594` 只在 `not old_occ` 时写入 —— 逻辑没错，
**违规发生在模型输出阶段**。所以靠"模型自律"不成立，必须在写盘前加机械校验。

设计要点（三条都不能少）
------------------------
1. **不静默改数据**：每条被处理的 item 都写 `occurred_at_audit = {violation, reason, cleared}`，
   被清空的原文留在 `occurred_at_cleared` —— 可审计、可回滚、可统计。
2. **不一刀切**：现场实时动作（点餐/称重/付款/播报口播的当下动作）确实可能
   发生时间≈说话时间。用文本线索分档，而不是把所有相等都判死。
3. **纯函数 + 幂等**：`audit_item()` 不碰 IO；重复跑不会二次清空（已清空的项
   原样返回，audit 结果不变）。

规则（按优先级，先命中先定档）
------------------------------
===========  ==========================================  ============  ==========================
档位          判据                                         是否清空      reason
===========  ==========================================  ============  ==========================
not_equal    occurred≠spoken（秒级）                       保留          ``timestamps_differ``
no_occurred  occurred_at 为空                              保留          ``already_null``
media_replay 命中媒体体裁词（播报/解说/念唱/转述/背景播放…）    **清空**      ``media_replay``
retrospective 命中回顾词（昨天/上次/之前/那时候/我记得…）       **清空**      ``retrospective_cue``
live_action  命中现场词（现在/刚刚/正在/这是/我买了/今天…）      保留          ``live_action_cue``
no_anchor    以上都不命中                                  **清空**      ``no_temporal_anchor``
===========  ==========================================  ============  ==========================

`no_anchor` 选"清空"而不是"保留"，是为了对齐 prompt 契约：模型拿不出证据就该填 null；
实测 100% 相等的分布也说明它并没有真的在算。这条最保守，也最容易通过统计核对。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable, Mapping

SCHEMA = "occurred-at-audit-v1"

#: 媒体体裁词：内容是"被播放/被转述"的，其发生时刻早于播放时刻，绝不能等于 spoken_at。
MEDIA_CUES = (
    "播报", "解说", "念唱", "演唱", "跟唱", "歌词", "转述", "背景播放", "外放",
    "节目", "朗读", "口播", "新闻", "报道", "通报", "截图显示", "视频称", "广告",
)
#: 回顾词：明确指向"过去某时刻"，说话时刻必然不等于发生时刻。
RETRO_CUES = (
    "昨天", "昨晚", "前天", "上次", "之前", "以前", "那时候", "当时", "我记得",
    "曾经", "那年", "去年", "早年", "前几天", "上个月", "上周", "早上", "上午",
    "下午", "晚上", "刚才那会儿", "那时",
)
#: 现场词：说话与发生可能真在同一刻（点餐/称重/递东西/付款…）。
LIVE_CUES = (
    "现在", "刚刚", "刚才", "正在", "这是", "我买了", "今天", "目前", "此刻",
    "这会儿", "马上", "来一份", "点单", "要一份",
)

_MEDIA_RE = re.compile("|".join(MEDIA_CUES))
_RETRO_RE = re.compile("|".join(RETRO_CUES))
_LIVE_RE = re.compile("|".join(LIVE_CUES))

#: 结果枚举（写进 audit.reason，便于统计与前端筛选）
REASON_DIFFER = "timestamps_differ"
REASON_NULL = "already_null"
REASON_MEDIA = "media_replay"
REASON_RETRO = "retrospective_cue"
REASON_LIVE = "live_action_cue"
REASON_NO_ANCHOR = "no_temporal_anchor"

CLEARING_REASONS = frozenset({REASON_MEDIA, REASON_RETRO, REASON_NO_ANCHOR})


def _parse_second(value: Any) -> datetime | None:
    """把 ISO 时间解析到"秒"精度；解析不了返回 None（= 不可比，不做判断）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(microsecond=0)


def item_text(item: Mapping[str, Any]) -> str:
    """参与线索判断的文本：action + 所有 quote（不掺标题，避免被无关词带偏）。"""
    parts: list[str] = []
    action = item.get("action")
    if isinstance(action, str) and action.strip():
        parts.append(action.strip())
    for evidence in item.get("evidence") or []:
        if isinstance(evidence, Mapping):
            quote = evidence.get("quote")
            if isinstance(quote, str) and quote.strip():
                parts.append(quote.strip())
    return "\n".join(parts)


def classify_text(text: str) -> str:
    """仅按文本判档（供统计/单测直接调用）。优先级：媒体 > 回顾 > 现场。"""
    if not text:
        return REASON_NO_ANCHOR
    if _MEDIA_RE.search(text):
        return REASON_MEDIA
    if _RETRO_RE.search(text):
        return REASON_RETRO
    if _LIVE_RE.search(text):
        return REASON_LIVE
    return REASON_NO_ANCHOR


def audit_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """只判定、不改数据：返回 ``{"violation","reason","cleared"}``（外加证据片段）。

    * ``violation`` = 该 item 的 occurred_at 是否属于"照抄 spoken_at"这一类；
    * ``cleared``   = 建议/已经清空（与 violation 不等价：live_action 是 violation=False）。
    """
    occurred = item.get("occurred_at")
    spoken = item.get("spoken_at")
    occ_dt = _parse_second(occurred)
    spoken_dt = _parse_second(spoken)

    if occ_dt is None:
        return {"violation": False, "reason": REASON_NULL, "cleared": False,
                "schema": SCHEMA}
    if spoken_dt is None or occ_dt != spoken_dt:
        return {"violation": False, "reason": REASON_DIFFER, "cleared": False,
                "schema": SCHEMA}

    reason = classify_text(item_text(item))
    cleared = reason in CLEARING_REASONS
    return {
        "schema": SCHEMA,
        "violation": cleared,          # 留着 live_action 不算违规（是真发生时刻的候选）
        "reason": reason,
        "cleared": cleared,
        "matched_cue": reason,
    }


def apply_audit(
    activity: Mapping[str, Any],
    *,
    clear: bool = True,
) -> dict[str, Any]:
    """对一张活动就地注解（可选清空），返回统计。幂等：重复调用结果不变。

    ``clear=False`` 只写审计字段、不动 occurred_at（dry-run 用这个）。
    """
    stats = {"items": 0, "violation": 0, "cleared": 0, "kept": 0,
             "already_null": 0, "equal_to_spoken": 0, "differ": 0,
             "by_reason": {}, "already_audited": 0}
    items = activity.get("items")
    if not isinstance(items, list):
        return stats
    for item in items:
        if not isinstance(item, dict):
            continue
        stats["items"] += 1
        prior = item.get("occurred_at_audit")
        if isinstance(prior, Mapping) and prior.get("schema") == SCHEMA:
            stats["already_audited"] += 1
            result = dict(prior)
        else:
            result = audit_item(item)
            item["occurred_at_audit"] = result
        reason = str(result.get("reason") or "")
        stats["by_reason"][reason] = stats["by_reason"].get(reason, 0) + 1
        if result.get("violation"):
            stats["violation"] += 1
        if reason == REASON_NULL:
            stats["already_null"] += 1
        elif reason == REASON_DIFFER:
            stats["differ"] += 1
        else:
            stats["equal_to_spoken"] += 1
        if result.get("cleared"):
            stats["cleared"] += 1
            if clear and item.get("occurred_at") is not None:
                # 原文进 occurred_at_cleared，可回滚、可统计；不覆盖已有值
                item.setdefault("occurred_at_cleared", item.get("occurred_at"))
                item["occurred_at"] = None
        elif reason != REASON_NULL:
            stats["kept"] += 1
    return stats


def audit_activity_card(activity: Mapping[str, Any]) -> dict[str, Any]:
    """卡片级只做**标注**，不清空。

    为什么不清：``occurrence_start`` 决定这张卡在时间轴上的位置，清掉会让卡片消失；
    而卡片级"与发言时间接近"本身可能成立（对话类活动确实就是当时）。
    这里只把事实摆出来，供人工/前端决定怎么显示。
    """
    start = _parse_second(activity.get("occurrence_start"))
    if start is None:
        return {"schema": SCHEMA, "comparable": False, "same_minute_items": 0, "items": 0}
    same = total = 0
    for item in activity.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        spoken = _parse_second(item.get("spoken_at"))
        if spoken is None:
            continue
        total += 1
        if abs((spoken - start).total_seconds()) <= 60:
            same += 1
    return {
        "schema": SCHEMA,
        "comparable": True,
        "occurrence_start": activity.get("occurrence_start"),
        "items": total,
        "same_minute_items": same,
        "suspicious": bool(total and same == total),
    }


def audit_payload(payload: Mapping[str, Any], *, clear: bool = True) -> dict[str, Any]:
    """整份 payload 的审计入口（publish_day 调用）。返回汇总，供日志/元数据。"""
    totals = {"activities": 0, "items": 0, "violation": 0, "cleared": 0, "kept": 0,
              "already_null": 0, "equal_to_spoken": 0, "differ": 0,
              "cards_suspicious": 0, "by_reason": {}}
    for key in ("activities", "unassociated_items"):
        for activity in payload.get(key) or []:
            if not isinstance(activity, dict):
                continue
            totals["activities"] += 1
            stats = apply_audit(activity, clear=clear)
            totals["items"] += stats["items"]
            totals["violation"] += stats["violation"]
            totals["cleared"] += stats["cleared"]
            totals["kept"] += stats["kept"]
            totals["already_null"] += stats["already_null"]
            totals["equal_to_spoken"] += stats["equal_to_spoken"]
            totals["differ"] += stats["differ"]
            for reason, count in stats["by_reason"].items():
                totals["by_reason"][reason] = totals["by_reason"].get(reason, 0) + count
            card = audit_activity_card(activity)
            if card.get("comparable"):
                activity["occurrence_start_audit"] = card
                if card.get("suspicious"):
                    totals["cards_suspicious"] += 1
    totals["clear_applied"] = bool(clear)
    totals["schema"] = SCHEMA
    return totals


__all__ = [
    "SCHEMA", "MEDIA_CUES", "RETRO_CUES", "LIVE_CUES", "CLEARING_REASONS",
    "REASON_DIFFER", "REASON_NULL", "REASON_MEDIA", "REASON_RETRO",
    "REASON_LIVE", "REASON_NO_ANCHOR",
    "item_text", "classify_text", "audit_item", "apply_audit",
    "audit_activity_card", "audit_payload",
]
