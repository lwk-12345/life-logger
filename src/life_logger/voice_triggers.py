"""Spoken micro-command spotting and fast capture for Life Logger.

Detects high-intent conversational triggers:
- TODO: "记一下/提醒我/待办/别忘了..." -> Extracted task item
- EXPENSE: "花了...元/块/买了...花了..." -> Extracted expense amount & description
- IDEA: "灵感/想法/点子/记个思考..." -> Extracted idea capsule
- PRIVACY: "这条别记/暂停记录/别录了..." -> Redaction / mute trigger
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import sys
import re
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from life_logger.food_lookup import (
    lookup_command_reply,
    parse_nutrition_label_text,
    parse_spoken_label,
)
from life_logger.timeutil import local_date as local_date_of
from life_logger.work_apps import should_suppress
from life_logger.voice_lexicon import (
    HABIT_ADD_ALT,
    HABIT_DELETE_ALT,
    HABIT_DONE_ALT,
    NUTRITION_REPORT_ALT,
    TODO_DELETE_ALT,
    TODO_DONE_ALT,
)

logger = logging.getLogger(__name__)

TRIGGER_TYPES = (
    "todo",
    "todo_delete",
    "todo_done",
    "habit",
    "habit_delete",
    "habit_done",
    "nutrition_label",
    "expense",
    "idea",
    "query",
    "privacy_mute",
)

# 正则表达式模式
_TODO_PATTERNS = [
    re.compile(r"(?:记录一下|记一下|记录下|记下|帮我记(?:一下)?|我要记(?:一下)?|提醒我|待办|别忘了|记得要|记得)(?:[:：\s]*)(.+)", re.IGNORECASE),
    re.compile(r"帮我记(?:一下)?(?:[:：\s]*)(.+)", re.IGNORECASE),
]

# [2026-10-13] 习惯口令 —— 与待办**完全平行**的另一条线，判定必须排在待办之前：
# 「习惯」字面不含「待办」，但「添加一个习惯」里的「一个」等词会被宽松的待办正则误吞，
# 且用户心智上习惯 ≠ 待办（习惯天天重置、无完成态）。放在最前面最安全。
_HABIT_DELETE_PATTERNS = [
    re.compile(rf"(?:{HABIT_DELETE_ALT})(?:[:：\s]*)(.+)", re.IGNORECASE),
]
_HABIT_DONE_PATTERNS = [
    re.compile(rf"(?:{HABIT_DONE_ALT})(?:[:：\s]*)(.+)", re.IGNORECASE),
]
_HABIT_PATTERNS = [
    re.compile(rf"(?:{HABIT_ADD_ALT})(?:[:：\s]*)(.+)", re.IGNORECASE),
]

# [2026-10-13 收口] 待办状态变更口令 —— **必须在 _TODO_PATTERNS 之前判定**。
# 否则上面那条裸「待办」会先把「删除待办 买水」抢成「新建待办：买水」，方向反了
# （实测确认）。判定顺序在 consume() 里：隐私 → 记账 → **待办删除/打卡** → 新建待办。
_TODO_DELETE_PATTERNS = [
    re.compile(rf"(?:{TODO_DELETE_ALT})(?:[:：\s]*)(.+)", re.IGNORECASE),
]
_TODO_DONE_PATTERNS = [
    re.compile(rf"(?:{TODO_DONE_ALT})(?:[:：\s]*)(.+)", re.IGNORECASE),
]

_QUERY_PATTERNS = [
    re.compile(r"(?:我要查(?:一下)?|我想查(?:一下)?|帮我查(?:一下)?|查询一下|查一下|你查一下|检查一下|调查一下)(?:[:：\s]*)(.+)", re.IGNORECASE),
]

_EXPENSE_PATTERNS = [
    re.compile(r"(?:花了|付了|消费|买了)(.+?)(?:花了|用了)?([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|块|块钱)", re.IGNORECASE),
    re.compile(r"([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|块|块钱)(?:买了|支付了|花在)(.+)", re.IGNORECASE),
    re.compile(r"(?:买单|结账|记账)(?:[:：\s]*)(.+?)([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|块)", re.IGNORECASE),
]

_IDEA_PATTERNS = [
    re.compile(r"(?:灵感|想法|点子|有个点子|突然想到|记个思考)(?:[:：\s]*)(.+)", re.IGNORECASE),
]

_PRIVACY_PATTERNS = [
    re.compile(r"(?:这条别记|暂停记录|别录了|关闭录音|敏感内容|这段删除)", re.IGNORECASE),
]

# [2026-10-13] 营养口令 —— 「念一次存一次」：把用户报的营养成分表写进自建品牌库（层①）。
# 判定放在隐私之后、记账之前：营养成分表里必然带数字，别让「记账」等拿数字的正则先抢。
_NUTRITION_PATTERNS = [
    re.compile(rf"(?:{NUTRITION_REPORT_ALT})(?:[:：\s]*)(.+)", re.IGNORECASE),
]


#: 即时抓取内容的最小有效信息量（剔除标点后）。
# 实测垃圾卡片：「吧」（1 字）、「」（空）、「一个东西」、「他的」、
# 以及电视台词被截出来的「大明呢，大明最后怎么样了」。
# 3 个信息字是下限（”虾牛马“这种三字专名要能过），短于 3 字一律不是口令。
MIN_CAPTURE_CONTENT_CHARS = 3

#: 纯语气词 / 连接词 / 悬空指代：抽出来当待办毫无意义。
_FILLER_ONLY = {
    "吧", "啊", "呢", "呀", "哦", "嗯", "呃", "唉", "吗", "来", "对", "是",
    "这个", "那个", "东西", "什么", "怎么样", "什么情况", "然后", "所以", "就是",
    "其他", "一个东西", "一个想法", "当时", "那时候",
}


def is_trusted_self_voice(
    *,
    source_label: str | None = None,
    acoustic_proximity: str | None = None,
    voice_similarity: float | None = None,
) -> bool:
    """判断这条转写是否"可当作本人在说话"。

    没有传任何身份参数时返回 True（不改变旧调用方行为）；
    一旦给了身份信息，就要求**正向证据**，而不是只要“不是别人”就算本人。

    实测反面案例（2026-10-17 电视剧情）：
        source_label=unknown, acoustic_proximity=far, voice_similarity=0.366
    """
    if source_label is None and acoustic_proximity is None and voice_similarity is None:
        return True
    if source_label is not None and str(source_label) != "self":
        return False
    if acoustic_proximity is not None and str(acoustic_proximity) == "far":
        return False
    if voice_similarity is not None:
        try:
            # 不低于 0.30：0.36 那个案例（电视台词）已经被 label/proximity 拦住，
            # 但实测本人正常语句也可能只有 0.42（“你再查一下这个GitHub”），
            # 阈值定太高会误杀真实口令。
            if float(voice_similarity) < 0.30:
                return False
        except (TypeError, ValueError):
            pass
    return True


def is_meaningful_capture_content(content: str) -> bool:
    """即时抓取内容是否有足够信息量（防止「吧」「」「他的」这种碎片上板）。"""
    text = str(content or "").strip()
    if not text:
        return False
    core = re.sub(r"[\s，,。.、；;：:！!？?\-—_…·\"'“”‘’()（）\[\]【】]", "", text)
    if not core or core in _FILLER_ONLY:
        return False
    # 悬空指代：「他的」「学校的」「其他的」—— 抽成待办没有主语，等于空卡
    if len(core) <= 4 and core.endswith("的"):
        return False
    # 中文按字、英文按词、数字按位：至少 3 个信息单位
    cjk = len(re.findall(r"[\u4e00-\u9fa5]", core))
    words = len(re.findall(r"[A-Za-z]{2,}", core))
    digits = len(re.findall(r"\d", core))
    return (cjk + words + digits) >= MIN_CAPTURE_CONTENT_CHARS


@dataclass(frozen=True)
class VoiceTriggerCapture:
    id: str
    trigger_type: str  # todo, expense, idea, privacy_mute
    raw_text: str
    extracted_content: str
    metadata: dict[str, Any]
    captured_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _scan_voice_triggers(
    text: str,
    *,
    timestamp: str | None = None,
    source_label: str | None = None,
    acoustic_proximity: str | None = None,
    voice_similarity: float | None = None,
    input_context: Any = None,
    physical_pin: str | None = None,
    pin_age_seconds: float | None = None,
) -> list[VoiceTriggerCapture]:
    """Scan transcript text for immediate voice micro-commands.

    【2026-10-17】新增身份 / 近场门槛。
    实测事故：电视剧情台词「别忘了，」和「那什么事都提前算在时间，大明呢」
    被当成用户的待办口令，在看板上生成一堆 `即时抓取·待办 pending` 卡片，
    而那两条的真实属性是 ``source_label=unknown``、``acoustic_proximity=far``、
    声纹相似度 0.36 —— **根本不是本人在说话**。

    旧门槛只排除 ``non_self``，把 ``unknown``（声纹不可用 / 离得远）也放进来了。
    现在改为「必须有正向本人证据」：
      - ``source_label`` 为 self；
      - 且 ``acoustic_proximity`` 不是 far。
    调用方没传身份信息时（旧脚本 / 单测）维持原行为，不误伤。
    """
    if not text or not text.strip():
        return []

    if not is_trusted_self_voice(
        source_label=source_label,
        acoustic_proximity=acoustic_proximity,
        voice_similarity=voice_similarity,
    ):
        return []

    cleaned = text.strip()
    # [2026-11-04 UTC+8] 原来是 `datetime.now().isoformat()`：**裸墙上时间、连偏移都没有**，
    # 系统时区一变就会被下游当北京时间解析，差 8~16 小时且无从察觉。
    captured_at = timestamp or datetime.now(timezone(timedelta(hours=8))).isoformat()
    results: list[VoiceTriggerCapture] = []

    # 1. 检查隐私静音
    for pattern in _PRIVACY_PATTERNS:
        if pattern.search(cleaned):
            results.append(
                VoiceTriggerCapture(
                    id=uuid.uuid4().hex[:12],
                    trigger_type="privacy_mute",
                    raw_text=cleaned,
                    extracted_content="用户请求抹除/暂停当前段落记录",
                    metadata={"action": "discard_segment"},
                    captured_at=captured_at,
                )
            )
            return results

    # 1.4 [2026-11-06] 前台是**工作软件**时，口令词不触发（用户规则）。
    #     隐私静音已经在上面处理完（那是安全开关，永不受此影响）；
    #     记账(expense) 与灵感(idea) 也不压 —— 说到钱不能漏账。
    #     判不出来（没有时间/没有窗口/时间没时区）一律当未知 → 照常触发。
    work_app_mark = should_suppress(
        context=input_context,
        moment=timestamp,
        physical_pin=physical_pin,
        pin_age_seconds=pin_age_seconds,
    )
    suppressed = work_app_mark is not None
    if suppressed:
        logger.info(
            "口令词被工作软件前台抑制：app=%s bundle=%s text=%s",
            work_app_mark.get("app_label"), work_app_mark.get("target_bundle"), cleaned[:60],
        )

    # 1.5 [2026-10-13] 营养口令（「记录营养 菲诺厚椰乳 每100克 94千卡 …」）
    #     必须在记账之前判定：营养成分表必然带数字，先让记账的正则扫一遍容易被抢。
    for pattern in _NUTRITION_PATTERNS:
        match = pattern.search(cleaned)
        if match and not suppressed:
            parsed = parse_nutrition_label_text(match.group(1).strip())
            if parsed:
                per_100g = parsed["per_100g"]
                summary = f"{parsed['item']} 每100克 {per_100g['energy_kcal']:.0f}千卡"
                results.append(
                    VoiceTriggerCapture(
                        id=uuid.uuid4().hex[:12],
                        trigger_type="nutrition_label",
                        raw_text=cleaned,
                        extracted_content=summary,
                        metadata={
                            "action": "remember_food",
                            "library": "brand",
                            **parsed,
                        },
                        captured_at=captured_at,
                    )
                )
            else:
                # 解析不出数值（没报千卡/千焦）：不猜，落成一条待处理提示
                results.append(
                    VoiceTriggerCapture(
                        id=uuid.uuid4().hex[:12],
                        trigger_type="nutrition_label",
                        raw_text=cleaned,
                        extracted_content=match.group(1).strip(),
                        metadata={
                            "action": "needs_review",
                            "item": match.group(1).strip(),
                            "reason": "没听出可用的营养数值（至少要有千卡/千焦）",
                        },
                        captured_at=captured_at,
                    )
                )
            return results

    # 1.6 [2026-11-06] 没有口令词的连续念表：用户会直接念「首先是面的营养成分，每一百克636千焦…」。
    #     判据：出现营养成分/营养表，且至少两个营养词 + 数字。解析结果 ok 才写库，
    #     拿不准（缺名字/缺净含量/数字太少）落 needs_review，绝不猜。
    if not suppressed and ("营养成分" in cleaned or "营养表" in cleaned) and len(
        [w for w in ("蛋白质", "脂肪", "碳水", "能量", "千焦", "千卡", "糖", "钠") if w in cleaned]
    ) >= 2 and any(ch.isdigit() for ch in cleaned):
        parsed = parse_spoken_label(cleaned)
        if parsed.get("status") == "ok":
            per_100g = parsed["per_100g"]
            results.append(
                VoiceTriggerCapture(
                    id=uuid.uuid4().hex[:12],
                    trigger_type="nutrition_label",
                    raw_text=cleaned,
                    extracted_content=f"{parsed['item']} 每100克 {per_100g['energy_kcal']:.0f}千卡",
                    metadata={"action": "remember_food", "library": "brand", **parsed},
                    captured_at=captured_at,
                )
            )
        else:
            results.append(
                VoiceTriggerCapture(
                    id=uuid.uuid4().hex[:12],
                    trigger_type="nutrition_label",
                    raw_text=cleaned,
                    extracted_content=(parsed.get("item") or cleaned)[:60],
                    metadata={"action": "needs_review", **parsed},
                    captured_at=captured_at,
                )
            )
        return results

    # 2. 检查记账
    expense_triggers = ("花", "买", "付", "消费", "记账", "买单", "结账")
    if any(kw in cleaned for kw in expense_triggers):
        num_match = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|块钱|块)", cleaned)
        if num_match:
            try:
                amount = float(num_match.group(1))
                # 提取描述：将金额部分移除
                desc = cleaned[:num_match.start()] + cleaned[num_match.end():]
                # 清理常见前缀与无用虚词
                desc = re.sub(r"(?:刚才|今天|刚刚|帮我|记一笔|记账|花了|用了|付了|买单|结账|消费|买|一共|共计)", "", desc).strip("，, 。. :： ")
                if not desc:
                    desc = "消费支出"
                results.append(
                    VoiceTriggerCapture(
                        id=uuid.uuid4().hex[:12],
                        trigger_type="expense",
                        raw_text=cleaned,
                        extracted_content=f"{desc}: ¥{amount:.2f}",
                        metadata={
                            "amount": amount,
                            "currency": "CNY",
                            "item": desc,
                        },
                        captured_at=captured_at,
                    )
                )
                return results
            except ValueError:
                pass

    # 2.4 【2026-10-13】习惯：删除 / 打卡 / 新建 —— 顺序同待办（删除、打卡在前，新建在后）。
    #     整段放在待办判定**之前**，习惯与待办互不抢词。
    for pattern in _HABIT_DELETE_PATTERNS:
        match = pattern.search(cleaned)
        if match and not suppressed:
            target = match.group(1).strip("，, 。. ")
            results.append(
                VoiceTriggerCapture(
                    id=uuid.uuid4().hex[:12],
                    trigger_type="habit_delete",
                    raw_text=cleaned,
                    extracted_content=target,
                    metadata={"action": "dismiss", "target": target},
                    captured_at=captured_at,
                )
            )
            return results

    for pattern in _HABIT_DONE_PATTERNS:
        match = pattern.search(cleaned)
        if match and not suppressed:
            target = match.group(1).strip("，, 。. ")
            results.append(
                VoiceTriggerCapture(
                    id=uuid.uuid4().hex[:12],
                    trigger_type="habit_done",
                    raw_text=cleaned,
                    extracted_content=target,
                    metadata={"action": "check", "target": target},
                    captured_at=captured_at,
                )
            )
            return results

    for pattern in _HABIT_PATTERNS:
        match = pattern.search(cleaned)
        if match and not suppressed:
            target = match.group(1).strip("，, 。. ")
            results.append(
                VoiceTriggerCapture(
                    id=uuid.uuid4().hex[:12],
                    trigger_type="habit",
                    raw_text=cleaned,
                    extracted_content=target,
                    metadata={"status": "active"},
                    captured_at=captured_at,
                )
            )
            return results

    # 2.5 【2026-10-13 收口】待办删除 / 待办打卡 —— 必须在「新建待办」之前判定，
    #     否则裸「待办」会把「删除待办 X」抢成新建（实测方向相反）。
    for pattern in _TODO_DELETE_PATTERNS:
        match = pattern.search(cleaned)
        if match and not suppressed:
            target = match.group(1).strip("，, 。. ")
            results.append(
                VoiceTriggerCapture(
                    id=uuid.uuid4().hex[:12],
                    trigger_type="todo_delete",
                    raw_text=cleaned,
                    extracted_content=target,
                    metadata={"action": "dismiss", "target": target},
                    captured_at=captured_at,
                )
            )
            return results

    for pattern in _TODO_DONE_PATTERNS:
        match = pattern.search(cleaned)
        if match and not suppressed:
            target = match.group(1).strip("，, 。. ")
            results.append(
                VoiceTriggerCapture(
                    id=uuid.uuid4().hex[:12],
                    trigger_type="todo_done",
                    raw_text=cleaned,
                    extracted_content=target,
                    metadata={"action": "complete", "target": target},
                    captured_at=captured_at,
                )
            )
            return results

    # 3. 检查待办
    for pattern in _TODO_PATTERNS:
        match = pattern.search(cleaned)
        if match and not suppressed:
            todo_content = match.group(1).strip("，, 。. ")
            results.append(
                VoiceTriggerCapture(
                    id=uuid.uuid4().hex[:12],
                    trigger_type="todo",
                    raw_text=cleaned,
                    extracted_content=todo_content,
                    metadata={"status": "pending"},
                    captured_at=captured_at,
                )
            )
            break

    # 4. 检查灵感
    for pattern in _IDEA_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            idea_content = match.group(1).strip("，, 。. ")
            results.append(
                VoiceTriggerCapture(
                    id=uuid.uuid4().hex[:12],
                    trigger_type="idea",
                    raw_text=cleaned,
                    extracted_content=idea_content,
                    metadata={"tags": ["inspiration"]},
                    captured_at=captured_at,
                )
            )
            break

    # 1.7 [2026-11-06] 「查一下营养库 / 查一下 X 的营养」：只在句子里出现营养类词时走营养库，
    #     否则照旧落到第 5 步的通用查询。优先级写在 §E 报告里。
    if not suppressed and any(w in cleaned for w in ("营养", "热量", "成分", "蛋白质", "碳水", "脂肪")) and (
        "查" in cleaned or "搜" in cleaned
    ):
        reply = lookup_command_reply(cleaned)
        if reply is not None:
            results.append(
                VoiceTriggerCapture(
                    id=uuid.uuid4().hex[:12],
                    trigger_type="nutrition_lookup",
                    raw_text=cleaned,
                    extracted_content=str(reply.get("reply") or "")[:200],
                    metadata={"action": "nutrition_lookup", **reply},
                    captured_at=captured_at,
                )
            )
            return results

    # 5. 检查查询/自查
    for pattern in _QUERY_PATTERNS:
        match = pattern.search(cleaned)
        if match and not suppressed:
            query_content = match.group(1).strip("，, 。. ")
            results.append(
                VoiceTriggerCapture(
                    id=uuid.uuid4().hex[:12],
                    trigger_type="query",
                    raw_text=cleaned,
                    extracted_content=query_content,
                    metadata={"status": "pending", "query_text": query_content},
                    captured_at=captured_at,
                )
            )
            break

    # 【2026-10-17】最后一道质量闸：抽出来的内容必须有信息量。
    # 「别忘了，」后面的逗号被 strip 后剩空串；「吧」只剩一个语气词——
    # 这些都会占据看板卡片但没有任何意义，直接丢弃。
    # 注意：todo/idea/query/habit 这类需人工执行的要闸；
    # expense / nutrition 带结构化数值，不受此闸影响。
    _QUALITY_GATED = {"todo", "idea", "query", "habit", "habit_delete", "todo_delete", "todo_done", "habit_done"}
    results = [
        r for r in results
        if r.trigger_type not in _QUALITY_GATED
        or is_meaningful_capture_content(r.extracted_content)
    ]

    return results


_SOURCE_PROVENANCE_FIELDS = (
    "recording_id",
    "session_id",
    "segment_id",
    "start_offset_seconds",
    "end_offset_seconds",
    "absolute_started_at",
    "absolute_ended_at",
)

def _safe_source_id(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    if not text:
        return None
    # Never persist a filesystem path or credential-like value as an ID.
    if re.search(r"(?:/|\\|token\s*=|bearer\s+)", text, re.IGNORECASE):
        return None
    return text

def _safe_source_time(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    if not text:
        return None
    # ISO-like coordinates only; never a filesystem path.
    if re.search(r"(?:/|\\|token\s*=|bearer\s+)", text, re.IGNORECASE):
        return None
    return text

def _safe_source_offset(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 3)

def _normalize_source_provenance(source: dict[str, Any] | None) -> dict[str, Any]:
    if source is None:
        return {}
    raw = source if isinstance(source, dict) else {}
    out: dict[str, Any] = {
        "schema": "capture-source-v1",
        "time_reference": "recording_start",
        "time_unit": "seconds",
    }
    missing: list[str] = []
    for key in _SOURCE_PROVENANCE_FIELDS:
        value = raw.get(key)
        if key in {"start_offset_seconds", "end_offset_seconds"}:
            safe = _safe_source_offset(value)
        elif key in {"absolute_started_at", "absolute_ended_at"}:
            safe = _safe_source_time(value)
        else:
            safe = _safe_source_id(value)
        if safe is None:
            missing.append(key)
        else:
            out[key] = safe
    out["missing_fields"] = missing
    out["status"] = "complete" if not missing else "partial"
    return out

def scan_voice_triggers(
    text: str,
    *,
    timestamp: str | None = None,
    source_label: str | None = None,
    acoustic_proximity: str | None = None,
    voice_similarity: float | None = None,
    source_provenance: dict[str, Any] | None = None,
    input_context: Any = None,
    physical_pin: str | None = None,
    pin_age_seconds: float | None = None,
) -> list[VoiceTriggerCapture]:
    """Scan triggers, optionally attaching raw transcript coordinates.

    `source_provenance` is optional so old callers keep the exact old metadata.
    It carries coordinates only; no utterance/hash identity is created and no
    occurrence-time inference is made.
    """
    captures = _scan_voice_triggers(
        text,
        timestamp=timestamp,
        source_label=source_label,
        acoustic_proximity=acoustic_proximity,
        voice_similarity=voice_similarity,
        input_context=input_context,
        physical_pin=physical_pin,
        pin_age_seconds=pin_age_seconds,
    )
    if source_provenance is None:
        return captures
    provenance = _normalize_source_provenance(source_provenance)
    return [
        replace(capture, metadata={**capture.metadata, "source_provenance": provenance})
        for capture in captures
    ]


class QuickCaptureLockTimeout(TimeoutError):
    """等待抓取文件锁超时（**有界等待**，绝不无限阻塞）。"""


class QuickCaptureStoreError(OSError):
    """抓取文件读写失败（原子写失败 / 文件损坏）——显式失败，绝不无锁直写。"""


#: 抓取文件锁的默认超时（秒）；环境变量 VOICE_CAPTURE_LOCK_TIMEOUT 可覆盖。
DEFAULT_CAPTURE_LOCK_TIMEOUT = 5.0
#: 清理残留 tmp 的年龄阈值（秒）：只删「早就没人管」的 tmp，绝不动正在写的。
DEFAULT_CAPTURE_TMP_STALE_SECONDS = 300.0


def _capture_lock_timeout() -> float:
    raw = os.environ.get("VOICE_CAPTURE_LOCK_TIMEOUT", "")
    try:
        value = float(raw)
        return value if value > 0 else DEFAULT_CAPTURE_LOCK_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_CAPTURE_LOCK_TIMEOUT


def _capture_tmp_stale_seconds() -> float:
    raw = os.environ.get("VOICE_CAPTURE_TMP_STALE", "")
    try:
        value = float(raw)
        return value if value > 0 else DEFAULT_CAPTURE_TMP_STALE_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_CAPTURE_TMP_STALE_SECONDS


@contextlib.contextmanager
def _file_lock_with_timeout(lock_path: Path, timeout: float | None = None):
    """跨进程排它锁（flock LOCK_EX|LOCK_NB 轮询），超时抛 QuickCaptureLockTimeout。

    与 ``life_logger.atomic.file_lock`` 的区别：**有界等待**。后者是阻塞式
    ``LOCK_EX``，另一进程卡住持锁时会让 append 无限挂起（2026-10-30 独立验证实测
    >8s 不返回），因此这里自己实现带 deadline 的版本，不修改 atomic.py 的公共语义。
    """
    wait = _capture_lock_timeout() if timeout is None else timeout
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        try:
            import fcntl
        except ImportError:      # 非 POSIX：显式降级（不假装加锁），行为由调用方可见
            yield
            return
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise QuickCaptureLockTimeout(
                        f"voice_triggers: 等待抓取文件锁超时（{wait:g}s）：{lock_path}"
                    ) from None
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        handle.close()


#: [2026-10-30 降噪 v2] 告警去重 memo：key = resolve 后路径，value = (skipped, kept)。
#: 关键：**memo 更新与是否告警解耦** —— 读到 skipped==0（变干净）时必须清除该路径，
#: 否则「脏 → 修好 → 再脏」会静默不告警（独立验证窗指出的唯一确定缺陷）。
#: 不持久化：进程重启自然清空。
_CAPTURE_WARNING_MEMO: dict[str, tuple[int, int]] = {}


def _reset_capture_warning_memo() -> None:
    """测试/诊断钩子：清空告警去重 memo。"""
    _CAPTURE_WARNING_MEMO.clear()


def _should_warn_capture_skipped(path: Path, skipped: int, kept: int) -> bool:
    """是否要为本路径的本次扫描打告警。

    * skipped <= 0（文件干净）→ **清除 memo** 并返回 False（保证"再脏"能重新告警）；
    * 与上次 (skipped, kept) 相同 → 不重复告警；
    * 否则记录并告警（计数变化视为新情况）。
    """
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    if skipped <= 0:
        _CAPTURE_WARNING_MEMO.pop(key, None)
        return False
    if _CAPTURE_WARNING_MEMO.get(key) == (skipped, kept):
        return False
    _CAPTURE_WARNING_MEMO[key] = (skipped, kept)
    return True


class QuickCaptureStore:
    """Persistent storage for instant voice captures."""

    def __init__(self, storage_path: Path) -> None:
        self.storage_path = storage_path
        self._ensure_storage()

    def _lock_path(self) -> Path:
        return self.storage_path.with_suffix(".lock")

    def _tmp_glob(self) -> str:
        # atomic.write_bytes_atomic 用 mkstemp(prefix=f".{name}.", suffix=".tmp")
        return f".{self.storage_path.name}.*.tmp"

    def cleanup_stale_tmp(self, stale_seconds: float | None = None) -> list[str]:
        """删除早于阈值的残留 tmp（说明上一进程在 replace 前被杀）。

        只按 mtime 判断：正在写入的 tmp 是「刚刚」的，不会被误删；
        陈旧阈值默认 300s，可用 VOICE_CAPTURE_TMP_STALE 覆盖。
        """
        threshold = _capture_tmp_stale_seconds() if stale_seconds is None else stale_seconds
        removed: list[str] = []
        parent = self.storage_path.parent
        if not parent.exists():
            return removed
        now = time.time()
        for candidate in parent.glob(self._tmp_glob()):
            try:
                age = now - candidate.stat().st_mtime
            except OSError:
                continue
            if age < threshold:
                continue
            try:
                candidate.unlink()
                removed.append(str(candidate))
            except OSError:
                pass
        return removed

    def _ensure_storage(self) -> None:
        """首次创建空文件（**锁内二次检查**）。

        [2026-10-30 修] 原写法 `if not exists: write([])` 是 TOCTOU：
        A 进程已 append 出 1 条后，B 进程才走到这里 → 用 `[]` 覆盖整个文件，
        实测 3×50 会偶发 149/150。必须在锁内再查一次存在性。
        """
        if self.storage_path.exists():
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock_with_timeout(self._lock_path()):
            if self.storage_path.exists():
                return
            try:
                from life_logger.atomic import write_json_atomic

                write_json_atomic(self.storage_path, [])
            except Exception:
                self.storage_path.write_text("[]", encoding="utf-8")

    def list_all(self) -> list[dict[str, Any]]:
        """只读接口：**文件不存在 → []（正常初态）；损坏/读取失败 → 显式抛错**。

        [2026-10-30 读侧修] 原实现把 ``OSError`` / ``JSONDecodeError`` 一律吞成
        ``[]``：文件被截断/损坏时，``/api/quick-captures``、看板与日报的 capture
        投影会静默显示「0 条」，用户以为那天没有记录 —— 这是把 unknown 当成了 0。

        现在只有 ``FileNotFoundError`` 返回 ``[]``；其余一律抛
        ``QuickCaptureStoreError``，由调用方显式标记 unknown（绝不静默当 0 条）。
        """
        try:
            text = self.storage_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise QuickCaptureStoreError(
                f"voice_triggers: 读取抓取文件失败（{self.storage_path}）："
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not text.strip():
            raise QuickCaptureStoreError(
                f"voice_triggers: 抓取文件为空/半截（{self.storage_path}），"
                "无法区分空历史与损坏，按损坏处理"
            )
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise QuickCaptureStoreError(
                f"voice_triggers: 抓取文件损坏，无法读取（{self.storage_path}）：{exc}"
            ) from exc
        if not isinstance(doc, list):
            raise QuickCaptureStoreError(
                f"voice_triggers: 抓取文件格式异常（{self.storage_path}）：顶层不是 list"
            )
        # [2026-10-30 修] 顶层 list 但元素可能不是 dict（旧版本/外部工具写入 [1,2,3]）：
        # 下游 filter_by_date 会 item.get(...) → AttributeError，不在 local_web do_GET
        # 顶层 except 范围内 → 冒到 socketserver → 连接被断（用户看到「网络错误」）。
        # 方案 A：跳过非 dict 元素并显式告警（不抛错），保证「3 条坏元素」可区别于
        # 「本来只有 2 条」；合法条目仍可见（方案 B 会让整天历史 400 不可见）。
        good = [x for x in doc if isinstance(x, dict)]
        skipped = len(doc) - len(good)
        # 每次读都调用（干净时清除 memo），告警与否由函数决定 —— 不能再用 `if skipped and ...`
        # 短路，否则文件变干净时 memo 不更新，"脏→修好→再脏" 会静默。
        if _should_warn_capture_skipped(self.storage_path, skipped, len(good)):
            print(
                f"[voice_triggers] WARNING: 抓取文件含 {skipped} 条非对象元素，已跳过"
                f"（保留 {len(good)} 条，文件 {self.storage_path.name}）",
                file=sys.stderr,
            )
        return good

    def _read_records_for_update(self) -> list[dict[str, Any]]:
        """锁内读：不存在 → []；损坏 → 显式失败（绝不静默 [] 后覆盖历史）。"""
        try:
            text = self.storage_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise QuickCaptureStoreError(
                f"voice_triggers: 读取抓取文件失败（{self.storage_path}）：{type(exc).__name__}: {exc}"
            ) from exc
        if not text.strip():
            # 空文件 = 半截写/损坏，不是「空历史」：静默当 [] 再用新记录覆盖，
            # 会把「损坏」伪装成「正常写入」。与 list_all() 同一口径：显式失败。
            raise QuickCaptureStoreError(
                f"voice_triggers: 抓取文件为空/半截，拒绝覆盖（{self.storage_path}）"
            )
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise QuickCaptureStoreError(
                f"voice_triggers: 抓取文件损坏，拒绝覆盖（{self.storage_path}）：{exc}"
            ) from exc
        if not isinstance(doc, list):
            raise QuickCaptureStoreError(
                f"voice_triggers: 抓取文件格式异常（{self.storage_path}）：顶层不是 list"
            )
        # 同 list_all：非 dict 元素不得进入下游（append 时顺手剔除，使文件自愈）
        return [x for x in doc if isinstance(x, dict)]

    def append(self, captures: Sequence[VoiceTriggerCapture]) -> None:
        """追加抓取记录。

        并发安全 = 有界锁 + 锁内读 + 原子写：
          * 锁：``_file_lock_with_timeout``（LOCK_NB 轮询，超时抛 QuickCaptureLockTimeout）；
          * 读：**锁内**重读，避免用锁外的旧列表覆盖别人刚写的记录；
          * 写：``write_json_atomic``（tmp+fsync+replace）；失败显式抛错，
            **绝不**回退到无锁 ``write_text``（那会丢锁、丢原子性、留下半截 JSON）。
        """
        if not captures:
            return
        self.cleanup_stale_tmp()
        with _file_lock_with_timeout(self._lock_path()):
            records = self._read_records_for_update()
            seen = {
                (str(r.get("trigger_type") or ""), str(r.get("extracted_content") or ""), str(r.get("captured_at") or ""))
                for r in records
                if isinstance(r, dict)
            }
            added = 0
            for c in captures:
                d = c.to_dict()
                key = (str(d.get("trigger_type") or ""), str(d.get("extracted_content") or ""), str(d.get("captured_at") or ""))
                if key in seen:
                    continue
                seen.add(key)
                records.append(d)
                added += 1
            if added == 0:
                return
            try:
                from life_logger.atomic import write_json_atomic

                write_json_atomic(self.storage_path, records, indent=2)
            except Exception as exc:
                raise QuickCaptureStoreError(
                    f"voice_triggers: 抓取文件原子写失败（{self.storage_path}），"
                    f"本次 {added} 条未落盘：{type(exc).__name__}: {exc}"
                ) from exc

    def filter_by_date(self, date_str: str) -> list[dict[str, Any]]:
        """Return captures whose **local (Beijing)** date matches date_str.

        【2026-10-17 修】原来第一行是 ``if ts.startswith(date_str): matched.append``
        —— 这是个 bug：``captured_at`` 存的是 UTC，北京时间 10-17 00:30 落盘是
        ``2026-10-16T16:30:00+00:00``，``startswith("2026-10-16")`` 会提前命中，
        于是凌晨的即时抓取被挂到了**前一天**的看板上。
        必须一律先转本地时区再比日期，不能拿 UTC 字符串前缀当日期。
        """
        all_items = self.list_all()
        matched: list[dict[str, Any]] = []
        for item in all_items:
            ts = str(item.get("captured_at", "")).strip()
            if not ts:
                continue
            local_date = local_date_of(ts)
            if local_date == date_str:
                matched.append(item)
        return matched
