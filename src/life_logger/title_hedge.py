"""标题第一人称断言降级（hedge）—— 纯函数模块。

## 为什么需要

`safe_identity.py` 的既定红线：`source_label`（ASR 声纹标签）**只算候选**，
不足以确认「本人」。真实数据里因此出现三方矛盾：

    items[].identity_safe.identity = "unknown"
    speech_target                  = "未知"
    title                          = "用户本人询问智能体任务派发…"   ← 确定的第一人称断言

模型在 `uncertainties` 里把 `source_label == "self"` 当成「可认定用户本人发言」，
于是标题写成了确定行为。本模块只做一件事：**把确定的第一人称断言降级为 hedge 表述**。

## 判据（只用 identity_safe）

`needs_hedge()` 只读 `items[].identity_safe.identity`：

* 任意一个 item 不是 `"self"`（含缺失 / None / 非 dict / 其它值）→ 需要 hedge
* 全部 item 都是 `"self"` → 不需要

**绝不**使用 `source_label` / `origin` / `voice_similarity` / `provenance`
作为「确认本人」的依据 —— 这正是被修的那个 bug。

## 纯函数

无 I/O、不读盘、不写盘、不 import 服务层模块。
"""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping

__all__ = ["hedge_first_person", "hedge_text", "needs_hedge",
           "hedge_activity", "hedge_activity_display",
           "needs_unanchored_hedge", "hedge_unanchored_assertion"]

#: 已经带了 hedge 语气的词。出现这些词时，**不再二次改写**（除非仍含"用户本人"这种确定断言）。
_HEDGE_MARKERS = ("疑似", "据称", "转述", "提及", "来自媒体", "录音中", "可能", "不确定")

#: `本人` 后面紧跟这些动词时视为「确定行为断言」。
_SELF_VERBS = "在|说|问|买|看|听|刷|提|询|称|讲|道|做|去|吃|喝|用|叫|让|要|想|觉得|记得"

#: 「用户本人提及 X」→「录音中提及：X」（保留提及语义，去掉身份断言）
_USER_SELF_MENTION = re.compile(r"^用户本人(?:口头)?提及[：:]?\s*(.+)$")
#: 「用户本人询问/问 X」→「录音中疑似本人询问X」
_USER_SELF_ASK = re.compile(r"^用户本人(询问|问|反问|追问|质询|要求|建议|表示|称|说)")
#: 其它「用户本人…」
_USER_SELF_ANY = re.compile(r"^用户本人")
#: 「本人+动词」出现在句首或分隔符之后
_SELF_VERB = re.compile(r"(^|[，。、；：\s])本人(?=" + _SELF_VERBS + r")")
#: [2026-10-29] 副词插入形态：`本人正在写代码` / `本人正吃饭` / `本人在开会`。
#: 旧判据要求「本人」**紧跟**动词，`正在`/`正` 不是动词表成员 → 整句原样放过（红线缺口）。
#: 这里要求「本人 + (正在|正|在) + 一个汉字」，边界仍紧贴「本人」，不会句中任意位置误伤。
_SELF_VERB_ADVERB = re.compile(
    r"(^|[，。、；：\s])本人(?=(?:正在|正|在)[\u4e00-\u9fff])")
#: [2026-10-29] 中英混排：`I 用户本人 coding` / `本人 coding`。
#: 只匹配「本人/用户本人 紧跟英文单词（≥2 字母）」——有边界，不是无差别匹配英文。
_SELF_LATIN = re.compile(r"(?<!疑似)(?:用户)?本人(?=\s*[A-Za-z]{2,})")
#: 句中残留的「用户本人」（不在句首、后接非动词）→ 最后兜底替换
_USER_SELF_INLINE = re.compile(r"(?<!疑似)用户本人")


def _already_hedged(title: str) -> bool:
    """标题是否已经是合法的 hedge 表述（不含确定的第一人称断言）。"""
    if "用户本人" in title:
        return False
    if _SELF_VERB.search(title):
        return False
    if _SELF_VERB_ADVERB.search(title):
        return False
    if _SELF_LATIN.search(title):
        return False
    if _USER_SELF_INLINE.search(title):
        return False
    # 含 hedge 词，且没有确定断言 → 视为已 hedge
    return any(m in title for m in _HEDGE_MARKERS)


