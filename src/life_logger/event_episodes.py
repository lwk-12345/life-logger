"""事件粒度合并（episode 分组）——纯函数，不写文件、不改事件。

背景与规则见 `work/event-granularity-20261023/REPORT.md`。

这个模块只做一件事：把**同一次讲述里被切碎的若干事件**归成一个 episode，
让前端可以「一场讨论一张卡」。它不修改任何事件字典，不重算金额、时间或标题，
也不写盘；调用方拿到的 `events` 仍是原对象。

三条硬约束（对应任务书第 2、3 条）：

1. **不伪造时间**：只用事件里已有的事件发生时间（`time.absolute`，且不是
   `2025-01-01` 占位值）或讲述时间（`evidence.reported_at`）算时间差；
   两者都没有时，只允许用「同一 segment」这一结构关系归组，绝不拿
   `segment_index × 60s` 反推发生时间。
2. **红线不合并**：金额、待办/计划、不同对象、不同讲述人、跨 activity 族。
3. **canonical ID 与 provenance 全保留**：输入里的每个 `event_id` 都能在
   `alias` 里找到归属；被 R3 判为重复的 session 级事件也会保留在
   `dropped` 里并指向存活的 episode，而不是消失。

用法::

    from life_logger.event_episodes import build_episodes
    result = build_episodes(events)
    result["episodes"]   # [{"episode_id", "event_ids", "primary_event_id", ...}]
    result["alias"]      # {event_id: episode_id}
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

#: 同一 segment 内直接合并（一段音频约 60s，天然就在窗口内）。
SAME_SEGMENT_WINDOW_SECONDS = 60.0
#: 相邻 segment 续句合并的窗口。
ADJACENT_SEGMENT_WINDOW_SECONDS = 120.0
#: 相邻 segment 续句合并的文本相似度下限。
CONTINUATION_JACCARD = 0.5
#: R3 管线去重的相似度下限。
DEDUPE_SIMILARITY = 0.6
#: R3 去重时允许「短引文被长引文完全包含」的最小长度。
CONTAINMENT_MIN_CHARS = 8
#: R4 视为碎片的长度上限。
FRAGMENT_MAX_CHARS = 12

#: 已知的占位日期：这些时间不可用于任何时间差计算。
PLACEHOLDER_DATES = ("2025-01-01",)

#: 计划/待办类词：命中即认为这条事件是「计划」而不是「已发生」。
PLAN_MARKERS = (
    "明天", "后天", "凌晨", "等下", "等会", "一会儿", "计划", "准备", "打算",
    "要去", "想做", "待办", "todo",
)

#: 碎片开头：这些以指代/连接词开头的短句必须挂到父事件上。
FRAGMENT_PREFIXES = ("然后", "他", "她", "它", "这", "那", "对", "而且", "所以")

#: activity_type / category → 语义族。
# 注意：不能用 `startswith("work")`，否则 `workout` 会被误判成工作事件
# （样本里正好撞上：健身房力量训练被判成 work，导致 R3 去重失效）。
_ACTIVITY_FAMILY = {
    "work": "work",
    "work_communication": "work",
    "work_discussion": "work",
    "work_information": "work",
    "work_issue": "work",
    "project_review": "work",
    "meeting": "work",
    "workout": "exercise",
    "strength_training": "exercise",
    "exercise": "exercise",
    "meal": "meal",
    "food": "meal",
    "diet": "meal",
    "task": "task",
    "todo": "task",
    "plan": "task",
}

_CATEGORY_FAMILY = {
    "work": "work",
    "work_communication": "work",
    "exercise": "exercise",
    "food": "meal",
    "diet": "meal",
    "meal": "meal",
    "todo": "task",
    "task": "task",
}


def _text(value: Any) -> str:
    return str(value or "")


def _normalized(text: str) -> str:
    """去掉标点/语气词，只留下用于相似度比较的实词序列。"""
    return re.sub(r"[\s，。,.！!？?、；;：:…「」“”\"'（）()]+", "", text or "")


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def jaccard(a: str, b: str) -> float:
    """字符二元组 Jaccard。空串相似度为 0。"""
    left, right = _bigrams(_normalized(a)), _bigrams(_normalized(b))
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _is_contained(short: str, long: str, *, min_chars: int | None = None) -> bool:
    s, l = _normalized(short), _normalized(long)
    floor = CONTAINMENT_MIN_CHARS if min_chars is None else min_chars
    if len(s) < floor or len(l) <= len(s):
        return False
    return s in l


def similarity(a: str, b: str) -> float:
    """引文相似度：Jaccard 与「短句被长句完全包含」取大者。"""
    score = jaccard(a, b)
    if _is_contained(a, b) or _is_contained(b, a):
        score = max(score, DEDUPE_SIMILARITY)
    return score


def _family(event: Mapping[str, Any]) -> str:
    activity = _text(event.get("activity_type")).casefold()
    if activity in _ACTIVITY_FAMILY:
        return _ACTIVITY_FAMILY[activity]
    if activity.startswith("work_"):
        return "work"
    if activity.startswith("exercise"):
        return "exercise"
    categories = [str(item).casefold() for item in (event.get("categories") or [])]
    for category in categories:
        family = _CATEGORY_FAMILY.get(category)
        if family:
            return family
    return activity or "other"


def _evidence(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("evidence")
    return value if isinstance(value, Mapping) else {}


def _quote(event: Mapping[str, Any]) -> str:
    return _text(_evidence(event).get("quote") or event.get("quote"))


def _amount(event: Mapping[str, Any]) -> float | None:
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    expense = data.get("expense") if isinstance(data.get("expense"), Mapping) else {}
    for raw in (expense.get("amount"), _evidence(event).get("proposed_expense_amount")):
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value:
            return value
    categories = [str(item).casefold() for item in (event.get("categories") or [])]
    if "expense" in categories and "amount" not in expense:
        # 有消费分类但没金额：依然按「涉及钱」处理，避免把两笔消费并成一条。
        return 0.0
    return None


def _object_key(event: Mapping[str, Any]) -> str:
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    for key in ("food", "expense"):
        block = data.get(key)
        if not isinstance(block, Mapping):
            continue
        item = _text(block.get("item")).strip()
        if item:
            return f"{key}:{item}"
        items = block.get("items")
        if isinstance(items, list):
            names = [
                _text(entry.get("name") if isinstance(entry, Mapping) else entry).strip()
                for entry in items
            ]
            names = [name for name in names if name]
            if names:
                return f"{key}:{','.join(sorted(names))}"
    return ""


def _is_plan(event: Mapping[str, Any]) -> bool:
    categories = [str(item).casefold() for item in (event.get("categories") or [])]
    if "todo" in categories or "task" in categories:
        return True
    if _text(event.get("activity_type")).casefold() in {"task", "todo", "plan"}:
        return True
    haystack = f"{_text(event.get('title'))} {_quote(event)}"
    return any(marker in haystack for marker in PLAN_MARKERS)


def _speaker(event: Mapping[str, Any]) -> str:
    return _text(_evidence(event).get("source_label") or event.get("source_label"))


def _segment(event: Mapping[str, Any]) -> tuple[str, int] | None:
    """返回 (block, index)，例如 watch_000223_000002 -> ("000223", 2)。"""
    raw = _text(_evidence(event).get("segment_id") or event.get("segment_id"))
    match = re.search(r"_(\d{6})_(\d{6})$", raw)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _event_time(event: Mapping[str, Any]) -> datetime | None:
    """取可用于「时间差」的时间：优先发生时间，其次讲述时间。

    占位日期（2025-01-01）一律视为不可用 —— 任务书明确不拿它做任何判断。
    """
    time_info = event.get("time") if isinstance(event.get("time"), Mapping) else {}
    candidates = [time_info.get("absolute"), _evidence(event).get("reported_at")]
    for raw in candidates:
        text = _text(raw).strip()
        if not text or text[:10] in PLACEHOLDER_DATES:
            continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _is_fragment(event: Mapping[str, Any]) -> bool:
    text = _normalized(_quote(event))
    if not text or len(text) > FRAGMENT_MAX_CHARS:
        return False
    return text.startswith(FRAGMENT_PREFIXES)


def _red_line(a: Mapping[str, Any], b: Mapping[str, Any]) -> str | None:
    """R5：命中任一条即绝不合并，返回原因。"""
    amount_a, amount_b = _amount(a), _amount(b)
    if amount_a is not None or amount_b is not None:
        return "amount"
    if _is_plan(a) or _is_plan(b):
        return "plan_or_todo"
    object_a, object_b = _object_key(a), _object_key(b)
    if object_a and object_b and object_a != object_b:
        return "different_object"
    speaker_a, speaker_b = _speaker(a), _speaker(b)
    if speaker_a and speaker_b and speaker_a != speaker_b:
        return "different_speaker"
    if _family(a) != _family(b):
        return "cross_family"
    return None


def _gap_seconds(a: Mapping[str, Any], b: Mapping[str, Any]) -> float | None:
    time_a, time_b = _event_time(a), _event_time(b)
    if time_a is None or time_b is None:
        return None
    return abs((time_b - time_a).total_seconds())


def _can_merge(a: Mapping[str, Any], b: Mapping[str, Any]) -> tuple[bool, str]:
    """R1/R2/R4 判定。返回 (是否合并, 依据)。"""
    blocked = _red_line(a, b)
    if blocked:
        return False, f"red_line:{blocked}"

    seg_a, seg_b = _segment(a), _segment(b)
    if seg_a and seg_b and seg_a[0] == seg_b[0]:
        # 同一个 segment block = 同一段 60s 音频，天然落在 R1 窗口内。
        if _is_fragment(a) or _is_fragment(b):
            return True, "R4:fragment_same_segment"
        gap = _gap_seconds(a, b)
        if gap is None or gap <= SAME_SEGMENT_WINDOW_SECONDS:
            return True, "R1:same_segment"
        return False, "gap>60s"

    # 相邻 segment 续句：必须文本高度相似，且时间可证、≤120s。
    if seg_a and seg_b and seg_a[0] != seg_b[0]:
        try:
            distance = abs(int(seg_a[0]) - int(seg_b[0]))
        except ValueError:
            return False, "segment_unparsable"
        if distance != 1:
            return False, "segment_not_adjacent"
        if similarity(_quote(a), _quote(b)) < CONTINUATION_JACCARD:
            return False, "low_similarity"
        gap = _gap_seconds(a, b)
        if gap is not None and gap > ADJACENT_SEGMENT_WINDOW_SECONDS:
            return False, "gap>120s"
        return True, "R2:adjacent_continuation"

    # 没有 segment 信息（例如 session 级事件）：只允许文本去重，不做时间合并。
    return False, "no_segment"


def explain_merge(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """诊断用：这对事件能否合并、依据是什么。

    生产路径不需要调用它；它的存在是为了让测试能明确断言
    「是红线拦住的」而不是「碰巧因为缺 segment 才没合并」。
    """
    allowed, reason = _can_merge(a, b)
    return {"can_merge": allowed, "reason": reason}


def _episode_id(event_ids: Sequence[str]) -> str:
    digest = hashlib.sha1("|".join(sorted(event_ids)).encode("utf-8")).hexdigest()
    return f"ep_{digest[:12]}"


@dataclass
class _Group:
    members: list[Mapping[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def add(self, event: Mapping[str, Any], reason: str) -> None:
        self.members.append(event)
        if reason and reason not in self.reasons:
            self.reasons.append(reason)


def _dedupe_session_level(
    events: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    """R3：session 级事件若与 ctx 级同 session 高度重复，让位给 ctx 级。

    没有重复的 session 级事件**不删除**（03/04 这类独有事实必须保留），
    只标记 `date_placeholder`，交给下游决定要不要上时间轴。
    """
    ctx = [e for e in events if _text(e.get("event_id")).startswith("evt_ctx_")]
    kept: list[Mapping[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for event in events:
        eid = _text(event.get("event_id"))
        if not eid.startswith("evt_session_"):
            kept.append(event)
            continue
        best: tuple[float, str] | None = None
        for candidate in ctx:
            if _text(event.get("evidence", {}).get("recording_id")) != _text(
                candidate.get("evidence", {}).get("recording_id")
            ):
                continue
            quote_a, quote_b = _quote(event), _quote(candidate)
            score = similarity(quote_a, quote_b)
            # session 级事件是"整场粗摘要"，引文常常只是 ctx 引文的一个短片段；
            # 仅当同族且短引文被完整包含时才按重复处理（04/11 那种不同对象不满足）。
            contained = (
                _family(event) == _family(candidate)
                and (
                    _is_contained(quote_a, quote_b, min_chars=4)
                    or _is_contained(quote_b, quote_a, min_chars=4)
                )
            )
            if contained:
                score = max(score, DEDUPE_SIMILARITY)
            if score >= DEDUPE_SIMILARITY and (best is None or score > best[0]):
                best = (score, _text(candidate.get("event_id")))
        if best is None:
            kept.append(event)
            continue
        dropped.append(
            {
                "event_id": eid,
                "duplicate_of": best[1],
                "similarity": round(best[0], 3),
                "rule": "R3:session_ctx_duplicate",
                # 去重只代表「同一条事实的另一种表述」，原文细节必须留档：
                # 例如 session 级那句「不要等他六个全部结束工作了你再向我汇报」，
                # 在 ctx 级的引文里被截断了，不能因为去重就消失。
                "quote": _quote(event),
                "title": _text(event.get("title")),
                "activity_type": _text(event.get("activity_type")),
                "categories": list(event.get("categories") or []),
                "similarity_to_kept_quote": _quote(
                    next(
                        (c for c in ctx if _text(c.get("event_id")) == best[1]),
                        {},
                    )
                ),
                "provenance": _provenance(event),
            }
        )
    return kept, dropped


def _provenance(event: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _evidence(event)
    return {
        "event_id": _text(event.get("event_id")),
        "recording_id": _text(evidence.get("recording_id") or event.get("recording_id")),
        "source_id": _text(evidence.get("source_id") or event.get("source_id")),
        "segment_id": _text(evidence.get("segment_id") or event.get("segment_id")),
        "artifact_path": _text(event.get("artifact_path") or evidence.get("artifact_path")),
        "reported_at": _text(evidence.get("reported_at") or event.get("reported_at")),
    }


def _sort_key(event: Mapping[str, Any]) -> tuple[int, str, str]:
    moment = _event_time(event)
    seg = _segment(event)
    return (
        int(moment.timestamp()) if moment else 10**12,
        seg[0] if seg else "999999",
        _text(event.get("event_id")),
    )


def build_episodes(
    events: Iterable[Mapping[str, Any]],
    *,
    same_segment_window: float = SAME_SEGMENT_WINDOW_SECONDS,
    adjacent_segment_window: float = ADJACENT_SEGMENT_WINDOW_SECONDS,
    continuation_jaccard: float = CONTINUATION_JACCARD,
) -> dict[str, Any]:
    """把事件列表归并为 episode 分组。纯函数：不修改入参、不写文件。

    返回::

        {
          "episodes": [{episode_id, event_ids, primary_event_id, member_count,
                        reasons, provenance, time_unknown}],
          "alias": {event_id: episode_id},
          "dropped": [{event_id, duplicate_of, similarity, rule, provenance}],
          "stats": {input_events, kept_events, episodes, dropped, merged_groups},
        }
    """
    del same_segment_window, adjacent_segment_window, continuation_jaccard
    originals = list(events)
    kept, dropped = _dedupe_session_level(originals)

    ordered = sorted(kept, key=_sort_key)
    groups: list[_Group] = []
    used: set[int] = set()

    for index, event in enumerate(ordered):
        if index in used:
            continue
        group = _Group()
        group.add(event, "root")
        used.add(index)
        changed = True
        while changed:
            changed = False
            for other_index, other in enumerate(ordered):
                if other_index in used:
                    continue
                if any(_can_merge(member, other)[0] for member in group.members):
                    reason = next(
                        _can_merge(member, other)[1]
                        for member in group.members
                        if _can_merge(member, other)[0]
                    )
                    group.add(other, reason)
                    used.add(other_index)
                    changed = True
        groups.append(group)

    episodes = []
    alias: dict[str, str] = {}
    episode_by_event: dict[str, str] = {}
    for group in groups:
        ids = [_text(member.get("event_id")) for member in group.members]
        episode_id = _episode_id(ids)
        primary = _text(group.members[0].get("event_id"))
        for member in group.members:
            member_id = _text(member.get("event_id"))
            alias[member_id] = episode_id
            episode_by_event[member_id] = episode_id
        episodes.append(
            {
                "episode_id": episode_id,
                "event_ids": ids,
                "primary_event_id": primary,
                "member_count": len(ids),
                "reasons": group.reasons,
                "provenance": [_provenance(member) for member in group.members],
                "time_unknown": all(_event_time(member) is None for member in group.members),
            }
        )

    # alias 的唯一语义：event_id -> episode_id。
    # 被 R3 去重的事件也映射到「它归属的那个 episode」，绝不在这里塞 event_id，
    # 否则下游拿到 alias 值后无法判断该按 episode 还是按 event 二次解析。
    for item in dropped:
        target_episode = episode_by_event.get(item["duplicate_of"])
        if target_episode is None:
            # 理论上不会发生（duplicate_of 一定来自 kept 事件）。真发生时宁可显式失败，
            # 也不让 alias 里混进一个 event_id 造成下游静默错解析。
            raise RuntimeError(
                f"dropped event {item['event_id']} 的 duplicate_of={item['duplicate_of']} "
                "不在任何 episode 中；alias 契约无法满足"
            )
        item["duplicate_of_episode_id"] = target_episode
        alias[item["event_id"]] = target_episode

    return {
        "episodes": episodes,
        "alias": alias,
        "alias_schema": "event_id -> episode_id",
        "dropped": dropped,
        "stats": {
            "input_events": len(originals),
            "kept_events": len(kept),
            "episodes": len(episodes),
            "dropped": len(dropped),
            "merged_groups": sum(1 for episode in episodes if episode["member_count"] > 1),
        },
    }


# ---------------------------------------------------------------------------
# 通用活动投影（activity projection）· 纯函数 · 2026-10-25 第一切片
# ---------------------------------------------------------------------------
#
# 设计依据：`work/meal-event-integration-audit-20261025/REPORT.md`（用户已批准），
# 修订点：活动负责关联，子项目沿各自有依据的时间显示；短活动紧凑合并，
# 长活动不能强塞一张卡。
#
# 与 `build_episodes()` 的关系：**互不影响**。`build_episodes` 仍是"同一段讲述里的碎片
# 归组"，本函数在其之上给出"一项活动 + 若干可追溯子事实"的投影，供后续前端/读模型接线。
# 本函数不修改入参、不读盘、不写盘、不调模型、不重算金额。
#
# 十条硬约束（对应方案第三节/第四节）：
#   1. 缺证据不强合：对象/主体/时间任一不足 → 各自成活动，最多给 `possible` 关系提示。
#   2. 关系显式化：duplicate（同一事实多证据）/ complement（互补细节）/
#      revision（明确改口，保留旧值）/ conflict（并列冲突，不取大不相加）/ possible（仅候选）。
#   3. 状态分开：planned / in_progress / completed / cancelled / unknown / conflict；
#      "没提到"记 unknown，**绝不**输出"没做"。计划与结果并存时整卡标 conflict，
#      但子动作各自保留自己的状态，不给整场一键完成。
#   4. 时长只认明确证据：结构化 `duration_minutes` 或显式 `[start, end]`；
#      录音/对话跨度、两次提及间隔**永远不**当作活动时长。
#   5. 金额只引用：`ledger_refs` 逐条列出（含 role），**不求和、不改账**；
#      同一金额的多条播报只作为可能的付款证据并列。
#   6. 主体分离：`self` / `non_self` / `unknown` 不互相归并；播报声不能变成"本人付款"。
#   7. 跨日只关联不搬移：子事实保留各自 `date`，活动另给 `date_span`。
#   8. activity_id 稳定：由 kind+主体+对象键+首日锚点决定，后续补一句话不改变 ID。
#   9. 每侧与合计不双计：`weight_note`（左右各10kg）与 `weight_kg`（20）作为同一组次的
#      两种表述进 facts，不生成两条动作。
#  10. 无中文金额正则：金额只从结构化字段取；不做"X元"文本推断。

ACTIVITY_SCHEMA = "activity-projection-v1"

#: 活动类型（自有词表，命中不上就归 other，不强塞分类）。
ACTIVITY_KINDS: tuple[str, ...] = (
    "meal", "purchase", "fitness", "game", "work", "travel", "routine", "other",
)

#: 明确改口/细化标记：出现时把后一条作为 revision（保留旧值为历史）。
REVISION_MARKERS = (
    "改成", "换成", "改为", "不是", "不要", "就好", "稍微", "少放", "多放",
)

#: 明确取消/未做标记：只有命中才允许出现 cancelled。
CANCEL_MARKERS = ("取消", "不去了", "没去", "不练了", "不吃了", "算了", "不来了", "退掉")

#: 明确进行中标记。
IN_PROGRESS_MARKERS = ("开始", "正在", "刚开始", "进行中")

#: 明确完成标记。
COMPLETED_MARKERS = ("练完", "跑完", "做完", "吃完了", "吃过了", "买完", "已完成", "搞完")

#: 结构化数据里可以成为 facts 的区块（白名单，避免把任意字段当事实）。
FACT_SECTIONS = ("exercise", "game", "food", "meal", "expense", "work", "travel")

#: 金额角色的取值。
LEDGER_ROLES = ("ledger", "order", "quote", "broadcast", "unknown")

#: 子动作状态枚举（"没提到" ≠ "没做"）。
ACTIVITY_STATES: tuple[str, ...] = (
    "planned", "actual", "in_progress", "completed", "cancelled", "unknown", "conflict",
)

#: 训练子类词表（只做归类，不做金额解析）。用于把"力量"与"有氧"分开成两项活动。
STRENGTH_MARKERS = ("力量", "哑铃", "杠铃", "卧推", "深蹲", "硬拉", "器械", "组", "重量")
CARDIO_MARKERS = ("跑", "爬坡", "有氧", "骑行", "椭圆", "划船机", "跳绳")


def _is_broadcast_event(event: Mapping[str, Any]) -> bool:
    """收款播报：非本人主体 + 支出类别 + 有金额。这类事件只能作候选证据，不能与他人订单合并。"""
    categories = {str(item).strip().lower() for item in (event.get("categories") or [])}
    amount, _ = _structured_amount(event)
    return "expense" in categories and amount is not None and _actor_of(event) != "self"


def _sub_family(event: Mapping[str, Any]) -> str:
    """活动的子类（strength / cardio / running / …）。不足以判断时返回空串。"""
    data = event.get("data")
    data = data if isinstance(data, Mapping) else {}
    exercise = data.get("exercise")
    if isinstance(exercise, Mapping):
        workout = _text(exercise.get("workout_type")).strip().lower()
        if workout:
            return workout
    if _kind_of(event) != "fitness":
        return ""
    text = " ".join([_text(event.get("title")), _quote(event)])
    strength = sum(1 for marker in STRENGTH_MARKERS if marker in text)
    cardio = sum(1 for marker in CARDIO_MARKERS if marker in text)
    if strength and strength > cardio:
        return "strength"
    if cardio and cardio > strength:
        return "cardio"
    return ""


def _kind_of(event: Mapping[str, Any]) -> str:
    """活动类型。只看结构化 categories / activity_type，不做文本猜测。"""
    categories = {str(item).strip().lower() for item in (event.get("categories") or [])}
    activity_type = _text(event.get("activity_type")).lower()
    if categories & {"exercise", "fitness", "workout"} or activity_type.startswith(
        ("body_", "workout", "exercise")
    ):
        return "fitness"
    if categories & {"game", "gaming"} or activity_type.startswith("game"):
        return "game"
    if categories & {"food", "meal", "drink"}:
        return "meal"
    if categories & {"expense", "purchase", "shopping"}:
        return "purchase"
    if categories & {"work", "work_communication", "todo"}:
        return "work"
    if categories & {"travel", "transport"}:
        return "travel"
    if categories & {"routine", "health"}:
        return "routine"
    return "other"


def _actor_of(event: Mapping[str, Any]) -> str:
    """谁在做（不是谁开口）。unknown 不升级成 self。"""
    evidence = _evidence(event)
    label = _text(evidence.get("source_label")).strip().lower()
    if label in {"self", "non_self"}:
        return label
    return "unknown"


#: 纯数字/货币形态的词元（不做金额推断，只是不把 "¥10.00" 当成活动对象）。
_NUMERIC_TOKEN_CHARS = set("0123456789.¥$¥元块毛角万亿千-—~～")


def _is_numeric_token(token: str) -> bool:
    return bool(token) and all(char in _NUMERIC_TOKEN_CHARS for char in token)


def _content_bigrams(text: str) -> set[str]:
    """内容二元组：只保留"两个字符都不是数字/货币"的二元组。"""
    grams = _bigrams(_normalized(text))
    return {
        gram
        for gram in grams
        if len(gram) == 2 and not any(char.isdigit() for char in gram)
    }


def _object_tokens(event: Mapping[str, Any]) -> set[str]:
    """对象词元：只取结构化物品/动作名 + 标题，不解析金额。"""
    tokens: set[str] = set()
    data = event.get("data")
    data = data if isinstance(data, Mapping) else {}

    def collect(value: Any) -> None:
        if isinstance(value, str):
            for piece in re.split(r"[\s,，。、;；:：/|（）()\[\]【】]+", value):
                piece = piece.strip()
                if 1 < len(piece) <= 12 and not _is_numeric_token(piece):
                    tokens.add(piece)
        elif isinstance(value, Mapping):
            for key in ("item", "name", "dish", "product", "summary", "title"):
                if key in value:
                    collect(value[key])
            for key in ("exercises", "items"):
                if key in value:
                    collect(value[key])
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    for section in FACT_SECTIONS:
        if section in data:
            collect(data[section])
    collect(_text(event.get("title")))
    return tokens


def _activity_object_key(event: Mapping[str, Any]) -> str:
    tokens = sorted(_object_tokens(event))
    return "|".join(tokens[:4]) if tokens else ""


def _objects_overlap(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    tokens_a, tokens_b = _object_tokens(a), _object_tokens(b)
    if tokens_a & tokens_b:
        return True
    grams_a = _content_bigrams(" ".join(sorted(tokens_a)))
    grams_b = _content_bigrams(" ".join(sorted(tokens_b)))
    return bool(grams_a & grams_b)


def _structured_amount(event: Mapping[str, Any]) -> tuple[float | None, str]:
    """金额只从结构化字段取（不做中文金额正则）。"""
    data = event.get("data")
    data = data if isinstance(data, Mapping) else {}
    for section, key in (("expense", "amount"), ("purchase", "amount"), ("payment", "amount")):
        block = data.get(section)
        if isinstance(block, Mapping):
            raw = block.get(key)
            try:
                if raw is not None:
                    return float(raw), f"data.{section}.{key}"
            except (TypeError, ValueError):
                pass
    raw = data.get("amount") if isinstance(data, Mapping) else None
    try:
        if raw is not None:
            return float(raw), "data.amount"
    except (TypeError, ValueError):
        pass
    return None, ""


def _ledger_index(ledger: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    """把账本 payload 变成 event_id -> transaction 的只读索引（不复制金额计算逻辑）。"""
    if not isinstance(ledger, Mapping):
        return {}
    index: dict[str, Mapping[str, Any]] = {}
    for key in ("transactions", "payment_transactions", "all_transactions"):
        rows = ledger.get(key)
        if not isinstance(rows, (list, tuple)):
            continue
        for row in rows:
            if isinstance(row, Mapping) and row.get("event_id"):
                index.setdefault(_text(row.get("event_id")), row)
    return index


def _ledger_ref(event: Mapping[str, Any], index: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    """金额引用（**只引用不求和**）。"""
    event_id = _text(event.get("event_id"))
    amount, source = _structured_amount(event)
    entry = index.get(event_id)
    actor = _actor_of(event)
    categories = {str(item).strip().lower() for item in (event.get("categories") or [])}
    if entry is not None:
        try:
            ledger_amount = float(entry.get("amount"))
        except (TypeError, ValueError):
            ledger_amount = amount
        return {
            "event_id": event_id,
            "amount": ledger_amount,
            "currency": _text(entry.get("currency")) or "CNY",
            "role": "ledger",
            "source": "expense_ledger",
            "amount_policy": "reference_only_no_sum",
        }
    if amount is None:
        return None
    if actor in {"non_self", "unknown"} and "expense" in categories:
        role = "broadcast"
    elif "expense" in categories or "food" in categories:
        role = "order"
    else:
        role = "quote"
    return {
        "event_id": event_id,
        "amount": amount,
        "currency": "CNY",
        "role": role,
        "source": source or "event.data",
        "amount_policy": "reference_only_no_sum",
    }


def _intervals(event: Mapping[str, Any]) -> list[tuple[datetime, str]] | list[tuple[datetime, datetime]]:
    """显式区间：只有 absolute + absolute_ended_at 且不是占位日期时才算。"""
    time_info = event.get("time")
    time_info = time_info if isinstance(time_info, Mapping) else {}
    start = _event_time(event)
    raw_end = time_info.get("absolute_ended_at")
    if start is None or not raw_end:
        return []
    try:
        end = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return []
    if end <= start:
        return []
    return [(start, end)]


def _explicit_duration(event: Mapping[str, Any]) -> dict[str, Any]:
    """时长只认明确证据；没有就明说没有，绝不拿录音跨度充数。"""
    data = event.get("data")
    data = data if isinstance(data, Mapping) else {}
    for section in ("exercise", "game", "meal", "work", "travel"):
        block = data.get(section)
        if not isinstance(block, Mapping):
            continue
        for key, unit in (
            ("duration_minutes", "minutes"),
            ("duration_min", "minutes"),
            ("total_duration_min", "minutes"),
            ("duration_seconds", "seconds"),
        ):
            raw = block.get(key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            minutes = value / 60.0 if unit == "seconds" else value
            if minutes <= 0:
                continue
            return {
                "minutes": round(minutes, 1),
                "basis": f"explicit_field:{section}.{key}",
                "evidence_event_ids": [_text(event.get("event_id"))],
            }
    intervals = _intervals(event)
    if intervals:
        start, end = intervals[0]
        return {
            "minutes": round((end - start).total_seconds() / 60.0, 1),
            "basis": "explicit_interval:time.absolute→absolute_ended_at",
            "evidence_event_ids": [_text(event.get("event_id"))],
        }
    note = "no_explicit_duration"
    if _evidence(event).get("reported_at") and _evidence(event).get("start_offset_seconds") is not None:
        note = "recording_span_is_not_duration"
    return {"minutes": None, "basis": None, "evidence_event_ids": [], "note": note}


def _state_of(event: Mapping[str, Any]) -> str:
    """单个子动作的状态（unknown 不等于没做）。"""
    data = event.get("data")
    data = data if isinstance(data, Mapping) else {}
    text = " ".join(
        [
            _text(event.get("title")),
            _quote(event),
            _text(event.get("event_state")),
        ]
    )
    if any(marker in text for marker in CANCEL_MARKERS):
        return "cancelled"
    for section in ("exercise", "game", "meal"):
        block = data.get(section)
        if isinstance(block, Mapping):
            state = _text(block.get("state") or block.get("status")).lower()
            if state in {"completed", "done", "finished"}:
                return "completed"
            if state in {"in_progress", "started", "running"}:
                return "in_progress"
            if block.get("completed") is True:
                return "completed"
    state = _text(event.get("event_state")).lower()
    if state in {"completed", "done"}:
        return "completed"
    if state in {"in_progress", "started"}:
        return "in_progress"
    if any(marker in text for marker in COMPLETED_MARKERS):
        return "completed"
    if any(marker in text for marker in IN_PROGRESS_MARKERS):
        return "in_progress"
    if _is_plan(event):
        return "planned"
    claim = _text(_evidence(event).get("claim_type")).strip().lower()
    if claim in {"event", "order", "purchase", "transaction"}:
        # 事情确实被观察到（点单/购买/发生），但完成与否未提及 → actual，不是 completed。
        return "actual"
    return "unknown"


def _local_date(value: datetime) -> str:
    """人类可见日期一律按 Asia/Shanghai（跨午夜时 UTC 日期会差一天）。"""
    try:
        return value.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    except Exception:
        return value.date().isoformat()


def _event_date(event: Mapping[str, Any]) -> str | None:
    moment = _event_time(event)
    if moment is not None:
        return _local_date(moment)
    reported = _text(_evidence(event).get("reported_at"))
    if reported:
        try:
            parsed = datetime.fromisoformat(reported.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return _local_date(parsed)
    date = _text((event.get("time") or {}).get("date")) if isinstance(event.get("time"), Mapping) else ""
    return date or None


def _time_view(event: Mapping[str, Any]) -> dict[str, Any]:
    """子项自己的时间视图：发生/讲述分开，精度如实。"""
    time_info = event.get("time")
    time_info = time_info if isinstance(time_info, Mapping) else {}
    moment = _event_time(event)
    reported = _text(_evidence(event).get("reported_at"))
    precision = _text(time_info.get("precision")) or "unknown"
    basis = "occurrence" if moment is not None else ("reported" if reported else "unknown")
    return {
        "occurred_at": moment.isoformat() if moment is not None else None,
        "reported_at": reported or None,
        "precision": precision,
        "basis": basis,
        "date": _event_date(event),
        "display": (
            moment.strftime("%H:%M")
            if moment is not None and precision in {"exact", "minute", "inferred", "approximate"}
            else None
        ),
    }


def _extract_facts(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """从结构化区块抽 facts（白名单 + 原样值，不改写、不估算）。"""
    data = event.get("data")
    data = data if isinstance(data, Mapping) else {}
    event_id = _text(event.get("event_id"))
    facts: list[dict[str, Any]] = []

    exercise = data.get("exercise")
    if isinstance(exercise, Mapping):
        for exercise_row in exercise.get("exercises") or []:
            if not isinstance(exercise_row, Mapping):
                continue
            name = _text(exercise_row.get("name")) or "未命名动作"
            raw_sets = exercise_row.get("sets")
            rows: list[Mapping[str, Any]]
            if isinstance(raw_sets, (list, tuple)):
                rows = [row for row in raw_sets if isinstance(row, Mapping)]
            elif isinstance(raw_sets, bool) or raw_sets is None:
                rows = []
            elif isinstance(raw_sets, int):
                # `sets: 1` 是"组数"，明细在动作行本身（weight_kg / weight_note）
                rows = [exercise_row]
            else:
                rows = []
            if not rows:
                rows = [exercise_row]
            for index, row in enumerate(rows, start=1):
                facts.append(
                    {
                        "fact_id": f"{event_id}:set:{index}",
                        "key": "exercise_set",
                        "object": name,
                        "set_index": row.get("set") or index,
                        "value": {
                            "reps": row.get("reps"),
                            "weight_kg": row.get("weight_kg"),
                            "weight_note": row.get("note") or row.get("weight_note"),
                            "rest_seconds": row.get("rest_seconds"),
                            "sets_declared": raw_sets if isinstance(raw_sets, int) else len(rows),
                        },
                        "unit": "kg" if row.get("weight_kg") is not None else None,
                        "estimate": False,
                        "evidence_event_ids": [event_id],
                    }
                )
        for key in ("body_part", "workout_type", "target_muscles", "summary", "feeling"):
            if exercise.get(key) is not None:
                facts.append(
                    {
                        "fact_id": f"{event_id}:exercise:{key}",
                        "key": f"exercise.{key}",
                        "value": exercise.get(key),
                        "unit": None,
                        "estimate": False,
                        "evidence_event_ids": [event_id],
                    }
                )

    game = data.get("game")
    if isinstance(game, Mapping):
        for key, value in game.items():
            if value is None:
                continue
            facts.append(
                {
                    "fact_id": f"{event_id}:game:{key}",
                    "key": f"game.{key}",
                    "value": value,
                    "unit": None,
                    "estimate": False,
                    "evidence_event_ids": [event_id],
                }
            )

    for section in ("food", "meal"):
        block = data.get(section)
        if isinstance(block, Mapping):
            for key, value in block.items():
                if value is None:
                    continue
                facts.append(
                    {
                        "fact_id": f"{event_id}:{section}:{key}",
                        "key": f"{section}.{key}",
                        "value": value,
                        "unit": None,
                        "estimate": False,
                        "evidence_event_ids": [event_id],
                    }
                )
    return facts


def _is_revision_of(newer: Mapping[str, Any], older: Mapping[str, Any]) -> bool:
    """后一条是否是对前一条的明确改口/细化（同对象 + 改口标记）。"""
    if not _objects_overlap(newer, older):
        return False
    text = f"{_text(newer.get('title'))} {_quote(newer)}"
    return any(marker in text for marker in REVISION_MARKERS)


def _session_conflict(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """两侧都有 recording_id 且不同 → 明确是两次独立录音（不是"证据不足"）。"""
    rec_a = _text(_evidence(a).get("recording_id")).strip()
    rec_b = _text(_evidence(b).get("recording_id")).strip()
    return bool(rec_a) and bool(rec_b) and rec_a != rec_b


def _is_duplicate_of(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """同一事实被多条通道/多次抽取重复计（同段或近段 + 高相似 + 同对象）。

    **不同 recording_id 的两条不算重复**（返工 2026-10-25）：两台设备/两个会话各说一次
    "鸭腿饭"，是两场活动；只有出现明确关联证据（同单号/回头指代）才允许合并。
    """
    if _actor_of(a) != _actor_of(b):
        return False
    if _session_conflict(a, b):
        return False
    if not _objects_overlap(a, b):
        return False
    seg_a, seg_b = _segment(a), _segment(b)
    same_segment = bool(seg_a and seg_b and seg_a[0] == seg_b[0] and seg_a[1] == seg_b[1])
    gap = _gap_seconds(a, b)
    close = gap is not None and gap <= SAME_SEGMENT_WINDOW_SECONDS
    if not (same_segment or close):
        return False
    if similarity(_quote(a), _quote(b)) >= DEDUPE_SIMILARITY:
        return True
    amount_a, _ = _structured_amount(a)
    amount_b, _ = _structured_amount(b)
    return amount_a is not None and amount_a == amount_b and _is_contained(_quote(a), _quote(b))


def _activity_can_group(a: Mapping[str, Any], b: Mapping[str, Any]) -> tuple[bool, str]:
    """能否进同一活动。**只有结构化证据才允许合并**，文字相似/时间相近最多给 possible。

    优先级（2026-10-25 三次返工）：
      1. 结构化实例键：相等 → 合并；两边都有但不同 → **硬否定**（压过一切文字）。
      2. 主体/类型不同 → 否。
      3. 收款播报各自成卡。
      4. 同一段音频的重复抽取 → 合并（duplicate）。
      5. 两笔**带金额**的购买/订单，没有共同实例键 → 不合并（"再来一份"是新订单）。
      6. 同录音 + 明确更正 → revision；同录音 + 对象重叠 + 时间相近 → 互补细节。
      7. 其它（跨录音、时间远、对象不重合）→ 不合并，由调用方给 possible 提示。
    """
    ids_a, ids_b = _instance_ids(a), _instance_ids(b)
    if ids_a and ids_b:
        if ids_a & ids_b:
            return True, "same_instance_id"
        return False, "different_instance_id"
    if _kind_of(a) != _kind_of(b):
        return False, "different_kind"
    if _actor_of(a) != _actor_of(b):
        return False, "different_actor"
    broadcast_a, broadcast_b = _is_broadcast_event(a), _is_broadcast_event(b)
    if broadcast_a or broadcast_b:
        return (True, "duplicate_fact") if _is_duplicate_of(a, b) else (False, "broadcast_evidence_only")
    if _is_duplicate_of(a, b):
        return True, "duplicate_fact"
    if _session_conflict(a, b):
        # 不同录音、又没有共同实例键 → 证据不足，绝不合并。
        return False, "cross_session_needs_instance"
    amount_a, _ = _structured_amount(a)
    amount_b, _ = _structured_amount(b)
    if amount_a is not None and amount_b is not None:
        # 两笔带金额的订单/购买：没有实例键就不合并（避免"再来一份"被吞）。
        return False, "separate_instance_required"
    if _is_revision_of(a, b) or _is_revision_of(b, a):
        return True, "revision_same_object"
    # 子类不同（力量 vs 有氧等）→ 不同活动。这是**否定**规则，不放宽合并、不扩词表。
    sub_a, sub_b = _sub_family(a), _sub_family(b)
    if sub_a and sub_b and sub_a != sub_b:
        return False, "different_sub_family"
    gap = _gap_seconds(a, b)
    if not _objects_overlap(a, b):
        # 同一段录音里紧邻的两句、且都不是带金额的购买 → 视为同活动的互补碎片；
        # 带金额的购买一律要求实例键（见上），避免"再来一份"被吞。
        if gap is not None and gap <= ADJACENT_SEGMENT_WINDOW_SECONDS:
            return True, "complement_same_session"
        return False, "no_object_overlap"
    if gap is not None and gap <= ADJACENT_SEGMENT_WINDOW_SECONDS:
        return True, "complement_close_in_time"
    if _segment(a) and _segment(b) and _segment(a)[0] == _segment(b)[0]:
        return True, "complement_same_recording"
    return False, "insufficient_evidence"


#: 活动身份的时间桶粒度（分钟）。用于把同一天同对象的"两场活动"区分开，
#: 同时保证"补充后续句子 / 更正 / 删除非锚点事件"不改变 ID。
ACTIVITY_ID_BUCKET_MINUTES = 10

#: 结构化"同一实例"键：只有这些 id 相等才算同一笔/同一实例；不做中文词表推断。
INSTANCE_ID_KEYS = ("order_id", "transaction_id", "receipt_id", "instance_id", "payment_id")


def _instance_ids(event: Mapping[str, Any]) -> set[str]:
    """事件自带的结构化实例标识（order_id / transaction_id / …）。"""
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    evidence = _evidence(event)
    found: set[str] = set()
    for block in (data, evidence):
        for key in INSTANCE_ID_KEYS:
            value = _text(block.get(key)).strip()
            if value:
                found.add(f"{key}:{value}")
    return found


def _identity_moment(event: Mapping[str, Any]) -> datetime | None:
    """身份锚点时间：优先真实发生时间，其次讲述时间（都换算到本地时区）。"""
    moment = _event_time(event)
    if moment is not None:
        return moment
    reported = _text(_evidence(event).get("reported_at"))
    if not reported:
        return None
    try:
        parsed = datetime.fromisoformat(reported.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _identity_bucket(event: Mapping[str, Any]) -> str:
    """10 分钟粒度的时间桶（本地时区）。无时间 → "unknown"。"""
    moment = _identity_moment(event)
    if moment is None:
        return "unknown"
    try:
        local = moment.astimezone(ZoneInfo("Asia/Shanghai"))
    except Exception:
        local = moment
    floored = (local.minute // ACTIVITY_ID_BUCKET_MINUTES) * ACTIVITY_ID_BUCKET_MINUTES
    return local.replace(minute=floored, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def _activity_content_key(kind: str, actor: str, object_key: str, anchor: str | None,
                          bucket: str, amount: float | None, first_quote: str) -> str:
    """快照身份的构成要素：只含**本活动自己**的内容，不含邻居/序号。"""
    return "|".join(
        [
            kind,
            actor,
            object_key,
            anchor or "unknown",
            bucket,
            "" if amount is None else f"{amount:.2f}",
            hashlib.sha256(_normalized(first_quote).encode("utf-8")).hexdigest()[:8],
        ]
    )


def _activity_id(instance_id: str | None, content_key: str) -> str:
    """活动 ID。

    * 有结构化实例键（order_id/transaction_id…）→ 由实例派生：
      `identity.basis="source_instance_id"`，**可跨修订稳定**，可作为账本引用。
    * 没有实例键 → 由本活动自身内容派生：`identity.basis="snapshot_local"`，
      **明确不承诺跨修订稳定**，`persistent_reference_ok=False`（禁止当持久账本引用）。
    两种都不使用"同桶序号"，因此邻居增删不会让本活动 ID 漂移。
    """
    if instance_id:
        seed = f"instance|{instance_id}|{content_key}"
    else:
        seed = f"snapshot|{content_key}"
    return "act_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _compose_relations(members: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """活动内部关系：duplicate / revision / complement / conflict。"""
    relations: list[dict[str, Any]] = []
    ordered = sorted(members, key=_sort_key)
    for index, newer in enumerate(ordered):
        for older in ordered[:index]:
            newer_id = _text(newer.get("event_id"))
            older_id = _text(older.get("event_id"))
            if _is_duplicate_of(newer, older):
                relations.append(
                    {
                        "type": "duplicate",
                        "source_event_id": newer_id,
                        "target_event_id": older_id,
                        "reason": "同一事实的重复抽取/重复播报",
                        "resolved": True,
                    }
                )
                continue
            if _is_revision_of(newer, older):
                relations.append(
                    {
                        "type": "revision",
                        "source_event_id": newer_id,
                        "target_event_id": older_id,
                        "reason": "后一句明确改口/细化，旧值保留为历史",
                        "resolved": True,
                    }
                )
                continue
            newer_state, older_state = _state_of(newer), _state_of(older)
            if "planned" in {newer_state, older_state} and (
                {newer_state, older_state} & {"completed", "actual", "in_progress"}
            ):
                relations.append(
                    {
                        "type": "conflict",
                        "source_event_id": newer_id,
                        "target_event_id": older_id,
                        "reason": "同一活动的计划与完成陈述并存，未给出对应关系",
                        "resolved": False,
                    }
                )
                continue
            relations.append(
                {
                    "type": "complement",
                    "source_event_id": newer_id,
                    "target_event_id": older_id,
                    "reason": "同活动互补细节",
                    "resolved": True,
                }
            )
    return relations


def _activity_state(states: Sequence[str]) -> str:
    """整卡状态：计划与实际/完成并存 → conflict（不判定完成）；都没提 → unknown。"""
    present = set(states)
    if "cancelled" in present and present <= {"cancelled", "planned"}:
        return "cancelled"
    if "planned" in present and ({"completed", "actual", "in_progress"} & present):
        return "conflict"
    for candidate in ("completed", "actual", "in_progress", "planned", "cancelled"):
        if candidate in present:
            return candidate
    return "unknown"


def build_activity_projection(
    events: Iterable[Mapping[str, Any]],
    *,
    ledger: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把事件投影成「一项活动 + 若干可追溯子事实」。纯函数：不改入参、不读写盘。

    返回::

        {
          "schema": "activity-projection-v1",
          "activities": [{activity_id, kind, title, subject, state, states,
                          time, duration, location, actions, facts, relations,
                          ledger_refs, evidence, source_event_ids, date_span,
                          boundary_notes, confidence}],
          "alias": {event_id: activity_id},
          "unattached": [],
          "relations": [...],        # 活动之间的关系（possible/possible_payment…）
          "stats": {...},
          "contract": {...},         # 接线用接口契约（见函数末尾）
        }
    """
    originals = [dict(event) for event in events]
    ledger_index = _ledger_index(ledger)

    ordered = sorted(originals, key=_sort_key)
    groups: list[list[Mapping[str, Any]]] = []
    used: set[int] = set()
    for index, event in enumerate(ordered):
        if index in used:
            continue
        group = [event]
        used.add(index)
        changed = True
        while changed:
            changed = False
            for other_index, other in enumerate(ordered):
                if other_index in used:
                    continue
                if any(_activity_can_group(member, other)[0] for member in group):
                    group.append(other)
                    used.add(other_index)
                    changed = True
        groups.append(group)

    # 先算出每个分组的身份要素，再按 (日期, 时间桶, 排序键) 稳定排序后分配序号，
    # 保证：输入顺序变化 → ID 不变；同一天同对象的两场活动 → 序号不同 → ID 唯一。
    descriptors: list[dict[str, Any]] = []
    for group in groups:
        members = sorted(group, key=_sort_key)
        first = members[0]
        dates = sorted({d for d in (_event_date(member) for member in members) if d})
        buckets = sorted(
            bucket
            for bucket in (_identity_bucket(member) for member in members)
            if bucket != "unknown"
        )
        descriptors.append(
            {
                "members": members,
                "first": first,
                "kind": _kind_of(first),
                "actor": _actor_of(first),
                "object_key": _activity_object_key(first),
                "anchor": dates[0] if dates else None,
                "bucket": buckets[0] if buckets else "unknown",
                "sort_key": _sort_key(first),
            }
        )
    descriptors.sort(key=lambda item: (item["anchor"] or "", item["bucket"], item["sort_key"]))
    for descriptor in descriptors:
        instance_ids: set[str] = set()
        for member in descriptor["members"]:
            instance_ids |= _instance_ids(member)
        amount = None
        for member in descriptor["members"]:
            value, _ = _structured_amount(member)
            if value is not None:
                amount = value
                break
        descriptor["instance_id"] = sorted(instance_ids)[0] if instance_ids else None
        descriptor["content_key"] = _activity_content_key(
            descriptor["kind"],
            descriptor["actor"],
            descriptor["object_key"],
            descriptor["anchor"],
            descriptor["bucket"],
            amount,
            _quote(descriptor["members"][0]),
        )
        descriptor["activity_id"] = _activity_id(descriptor["instance_id"], descriptor["content_key"])

    # 内容完全相同的两个快照活动（同对象/同桶/同额/同首句）：只用**本组自身成员 id** 做后缀，
    # 因此邻居增删不会让任何一方漂移；实例键活动天然唯一，不参与。
    collisions: dict[str, list[dict[str, Any]]] = {}
    for descriptor in descriptors:
        collisions.setdefault(descriptor["activity_id"], []).append(descriptor)
    for base_id, rows in collisions.items():
        if len(rows) < 2:
            continue
        for descriptor in rows:
            suffix = hashlib.sha256(
                "|".join(sorted(_text(m.get("event_id")) for m in descriptor["members"])).encode("utf-8")
            ).hexdigest()[:6]
            descriptor["activity_id"] = f"{base_id}_{suffix}"

    activities: list[dict[str, Any]] = []
    alias: dict[str, str] = {}
    for descriptor in descriptors:
        members = descriptor["members"]
        first = descriptor["first"]
        kind = descriptor["kind"]
        actor = descriptor["actor"]
        object_key = descriptor["object_key"]
        dates = sorted({d for d in (_event_date(member) for member in members) if d})
        anchor = descriptor["anchor"]
        activity_id = descriptor["activity_id"]

        actions: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        ledger_refs: list[dict[str, Any]] = []
        durations: list[dict[str, Any]] = []
        states: list[str] = []
        for position, member in enumerate(members, start=1):
            member_id = _text(member.get("event_id"))
            state = _state_of(member)
            states.append(state)
            duration = _explicit_duration(member)
            durations.append(duration)
            ref = _ledger_ref(member, ledger_index)
            if ref is not None:
                ledger_refs.append(ref)
            member_facts = _extract_facts(member)
            facts.extend(member_facts)
            actions.append(
                {
                    "action_id": f"{activity_id}:a{position}",
                    "label": _text(member.get("title")) or _quote(member)[:24],
                    "object": sorted(_object_tokens(member))[:3],
                    "state": state,
                    "time": _time_view(member),
                    "duration": duration,
                    "amount_ref": ref,
                    "fact_ids": [fact["fact_id"] for fact in member_facts],
                    "evidence": _provenance(member),
                }
            )

        planned = [d for d, s in zip(durations, states) if s == "planned" and d["minutes"] is not None]
        actual = [
            d
            for d, s in zip(durations, states)
            if s in {"completed", "in_progress", "actual"} and d["minutes"] is not None
        ]
        duration_block = {
            "planned_minutes": planned[0]["minutes"] if planned else None,
            "actual_minutes": actual[0]["minutes"] if actual else None,
            "basis": (actual[0]["basis"] if actual else (planned[0]["basis"] if planned else None)),
            "evidence_event_ids": sorted(
                {eid for row in planned + actual for eid in row.get("evidence_event_ids") or []}
            ),
            "interval_unions": [],
            "note": None,
        }
        if duration_block["actual_minutes"] is None and duration_block["planned_minutes"] is None:
            notes = [row.get("note") for row in durations if row.get("note")]
            if "recording_span_is_not_duration" in notes:
                duration_block["note"] = "recording_span_is_not_duration"
            else:
                duration_block["note"] = notes[0] if notes else "no_explicit_duration"
        # 多段显式区间去重（重叠区间并集），不重复计时长。
        intervals: list[tuple[datetime, datetime]] = []
        for member in members:
            for interval in _intervals(member):
                intervals.append(interval)
        if len(intervals) > 1:
            merged: list[list[datetime]] = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            duration_block["interval_unions"] = [
                {"start": start.isoformat(), "end": end.isoformat()} for start, end in merged
            ]
            if duration_block["actual_minutes"] is None:
                duration_block["actual_minutes"] = round(
                    sum((end - start).total_seconds() for start, end in merged) / 60.0, 1
                )
                duration_block["basis"] = "explicit_interval_union"

        boundary_notes: list[str] = []
        if duration_block["note"] == "recording_span_is_not_duration":
            boundary_notes.append("只有录音跨度，未作为活动时长")
        if len({action["time"]["date"] for action in actions if action["time"]["date"]}) > 1:
            boundary_notes.append("跨日活动：子项保留各自日期，统计不搬移")
        if any(ref["role"] == "broadcast" for ref in ledger_refs) and any(
            ref["role"] in {"order", "ledger"} for ref in ledger_refs
        ):
            boundary_notes.append("播报金额仅作候选证据，未与订单金额相加")

        activity = {
            "activity_id": activity_id,
            "activity_identity": {
                "basis": "source_instance_id" if descriptor["instance_id"] else "snapshot_local",
                "instance_id": descriptor["instance_id"],
                "kind": kind,
                "actor": actor,
                "object_key": object_key,
                "anchor_date": anchor,
                "time_bucket": descriptor["bucket"],
                "bucket_minutes": ACTIVITY_ID_BUCKET_MINUTES,
                "content_key": descriptor["content_key"],
                "stable_across_revisions": bool(descriptor["instance_id"]),
                "persistent_reference_ok": bool(descriptor["instance_id"]),
            },
            "kind": kind,
            "title": _text(first.get("title")) or _quote(first)[:24],
            "subject": {"actor": actor},
            "state": _activity_state(states),
            "states": {
                "planned": states.count("planned"),
                "actual": states.count("actual"),
                "in_progress": states.count("in_progress"),
                "completed": states.count("completed"),
                "cancelled": states.count("cancelled"),
                "unknown": states.count("unknown"),
                "conflict": "planned" in states
                and bool({"completed", "actual", "in_progress"} & set(states)),
            },
            "time": {
                "start": actions[0]["time"]["occurred_at"] if actions else None,
                "end": actions[-1]["time"]["occurred_at"] if actions else None,
                "dates": dates,
                "precision": sorted({action["time"]["precision"] for action in actions}),
            },
            "duration": duration_block,
            "location": None,
            "actions": actions,
            "facts": facts,
            "relations": _compose_relations(members),
            "ledger_refs": ledger_refs,
            "evidence": [_provenance(member) for member in members],
            "source_event_ids": [_text(member.get("event_id")) for member in members],
            "date_span": dates,
            "boundary_notes": boundary_notes,
            "confidence": "high" if len(members) == 1 else ("medium" if object_key else "low"),
        }
        for member in members:
            alias[_text(member.get("event_id"))] = activity_id
        activities.append(activity)

    # 活动之间：同额不同主体/不同渠道的播报只提示"可能相关"，绝不合并、绝不相加。
    relations: list[dict[str, Any]] = []
    for left_index, left in enumerate(activities):
        for right in activities[left_index + 1 :]:
            # (a) 计划 vs 实际：对象相同但证据不足 / 跨录音 → 只提示可能属于同一活动。
            left_states = set(left["states"].keys()) & {
                key for key in ("planned", "actual", "completed", "in_progress") if left["states"].get(key)
            }
            right_states = set(right["states"].keys()) & {
                key for key in ("planned", "actual", "completed", "in_progress") if right["states"].get(key)
            }
            left_tokens: set[str] = set()
            right_tokens: set[str] = set()
            for action in left["actions"]:
                left_tokens.update(action.get("object") or [])
            for action in right["actions"]:
                right_tokens.update(action.get("object") or [])
            shares_object = bool(left_tokens & right_tokens) or bool(
                _content_bigrams(" ".join(sorted(left_tokens)))
                & _content_bigrams(" ".join(sorted(right_tokens)))
            )
            if (
                "planned" in (left_states | right_states)
                and (left_states | right_states) & {"actual", "completed", "in_progress"}
                and shares_object
            ):
                relations.append(
                    {
                        "type": "possible",
                        "reason": "对象相同的计划与实际分散在不同活动，证据不足未合并",
                        "resolved": False,
                        "source_activity_id": left["activity_id"],
                        "target_activity_id": right["activity_id"],
                        "evidence_event_ids": left["source_event_ids"] + right["source_event_ids"],
                    }
                )
                continue
            # (b) 对象相同、时间相近但证据不足 → 只提示"可能相关"，不合并。
            def _broadcast_only(activity: Mapping[str, Any]) -> bool:
                refs = activity.get("ledger_refs") or []
                return bool(refs) and all(ref.get("role") == "broadcast" for ref in refs)

            if shares_object and not (_broadcast_only(left) and _broadcast_only(right)):
                relations.append(
                    {
                        "type": "possible",
                        "reason": "对象相同但缺少结构化同一实例证据，未合并",
                        "resolved": False,
                        "source_activity_id": left["activity_id"],
                        "target_activity_id": right["activity_id"],
                        "evidence_event_ids": left["source_event_ids"] + right["source_event_ids"],
                    }
                )
                continue
            # (c) 同额不同主体/渠道（典型：两条同额收款播报）→ 仅候选。
            left_amounts = {ref["amount"] for ref in left["ledger_refs"]}
            right_amounts = {ref["amount"] for ref in right["ledger_refs"]}
            if not left_amounts or not right_amounts or not (left_amounts & right_amounts):
                continue
            if left["subject"]["actor"] == right["subject"]["actor"]:
                continue
            relations.append(
                {
                    "type": "possible",
                    "reason": "相同金额、不同主体/渠道，仅作候选，未合并未相加",
                    "resolved": False,
                    "source_activity_id": left["activity_id"],
                    "target_activity_id": right["activity_id"],
                    "evidence_event_ids": left["source_event_ids"] + right["source_event_ids"],
                }
            )

    # 同额播报只作为候选证据挂在订单活动旁边（不合并、不相加）。
    broadcast_refs = [
        (activity["activity_id"], ref)
        for activity in activities
        for ref in activity["ledger_refs"]
        if ref["role"] == "broadcast"
    ]
    for activity in activities:
        own_amounts = {
            ref["amount"]
            for ref in activity["ledger_refs"]
            if ref["role"] in {"ledger", "order", "quote"}
        }
        candidates = [
            ref
            for owner, ref in broadcast_refs
            if owner != activity["activity_id"] and ref["amount"] in own_amounts
        ]
        activity["broadcast_candidates"] = candidates
        if candidates:
            activity["boundary_notes"].append("同额播报仅作候选证据，未与订单金额相加")

    # 未分组记录：单成员活动原样列出，便于前端提供"未分组"视图，不丢原始子项。
    unattached = sorted(
        event_id
        for activity in activities
        if len(activity["source_event_ids"]) == 1
        for event_id in activity["source_event_ids"]
    )

    # 唯一性硬断言：activity_id 冲突会让 alias / relations / 前端分组串台。
    activity_ids = [activity["activity_id"] for activity in activities]
    if len(activity_ids) != len(set(activity_ids)):
        duplicates = sorted({value for value in activity_ids if activity_ids.count(value) > 1})
        raise RuntimeError(f"activity_id 不唯一: {duplicates}")

    return {
        "schema": ACTIVITY_SCHEMA,
        "activities": activities,
        "alias": alias,
        "unattached": unattached,
        "relations": relations,
        "stats": {
            "input_events": len(originals),
            "activities": len(activities),
            "multi_action_activities": sum(1 for item in activities if len(item["actions"]) > 1),
            "by_kind": {
                kind: sum(1 for item in activities if item["kind"] == kind)
                for kind in ACTIVITY_KINDS
                if any(item["kind"] == kind for item in activities)
            },
            "ledger_ref_count": sum(len(item["ledger_refs"]) for item in activities),
            "merged_groups": sum(1 for item in activities if len(item["actions"]) > 1),
        },
        "contract": {
            "schema": ACTIVITY_SCHEMA,
            "consumes": "canonical events（event_view.build_event_view 输出）",
            "must_not": [
                "重算金额/新增金额正则",
                "把播报声当本人付款",
                "用录音跨度当活动时长",
                "把计划当完成",
                "把 unknown 当没做",
                "跨日后搬动子项日期",
            ],
            "id_stability": (
                "有结构化实例键（order_id/transaction_id/receipt_id/instance_id/payment_id）时："
                "activity_id=sha256('instance|'+实例键+内容键)，可跨修订稳定，可作账本引用；"
                "无实例键时：activity_id=sha256('snapshot|'+kind|actor|object_key|anchor_date|"
                "time_bucket|amount|首句哈希)，标记 basis=snapshot_local，**不承诺跨修订稳定**"
            ),
            "stability_guarantees": [
                "同一结构化实例键（同 order_id 等）→ 始终同一 activity_id，跨时间桶/跨录音都稳定",
                "补充同一活动内的后续句子（同桶、无金额冲突）→ 快照身份不变",
                "删除或插入**邻居活动**（同桶其它场次）→ 快照身份不变（不使用 ordinal）",
                "输入顺序变化 → activity_id 不变（按 anchor/bucket/排序键稳定排序）",
            ],
            "stability_boundaries": [
                "无实例键时删除/移动决定 time_bucket 的锚点事件 → 快照身份变化（已如实标注 snapshot_local）",
                "无实例键时对象键变化（鸭腿→鸡腿改名）→ 快照身份变化",
                "两条内容完全相同的快照活动 → 用**本组自身**成员 id 后缀区分；只影响这两条，不影响邻居",
                "快照身份不得作为持久账本引用：identity.persistent_reference_ok=false",
            ],
            "migration": (
                "优先用 identity.instance_id 映射；没有实例键时用 (kind, actor, object_key, anchor_date, "
                "time_bucket, content_key) 重算，并明确告知调用方这是快照级身份；"
                "无法重算（锚点被删/跨桶）→ 按新活动处理，不做隐式合并，也不回写持久 ID 表"
            ),
            "grouping_evidence": (
                "只认结构化证据：相同实例键 / 同一段音频的重复抽取 / 同录音内的更正与互补细节。"
                "中文指代、时间相近、文本相似都**不构成**合并理由，最多产出 possible 关系"
            ),
            "ledger": "ledger_refs 只引用（role=ledger/order/quote/broadcast），不求和",
            "states": "planned/in_progress/completed/cancelled/unknown/conflict 分立",
            "relations": "duplicate/complement/revision/conflict/possible，未解决标 resolved=false",
            "duration": "仅 explicit_field / explicit_interval / explicit_interval_union",
            "wiring": "接线只需把 event_view 事件 + expense_ledger 传入；不得在前端生成事实",
        },
    }


