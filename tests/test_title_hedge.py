"""title_hedge 单测：第一人称断言降级。

覆盖派单要求的 6 组：
1. 四条真实标题 → hedge 后不再含「用户本人」
2. 已 hedge 表述 → 原样返回
3. identity_safe.identity == "self" → 不 hedge（反向对照）
4. 红线：source_label="self" 但 identity_safe="unknown" → 必须 hedge（防回归）
5. 幂等：连调两次，title_original 不被覆盖
6. 字段保真：除 4 个键外逐键相等（深比较）
"""
from __future__ import annotations

import copy

import pytest

from life_logger.title_hedge import (hedge_activity, hedge_first_person, needs_hedge,
                                     hedge_unanchored_assertion)


def _activity(title: str, identities: list[str], **extra):
    """构造最小活动（items 里带 identity_safe）。"""
    act = {
        "activity_id": "act_test_001",
        "title": title,
        "speech_target": "未知",
        "status": "unknown",
        "category": "生活·日常",
        "items": [
            {
                "item_id": f"item_{i}",
                "action": "测试动作",
                "evidence": [{"source_id": f"src_{i}", "quote": "原话逐字"}],
                "identity_safe": {"schema": "evidence-safe-identity-v1", "identity": ident,
                                  "candidate_only": True},
            }
            for i, ident in enumerate(identities)
        ],
        "uncertainties": ["source_label 为 self，可认定为用户本人发言"],
        "occurrence_start": "2026-10-28T10:00:00+08:00",
        "occurrence_end": "2026-10-28T10:05:00+08:00",
        "related_transaction_ids": [],
        "reported_at": "2026-10-28T10:06:00+08:00",
    }
    act.update(extra)
    return act


# ══════════════════════════════════════════════════════════
# 1. 四条真实标题（10-28 实测原文）
# ══════════════════════════════════════════════════════════
REAL_FOUR = [
    "用户本人询问智能体任务派发与终端空闲情况",
    "用户本人询问是否优化提示词",
    "用户本人询问此前派发程序检查UI设计任务的反馈",
    "用户本人提及“小鹿没有被封杀”及诈骗传闻",
]


@pytest.mark.parametrize("title", REAL_FOUR)
def test_real_four_titles_lose_first_person_assertion(title):
    out = hedge_activity(_activity(title, ["unknown"]))
    assert "用户本人" not in out["title"], out["title"]
    # 语义仍保留：询问/提及
    assert ("询问" in out["title"]) or ("提及" in out["title"])
    # 原件保留
    assert out["title_original"] == title
    assert out["title_source"] == "model"
    assert out["title_hedged"] is True


def test_mention_becomes_transcript_wording():
    out = hedge_activity(_activity("用户本人提及“小鹿没有被封杀”及诈骗传闻", ["unknown"]))
    assert out["title"] == "录音中提及：“小鹿没有被封杀”及诈骗传闻"


def test_ask_keeps_verb_and_adds_hedge():
    out = hedge_activity(_activity("用户本人询问是否优化提示词", ["unknown"]))
    assert out["title"] == "录音中疑似本人询问是否优化提示词"


# ══════════════════════════════════════════════════════════
# 2. 已经是 hedge 的表述 → 原样返回
# ══════════════════════════════════════════════════════════
@pytest.mark.parametrize("title", [
    "录音中提及：小鹿没有被封杀",
    "转述某主播关于诈骗的传闻",
    "疑似本人在客厅走动",
    "来自媒体的剧情对白片段",
    "据称当晚有直播活动",
    "可能是在讨论游戏对局",
])
def test_already_hedged_titles_unchanged(title):
    assert hedge_first_person(title) == title
    out = hedge_activity(_activity(title, ["unknown"]))
    assert out["title"] == title


# ══════════════════════════════════════════════════════════
# 3. 反向对照：identity_safe 全是 self → 不 hedge
# ══════════════════════════════════════════════════════════
def test_no_hedge_when_identity_confirmed_self():
    act = _activity("用户本人询问智能体任务派发与终端空闲情况", ["self"])
    assert needs_hedge(act) is False
    out = hedge_activity(act)
    assert out["title"] == act["title"]          # 标题未被改写
    assert "title_original" not in out            # 不写原件


def test_no_hedge_when_all_items_self():
    act = _activity("本人买了一份寿司", ["self", "self"])
    assert needs_hedge(act) is False


