"""饮食记录的餐次、复述合并与跨日归属规则（2026-11-06 Q912 + Q911 A/B/C/D）。

为什么单独一个模块
------------------
`build_nutrition_ledger()` 原来把「哪一餐 / 是不是同一顿 / 是不是回顾」全部交给
`food_data["meal"]` 和标题里的关键字，导致四类真实错误：

* 餐次取的是**说话时刻**而不是原话（下午说「中午吃了汉堡」被算成加餐）；
* 同一顿被复述两次就记两笔（10-26 汉堡 480 + 450）；
* 整餐 + 配料各记一笔（10-27 土豆/腊肠/毛豆 算 5 次）；
* 「昨天晚上我吃了…」算到今天。

本模块只做**规则判断**（纯函数，无 I/O、不查库、不改事件），
热量折算仍留在 `food_nutrition` / `food_lookup`。

用户口径（Q912，逐条对应）：

1. 餐次以原话为准，没说的进「未说明」，不猜成加餐；
2. 多次汇报累加（中午、晚上各算一次；同一种食物不同餐次算两次）；
3. **只有同一餐的复述才合并**；后面补克数=补充同一条；「又吃了/再来一个」=新的一条；
   拿不准 → 进「需要我判断」，不静默合并也不静默相加；
4. 跨日回顾（「昨天晚上吃了」）归昨天，不算今天；
5. 改口以最后说的为准。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

__all__ = [
    "MEAL_KEYS",
    "mark_retrospective_clusters",
    "assign_meal_by_context",
    "portion_grams",
    "grams_from_text",
    "meal_from_text",
    "is_retrospective",
    "is_additive",
    "is_generic_item",
    "is_plan_or_discussion",
    "normalize_item",
    "name_core",
    "split_composite",
    "dedupe_same_meal",
]

#: 合法的餐次键（``unknown`` = 未说明，绝不落到 snacks）。
MEAL_KEYS = ("breakfast", "lunch", "dinner", "snacks", "unknown")

_BREAKFAST = re.compile(r"早饭|早餐|早上|早晨|清晨|上午")
_LUNCH = re.compile(r"午饭|午餐|中午|晌午|中饭")
_DINNER = re.compile(r"晚饭|晚餐|晚上|傍晚|夜里|晚间|晚饭前")
_SNACK = re.compile(r"加餐|夜宵|宵夜|零食|下午茶|嘴馋")

#: 回顾用语：这条记录描述的是**更早**的一天。
_RETRO = re.compile(r"昨天|昨晚|昨日|前天|大前天|上一?天|隔天")

#: 「又吃了一份」= 新的一条，不是复述。
_ADDITIVE = re.compile(
    r"又吃|又喝|又点|又买|又加|再吃|再喝|再来|再来一|再点|再加|还要|还能再|第二份|第二杯|"
    r"另一个|另一份|另外一个|接着吃|继续吃|追加"
)

#: 泛指词：不是具体食物，不能进合计。
_GENERIC_ITEMS = frozenset({
    "食物", "这个东西", "东西", "这个", "那个", "这些", "那些", "一份食物",
    "蘸料食物", "蘸料", "吃的", "喝的", "餐", "饭", "菜", "食物1份",
})

#: 讨论 / 采购 / 计划 / 询问：不是"已经吃下去"。
_NON_INTAKE = re.compile(
    r"讨论|采购|计划|打算|准备|打算买|想买|想吃|想喝|要买|去买|买了|"
    r"每天|每日|一天|一周|一个月|以后|下次|回头|考虑|建议|询问|问问|多少钱|"
    r"补酶|补剂|囤|储备"
)

#: 中文里常见的「做法/状态」修饰，不改变食物本身。
_DECORATIONS = (
    "清蒸", "红烧", "凉拌", "水煮", "干煸", "爆炒", "清炒", "小炒",
    "蒸", "煮", "炒", "烤", "煎", "炸", "炖", "焖", "卤", "拌", "焯",
    "熟的", "生的", "熟", "生", "好", "的",
)

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def _cn_number(text: str) -> float | None:
    """把「三百」「五十」「一百二十」「十八点儿六」「七点八」「半」解析成数字；解析不了返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw == "半":
        return 0.5
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return float(raw)
    if "点" in raw:
        parts = re.split(r"点儿?", raw, maxsplit=1)
        if len(parts) == 2 and parts[0] and parts[1]:
            int_val = 0.0 if parts[0] in ("零", "〇", "0") else _cn_number(parts[0])
            if int_val is not None:
                frac_digits: list[str] = []
                for ch in parts[1]:
                    if ch in _CN_DIGITS:
                        frac_digits.append(str(_CN_DIGITS[ch]))
                    elif ch.isdigit():
                        frac_digits.append(ch)
                    else:
                        return None
                if frac_digits:
                    return float(f"{int(int_val)}.{''.join(frac_digits)}")
        return None
    total, section, pending = 0, 0, None
    for char in raw:
        if char in _CN_DIGITS:
            pending = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            section += (1 if pending is None else pending) * _CN_UNITS[char]
            pending = None
        else:
            return None
    if pending is not None:
        section += pending
    total += section
    return float(total) if total > 0 else None


