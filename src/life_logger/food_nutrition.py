"""配料级营养换算：本地成分表 + 中文数量解析（事件层热量估算的确定性兜底）。

为什么存在（实测案例 2026-10-12 08:37）：

    用户说「记录一下，我现在要做一杯生椰拿铁」+「一百四十克菲诺的椰乳」+「没冰，加上一杯浓缩」。
    事件层只凭「生椰拿铁」四个字，按市售咖啡店标准中杯（``weight_g: 350``）估成
    ``estimated_calories: 220`` / 碳水 30g —— 而按配料算：

        菲诺厚椰乳 93.8 kcal/100g × 140g = 131.3 kcal
        + 意式浓缩 1 份（30ml）≈ 3.9 kcal
        = 约 135 kcal（蛋白 1.6g / 脂肪 9.8g / 碳水 10.1g / 钠 118mg）

    → 高估约 85 kcal（+62%），其中碳水虚高 20g（约 80 kcal）。

本模块只做一件事：**当原话里出现「配料 + 克重/份量」时，用本地成分表做乘除换算，
覆盖模型的模糊估值**，并写下 ``estimate_basis`` / ``estimate_confidence`` 供下游区分
「按配料算」与「按标准份估」。事件结构（item/meal/estimated_calories/nutrients/portion）
保持不变，只增字段，不删字段。

保守边界（宁可不改，也不乱改）：

* 只有名字、没有克重（「喝了杯拿铁」）→ 不动数值，只标 ``standard_serving`` + 低置信度。
* 至少要有一个 **克/毫升** 级别的配料才触发换算；只有「一杯/一份」这类计数单位时不触发，
  否则「一杯浓缩」会把整杯生椰拿铁算成 4 kcal（低估比高估更危险）。
* 配料提及必须与 food 事件在时间上同一窗口（默认 ±30 分钟）且分配到最近的那个事件，
  每条提及只归一个事件，不重复计。
* 别人的话（``source_label == "non_self"``）不计入本人热量。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from life_logger.food_lookup import (
    SERVING_SPOKEN,
    FoodLookup,
    FoodNutrition,
)

FOOD_NUTRIENT_KEYS: tuple[str, ...] = (
    "energy_kcal",
    "protein_g",
    "fat_g",
    "carbohydrates_g",
    "sodium_mg",
)

# 「按配料算」/「按标签算」/「按标准份估」/「按本地营养库算」——写进 food 载荷，下游可据此区分置信度。
BASIS_INGREDIENT_MATH = "ingredient_math"
BASIS_LABEL_MATH = "label_math"
BASIS_STANDARD_SERVING = "standard_serving"
# [2026-10-13] 四层查询链命中：用「自建品牌库/中国食物成分表/USDA/OFF」的每 100g × 份量算出来。
# 与 ingredient_math 的区别：ingredient_math 是原话里给了配料克重，
# library_math 只凭**食物名**（克重可能是库里/经验表的份量，也可能是原话另说的克重）。
BASIS_LIBRARY_MATH = "library_math"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

# 允许覆盖模型估值的查询置信度：查得够准才动数字，查得含糊只挂参考值。
_OVERRIDE_CONFIDENCES: frozenset[str] = frozenset({CONFIDENCE_HIGH, CONFIDENCE_MEDIUM})

# 默认时间窗：配料提及与 food 事件相隔不超过半小时才允许归到一起。
DEFAULT_MATCH_WINDOW_SECONDS = 1800.0

# 单次提及的克重上限（超过基本是 ASR 把时间/价格听成克重）。
_MAX_MENTION_GRAMS = 5000.0


@dataclass(frozen=True)
class IngredientProfile:
    """一种已知配料的成分表。``per_100g`` 是每 100g/100ml 的值。"""

    name: str
    keywords: tuple[str, ...]
    per_100g: Mapping[str, float]
    unit_grams: float | None = None
    unit_label: str | None = None
    evidence: str = "generic_reference"

    def scale(self, grams: float) -> dict[str, float]:
        factor = grams / 100.0
        return {key: float(value) * factor for key, value in self.per_100g.items()}


def _profile(
    name: str,
    keywords: Iterable[str],
    energy_kcal: float,
    protein_g: float,
    fat_g: float,
    carbohydrates_g: float,
    sodium_mg: float,
    *,
    unit_grams: float | None = None,
    unit_label: str | None = None,
    evidence: str = "generic_reference",
) -> IngredientProfile:
    return IngredientProfile(
        name=name,
        keywords=tuple(keywords),
        per_100g={
            "energy_kcal": energy_kcal,
            "protein_g": protein_g,
            "fat_g": fat_g,
            "carbohydrates_g": carbohydrates_g,
            "sodium_mg": sodium_mg,
        },
        unit_grams=unit_grams,
        unit_label=unit_label,
        evidence=evidence,
    )


# 奶茶/咖啡常用配料优先覆盖。「user_verified」= 用户提供/本次缺陷相关；
# 「generic_reference」= 通用食物成分表量级的参考值，允许被后续按标签换算覆盖。
INGREDIENTS: tuple[IngredientProfile, ...] = (
    # —— 椰乳系（本次缺陷的主角）——
    _profile(
        "菲诺厚椰乳",
        ("菲诺厚椰乳", "菲诺的椰乳", "菲诺椰乳", "菲诺", "厚椰乳", "生椰乳", "椰乳", "椰奶"),
        93.8,
        1.0,
        7.0,
        6.8,
        82.0,
        unit_grams=200.0,
        unit_label="杯",
        evidence="user_verified",
    ),
    _profile(
        "椰浆",
        ("椰浆",),
        138.0,
        1.6,
        12.5,
        4.5,
        20.0,
        unit_grams=50.0,
        unit_label="份",
    ),
    _profile(
        "意式浓缩",
        ("意式浓缩", "浓缩咖啡", "浓缩"),
        13.0,
        0.7,
        0.2,
        2.0,
        10.0,
        unit_grams=30.0,
        unit_label="杯/份（单份 30ml，约 4 kcal）",
        evidence="user_verified",
    ),
    # —— 奶基底 ——
    _profile("全脂牛奶", ("全脂牛奶", "鲜牛奶", "牛奶"), 65.0, 3.3, 3.6, 4.9, 44.0, unit_grams=200.0, unit_label="杯"),
    _profile("脱脂牛奶", ("脱脂牛奶", "脱脂奶"), 35.0, 3.4, 0.2, 5.0, 45.0, unit_grams=200.0, unit_label="杯"),
    _profile("燕麦奶", ("燕麦奶", "燕麦饮", "oat milk"), 45.0, 1.0, 1.5, 6.7, 40.0, unit_grams=200.0, unit_label="杯"),
    _profile("淡奶油", ("淡奶油", "稀奶油", "动物奶油"), 350.0, 2.1, 37.0, 3.0, 40.0, unit_grams=30.0, unit_label="份"),
    _profile("炼乳", ("炼乳", "炼奶"), 330.0, 7.5, 8.5, 55.0, 130.0, unit_grams=20.0, unit_label="勺"),
    # —— 甜味/风味 ——
    _profile("白砂糖", ("白砂糖", "砂糖", "白糖"), 400.0, 0.0, 0.0, 100.0, 1.0, unit_grams=5.0, unit_label="包/勺"),
    _profile("糖浆", ("糖浆", "果糖浆", "风味糖浆"), 300.0, 0.0, 0.0, 75.0, 10.0, unit_grams=15.0, unit_label="泵/份"),
    _profile("蜂蜜", ("蜂蜜",), 304.0, 0.3, 0.0, 82.0, 5.0, unit_grams=20.0, unit_label="勺"),
    # —— 常用饮品/主食/蛋白（健身与日常记录常见）——
    _profile("豆浆", ("豆浆", "豆奶"), 31.0, 3.0, 1.6, 1.2, 3.0, unit_grams=200.0, unit_label="杯"),
    _profile("燕麦片", ("燕麦片", "燕麦"), 380.0, 13.0, 7.0, 66.0, 5.0, unit_grams=40.0, unit_label="份"),
    _profile("米饭", ("米饭", "白饭"), 116.0, 2.6, 0.3, 25.9, 2.0, unit_grams=150.0, unit_label="碗"),
    _profile("鸡胸肉", ("鸡胸肉", "鸡胸", "鸡胸脯肉"), 165.0, 31.0, 3.6, 0.0, 74.0, unit_grams=100.0, unit_label="份"),
    _profile("鸡蛋", ("鸡蛋", "水煮蛋", "煎蛋"), 143.0, 12.6, 9.5, 0.7, 142.0, unit_grams=50.0, unit_label="个"),
    _profile("蛋白粉", ("蛋白粉", "乳清蛋白"), 380.0, 75.0, 5.0, 10.0, 300.0, unit_grams=30.0, unit_label="勺"),
    _profile("酸奶", ("酸奶", "希腊酸奶"), 72.0, 2.5, 2.7, 9.3, 39.0, unit_grams=150.0, unit_label="杯"),
    _profile("香蕉", ("香蕉",), 93.0, 1.1, 0.3, 22.0, 1.0, unit_grams=120.0, unit_label="根"),
    _profile("苹果", ("苹果",), 53.0, 0.2, 0.2, 13.5, 1.0, unit_grams=200.0, unit_label="个"),
    _profile("蓝莓", ("蓝莓",), 57.0, 0.7, 0.3, 14.0, 1.0, unit_grams=100.0, unit_label="份"),
    # —— 酒水/零卡 ——
    _profile("威士忌", ("威士忌", "whisky", "whiskey"), 250.0, 0.0, 0.0, 0.0, 0.0, unit_grams=45.0, unit_label="份（约 45ml）"),
    _profile("苏打水", ("苏打水", "气泡水", "sparkling water"), 0.0, 0.0, 0.0, 0.0, 5.0, unit_grams=330.0, unit_label="罐"),
    _profile("乌龙茶", ("乌龙茶", "绿茶", "红茶"), 0.0, 0.0, 0.0, 0.0, 5.0, unit_grams=250.0, unit_label="杯"),
    _profile("可乐", ("可乐", "coke"), 43.0, 0.0, 0.0, 10.6, 4.0, unit_grams=330.0, unit_label="罐"),
    _profile("生脉饮", ("生脉饮",), 250.0, 2.0, 0.0, 60.0, 50.0, unit_grams=10.0, unit_label="支"),
)

# 中文数字 → 阿拉伯数字
_CN_DIGITS: Mapping[str, float] = {
    "零": 0.0,
    "〇": 0.0,
    "一": 1.0,
    "壹": 1.0,
    "二": 2.0,
    "两": 2.0,
    "贰": 2.0,
    "三": 3.0,
    "四": 4.0,
    "五": 5.0,
    "六": 6.0,
    "七": 7.0,
    "八": 8.0,
    "九": 9.0,
}
_CN_UNITS: Mapping[str, float] = {"十": 10.0, "百": 100.0, "千": 1000.0}

_NUMBER_PATTERN = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千壹贰]+|半)"

# 单位 → 克换算系数。毫升按密度 1 处理（奶/水/糖浆类误差可接受）。
_WEIGHT_UNITS: Mapping[str, float] = {
    "千克": 1000.0,
    "公斤": 1000.0,
    "kg": 1000.0,
    "KG": 1000.0,
    "Kg": 1000.0,
    "克": 1.0,
    "g": 1.0,
    "G": 1.0,
    "毫升": 1.0,
    "ml": 1.0,
    "mL": 1.0,
    "ML": 1.0,
    "升": 1000.0,
    "L": 1000.0,
}
_COUNT_UNITS = ("杯", "份", "个", "瓶", "罐", "包", "袋", "支", "勺", "碗", "片", "只", "shot", "shots")

_UNIT_ALTERNATION = "|".join(
    re.escape(unit)
    for unit in sorted(
        [*_WEIGHT_UNITS.keys(), *_COUNT_UNITS], key=len, reverse=True
    )
)
_QUANTITY_RE = re.compile(
    rf"(?P<qty>{_NUMBER_PATTERN})\s*(?P<unit>{_UNIT_ALTERNATION})"
)

# ── [2026-11-07 「不知重量就不算」第二阶段] 营养库体检门槛 ────────────────
# 数字依据：work/food-library-audit-20261107/report.md §4（全库 10,613 条实测命中数）。
# 旧门槛（钠>3000 / 能量>900 / 偏差 40%）会误杀盐、酱油、油脂、酒类，已废弃。

# 1) 绝对荒谬层（体检：钠>20000 命中 12 条全是盐/鸡精/汤粉块；另两条零误杀）
MAX_SODIUM_MG_PER_100G = 20000.0
MAX_KCAL_PER_100G = 900.0
MAX_KCAL_FAT_GUARD = 90.0          # 能量>900 且 脂肪<90g → 荒谬（纯油脂是合法的）
MAX_MACRO_SUM_G_PER_100G = 105.0   # 蛋白+脂肪+碳水 不可能 >105g/100g

# 2) 形态层（体检：钠>3000 且名字含汤/汁/羹 → 全库恰好命中事故那条，零误杀）
SOUP_SHAPE_TOKENS = ("汤", "汁", "羹")
SOUP_SHAPE_MAX_SODIUM_MG = 3000.0

# 3) 自洽层（Atwater：4/4/9；体检：计算值≥50 且 能量<0.5×计算值 → 4 条，排除甜味剂后 2 条真错）
ATWATER_MIN_KCAL_PER_100G = 50.0
ATWATER_MIN_RATIO = 0.5
ZERO_CALORIE_TOKENS = ("零卡", "无糖", "diet", "sugar free", "sugar-free", "代糖", "stevia", "monk fruit")

# 4) contains 匹配：查询的核心字在库名里一个都不出现 → 当查不到
CONTAINS_MIN_CORE_HITS = 2
_CONTAINS_STOP_CHARS = set("的和与及熬煮炒炖焖烤炸拌了过个份杯碗瓶斤两克升毫我你他她吃喝点买做用加是有在就都也还把这那")

#: 份量来源判定的 estimate_basis 取值（6 号窗晚上复盘会按这个读，不许改名）
BASIS_NO_WEIGHT_SPOKEN = "no_weight_spoken"
BASIS_NO_PORTION_SPOKEN = "no_portion_spoken"
BASIS_IMPLAUSIBLE_NUTRITION = "implausible_nutrition"
BASIS_CONTAINS_MISMATCH = "contains_mismatch"
UNWEIGHED_BASES = (
    BASIS_NO_WEIGHT_SPOKEN, BASIS_NO_PORTION_SPOKEN,
    BASIS_IMPLAUSIBLE_NUTRITION, BASIS_CONTAINS_MISMATCH,
)


def nutrition_implausible(per_100g: Mapping[str, Any] | None, name: str = "") -> str | None:
    """每 100g 数值荒谬 → 返回原因；合理 → None。只判读，不改库。"""
    if not isinstance(per_100g, Mapping):
        return None

    def num(key: str) -> float | None:
        value = per_100g.get(key)
        try:
            value = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return value if value == value and abs(value) != float("inf") else None

    sodium = num("sodium_mg")
    energy = num("energy_kcal")
    protein = num("protein_g") or 0.0
    fat = num("fat_g") or 0.0
    carbs = num("carbohydrates_g") or 0.0
    label = str(name or "").lower()

    if sodium is not None and sodium > MAX_SODIUM_MG_PER_100G:
        return "sodium_over_absolute_cap"
    if energy is not None and energy > MAX_KCAL_PER_100G and fat < MAX_KCAL_FAT_GUARD:
        return "energy_over_cap_without_fat"
    if protein + fat + carbs > MAX_MACRO_SUM_G_PER_100G:
        return "macro_sum_over_cap"
    if (sodium is not None and sodium > SOUP_SHAPE_MAX_SODIUM_MG
            and any(token in str(name or "") for token in SOUP_SHAPE_TOKENS)):
        return "soup_shape_with_high_sodium"
    if energy is not None and not any(token in label for token in ZERO_CALORIE_TOKENS):
        atwater = 4.0 * carbs + 4.0 * protein + 9.0 * fat
        if atwater >= ATWATER_MIN_KCAL_PER_100G and energy < ATWATER_MIN_RATIO * atwater:
            return "energy_below_atwater"
    return None


def contains_core_mismatch(query: str, matched_name: str) -> bool:
    """contains 命中但核心字对不上（「鱼骨头汤」≠「油菜和笋的和风汤」）→ True。"""
    core = {ch for ch in str(query or "") if _is_cjk(ch) and ch not in _CONTAINS_STOP_CHARS}
    if not core:
        return False
    hits = sum(1 for ch in core if ch in str(matched_name or ""))
    return hits < min(CONTAINS_MIN_CORE_HITS, len(core))


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


# 「浓缩果汁 / 浓缩液」不是浓缩咖啡；「脱脂牛奶」「燕麦奶」有各自的条目；
# 「蛋白粉」不是鸡蛋。命中这些前后缀就不算这条配料。
_KEYWORD_EXCLUSIONS: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "浓缩": ((), ("果", "汁", "液", "汤", "酱")),
    "牛奶": (("脱脂", "低脂", "去脂"), ()),
    "燕麦": ((), ("奶", "饮")),
    "鸡蛋": (("蛋白",), ()),
    "椰奶": ((), ("粉",)),
    "可乐": ((), ("饼",)),
}

_NUTRITION_LABEL_RE = re.compile(
    r"(每\s*100\s*(?:克|g|毫升|ml)|营养成分表|能量\s*\d|千焦|kJ)"
)


def parse_quantity(text: str) -> float | None:
    """解析数量文本：``140``→140、``一百四十``→140、``半``→0.5。

    口语里的「一百四」按字面算 104（不做 140 的推测），宁可保守。
    """
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
        return float(cleaned)
    total = 0.0
    current = 0.0
    seen = False
    for char in cleaned:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
            seen = True
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            total += (current or 1.0) * unit
            current = 0.0
            seen = True
        elif char == "半":
            current += 0.5
            seen = True
        else:
            return None
    if not seen:
        return None
    return total + current


@dataclass(frozen=True)
class IngredientMention:
    """原文里的一处「配料 + 份量」。"""

    name: str
    grams: float
    unit: str
    quantity_text: str
    nutrients: Mapping[str, float] = field(default_factory=dict)
    evidence: str = "generic_reference"
    span: tuple[int, int] = (0, 0)
    quote: str = ""

    @property
    def energy_kcal(self) -> float:
        return float(self.nutrients.get("energy_kcal") or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.name,
            "grams": round(self.grams, 1),
            "quantity": f"{self.quantity_text}{self.unit}",
            "energy_kcal": round(self.energy_kcal, 1),
            "nutrients": {key: round(float(value), 1) for key, value in self.nutrients.items()},
            "evidence": self.evidence,
            "quote": self.quote,
        }


def _quantity_before(text: str, index: int, *, max_chars: int = 14, max_gap: int = 6):
    window_start = max(0, index - max_chars)
    window = text[window_start:index]
    matches = list(_QUANTITY_RE.finditer(window))
    if not matches:
        return None
    match = matches[-1]
    if index - (window_start + match.end()) > max_gap:
        return None
    return match, (window_start + match.start(), window_start + match.end())


def _quantity_after(text: str, index: int, *, max_chars: int = 12, max_gap: int = 4):
    window = text[index : index + max_chars]
    match = _QUANTITY_RE.search(window)
    if match is None or match.start() > max_gap:
        return None
    return match, (index + match.start(), index + match.end())


def _keyword_is_false_friend(text: str, start: int, keyword: str) -> bool:
    prefixes, suffixes = _KEYWORD_EXCLUSIONS.get(keyword, ((), ()))
    before = text[max(0, start - 2) : start]
    after = text[start + len(keyword) : start + len(keyword) + 2]
    if any(before.endswith(prefix) for prefix in prefixes):
        return True
    return any(after.startswith(suffix) for suffix in suffixes)


def _mention_grams(
    profile: IngredientProfile, match: re.Match[str]
) -> tuple[float, str] | None:
    quantity = parse_quantity(match.group("qty"))
    if quantity is None or quantity <= 0:
        return None
    unit = match.group("unit")
    if unit in _WEIGHT_UNITS:
        grams = quantity * _WEIGHT_UNITS[unit]
    elif unit in _COUNT_UNITS:
        if profile.unit_grams is None:
            return None
        grams = quantity * profile.unit_grams
    else:
        return None
    if not 0 < grams <= _MAX_MENTION_GRAMS:
        return None
    return grams, unit


def extract_ingredient_mentions(text: str) -> list[IngredientMention]:
    """从一句（或一段）转写里抽出所有「配料 + 克重/份量」。

    同一处份量只记一次：``一百四十克菲诺的椰乳`` 里品牌词「菲诺」与品类词「椰乳」
    指向同一份 140g，不会算两遍（按 ``(配料名, 份量 span)`` 去重）。

    两条保守规则（都是真实转写里踩出来的）：

    * ``每100克是163千焦`` 是成分表口径，不是摄入量 —— 「每」开头的份量一律不当作摄入克重。
    * 前后都挂着份量时（``一份豆浆300克``）优先取克/毫升，而不是计数单位（份/杯/个）。
    """
    if not text:
        return []
    mentions: list[IngredientMention] = []
    seen: set[tuple[str, int, int]] = set()
    for profile in INGREDIENTS:
        for keyword in profile.keywords:
            for hit in re.finditer(re.escape(keyword), text):
                if _keyword_is_false_friend(text, hit.start(), keyword):
                    continue
                candidates: tuple[
                    tuple[re.Match[str], tuple[int, int]] | None,
                    tuple[re.Match[str], tuple[int, int]] | None,
                ] = (_quantity_before(text, hit.start()), _quantity_after(text, hit.end()))
                resolved = _pick_quantity(text, candidates[0], candidates[1])
                if resolved is None:
                    continue
                match, span = resolved
                grams_info = _mention_grams(profile, match)
                if grams_info is None:
                    continue
                grams, unit = grams_info
                key = (profile.name, span[0], span[1])
                if key in seen:
                    continue
                seen.add(key)
                mentions.append(
                    IngredientMention(
                        name=profile.name,
                        grams=grams,
                        unit=unit,
                        quantity_text=match.group("qty"),
                        nutrients=profile.scale(grams),
                        evidence=profile.evidence,
                        span=span,
                        quote=text[max(0, min(hit.start(), span[0]) - 6) : hit.end() + 4],
                    )
                )
    mentions.sort(key=lambda item: item.span[0])
    return mentions


def _pick_quantity(
    text: str,
    before: tuple[re.Match[str], tuple[int, int]] | None,
    after: tuple[re.Match[str], tuple[int, int]] | None,
) -> tuple[re.Match[str], tuple[int, int]] | None:
    """在「配料前」「配料后」两处候选里挑一个。

    * 排序：克/毫升（0）优先于计数单位（1）；同档取靠近配料的那一处。
    * 剔除「每100克」这种成分表规格口径 —— 那是每 100g 的营养标签，不是本次摄入量。
    """
    ranked: list[tuple[tuple[int, int], tuple[re.Match[str], tuple[int, int]]]] = []
    for order, candidate in enumerate((before, after)):
        if candidate is None:
            continue
        match, span = candidate
        if text[max(0, span[0] - 1) : span[0]].endswith("每"):
            continue  # 每100克 = 成分表规格，不是本次摄入量
        unit_rank = 0 if match.group("unit") in _WEIGHT_UNITS else 1
        ranked.append(((unit_rank, order), candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def has_weight_based_mention(mentions: Sequence[IngredientMention]) -> bool:
    """至少要有一条「克/毫升」级配料才允许触发换算（避免只凭「一杯浓缩」就覆盖整杯）。"""
    return any(mention.unit in _WEIGHT_UNITS for mention in mentions)


def sum_nutrition(mentions: Sequence[IngredientMention]) -> dict[str, float]:
    totals = {key: 0.0 for key in FOOD_NUTRIENT_KEYS}
    for mention in mentions:
        for key in FOOD_NUTRIENT_KEYS:
            totals[key] += float(mention.nutrients.get(key) or 0.0)
    return totals


def has_nutrition_label(text: str) -> bool:
    """原文是否自带营养成分表/千焦数值（此时模型自己做换算，不该被标成模糊估值）。"""
    return bool(_NUTRITION_LABEL_RE.search(text or ""))


def _food_payload(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """取出可写的 food 载荷；兼容 ``{"food": {...}}`` 与多包一层的形状。"""
    if not isinstance(event, Mapping):
        return None
    data = event.get("data")
    if not isinstance(data, Mapping) or not isinstance(data, dict):
        return None
    food = data.get("food")
    if not isinstance(food, Mapping):
        return None
    nested = food.get("food")
    if isinstance(nested, Mapping) and not any(
        key in food for key in ("estimated_calories", "item", "meal", "nutrients")
    ):
        food = nested
    if not isinstance(food, dict):
        food = dict(food)
    data["food"] = food
    return food


def _record_offset(row: Mapping[str, Any]) -> float | None:
    value = row.get("start_offset_seconds")
    if isinstance(value, bool) or value is None:
        return None
    try:
        offset = float(value)
    except (TypeError, ValueError):
        return None
    return offset


def _event_offset(event: Mapping[str, Any]) -> float | None:
    time = event.get("time")
    if isinstance(time, Mapping):
        value = time.get("relative_seconds")
        if not isinstance(value, bool) and value is not None:
            try:
                if isinstance(value, (int, float)):
                    return float(value)
            except (TypeError, ValueError):
                pass
    evidence = event.get("evidence")
    if isinstance(evidence, Mapping):
        value = evidence.get("start_offset_seconds")
        if not isinstance(value, bool) and value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _event_segment(event: Mapping[str, Any]) -> str:
    evidence = event.get("evidence")
    if isinstance(evidence, Mapping):
        return str(evidence.get("segment_id") or "")
    return ""


def _match_cost(
    offset: float | None,
    segment: str,
    event: Mapping[str, Any],
    window_seconds: float,
) -> float | None:
    """配料提及 → 事件的归并代价；None = 不归给这个事件。"""
    event_segment = _event_segment(event)
    same_segment = bool(segment) and segment == event_segment
    event_offset = _event_offset(event)
    if offset is not None and event_offset is not None:
        distance = abs(offset - event_offset)
        if same_segment:
            return distance
        if distance <= window_seconds:
            return distance + 1.0  # 同一段内的优先，但不能跨段无条件抢占
        return None
    if same_segment:
        return 0.0
    return None


def _mark_standard_serving(food: dict[str, Any], *, hint_text: str = "") -> None:
    basis = food.get("estimate_basis")
    if basis == BASIS_INGREDIENT_MATH:
        return
    if basis == BASIS_LABEL_MATH or has_nutrition_label(hint_text):
        food["estimate_basis"] = BASIS_LABEL_MATH
        food.setdefault("estimate_confidence", CONFIDENCE_HIGH)
        return
    food["estimate_basis"] = BASIS_STANDARD_SERVING
    food["estimate_confidence"] = CONFIDENCE_LOW


def _apply_ingredient_math(
    food: dict[str, Any],
    mentions: Sequence[IngredientMention],
    *,
    lookup: FoodLookup | None = None,
) -> None:
    totals = sum_nutrition(mentions)
    total_grams = sum(mention.grams for mention in mentions)
    food["estimated_calories"] = int(round(totals["energy_kcal"]))
    food["nutrients"] = {
        "energy_kcal": int(round(totals["energy_kcal"])),
        "protein_g": round(totals["protein_g"], 1),
        "fat_g": round(totals["fat_g"], 1),
        "carbohydrates_g": round(totals["carbohydrates_g"], 1),
        "sodium_mg": int(round(totals["sodium_mg"])),
    }
    if total_grams > 0:
        food["weight_g"] = int(round(total_grams))
    ingredients: list[dict[str, Any]] = []
    sources: list[str] = []
    for mention in mentions:
        payload = mention.to_dict()
        # 每个配料也标清「这条数值是哪一层给的」（层① 自建库 / 成分表 / …）
        resolved = lookup_food_nutrition(mention.name, lookup=lookup)
        if resolved is not None:
            payload["source"] = resolved.source
            payload["source_label"] = resolved.source_label
            payload["matched_name"] = resolved.matched_name
            payload["per_100g"] = {
                key: round(float(value), 2) for key, value in resolved.per_100g.items()
            }
            if resolved.source_label not in sources:
                sources.append(resolved.source_label)
        ingredients.append(payload)
    food["ingredients"] = ingredients
    if sources:
        food["nutrition_source_labels"] = sources
    food["estimate_basis"] = BASIS_INGREDIENT_MATH
    food["estimate_confidence"] = CONFIDENCE_HIGH


# --------------------------------------------------------------------------------------
# 四层查询链（food_lookup）接入
# --------------------------------------------------------------------------------------


def resolve_food_lookup(lookup: FoodLookup | None = None) -> FoodLookup | None:
    """拿默认查询引擎。查询层本身出任何问题都返回 None —— 静默降级，不抛错。"""
    if lookup is not None:
        return lookup
    try:
        from life_logger.food_lookup import default_food_lookup

        return default_food_lookup()
    except Exception:
        return None


def lookup_food_nutrition(
    name: str, *, lookup: FoodLookup | None = None
) -> FoodNutrition | None:
    """按食物名查四层链：自建品牌库 → 中国食物成分表 → USDA → Open Food Facts。"""
    engine = resolve_food_lookup(lookup)
    if engine is None:
        return None
    try:
        return engine.lookup(name)
    except Exception:
        return None


def _library_payload(result: FoodNutrition, *, grams: float | None) -> dict[str, Any]:
    """把查询结果拍平成可写进 food 载荷的可追溯字段。"""
    serving = grams or result.effective_serving_g()
    payload: dict[str, Any] = {
        "source": result.source,
        "source_label": result.source_label,
        "matched_name": result.matched_name,
        "confidence": result.confidence,
        "match_kind": result.match_kind,
        "per_100g": {key: round(float(value), 2) for key, value in result.per_100g.items()},
        "serving_g": round(float(serving), 1) if serving else None,
        "serving_label": result.serving_label,
        "serving_source": SERVING_SPOKEN if grams else result.serving_source,
    }
    if result.cached:
        payload["cached"] = True
    if result.recipe:
        payload["recipe"] = [{"item": item, "grams": item_grams} for item, item_grams in result.recipe]
    return payload


def _apply_library_lookup(
    food: dict[str, Any],
    name: str,
    *,
    lookup: FoodLookup | None = None,
    grams: float | None = None,
) -> bool:
    """按**食物名**查库并落到 food 载荷。

    返回是否**覆盖了**模型估值：

    * 查到且置信度够（high/medium）→ 覆盖 ``estimated_calories``/``nutrients``，
      ``estimate_basis`` 标成 ``library_math``，并把来源/命中名/每100g 一起写进去。
    * 查到但置信度只有 low（模糊匹配）→ 只挂 ``nutrition_reference`` 参考块，不动数值。
    * 查不到 → 返回 False，交给原来的「按标准份估」标记。
    """
    result = lookup_food_nutrition(name, lookup=lookup)
    if result is None:
        return False
    payload = _library_payload(result, grams=grams)
    if result.confidence not in _OVERRIDE_CONFIDENCES:
        food["nutrition_reference"] = payload
        return False
    serving = payload.get("serving_g")
    if not serving:
        food["nutrition_reference"] = payload
        return False
    values = result.scale(float(serving))
    if values.get("energy_kcal") is None:
        food["nutrition_reference"] = payload
        return False
    food["estimated_calories"] = int(round(values["energy_kcal"]))
    food["nutrients"] = {
        "energy_kcal": int(round(values.get("energy_kcal") or 0.0)),
        "protein_g": round(values.get("protein_g") or 0.0, 1),
        "fat_g": round(values.get("fat_g") or 0.0, 1),
        "carbohydrates_g": round(values.get("carbohydrates_g") or 0.0, 1),
        "sodium_mg": int(round(values.get("sodium_mg") or 0.0)),
    }
    food["weight_g"] = int(round(float(serving)))
    food["estimate_basis"] = BASIS_LIBRARY_MATH
    food["estimate_confidence"] = result.confidence
    food["nutrition_source"] = payload["source"]
    food["nutrition_source_label"] = payload["source_label"]
    food["nutrition_matched_name"] = payload["matched_name"]
    food["nutrition_serving_g"] = payload["serving_g"]
    food["nutrition_serving_source"] = payload["serving_source"]
    food["nutrition_per_100g"] = payload["per_100g"]
    if payload.get("match_kind"):
        food["nutrition_match_kind"] = payload["match_kind"]
    if payload.get("recipe"):
        food["nutrition_recipe"] = payload["recipe"]
    return True


def nutrition_for_item(
    item: str, grams: float | None = None, *, lookup: FoodLookup | None = None,
) -> dict[str, Any] | None:
    """按食物名查四层库并折算到 ``grams``（活动层 food 事实走同一条链，Q911-E）。

    返回可直接合并进 food 载荷的字段；查不到/置信度不够返回 ``None``
    （调用方保持「未知」，**绝不当 0**）。

    * ``grams`` 给了 → 按克数算，``estimate_basis="library_math"``、``serving_source="spoken"``；
    * 没给 → 用库的默认份量，``estimate_basis="standard_serving"``、``estimate_confidence="low"``，
      并标 ``estimated_serving=True``，让上层知道这是估算。
    """
    name = str(item or "").strip()
    if not name:
        return None
    result = lookup_food_nutrition(name, lookup=lookup)
    if result is None or result.confidence not in _OVERRIDE_CONFIDENCES:
        return None
    serving = float(grams) if grams and grams > 0 else float(result.effective_serving_g() or 0.0)
    if serving <= 0:
        return None
    values = result.scale(serving)
    if values.get("energy_kcal") is None:
        return None
    spoken = bool(grams and grams > 0)
    # 缺项必须保持未知：0.0 会被下游当成「确实没有碳水/钠」，把估算变成假精确。
    known = {key: values[key] for key in FOOD_NUTRIENT_KEYS if values.get(key) is not None}
    missing = [key for key in FOOD_NUTRIENT_KEYS if key not in known]
    nutrients = {
        "energy_kcal": int(round(known["energy_kcal"])) if "energy_kcal" in known else None,
        "protein_g": round(known["protein_g"], 1) if "protein_g" in known else None,
        "fat_g": round(known["fat_g"], 1) if "fat_g" in known else None,
        "carbohydrates_g": round(known["carbohydrates_g"], 1) if "carbohydrates_g" in known else None,
        "sodium_mg": int(round(known["sodium_mg"])) if "sodium_mg" in known else None,
    }
    basis = result.estimate_basis or (BASIS_LIBRARY_MATH if spoken else BASIS_STANDARD_SERVING)
    estimate_confidence = result.estimate_confidence or (result.confidence if spoken else CONFIDENCE_LOW)
    return {
        "estimated_calories": int(round(values["energy_kcal"])),
        "nutrients": nutrients,
        "missing_nutrients": missing,
        "weight_g": int(round(serving)),
        "estimate_basis": basis,
        "estimate_confidence": estimate_confidence,
        "estimated_serving": not spoken,
        "nutrition_source": result.source,
        "nutrition_source_label": result.source_label,
        "nutrition_matched_name": result.matched_name,
        "nutrition_serving_g": round(serving, 1),
        "nutrition_serving_source": SERVING_SPOKEN if spoken else result.serving_source,
        "nutrition_per_100g": {k: round(float(v), 2) for k, v in result.per_100g.items()},
    }


def _spoken_weight_for(
    item_name: str,
    event: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    window_seconds: float,
) -> float | None:
    """在**同一时间窗**的本人原话里找「原话自己报的克重」（如「两百克乌冬面」）。

    这条是为了补上「配料表里没有、但原话报了克重」的食物：名字能查到、克重也说了，
    就不该退回经验份量。只认克/毫升级单位，且必须与事件在同一窗口内。
    """
    needles = _spoken_weight_needles(item_name)
    if not needles:
        return None
    best: tuple[float, float] | None = None  # (归并代价, 克重)
    for row in records:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("source_label") or "self") == "non_self":
            continue
        text = str(row.get("text") or "")
        if not text or not any(needle in text for needle in needles):
            continue
        cost = _match_cost(
            _record_offset(row), str(row.get("segment_id") or ""), event, window_seconds
        )
        if cost is None:
            continue
        for needle in needles:
            for hit in re.finditer(re.escape(needle), text):
                for candidate in (
                    _quantity_before(text, hit.start()),
                    _quantity_after(text, hit.end()),
                ):
                    if candidate is None:
                        continue
                    match, _span = candidate
                    if match.group("unit") not in _WEIGHT_UNITS:
                        continue
                    quantity = parse_quantity(match.group("qty"))
                    if not quantity or quantity <= 0:
                        continue
                    grams = quantity * _WEIGHT_UNITS[match.group("unit")]
                    if not 0 < grams <= _MAX_MENTION_GRAMS:
                        continue
                    if best is None or cost < best[0]:
                        best = (cost, grams)
    return best[1] if best is not None else None


def _spoken_weight_needles(item_name: str) -> list[str]:
    """原话里该找哪些名字：先整名，再退到「去掉做法修饰」的食物本体。

    为什么需要（2026-11-06 Q911-A 实测）：事件名是「蒸土豆」「腊肠（蒸）」，
    原话是「三百克蒸好的土豆」——整名不是原话子串，于是 `_spoken_weight_for` 返回 None，
    100 克默认份量就覆盖了用户明说的 300 克（10-26 蒸土豆 77kcal 应为 231kcal）。
    """
    raw = str(item_name or "").strip()
    if not raw:
        return []
    needles: list[str] = []
    for candidate in (raw, raw.split("（")[0].split("(")[0]):
        candidate = candidate.strip()
        if len(candidate) >= 2 and candidate not in needles:
            needles.append(candidate)
    try:
        from life_logger.nutrition_meal_rules import name_core

        core = name_core(raw)
        if len(core) >= 2 and core not in needles:
            needles.append(core)
    except Exception:                      # 规则模块缺失也绝不影响主链路
        pass
    # 长的优先：整名命中时不再退到本体，避免「腊肠（蒸）」误配到别的配料
    return needles


def apply_food_nutrition_calibration(
    events: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    *,
    window_seconds: float = DEFAULT_MATCH_WINDOW_SECONDS,
    lookup: FoodLookup | None = None,
) -> dict[str, Any]:
    """按「同一会话里的配料 + 克重」重算 food 事件的热量/营养；查不到就按名字查本地营养库。

    * ``events`` 会被就地修改（只改 ``data.food`` 的数值并补字段）。
    * 返回统计摘要，便于写进 context-extraction.json 追溯。

    三条路，优先级从高到低：

    1. **配料乘除**（``ingredient_math``，高置信度）：原话给了「配料 + 克重」。
    2. **按名字查库**（``library_math``，中/高）：原话只报了食物名（「喝了个生椰拿铁」），
       用四层查询链（自建品牌库 → 中国食物成分表 → USDA → OFF）的每 100g × 份量。
    3. **按标准份估**（``standard_serving``，低）：查不到 —— 保留模型估值，只标记低置信度。
    """
    summary: dict[str, Any] = {
        "food_events": 0,
        "ingredient_math": 0,
        "library_math": 0,
        "library_reference": 0,
        "standard_serving": 0,
        "unmatched_mentions": 0,
    }
    food_events: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for event in list(events):
        food = _food_payload(event)
        if food is None:
            continue
        food_events.append((event, food))
    summary["food_events"] = len(food_events)
    if not food_events:
        return summary

    # 1) 收集本人原话里的配料提及（同一句重复出现只算一次，ASR 跨段重叠会带来重复）
    candidate_mentions: list[tuple[IngredientMention, float | None, str, str]] = []
    seen_texts: set[str] = set()
    text_by_source: dict[str, str] = {}
    text_by_segment: dict[str, list[str]] = {}
    for row in records:
        if not isinstance(row, Mapping):
            continue
        source_id = str(row.get("source_id") or "")
        raw_text = str(row.get("text") or "").strip()
        if source_id and raw_text:
            text_by_source[source_id] = raw_text
        segment = str(row.get("segment_id") or "")
        if segment and raw_text:
            text_by_segment.setdefault(segment, []).append(raw_text)
        if str(row.get("source_label") or "self") == "non_self":
            continue
        text = raw_text
        if not text or text in seen_texts:
            continue
        seen_texts.add(text)
        mentions = extract_ingredient_mentions(text)
        if not mentions:
            continue
        offset = _record_offset(row)
        for mention in mentions:
            candidate_mentions.append((mention, offset, segment, text))

    def event_hint(event: Mapping[str, Any]) -> str:
        """事件自身的原文（含 quote / 所属 source / 所属段），用于识别「原话自带成分表」。"""
        parts = [str(event.get("title") or "")]
        evidence = event.get("evidence")
        if isinstance(evidence, Mapping):
            parts.append(str(evidence.get("quote") or ""))
            parts.append(text_by_source.get(str(evidence.get("source_id") or ""), ""))
            parts.extend(text_by_segment.get(str(evidence.get("segment_id") or ""), []))
        return " ".join(part for part in parts if part)

    if not candidate_mentions:
        for event, food in food_events:
            _resolve_food_event(
                food,
                event,
                hint_text=event_hint(event),
                records=records,
                window_seconds=window_seconds,
                lookup=lookup,
                summary=summary,
            )
        return summary

    # 2) 每条提及归到最近的 food 事件（每条只归一个，避免重复计）
    assigned: dict[int, list[IngredientMention]] = {index: [] for index in range(len(food_events))}
    for mention, offset, segment, _text in candidate_mentions:
        best_index: int | None = None
        best_cost: float | None = None
        for index, (event, _food) in enumerate(food_events):
            cost = _match_cost(offset, segment, event, window_seconds)
            if cost is None:
                continue
            if best_cost is None or cost < best_cost:
                best_index, best_cost = index, cost
        if best_index is None:
            summary["unmatched_mentions"] += 1
            continue
        assigned[best_index].append(mention)

    # 3) 逐事件落地：有克重级配料 → 配料乘除；否则按名字查库 → 再否则标标准份
    for index, (event, food) in enumerate(food_events):
        mentions = assigned.get(index) or []
        if mentions and has_weight_based_mention(mentions):
            _apply_ingredient_math(food, mentions, lookup=lookup)
            summary["ingredient_math"] += 1
            continue
        _resolve_food_event(
            food,
            event,
            hint_text=event_hint(event),
            records=records,
            window_seconds=window_seconds,
            lookup=lookup,
            summary=summary,
        )
    return summary


def _resolve_food_event(
    food: dict[str, Any],
    event: Mapping[str, Any],
    *,
    hint_text: str,
    records: Sequence[Mapping[str, Any]],
    window_seconds: float,
    lookup: FoodLookup | None,
    summary: dict[str, Any],
) -> None:
    """没有配料克重时的那条路：按食物名查本地营养库 → 原话报过的克重 → 标准份。

    ``summary`` 就地累加计数（``library_math`` / ``library_reference`` / ``standard_serving``）。
    """
    item_name = str(food.get("item") or "").strip()
    if not item_name:
        _mark_standard_serving(food, hint_text=hint_text)
        summary["standard_serving"] += 1
        return
    # 原话里「两百克乌冬面」这种自己报过的克重（配料表里没有的食物也能吃到这条）
    spoken_grams = _spoken_weight_for(item_name, event, records, window_seconds=window_seconds)
    if _apply_library_lookup(food, item_name, lookup=lookup, grams=spoken_grams):
        summary["library_math"] += 1
        return
    if food.get("nutrition_reference"):
        # 查到了但只是模糊命中：数值不动，只挂参考块
        _mark_standard_serving(food, hint_text=hint_text)
        summary["library_reference"] += 1
        summary["standard_serving"] += 1
        return
    _mark_standard_serving(food, hint_text=hint_text)
    summary["standard_serving"] += 1