# ══════════════════════════════════════════════════════════
# 4. 红线：source_label="self" 但 identity_safe="unknown" → 必须 hedge
# ══════════════════════════════════════════════════════════
def test_redline_source_label_self_but_identity_unknown_must_hedge():
    act = _activity("用户本人询问是否优化提示词", ["unknown"],
                    provenance=[{"source_kind": "formal", "source_label": "self"}])
    # 显式放一个 source_label=self 在 item 上
    act["items"][0]["source_label"] = "self"
    act["items"][0]["origin"] = "self"
    assert needs_hedge(act) is True, "source_label=self 不得被当成身份确认"
    out = hedge_activity(act)
    assert "用户本人" not in out["title"]
    assert out["title"].startswith("录音中疑似本人")


@pytest.mark.parametrize("bad_identity", ["unknown", "", None, "other_person", "mixed"])
def test_any_non_self_requires_hedge(bad_identity):
    act = _activity("本人在说话", ["self", bad_identity])
    assert needs_hedge(act) is True


def test_missing_identity_safe_requires_hedge():
    act = _activity("用户本人询问某件事", ["unknown"])
    del act["items"][0]["identity_safe"]
    assert needs_hedge(act) is True


def test_no_items_requires_hedge():
    act = _activity("用户本人询问某件事", [])
    assert needs_hedge(act) is True


# ══════════════════════════════════════════════════════════
# 5. 幂等
# ══════════════════════════════════════════════════════════
def test_idempotent_title_and_original_not_overwritten():
    act = _activity("用户本人询问是否优化提示词", ["unknown"])
    first = hedge_activity(act)
    second = hedge_activity(first)
    assert second["title"] == first["title"]                       # 不再二次改写
    assert second["title_original"] == act["title"]                 # 原件不被覆盖
    assert second["title_original"] != first["title"]
    assert second["title_hedged"] is True
    assert "用户本人" not in second["title"]


# ══════════════════════════════════════════════════════════
# 6. 字段保真：除 4 个键外逐键相等（深比较）
# ══════════════════════════════════════════════════════════
ALLOWED = {"title", "title_original", "title_source", "title_hedged"}


def test_all_other_keys_deep_equal():
    act = _activity("用户本人提及“小鹿没有被封杀”及诈骗传闻", ["unknown", "unknown"])
    before = copy.deepcopy(act)
    out = hedge_activity(act)

    # 只允许新增/改动这 4 个键
    assert set(out) - set(act) <= ALLOWED, f"多出键：{set(out) - set(act)}"
    assert set(act) - set(out) == set(), f"丢键：{set(act) - set(out)}"

    for key in act:
        if key in ALLOWED:
            continue
        assert out[key] == before[key], f"键 {key} 被改动"

    # items / evidence / quote 一个都不能少
    assert out["items"] == before["items"]
    assert out["items"][0]["evidence"][0]["quote"] == "原话逐字"
    assert out["uncertainties"] == before["uncertainties"]


def test_input_not_mutated():
    act = _activity("用户本人询问是否优化提示词", ["unknown"])
    snapshot = copy.deepcopy(act)
    hedge_activity(act)
    assert act == snapshot, "hedge_activity 不得就地修改入参"


def test_not_activity_returns_empty_dict():
    assert hedge_activity(None) == {}
    assert hedge_activity("not-a-dict") == {}
    assert needs_hedge(None) is True


# ── 2026-10-29 扩展：hedge_text / hedge_activity_display ─────────────────────
def test_hedge_text_mid_sentence_and_idempotent():
    from life_logger.title_hedge import hedge_text

    src = "与 rec_a、rec_b 同为用户本人询问且时间接近，暂独立保留"
    once = hedge_text(src)
    assert "用户本人" not in once and "疑似本人" in once
    assert hedge_text(once) == once                     # 幂等，不二次叠加
    # 已带限定语 → 原样
    assert hedge_text("疑似本人询问过，暂无法确认") == "疑似本人询问过，暂无法确认"
    # 非字符串/空 → 原样
    assert hedge_text(None) is None
    assert hedge_text(123) == 123
    assert hedge_text("") == ""