_GRAMS_NEAR = re.compile(
    r"(半|\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+(?:点儿?[零〇一二三四五六七八九\d]+)?)\s*(千克|公斤|kg|KG|克|g|G|毫升|ml|ML)"
)


def grams_from_text(text: str, item: str) -> float | None:
    """在**原话**里找「离食物名最近的那个重量」，支持中文数字（三百克）。"""
    raw = str(text or "")
    core = name_core(item)
    if not raw or not core:
        return None
    best: tuple[int, float] | None = None
    for hit in re.finditer(re.escape(core), raw):
        for match in _GRAMS_NEAR.finditer(raw, max(0, hit.start() - 16), min(len(raw), hit.end() + 16)):
            if raw[max(0, match.start() - 1) : match.start()].endswith("每"):
                continue
            value = _cn_number(match.group(1))
            if value is None or value <= 0:
                continue
            unit = match.group(2)
            if unit in ("千克", "公斤", "kg", "KG"):
                value *= 1000.0
            if value > 5000:
                continue
            distance = abs(match.start() - hit.start())
            if best is None or distance < best[0]:
                best = (distance, value)
    return best[1] if best else None


_PORTION_MULTIPLIED_GRAMS = re.compile(
    r"(?P<count>\d+(?:\.\d+)?|[一二两三四五六七八九十半]+)\s*(?:包|袋|份|盒|瓶|罐|碗|杯|个|支|片)"
    r"[^。；;\n]{0,12}?每\s*(?:包|袋|份|盒|瓶|罐|碗|杯|个|支|片)?\s*(?:是)?\s*"
    r"(?P<val>\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+)\s*(?P<unit>千克|公斤|kg|KG|克|g|G|毫升|ml|ML)"
)
_PER_UNIT_SPEC_RE = re.compile(
    r"每\s*(?:100|一百|[一二两三四五六七八九十\d]+)?\s*(?:包|袋|份|盒|瓶|罐|碗|杯|个|支|片)?\s*(?:是)?\s*"
    r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+)\s*(?:千克|公斤|kg|KG|克|g|G|毫升|ml|ML)"
)
_PORTION_GRAMS = re.compile(r"(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+)\s*(千克|公斤|kg|KG|克|g|G|毫升|ml|ML)")


def portion_grams(portion: Any) -> float | None:
    """从 ``portion`` 文本里取克数（``300克`` / ``50g`` / ``1.5千克`` / ``两包，每包250克``）；取不到返回 None。

    只认**重量**单位：``1杯`` / ``一份`` 不换算（那是份量，不是克重）。
    """
    text = str(portion or "")
    if not text:
        return None
    mult = _PORTION_MULTIPLIED_GRAMS.search(text)
    if mult:
        count = _cn_number(mult.group("count"))
        val = _cn_number(mult.group("val"))
        if count and val and count > 0 and val > 0:
            total = count * val
            if mult.group("unit") in ("千克", "公斤", "kg", "KG"):
                total *= 1000.0
            if 0 < total <= 5000:
                return total
    cleaned = _PER_UNIT_SPEC_RE.sub("", text)
    match = _PORTION_GRAMS.search(cleaned)
    if not match:
        return None
    value = _cn_number(match.group(1))
    if value is None:
        return None
    unit = match.group(2)
    if unit in ("千克", "公斤", "kg", "KG"):
        value *= 1000.0
    if value <= 0 or value > 5000:
        return None
    return value