# ---------------------------------------------------------------------------
# 点餐总额绑定（Q364）· 纯函数 · 2026-10-25
# ---------------------------------------------------------------------------
#
# 问题：本人点餐时口述了总结算金额（"三十八"、"三块"），但抽取没有把它变成
# 结构化金额 → 当日账本漏记（10-24 显示 ¥0）。
#
# 规则（严格，宁可漏记也不猜）：
#   1. 只对**本人**（source_label=self）的餐饮/消费事件绑定；
#   2. 事件必须已经带菜名（引用各菜品由 `dish_event_ids` 记录），且自身没有金额；
#   3. 金额只从该事件原话里的**显式总额**取：阿拉伯数字 + 元/块，或中文数字 + 元/块；
#   4. 命中收款播报标记（到账/收款/播报/收钱/扫码支付…）一律跳过 —— 背景短剧/广告不入账；
#   5. 不求和、不改原事件：返回**绑定了金额的事件副本**，原对象保持不变。

MEAL_KINDS = ("meal", "purchase", "food", "expense")
BROADCAST_MARKERS = ("到账", "收款", "收钱", "播报", "扫码支付", "收钱吧", "二维码收款")
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_ARABIC_TOTAL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块)")
_CN_TOTAL_RE = re.compile(r"([零〇一二三四五六七八九十百千两]{1,6})\s*(?:元|块钱|块)")
# 整句就是"一共三十八 / 总共三十八 / 就三十八 / 三十八"这类无单位总额（只认整句，避免误抓菜名里的数字）
_CN_BARE_TOTAL_RE = re.compile(
    r"^(?:一共|总共|合计|共|就|是)?\s*([零〇一二三四五六七八九十百千两]{1,6})\s*[。！!,.，]?$"
)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _cn_to_number(text: str) -> float | None:
    """中文数字 → 数值（只支持 0–9999 的常见口语写法，如 三十八 / 一百二 / 十五）。"""
    if not text:
        return None
    total, current = 0, 0
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            total += (current or 1) * unit
            current = 0
        else:
            return None
    return float(total + current)


