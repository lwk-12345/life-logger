"""Q822 / Q823 回归：`non_self` ≠ 媒体；证据不足 ≠ 本人生活。

两件被推翻了的事（都是同一个错误的两个方向）：

  * 旧规则 A：`self == 0` ⇒ `media`
    → 把家人对话里**对方说的话**判成视频台词。实测 11-02 的 29 个 media 子项里
      只有 7 个是 far 场（可能屏幕串音），其余 22 个是 near/uncertain = 坐在对面的人：
      act_145–151 与妈妈聊天、act_160/162、act_072、act_127。
  * 旧规则 B：无任何声纹证据 ⇒ `live`
    → 把「不知道」写成「本人生活」。10-02/10-08/10-26 三天从没跑过
      `backfill_evidence_source_label.py`，272 张卡全靠标题猜。

新口径（与任务书 ①–④ 一一对应）：
  ① 用户明确纠正 > 一切
  ② 有 self 证据 + 无明确媒体结构证据 → live
  ③ 只有 non_self + 明确媒体结构证据   → media
  ④ 其余（无声纹 / 只有 unknown / 证据不足） → **unknown**（不折叠，标待确认）

本文件只测判定语义，不测像素；渲染另由
`work/three-state-unknown/check_render.mjs` 覆盖。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import feedback_store as fs  # noqa: E402
from life_logger.activity_timeline_runtime import (  # noqa: E402
    _tag_item_lifecycle,
    _tag_lifecycle,
    activity_lifecycle,
)


@pytest.fixture(autouse=True)
def _isolated_feedback(tmp_path, monkeypatch):
    """隔离人工判定库：否则本机 real feedback 会串进来改变结论。"""
    monkeypatch.setattr(fs, "FEEDBACK_DIR", tmp_path / "feedback")
    fs._CACHE.clear()
    yield
    fs._CACHE.clear()


# ---------------------------------------------------------------------------
# 构造器
# ---------------------------------------------------------------------------
def ev(label: str, prox: str = "near", quote: str = "原话") -> dict:
    return {
        "source_id": f"src_{label}_{prox}_{quote}",
        "segment_id": "watch_000002_000001",
        "row_id": "1",
        "quote": quote,
        "source_label": label,
        "acoustic_proximity": prox,
        "speaker": "说话人一",
    }


def state(one_item: dict) -> str:
    """子项的三态；**缺字段等价于 live**（前端只把 "media" 折起来）。

    记录这条等价关系而不是强行给每个子项补字段：826 个子项里绝大多数都是
    live，全写一遍只会让产物膨胀，还可能被误当成"系统下了判断"。
    """
    return str(one_item.get("item_lifecycle") or "live")


def item(*evidence, item_id: str = "i1", action: str = "动作", **extra) -> dict:
    base = {"item_id": item_id, "action": action, "evidence": list(evidence)}
    base.update(extra)
    return base


def card(*items, title: str = "现场聊天", **extra) -> dict:
    base = {"activity_id": "act_test", "title": title, "items": list(items)}
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# 1. 用户确认的真实对话（act_150 结构复刻）—— 对方发言不得被折
# ---------------------------------------------------------------------------
def test_confirmed_family_chat_interlocutor_is_not_media():
    """act_150 实测结构：3 个 item，2 个纯 non_self(near)，1 个 self。

    用户原话：「2026-11-02 与妈妈聊天的 act_145–act_151 属于其生活」。
    对方说的那句「那天减肥饿得胃疼所以不减」曾经被折进「视频台词」。
    """
    activity = card(
        item(ev("non_self", "near", "我那天我饿的胃疼，所以我不减。"), item_id="i1",
             action="对方称那天减肥饿得胃疼所以不减"),
        item(ev("non_self", "near", "嗯，我后面都不不减。"),
             ev("non_self", "uncertain", "又开始吃那个了。"), item_id="i2",
             action="对方称后面都不减"),
        item(ev("self", "near", "正常吃，把该吃的吃好就不会饿。"), item_id="i3",
             action="本人建议正常吃"),
        title="现场听对方讲减肥饿到胃疼后不再减",
    )

    assert activity_lifecycle(activity, "2026-11-02") == "live"

    payload = _tag_lifecycle({"date": "2026-11-02", "activities": [activity]})
    tagged = _tag_item_lifecycle(payload)
    out = tagged["activities"][0]
    # 对方（near/uncertain 场）属于这场对话 → 与本人一样是 live 内容，只是 source_label 不同
    assert [state(i) for i in out["items"]] == ["live", "live", "live"]
    # 关键断言：一条都不许进 media，否则前端会折进「非本人发言/视频」
    assert out.get("mixed_media_item_count", 0) == 0
    # 归属没变：对方的话仍是 non_self，不会被冒充成本人
    assert out["items"][0]["evidence"][0]["source_label"] == "non_self"
    # 原话必须原样保留（禁止改写用户证据）
    quotes = [e["quote"] for i in out["items"] for e in i["evidence"]]
    assert "我那天我饿的胃疼，所以我不减。" in quotes


def test_uncertain_proximity_is_not_media():
    """`uncertain` 场比 near 更弱，同样不能判成媒体。"""
    activity = card(
        item(ev("self", "near"), item_id="i1"),
        item(ev("non_self", "uncertain", "对方劝减肥"), item_id="i2"),
    )
    payload = _tag_lifecycle({"date": "d", "activities": [activity]})
    out = _tag_item_lifecycle(payload)["activities"][0]
    assert state(out["items"][1]) != "media"
    assert out.get("mixed_media_item_count", 0) == 0


# ---------------------------------------------------------------------------
# 2. 独立媒体证据（屏幕串音）—— 仍然要折
# ---------------------------------------------------------------------------
def test_far_field_non_self_item_stays_media():
    """non_self 行**全部** far 场 → 才可能是视频/广播串音，仍判 media 并折叠。"""
    activity = card(
        item(ev("self", "near"), item_id="i1", action="我评价这个架构"),
        item(ev("non_self", "far", "那这套架构它算不算创新呢？"), item_id="i2",
             action="视频里在讲架构"),
        item(ev("non_self", "far", "从底层技术方向来看…"), item_id="i3", action="视频继续"),
    )
    payload = _tag_lifecycle({"date": "d", "activities": [activity]})
    out = _tag_item_lifecycle(payload)["activities"][0]
    assert [state(i) for i in out["items"]] == ["live", "media", "media"]
    assert out["mixed_media_item_count"] == 2
    assert out["mixed_media_strong_count"] == 2
    assert out.get("mixed_unknown_item_count", 0) == 0


def test_mixed_far_and_near_is_not_media():
    """同一子项里既有 far 又有 near → 不能宣称是屏幕声 → 不折叠。

    这是「宁可少折」的具体落点：只要有一条 near，就按现场对话处理。
    """
    activity = card(
        item(ev("self", "near"), item_id="i1"),
        item(ev("non_self", "far", "远处有声"), ev("non_self", "near", "近处也有人"),
             item_id="i2"),
    )
    payload = _tag_lifecycle({"date": "d", "activities": [activity]})
    out = _tag_item_lifecycle(payload)["activities"][0]
    assert state(out["items"][1]) != "media"
    assert out.get("mixed_media_item_count", 0) == 0


# ---------------------------------------------------------------------------
# 3. 未知来源 —— 既不认成本人，也不认成媒体
# ---------------------------------------------------------------------------
def test_no_voiceprint_item_is_unknown_not_live():
    """完全没有已归属证据的子项 → unknown（以前会被等于卡片 lifecycle=live）。"""
    activity = card(
        item(ev("self", "near"), item_id="i1"),
        item(item_id="i2"),                       # 无 evidence
        item({"source_id": "x", "quote": "只有 unknown"}, item_id="i3"),
    )
    payload = _tag_lifecycle({"date": "d", "activities": [activity]})
    out = _tag_item_lifecycle(payload)["activities"][0]
    assert [state(i) for i in out["items"]] == ["live", "unknown", "unknown"]


def test_card_without_any_voiceprint_is_unknown_not_live():
    """整张卡没有任何已归属证据 → unknown（新口径第 ④ 条）。

    旧代码在这里 `return "live"`，等于把「不知道」写成「本人生活」。
    标题也刻意选成不含任何「现场/自述/日常」线索的，否则那是文字证据、不算证据不足。
    """
    activity = card(item({"source_id": "s1", "quote": "一句话"}), title="活动 12")
    assert activity_lifecycle(activity, "2026-10-08") == "unknown"


def test_card_without_voiceprint_but_live_hint_stays_live():
    """标题里有明确的现场/自述线索 → live。

    这是**有意保留**的：不然 10-08 那种还没回填 `source_label` 的日子会把
    「自述/日常」这类明显的生活记录也误判成待确认。
    """
    activity = card(item({"source_id": "s1", "quote": "一句话"}), title="自述今天在家记录")
    assert activity_lifecycle(activity, "2026-10-08") == "live"


def test_unknown_only_labels_is_unknown():
    activity = card(item(ev("unknown", "near"), ev("unknown", "far"), item_id="i1"))
    assert activity_lifecycle(activity, "d") == "unknown"


# ---------------------------------------------------------------------------
# 4. 明确媒体结构证据 —— 即使 0 self 也是 media（不能一律 unknown）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("title, target", [
    ("观看B站视频：读懂大模型", "媒体内容"),
    ("视频里讲解营养学", "未知"),
    ("他在节目里说这段历史", "未知"),
])
def test_explicit_media_structure_beats_unknown(title, target):
    activity = {
        "activity_id": "act_x",
        "title": title,
        "speech_target": target,
        "items": [item(ev("non_self", "far"), item_id="i1")],
    }
    assert activity_lifecycle(activity, "d") == "media"


def test_self_evidence_beats_media_keywords():
    """有 self 证据时，标题里出现「视频」也不算媒体（第 ② 条）。"""
    activity = card(
        item(ev("self", "near", "我在拍视频"), item_id="i1"),
        title="讨论拍视频的事",
        category="浏览·视频",
    )
    assert activity_lifecycle(activity, "d") == "live"


# ---------------------------------------------------------------------------
# 5. 用户纠正 > 一切
# ---------------------------------------------------------------------------
def test_user_confirmation_wins_over_unknown():
    from life_logger import feedback_store as fb

    fb.record({"day": "2026-10-08", "activity_id": "act_test", "target": "lifecycle",
               "before": "unknown", "after": "live", "provenance": "user_confirmed",
               "basis": "用户确认属于生活"})
    activity = card(item({"source_id": "s", "quote": "没标签"}), title="活动 12")
    # 没有人工记录时是 unknown（证据不足）
    assert activity_lifecycle(activity, "2026-10-09") == "unknown"
    # 有用户确认后是 live，人工判定压过一切自动规则
    assert activity_lifecycle(activity, "2026-10-08") == "live"


# ---------------------------------------------------------------------------
# 6. 幂等 + 不写盘 + 原话不变
# ---------------------------------------------------------------------------
def test_tag_item_lifecycle_is_idempotent_and_pure():
    activity = card(
        item(ev("self", "near"), item_id="i1"),
        item(ev("non_self", "near"), item_id="i2"),
        item(ev("non_self", "far"), item_id="i3"),
    )
    payload = {"date": "d", "activities": [activity]}
    once = _tag_item_lifecycle(payload)
    twice = _tag_item_lifecycle(once)
    for a in (once["activities"][0], twice["activities"][0]):
        assert [state(i) for i in a["items"]] == ["live", "live", "media"]
    # 输入 payload 未被就地修改
    assert "item_lifecycle" not in activity["items"][0]


def test_tag_lifecycle_no_longer_marks_items_by_self_zero():
    """`_tag_lifecycle` 不能再自己给子项打 media —— 那是被推翻的「0 self ⇒ 媒体」。"""
    activity = card(
        item(ev("self", "near"), item_id="i1"),
        item(ev("non_self", "near"), item_id="i2"),
    )
    out = _tag_lifecycle({"date": "d", "activities": [activity]})["activities"][0]
    assert out["lifecycle"] == "live"
    assert all("item_lifecycle" not in i for i in out["items"])


def test_full_pipeline_keeps_original_quotes_and_fields():
    """投影只**加**字段，不改动原话/时间/金额等真实数据。"""
    activity = {
        "activity_id": "act_keep",
        "title": "现场聊天",
        "occurrence_start": "2026-11-02T19:40:00+08:00",
        "spoken_at": "2026-11-02T19:41:00+08:00",
        "time_source": "spoken_only",
        "items": [
            item(ev("self", "near", "本人原话"), item_id="i1",
                 action="本人说", occurred_at="2026-11-02T19:40:00+08:00", amount=5, currency="CNY"),
            item(ev("non_self", "near", "对方原话"), item_id="i2", action="对方说"),
        ],
    }
    payload = {"date": "2026-11-02", "activities": [activity]}
    before = repr(payload)
    out = _tag_item_lifecycle(payload)
    after_card = out["activities"][0]
    assert after_card["occurrence_start"] == "2026-11-02T19:40:00+08:00"
    assert after_card["spoken_at"] == "2026-11-02T19:41:00+08:00"
    assert after_card["time_source"] == "spoken_only"
    assert after_card["items"][0]["amount"] == 5
    assert [e["quote"] for i in after_card["items"] for e in i["evidence"]] == ["本人原话", "对方原话"]
    # 输入没被就地改动
    assert repr(payload) == before