_PUNCT = re.compile(r"[\s，,。.、;；:：!！?？()（）\[\]【】\"'“”‘’]")
_NUMBER_WORDS = re.compile(r"^[一二两三四五六七八九十百千\d]+(?:克|g|G|毫升|ml|ML|份|个|杯|碗|个)$")


def _clean(text: Any) -> str:
    return _PUNCT.sub("", str(text or ""))


def meal_from_text(
    title: str,
    quote: str,
    raw_meal: Any = None,
    *,
    occurred_at: Any = None,
) -> str:
    """按**原话**判定餐次；原话没说就返回 ``unknown``（绝不猜成 snacks）。

    ``raw_meal``（模型给的 meal 字段）只在原话没有明确餐次词时作为次选，
    且必须是合法枚举；``occurred_at`` 只用来在「早上/晚上」都没说时兜底区分
    早/晚（12 点前 → breakfast，17 点后 → dinner），不确定则仍返回 unknown。
    """
    # [返工 #4] **原话**是餐次的唯一证据；标题是模型摘要，只在原话没有任何餐次词、
    # 且标题写了「加餐/夜宵」并且时间确实是深夜（≥21 点）时才采信——「下午加餐：一份炸薯条」
    # 属于模型臆测（原话只有"一份炸薯条"），不能算餐次。
    text = str(quote or "").strip() or str(title or "").strip()
    if _LUNCH.search(text):
        return "lunch"
    if _BREAKFAST.search(text):
        return "breakfast"
    if _DINNER.search(text):
        return "dinner"
    if _SNACK.search(text):
        return "snacks"
    # [返工 #4] 模型的 meal 字段**不能用来猜"加餐"**（10-26「一份炸薯条」标题写"下午加餐"、
    # 11-02「薯条牛逼啊」被判 snack，都被总控判为错）。但 breakfast/lunch/dinner 三个正餐档
    # 是既有产物里已经承载的字段（老账本依赖它），保留；未知仍为「未说明」。
    raw = str(raw_meal or "").strip().lower()
    if raw in ("breakfast", "早", "早餐", "早饭"):
        return "breakfast"
    if raw in ("lunch", "午", "午餐", "午饭"):
        return "lunch"
    if raw in ("dinner", "晚", "晚餐", "晚饭"):
        return "dinner"
    if not re.search(r"早|午|晚|加餐|夜宵|宵夜|零食|下午茶", text):
        late = _hour_of(occurred_at)
        if late is not None and late >= 21 and re.search(r"加餐|夜宵|宵夜|零食", str(title or "")):
            return "snacks"

    # 原话里自己报的时间点（「十二点喝的咖啡」「七点吃了」）→ 用**这句话里的钟点**，
    # 不用说话时刻（返工 #4：十二点 = 午餐时段）。
    # [2026-11-08] 排除口语小数点（「十八点儿六克」「蛋白质七点八克」里的「点」是小数点，不是钟点）。
    clock_text = _DECIMAL_POINT_IN_TEXT_RE.sub("", text)
    hour = _clock_in_text(clock_text)
    if hour is None:
        hour = _hour_of(occurred_at)
        if not re.search(r"\d{1,2}\s*点|[一二三四五六七八九十]+点", clock_text):
            hour = None
    if hour is not None:
        if 11 <= hour < 15:
            return "lunch"
        if 15 <= hour <= 23 or 1 <= hour <= 4:
            return "dinner"
        if 5 <= hour <= 10:
            # 「七点吃了」原话没说早/晚：有「早/晨」才是早餐，否则按晚餐处理
            # （10-26 的「七点吃了。五十克的青椒」和晚饭在同一段里）。
            if re.search(r"早|晨", text):
                return "breakfast"
            return "dinner"
    return "unknown"