def hedge_first_person(title: str) -> str:
    """把确定的第一人称断言降级为 hedge 表述。

    规则（顺序敏感）：

    1. ``用户本人提及 X``  → ``录音中提及：X``      （保留「提及」，去掉身份断言）
    2. ``用户本人询问/问/说/…`` → ``录音中疑似本人询问/问/说/…``
    3. 其它 ``用户本人…``   → ``录音中疑似本人…``
    4. ``本人+动词``        → ``疑似本人+动词``
    5. 已经是 hedge 的标题  → **原样返回**（幂等，不二次改写）
    """
    if not isinstance(title, str) or not title.strip():
        return title if isinstance(title, str) else ""

    text = title.strip()

    # 5. 先判是否已 hedge（"用户本人提及"不算已 hedge，它仍断言了身份）
    if _already_hedged(text):
        return title

    # 1. 「用户本人提及 X」——「提及」本身已是转述，只需去掉身份前缀
    m = _USER_SELF_MENTION.match(text)
    if m:
        return "录音中提及：" + m.group(1).strip()

    # 2. 「用户本人询问/问/…」→ 保留动词，只加 hedge 前缀
    if _USER_SELF_ASK.match(text):
        return "录音中疑似本人" + text[len("用户本人"):]

    # 3. 其它「用户本人…」
    if _USER_SELF_ANY.match(text):
        return "录音中疑似本人" + text[len("用户本人"):]

    # 4. 「本人+动词」→「疑似本人+动词」
    if _SELF_VERB.search(text):
        return _SELF_VERB.sub(lambda m: m.group(1) + "疑似本人", text)

    # 4b. 「本人 + 正在/正/在 + 汉字」（副词插入）→「疑似本人 + …」
    if _SELF_VERB_ADVERB.search(text):
        return _SELF_VERB_ADVERB.sub(lambda m: m.group(1) + "疑似本人", text)

    # 4c. 中英混排「本人/用户本人 + 英文动词」→「疑似本人 …」
    if _SELF_LATIN.search(text):
        return _SELF_LATIN.sub("疑似本人", text)

    # 4d. 句中残留的「用户本人」（后接非动词/非英文）→ 去掉身份断言
    if _USER_SELF_INLINE.search(text):
        return _USER_SELF_INLINE.sub("疑似本人", text)

    # 没有确定断言，原样返回
    return title


# ══════════════════════════════════════════════════════════════════════════
# [2026-10-29 红线补丁] 无锚定断言（un-anchored assertion）
#
# 真实缺口：`hedge_first_person()` 只处理**显式第一人称**（"用户本人…"/"本人…"）。
# 但生产里还有一类标题**没有主语**却直接断言具体行为，例如：
#
#     购买寿司并点猪杂汤饭
#     在店内现场要一包十元红塔山和一桶三块钱的水
#     傍晚店内点单与现场用餐交流      ← 同活动 summary 明写「没有点单…证据」
#
# 这些标题在 `identity_safe == unknown` 时会被读者理解为「本人确实做了」，
# 属于同一条红线的漏网形态。这里做**最小前缀降级**，不改写事实、不删原标题
# （`title_original` 保留原件，前端可展开查看）。
#
# 判据（刻意保守，避免改写"描述内容"的标题）：
#   1. 已带 hedge 词 → 不动
#   2. 命中具体行为动词，且**动词之前**没有转述/描述/计划锚点 → 降级
#   3. 动词后紧跟「的」（名词化，如"购买的证据"）→ 不动
# ══════════════════════════════════════════════════════════════════════════
#: 非「点」的其它消费/行为动词（保留 v2 的已验证形态）。
_OTHER_ACTIONS = re.compile(
    r'购买了|买了|购入|要一包|要一桶|要了一|付了|付款|结账|进店|到店|吃了|喝了')
#: 完成体（「点了/点单/吃了…」）——比「计划」强：句中出现计划也不能否定已完成动作。
_PERFECTIVE = re.compile(r'点(?:了|过)|(?:单|加|再)点|(?<![报清盘点指查试])点单|购买了|买了|吃了|喝了|付了|付款|结账')
#: 量词/数量锚点：点/买/吃 + (了|过)? + 数词? + 大小中? + 量词。
_MEASURE = r'份|杯|碗|瓶|盘|个|只|条|笼|串|盒|袋|桶|包|斤|两|克|元|块|支|听|罐|打|勺|口|张|件|双|套|样|款|种'
_QUANTIFIED = re.compile(
    r'(?:点|买|吃)(?:了|过)?(?:[一两二三四五六七八九十几半\d]+)?(?:大|小|中)?(?:' + _MEASURE + r')')