def parse_meal_total(quote: str) -> tuple[float, str] | None:
    """从原话里取**显式总额**（阿拉伯优先，其次中文）。返回 (金额, 命中片段)。"""
    text = _text(quote)
    if not text:
        return None
    match = _ARABIC_TOTAL_RE.search(text)
    if match:
        return float(match.group(1)), match.group(0)
    match = _CN_TOTAL_RE.search(text)
    if match:
        value = _cn_to_number(match.group(1))
        if value and value > 0:
            return value, match.group(0)
    bare = _CN_BARE_TOTAL_RE.match(text.strip())
    if bare and not any(unit in bare.group(1) for unit in ("百", "千")):
        # 含百/千的中文口语（如"一百二"）歧义大（102? 120?）→ 不猜
        value = _cn_to_number(bare.group(1))
        if value and value > 0:
            return value, bare.group(0).strip()
    return None


def bind_meal_totals(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """把"点餐 + 原话总额"绑定成带金额的事件副本（不改入参、不求和）。

    返回::

        {"events": [...原事件 + 绑定副本...],
         "bindings": [{event_id, amount, matched_text, dish_event_ids, evidence}],
         "skipped": [{event_id, reason}]}
    """
    originals = [event for event in events if isinstance(event, Mapping)]
    bound_events: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for event in originals:
        event_id = _text(event.get("event_id"))
        evidence = _as_mapping(event.get("evidence"))
        data = _as_mapping(event.get("data"))
        actor = _text(evidence.get("source_label")).strip().lower()
        quote = _text(evidence.get("quote")) or _text(event.get("title"))
        categories = {str(item).strip().lower() for item in (event.get("categories") or [])}
        existing = _as_mapping(data.get("expense"))

        def skip(reason: str) -> None:
            skipped.append({"event_id": event_id, "reason": reason})
            bound_events.append(dict(event))

        if actor != "self":
            skip("not_self")
            continue
        if not categories & set(MEAL_KINDS):
            skip("not_meal_or_expense")
            continue
        if existing.get("amount") is not None:
            skip("amount_already_present")
            continue
        if any(marker in quote for marker in BROADCAST_MARKERS):
            skip("broadcast_or_ad_isolated")
            continue
        parsed = parse_meal_total(quote)
        if parsed is None:
            skip("no_explicit_total")
            continue

        amount, matched = parsed
        dish_ids = [
            _text(other.get("event_id"))
            for other in originals
            if other is not event
            and _text(_as_mapping(other.get("evidence")).get("source_label")).strip().lower() == "self"
            and _text(_as_mapping(other.get("evidence")).get("recording_id"))
            == _text(evidence.get("recording_id"))
            and {str(item).strip().lower() for item in (other.get("categories") or [])} & {"food", "meal"}
        ]
        copy = dict(event)
        copy_evidence = dict(evidence)
        copy_data = dict(data)
        copy_data["expense"] = {
            "amount": round(amount, 2),
            "currency": _text(existing.get("currency")) or "CNY",
            "amount_source": "quote_total",
            "binding": "meal_total",
            "matched_text": matched,
        }
        copy_evidence["amount_binding"] = {
            "source": "quote_total",
            "matched_text": matched,
            "dish_event_ids": dish_ids,
            "policy": "explicit_total_only_no_sum",
        }
        copy["data"] = copy_data
        copy["evidence"] = copy_evidence
        bound_events.append(copy)
        bindings.append({
            "event_id": event_id,
            "amount": round(amount, 2),
            "matched_text": matched,
            "dish_event_ids": dish_ids,
            "evidence": {"quote_matched": matched, "policy": "explicit_total_only_no_sum"},
        })

    return {
        "events": bound_events,
        "bindings": bindings,
        "skipped": skipped,
        "stats": {
            "input_events": len(originals),
            "bound": len(bindings),
            "skipped": len(skipped),
            "bound_total_cny": round(sum(item["amount"] for item in bindings), 2),
        },
        "contract": {
            "only_self": "只绑定 source_label=self 的事件；非本人一律跳过",
            "broadcast_isolated": "命中到账/收款/播报等标记 → 跳过（短剧/广告不入账）",
            "explicit_total_only": "只取原话里的显式总额（数字+元/块 或 中文数字+元/块），不推断、不求和",
            "no_history_rewrite": "返回事件副本，原对象不变；不改历史产物",
        },
    }