_DECIMAL_POINT_IN_TEXT_RE = re.compile(
    r"(?:\d+|[零〇一二两三四五六七八九十百千]+)点(?:儿[零〇一二三四五六七八九\d]+|[零〇一二三四五六七八九\d]+\s*(?:克|g|G|毫克|mg|MG|千克|kg|千焦|kJ|kj|KJ|千卡|大卡|kcal|升|毫升|ml|ML|斤|两|%|元|块))"
)


def _clock_in_text(text: str) -> int | None:
    """从原话里取钟点：``十二点`` / ``12点`` / ``七点半`` → 12 / 12 / 7。"""
    cleaned = _DECIMAL_POINT_IN_TEXT_RE.sub("", str(text or ""))
    match = re.search(r"(\d{1,2})\s*点", cleaned)
    if match:
        value = int(match.group(1))
        return value if 0 <= value <= 23 else None
    match = re.search(r"([零〇一二两三四五六七八九十]{1,3})\s*点", cleaned)
    if not match:
        return None
    value = _cn_number(match.group(1))
    if value is None:
        return None
    # 「十二点」= 12；「一点」= 13（下午一点）——只看 1..23 的钟点，0 点留给 unknown
    hour = int(value)
    return hour if 1 <= hour <= 23 else None


def _hour_of(value: Any) -> int | None:
    text = str(value or "")
    match = re.search(r"T(\d{2}):", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d{1,2}):\d{2}", text)
    return int(match.group(1)) if match else None


def is_retrospective(text: str) -> bool:
    """「昨天晚上我吃了…」这类回顾（说的是更早的一天）。"""
    return bool(_RETRO.search(str(text or "")))


def is_additive(text: str) -> bool:
    """「又吃了一个 / 再来一份」——同一天同一餐的**新**一份，不是复述。"""
    return bool(_ADDITIVE.search(str(text or "")))


def is_generic_item(name: str) -> bool:
    """泛指词（「食物」「蘸料食物」）不能进合计。"""
    cleaned = _clean(name)
    return (not cleaned) or cleaned in _GENERIC_ITEMS or _NUMBER_WORDS.match(cleaned) is not None


def is_plan_or_discussion(text: str) -> bool:
    """讨论/采购/计划/询问，不是已吃下去。"""
    return bool(_NON_INTAKE.search(str(text or "")))


def normalize_item(name: str) -> str:
    """同一食物的稳定键：去标点、去做法修饰、去尾部的「（蒸）」这类括号。"""
    cleaned = _clean(name)
    cleaned = re.sub(r"（[^）]*）|\([^)]*\)", "", cleaned)
    return name_core(cleaned)


def name_core(name: str) -> str:
    """去掉「蒸/煮/炒…」这类做法修饰，留下食物本体（用于匹配原话里的克数）。"""
    core = _clean(name)
    core = re.sub(r"（[^）]*）|\([^)]*\)", "", core)
    changed = True
    while changed:
        changed = False
        for decoration in _DECORATIONS:
            if core.startswith(decoration) and len(core) > len(decoration) + 1:
                core = core[len(decoration):]
                changed = True
            if core.endswith(decoration) and len(core) > len(decoration) + 1:
                core = core[: -len(decoration)]
                changed = True
    return core


def split_composite(name: str) -> list[str]:
    """把「土豆300g、腊肠50g、蒸毛豆仁100g」拆成配料名列表（用于「整餐 vs 配料」去重）。"""
    raw = str(name or "")
    if not re.search(r"[、,，/和与+]", raw):
        return []
    parts = re.split(r"[、,，/和与+]", raw)
    out: list[str] = []
    for part in parts:
        core = re.sub(r"\d+(?:\.\d+)?\s*(?:克|g|G|千克|kg|毫升|ml)?", "", part)
        core = name_core(core)
        if len(core) >= 2 and core not in out:
            out.append(core)
    return out if len(out) >= 2 else []


def _has_grams(record: Mapping[str, Any]) -> bool:
    weight = record.get("weight_g")
    try:
        return weight is not None and float(weight) > 0
    except (TypeError, ValueError):
        return False


_CONNECTIVE = re.compile(r"^(?:然后|再加上|再加|加上|还有|又加|再|加|以及|和)")


def _segment_ordinal(segment_id: Any) -> int | None:
    """``rec_xxx_seg_001302`` → 1302（用于判断"紧接在同一段回顾之后"）。"""
    match = re.search(r"(\d{3,})$", str(segment_id or ""))
    return int(match.group(1)) if match else None