#: 「点」作为名词/复合词的一部分时，**前一个字**属于这个封闭语法类（X点：景点/优点/早点/盘点…）。
#: 这不是菜名清单，而是「不能出现在动词短语末尾」的黏着语素集合；宾语侧保持开放。
_COMPOUND_PREFIX = set(
    "报清盘点指查试快甜早晚饭景观焦热难重疑节起终地网优缺弱痛卖亮拐赛局特差有零几标钟装小一秒分时半"
    "三二四五六七八九十百千万要丁")
#: 「点」后面跟这些语素时是「点+V」复合词（点唱/点赞/点击/点火/点播…），不是动词+宾语。
_POINT_COMPOUND_AFTER = set("唱名击开赞评数破燃醒检算拨头缀明播映球火灯钞心券卡歌阵滴子点钟题兵")
#: 功能词：点/买/吃 后面跟它们时不是宾语。
_FUNCTION_AFTER = set("与和的在并还又都也把是就才再了吗呢吧之其此该这那哪什怎么样我你他她它咱俺您")
#: 「吃/买 + 结果补语」不是宾语（吃完/吃过/买好…）。
_RESULT_COMPLEMENT = set("完过好掉饱撑光亏法货惊力醋香相")
#: 调味（少点辣/多点醋/少放点盐/多加点糖…）：是口味调整，不是购买行为。
_SEASONING = re.compile(
    r'(?:少|多|不要|别|不)(?:放|加|来|搁)?点(?:儿|点)?'
    r'(?:辣|醋|盐|糖|甜|咸|酸|苦|麻|鲜|香|酱油|辣椒|胡椒|孜然|香菜|葱|蒜|姜|油|味精|酱)')
#: 程度（多点耐心/多点时间…）：「多点 + 抽象名词」不是消费。
_DEGREE = re.compile(
    r'多点(?:耐心|时间|心思|力气|精神|自信|勇气|运气|理解|包容|关注|照顾|休息|准备|耐心点)')
#: 非购买的「点」（点烟/点香/点蜡烛/点蚊香）：A0 裁定按「不降级」处理。
_NON_PURCHASE_LIGHT = re.compile(r'点(?:了|着|上|个)?(?:[一二两三四五六七八九十几半\d]+)?(?:根|支|盘|盒|个)?(?:烟|香|蜡烛|蚊香)')
#: 计划/意愿词与转折/顺承切分（分句内判定，不整句放弃）。
_PLAN_WORDS = re.compile(r'计划|打算|意愿|口头')
_CLAUSE_DELIM = re.compile(
    r'[，。；！？,;!?：:]|但是|但|可是|不过|然而|最后|还是|结果|于是|然后|改成|改为|最终|却|反而|而是')
#: 动词前的转述/描述锚点（**只在同一分句内**生效）。
_ACTION_ANCHOR = re.compile(
    r'口述|复述|汇报|报告|讲述|自述|回顾|记录|讨论|询问|问|称|说(?!好)|提到|提及|转述|观看|浏览|播放|片段|对白|交流|对局|游戏|'
    r'价格|吃法|建议|提议|是否|无|未|没有|媒体|视频|影片|节目|新闻|广告|教程|评论|问答')
_UNANCHORED_HEDGE = re.compile(
    r'疑似|可能|也许|大概|据称|据说|转述|录音中|片段中|未确认|不确定|待确认|无证据|只证明|不代表')

#: 无锚定断言统一前缀（与既有第一人称降级措辞保持一致）
UNANCHORED_HEDGE_PREFIX = "录音中疑似："


def _clause_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _CLAUSE_DELIM.finditer(text):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))
    return spans


def _span_of(pos: int, spans: list[tuple[int, int]]) -> tuple[int, int]:
    for start, end in spans:
        if start <= pos < end:
            return (start, end)
    return (0, 0)


