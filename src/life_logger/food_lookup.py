# -*- coding: utf-8 -*-
"""四层食物营养查询链：按名字查到可信的「每 100g」成分值，且每条都带来源。

为什么存在（2026-10-13 用户拍板的架构）：

    你说吃了什么
        ↓
    ① 品牌库（自建：用户念过/拍过的营养成分表）   ← 本地，优先级最高
        ↓ 没命中
    ② 中国食物成分表（官方库）                      ← 本地
        ↓ 没命中
    ③ USDA 精选（pyfooda 随包数据）                 ← 本地
        ↓ 没命中
    ④ Open Food Facts（联网）→ **查到就写回①**      ← 这就是「后台记忆」

要解决的缺陷：``food_nutrition.apply_food_nutrition_calibration`` 只在原话报了**克重**
时才做乘除换算（「一百四十克菲诺的椰乳」），绝大多数时候（「喝了个生椰拿铁」）只能沿用
模型的模糊估值。本模块补上「**没报克重也能按名字查到可靠数值**」这条腿。

设计约定
--------

* **层间短路**：上一层命中就返回，不再往下问（OFF 是联网层，能不打就不打）。
* **静默降级**：任何一层缺文件 / 读坏了 / 没网 / 字段怪，都当作「这层没命中」，
  绝不让查询变成新的失败点（参照 ``dictation.load_dictation_windows`` 的风格）。
* **可追溯**：返回值带 ``source``（哪一层）+ ``confidence``（多确信）+ ``matched_name``
  （库里实际命中的条目名），供前端显示「（中国食物成分表）」这类标签。
* **不猜**：模糊匹配有阈值下限（``_FUZZY_MIN_RATIO``），宁可返回 None 让上游保留原估值。

数据文件（``output/local-web/food-data/``，由 ``scripts/build_food_data.py`` 编译）：

* ``cfct.json``        中国食物成分表（第6版）1661 条
* ``foodwake.json``    foodwake 中文食物成分 1607 条
* ``usda-subset.json`` USDA FoodData Central 通用食物 7311 条
* ``brand-library.json`` 自建品牌库（用户念过/拍过 + OFF 写回），运行期可写

路径可用环境变量 ``LIFE_LOGGER_FOOD_DATA`` 覆盖（指向目录）。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
from datetime import datetime
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------

NUTRIENT_KEYS: tuple[str, ...] = (
    "energy_kcal",
    "protein_g",
    "fat_g",
    "carbohydrates_g",
    "sodium_mg",
)

# 层名（写进 food 载荷的 nutrition_source 字段，前端据此显示来源标签）
SOURCE_BRAND = "brand"
SOURCE_CFCT = "cfct"
SOURCE_FOODWAKE = "foodwake"
SOURCE_USDA = "usda"
SOURCE_OFF = "off"
SOURCE_BOOHEE = "boohee"

SOURCE_LABELS: Mapping[str, str] = {
    SOURCE_BRAND: "自建品牌库",
    SOURCE_CFCT: "中国食物成分表",
    SOURCE_FOODWAKE: "食物库",
    SOURCE_USDA: "USDA",
    SOURCE_OFF: "Open Food Facts",
    SOURCE_BOOHEE: "薄荷健康",
}

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

_CONFIDENCE_ORDER: Mapping[str, int] = {
    CONFIDENCE_HIGH: 2,
    CONFIDENCE_MEDIUM: 1,
    CONFIDENCE_LOW: 0,
}

# 命中方式 → 置信度（可追溯：「这是精确命中」还是「这是模糊猜的」）
MATCH_EXACT = "exact"
MATCH_ALIAS = "alias"
MATCH_CONTAINS = "contains"
MATCH_TOKENS = "tokens"
MATCH_FUZZY = "fuzzy"
MATCH_RECIPE = "recipe"

# 候选排序偏好：同名同义条目里挑「最像人日常吃的那一份」。
# 成分表里往往同时躺着「香蕉（红皮）」「香蕉［甘蕉］」这类变体，谁先建索引谁是
# 按文件顺序撞运气 —— 这里用打分统一排队，规则全部写在明面上：
#   代表值/均值（官方定义的标准条目）> 熟食做法 > 生/干/粉/速溶/半成品 > 名字短
_POSITIVE_QUALIFIERS: tuple[str, ...] = (
    "代表值",
    "均值",
    "酿造",
    "冲泡",
    "用水制备",
    "熟",
    "蒸",
    "煮",
    "烤",
    "煎",
    "炒",
    "炖",
    "焖",
    "cooked",
    "roasted",
    "grilled",
    "baked",
    "boiled",
    "steamed",
    "stewed",
    "sauteed",
)
_NEGATIVE_QUALIFIERS: tuple[str, ...] = (
    "速溶",
    "脱水",
    "干制",
    "粉",
    "生鲜",
    "uncooked",
    "raw",
    "dry",
    "powder",
    "dehydrated",
    "concentrate",
    "babyfood",
    "infant",
    "frozen",
    "breaded",
    "candies",
    "snack",
    # 「另一个食物」词：查询是基底食物名（rice/egg/milk/chicken breast）时，
    # 这些词意味着端上来的是**别的东西**（米饭 vs 米饼、鸡蛋 vs 蛋酒、鸡胸 vs 鸡肉卷）
    "cake",
    "nog",
    "vinegar",
    "flour",
    "cracker",
    "cookie",
    "pudding",
    "beverage",
    "syrup",
    "candy",
    "sauce",
    "soup",
    "noodle",
    "roll",
    "spread",
    "tender",
    "deli",
    "burrito",
    "sandwich",
    "burger",
    "pizza",
    "pie",
    "tart",
    "muffin",
    "biscuit",
    "waffle",
    "nugget",
    "stick",
    "patty",
    "meatball",
    "lasagna",
    "casserole",
    "dressing",
    "gravy",
    "milk",
    "yogurt",
    "cream",
    "butter",
    "juice",
    "drink",
    "soda",
    "salad",
    "feet",
    "giblet",
    "neck",
    "heart",
    "liver",
    "kidney",
    "bacon",
    "cured",
    "stuffing",
    "fried",
    "instant",
    "tube",
    # 内脏/边角部位：说「鸡肉/猪肉」时不该落到鸡背、猪耳朵上
    "back",
    "ear",
    "snout",
    "tail",
    "tongue",
    "brain",
    "tripe",
    "gizzard",
    "deviled",
)
_NEGATIVE_RE = re.compile("|".join(re.escape(word) for word in _NEGATIVE_QUALIFIERS))
_POSITIVE_RE = re.compile("|".join(re.escape(word) for word in _POSITIVE_QUALIFIERS))
# 全大写单词 = USDA 里的连锁/品牌条目（"APPLEBEE'S, ..."），降权
_BRANDISH_RE = re.compile(r"\b[A-Z]{3,}\b")
# 打分 → 置信度：≥0.8 可以覆盖模型估值，0.3~0.8 只作参考，<0.3 直接判不命中
_SCORE_MEDIUM = 0.8
_SCORE_MIN = 0.3
# 中文包含命中还要看长度比例：「某个很长的食物名」套上「某个很长的食物名字」这种
# 查询只占名字一角的情况，不许升到 medium（medium 才有权覆盖模型估值）。
_MIN_CONTAINS_COVERAGE = 0.6

_LATIN_WORD_RE = re.compile(r"[a-z0-9]+")


def _entry_score(name: str, *, query_tokens: frozenset[str] = frozenset(), prefix: str = "") -> float:
    """条目名打分：越大越该被选中。规则见 ``_POSITIVE_QUALIFIERS`` 注释。"""
    normalized = normalize_name(name)
    if not normalized:
        return -1.0
    score = 1.0
    score += 0.4 * len(_POSITIVE_RE.findall(normalized))
    score -= 0.6 * len(_NEGATIVE_RE.findall(normalized))
    if _BRANDISH_RE.search(name):
        score -= 0.8
    score -= 0.02 * len(normalized)
    if query_tokens:
        tokens = _tokenize(name)
        extra = len(tokens - query_tokens)
        score -= 0.15 * extra
        if prefix and normalized.startswith(prefix):
            score += 0.4
    return score


def _is_structural_match(name_key: str, normalized: str) -> bool:
    """条目名是否「就是这个食物」（而不只是含这个词）。

    ``Milk, whole`` ↔ ``milk`` → True（查询词打头，后面直接跟逗号）
    ``Rice milk`` ↔ ``rice`` → False（那是另一种东西）
    复数后缀也算：``Apples, raw`` ↔ ``apple`` → True。
    """
    if not name_key or not normalized:
        return False
    if name_key == normalized:
        return True
    for tail in ("", "s", "es"):
        prefix = normalized + tail
        if name_key.startswith(prefix) and len(name_key) > len(prefix) and name_key[len(prefix)] == ",":
            return True
    return False


def _tokenize(text: str) -> frozenset[str]:
    return frozenset(_LATIN_WORD_RE.findall(str(text or "").lower()))


# 份量从哪来（没报克重时，必须说清这个「标准份量」是谁定的）
SERVING_LIBRARY = "library"  # 库里自带（品牌条目 unit_grams / USDA portion）
SERVING_HINT = "hint"        # 常见份量经验表（本文件 SERVING_HINTS）
SERVING_RECIPE = "recipe"    # 复合配方里各配料克重相加
SERVING_SPOKEN = "spoken"    # 原话里直接报出的克重（最准）
SERVING_NONE = "none"

DEFAULT_SERVING_G = 100.0

# 库里的「一份」上限：超过它多半是整包/整块而不是一顿的量（见 _entry_serving）。
_MAX_TRUSTED_SERVING_G = 400.0

# 模糊匹配下限：低于它宁可返回 None。0.75 = 「鸡胸肉 ↔ 鸡胸脯肉」(0.857) 能过，
# 「鸭肉 ↔ 鸡肉」这种只差一个字但意思不同的（0.5）过不了。
_FUZZY_MIN_RATIO = 0.75

# 没报克重时按常见份量估。**仅在没有更好信息时使用**，且会在载荷里标 serving_source=hint。
# 单位意图是「一份/一碗/一杯」的常见克重，不是精确值。
SERVING_HINTS: tuple[tuple[tuple[str, ...], float, str], ...] = (
    (("米饭", "白饭", "蒸饭", "盖饭", "炒饭", "拌饭"), 200.0, "碗"),
    (("面条", "拉面", "汤面", "面线", "粉", "河粉", "米线", "乌冬"), 300.0, "碗"),
    (("粥", "稀饭", "羹", "汤"), 300.0, "碗"),
    (("馒头", "花卷", "包子"), 100.0, "个"),
    (("饺子", "水饺", "云吞", "馄饨"), 250.0, "份"),
    (("面包", "吐司", "吐司片"), 60.0, "片"),
    (("鸡蛋", "水煮蛋", "煎蛋", "茶叶蛋"), 50.0, "个"),
    (("牛奶", "豆浆", "酸奶", "拿铁", "咖啡", "奶茶", "果汁", "饮料", "可乐", "啤酒", "椰乳", "燕麦奶", "苏打水", "气泡水", "茶"), 250.0, "杯"),
    (("油条", "春卷", "鸡翅", "鸡腿"), 60.0, "根"),
    (("苹果", "梨", "橙", "橘", "桃", "香蕉", "芒果", "柿子"), 180.0, "个"),
    (("牛肉", "猪肉", "鸡肉", "羊肉", "鱼", "虾", "豆腐", "鸡胸", "排骨", "炒菜", "青菜", "蔬菜", "沙拉"), 150.0, "份"),
    (("坚果", "花生", "瓜子", "核桃", "巴旦木", "开心果"), 30.0, "小包"),
    (("薯条", "薯片", "饼干", "蛋糕", "点心", "巧克力"), 50.0, "份"),
)

# 复合配方（层①的一部分）：用户自己报过的配方，按「配料(层①条目) + 默认克重」展开。
# 只有**没报克重**时才用；报了克重走 food_nutrition 的配料乘除（那条路更准）。
# 生椰拿铁这一条 = 2026-10-12 用户自己做的配方（菲诺厚椰乳 + 单份浓缩）。
COMPOSITE_RECIPES: Mapping[str, tuple[tuple[str, float], ...]] = {
    "生椰拿铁": (("菲诺厚椰乳", 200.0), ("意式浓缩", 30.0)),
    "燕麦拿铁": (("燕麦奶", 200.0), ("意式浓缩", 30.0)),
    "美式咖啡": (("意式浓缩", 30.0),),
    "浓缩咖啡": (("意式浓缩", 30.0),),
}

# 口语 → 库内条目名。只收「去掉修饰后仍对不上」的常见说法。
_ALIASES: Mapping[str, str] = {
    "白米饭": "米饭",
    "大米饭": "米饭",
    "蒸米饭": "米饭",
    "鸡胸肉": "鸡胸脯肉",
    "鸡胸": "鸡胸脯肉",
    "全脂奶": "纯牛奶（代表值，全脂）",
    "牛奶": "纯牛奶（代表值，全脂）",
    "鲜牛奶": "纯牛奶（代表值，全脂）",
    "白煮蛋": "鸡蛋",
    "水煮蛋": "鸡蛋",
    "瘦肉": "猪里脊肉",
    "培根": "培根（猪肉）",
    # [2026-11-06] 吃的是熟的：标准份给的是「一碗 300g（熟重）」，不能配生面/干面的成分。
    # 只指向成分表里**已经存在**的熟制条目（面条（富强粉，煮）107 kcal/100g），
    # 不新增任何成分数据。河粉/米线/米粉/乌冬没有熟制条目，保持现状（见报告）。
    "面条": "面条（富强粉，煮）",
    "汤面": "面条（富强粉，煮）",
    "拉面": "面条（富强粉，煮）",
    "面线": "面条（富强粉，煮）",
    "乌冬": "面条（富强粉，煮）",
}

#: [2026-11-08] 明确按「生米/生重」记录的主食 → 指向中国食物成分表「稻米（代表值）」（346 kcal/100g），
#: 避免「生米」模糊命中「炝花生米」（529 kcal）或被当成熟米饭（116 kcal）。
_RAW_ALIASES: Mapping[str, str] = {
    "生米": "稻米（代表值）",
    "生大米": "稻米（代表值）",
    "生米饭": "稻米（代表值）",
    "大米": "稻米（代表值）",
    "米饭（生重）": "稻米（代表值）",
    "大米（生重）": "稻米（代表值）",
    "米（生重）": "稻米（代表值）",
    "生重米": "稻米（代表值）",
    "生重米饭": "稻米（代表值）",
    "生重大米": "稻米（代表值）",
}

#: 出现这些词说明用户说的是**生料/干品/购买**，不能套「熟制」别名。
_RAW_CONTEXT_MARKERS: tuple[str, ...] = (
    "干面", "挂面", "生面", "未煮", "没煮", "生粉", "干粉", "面粉", "干河粉",
    "买", "买了", "买了一包", "一包", "一袋", "一箱", "生", "干",
)


def _raw_context(query: str) -> bool:
    """是不是生料/购买语境。命中就不做熟制别名替换。"""
    text = str(query or "")
    return any(marker in text for marker in _RAW_CONTEXT_MARKERS)

_OFF_ENDPOINT = "https://world.openfoodfacts.org/cgi/search.pl"
_OFF_USER_AGENT = "LifeLogger/1.0 (personal voice life log; contact: local user)"
_OFF_TIMEOUT_SECONDS = 8.0
_OFF_MAX_CANDIDATES = 5

# 千焦 → 千卡（OFF 有些产品只给 kJ）
_KJ_PER_KCAL = 4.184

_ENV_DATA_DIR = "LIFE_LOGGER_FOOD_DATA"
_ENV_OFF_DISABLE = "LIFE_LOGGER_FOOD_OFF"
_ENV_OFF_FORCE = "LL_FOOD_OFF"
_ENV_BOOHEE_DISABLE = "LIFE_LOGGER_FOOD_BOOHEE_OFF"
#: 联网层（OFF/薄荷）查不到的名字多久内不再联网重查。
ONLINE_MISS_TTL_SECONDS = 7 * 24 * 3600
_ENV_BOOHEE_FORCE = "LL_FOOD_BOOHEE"

_BRACKET_RE = re.compile(r"[（(\[［【][^）)\]］】]*[）)\]］】]")
_SPACE_RE = re.compile(r"[\s\u3000]+")


# --------------------------------------------------------------------------------------
# 名称归一化与匹配
# --------------------------------------------------------------------------------------


#: 复合菜品词族：查询里出现这些词时，命中的条目**必须同属这一族**，
#: 否则就是「拿原料凑菜品」（2026-11-06：猪肉芹菜饺子 → 猪肉（清蒸））。
_DISH_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("饺子", "水饺", "锅贴"),
    ("馄饨", "云吞", "抄手"),
    ("包子", "肉包", "菜包", "小笼包"),
    ("面条", "拉面", "汤面", "炒面", "拌面", "刀削面"),
    ("炒饭", "盖饭", "盖浇饭", "拌饭"),
    ("汉堡", "三明治", "披萨", "卷饼", "馅饼"),
    ("粥", "汤", "沙拉", "寿司"),
    ("肉丸", "丸子", "胸肉丸", "牛肉丸", "鱼丸", "虾丸"),
    ("料理包",),
)


def dish_family(name: str) -> str | None:
    """返回菜品词族标识（``饺子`` 与 ``水饺`` 同族）；不是复合菜名返回 None。"""
    normalized = normalize_name(name)
    for members in _DISH_FAMILIES:
        for word in members:
            if word in normalized:
                return members[0]
    return None


def dish_guard_allows(query: str, entry_name: str) -> bool:
    """查询是复合菜名时，条目也必须是同一菜品族。

    「猪肉芹菜饺子」可以有权威饺子条目就命中它；没有就返回未知，
    绝不回退到「猪肉」把一道菜算成一块肉。
    """
    family = dish_family(query)
    if family is None:
        return True
    return dish_family(entry_name) == family

def normalize_name(text: str) -> str:
    """把名字归一化成可比较的形式：全角→半角、去空白、去标点、小写。

    只做**不会改变词义**的变形（不删「的」「大」「小」这类实词），
    否则「生椰」与「生椰乳」会被错误等同。
    """
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", str(text))
    value = _SPACE_RE.sub("", value)
    value = value.strip("，,。.、:：;；!！?？~～-—_\"'“”‘’")
    return value.lower()


def strip_brackets(text: str) -> str:
    """去掉括号补充说明：``米饭（蒸，代表值）`` → ``米饭``；``纯牛奶（全脂，光明牌）`` → ``纯牛奶``。"""
    if not text:
        return ""
    return _BRACKET_RE.sub("", str(text)).strip()


def _off_name_matches(query: str, product_name: Any) -> bool:
    """OFF 商品名与查询词是否相关（防止把无关商品写回品牌库）。

    规则：两边都去掉括号补充与标点后，
      * 任一方向包含另一半即可（「无醇啤酒」↔「双柚风味无醇拉格啤酒」）；
      * 任一边核心长度 <2 一律判不相关（单字没有判别力）。
    """
    q = normalize_name(strip_brackets(str(query or "")))
    name = str(product_name or "")
    p = normalize_name(strip_brackets(name))
    if len(q) < 2 or len(p) < 2:
        return False
    if q in p or p in q:
        return True
    # 「无醇啤酒」vs「双柚风味无醇拉格啤酒」：文字被品牌词隔开，用字符覆盖判相关。
    q_chars, p_chars = set(q), set(p)
    return q_chars <= p_chars or p_chars <= q_chars


def _candidates_for(query: str) -> list[str]:
    """一次查询要试的几种写法（顺序即优先级）。"""
    normalized = normalize_name(query)
    out: list[str] = []
    if not normalized:
        return out
    stripped = normalize_name(strip_brackets(normalized))
    raw_alias = _RAW_ALIASES.get(normalized) or (
        _RAW_ALIASES.get(stripped) if not _BRACKET_RE.search(str(query or "")) else None
    )
    if raw_alias:
        # 明确标了生重/生米（如「米饭（生重）」「生米饭」）时，绝不把去括号后的「米饭」放进候选，
        # 否则会在层① 命中熟米饭种子（116 kcal/100g）而抢走层② 的「稻米（代表值）」（346 kcal/100g）。
        raw_norm = normalize_name(raw_alias)
        out.append(raw_norm)
        raw_stripped = normalize_name(strip_brackets(raw_norm))
        if raw_stripped and raw_stripped not in out:
            out.append(raw_stripped)
        if normalized not in out:
            out.append(normalized)
        return out
    out.append(normalized)
    if stripped and stripped != normalized:
        out.append(stripped)
    alias = _ALIASES.get(normalized) or _ALIASES.get(stripped)
    # [2026-11-06] 「干面/挂面/生面/买了一包面条」说的是生料或购买语境，
    # 不能借别名换成熟制条目。生料保持原样走原查找链。
    if alias and not _raw_context(str(query or "")):
        # 别名要排在最前：否则「面条」会先命中「面条（生，代表值）」的去括号精确匹配，
        # 熟制别名永远轮不到。
        out.insert(0, normalize_name(alias))
    return out


def _similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right).ratio()


def _entry_has_energy(entry: Mapping[str, Any]) -> bool:
    """条目必须有热量才算可用：没热量的条目对「算吃了多少」毫无价值，
    留着只会在匹配时把更靠谱的同名条目挤掉（构建期已过滤，这里是第二道闸）。"""
    return _entry_per_100g(entry).get("energy_kcal") is not None


def _entry_per_100g(entry: Mapping[str, Any]) -> dict[str, float]:
    raw = entry.get("per_100g")
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, float] = {}
    for key in NUTRIENT_KEYS:
        value = raw.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _entry_serving(entry: Mapping[str, Any]) -> tuple[float | None, str | None]:
    """库里自带的「一份」克重。

    USDA 的 ``portion_gram_weight`` 偶尔是整包/整块（实测「Tofu, hard, prepared with
    nigari」写着 488g），超过 ``_MAX_TRUSTED_SERVING_G`` 就**不当作一份** ——
    宁可回退到经验份量表，也不要按 488g 豆腐去算一顿的量。
    """
    for key in ("serving_g", "unit_grams"):
        value = entry.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            grams = float(value)
        except (TypeError, ValueError):
            continue
        if 0 < grams <= _MAX_TRUSTED_SERVING_G:
            label = entry.get("serving_label") or entry.get("unit_label")
            return grams, (str(label) if label else None)
    return None, None


def _serving_hint(name: str) -> tuple[float, str] | None:
    """常见份量经验表：返回 (克重, 单位)。命中最长的关键词优先。"""
    normalized = normalize_name(name)
    best: tuple[int, float, str] | None = None
    for keywords, grams, unit in SERVING_HINTS:
        for keyword in keywords:
            key = normalize_name(keyword)
            if key and key in normalized:
                if best is None or len(key) > best[0]:
                    best = (len(key), grams, unit)
    if best is None:
        return None
    return best[1], best[2]


# --------------------------------------------------------------------------------------
# 结果对象
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FoodNutrition:
    """一次查询的结果：每 100g 成分 + 来源 + 置信度。"""

    query: str
    name: str
    matched_name: str
    per_100g: Mapping[str, float]
    source: str
    confidence: str
    match_kind: str = MATCH_EXACT
    serving_g: float | None = None
    serving_label: str | None = None
    serving_source: str = SERVING_NONE
    evidence: str = ""
    cached: bool = False
    recipe: tuple[tuple[str, float], ...] = ()
    note: str = ""
    #: 「nearest_match」/「median_of_candidates」等在线多候选挑选依据；本地命中为空
    estimate_basis: str = ""
    estimate_confidence: str = ""

    @property
    def source_label(self) -> str:
        return source_label(self.source)

    def scale(self, grams: float) -> dict[str, float]:
        """按克数换算本次摄入值（每 100g × 克数 / 100）。"""
        factor = float(grams) / 100.0
        return {key: round(float(value) * factor, 2) for key, value in self.per_100g.items()}

    def effective_serving_g(self, *, default: float | None = DEFAULT_SERVING_G) -> float | None:
        """本次要用的份量克重：库里的份量 → 经验份量 → ``default``。"""
        if self.serving_g and self.serving_g > 0:
            return float(self.serving_g)
        hint = _serving_hint(self.matched_name or self.name)
        if hint is not None:
            return hint[0]
        return default

    def describe(self) -> dict[str, Any]:
        """给前端/载荷用的可追溯摘要。"""
        payload: dict[str, Any] = {
            "query": self.query,
            "matched_name": self.matched_name,
            "source": self.source,
            "source_label": self.source_label,
            "confidence": self.confidence,
            "match_kind": self.match_kind,
            "per_100g": {key: round(float(value), 2) for key, value in self.per_100g.items()},
        }
        if self.serving_g:
            payload["serving_g"] = round(float(self.serving_g), 1)
            payload["serving_label"] = self.serving_label
            payload["serving_source"] = self.serving_source
        if self.cached:
            payload["cached"] = True
        if self.recipe:
            payload["recipe"] = [{"item": item, "grams": grams} for item, grams in self.recipe]
        if self.note:
            payload["note"] = self.note
        if self.estimate_basis:
            payload["estimate_basis"] = self.estimate_basis
        if self.estimate_confidence:
            payload["estimate_confidence"] = self.estimate_confidence
        return payload


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(str(source or ""), str(source or ""))


def source_tag(source: str, *, cached: bool = False) -> str:
    """前端标签：``（中国食物成分表）``。"""
    label = source_label(source)
    if not label:
        return ""
    return f"（{label}{'，已缓存' if cached else ''}）"


# --------------------------------------------------------------------------------------
# 本地索引（层②③）
# --------------------------------------------------------------------------------------


class LocalFoodIndex:
    """一份本地成分表的查询索引（中国食物成分表 / foodwake / USDA 都是这个类）。

    匹配分三段（顺序即优先级）：

    1. **精确**：归一化全名相等，或去掉括号补充说明后相等（``米饭（蒸，代表值）`` ↔ ``米饭``）。
    2. **词元**（英文查询）：查询的每个词都出现在条目名的词集合里，按打分排序
       （``chicken breast`` → ``Chicken breast, sauteed, skin not eaten``）。
    3. **包含**（中文查询）：互为子串，按打分排序（``咖啡`` → ``咖啡（酿造、用水制备）``）。
       外加编辑距离兜底（``鸡胸肉`` ↔ ``鸡胸脯肉``）。

    打分见 ``_entry_score``；低于 ``_SCORE_MIN`` 一律判不命中 —— 宁可返回 None，
    也不要拿「Rice cake」去糊弄「rice」。
    """

    def __init__(
        self,
        source: str,
        entries: Iterable[Mapping[str, Any]],
        *,
        exact_confidence: str = CONFIDENCE_HIGH,
        fuzzy_confidence: str = CONFIDENCE_LOW,
    ) -> None:
        self.source = source
        self.exact_confidence = exact_confidence
        self.fuzzy_confidence = fuzzy_confidence
        self.entries: list[Mapping[str, Any]] = []
        self._by_name: dict[str, list[Mapping[str, Any]]] = {}
        self._by_stripped: dict[str, list[Mapping[str, Any]]] = {}
        self._contains_pool: list[tuple[str, Mapping[str, Any]]] = []
        self._word_pool: list[tuple[frozenset[str], Mapping[str, Any]]] = []
        for item in entries:
            if not isinstance(item, Mapping):
                continue
            entry = dict(item)
            if not _entry_has_energy(entry):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            self.entries.append(entry)
            key = normalize_name(name)
            if key:
                self._by_name.setdefault(key, []).append(entry)
            stripped = normalize_name(strip_brackets(name))
            if stripped:
                self._by_stripped.setdefault(stripped, []).append(entry)
            self._contains_pool.append((stripped or key, entry))
            if _LATIN_WORD_RE.search(name):
                tokens = _tokenize(name)
                if tokens:
                    self._word_pool.append((tokens, entry))

    def __len__(self) -> int:
        return len(self.entries)

    # -- 内部 ------------------------------------------------------------------------
    @staticmethod
    def _pick(candidates: Sequence[Mapping[str, Any]], query: str, prefix: str = "") -> Mapping[str, Any]:
        """同名候选里挑一条：分数最高，分数相同取名字短的。"""
        tokens = _tokenize(query) if _LATIN_WORD_RE.search(query) else frozenset()
        ranked = sorted(
            candidates,
            key=lambda entry: (
                -_entry_score(str(entry.get("name") or ""), query_tokens=tokens, prefix=prefix),
                len(str(entry.get("name") or "")),
            ),
        )
        return ranked[0]

    def _scored(self, query: str) -> tuple[Mapping[str, Any], str, str] | None:
        """打分命中：英文走词元、中文走包含，再兜一层编辑距离。

        **英文分词必须用原始查询**：``normalize_name`` 会把空格吃掉，
        ``chicken breast`` 会变成 ``chickenbreast`` 一个词，整词匹配就全落空了。
        """
        normalized = normalize_name(strip_brackets(query))
        if len(normalized) < 2:
            return None
        if _LATIN_WORD_RE.search(normalized):
            return self._scored_latin(normalized, _tokenize(query))
        return self._scored_chinese(normalized)

    def _scored_latin(
        self, normalized: str, query_tokens: frozenset[str]
    ) -> tuple[Mapping[str, Any], str, str] | None:
        """英文查询：查询的每个词都要在条目名里出现（整词，不是子串）。

        用**一个连续打分**排序（而不是逐项比较字典序）——「多余词多但同族」与
        「多余词少但是另一道菜」之间需要能互相换算：

        ``1.0 全部词命中 + 1.0 名字就是这个食物（以查询开头且后面跟逗号）
        - 1.2×「另一道菜」词（cake/milk/salad/bacon…） + 0.35×做法词（cooked/boiled…）
        - 0.25×多余词数 - 0.8×品牌连锁 - 0.01×名字长度``

        只要沾了「另一道菜」或品牌词，置信度一律降到 low —— 只做参考，不覆盖模型估值。
        """
        if not query_tokens:
            return None
        ranked: list[tuple[float, int, Mapping[str, Any]]] = []
        for tokens, entry in self._word_pool:
            if not query_tokens <= tokens:
                continue
            name = str(entry.get("name") or "")
            name_key = normalize_name(name)
            extra_tokens = sorted(tokens - query_tokens)
            # 负面/正面判定只看**查询之外的**词：查「orange juice」时 juice 不该算负面
            negatives = sum(1 for word in extra_tokens if _NEGATIVE_RE.search(word))
            positives = sum(1 for word in extra_tokens if _POSITIVE_RE.search(word))
            brandish = 1 if _BRANDISH_RE.search(name) else 0
            score = 1.0
            if _is_structural_match(name_key, normalized):
                score += 1.0
            score -= 1.2 * negatives
            score += 0.5 * positives
            score -= 0.25 * len(extra_tokens)
            score -= 0.8 * brandish
            score -= 0.01 * len(name_key)
            ranked.append((score, negatives, entry))
        if not ranked:
            return None
        ranked.sort(key=lambda item: (-item[0], item[1]))
        _score, negatives, entry = ranked[0]
        # 沾了「另一道菜」词就降级：宁可只做参考，也不要拿「Rice milk」当「rice」
        confidence = CONFIDENCE_LOW if negatives else CONFIDENCE_MEDIUM
        return entry, MATCH_TOKENS, confidence

    def _scored_chinese(self, normalized: str) -> tuple[Mapping[str, Any], str, str] | None:
        """中文查询：互为子串 + 打分（代表值/熟食/短名优先，速溶/脱水/粉降权）。"""
        best: tuple[float, float, Mapping[str, Any]] | None = None
        for stripped, entry in self._contains_pool:
            if not stripped:
                continue
            if normalized not in stripped and stripped not in normalized:
                continue
            # [2026-11-07 第二阶段] contains 命中的核心字对不上 → 当查不到
            # （「鱼骨头汤」不再配到「无印良品 油菜和笋的和风汤」）。
            from life_logger.food_nutrition import contains_core_mismatch

            if contains_core_mismatch(normalized, stripped):
                continue
            # 覆盖度：查询越接近整名越好（「红烧牛肉」→「牛肉（代表值）」优于「牛舌」）
            short, long = sorted((len(normalized), len(stripped)))
            coverage = short / max(long, 1)
            score = _entry_score(str(entry.get("name") or ""), prefix=normalized)
            score += 0.4 * coverage
            if best is None or score > best[0]:
                best = (score, coverage, entry)
        if best is not None and best[0] >= _SCORE_MIN:
            # 长度差太远（查询只占名字的一小截）不许覆盖模型估值，只做参考
            strong = (
                best[0] >= _SCORE_MEDIUM
                and best[1] >= _MIN_CONTAINS_COVERAGE
                and not _NEGATIVE_RE.search(str(best[2].get("name") or ""))
            )
            confidence = CONFIDENCE_MEDIUM if strong else CONFIDENCE_LOW
            return best[2], MATCH_CONTAINS, confidence
        # 编辑距离兜底（中文错别字/少一个字：「鸡胸肉」↔「鸡胸脯肉」）
        if len(normalized) >= 3:
            fuzzy: tuple[float, Mapping[str, Any]] | None = None
            for stripped, entry in self._contains_pool:
                if not stripped or abs(len(stripped) - len(normalized)) > 3:
                    continue
                ratio = _similarity(normalized, stripped)
                if ratio >= _FUZZY_MIN_RATIO and (fuzzy is None or ratio > fuzzy[0]):
                    fuzzy = (ratio, entry)
            if fuzzy is not None:
                return fuzzy[1], MATCH_FUZZY, self.fuzzy_confidence
        return None

    # -- 对外 ------------------------------------------------------------------------
    def match(self, query: str) -> tuple[Mapping[str, Any], str, str] | None:
        """返回 ``(条目, 命中方式, 置信度)``；不命中返回 None。"""
        prefix = normalize_name(strip_brackets(query))
        for candidate in _candidates_for(query):
            exact = self._by_name.get(candidate)
            if exact:
                return self._pick(exact, query, prefix), MATCH_EXACT, self.exact_confidence
            stripped = self._by_stripped.get(candidate)
            if stripped:
                return self._pick(stripped, query, prefix), MATCH_EXACT, self.exact_confidence
        return self._scored(query)


def _load_json_entries(path: Path) -> list[Mapping[str, Any]]:
    """读编译好的本地库；文件不存在/坏了一律返回空表（静默降级）。"""
    try:
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if isinstance(payload, Mapping):
        entries = payload.get("entries")
        if isinstance(entries, list):
            return [item for item in entries if isinstance(item, Mapping)]
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    return []


# --------------------------------------------------------------------------------------
# 层①：自建品牌库（用户念过/拍过 + OFF 写回）
# --------------------------------------------------------------------------------------


def _coerce_brand_entry(item: Any) -> dict[str, Any] | None:
    """把 ``IngredientProfile``（food_nutrition 的 26 条）或 dict 统一成品牌库条目。"""
    if isinstance(item, Mapping):
        name = str(item.get("name") or "").strip()
        per_100g = _entry_per_100g(item)
        keywords = [str(k) for k in (item.get("keywords") or []) if str(k).strip()]
        if not _entry_has_energy({"per_100g": per_100g}):
            return None
        entry: dict[str, Any] = {
            "name": name,
            "per_100g": per_100g,
            "keywords": keywords,
            "evidence": str(item.get("evidence") or "generic_reference"),
            "origin": str(item.get("origin") or SOURCE_BRAND),
        }
        serving = item.get("serving_g") or item.get("unit_grams")
        if serving:
            entry["serving_g"] = serving
        label = item.get("serving_label") or item.get("unit_label")
        if label:
            entry["serving_label"] = str(label)
        # 在线补查的溯源：code 与最初查询词必须随条目保存，下次才知道它从哪来
        for extra in ("code", "query", "is_liquid"):
            if item.get(extra) not in (None, ""):
                entry[extra] = item[extra]
        return entry if name and per_100g else None
    name = str(getattr(item, "name", "") or "").strip()
    per_100g = getattr(item, "per_100g", None)
    if not name or not isinstance(per_100g, Mapping):
        return None
    keywords = [str(k) for k in (getattr(item, "keywords", ()) or ()) if str(k).strip()]
    per = _entry_per_100g({"per_100g": per_100g})
    if not _entry_has_energy({"per_100g": per}):
        return None
    label = getattr(item, "unit_label", None)
    return {
        "name": name,
        "per_100g": per,
        "keywords": keywords,
        "serving_g": getattr(item, "unit_grams", None),
        "serving_label": str(label) if label else None,
        "evidence": str(getattr(item, "evidence", "") or "generic_reference"),
        "origin": SOURCE_BRAND,
    }


class BrandFoodLibrary:
    """自建品牌库：用户念过/拍过的营养成分表 + OFF 查询结果写回。

    文件不存在时是**空的**（不报错）：种子条目由 ``food_nutrition.INGREDIENTS``
    以 ``seed_provider`` 的形式注入，运行期只往文件里追加新条目。
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        seed_provider: Callable[[], Iterable[Any]] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self._seed_provider = seed_provider
        self._lock = threading.RLock()
        self._entries: list[dict[str, Any]] | None = None

    # -- 读 -------------------------------------------------------------------------
    def _seed_entries(self) -> list[dict[str, Any]]:
        if self._seed_provider is None:
            return []
        try:
            raw = self._seed_provider()
        except Exception:  # pragma: no cover - 种子来源异常不该影响查询
            return []
        out: list[dict[str, Any]] = []
        for item in raw or ():
            entry = _coerce_brand_entry(item)
            if entry is not None:
                entry["seed"] = True
                out.append(entry)
        return out

    def entries(self) -> list[dict[str, Any]]:
        """用户条目（文件里的）在前，种子条目（INGREDIENTS）在后，同名去重。"""
        with self._lock:
            if self._entries is None:
                self._entries = self._read_file()
            return list(self._entries)

    def _read_file(self) -> list[dict[str, Any]]:
        if self.path is None:
            return []
        out: list[dict[str, Any]] = []
        for item in _load_json_entries(self.path):
            entry = _coerce_brand_entry(item)
            if entry is None:
                continue
            for extra in ("added_at", "note", "seed"):
                if extra in item:
                    entry[extra] = item[extra]
            out.append(entry)
        return out

    def all_entries(self) -> list[dict[str, Any]]:
        """文件条目 + 种子条目（查询用）。"""
        user = self.entries()
        seen = {normalize_name(str(entry.get("name") or "")) for entry in user}
        merged = list(user)
        for entry in self._seed_entries():
            key = normalize_name(str(entry.get("name") or ""))
            if key and key in seen:
                continue
            merged.append(entry)
        return merged

    # -- 写 -------------------------------------------------------------------------
    def remember(self, entry: Mapping[str, Any], *, note: str = "") -> dict[str, Any]:
        """记一条（写入本地 JSON）；同名条目按「后写的更新」覆盖数值字段。

        ``path=None`` 表示这个库从一开始就没有落盘位置（构造时没给路径）。
        此时**必须报错**：静默返回会让调用方以为「已记住、下次不再联网」，
        实际每次都要重新查询。
        """
        if self.path is None:
            raise ValueError("BrandFoodLibrary path is not configured; entry was not saved")
        coerced = _coerce_brand_entry(entry)
        if coerced is None:
            return {}
        coerced["added_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if note:
            coerced["note"] = note
        with self._lock:
            current = self._read_file()
            key = normalize_name(coerced["name"])
            replaced = False
            for index, existing in enumerate(current):
                if normalize_name(str(existing.get("name") or "")) == key:
                    merged = dict(existing)
                    merged.update({k: v for k, v in coerced.items() if v not in (None, "", (), [])})
                    # 关键词取并集：用户新念的别名不该把旧的冲掉
                    keywords = list(dict.fromkeys([*(existing.get("keywords") or []), *(coerced.get("keywords") or [])]))
                    merged["keywords"] = keywords
                    current[index] = merged
                    replaced = True
                    break
            if not replaced:
                current.append(coerced)
            self._entries = current
            self._write_file(current)
        return coerced

    def _write_file(self, entries: Sequence[Mapping[str, Any]]) -> None:
        """原子写：先写临时文件再 replace，避免 8765 长跑服务读到半截 JSON。"""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "source": SOURCE_BRAND,
                "version": 1,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "entries": list(entries),
            }
            from life_logger.atomic import write_json_atomic

            write_json_atomic(self.path, payload, indent=2)
        except OSError:
            return

    # -- 查 -------------------------------------------------------------------------
    def lookup(self, query: str) -> FoodNutrition | None:
        """精确/别名优先，弱命中（子串/模糊）兜底。"""
        return self.lookup_strict(query) or self.lookup_weak(query)

    def lookup_strict(self, query: str) -> FoodNutrition | None:
        """强命中：条目名相等 / 别名或关键词相等。

        这一段要**排在复合配方之前**（用户报过的整名最优先），而「子串关键词」
        （``菲诺的椰乳`` 里的 ``菲诺``）留给 ``lookup_weak`` —— 否则
        ``椰乳浓缩`` 这种复合词会被 ``椰乳`` 抢走，拆词永远轮不到。
        """
        entries = self.all_entries()
        if not entries:
            return None
        for candidate in _candidates_for(query):
            for entry in entries:
                if normalize_name(str(entry.get("name") or "")) == candidate:
                    return self._result(query, entry, MATCH_EXACT, CONFIDENCE_HIGH)
                for word in entry.get("keywords") or []:
                    key = normalize_name(word)
                    # [2026-11-06] 单字关键词（如「原味白奶酪」条目上的「面」「水」）
                    # 会把「面」「水」这种泛称直接认成该食物。weak 路径早就有 len<2 的守卫，
                    # strict 路径没有 —— 这里补齐，只影响关键词、不影响条目名精确匹配。
                    if len(key) < 2:
                        continue
                    if key == candidate:
                        return self._result(query, entry, MATCH_ALIAS, CONFIDENCE_HIGH)
        return None

    def lookup_weak(self, query: str) -> FoodNutrition | None:
        """弱命中：关键词是查询的子串（``菲诺的椰乳`` ← ``菲诺``），或包含/编辑距离。"""
        entries = self.all_entries()
        if not entries:
            return None
        raw_norm = normalize_name(query)
        if raw_norm in _RAW_ALIASES:
            return None
        normalized = normalize_name(strip_brackets(query))
        keyword_hits: list[tuple[int, dict[str, Any]]] = []
        for entry in entries:
            for word in [str(entry.get("name") or ""), *(entry.get("keywords") or [])]:
                key = normalize_name(word)
                if len(key) < 2:
                    continue
                if key == normalized:
                    return self._result(query, entry, MATCH_ALIAS, CONFIDENCE_HIGH)
                if key in normalized:
                    if not dish_guard_allows(query, str(entry.get("name") or "")):
                        continue
                    keyword_hits.append((len(key), entry))
        if keyword_hits:
            keyword_hits.sort(key=lambda item: -item[0])
            entry = keyword_hits[0][1]
            return self._result(query, entry, MATCH_ALIAS, CONFIDENCE_MEDIUM)
        index = LocalFoodIndex(
            SOURCE_BRAND,
            entries,
            exact_confidence=CONFIDENCE_HIGH,
            fuzzy_confidence=CONFIDENCE_LOW,
        )
        matched = index.match(query)
        if matched is not None:
            entry, kind, confidence = matched
            entry_norm = normalize_name(strip_brackets(str(entry.get("name") or "")))
            if kind == MATCH_CONTAINS and len(normalized) < len(entry_norm) and len(normalized) < 4:
                return None
            if dish_guard_allows(query, str(entry.get("name") or "")):
                return self._result(query, entry, kind, confidence)
        return None

    def _result(self, query: str, entry: Mapping[str, Any], kind: str, confidence: str) -> FoodNutrition | None:
        per_100g = _entry_per_100g(entry)
        if not per_100g:
            return None
        serving_g, serving_label = _entry_serving(entry)
        origin = str(entry.get("origin") or SOURCE_BRAND)
        return FoodNutrition(
            query=query,
            name=str(entry.get("name") or ""),
            matched_name=str(entry.get("name") or ""),
            per_100g=per_100g,
            source=origin,
            confidence=confidence,
            match_kind=kind,
            serving_g=serving_g,
            serving_label=serving_label,
            serving_source=SERVING_LIBRARY if serving_g else SERVING_NONE,
            evidence=str(entry.get("evidence") or ""),
            cached=origin == SOURCE_OFF,
            note=str(entry.get("note") or ""),
        )

    def resolve(self, name: str) -> FoodNutrition | None:
        """按**条目名**精确取一条（复合配方展开用，不做模糊）。"""
        key = normalize_name(name)
        for entry in self.all_entries():
            if normalize_name(str(entry.get("name") or "")) == key:
                return self._result(name, entry, MATCH_EXACT, CONFIDENCE_HIGH)
        return None


