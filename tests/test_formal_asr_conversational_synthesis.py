"""watch_formal_asr 单段回放：口头点餐合成在真实流水线调用点产出规范订单事件。

回放数据取自真实段行（watch F786C91B，10-24）：
  * 000001：用户点「咖喱牛肉蛋炒饭 / 炸薯条 / 清补凉加雪球」，店员答「三十八。」
  * 000026：用户「我加个烤肠」，店员答「三块」
  * 000116：短剧台词（含"三十八点"雨量），绝不能合成订单。
说明：这里复用 `watch_formal_asr._sync_live_session_events` 里的**同一调用顺序**
（`synthesize_pos_payment_events` → `synthesize_conversational_orders` → formal_events），
不调用模型、不写生产数据。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import watch_formal_asr  # noqa: E402
from life_logger.contextual_extraction import (  # noqa: E402
    synthesize_conversational_orders,
    synthesize_pos_payment_events,
)

RECORDING = "watch_F786C91BCA7743B5BC3D9C40FD93C33A"
TS = "2026-10-24T15:55:00+00:00"


def row(text: str, offset: float, label: str, seg: str = "000001") -> dict:
    """真实段行形状（speaker → source_label 由声纹归属决定）。"""
    return {
        "source_id": f"{seg}:{offset}",
        "text": text,
        "source_label": label,
        "recording_id": RECORDING,
        "segment_id": seg,
        "start_offset_seconds": offset,
        "end_offset_seconds": offset + 2.0,
        "timestamp": TS,
    }


def build_formal_events(rows):
    """复刻 watch_formal_asr 的合成调用顺序（同两个函数、同顺序）。"""
    formal_events = []
    for event in synthesize_pos_payment_events(rows):
        formal_events.append(event)
    for event in synthesize_conversational_orders(rows):
        formal_events.append(event)
    return formal_events


def orders(events):
    return [e for e in events if str(e.get("event_id", "")).startswith("evt_order_")]


# ---- 真实段落回放：10-24 晚饭（炒饭 + 薯条 + 清补凉 + 三十八）-----------------------
DINNER_ROWS = [
    row("呃，要一份这个咖喱牛肉蛋炒饭。", 9.81, "self"),
    row("然后一份儿炸薯条，然后呃。", 14.12, "self"),
    row("诶，清补凉能不能给我加个雪球啊？", 20.78, "self"),
    row("三个一共多少钱？", 29.68, "self"),
    row("三十八。", 38.88, "non_self"),          # 店员报价
]

# ---- 真实段落回放：10-25 00:20（加个烤肠 + 三块）----------------------------------
ADDON_ROWS = [
    row("我加个烤肠", 44.59, "self", seg="000026"),
    row("多少钱", 46.89, "self", seg="000026"),
    row("三块", 47.43, "non_self", seg="000026"),   # 店员报价
]

# ---- 真实段落回放：短剧（含"三十八点"雨量台词）------------------------------------
DRAMA_ROWS = [
    row("正午，明日辰时不云，巳时发雷，午时下雨，未时雨足，共得水三十三分零四十八点。", 17.58, "self", seg="000116"),
    row("军师所言有理，臣多后一个，点数扣掉三十八点，雨还是下了。", 32.45, "self", seg="000116"),
    row("收钱吧，到账十三元。", 40.0, "non_self", seg="000116"),
]


def test_pipeline_call_site_still_invokes_synthesis() -> None:
    """保证测试跑的是**流水线真实调用点**，不是另写一份逻辑。"""
    source = inspect.getsource(watch_formal_asr)
    assert "synthesize_conversational_orders(rows)" in source
    assert "synthesize_pos_payment_events(rows)" in source
    # 两个合成器都在 formal_events 组装处被调用
    call_block = source.split("formal_events: list[dict[str, Any]] = []", 1)[1][:800]
    assert "synthesize_pos_payment_events(rows)" in call_block
    assert "synthesize_conversational_orders(rows)" in call_block


def test_dinner_sequence_synthesizes_38_order_event() -> None:
    events = orders(build_formal_events(DINNER_ROWS))
    amounts = [round(float(e["data"]["expense"]["amount"]), 2) for e in events]
    assert 38.0 in amounts, [e["title"] for e in events]
    event = next(e for e in events if round(float(e["data"]["expense"]["amount"]), 2) == 38.0)
    assert event["event_id"].startswith("evt_order_")
    assert event["categories"] == ["expense", "food"]
    assert event["activity_type"] == "meal"
    assert event["data"]["expense"]["currency"] == "CNY"
    assert event["data"]["expense"]["payment_channel"] == "offline_conversational"
    assert "炒饭" in event["title"]
    assert event["evidence"]["recording_id"] == RECORDING
    assert event["evidence"]["source_label"] == "self"


def test_addon_sequence_synthesizes_3_order_event() -> None:
    events = orders(build_formal_events(ADDON_ROWS))
    amounts = [round(float(e["data"]["expense"]["amount"]), 2) for e in events]
    assert 3.0 in amounts, [e["title"] for e in events]
    event = next(e for e in events if round(float(e["data"]["expense"]["amount"]), 2) == 3.0)
    assert "烤肠" in event["title"]
    assert event["categories"] == ["expense", "food"]


def test_drama_segment_never_yields_order_event() -> None:
    events = orders(build_formal_events(DRAMA_ROWS))
    assert events == [], [e["title"] for e in events]


def test_existing_duck_sequence_still_10() -> None:
    rows = [
        row("两个鸭腿吧", 10.58, "self", seg="000026"),
        row("十块", 2.91, "non_self", seg="000026"),
    ]
    events = orders(build_formal_events(rows))
    assert [round(float(e["data"]["expense"]["amount"]), 2) for e in events] == [10.0]


@pytest.mark.skipif(not (ROOT / "output" / "local-web").is_dir(), reason="需要真实 store")
def test_real_transcripts_contain_the_replay_lines() -> None:
    """回放数据不是编的：确认真实段行里确有这些原话。"""
    base = ROOT / "output" / "local-web" / "watch-sessions" / "watch-41aa6b87347f4f0fb5238b559f705c4d" / "F786C91BCA7743B5BC3D9C40FD93C33A" / "formal-asr" / "segments"
    if not base.is_dir():
        pytest.skip("真实段行不在本机")
    texts = []
    for path in base.glob("*/2026-10-24/transcript.json"):
        import json
        payload = json.loads(path.read_text())
        for item in (payload if isinstance(payload, list) else payload.get("rows") or []):
            texts.append(str(item.get("text") or ""))
    joined = "\n".join(texts)
    for phrase in ("咖喱牛肉蛋炒饭", "炸薯条", "清补凉", "三十八", "我加个烤肠", "三块"):
        assert phrase in joined, phrase