def _is_bare_verb_object(text: str, i: int) -> bool:
    """判断位置 i 的 点/买/吃 是否是「动词 + 开放宾语」结构（不枚举菜名）。"""
    verb = text[i]
    if i + 1 >= len(text):
        return False
    after = text[i + 1]
    if after in _FUNCTION_AFTER:
        return False
    if not ('\u4e00' <= after <= '\u9fff'):
        return False
    if verb == '点':
        if i > 0 and text[i - 1] in _COMPOUND_PREFIX:
            return False
        if after in _POINT_COMPOUND_AFTER:
            if after == '火' and text[i + 2:i + 3] in {'锅', '炉'}:
                return True
            return False
        return True
    # 买 / 吃
    if i > 0 and text[i - 1] in set('不没别勿'):
        return False
    if after in _RESULT_COMPLEMENT:
        return False
    return True


def _candidates(text: str):
    """产出 (start, end, kind)；kind ∈ perfective/quantified/bare。"""
    for m in _PERFECTIVE.finditer(text):
        if text[m.start()] == '点' and m.start() > 0 and text[m.start() - 1] in _COMPOUND_PREFIX:
            continue
        yield (m.start(), m.end(), 'perfective')
    for m in _QUANTIFIED.finditer(text):
        if text[m.start()] == '点' and m.start() > 0 and text[m.start() - 1] in _COMPOUND_PREFIX:
            continue
        # 「点火/点灯」这类复合词即使后面接量词也不当行为（点火器/点灯节）
        if text[m.start()] == '点' and m.start() + 1 < len(text):
            after = text[m.start() + 1]
            if after in _POINT_COMPOUND_AFTER and after != '火':
                continue
        yield (m.start(), m.end(), 'quantified')
    for m in _OTHER_ACTIONS.finditer(text):
        yield (m.start(), m.end(), 'perfective')
    for m in re.finditer(r'[点买吃]', text):
        if _is_bare_verb_object(text, m.start()):
            yield (m.start(), m.end(), 'bare')


def needs_unanchored_hedge(title: str) -> bool:
    """标题是否是「无锚定的具体行为断言」（需要加不确定性前缀）。

    [2026-10-30 v3] **结构判据**（不再枚举菜名）：
      1. 完成体（点了/点单/购买了/买了/吃了…）→ 直接判「是」；
      2. 数量/量词锚点（点一份/两杯/三碗/半斤/点碗面…）→ 直接判「是」；
      3. 开放宾语：点/买/吃 + 非功能词汉字，且「点」不在 X点 复合词里、后面不是点+V 复合语素
         → 判「是」（宾语集合是开放的，所以能覆盖点麻辣烫/点干锅/点小笼包…）；
      4. 计划词只在**同一分句**内、且动作不是完成体/数量形态时，才放弃；
      5. 转述/描述锚点只在**同一分句**的前缀里生效。
    """
    if not isinstance(title, str) or not title.strip():
        return False
    text = title.strip()
    if _UNANCHORED_HEDGE.search(text):
        return False
    if _SEASONING.search(text) or _DEGREE.search(text) or _NON_PURCHASE_LIGHT.search(text):
        return False
    spans = _clause_spans(text)
    plan_positions = [m.start() for m in _PLAN_WORDS.finditer(text)]

    cands = sorted(_candidates(text), key=lambda c: (c[0], c[1]))
    for start, end, kind in cands:
        if kind == 'bare':
            cstart, cend = _span_of(start, spans)
            if any(cstart <= p < cend for p in plan_positions):
                continue                      # 纯计划（同分句、非完成体）→ 跳过
        if _ACTION_ANCHOR.search(text[:start]):
            continue
        # 第三人称/机构主语（法国人吃… / 店员点单）不是本人行为 → 跳过
        if kind == 'bare':
            cstart, _ = _span_of(start, spans)
            if re.search(r'(?:人|者|员|们|店|馆|厨|主播)\s*$', text[cstart:start]):
                continue
        if text[end:end + 1] == '的':
            continue
        return True
    return False


def hedge_unanchored_assertion(title: str) -> str:
    """无锚定断言 → ``录音中疑似：<原标题>``；其余原样返回（幂等）。"""
    if not needs_unanchored_hedge(title):
        return title
    return UNANCHORED_HEDGE_PREFIX + title.strip()


#: 已带限定语/否定语境 → 视为已 hedge，不再改写（幂等；与 `_already_hedged` 同源语义）。
_NEG_CONTEXT = re.compile(
    r"疑似|据称|转述|来自媒体|录音中|可能|不确定|无法确认|无法可靠|无证据|不代表"
    r"|不能认定|待确认|并非本人|未能确认|无法判定"
)
#: 任意位置的「本人+动词」（排除已加「疑似」前缀的、以及「用户本人」整体）
_SELF_VERB_ANY = re.compile(r"(?<!疑似)(?<!用户)本人(?=" + _SELF_VERBS + r")")