def _seconds_between(left: Any, right: Any) -> float | None:
    from datetime import datetime

    def parse(value):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    a, b = parse(left), parse(right)
    if a is None or b is None:
        return None
    if (a.tzinfo is None) != (b.tzinfo is None):
        return None
    return abs((a - b).total_seconds())


def mark_retrospective_clusters(records: Sequence[dict[str, Any]]) -> None:
    """按**连续上下文**判定"这段是回顾"（就地写 ``retrospective`` 字段）。

    规则（返工 #2）：一句里有「昨天」→ 回顾；**紧随其后**的同一录音、段号 +1~+5、
    间隔 ≤ 90 秒、且以「然后/加上/再加上/还有/再/加」承接的句子，也算同一段回顾
    （10-27：「昨天我吃的是三百克的土豆」「然后加上五十克的腊肠」「再加上一百克的毛豆仁」）。

    判据只看**原话**（``quote``）；事件标题是模型摘要，里面出现「昨天」不算证据
    （10-26「昨天蒸腊肠，缩水后约50克」就是这么被误判成回顾、整条从当天消失的）。
    """
    ordered = sorted(
        (record for record in records),
        key=lambda item: (_segment_ordinal(item.get("segment_id")) or 10**9, str(item.get("reported_at") or "")),
    )
    anchors: list[dict[str, Any]] = []
    for record in ordered:
        quote = str(record.get("quote") or "")
        explicit = is_retrospective(quote)
        inherited = False
        if not explicit:
            ordinal = _segment_ordinal(record.get("segment_id"))
            for anchor in anchors:
                anchor_ordinal = _segment_ordinal(anchor.get("segment_id"))
                if record.get("recording_id") and anchor.get("recording_id") and record.get("recording_id") != anchor.get("recording_id"):
                    continue
                if ordinal is None or anchor_ordinal is None:
                    continue
                if not 0 < ordinal - anchor_ordinal <= 5:
                    continue
                gap = _seconds_between(record.get("reported_at"), anchor.get("reported_at"))
                if gap is not None and gap > 90:
                    continue
                if _CONNECTIVE.match(_clean(quote)):
                    inherited = True
                    break
        if explicit or inherited:
            record["retrospective"] = True
            record["retro_reason"] = "explicit" if explicit else "context_continuation"
            anchors.append(record)


def assign_meal_by_context(records: Sequence[dict[str, Any]]) -> None:
    """未说明餐次的条目，若**紧挨着**（同录音、±60 秒）已说明餐次的条目，则沿用并写依据。

    返工 #4：10-26「一份炸薯条」在「中午吃了一个汉堡」之后 7 秒、同一录音 → 午餐（写依据）；
    11-02「薯条牛逼啊」没有邻近餐次证据 → 保持「未说明」。
    """
    stated = [r for r in records if str(r.get("meal") or "unknown") != "unknown"]
    for record in records:
        if str(record.get("meal") or "unknown") != "unknown":
            continue
        best: tuple[float, dict[str, Any]] | None = None
        for other in stated:
            same_recording = (not record.get("recording_id") or not other.get("recording_id")
                              or record.get("recording_id") == other.get("recording_id"))
            gap = _seconds_between(record.get("reported_at"), other.get("reported_at"))
            if gap is None:
                continue
            # 同录音 60 秒内；跨录音只在 ≤15 秒（10-26「五十克。」与「七点吃了。五十克的青椒」
            # 相隔 6 秒、分了两个录音，但显然是同一句的两个片段）。
            if gap > (60 if same_recording else 15):
                continue
            if best is None or gap < best[0]:
                best = (gap, other)
        if best is not None:
            record["meal"] = best[1].get("meal")
            record["meal_reason"] = f"context_adjacent:{best[1].get('item')}({int(best[0])}s)"