def test_hedge_activity_display_does_not_touch_evidence():
    from life_logger.title_hedge import hedge_activity_display

    act = {"activity_id": "a", "title": "用户本人询问X",
           "summary": "用户本人询问了X",
           "uncertainties": ["用户本人询问X，待观察"],
           "items": [{"item_id": "i", "identity_safe": {"identity": "unknown"},
                      "action": "用户本人询问X",
                      "evidence": [{"quote": "用户本人询问X"}]}]}
    out = hedge_activity_display(act)
    assert out["items"][0]["evidence"][0]["quote"] == "用户本人询问X"
    assert out["items"][0]["action"] == "疑似本人询问X"
    assert out["summary_original"] == act["summary"]
    assert out["uncertainties_original"] == act["uncertainties"]
    # 入参不被就地修改
    assert act["summary"] == "用户本人询问了X"


# ── 2026-10-29：副词插入 / 中英混排漏网（A0 亲测的 6 个 MISS）────────────────
import pytest as _pytest  # noqa: E402


@_pytest.mark.parametrize("src,expected", [
    ("本人正在写代码", "疑似本人正在写代码"),
    ("本人正在开会", "疑似本人正在开会"),
    ("本人正在吃饭", "疑似本人正在吃饭"),
    ("本人正在看直播", "疑似本人正在看直播"),
    ("本人正在外出", "疑似本人正在外出"),
    ("本人正写代码", "疑似本人正写代码"),
    ("本人在开会", "疑似本人在开会"),
    ("I 用户本人 coding", "I 疑似本人 coding"),
    ("本人 coding", "疑似本人 coding"),
])
def test_adverb_and_latin_forms_are_hedged(src, expected):
    from life_logger.title_hedge import hedge_first_person

    assert hedge_first_person(src) == expected


@_pytest.mark.parametrize("src,expected", [
    ("本人在家休息", "疑似本人在家休息"),
    ("本人在公司加班", "疑似本人在公司加班"),
    ("本人在健身", "疑似本人在健身"),
    ("本人在做饭", "疑似本人在做饭"),
    ("本人在打车", "疑似本人在打车"),
    ("用户本人在写代码", "录音中疑似本人在写代码"),
    ("用户本人写代码", "录音中疑似本人写代码"),
    ("用户本人到公司了", "录音中疑似本人到公司了"),
    ("用户本人点外卖", "录音中疑似本人点外卖"),
    ("用户本人付了钱", "录音中疑似本人付了钱"),
])
def test_known_good_forms_unchanged(src, expected):
    """已正确的 39 类形态不得被新规则改坏（这里抽查 10 例，输出逐字节固定）。"""
    from life_logger.title_hedge import hedge_first_person

    assert hedge_first_person(src) == expected


def test_adverb_forms_idempotent():
    from life_logger.title_hedge import hedge_first_person

    for src in ("本人正在写代码", "本人正在开会", "I 用户本人 coding", "本人在家休息"):
        once = hedge_first_person(src)
        assert hedge_first_person(once) == once
        assert "疑似疑似" not in once


def test_neutral_and_non_assertion_not_touched():
    from life_logger.title_hedge import hedge_first_person

    for src in ("媒体内容：法国人吃青蛙腿的问答/辩论片段",
                "讲述分工方法与读书内容（外语音频转录）",
                "这份材料由本人保管",           # 「本人」前不是分隔符 → 不动
                "疑似本人正在写代码",           # 已 hedge → 不动
                "据称本人正在写代码"):          # 已带限定语 → 不动
        assert hedge_first_person(src) == src


# ══════════════════════════════════════════════════════════
# [2026-10-30 返工] 「点 + 汉字宾语」形态（点瘦肉粉 / 点芝麻酱）
# ══════════════════════════════════════════════════════════
@pytest.mark.parametrize("title", [
    "现场点瘦肉粉并询问价格，后在店内吃一碗",
    "傍晚现场点芝麻酱并称赞饭菜好吃",
])
def test_point_object_form_is_hedged(title):
    out = hedge_activity(_activity(title, ["unknown"]))
    assert out["title"] == "录音中疑似：" + title
    assert out["title_original"] == title