def hedge_text(text: Any) -> Any:
    """把**任意文本字段**里的确定第一人称断言降级（用于 summary / uncertainties / action）。

    * 与 `hedge_first_person` 同一判据语义：已带 `_NEG_CONTEXT` 限定语 → 原样返回（幂等）
    * `用户本人…` → `疑似本人…`；`…本人+动词` → `…疑似本人+动词`
    * 非字符串/空串 → 原样返回（绝不抛）
    """
    if not isinstance(text, str) or not text.strip():
        return text
    if _NEG_CONTEXT.search(text):
        return text
    out = text.replace("用户本人", "疑似本人")
    out = _SELF_VERB_ANY.sub(lambda m: "疑似本人", out)
    return out


def hedge_activity_display(activity: Mapping[str, Any] | None) -> dict[str, Any]:
    """展示层：把**同一活动里所有可承载身份断言的文本**降级（含 title）。

    [2026-10-29 红线补] 只改 title 不够：真实数据里 `uncertainties` 仍写着
    「与 rec_x、rec_y 同为用户本人询问且时间接近…」，重启后照样显示。
    本函数在 `hedge_activity`（title 三键）基础上，再处理：

    * `summary`             → 改 `summary` + 留 `summary_original`
    * `uncertainties[]`     → 逐条改 + 留 `uncertainties_original`（与改动前一模一样的 list）
    * `items[].action`      → 逐条改 + 该 item 上留 `action_original`

    **绝不触碰 `items[].evidence[].quote`**（ASR 逐字原文，用户红线：保留原话与证据）。
    判据仍只用 `needs_hedge()`（`items[].identity_safe.identity` 全部为 self 才不改）。
    已是 hedge 语气（`_NEG_CONTEXT`）→ 不再叠加（幂等）。
    """
    if not isinstance(activity, Mapping):
        return {}

    out = hedge_activity(activity)          # title 三键 + 深拷贝
    if not needs_hedge(activity):
        return out

    summary = out.get("summary")
    if isinstance(summary, str) and summary.strip():
        hedged_summary = hedge_text(summary)
        if hedged_summary != summary:
            out.setdefault("summary_original", summary)
            out["summary"] = hedged_summary

    uncertainties = out.get("uncertainties")
    if isinstance(uncertainties, list) and uncertainties:
        new_list: list[Any] = []
        changed = False
        for entry in uncertainties:
            hedged_entry = hedge_text(entry)
            if hedged_entry != entry:
                changed = True
            new_list.append(hedged_entry)
        if changed:
            out.setdefault("uncertainties_original", list(uncertainties))
            out["uncertainties"] = new_list

    items = out.get("items")
    if isinstance(items, list) and items:
        new_items: list[Any] = []
        changed_items = False
        for item in items:
            if isinstance(item, Mapping) and isinstance(item.get("action"), str):
                action = str(item.get("action"))
                hedged_action = hedge_text(action)
                if hedged_action != action:
                    copied = dict(item)
                    copied.setdefault("action_original", action)
                    copied["action"] = hedged_action
                    new_items.append(copied)
                    changed_items = True
                    continue
            new_items.append(item)
        if changed_items:
            out["items"] = new_items
    return out


def _identity_of(item: Any) -> str:
    """取 item 的 identity_safe.identity；任何异常形态一律返回空串（= 未确认）。"""
    if not isinstance(item, Mapping):
        return ""
    safe = item.get("identity_safe")
    if not isinstance(safe, Mapping):
        return ""
    value = safe.get("identity")
    return value.strip() if isinstance(value, str) else ""


def needs_hedge(activity: Mapping[str, Any] | None) -> bool:
    """是否需要给标题降级。

    判据**只用** ``items[].identity_safe.identity``：

    * 没有任何 items / 任意 item 的 identity 不是 ``"self"`` → 需要 hedge
    * 全部 item 都是 ``"self"`` → 不需要

    明确**不使用** ``source_label`` / ``origin`` / ``voice_similarity`` / ``provenance``。
    """
    if not isinstance(activity, Mapping):
        return True
    if activity.get("user_corrected") is True or activity.get("title_source") == "user_correction":
        return False
    items = activity.get("items")
    if not isinstance(items, list) or not items:
        return True
    for item in items:
        if _identity_of(item) != "self":
            return True
    return False