# --------------------------------------------------------------------------------------
# 层④：Open Food Facts（联网，查到写回层①）
# --------------------------------------------------------------------------------------


def _off_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _off_per_100g(product: Mapping[str, Any]) -> dict[str, float]:
    """从 OFF 产品里取每 100g 值。OFF 的 nutriments 字段名是 ``*_100g``。"""
    nutriments = product.get("nutriments")
    if not isinstance(nutriments, Mapping):
        return {}

    def pick(*keys: str) -> float | None:
        for key in keys:
            number = _off_number(nutriments.get(key))
            if number is not None:
                return number
        return None

    protein = pick("proteins_100g")
    fat = pick("fat_100g")
    carbs = pick("carbohydrates_100g")
    sodium = pick("sodium_100g")
    sodium_mg = pick("sodium_100g_mg")
    if sodium is not None and sodium_mg is None:
        sodium_mg = sodium * 1000.0  # OFF 的 sodium_100g 单位是克
    # 盐 → 钠（400mg 钠 ≈ 1g 盐），只在没有钠字段时换算
    if sodium_mg is None:
        salt = pick("salt_100g")
        if salt is not None:
            sodium_mg = salt * 400.0
    energy = pick("energy-kcal_100g", "energy-kcal")
    if energy is None:
        energy_kj = pick("energy-kj_100g", "energy_100g", "energy-kj")
        if energy_kj is not None:
            energy = energy_kj / _KJ_PER_KCAL
    values = {
        "energy_kcal": energy,
        "protein_g": protein,
        "fat_g": fat,
        "carbohydrates_g": carbs,
        "sodium_mg": sodium_mg,
    }
    if values["energy_kcal"] is None:
        return {}
    return {key: round(float(value), 2) for key, value in values.items() if value is not None}