def _is_restatement(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """两条是不是同一段复述：字数不同，且一句包含另一句（去标点后）。"""
    a = _clean(left.get("quote") or left.get("title") or "")
    b = _clean(right.get("quote") or right.get("title") or "")
    if not a or not b or a == b:
        return False
    return a in b or b in a


def _richness(record: Mapping[str, Any]) -> tuple[int, int, int]:
    """保留哪一条：有克重 > 有热量 > 有配料依据。"""
    basis = str(record.get("estimate_basis") or "")
    return (
        1 if _has_grams(record) else 0,
        1 if record.get("calories_known") else 0,
        2 if basis in ("ingredient_math", "library_math") else 1 if basis else 0,
    )


def dedupe_same_meal(records: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """同一天内的合并规则。返回 ``(kept, merged_away, needs_review)``。

    * **同餐次 + 同食物**：
        - 原文出现「又吃了/再来一份」→ 两条都保留（新的一份）；
        - 一条有克数、另一条没有 → 合并成一条（补充克数，不新增热量）；
        - 两条都有/都没有克数 → 判为**复述**，保留信息更全的一条。
    * **整餐 vs 配料**：某条 item 是由多段配料拼成的（含「、，/」），且它的每个配料
      在**同一餐**都有独立记录 → 丢掉整餐那条（配料那几条是逐条查库算的，更准）。
    * 拿不准（同食物、同餐次、克数冲突且没有「又吃了」字样）→ 进 ``needs_review``，
      **两条都留在 kept**（合计按已确认部分算，未确认单独标）。
    """
    kept: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []

    by_meal: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_meal.setdefault(str(record.get("meal") or "unknown"), []).append(record)

    # ---- 1) 整餐 vs 配料（**跨餐次**判定：整餐卡和配料卡常被分到不同餐次）----
    components: dict[str, dict[str, Any]] = {}
    for record in records:
        key = normalize_item(str(record.get("item") or ""))
        if key:
            components.setdefault(key, record)
    composites: set[int] = set()
    for record in records:
        parts = split_composite(str(record.get("item") or ""))
        if not parts:
            continue
        hits = [p for p in parts if p in components and components[p] is not record]
        if len(hits) >= 2:
            composites.add(id(record))
            merged.append({**record, "merge_reason": "composite_duplicate_of_components",
                           "merged_into": [components[p].get("item") for p in hits]})

    for meal, group in by_meal.items():

        # ---- 2) 同食物 复述/补充 ----
        buckets: dict[str, list[dict[str, Any]]] = {}
        for record in group:
            if id(record) in composites:
                continue
            key = normalize_item(str(record.get("item") or ""))
            if not key:
                kept.append(record)
                continue
            buckets.setdefault(key, []).append(record)

        for key, rows in buckets.items():
            if len(rows) == 1:
                kept.append(rows[0])
                continue
            additive = [r for r in rows if is_additive(f"{r.get('title','')} {r.get('quote','')}")]
            if additive and len(additive) < len(rows):
                # 有「又吃了」→ 视作新的一份，全部保留（累加）
                kept.extend(rows)
                continue
            if additive:
                kept.extend(rows)
                continue
            rows_sorted = sorted(rows, key=_richness, reverse=True)
            primary = rows_sorted[0]
            for other in rows_sorted[1:]:
                grams_a, grams_b = _has_grams(primary), _has_grams(other)
                if grams_a != grams_b:
                    # 一条只是补克数的复述 → 合并，不新增热量（Q912-3）
                    merged.append({**other, "merge_reason": "restatement_with_grams",
                                   "merged_into": [primary.get("item")]})
                    continue
                # 两条都有/都没有克数：只有在「原话是彼此的复述」时才合并。
                # 判据（缺一不可）：字数不同 + 一句包含另一句。
                # 完全相同的一句话出现在两条独立事件里，**不**视为同一次（Q892：
                # 同一句话不能证明同一次发生），保持两条。
                if _is_restatement(primary, other):
                    merged.append({**other, "merge_reason": "same_meal_restatement",
                                   "merged_into": [primary.get("item")]})
                    continue
                if grams_a and grams_b:
                    # Q912-3：同餐同食物且**份量一致** → 同一条（10-27 两条 50g 腊肠）。
                    weight_a = float(primary.get("weight_g") or 0)
                    weight_b = float(other.get("weight_g") or 0)
                    if weight_a > 0 and abs(weight_a - weight_b) <= 0.05 * max(weight_a, weight_b):
                        merged.append({**other, "merge_reason": "same_meal_same_portion",
                                       "merged_into": [primary.get("item")]})
                        continue
                    cal_a = primary.get("calories_kcal")
                    cal_b = other.get("calories_kcal")
                    if cal_a is not None and cal_b is not None and abs(float(cal_a) - float(cal_b)) > 0.15 * max(float(cal_a), float(cal_b)):
                        primary.setdefault("needs_review", "same_meal_portion_conflict")
                        other.setdefault("needs_review", "same_meal_portion_conflict")
                        review.append(other)
                kept.append(other)
            kept.append(primary)

    return kept, merged, review


#: 指向“记录/助手输出”的词——只有同时出现，才可能是对助手的反馈，而不是吃饭。
_RECORD_POINTER = re.compile(r"记录|显示|记上|记下|漏记|陈述")
_FEEDBACK_COMPLAINT = re.compile(
    r"没有看到|没看到|未看到|没显示|未显示|没记|未记|怎么没有|漏了|漏记|你漏|你没记|陈述自己"
)
#: 对“没看到…吃了…”这类抱怨的补充；要求“吃/饭/餐”紧跟其后，避免误杀
#: “我吃了没看到价格的面”这种真实吃饭句（那里“没看到”后面是“价格”）。
_MISSING_MEAL = re.compile(r"(?:没有|没|未)看到[^。！？]{0,12}(?:吃|饭|餐|晚餐|早餐|午餐)")

#: 食物名里不该出现的非食物词。
_IMPLAUSIBLE_FOOD_TOKENS = re.compile(
    r"看到|显示|记录|陈述|没有|未显示|我|你"
)
_FOODISH_VERBS = re.compile(r"吃|喝|说|看|记|显示|没有|陈述")


#: 只在读取时生效的“只算本人”方案开关（A/B/C/off）。
FOOD_SELF_ONLY_MODE = "A"
_QUERY_TITLE = re.compile(r"查询：|他提到|她提到|视频里")


def food_person_exclusion(record: Mapping[str, Any], mode: str | None = None) -> str | None:
    """A/B/C 三方案：判断饮食记录是否不是用户本人。

    A：source_label=non_self 排除；
    B：A + claim_type=observation 且标题是“查询/他提到/她提到”；
    C：只有 self 或活动层本人（domain_events 已要求 self 证据）才保留，其余排除。
    """
    effective = (mode or FOOD_SELF_ONLY_MODE or "off").strip().upper()
    if effective not in {"A", "B", "C"}:
        return None
    label = str(record.get("source_label") or "").strip().lower()
    origin = str(record.get("origin") or "").strip().lower()
    title = str(record.get("title") or "")
    if effective == "C":
        if label == "self" or (origin == "activity" and record.get("activity_self_evidence")):
            return None
        return "unverified_speaker"
    if label == "non_self":
        return "not_self_speaker"
    if effective == "B" and str(record.get("claim_type") or "").strip().lower() == "observation" and _QUERY_TITLE.search(title):
        return "not_self_speaker"
    return None


def is_feedback_to_assistant(text: str) -> bool:
    """是不是“对助手/对记录的反馈/抱怨”，而不是描述自己吃了什么。

    保守规则：必须同时出现“指向记录本身的词”（记录/显示/记上/…）和
    抱怨语气；或者明确是“没看到我…吃了…”这种漏记抱怨。
    真实的“我吃了没看到价格的面”只有“没看到价格”，没有记录词，不会命中。
    """
    blob = str(text or "")
    if not blob.strip():
        return False
    if _RECORD_POINTER.search(blob) and _FEEDBACK_COMPLAINT.search(blob):
        return True
    return bool(_MISSING_MEAL.search(blob))


def is_implausible_food_name(name: str) -> bool:
    """食物名合理性：人称、记录/显示类动词、整句长度都不像食物名。"""
    cleaned = _clean(name)
    if not cleaned:
        return False
    if _IMPLAUSIBLE_FOOD_TOKENS.search(cleaned):
        return True
    hanzi = len(re.findall(r"[\u4e00-\u9fff]", cleaned))
    return hanzi > 12 and bool(_FOODISH_VERBS.search(cleaned))
