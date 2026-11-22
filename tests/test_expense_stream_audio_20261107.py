"""视频/直播音轨里的钱不进消费账（2026-11-07）。

真实事故：2026-11-06 18:35:22「他一百块钱，」被记成 100 元（evt_6daa11c44387）。
前后 120 秒每一行都是 `audio_content_class=stream`（新闻/评论视频），说话人标签却是 self。
用户经常一边放视频一边说话，所以默认档 X 只在**第三人称 / 无第一人称 + 无支付动作**时才判定；
激进档 Y 只按 `audio_content_class` 判，用来量化误杀。
"""
from __future__ import annotations

import pytest

from life_logger import expense_policy as ep


@pytest.fixture(autouse=True)
def _restore_policy():
    original = ep.MEDIA_AUDIO_EXPENSE_POLICY
    yield
    ep.MEDIA_AUDIO_EXPENSE_POLICY = original


def _event(quote, *, amount=100.0, content_class="stream", title="消费",
           event_id="evt_stream_1", label="self"):
    evidence = {"quote": quote, "source_label": label}
    if content_class is not None:
        evidence["audio_content_class"] = content_class
    return {
        "event_id": event_id,
        "title": title,
        "activity_type": "purchase",
        "categories": ["expense"],
        "data": {"expense": {"amount": amount, "currency": "CNY", "item": title}},
        "evidence": evidence,
    }


def _ledger(events):
    return ep.build_expense_ledger(events)


def _pending_reasons(ledger):
    return {t.get("reason") for t in ledger.get("pending_transactions") or []}


# --- 1. 真实事故：X / Y 都必须拿掉 -------------------------------------------------

@pytest.mark.parametrize("policy", ["x", "y"])
def test_real_1106_third_person_stream_is_not_counted(policy):
    ep.MEDIA_AUDIO_EXPENSE_POLICY = policy
    ledger = _ledger([_event("他一百块钱，", event_id="evt_6daa11c44387", title="他一百块钱")])

    assert ledger["total_amount"] == 0.0, ledger["transactions"]
    assert not ledger["transactions"], ledger["transactions"]
    assert "evt_6daa11c44387" not in {t.get("event_id") for t in ledger["transactions"]}
    pending = {t.get("event_id"): t for t in ledger.get("pending_transactions") or []}
    assert "evt_6daa11c44387" in pending, ledger.get("pending_transactions")
    assert pending["evt_6daa11c44387"]["amount"] == 100.0
    if policy == "x":
        assert pending["evt_6daa11c44387"]["reason"] == ep.PENDING_MEDIA_THIRD_PERSON
    else:
        assert pending["evt_6daa11c44387"]["reason"] == ep.PENDING_MEDIA_STREAM


# --- 2. 本人第一人称 + 支付动作：X 下仍计入 ---------------------------------------

def test_first_person_with_payment_verb_is_still_counted_under_x():
    ep.MEDIA_AUDIO_EXPENSE_POLICY = "x"
    ledger = _ledger([_event("我刚花了二十块买烟", amount=20.0, event_id="evt_self_pay")])

    assert ledger["total_amount"] == 20.0, ledger["transactions"]
    assert [t.get("event_id") for t in ledger["transactions"]] == ["evt_self_pay"]


def test_third_person_but_explicit_payment_verb_is_counted_under_x():
    """「我给他付了一百块」= 本人真的付了，不能因为出现"他"就拦。"""
    ep.MEDIA_AUDIO_EXPENSE_POLICY = "x"
    ledger = _ledger([_event("我给他付了一百块", amount=100.0, event_id="evt_paid_for_him")])
    assert ledger["total_amount"] == 100.0, ledger["transactions"]


def test_y_policy_overkills_the_same_first_person_sentence():
    """对比档 Y 的代价：同一句「我刚花了二十块买烟」在 Y 下会被拿掉（记录用，不是期望行为）。"""
    ep.MEDIA_AUDIO_EXPENSE_POLICY = "y"
    ledger = _ledger([_event("我刚花了二十块买烟", amount=20.0, event_id="evt_self_pay")])
    assert ledger["total_amount"] == 0.0
    assert _pending_reasons(ledger) == {ep.PENDING_MEDIA_STREAM}


# --- 3. speech 类 / 无类别 不受影响 ------------------------------------------------

@pytest.mark.parametrize("policy", ["x", "y"])
def test_speech_audio_class_is_never_touched(policy):
    ep.MEDIA_AUDIO_EXPENSE_POLICY = policy
    ledger = _ledger([_event("他一百块钱，", content_class="speech", event_id="evt_speech")])
    assert ledger["total_amount"] == 100.0, ledger["transactions"]


@pytest.mark.parametrize("policy", ["x", "y"])
def test_missing_audio_class_is_not_suppressed(policy):
    """拿不到类别 = 未知 → 不压制（未知不改变行为）。"""
    ep.MEDIA_AUDIO_EXPENSE_POLICY = policy
    ledger = _ledger([_event("他一百块钱，", content_class=None, event_id="evt_no_class")])
    assert ledger["total_amount"] == 100.0, ledger["transactions"]


def test_music_class_also_counts_as_media():
    ep.MEDIA_AUDIO_EXPENSE_POLICY = "x"
    ledger = _ledger([_event("他一百块钱，", content_class="music", event_id="evt_music")])
    assert ledger["total_amount"] == 0.0
    assert ep.PENDING_MEDIA_THIRD_PERSON in _pending_reasons(ledger)


# --- 4. 开关 "off" = 旧行为，逐位一致 ----------------------------------------------

def test_off_switch_keeps_old_behaviour():
    ep.MEDIA_AUDIO_EXPENSE_POLICY = "off"
    ledger = _ledger([_event("他一百块钱，", event_id="evt_off")])
    assert ledger["total_amount"] == 100.0, ledger["transactions"]
    assert _pending_reasons(ledger) == set()


# --- 5. 判定函数本身 --------------------------------------------------------------

@pytest.mark.parametrize("quote,content_class,expected", [
    ("他一百块钱，", "stream", ep.PENDING_MEDIA_THIRD_PERSON),
    ("她说三百块", "stream", ep.PENDING_MEDIA_THIRD_PERSON),
    ("一百块钱", "stream", ep.PENDING_MEDIA_THIRD_PERSON),      # 无第一人称
    ("我刚花了二十块买烟", "stream", None),                      # 第一人称 + 支付动词
    ("他一百块钱，", "speech", None),
    ("他一百块钱，", None, None),
])
def test_media_audio_pending_reason_matrix(quote, content_class, expected):
    ep.MEDIA_AUDIO_EXPENSE_POLICY = "x"
    event = _event(quote, content_class=content_class)
    assert ep.media_audio_pending_reason(event) == expected