def fetch_openfoodfacts(name: str, *, opener: Callable[..., Any] | None = None) -> dict[str, Any] | None:
    """联网查 Open Food Facts。失败（无网/超时/字段怪）一律返回 None，不打日志、不抛错。

    ``opener`` 只为测试注入（签名同 ``urllib.request.urlopen``）。
    """
    query = str(name or "").strip()
    if not query:
        return None
    params = {
        "search_terms": query,
        "search_simple": "1",
        "action": "process",
        "json": "1",
        "page_size": str(_OFF_MAX_CANDIDATES),
        "fields": "code,product_name,product_name_zh,brands,nutriments,serving_quantity,countries_tags",
    }
    url = f"{_OFF_ENDPOINT}?{urllib.parse.urlencode(params)}"
    # [实测坑] OFF 对**没有 Accept 头**的请求一律回 503「Service Temporarily Unavailable」
    # （urllib 默认不发 Accept；同样的 URL 用 curl 或用带 Accept 的 urllib 都是 200）。
    # 显式带上 Accept + 描述性 UA，否则层④ 会一直静默跳过、看起来像「没网」。
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _OFF_USER_AGENT, "Accept": "application/json"},
    )
    open_call = opener or urllib.request.urlopen
    try:
        with open_call(request, timeout=_OFF_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    products = payload.get("products") if isinstance(payload, Mapping) else None
    if not isinstance(products, list):
        return None
    for product in products:
        if not isinstance(product, Mapping):
            continue
        per_100g = _off_per_100g(product)
        if not per_100g:
            continue
        product_name = str(product.get("product_name_zh") or product.get("product_name") or "").strip()
        if not product_name:
            continue
        serving = _off_number(product.get("serving_quantity"))
        entry: dict[str, Any] = {
            "name": product_name,
            "per_100g": per_100g,
            "keywords": [query] if normalize_name(query) != normalize_name(product_name) else [],
            "evidence": "open_food_facts",
            "origin": SOURCE_OFF,
        }
        if serving and serving > 0:
            entry["serving_g"] = round(serving, 1)
            entry["serving_label"] = "份"
        code = str(product.get("code") or "").strip()
        if code:
            entry["note"] = f"Open Food Facts barcode={code}"
        return entry
    return None




# --------------------------------------------------------------------------------------
# 薄荷多候选挑选（用户 2026-11-06：不再让人判断）
# --------------------------------------------------------------------------------------


def _search_energy(item: Mapping[str, Any]) -> float | None:
    """搜索结果的 calory 是「每 weight 克」，统一折算到每 100g。"""
    calory = item.get("calory")
    try:
        value = float(calory) if calory is not None else None
    except (TypeError, ValueError):
        return None
    if value is None or value <= 0:
        return None
    try:
        weight = float(item.get("weight") or 100)
    except (TypeError, ValueError):
        weight = 100.0
    if weight <= 0:
        return value
    return value * 100.0 / weight


def _exact_name_match(query: str, name: str) -> bool:
    return normalize_name(strip_brackets(query)) == normalize_name(strip_brackets(name))


def _candidate_key(query: str, item: Mapping[str, Any]) -> tuple[bool, bool, bool, float]:
    """排序键：菜品族 → 品牌+菜名 → 包含 → 字符相似度。"""
    name = str(item.get("name") or "")
    q = normalize_name(query)
    n = normalize_name(name)
    raw = str(query or "").strip().split()
    brand = normalize_name(raw[0]) if len(raw) > 1 else ""
    dish = normalize_name("".join(raw[1:])) if len(raw) > 1 else ""
    brand_and_dish = bool(brand and dish and brand in n and dish in n)
    contained = bool(q and n and (q in n or n in q))
    return (dish_guard_allows(query, name), brand_and_dish, contained, _similarity(q, n))


def choose_boohee_candidate(query: str, candidates: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], str]:
    """名称最近优先；分不出时取热量中位那条，并标出依据。"""
    ranked = sorted(candidates, key=lambda item: _candidate_key(query, item), reverse=True)
    best_key = _candidate_key(query, ranked[0])
    tied = [item for item in ranked if _candidate_key(query, item) == best_key]
    if len(tied) > 1:
        ordered = sorted(tied, key=lambda item: _search_energy(item) or 0.0)
        return ordered[len(ordered) // 2], "median_of_candidates"
    return ranked[0], "nearest_match"


# --------------------------------------------------------------------------------------
# 查询主体
# --------------------------------------------------------------------------------------


class FoodLookup:
    """四层查询链本体。构造时可注入数据目录、品牌库与 OFF 抓取函数（便于测试）。"""

    def __init__(
        self,
        data_dir: Path | str | None = None,
        *,
        brand_library: BrandFoodLibrary | None = None,
        seed_provider: Callable[[], Iterable[Any]] | None = None,
        off_fetcher: Callable[[str], dict[str, Any] | None] | None = None,
        off_enabled: bool | None = None,
        boohee_client: Any = None,
        boohee_enabled: bool | None = None,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self._lock = threading.RLock()
        self._brand = brand_library or BrandFoodLibrary(
            self.data_dir / "brand-library.json", seed_provider=seed_provider
        )
        explicit_fetcher = off_fetcher is not None
        self._off_fetcher = off_fetcher or fetch_openfoodfacts
        if off_enabled is None:
            setting = str(os.environ.get(_ENV_OFF_DISABLE, "")).strip().lower()
            off_enabled = setting not in {"0", "false", "no", "off"}
            # [2026-11-06] 跑测试时默认**不联网、不写回品牌库**：审计/单测里随手一次
            # lookup_food_nutrition('虾') 就会把无关 OFF 商品写进 output/local-web/food-data。
            # 需要测 OFF 的用例请显式传 off_fetcher/off_enabled 或设 LL_FOOD_OFF=1。
            # 注入过 fetcher 的用例（离线夹具）不受影响：它们本来就不联网。
            if not explicit_fetcher and os.environ.get("PYTEST_CURRENT_TEST") and str(
                os.environ.get(_ENV_OFF_FORCE, "")
            ).strip().lower() not in {"1", "true", "yes", "on"}:
                off_enabled = False
        self.off_enabled = bool(off_enabled)
        # 薄荷是**最后一层**在线补查（在 OFF 之后）。同样默认受 pytest 保护：
        # 单测里不显式注入 client 就不会联网。
        if boohee_client is None:
            from life_logger.boohee import BooheeClient

            boohee_client = BooheeClient(self.data_dir)
        explicit_boohee = boohee_enabled is not None
        if boohee_enabled is None:
            disabled = str(os.environ.get(_ENV_BOOHEE_DISABLE, "")).strip().lower() in {"0", "false", "no", "off"}
            boohee_enabled = not disabled
            if (not explicit_boohee and os.environ.get("PYTEST_CURRENT_TEST")
                    and str(os.environ.get(_ENV_BOOHEE_FORCE, "")).strip().lower() not in {"1", "true", "yes", "on"}):
                boohee_enabled = False
        self._boohee = boohee_client
        self._online_miss_lock = threading.Lock()
        self.boohee_enabled = bool(boohee_enabled)
        self._cfct: LocalFoodIndex | None = None
        self._foodwake: LocalFoodIndex | None = None
        self._usda: LocalFoodIndex | None = None

    # -- 懒加载（读不动就当这层不存在） -----------------------------------------------
    @property
    def brand(self) -> BrandFoodLibrary:
        return self._brand

    @property
    def cfct(self) -> LocalFoodIndex:
        with self._lock:
            if self._cfct is None:
                self._cfct = LocalFoodIndex(
                    SOURCE_CFCT,
                    _load_json_entries(self.data_dir / "cfct.json"),
                    exact_confidence=CONFIDENCE_HIGH,
                    fuzzy_confidence=CONFIDENCE_LOW,
                )
            return self._cfct

    @property
    def foodwake(self) -> LocalFoodIndex:
        with self._lock:
            if self._foodwake is None:
                self._foodwake = LocalFoodIndex(
                    SOURCE_FOODWAKE,
                    _load_json_entries(self.data_dir / "foodwake.json"),
                    # foodwake 是第三方整理的数据，整表比官方成分表低一档
                    exact_confidence=CONFIDENCE_MEDIUM,
                    fuzzy_confidence=CONFIDENCE_LOW,
                )
            return self._foodwake

    @property
    def usda(self) -> LocalFoodIndex:
        with self._lock:
            if self._usda is None:
                self._usda = LocalFoodIndex(
                    SOURCE_USDA,
                    _load_json_entries(self.data_dir / "usda-subset.json"),
                    exact_confidence=CONFIDENCE_MEDIUM,
                    fuzzy_confidence=CONFIDENCE_LOW,
                )
            return self._usda

    # -- 主流程 ---------------------------------------------------------------------
    def lookup(self, name: str, grams: float | None = None, *, allow_off: bool = True) -> FoodNutrition | None:
        """按名字查：层①品牌 → 复合配方 → 层②成分表/foodwake → 层③USDA → 层④OFF。

        ``grams`` 只影响返回对象里 ``effective_serving_g`` 的语义（本次摄入克重），
        每 100g 值的换算由调用方用 ``scale()`` 完成。
        """
        query = str(name or "").strip()
        if not query:
            return None
        # ① 品牌库强命中（用户念过/拍过的条目名、别名）
        result = self._safe(lambda: self._brand.lookup_strict(query))
        if result is not None:
            return self._finalize(result)
        # ①b 复合配方 / 拆词（由层① 的条目组合而成，如「生椰拿铁」）
        result = self._safe(lambda: self._composite(query))
        if result is not None:
            return self._finalize(result)
        # ①c 品牌库弱命中（子串关键词：「菲诺的椰乳」里的「菲诺」）
        result = self._safe(lambda: self._brand.lookup_weak(query))
        if result is not None:
            return self._finalize(result)
        # ② 中国食物成分表 → foodwake
        for index in (self.cfct, self.foodwake):
            result = self._safe(lambda index=index: self._from_index(query, index))
            if result is not None:
                return self._finalize(result)
        # ③ USDA 精选
        result = self._safe(lambda: self._from_usda(query))
        if result is not None:
            return self._finalize(result)
        # [2026-11-07] 联网层查不到的名字记下来，7 天内不再联网：/api/daily 每次读都会走到这里，
        # 不记的话同一个查不到的名字每次读页面都联网两次（实测 11-06 页面因此超过 4 秒读超时）。
        online_wanted = allow_off and (self.off_enabled or (self.boohee_enabled and self._boohee is not None))
        if online_wanted and self._online_miss_recent(query):
            return None
        # ④ Open Food Facts（联网；查到写回层①）
        if allow_off and self.off_enabled:
            result = self._safe(lambda: self._from_off(query))
            if result is not None:
                return self._finalize(result)
        # ⑤ 薄荷健康（最后一道在线补查；每日硬上限在 client 内）
        if allow_off and self.boohee_enabled and self._boohee is not None:
            result = self._safe(lambda: self._from_boohee(query))
            if result is not None:
                return self._finalize(result)
        if online_wanted:
            self._remember_online_miss(query)
        return None

    def _online_miss_path(self) -> Path:
        return self.data_dir / "online-misses.json"

    def _online_miss_recent(self, query: str) -> bool:
        key = normalize_name(query)
        if not key:
            return False
        try:
            misses = json.loads(self._online_miss_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        stamp = misses.get(key) if isinstance(misses, dict) else None
        return isinstance(stamp, (int, float)) and time.time() - float(stamp) < ONLINE_MISS_TTL_SECONDS

    def _remember_online_miss(self, query: str) -> None:
        key = normalize_name(query)
        if not key:
            return
        path = self._online_miss_path()
        with self._online_miss_lock:
            try:
                misses = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(misses, dict):
                    misses = {}
            except (OSError, ValueError):
                misses = {}
            misses[key] = time.time()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
                tmp.write_text(json.dumps(misses, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                pass

    def _finalize(self, result: FoodNutrition) -> FoodNutrition:
        """补齐份量：库里没自带份量时用常见份量经验表，并把「份量从哪来」写清楚。

        没报克重时，光有「每 100g」算不出热量 —— 份量必须显式标注来源，
        否则前端看到的数字就成了新的黑箱。
        """
        if result.serving_g and result.serving_g > 0:
            return result
        hint = _serving_hint(result.matched_name or result.name)
        if hint is None:
            return result
        grams, label = hint
        from dataclasses import replace

        return replace(result, serving_g=grams, serving_label=label, serving_source=SERVING_HINT)


    def _safe(self, call: Callable[[], FoodNutrition | None]) -> FoodNutrition | None:
        """任何一层出错都当作「没命中」——查询不能成为新的失败点。"""
        try:
            return call()
        except Exception:
            return None

    def _from_index(self, query: str, index: LocalFoodIndex) -> FoodNutrition | None:
        matched = index.match(query)
        if matched is None:
            return None
        entry, kind, confidence = matched
        # [2026-11-06] 复合菜名不许退化成单一原料：猪肉芹菜饺子 ≠ 猪肉（清蒸）。
        if not dish_guard_allows(query, str(entry.get("name") or "")):
            return None
        per_100g = _entry_per_100g(entry)
        if not per_100g:
            return None
        serving_g, serving_label = _entry_serving(entry)
        return FoodNutrition(
            query=query,
            name=str(entry.get("name") or ""),
            matched_name=str(entry.get("name") or ""),
            per_100g=per_100g,
            source=index.source,
            confidence=confidence,
            match_kind=kind,
            serving_g=serving_g,
            serving_label=serving_label,
            serving_source=SERVING_LIBRARY if serving_g else SERVING_NONE,
            evidence=str(entry.get("source") or index.source),
        )

    def _from_usda(self, query: str) -> FoodNutrition | None:
        matched = self.usda.match(query)
        if matched is None:
            return None
        entry, kind, confidence = matched
        if kind == MATCH_FUZZY:
            # USDA 是英文长名（"Chicken, broilers or fryers, ..."），编辑距离容易误伤；
            # 模糊命中一律降级到 low，交给上游决定要不要覆盖模型估值。
            confidence = CONFIDENCE_LOW
        per_100g = _entry_per_100g(entry)
        if not per_100g:
            return None
        serving_g, serving_label = _entry_serving(entry)
        return FoodNutrition(
            query=query,
            name=str(entry.get("name") or ""),
            matched_name=str(entry.get("name") or ""),
            per_100g=per_100g,
            source=SOURCE_USDA,
            confidence=confidence,
            match_kind=kind,
            serving_g=serving_g,
            serving_label=serving_label,
            serving_source=SERVING_LIBRARY if serving_g else SERVING_NONE,
            evidence=SOURCE_USDA,
        )

    def _composite(self, query: str) -> FoodNutrition | None:
        """复合配方：把「生椰拿铁」拆成层①里的配料，按默认克重组合。"""
        normalized = normalize_name(query)
        if not normalized:
            return None
        for recipe_name, components in COMPOSITE_RECIPES.items():
            if normalize_name(recipe_name) != normalized:
                continue
            return self._build_composite(query, recipe_name, components)
        # 没写死的复合词，尝试用层①词表做最长匹配切分（「椰乳+浓缩」这类）
        return self._decompose(query)

    def _build_composite(
        self, query: str, label: str, components: Sequence[tuple[str, float]]
    ) -> FoodNutrition | None:
        resolved: list[tuple[FoodNutrition, float]] = []
        for item_name, grams in components:
            found = self._brand.resolve(item_name)
            if found is None:
                return None
            resolved.append((found, float(grams)))
        total_grams = sum(grams for _found, grams in resolved)
        if total_grams <= 0:
            return None
        per_100g: dict[str, float] = {}
        for found, grams in resolved:
            for key, value in found.per_100g.items():
                per_100g[key] = per_100g.get(key, 0.0) + float(value) * grams / total_grams
        return FoodNutrition(
            query=query,
            name=label,
            matched_name="+".join(found.name for found, _grams in resolved),
            per_100g={key: round(value, 2) for key, value in per_100g.items()},
            source=SOURCE_BRAND,
            confidence=CONFIDENCE_MEDIUM,
            match_kind=MATCH_RECIPE,
            serving_g=round(total_grams, 1),
            serving_label="份",
            serving_source=SERVING_RECIPE,
            evidence="user_verified",
            recipe=tuple((found.name, grams) for found, grams in resolved),
            note="复合配方（由自建品牌库条目按默认克重组合）",
        )

    def _decompose(self, query: str) -> FoodNutrition | None:
        """兜底拆词：用层①词表 + 复合配方名对查询做最长匹配切分。

        全切中且至少两段才认（「椰乳浓缩」→ 椰乳 + 意式浓缩）。切不干净就返回 None，
        绝不把「随便一个长词」拆成配料组合。
        """
        normalized = normalize_name(query)
        if len(normalized) < 4:
            return None
        # 词表：层①条目名 + 别名/关键词（→ 用该条目的默认份量），以及复合配方名（→ 展开成配料）
        vocabulary: list[tuple[str, str, tuple[tuple[str, float], ...] | None]] = []
        for entry in self._brand.all_entries():
            name = str(entry.get("name") or "")
            for word in [name, *(entry.get("keywords") or [])]:
                key = normalize_name(word)
                if len(key) >= 2:
                    vocabulary.append((key, name, None))
        for recipe_name, components in COMPOSITE_RECIPES.items():
            key = normalize_name(recipe_name)
            if len(key) >= 2:
                vocabulary.append((key, recipe_name, tuple(components)))
        vocabulary.sort(key=lambda item: -len(item[0]))
        position = 0
        parts: list[tuple[str, str, tuple[tuple[str, float], ...] | None]] = []
        while position < len(normalized):
            hit = next((item for item in vocabulary if normalized.startswith(item[0], position)), None)
            if hit is None:
                return None
            parts.append(hit)
            position += len(hit[0])
        components: list[tuple[str, float]] = []
        for _key, label, recipe in parts:
            if recipe is not None:
                components.extend(list(recipe))
                continue
            found = self._brand.resolve(label)
            serving = found.serving_g if found is not None else None
            components.append((label, float(serving or DEFAULT_SERVING_G)))
        merged: dict[str, float] = {}
        for item_name, grams in components:
            merged[item_name] = merged.get(item_name, 0.0) + float(grams)
        if len(merged) < 2:
            return None
        return self._build_composite(query, query.strip(), list(merged.items()))


    def _from_boohee(self, query: str) -> FoodNutrition | None:
        """最后一道在线补查：多候选自动挑选，命中写回品牌库。

        用户录入/本地库在前面已经命中，不会走到这里。挑不出来就返回 None，
        由调用方保持「未知」；不把候选推给用户判断。
        """
        if len(normalize_name(query)) < 2:
            return None
        candidates = self._boohee.search(query)
        usable = [c for c in candidates
                  if isinstance(c, Mapping) and c.get("name") and _search_energy(c) is not None
                  and dish_guard_allows(query, str(c["name"]))]
        if not usable:
            return None
        item, basis = choose_boohee_candidate(query, usable)
        detail = self._boohee.detail(str(item.get("code") or "")) or {}
        per_100g = dict(detail.get("per_100g") or {})
        if per_100g.get("energy_kcal") is None:
            energy = _search_energy(item)
            if energy is None:
                return None
            per_100g["energy_kcal"] = round(energy, 2)
        name = str(item.get("name") or "")
        # 自动挑选的两条路径都可以进入下游（用户已决定不再人工判断）；
        # 但「中位」的依据更弱，用 estimate_confidence 单独记下来。
        confidence = CONFIDENCE_MEDIUM
        basis_confidence = CONFIDENCE_MEDIUM if basis == "nearest_match" and _exact_name_match(query, name) else CONFIDENCE_LOW
        entry = {
            "name": name,
            "per_100g": per_100g,
            "keywords": list(dict.fromkeys([query, name])),
            "origin": SOURCE_BOOHEE,
            "evidence": "boohee",
            "code": str(item.get("code") or ""),
            "query": query,
            "is_liquid": bool(item.get("is_liquid")),
            # 搜索结果的 weight 是「每份参考克重」，不是一顿的份量：不能当 serving_g，
            # 否则「吃了一碗水饺」会被算成 100g。份量仍由经验表/原话给出。
            "note": f"薄荷健康：{name}（{basis}，候选{len(usable)}条）",
        }
        self._brand.remember(entry, note=entry["note"])
        return FoodNutrition(
            query=query,
            name=name,
            matched_name=name,
            per_100g=per_100g,
            source=SOURCE_BOOHEE,
            confidence=confidence,
            match_kind=MATCH_FUZZY if basis == "median_of_candidates" else MATCH_CONTAINS,
            serving_g=None,
            serving_label=None,
            serving_source=SERVING_NONE,
            evidence="boohee",
            note=entry["note"],
            estimate_basis=basis,
            estimate_confidence=basis_confidence,
        )

    def _from_off(self, query: str) -> FoodNutrition | None:
        # [2026-11-06] 单字查询（归一化后 <2）一律不联网：OFF 的模糊搜索会把
        # 「虾」「鱼」「粥」「粉」匹配成奶酪、坚果之类的无关商品，还会写回品牌库。
        if len(normalize_name(query)) < 2:
            return None
        fetched = self._off_fetcher(query)
        if not isinstance(fetched, Mapping):
            return None
        entry = _coerce_brand_entry(fetched)
        if entry is None:
            return None
        # 商品名必须和查询词相关：要么商品名包含查询核心，要么查询包含商品核心。
        if not _off_name_matches(query, entry.get("name")):
            return None
        # 写回前清掉单字关键词：单字关键词（面/水/粉/鱼/羹/虾/粥）会把任何同名查询
        # 拽到这条商品上，是 11-06 那次「虾→原味白奶酪」的直接原因。
        entry["keywords"] = [k for k in (entry.get("keywords") or [])
                             if len(normalize_name(str(k))) >= 2]
        # 查到就写回层① —— 这就是「后台记忆」：下次同样的话不再联网
        self._brand.remember(entry, note=str(entry.get("note") or ""))
        per_100g = entry["per_100g"]
        serving_g = entry.get("serving_g")
        return FoodNutrition(
            query=query,
            name=entry["name"],
            matched_name=entry["name"],
            per_100g=per_100g,
            source=SOURCE_OFF,
            confidence=CONFIDENCE_MEDIUM,
            match_kind=MATCH_EXACT,
            serving_g=float(serving_g) if serving_g else None,
            serving_label=entry.get("serving_label"),
            serving_source=SERVING_LIBRARY if serving_g else SERVING_NONE,
            evidence="open_food_facts",
            cached=False,
            note=str(entry.get("note") or ""),
        )

    # -- 汇报入口（「念一次存一次」） -------------------------------------------------
    def remember_user_entry(
        self,
        name: str,
        per_100g: Mapping[str, Any],
        *,
        serving_g: float | None = None,
        serving_label: str | None = None,
        keywords: Sequence[str] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """用户报一条营养成分表 → 写进层①。"""
        entry: dict[str, Any] = {
            "name": str(name or "").strip(),
            "per_100g": {key: value for key, value in dict(per_100g).items() if value is not None},
            "evidence": "user_reported",
            "origin": SOURCE_BRAND,
        }
        if serving_g:
            entry["serving_g"] = float(serving_g)
        if serving_label:
            entry["serving_label"] = str(serving_label)
        if keywords:
            entry["keywords"] = [str(item) for item in keywords if str(item).strip()]
        return self._brand.remember(entry, note=note)


def default_data_dir() -> Path:
    """本地库目录：环境变量优先，默认 ``<repo>/output/local-web/food-data``。"""
    override = str(os.environ.get(_ENV_DATA_DIR) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "output" / "local-web" / "food-data"


# --------------------------------------------------------------------------------------
# 「念一次存一次」：把口语里的营养成分表解析成品牌库条目
# --------------------------------------------------------------------------------------

# 营养字段 → 口语里的说法与单位。数值单位必须显式出现（「钠82毫克」），
# 没有单位就按该字段的默认单位（能量千卡 / 三大营养素克 / 钠毫克）。
# [2026-11-08] 兼容 MOSS ASR 转写的中文数字与儿化小数（「四百二十九千焦」「十八点儿六克」「七点八克」）。
_LABEL_CN_DIGITS: Mapping[str, int] = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2,
    "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_LABEL_CN_UNITS: Mapping[str, int] = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_LABEL_NUM_PATTERN = (
    r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万壹贰]+(?:点儿?[零〇一二三四五六七八九\d]+)?)"
)
_LABEL_GAP = r"[^\d零〇一二两三四五六七八九十百千万壹贰]{0,8}"


def _parse_label_number(raw: str) -> float | None:
    """解析口语营养标签里的数字（阿拉伯数字或中文数字，支持「十八点儿六」「四百二十九」）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    if "点" in text:
        parts = re.split(r"点儿?", text, maxsplit=1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        int_val = 0.0 if parts[0] in ("零", "〇", "0") else _parse_label_number(parts[0])
        if int_val is None:
            return None
        frac_digits: list[str] = []
        for ch in parts[1]:
            if ch in _LABEL_CN_DIGITS:
                frac_digits.append(str(_LABEL_CN_DIGITS[ch]))
            elif ch.isdigit():
                frac_digits.append(ch)
            else:
                return None
        return float(f"{int(int_val)}.{''.join(frac_digits)}") if frac_digits else None
    total = 0
    section = 0
    pending: int | None = None
    seen = False
    for ch in text:
        if ch in _LABEL_CN_DIGITS:
            pending = _LABEL_CN_DIGITS[ch]
            seen = True
        elif ch in _LABEL_CN_UNITS:
            unit = _LABEL_CN_UNITS[ch]
            section += (1 if pending is None else pending) * unit
            pending = None
            seen = True
        else:
            return None
    if not seen:
        return None
    if pending is not None:
        section += pending
    total += section
    return float(total)


_LABEL_FIELD_PATTERNS: Mapping[str, tuple[str, str, str]] = {
    # 字段: (正则, 默认单位, 单位换算 → 目标单位)
    "energy_kcal": (
        rf"(?:(?:能量|热量|卡路里|calories){_LABEL_GAP})?({_LABEL_NUM_PATTERN})\s*(千卡|大卡|卡路里|kcal|千焦|kj|KJ|kJ)",
        "kcal",
        "energy",
    ),
    "protein_g": (rf"蛋白质{_LABEL_GAP}({_LABEL_NUM_PATTERN})\s*(克|g|毫克|mg)?", "g", "mass_g"),
    "fat_g": (
        rf"(?<!饱和)(?<!反式)(?<!不饱和)脂肪{_LABEL_GAP}({_LABEL_NUM_PATTERN})\s*(克|g|毫克|mg)?",
        "g",
        "mass_g",
    ),
    "carbohydrates_g": (
        rf"(?:碳水化合物|碳水|糖类|醣类){_LABEL_GAP}({_LABEL_NUM_PATTERN})\s*(克|g|毫克|mg)?",
        "g",
        "mass_g",
    ),
    "sodium_mg": (rf"钠{_LABEL_GAP}({_LABEL_NUM_PATTERN})\s*(毫克|mg|克|g)?", "mg", "sodium"),
    # [2026-11-06] 糖单列：口语里「糖 5.6 克」是标签上的 Sugar 行，不能并进碳水
    #（碳水本身通常已含糖，合并会把能量算重）。「糖类/醣类」仍归碳水。
    "sugar_g": (
        rf"(?:糖(?!类)|含糖)[^\d零〇一二两三四五六七八九十百千万壹贰]{{0,4}}({_LABEL_NUM_PATTERN})\s*(克|g)?",
        "g",
        "mass_g",
    ),
}

_LABEL_PER_100G_RE = re.compile(r"每\s*(?:100|一百)\s*(?:克|g|毫升|ml)")
_LABEL_SERVING_RE = re.compile(
    rf"(?:一份|每份|一杯|一杯是|一包|每包|整包|整袋)[^\d零〇一二两三四五六七八九十百千万壹贰，,。；;\n]{{0,10}}({_LABEL_NUM_PATTERN})\s*(克|g|毫升|ml)"
)


def _convert_label_value(field_name: str, raw: str, unit: str, default_unit: str) -> float | None:
    """把「82毫克」「94千卡」「392千焦」「四百二十九千焦」「十八点儿六克」换算成目标单位。"""
    value = _parse_label_number(raw)
    if value is None:
        return None
    unit = (unit or default_unit).lower()
    kind = _LABEL_FIELD_PATTERNS[field_name][2]
    if kind == "energy":
        if unit in ("千焦", "kj"):
            return value / _KJ_PER_KCAL
        return value  # 千卡 / 大卡 / 卡路里
    if kind == "mass_g":
        if unit in ("毫克", "mg"):
            return value / 1000.0
        return value
    # sodium → 毫克
    if unit in ("克", "g"):
        return value * 1000.0
    return value


def parse_nutrition_label_text(text: str) -> dict[str, Any] | None:
    """解析口语营养成分表：``菲诺厚椰乳 每100克 94千卡 蛋白质1克 脂肪7克 碳水6.8克 钠82毫克``。

    返回 ``{"item": 名称, "per_100g": {...}, "serving_g":…}``；**一个数值都解析不出来**
    时返回 None（宁可让上游落进待确认区，也不往里写空条目）。
    解析的口径固定为「每 100 克」—— 口语绝大多数报的就是包装上的每 100g 列。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    field_text = re.sub(r"每\s*(?:100|一百)\s*(?:克|g|毫升|ml)", "，", raw)
    per_100g: dict[str, float] = {}
    for field_name, (pattern, default_unit, _kind) in _LABEL_FIELD_PATTERNS.items():
        match = re.search(pattern, field_text)
        if match is None:
            continue
        value = _convert_label_value(field_name, match.group(1), match.group(2) or "", default_unit)
        if value is not None:
            per_100g[field_name] = round(value, 2)
    if "energy_kcal" not in per_100g:
        return None
    # 名称 = 触发词之后、第一个营养关键词之前的那些字
    cut = len(raw)
    for pattern, _unit, _kind in _LABEL_FIELD_PATTERNS.values():
        found = re.search(pattern, raw)
        if found and found.start() < cut:
            cut = found.start()
    name = raw[:cut]
    name = _LABEL_PER_100G_RE.sub("", name)
    name = re.sub(r"每\s*100\s*(?:克|g|毫升|ml)", "", name)
    name = _LABEL_SERVING_RE.sub("", name)
    name = name.strip(" ，,。.、:：的+＋")
    if not name:
        return None
    payload: dict[str, Any] = {
        "item": name,
        "per_100g": per_100g,
        "per": "100g" if _LABEL_PER_100G_RE.search(raw) else "100g(推定)",
        "raw_text": raw,
    }
    serving = _LABEL_SERVING_RE.search(raw)
    if serving is not None:
        grams = _parse_label_number(serving.group(1)) or 0.0
        if serving.group(2) in ("毫升", "ml"):
            grams = grams  # 液体按 1g/ml 处理（与 food_nutrition 的口径一致）
        if grams > 0:
            payload["serving_g"] = round(grams, 1)
            payload["serving_label"] = "份"
    return payload


# --------------------------------------------------------------------------------------
# [2026-11-06] 连续念表（无口令词）+ 拿不准进待确认 + 「查一下营养库」
# --------------------------------------------------------------------------------------

#: 句首的口语填充词，清名字时要剥掉
_LABEL_NAME_FILLERS = ("首先是", "这个是", "那个是", "然后", "还有", "这个", "那个", "呃", "嗯", "哦",
                       "我记录一下", "记录一下", "记一下", "我记一下", "记录营养成分", "记录营养")
#: 名字尾部「…的营养成分（表）」这类后缀
_LABEL_NAME_TAIL_RE = re.compile(r"(?:的)?\s*营养成分(?:表)?\s*$|(?:的)?\s*营养表\s*$|(?:的)?\s*热量\s*$")
_LABEL_PER_SERVING_RE = re.compile(
    rf"(?:每\s*份|一份|每\s*包|一包|每\s*袋|每\s*瓶|每\s*盒)[^\d零〇一二两三四五六七八九十百千万壹贰，,。；;\n]{{0,10}}({_LABEL_NUM_PATTERN})?\s*(克|g|毫升|ml)?"
)
_SPOKEN_LABEL_MIN_FIELDS = 3


def clean_label_item_name(raw: str) -> str:
    """从「首先是面的营养成分」里取出「面」。取不出就返回空串（调用方转待确认）。"""
    name = str(raw or "").strip()
    # 口语里「首先是面的营养成分，呃，是每一百克…」——名字只在前半句里，先按标点切
    name = re.split(r"[，,。；;：:]", name)[0]
    name = _LABEL_NAME_TAIL_RE.sub("", name)
    name = _LABEL_PER_100G_RE.sub("", name)
    name = _LABEL_PER_SERVING_RE.sub("", name)
    name = re.sub(r"每\s*(?:100|一百)\s*(?:克|g|毫升|ml)", "", name)
    changed = True
    while changed:
        changed = False
        for filler in _LABEL_NAME_FILLERS:
            if name.startswith(filler):
                name = name[len(filler):]
                changed = True
    name = name.strip(" ，,。.、:：的+＋…").strip()
    # 只剩标点或只有一个「的」之类的碎片，视为没名字
    if len(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", name)) < 1:
        return ""
    return name[:40]


def parse_spoken_label(text: str, *, fallback_item: str = "") -> dict[str, Any]:
    """解析一次「念营养成分表」。返回结构里一定带 ``status``：

    * ``ok``           —— 可以写入品牌库（调用方再决定是否写）；
    * ``needs_review`` —— 拿不准，转「需要我判断」，``reason`` 说明为什么。

    口径：
      * 千焦一律换算成千卡（1 kcal = 4.184 kJ），由 ``_convert_label_value`` 完成；
      * 「每 100 克」→ ``basis=per_100g``；「每份 + 净含量」→ 换算到每 100 克并记 ``serving_g``；
      * 「每份」但没说净含量 → ``needs_review``（不猜份量）；
      * 糖单列 ``sugar_g``，**不并入** ``carbohydrates_g``。
    """
    raw = str(text or "").strip()
    if not raw:
        return {"status": "needs_review", "reason": "empty_text"}

    # 「糖是每100克2.5克」这类说法：先把「每100克」从数值流里摘掉，否则正则会把 100 当成糖量
    field_text = re.sub(r"每\s*(?:100|一百)\s*(?:克|g|毫升|ml)", "，", raw)

    per_100g: dict[str, float] = {}
    for field_name, (pattern, default_unit, _kind) in _LABEL_FIELD_PATTERNS.items():
        match = re.search(pattern, field_text)
        if match is None:
            continue
        value = _convert_label_value(field_name, match.group(1), match.group(2) or "", default_unit)
        if value is not None:
            per_100g[field_name] = round(value, 2)

    if not per_100g:
        return {"status": "needs_review", "reason": "no_nutrition_values", "raw_text": raw}

    # 名字：第一个营养词之前的整段
    cut = len(raw)
    for pattern, _unit, _kind in _LABEL_FIELD_PATTERNS.values():
        found = re.search(pattern, raw)
        if found and found.start() < cut:
            cut = found.start()
    item = clean_label_item_name(raw[:cut]) or str(fallback_item or "").strip()

    per_100g_re = _LABEL_PER_100G_RE.search(raw)
    serving = _LABEL_PER_SERVING_RE.search(raw)
    serving_g: float | None = None
    # 必须「数字 + 单位」才算净含量：「每份能量120千卡」里的 120 是能量，不是克数
    if serving is not None and serving.group(1) and serving.group(2):
        serving_g = _parse_label_number(serving.group(1))

    basis = "per_100g" if per_100g_re else ("per_serving" if serving else "unknown")
    reasons: list[str] = []
    if basis == "per_serving" and not serving_g:
        reasons.append("serving_without_net_weight")
    if basis == "unknown":
        reasons.append("basis_unknown")
    if not item:
        reasons.append("missing_item_name")
    if len(per_100g) < _SPOKEN_LABEL_MIN_FIELDS:
        reasons.append("too_few_fields")

    payload: dict[str, Any] = {
        "status": "needs_review" if reasons else "ok",
        "reasons": reasons,
        "item": item,
        "per_100g": per_100g,
        "basis": basis,
        "serving_g": serving_g,
        "raw_text": raw,
    }
    if reasons:
        payload["reason"] = reasons[0]
    if serving_g and basis == "per_serving":
        # 每份净含量已知 → 归一到每 100 克（下游一律按 per_100g 缩放）
        factor = 100.0 / serving_g
        payload["per_100g"] = {k: round(v * factor, 2) for k, v in per_100g.items()}
        payload["serving_label"] = "份"
    return payload


def spoken_label_entry(
    parsed: Mapping[str, Any],
    *,
    spoken_at: str = "",
    segment_ids: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """把 ``parse_spoken_label`` 的 ok 结果变成品牌库条目（source=user_spoken）。

    不 ok 时返回 None —— 调用方应把 ``needs_review`` 送进「需要我判断」。
    """
    if not isinstance(parsed, Mapping) or parsed.get("status") != "ok":
        return None
    item = str(parsed.get("item") or "").strip()
    per_100g = parsed.get("per_100g")
    if not item or not isinstance(per_100g, Mapping) or not per_100g:
        return None
    entry: dict[str, Any] = {
        "name": item,
        "per_100g": {k: float(v) for k, v in per_100g.items()},
        "source": "user_spoken",
        "evidence": "user_spoken",
        "origin": "user",
        "quote": str(parsed.get("raw_text") or "")[:400],
        "spoken_at": spoken_at,
        "confidence": "high",
    }
    if parsed.get("serving_g"):
        entry["serving_g"] = float(parsed["serving_g"])
        entry["serving_label"] = parsed.get("serving_label") or "份"
    if segment_ids:
        entry["segment_ids"] = list(segment_ids)
    return entry


def labels_conflict(existing: Mapping[str, Any], new: Mapping[str, Any], *, tolerance: float = 0.10) -> list[str]:
    """同一条目两次念的数值是否冲突（任一字段相对差 > tolerance 即冲突）。"""
    old = existing.get("per_100g") if isinstance(existing, Mapping) else None
    fresh = new.get("per_100g") if isinstance(new, Mapping) else None
    if not isinstance(old, Mapping) or not isinstance(fresh, Mapping):
        return []
    out: list[str] = []
    for key, value in fresh.items():
        prior = old.get(key)
        if prior in (None, 0):
            continue
        try:
            ratio = abs(float(value) - float(prior)) / abs(float(prior))
        except (TypeError, ValueError):
            continue
        if ratio > tolerance:
            out.append(f"{key}:{prior}→{value}")
    return out


# --- 多段念表合并 ---------------------------------------------------------------

#: 同一句话里出现这些词，说明还在念同一张表
_LABEL_FIELD_WORDS = ("能量", "热量", "蛋白质", "脂肪", "碳水", "糖", "钠", "千卡", "千焦")
#: 两段之间允许的最大间隔（秒）—— 超过就当成两次不同的念表
DEFAULT_MERGE_GAP_SECONDS = 90.0


def _label_segment_payload(text: str) -> dict[str, Any] | None:
    """单段是否像「在念表」，是就返回解析结果（可能 status=needs_review）。"""
    raw = str(text or "")
    hits = [w for w in _LABEL_FIELD_WORDS if w in raw]
    has_num = any(ch.isdigit() or ch in _LABEL_CN_DIGITS or ch in _LABEL_CN_UNITS for ch in raw)
    if len(hits) < 2 or not has_num:
        return None
    return parse_spoken_label(raw)


def merge_spoken_labels(
    segments: Sequence[Mapping[str, Any]],
    *,
    gap_seconds: float = DEFAULT_MERGE_GAP_SECONDS,
    speaker_gate: bool = True,
) -> list[dict[str, Any]]:
    """把「本人连续念表」的多段合成一次。

    规则（与派单一致）：
      * 只合并 ``source_label == "self"`` 的段（``speaker_gate=False`` 可关，仅供测试）；
      * 相邻两段间隔 ≤ ``gap_seconds``（默认 90 秒）；
      * 每段都得是「营养词 + 数字」；中间插一句「嗯」「然后」这种不含数字的短句会**打断**合并；
      * 食物名以**第一次出现名字**的那段为准，后面的段只补字段；
      * 同一字段两段数值相对差 > 10% → 整组 ``needs_review``（``reasons=[value_conflict]``）。

    ``segments`` 每项至少要有 ``text``，可选 ``spoken_at``（ISO，用于算间隔）、``segment_id``、``source_label``。
    返回每个合并组的解析结果（结构同 :func:`parse_spoken_label`，多一个 ``segment_ids``）。
    """
    ordered = [dict(seg) for seg in segments if isinstance(seg, Mapping) and str(seg.get("text") or "").strip()]
    ordered.sort(key=lambda seg: str(seg.get("spoken_at") or ""))

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    last_ts: datetime | None = None
    for seg in ordered:
        parsed = _label_segment_payload(str(seg["text"]))
        if parsed is None:
            # 不含营养数字的句子：把当前组收尾，避免把「然后呢」也算进表里
            if current:
                groups.append(current)
                current = []
                last_ts = None
            continue
        if speaker_gate and str(seg.get("source_label") or "self") != "self":
            if current:
                groups.append(current)
                current = []
                last_ts = None
            continue
        ts = _parse_dt(seg.get("spoken_at"))
        if current and last_ts is not None and ts is not None:
            if (ts - last_ts).total_seconds() > gap_seconds:
                groups.append(current)
                current = []
        current.append({"segment": seg, "parsed": parsed})
        last_ts = ts or last_ts
    if current:
        groups.append(current)

    merged: list[dict[str, Any]] = []
    for group in groups:
        per_100g: dict[str, float] = {}
        conflicts: list[str] = []
        for entry in group:
            for key, value in (entry["parsed"].get("per_100g") or {}).items():
                if key not in per_100g:
                    per_100g[key] = value
                    continue
                prior = per_100g[key]
                if prior:
                    ratio = abs(float(value) - float(prior)) / abs(float(prior))
                    if ratio > 0.10:
                        conflicts.append(f"{key}:{prior}→{value}")
        item = next((e["parsed"].get("item") for e in group if e["parsed"].get("item")), "") or ""
        reasons: list[str] = []
        if conflicts:
            reasons.append("value_conflict")
        if not item:
            reasons.append("missing_item_name")
        if len(per_100g) < _SPOKEN_LABEL_MIN_FIELDS:
            reasons.append("too_few_fields")
        merged.append({
            "status": "needs_review" if reasons else "ok",
            "reasons": reasons,
            "reason": reasons[0] if reasons else None,
            "item": item,
            "per_100g": per_100g,
            "basis": next((e["parsed"].get("basis") for e in group
                           if e["parsed"].get("basis") not in (None, "unknown")), "unknown"),
            "serving_g": next((e["parsed"].get("serving_g") for e in group if e["parsed"].get("serving_g")), None),
            "raw_text": " / ".join(str(e["segment"].get("text") or "") for e in group),
            "segment_ids": [str(e["segment"].get("segment_id") or "") for e in group],
            "merged_segments": len(group),
            "conflicts": conflicts,
        })
    return merged


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


_LOOKUP_COMMAND_RE = re.compile(
    r"(?:查一下|查查|查下|查询|搜一下)\s*(?:营养库|食物库|营养)?\s*(?:里|里面)?\s*(?:的)?\s*(.+?)\s*(?:的)?\s*(?:营养|热量|成分|数据)?\s*[？?。!！]*$"
)


def lookup_command_reply(text: str, lookup: "FoodLookup | None" = None) -> dict[str, Any] | None:
    """「查一下营养库 X / 查一下 X 的营养」→ 只回报，不写库。

    返回 ``{"kind":"found"|"not_found"|"no_query", "reply":…, "source":…}``；
    不是查询口令时返回 None（交给别的口令处理）。
    """
    raw = str(text or "").strip()
    if not raw or ("营养" not in raw and "热量" not in raw and "营养库" not in raw):
        return None
    match = _LOOKUP_COMMAND_RE.search(raw)
    if match is None:
        return None
    query = match.group(1).strip(" 的，,。.？?")
    if not query or query in {"营养库", "食物库"}:
        return {"kind": "no_query", "reply": "要查哪个食物？说「查一下 牛奶 的营养」就行。"}
    resolved = lookup if lookup is not None else (default_food_lookup() if _DEFAULT_LOOKUP or True else None)
    result = None
    if resolved is not None:
        try:
            result = resolved.lookup(query)
        except Exception:
            result = None
    if result is None:
        return {
            "kind": "not_found",
            "query": query,
            "reply": f"营养库里没有「{query}」的记录（本地库、中国成分表、USDA、Open Food Facts 都没查到）。",
        }
    values = []
    for key, label, unit in (
        ("energy_kcal", "能量", "千卡"),
        ("protein_g", "蛋白质", "克"),
        ("fat_g", "脂肪", "克"),
        ("carbohydrates_g", "碳水", "克"),
        ("sugar_g", "糖", "克"),
        ("sodium_mg", "钠", "毫克"),
    ):
        value = (getattr(result, "per_100g", {}) or {}).get(key)
        if value not in (None, ""):
            values.append(f"{label} {round(float(value), 1)}{unit}")
    source_cn = {
        "user": "你念的营养表",
        "user_spoken": "你念的营养表",
        "brand": "自建品牌库",
        "off": "Open Food Facts",
        "usda": "USDA",
        "cn_food": "中国成分表",
    }.get(str(getattr(result, "source", "")), str(getattr(result, "source", "") or "未知来源"))
    return {
        "kind": "found",
        "query": query,
        "source": str(getattr(result, "source", "")),
        "matched_name": str(getattr(result, "matched_name", "") or query),
        "per_100g": dict(getattr(result, "per_100g", {}) or {}),
        "match_kind": str(getattr(result, "match_kind", "")),
        "reply": f"{getattr(result, 'matched_name', query) or query}（每 100 克）："
                 + "，".join(values) + f"。来源：{source_cn}。",
    }


# --------------------------------------------------------------------------------------
# 模块级单例（给 food_nutrition / 语音口令共用）
# --------------------------------------------------------------------------------------

_DEFAULT_LOCK = threading.Lock()
_DEFAULT_LOOKUP: FoodLookup | None = None


def _brand_seed() -> list[Any]:
    """延迟导入 food_nutrition 取 26 条硬编码表（避免模块级循环导入）。"""
    from life_logger.food_nutrition import INGREDIENTS

    return list(INGREDIENTS)


def default_food_lookup() -> FoodLookup:
    global _DEFAULT_LOOKUP
    if _DEFAULT_LOOKUP is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_LOOKUP is None:
                _DEFAULT_LOOKUP = FoodLookup(seed_provider=_brand_seed)
    return _DEFAULT_LOOKUP


def lookup_food(name: str, grams: float | None = None, *, lookup: FoodLookup | None = None) -> FoodNutrition | None:
    """便捷入口：``lookup_food("生椰拿铁")``。"""
    engine = lookup or default_food_lookup()
    return engine.lookup(name, grams)


def apply_nutrition_captures(lookup: FoodLookup, captures: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把「记录营养 …」口令的解析结果落到层①品牌库（与习惯口令同款消费方式）。

    入参是 ``voice_triggers.scan_voice_triggers`` 的 ``to_dict()`` 列表，
    只处理 ``trigger_type == "nutrition_label"``。
    """
    outcomes: list[dict[str, Any]] = []
    for capture in captures or ():
        if not isinstance(capture, Mapping):
            continue
        if str(capture.get("trigger_type") or "") != "nutrition_label":
            continue
        metadata = capture.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        name = str(metadata.get("item") or "").strip()
        per_100g = metadata.get("per_100g")
        if not name or not isinstance(per_100g, Mapping):
            # 「记录营养 某东西」但没报数值：不猜、不写库，只回报原因（上游落待确认区）
            reason = str(metadata.get("reason") or "缺少食物名或营养数值")
            outcomes.append({"applied": False, "item": name, "reason": reason})
            continue
        try:
            stored = lookup.remember_user_entry(
                name,
                per_100g,
                serving_g=metadata.get("serving_g"),
                serving_label=metadata.get("serving_label"),
                note=f"语音汇报（{capture.get('captured_at') or ''}）",
            )
        except Exception:
            outcomes.append({"applied": False, "item": name, "reason": "写入失败"})
            continue
        outcomes.append({"applied": bool(stored), "item": name, "source": SOURCE_BRAND})
    return outcomes