@pytest.mark.parametrize("title", [
    "晚间点唱《朋友的酒》与带一打饮料的口头交流",
    "晚间游戏对局中交付物品、九张清点与补子弹交流",
    "深夜游戏对局中报点、换点与六套判断",
    "深夜游戏对局中报“没跟上”“修甲”“三个包快点吃”",
    "傍晚餐后买甜点水果及奶茶的口头计划",
    "傍晚现场点赞并点评了几句",
    "他说要点开设置并点击确认",
    "现场点名确认了三个人",
    "这几位选手点数占优",
])
def test_point_object_false_positive_forms_untouched(title):
    assert hedge_unanchored_assertion(title) == title
    out = hedge_activity(_activity(title, ["unknown"]))
    assert out["title"] == title


def test_point_object_idempotent_no_double_prefix():
    src = "现场点瘦肉粉并询问价格，后在店内吃一碗"
    once = hedge_activity(_activity(src, ["unknown"]))["title"]
    twice = hedge_activity(_activity(once, ["unknown"]))
    assert once == "录音中疑似：" + src
    assert twice["title"] == once
    assert "疑似：录音中疑似" not in twice["title"]
    assert twice["title_original"] == once   # 二次调用保留传入值（幂等：不覆盖）


# ── [2026-10-30 收窄] 「点 + 宾语」白名单：压测 19 误降 + 3 漏降的回归锁定 ──────
_POINT_FALSE_POSITIVES = [
    "有点累", "一点小事", "三点左右", "几点睡", "零点以后", "标点符号", "点滴", "点火",
    "点灯", "点球", "点钞", "点心", "点播", "点映", "点券", "点卡", "钟点工", "装点房间",
    "点明问题", "试点单位", "盘点单", "一点点水",
]
_POINT_TRUE_POSITIVES = [
    "点菜", "点餐", "点单", "点了两碗", "点外卖", "点一份炒饭", "点奶茶", "点咖啡",
    "点芝麻酱", "点瘦肉粉", "点两瓶水", "点一份水饺",
    "买甜点", "吃早点", "买早点",
]


def _unknown_activity(title: str) -> dict:
    return {"title": title, "items": [{"identity_safe": {"identity": "unknown"}}]}


@pytest.mark.parametrize("title", _POINT_FALSE_POSITIVES)
def test_point_nouns_are_not_hedged(title: str) -> None:
    hedged = hedge_activity(_unknown_activity(title))
    assert hedged["title"] == title, f"中性标题不应加前缀: {title}"


@pytest.mark.parametrize("title", _POINT_TRUE_POSITIVES)
def test_point_consumption_actions_are_hedged(title: str) -> None:
    hedged = hedge_activity(_unknown_activity(title))
    assert hedged["title"] == "录音中疑似：" + title
    assert hedged["title_original"] == title


def test_point_plan_context_is_not_hedged() -> None:
    title = "傍晚餐后买甜点水果及奶茶的口头计划"
    assert hedge_activity(_unknown_activity(title))["title"] == title


def test_point_hedge_is_idempotent() -> None:
    once = hedge_activity(_unknown_activity("点瘦肉粉"))
    twice = hedge_activity(once)
    assert twice["title"] == "录音中疑似：点瘦肉粉"
    assert twice["title_original"] == "点瘦肉粉"


@pytest.mark.parametrize("fact_type,title,item_extra", [
    ("expense", "花27元点两份清补凉", {"amount": 27, "expense": {"amount": 27, "currency": "CNY", "item": "两份清补凉"}}),
    ("food", "吃了200克鸡胸肉", {"food": {"name": "鸡胸肉", "weight_g": 200}}),
    ("smoke", "抽了一根烟", {"smoke": {"count": 1}}),
    ("exercise", "做3组卧推", {"exercise": {"name": "卧推", "sets": 3}}),
])
def test_quantified_self_fact_report_not_prefixed_with_unanchored_hedge(fact_type, title, item_extra):
    act = _activity(title, ["unknown"])
    act["items"][0]["fact_type"] = fact_type
    act["items"][0]["evidence"][0]["source_label"] = "self"
    act["items"][0].update(item_extra)
    out = hedge_activity(act)
    assert out["title"] == title
    assert not out["title"].startswith("录音中疑似：")

    # Already-prefixed on disk is also restored cleanly on read path
    pre_hedged = copy.deepcopy(act)
    pre_hedged["title"] = "录音中疑似：" + title
    pre_hedged["title_original"] = title
    pre_hedged["title_hedged"] = True
    restored = hedge_activity(pre_hedged)
    assert restored["title"] == title

