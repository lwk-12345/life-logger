"""口令词后的口述清洗测试。

清洗本身交给大模型（用户 2026-10-15：「直接把我的原句的转写的文字直接给大模型，
给个大模型一个提示词直接转写了呀，为什么做这么多，还要加一层规则去词呢」）。

所以这里的测试不测"规则删了哪些字"——规则层已经删掉了。测的是**接口契约**：

  1. 没接模型时，一行都不许动（清洗功能关闭，绝不影响现有行为）
  2. 接了模型时，用模型给的结果
  3. 模型失败/返回空 → 退回原文，绝不把用户的话弄丢
  4. 提示词把原话完整带进去，且带上了「不改原意」的约束
  5. original 永远保留原口述（可追溯）
"""

from __future__ import annotations

import pytest

from life_logger.todo_text_cleaner import (
    CLEAN_PROMPT,
    build_prompt,
    clean_command,
)


# ---------------------------------------------------------------------------
# ① 没接模型 = 功能关闭 = 一个字都不许改
#
# 这条最重要：现在生产上还没接模型（用户在选型），所以接上这个模块
# 之后行为必须与之前完全一致。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "呃，那个，明天拿快递",
        "查询：北京今天天气",
        "我我明天要要拿快递",
        "给妈妈打电话",          # 叠词陷阱：以前规则层把它吃成「给妈打电话」
        "北京今天天气",          # 叠词陷阱：以前被吃成「北京今天气」
    ],
)
def test_no_polisher_is_strictly_noop(raw: str) -> None:
    r = clean_command(raw, polisher=None)
    assert r.text == raw
    assert r.original == raw
    assert r.changed is False
    assert r.polished_by is None


def test_empty_input() -> None:
    r = clean_command("", polisher=None)
    assert r.text == ""
    assert r.changed is False


def test_whitespace_input() -> None:
    r = clean_command("   ", polisher=None)
    assert r.text == ""


# ---------------------------------------------------------------------------
# ② 接了模型 → 用模型的结果
# ---------------------------------------------------------------------------

def test_polisher_result_is_used() -> None:
    def fake_polisher(prompt: str) -> str:
        return "明天拿快递"

    r = clean_command("呃，那个，明天拿快递", polisher=fake_polisher)
    assert r.text == "明天拿快递"
    assert r.original == "呃，那个，明天拿快递"   # 原文保留，可追溯
    assert r.polished_by == "fake_polisher"


def test_polisher_receives_prompt_with_original_text() -> None:
    """模型必须收到「原话 + 约束」，不能只收到原话。"""
    seen: list[str] = []

    def spy(prompt: str) -> str:
        seen.append(prompt)
        return "ok"

    clean_command("呃明天拿快递", polisher=spy)
    assert len(seen) == 1
    assert "呃明天拿快递" in seen[0]
    assert "不要改变原意" in seen[0]


# ---------------------------------------------------------------------------
# ③ 失败必须退回原文（绝不能把用户的话弄丢）
# ---------------------------------------------------------------------------

def test_polisher_raises_falls_back_to_original() -> None:
    def broken(_prompt: str) -> str:
        raise RuntimeError("模型挂了")

    r = clean_command("明天拿快递", polisher=broken)
    assert r.text == "明天拿快递"
    assert r.polished_by is None


def test_polisher_returns_empty_falls_back() -> None:
    r = clean_command("明天拿快递", polisher=lambda _p: "")
    assert r.text == "明天拿快递"
    assert r.polished_by is None


def test_polisher_returns_whitespace_falls_back() -> None:
    r = clean_command("明天拿快递", polisher=lambda _p: "   \n  ")
    assert r.text == "明天拿快递"


def test_polisher_returns_none_falls_back() -> None:
    r = clean_command("明天拿快递", polisher=lambda _p: None)  # type: ignore[return-value]
    assert r.text == "明天拿快递"


# ---------------------------------------------------------------------------
# ④ 模型的输出杂质要被剥掉（它偶尔会自作主张）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model_output, expected",
    [
        ('"明天拿快递"', "明天拿快递"),
        ("“明天拿快递”", "明天拿快递"),
        ("1. 明天拿快递", "明天拿快递"),
        ("1、明天拿快递", "明天拿快递"),
        ("- 明天拿快递", "明天拿快递"),
        ("  明天拿快递  ", "明天拿快递"),
    ],
)
def test_model_output_is_cleaned(model_output: str, expected: str) -> None:
    r = clean_command("呃明天拿快递", polisher=lambda _p: model_output)
    assert r.text == expected


# ---------------------------------------------------------------------------
# ⑤ 提示词本身
# ---------------------------------------------------------------------------

def test_prompt_contains_original_text() -> None:
    p = build_prompt("我要拿快递")
    assert "我要拿快递" in p


def test_prompt_has_anti_hallucination_clause() -> None:
    """这几条约束是防模型乱改用户原意的，不能被人误删。"""
    for clause in ("不要改变原意", "不要添加原文里没有的信息"):
        assert clause in CLEAN_PROMPT


def test_prompt_asks_for_bare_sentence() -> None:
    assert "只输出整理后的那一句话" in CLEAN_PROMPT


# ---------------------------------------------------------------------------
# ⑥ 真实场景（用真实口述做端到端，模型用桩替代）
# ---------------------------------------------------------------------------

def test_real_case_stutter_and_particles() -> None:
    """用户 2026-10-15 描述的场景：口令词之后说了句带口吃和口水词的话。"""

    def stub(prompt: str) -> str:
        # 桩模拟模型会做的整理
        return "去拿快递"

    r = clean_command("呃…我那个…就是…明天要要拿快递吧", polisher=stub)
    assert r.text == "去拿快递"
    assert r.original == "呃…我那个…就是…明天要要拿快递吧"


def test_real_case_query_prefix() -> None:
    def stub(prompt: str) -> str:
        return "北京今天天气"

    r = clean_command("查询：北京今天的天天气怎么样", polisher=stub)
    assert r.text == "北京今天天气"


# ---------------------------------------------------------------------------
# ⑦ 叠词安全（以前规则层翻车的地方，现在模型自己处理，接口层不许再碰）
# ---------------------------------------------------------------------------

def test_reduplicated_words_untouched_without_polisher() -> None:
    for raw in ("给妈妈打电话", "爸爸说", "北京今天天气", "慢慢走"):
        assert clean_command(raw, polisher=None).text == raw
