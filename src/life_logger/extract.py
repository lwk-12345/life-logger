"""Evidence-preserving baseline extraction for Chinese life-log speech."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from life_logger.domain import EventTime, LifeEvent, TranscriptSegment, validate_event
from life_logger.todo_text_cleaner import clean_command, default_polisher
from life_logger.transcript_postprocess import postprocess_transcript_text
from life_logger.voice_lexicon import TODO_ADD_TERMS

_ACTIVE_MARKERS = (
    "记录一下",
    "记一笔",
    "记个想法",
    "帮我记",
    "记录",
    "提醒我",
    "记账",
    "记一笔账",
    "记个待办",
    "记一下",
    "记个账",
    "帮我记录",
)
_FOOD_INTENTION_MARKERS = (
    "吃啥",
    "吃什么",
    "想吃",
    "准备吃",
    "打算吃",
    "先吃一个",
    "喝啥",
    "喝什么",
)
_FOOD_COMPLETION_MARKERS = (
    "吃了",
    "吃的是",
    "吃完",
    "刚吃",
    "已经吃",
    "正在吃",
    "喝了",
    "喝的是",
    "喝完",
    "刚喝",
    "已经喝",
    "摄入",
)
_FOOD_ITEMS = (
    "鸡胸肉",
    "纯牛奶",
    "牛奶",
    "鸡蛋",
    "米饭",
    "饮料",
    "粥",
    "水",
    "咖啡",
    "茶",
    "炸鸡",
    "洋葱",
)
_WORK_MARKERS = (
    "开会",
    "项目",
    "沟通",
    "同事",
    "电话",
    "会议",
    "汇报",
    "断网",
    "修复失败",
    "修复",
    "切换工作",
    "工作安排",
    "工作内容",
    "工作任务",
)
_WORK_ACTION_MARKERS = (
    "开会",
    "会议",
    "沟通",
    "电话",
    "汇报",
    "修复",
    "提交",
    "完成",
    "测试",
    "开发",
    "修改",
    "处理",
    "上线",
    "部署",
    "面试",
    "评审",
    "排查",
)
_IDEA_MARKERS = ("想法", "我的想法", "想到", "我觉得")
_PURCHASE_MARKERS = (
    "消费",
    "花钱",
    "买了",
    "刚买",
    "已经买",
    "买完",
    "付款",
    "支付",
    "付了",
    "扫了",
    "转了",
    "刷了",
    "扣了",
    "买烟",
    "买包烟",
    "买香烟",
    "拿一包",
    "拿包",
    "来一包",
    "来包",
    "拿一盒",
    "来一盒",
    "买一包",
)
_CATEGORY_ORDER = (
    "routine",
    "expense",
    "food",
    "exercise",
    "work_communication",
    "idea",
    "todo",
)
_NARRATIVE_PAST_MARKERS = (
    "以前",
    "过去",
    "当时",
    "曾经",
    "那时候",
    "那会儿",
    "那阵子",
    "早先",
    "早前",
    "早些年",
    "去年",
    "上个月",
    "几年前",
    "记得那时候",
    "我记得当时",
    "原来买",
    "之前买",
    "之前花",
    "以前买",
    "以前花",
    "以前去",
    "之前去",
)
_NARRATIVE_CLEFT_PATTERN = re.compile(
    r"(?:我|我们|他|他们|她|她们)(?:当时|以前|之前)?是\s*[^，。！？!?]{1,40}?(?:买|花|付|购|充|兑换|淘|办)(?:了|过)?[^，。！？!?]{0,40}?的"
)
_COMPARISON_INQUIRY_MARKERS = (
    "划算吗",
    "划不划算",
    "划算不",
    "值不值",
    "值不值得",
    "贵不贵",
    "便宜吗",
    "是不是贵了",
    "值当吗",
    "贵了没有",
    "贵不",
)


def is_narrative_past_or_inquiry(text: str) -> tuple[bool, str]:
    """Detect historical narrative, reminiscing or price inquiries that should NOT be daily expenses.

    Returns (is_narrative, subtype) where subtype is 'experience_sharing' or 'curiosity_question'.
    """
    for marker in _COMPARISON_INQUIRY_MARKERS:
        if marker in text:
            return True, "curiosity_question"

    if _NARRATIVE_CLEFT_PATTERN.search(text):
        return True, "experience_sharing"

    for marker in _NARRATIVE_PAST_MARKERS:
        if marker in text:
            if any(k in text for k in ("买", "花", "付", "购", "充", "元", "块", "钱", "消费", "开通", "吃", "喝")):
                return True, "experience_sharing"

    return False, ""


_AMBIENT_POS_OR_CLERK_PATTERNS = (
    re.compile(r"请(?:解锁手机|出示(?:付款|二维)?码|刷卡|碰一下)"),
    re.compile(r"这(?:[个包瓶杯份串]|四个|三个|两个|一).*谁的"),
    re.compile(r"(?:谁的|哪位(?:顾客)?的)"),
    re.compile(r"(?:可以付了|一共(?:是)?[¥￥]?\d+).*谁的"),
    re.compile(r"下一位(?:顾客)?"),
)


def is_ambient_pos_or_clerk_utterance(text: str) -> bool:
    """Detect cashier announcements, POS prompts, and clerk questions to others."""
    cleaned = text.strip()
    if not cleaned:
        return False
    if any(m in cleaned for m in ("我要", "给我拿", "给我来", "帮我拿", "我想买", "我付了", "我花了", "我买了")):
        return False
    return any(p.search(cleaned) for p in _AMBIENT_POS_OR_CLERK_PATTERNS)


MIN_STRUCTURED_EVENT_CONFIDENCE = 0.90
_CHINESE_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _number(value: str) -> int | None:
    normalized = value.replace(",", "")
    if normalized.isdigit():
        return int(normalized)
    if not normalized or any(
        char not in _CHINESE_DIGITS and char not in {"十", "百", "千"}
        for char in normalized
    ):
        return None
    if all(char in _CHINESE_DIGITS for char in normalized):
        if len(normalized) == 1:
            return _CHINESE_DIGITS[normalized]
        return int("".join(str(_CHINESE_DIGITS[char]) for char in normalized))
    total = 0
    current = 0
    for char in normalized:
        if char in _CHINESE_DIGITS:
            current = _CHINESE_DIGITS[char]
            continue
        unit = {"十": 10, "百": 100, "千": 1000}[char]
        total += (current or 1) * unit
        current = 0
    return total + current


def _event_id(segment: TranscriptSegment, title: str, categories: list[str]) -> str:
    raw = "|".join((segment.recording_id, segment.segment_id, title, *categories))
    return f"evt_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def _is_abandoned_purchase_choice(text: str) -> bool:
    if "算了" not in text:
        return False
    if not any(marker in text for marker in ("拿", "换口味", "换一个", "不要")):
        return False
    return not any(
        marker in text
        for marker in ("花了", "花费", "买了", "刚买", "已经买", "付款", "支付")
    )


def _amount(text: str) -> float | None:
    if _is_abandoned_purchase_choice(text):
        return None
    # Exclude returned change or loose coins without payment action
    if any(k in text for k in ("零钱", "找零", "找你", "找您")) and not any(
        k in text for k in ("花了", "花费", "付了", "付款", "支付", "实付", "一共", "总共", "买了", "消费", "扫了", "转了")
    ):
        return None
    number_pattern = (
        r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|"
        r"\d+(?:\.\d+)?|"
        r"[零一二两三四五六七八九十百千]+"
    )
    excluded_price_context = re.compile(
        r"(?:优惠券|优惠|立减|减了|订单编号|订单号|编号|版本号|型号)\s*$"
    )
    non_monetary_piece_prefix = re.compile(
        r"(?:这|那|哪|同|成|每|上|下|各|整|另|大|小|在|分|碎|切)\s*$"
    )
    together_suffix = re.compile(
        r"^\s*(?:儿|去|来|走|玩|商量|讨论|开会|住|生活|共|同|并|看|听|聊|做|办|过)"
    )
    spending_context = re.compile(
        r"(?:花|付|收|扣|扫|转|买|给|折|价格|总价|实付|费用|一共|总共)"
    )

    def excluded(match: re.Match[str]) -> bool:
        prefix = text[max(0, match.start() - 12) : match.start()]
        return excluded_price_context.search(prefix) is not None

    preferred = re.search(
        rf"(?:实付|实际支付|总价|总共|合计|一共|折后(?:价)?|到手价|最终(?:价格|价)?)"
        rf"\s*(?:为|是|：|:)?\s*(?:[¥￥]\s*)?(?P<value>{number_pattern})"
        rf"\s*(?:元|块|块钱)?",
        text,
    )
    if preferred:
        raw_value = preferred.group("value").replace(",", "")
        value = _number(raw_value)
        return float(value) if value is not None else float(raw_value)

    paid = re.search(
        rf"(?:实付|实际支付|支付了?|付款了?|付了?|扫了?|转了?|刷了?|扣了?|花了?)\s*"
        rf"(?:[¥￥]\s*)?(?P<value>{number_pattern})\s*元",
        text,
    )
    if paid:
        raw_value = paid.group("value").replace(",", "")
        value = _number(raw_value)
        return float(value) if value is not None else float(raw_value)

    for match in re.finditer(
        rf"(?P<yuan>{number_pattern})\s*块(?:钱)?"
        rf"(?:(?P<jiao>{number_pattern})\s*(?:角|毛))?"
        rf"(?:(?P<fen>{number_pattern})\s*分)",
        text,
    ):
        if excluded(match):
            continue
        yuan = _number(match.group("yuan"))
        jiao = _number(match.group("jiao")) if match.group("jiao") else 0
        fen = _number(match.group("fen")) if match.group("fen") else 0
        if yuan is not None and jiao is not None and fen is not None:
            return float(yuan) + float(jiao) / 10 + float(fen) / 100

    for match in re.finditer(
        rf"(?P<yuan>{number_pattern})\s*块(?:钱)?"
        rf"(?:(?P<jiao>{number_pattern})\s*(?:毛)?)?",
        text,
    ):
        if excluded(match):
            continue
        prefix = text[max(0, match.start() - 12) : match.start()]
        suffix = text[match.end() : min(len(text), match.end() + 10)]
        yuan = _number(match.group("yuan"))
        if yuan is None:
            try:
                yuan = float(match.group("yuan"))
            except ValueError:
                continue
        jiao_text = match.group("jiao")
        jiao = _number(jiao_text) if jiao_text else None
        if jiao is None and jiao_text:
            try:
                jiao = int(float(jiao_text))
            except ValueError:
                jiao = None
        if (yuan == 1 or yuan == 1.0) and jiao is None and "块钱" not in match.group(0):
            if non_monetary_piece_prefix.search(prefix):
                continue
            if together_suffix.search(suffix) and not spending_context.search(prefix):
                continue
        return float(yuan) + (float(jiao) / 10 if jiao is not None else 0.0)
    match = re.search(
        rf"(?P<yuan>{number_pattern})\s*元"
        rf"(?:(?P<jiao>{number_pattern})\s*(?:角|毛))?"
        rf"(?:(?P<fen>{number_pattern})\s*分)?",
        text,
    )
    if match:
        if excluded(match):
            return None
        yuan = _number(match.group("yuan"))
        jiao = _number(match.group("jiao")) if match.group("jiao") else 0
        fen = _number(match.group("fen")) if match.group("fen") else 0
        if yuan is not None and jiao is not None and fen is not None:
            return float(yuan) + float(jiao) / 10 + float(fen) / 100
    match = re.search(
        rf"(?P<jiao>{number_pattern})\s*(?:角|毛)(?:钱)?"
        rf"(?:(?P<fen>{number_pattern})\s*分)?",
        text,
    )
    if match:
        if excluded(match):
            return None
        jiao = _number(match.group("jiao"))
        fen = _number(match.group("fen")) if match.group("fen") else 0
        if jiao is not None and fen is not None:
            return float(jiao) / 10 + float(fen) / 100
    match = re.search(rf"(?:[¥￥]\s*)?(?P<value>{number_pattern})\s*(?:元)", text)
    if match:
        if excluded(match):
            return None
        raw_value = match.group("value").replace(",", "")
        value = _number(raw_value)
        return float(value) if value is not None else float(raw_value)
    match = re.search(rf"[¥￥]\s*(?P<value>{number_pattern})", text)
    if match:
        if excluded(match):
            return None
        raw_value = match.group("value").replace(",", "")
        value = _number(raw_value)
        return float(value) if value is not None else float(raw_value)
    match = re.search(
        rf"(?:买了|刚买|已经买|买完|购买)\s*"
        rf"[^，,。！？!?；;]{{1,40}}[，,]\s*"
        rf"(?:[¥￥]\s*)?(?P<value>{number_pattern})"
        rf"(?=\s*(?:元|块|块钱)?(?:[，,。！？!?；;]|$|然后|之后))",
        text,
    )
    if match:
        if excluded(match):
            return None
        raw_value = match.group("value").replace(",", "")
        value = _number(raw_value)
        return float(value) if value is not None else float(raw_value)
    match = re.search(
        rf"(?:花了?|花费|消费|支付(?:了)?|付款(?:了)?|付了?|扫了?|转了?|刷了?|扣了?)\s*"
        rf"(?:[¥￥]\s*)?(?P<value>{number_pattern})(?!\s*(?:分钟|分|小时|天|公里|千米|米))"
        rf"(?=\s*[，,、]?\s*(?:买|购|拿|入手))",
        text,
    )
    if not match:
        return None
    raw_value = match.group("value").replace(",", "")
    value = _number(raw_value)
    return float(value) if value is not None else float(raw_value)


def _duration_minutes(text: str) -> int | None:
    match = re.search(r"(\d+|[一二两三四五六七八九十]+)\s*(?:分钟|分)", text)
    return _number(match.group(1)) if match else None


def _weight_grams(text: str) -> int | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:克|g)", text, flags=re.IGNORECASE)
    return int(float(match.group(1))) if match else None


def _quantity_and_unit(text: str) -> tuple[int | None, str | None]:
    match = re.search(
        r"(\d+|[一二两三四五六七八九十]+)\s*(瓶|杯|个|碗|份)",
        text,
    )
    if not match:
        return None, None
    return _number(match.group(1)), match.group(2)


def _food_items(text: str) -> list[dict[str, Any]]:
    candidates = _FOOD_ITEMS
    number_pattern = r"\d+|[零一二两三四五六七八九十]+"
    unit_pattern = r"瓶|杯|个|碗|份"
    matches: list[tuple[int, dict[str, Any]]] = []
    for item in candidates:
        pattern = (
            rf"(?P<quantity>{number_pattern})?\s*"
            rf"(?P<unit>{unit_pattern})?\s*{item}"
        )
        match = re.search(pattern, text)
        if not match:
            continue
        quantity = _number(match.group("quantity")) if match.group("quantity") else None
        value: dict[str, Any] = {"name": item}
        if quantity is not None:
            value["quantity"] = quantity
        if match.group("unit"):
            value["unit"] = match.group("unit")
        matches.append((match.start(), value))
    matches.sort(key=lambda pair: pair[0])
    return [value for _position, value in matches]


def _is_food_intake(text: str, items: list[dict[str, Any]]) -> bool:
    """Require evidence of intake or a concrete food item, not mere intent."""

    if items:
        return True
    if any(marker in text for marker in _FOOD_COMPLETION_MARKERS):
        return True
    return False


def _food_item(text: str) -> str | None:
    items = _food_items(text)
    return items[0]["name"] if items else None


def _purchase_item(text: str, title: str) -> str:
    food = _food_item(text)
    if food:
        return food
    if "短支的乐途" in text:
        return "短支的乐途"
    if "乐途" in text and any(marker in text for marker in ("包", "烟", "买", "花")):
        return "乐途"
    if "红塔山" in text:
        return "红塔山"
    if "河马" in text and any(marker in text for marker in ("买", "花")):
        return "河马"
    if "芒果" in text and any(marker in text for marker in ("包", "烟", "买", "拿", "来", "盒")):
        return "芒果香烟（10元档）"
    purchase_item = re.search(
        r"(?:买了|刚买|已经买|买完|购买(?:了)?|买的|买)\s*"
        r"(?:(?:一个|一件|一台|一只|一瓶|一包|个|件|台|只|瓶|包)\s*)?"
        r"(?:(?:\d+|[一二两三四五六七八九十百千]+)\s*(?:元|块|块钱)?\s*的\s*)?"
        r"(?P<item>[^，,。！？!?；;]{1,30})",
        text,
    )
    if purchase_item:
        item = purchase_item.group("item").strip()
        item = re.sub(r"\s*(?:[¥￥]?\s*\d+(?:\.\d+)?).*$", "", item).strip()
        if item:
            if item == "烟":
                return "香烟"
            return item
    if "烟" in text:
        return "香烟"
    return title


def _purchase_display_title(
    title: str,
    *,
    amount: float | None,
    item: str,
) -> str:
    if amount is not None and item == "短支的乐途":
        value = f"{amount:g}"
        return f"花了{value}元，买了一包「短支的乐途」"
    if item == "芒果香烟（10元档）":
        return "消费支出：购买芒果香烟（10元档）"
    return title


def _spoken_time(
    text: str,
    absolute_reference: str | None,
) -> tuple[str | None, str | None]:
    number_pattern = r"\d{1,2}|[零一二两三四五六七八九十]+"
    match = re.search(
        rf"(?P<period>凌晨|早上|上午|中午|下午|晚上)?"
        rf"(?P<hour>{number_pattern})点"
        rf"(?:(?P<minute>{number_pattern})分?|(?P<half>半))?",
        text,
    )
    if not match:
        return None, None
    prefix = text[: match.start()].rstrip()
    if prefix.endswith(("块", "块钱", "元")):
        return None, None
    hour = _number(match.group("hour"))
    minute = 30 if match.group("half") else _number(match.group("minute") or "零")
    if hour is None or minute is None or hour > 23 or minute > 59:
        return None, None
    period = match.group("period")
    if period in {"下午", "晚上"} and hour < 12:
        hour += 12
    if period == "凌晨" and hour == 12:
        hour = 0
    reported = f"{hour:02d}:{minute:02d}"
    if absolute_reference is None:
        return reported, None
    try:
        reference = datetime.fromisoformat(absolute_reference)
    except ValueError:
        return reported, None
    absolute = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return reported, absolute.isoformat()


def _title(text: str) -> str:
    cleaned = re.sub(r"^(记录一下|记一笔|记个想法|帮我记)[，,：:\s]*", "", text)
    return cleaned.rstrip("。！？!?；;，,").strip() or text.strip()


def _is_actionable_todo(
    text: str,
    physical_pin: str | None = None,
    pin_age_seconds: float | None = None,
    *,
    input_context: Any = None,
    moment: str | None = None,
    context_root: Any = None,
) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False

    # [2026-11-06] 前台是工作软件（Alma/终端/Codex/Xcode/Antigravity）时口令词不触发。
    # 判不出来（没有上下文/时间无时区/那一刻没窗口）一律当未知 → 保持原行为。
    # 「捏两下」60 秒窗口内的显式指令不受影响（should_suppress 内部已豁免）。
    from life_logger.work_apps import should_suppress

    if should_suppress(
        context=input_context,
        moment=moment,
        root=context_root,
        physical_pin=physical_pin,
        pin_age_seconds=pin_age_seconds,
    ) is not None:
        return False

    # 1. Negative hard exclusions: questions, negations, recollections, conversational chatter
    if any(
        m in cleaned
        for m in (
            "我记得",
            "还记得",
            "记不记得",
            "记得当时",
            "当时记得",
            "印象中",
            "记忆中",
            "你里面记得",
            "你要记得",
            "只记得",
            "不需要",
            "没必要",
            "用不着",
            "不用",
            "谁可以",
            "谁能",
            "谁更",
            "难道",
            "怎么",
            "为什么",
            "不知为何",
            "不知道为啥",
            "需要我的时候",
            "需要的时候",
            "一个需要",
            "种需要",
            "按需要",
            "根据需要",
            "好不好",
            "行不行",
            "可以吗",
            "是不是",
        )
    ):
        return False
    if cleaned.rstrip().endswith(("吗", "呢", "？", "?", "好不好", "可以吗")):
        return False
    if any(
        m in cleaned
        for m in ("当时是做", "已经做", "早就做", "之前做过", "以前做过")
    ):
        return False

    # 【2026-10-17 核心升级】物理手势「捏捏两次」(double_pinch) 1 分钟豁免窗口
    # 当用户使用捏捏手势激活后 60 秒内，与语音口令词同等提权：
    # 哪怕完全不说「记一下/提醒我」，命令句与意向直接升级为最高优先级 todo！
    if physical_pin == "double_pinch" and (pin_age_seconds is None or pin_age_seconds <= 60.0):
        return True

    # 2. 待办升级的**唯一开关**：口令词。
    #
    # [2026-10-13 收口] 原先这里还有三个分支（「记得+动词」「需要+动词」
    # 「明天要+动词」）也直接判 todo。实测它们每天堆出几十条无人管的待办
    # （用户 10-12 单日 253 条）。用户明确要求：只有口令词才算待办，
    # 其他一律记成想法。那三个分支已移到 _is_future_intent()，由调用处降级为 idea。
    return any(m in cleaned for m in TODO_ADD_TERMS)


def _is_future_intent(text: str) -> bool:
    """随口说的未来意向（「记得…」「需要…」「明天要…」）→ 记为想法(idea)，不是待办。

    [2026-10-13 收口] 本函数是从 _is_actionable_todo 拆出来的原分支 3/4/5。
    这些是「意向」而非「交代下来的事」；进待办会让列表爆炸，记为 idea 既保留
    又不污染待办列表。
    """
    cleaned = text.strip()
    if not cleaned:
        return False
    # 问句不当作意向
    if cleaned.rstrip().endswith(("吗", "呢", "？", "?", "好不好", "可以吗")):
        return False
    # 未来的祈使 / 义务 / 计划
    if re.search(
        r"(?:^|[，。！？\s])(?:明天|后天|下周|回头|待会儿|务必|千万)?记得(?:去|把|买|带|联系|发|做|交|换|修|关|开|拿|填|订|寄|查|办|打|写|收)",
        cleaned,
    ):
        return True
    if re.search(
        r"(?:^|[，。！？\s])(?:我|我们|咱们|今天|明天|之后|下周|待会儿|回头)?(?:还)?(?:真|确实)?需要(?:去|把|做|买|处理|联系|提交|完成|更新|检查|确认|准备|安排|交|办|寄|修|订|报销)",
        cleaned,
    ):
        return True
    if re.search(
        r"(?:^|[，。！？\s])(?:明天|后天|下周|今天必须|务必)要(?:去|做|买|交|发|联系|办|写|开)",
        cleaned,
    ):
        return True
    return False


def _ordered_categories(categories: set[str]) -> list[str]:
    return [category for category in _CATEGORY_ORDER if category in categories]


def _is_meaningful_work_event(text: str) -> bool:
    """Avoid promoting generic project chatter into a formal life event."""

    if any(marker in text for marker in _WORK_ACTION_MARKERS):
        return True
    if any(marker in text for marker in ("同事", "客户")) and any(
        marker in text for marker in ("说", "聊", "问", "回复", "联系")
    ):
        return True
    return False


class RuleBasedExtractor:
    """Extract high-signal events without inventing unsupported activity."""

    def extract(self, segment: TranscriptSegment) -> list[LifeEvent]:
        raw_text = segment.text.strip()
        if not raw_text:
            return []
        postprocess = postprocess_transcript_text(raw_text)
        text = postprocess.text.strip()

        active = any(marker in text for marker in _ACTIVE_MARKERS)
        matched: set[str] = set()
        data: dict[str, Any] = {}
        activity_type: str | None = None
        title = _title(text)
        reported_time, spoken_absolute = _spoken_time(
            text,
            segment.absolute_started_at,
        )

        is_narrative, narrative_subtype = is_narrative_past_or_inquiry(text)

        if any(
            keyword in text
            for keyword in ("起床", "睡觉", "入睡", "午睡", "出门", "回家")
        ):
            matched.add("routine")
            activity_type = "waking_up" if "起床" in text else "routine_observation"
            data["routine"] = {}
            if reported_time:
                data["routine"]["reported_time"] = reported_time

        items = _food_items(text)
        if (
            any(
                keyword in text
                for keyword in (
                    "吃",
                    "喝",
                    "早餐",
                    "午餐",
                    "晚餐",
                    "饮料",
                    "鸡蛋",
                    "鸡胸肉",
                    "米饭",
                    "粥",
                    "水",
                    "咖啡",
                    "茶",
                    "炸鸡",
                    "洋葱",
                )
            )
            and _is_food_intake(text, items)
            and not (is_narrative and not active)
            and not is_ambient_pos_or_clerk_utterance(text)
        ):
            matched.add("food")
            food_data: dict[str, Any] = {}
            item = items[0]["name"] if items else None
            quantity, unit = _quantity_and_unit(text)
            weight = _weight_grams(text)
            if items:
                food_data["items"] = items
            if item:
                food_data["item"] = item
            if quantity is not None and len(items) <= 1:
                food_data["quantity"] = quantity
                food_data["unit"] = unit
            if weight is not None:
                food_data["weight_grams"] = weight
            data["food"] = food_data
            activity_type = activity_type or "meal"

        amount = _amount(text)
        if (amount is not None or any(keyword in text for keyword in _PURCHASE_MARKERS)) and not is_ambient_pos_or_clerk_utterance(text):
            if is_narrative and not active:
                matched.add("idea")
                activity_type = narrative_subtype
                data["idea"] = {
                    "kind": narrative_subtype,
                    "historical": True,
                }
            else:
                matched.add("expense")
                purchase_item = _purchase_item(text, title)
                if amount is None and purchase_item == "芒果香烟（10元档）":
                    amount = 10.0
                data["expense"] = {
                    "amount": amount,
                    "currency": "CNY",
                    "item": purchase_item,
                }
                title = _purchase_display_title(
                    title,
                    amount=amount,
                    item=purchase_item,
                )
                if _food_item(text) == "饮料":
                    activity_type = "purchase_drink"
                else:
                    activity_type = activity_type or "purchase"

        if any(
            keyword in text
            for keyword in ("跑步", "走路", "步行", "健身", "运动", "瑜伽", "骑车")
        ):
            matched.add("exercise")
            if "跑步" in text:
                activity_type = "running"
            elif "走路" in text or "步行" in text:
                activity_type = "walking"
            else:
                activity_type = "exercise"
            data["exercise"] = {"duration_minutes": _duration_minutes(text)}

        if any(
            keyword in text for keyword in _WORK_MARKERS
        ) and _is_meaningful_work_event(text):
            matched.add("work_communication")
            activity_type = (
                "meeting" if "开会" in text or "会议" in text else "work_discussion"
            )

        if (
            "戴耳机" in text
            and "直播" in text
            and any(keyword in text for keyword in ("避免", "减少", "误触"))
        ):
            matched.add("idea")
            activity_type = "personal_strategy"
            data["idea"] = {"kind": "personal_strategy", "future": True}

        has_explicit_idea = any(keyword in text for keyword in _IDEA_MARKERS)
        has_concrete_event = bool(
            matched.intersection(
                {
                    "routine",
                    "food",
                    "expense",
                    "exercise",
                    "work_communication",
                }
            )
        )
        if (has_explicit_idea or (is_narrative and not active)) and not has_concrete_event:
            matched.add("idea")
            activity_type = activity_type or narrative_subtype or "idea"
            data.setdefault("idea", {})
            if narrative_subtype:
                data["idea"]["kind"] = narrative_subtype
                data["idea"]["historical"] = True

        pin_type = getattr(segment, "physical_pin", None) or getattr(segment, "pin_type", None)
        pin_age = getattr(segment, "pin_age_seconds", None)
        segment_context = (
            segment.get("input_context") if isinstance(segment, dict)
            else getattr(segment, "input_context", None)
        )
        segment_moment = (
            segment.get("absolute_started_at") if isinstance(segment, dict)
            else getattr(segment, "absolute_started_at", None)
        )
        if _is_actionable_todo(
            text,
            physical_pin=pin_type,
            pin_age_seconds=pin_age,
            input_context=segment_context,
            moment=segment_moment,
        ):
            # 【2026-10-15】口令词之后的这段口述，先过一遍清洗再入库。
            # 为什么要它：用户说口令词之后，句子里常带口水词、口吃、语气助词
            # （「呃…我那个…就是…明天要要拿快递吧」），原样进待办会一路带到
            # 小苏和 Ai Passport 屏幕上。用本地 Ollama qwen2.5:7b 整理成一句干净的话。
            #
            # 触发条件极窄（只有带 TODO_ADD_TERMS 口令词的文本才走到这里），
            # 一天几条，实测 1.5s/条，开销可忽略。
            # 模型没起来/超时/返回空 → clean_command 自动退回原文，绝不丢事。
            cleaned_title = clean_command(title, polisher=default_polisher()).text
            matched.add("todo")
            activity_type = "reminder"
            data["todo"] = {"text": cleaned_title}
        elif _is_future_intent(text):
            # [2026-10-13 收口] 「记得…/需要…/明天要…」是随口意向，记为想法而非待办。
            # 待办列表因此只保留用户用口令明确交代的事。
            matched.add("idea")
            activity_type = activity_type or "idea"
            data["idea"] = {"kind": "future_intent", "text": title}

        categories = _ordered_categories(matched)
        if not categories:
            return []
        for category in categories:
            data.setdefault(category, {})

        food_data = data.get("food") if isinstance(data.get("food"), dict) else {}
        food_items = food_data.get("items") if isinstance(food_data, dict) else []
        has_quantified_food = bool(
            food_data.get("weight_grams")
            or food_data.get("quantity")
            or any(
                isinstance(item, dict) and item.get("quantity") is not None
                for item in (food_items if isinstance(food_items, list) else [])
            )
        )
        exercise_data = (
            data.get("exercise") if isinstance(data.get("exercise"), dict) else {}
        )
        if active and {"expense", "food"}.issubset(categories):
            confidence = 0.96
        elif "expense" in categories and amount is not None:
            confidence = 0.95
        elif "idea" in categories and narrative_subtype:
            confidence = 0.92
        elif isinstance(data.get("idea"), dict) and data["idea"].get("kind") == "future_intent":
            # [2026-10-13 收口] 「记得…/需要…/明天要…」降级为想法后必须仍能被保留。
            # 不给这个分支的话，它们会落进 else 的 0.72，低于 0.90 门槛被整条丢弃 ——
            # 那就从「记成想法」变成了「彻底消失」，与用户要求相反。
            confidence = 0.92
        elif active or "todo" in categories:
            confidence = 0.93
        elif "routine" in categories and reported_time:
            confidence = 0.93
        elif (
            "exercise" in categories
            and exercise_data.get("duration_minutes") is not None
        ):
            confidence = 0.92
        elif "food" in categories and has_quantified_food:
            confidence = 0.92
        else:
            confidence = 0.72
        if postprocess.needs_review:
            confidence = min(confidence, 0.55)
        if confidence < MIN_STRUCTURED_EVENT_CONFIDENCE:
            return []
        evidence: dict[str, Any] = {
            "recording_id": segment.recording_id,
            "segment_id": segment.segment_id,
            "start_offset_seconds": segment.start_offset_seconds,
            "end_offset_seconds": segment.end_offset_seconds,
            "absolute_started_at": segment.absolute_started_at,
            "absolute_ended_at": segment.absolute_ended_at,
            "quote": text,
        }
        if active:
            evidence["active_direct"] = True
        else:
            evidence["passive_candidate"] = True
            if "expense" in categories:
                evidence["review_required"] = True
        if text != raw_text:
            evidence["raw_quote"] = raw_text
        if postprocess.corrections:
            evidence["text_corrections"] = list(postprocess.corrections)
        if postprocess.review_reasons:
            evidence["review_required"] = True
            evidence["review_reasons"] = list(postprocess.review_reasons)
        event = LifeEvent(
            event_id=_event_id(segment, title, categories),
            categories=categories,
            activity_type=activity_type,
            title=title,
            time=EventTime(
                absolute=spoken_absolute or segment.absolute_started_at,
                relative_seconds=segment.start_offset_seconds,
                precision=(
                    "exact"
                    if spoken_absolute or segment.absolute_started_at
                    else "relative"
                ),
            ),
            data=data,
            capture_mode="active" if active else "passive",
            source_type="audio",
            confidence=confidence,
            evidence=evidence,
            review_status="unreviewed",
        )
        validate_event(event.to_dict())
        return [event]