_FACT_TYPES_SELF_REPORT = frozenset({"expense", "food", "smoke", "exercise"})


def _item_has_amount_or_quantity(item: Mapping[str, Any]) -> bool:
    if item.get("amount") is not None and not isinstance(item.get("amount"), bool):
        return True
    exp = item.get("expense")
    if isinstance(exp, Mapping) and exp.get("amount") is not None and not isinstance(exp.get("amount"), bool):
        return True
    food = item.get("food")
    if isinstance(food, Mapping) and any(
        food.get(k) is not None and not isinstance(food.get(k), bool)
        for k in ("amount", "weight_g", "grams", "quantity", "count", "calories_kcal")
    ):
        return True
    smoke = item.get("smoke")
    if isinstance(smoke, Mapping) and smoke.get("count") is not None and not isinstance(smoke.get("count"), bool):
        return True
    ex = item.get("exercise")
    if isinstance(ex, Mapping) and any(
        ex.get(k) is not None and not isinstance(ex.get(k), bool)
        for k in ("sets", "reps", "weight_kg", "duration_minutes", "duration_seconds", "count", "distance_km")
    ):
        return True
    return False


def _is_quantified_self_fact_report(activity: Mapping[str, Any] | None) -> bool:
    """带消费/饮食/抽烟/运动事实（fact_type 为 expense/food/smoke/exercise 且有金额或数量）的本人报账。"""
    if not isinstance(activity, Mapping):
        return False
    if activity.get("lifecycle") in ("media", "unknown"):
        return False
    if str(activity.get("speech_target") or "").strip() in ("商家/店员", "媒体内容", "旁人"):
        return False
    items = activity.get("items")
    if not isinstance(items, list) or not items:
        return False
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if item.get("item_lifecycle") in ("media", "unknown"):
            continue
        if item.get("fact_type") not in _FACT_TYPES_SELF_REPORT:
            continue
        if not _item_has_amount_or_quantity(item):
            continue
        evs = [ev for ev in (item.get("evidence") or []) if isinstance(ev, Mapping)]
        labels = [
            str(ev.get("source_label_original") or ev.get("source_label") or "").strip()
            for ev in evs
            if (ev.get("source_label_original") or ev.get("source_label"))
        ]
        if labels and any(lbl != "self" for lbl in labels):
            continue
        return True
    return False


def hedge_activity(activity: Mapping[str, Any] | None) -> dict[str, Any]:
    """需要时返回 hedged 的新 dict；不需要时返回原 dict 的浅拷贝（不改内容）。

    * ``title`` 已 hedge
    * ``title_original`` **只在首次 hedge 时写入**（二次调用不覆盖）
    * ``title_source = "model"``（首次生成来源；已存在则不覆盖）
    * ``title_hedged = True``（已存在则保持 True）
    * 其余所有键 **原样保留**（深拷贝，不就地修改入参）
    """
    if not isinstance(activity, Mapping):
        return {}

    out = copy.deepcopy(dict(activity))

    title = out.get("title")
    if not isinstance(title, str):
        title = ""

    if not needs_hedge(activity):
        return out

    self_fact_report = _is_quantified_self_fact_report(activity)
    if self_fact_report and title.startswith(UNANCHORED_HEDGE_PREFIX):
        stripped = title[len(UNANCHORED_HEDGE_PREFIX):].strip()
        if stripped and hedge_first_person(stripped) == stripped:
            out["title"] = stripped
            return out

    hedged = hedge_first_person(title)
    # [2026-10-29] 第一人称没命中时，再判「无锚定具体行为断言」（红线漏网形态）。
    # 只加不确定性前缀，不改写事实；原件仍写入 title_original。
    if hedged == title and not self_fact_report:
        hedged = hedge_unanchored_assertion(title)

    # 幂等：已经有 title_original 就不再写（保留最初原件）
    if "title_original" not in out:
        out["title_original"] = title
    # 来源：只在缺失时补，不覆盖已有值
    out.setdefault("title_source", "model")
    out["title_hedged"] = True

    # 只有真的变了才替换 title（避免「标题本来就合规」时的无意义改写）
    if hedged != title:
        out["title"] = hedged
    return out
