"""「当时在对哪个应用说话」：读时关联 + 归档保留 + 空闲门控（2026-11-05）。

覆盖派单要求的六类：
  1. 时区混合（-08:00 与 +08:00）
  2. 晚到转写仍能关联（窗口已归档，latest 已轮转）
  3. 空闲超过 120 秒不关联
  4. mic 窗口优先（dictation 压过 front_app）
  5. 窗口缺失返回空、不报错
  6. 归档有上限（7 天 / 单文件大小）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from life_logger import app_context

SH = timezone(timedelta(hours=8))
PDT = timezone(timedelta(hours=-8))


def _mic(start: datetime, end: datetime, bundle: str | None, *, unknown: bool = False,
         idle: float | None = None, complete: bool = True) -> dict:
    return {
        "started_at": start.isoformat(), "ended_at": end.isoformat(),
        "provider": {"source": "mic_event", "pid": "468"},
        "front_bundles": [bundle] if bundle else [],
        "target_app": None if unknown else bundle,
        "target_unknown": unknown,
        "complete": complete,
        "idle_seconds": idle,
    }


def _front(start: datetime, end: datetime, bundle: str, *, idle: float | None = None,
           complete: bool = True) -> dict:
    return {
        "started_at": start.isoformat(), "ended_at": end.isoformat(),
        "provider": {"source": "front_sample"},
        "front_bundles": [bundle], "target_app": bundle, "target_unknown": False,
        "complete": complete, "idle_seconds": idle,
    }


BASE = datetime(2026, 11, 5, 15, 0, tzinfo=SH)


def test_timezone_mixed_offsets_still_match():
    """-08:00 的 mic 窗口与 +08:00 的时刻必须能对上（同一瞬间）。"""
    mic = _mic(BASE.astimezone(PDT), (BASE + timedelta(seconds=2)).astimezone(PDT), "com.yetone.alma")
    ctx = app_context.context_at(BASE + timedelta(seconds=1), [mic])
    assert ctx is not None
    assert ctx["kind"] == "dictation"
    assert ctx["app_label"] == "Alma"


def test_naive_timestamp_returns_none():
    assert app_context.context_at("2026-11-05T15:00:00", [_mic(BASE, BASE, "x")]) is None


def test_mic_window_wins_over_front_window():
    mic = _mic(BASE, BASE + timedelta(seconds=1), "com.yetone.alma", idle=1.0)
    front = _front(BASE - timedelta(seconds=5), BASE + timedelta(seconds=5), "com.tencent.xinWeChat", idle=1.0)
    ctx = app_context.context_at(BASE, [front, mic])
    assert ctx["kind"] == "dictation"
    assert ctx["app_label"] == "Alma"
    assert "语音输入到" in ctx["meaning"]


def test_front_app_when_no_mic_window():
    front = _front(BASE, BASE + timedelta(seconds=20), "com.tencent.xinWeChat", idle=3.0)
    ctx = app_context.context_at(BASE + timedelta(seconds=5), [front])
    assert ctx["kind"] == "front_app"
    assert ctx["confidence"] == "medium"
    assert ctx["meaning"] == "当时前台：微信"


def test_idle_over_limit_is_not_counted():
    front = _front(BASE, BASE + timedelta(seconds=20), "com.tencent.xinWeChat", idle=600.0)
    assert app_context.context_at(BASE + timedelta(seconds=5), [front]) is None


def test_idle_unknown_gives_low_confidence():
    front = _front(BASE, BASE + timedelta(seconds=20), "com.yetone.alma", idle=None)
    ctx = app_context.context_at(BASE + timedelta(seconds=5), [front])
    assert ctx["kind"] == "front_app"
    assert ctx["confidence"] == "low"
    assert ctx["evidence"] == "idle_unknown"


def test_mic_without_target_uses_nearest_front_sample():
    mic = _mic(BASE, BASE + timedelta(seconds=1), None, unknown=True)
    front = _front(BASE - timedelta(seconds=4), BASE - timedelta(seconds=3), "com.anthropic.claude", idle=0.0)
    ctx = app_context.context_at(BASE, [mic, front])
    assert ctx is not None and ctx["kind"] == "front_app"
    assert ctx["app_label"] == "Claude"


def test_missing_windows_returns_none_and_no_raise():
    assert app_context.context_at(BASE, []) is None
    payload = {"activities": [{"items": [{"spoken_at": BASE.isoformat(), "title": "x"}]}]}
    out = app_context.attach_to_payload(payload, date="2026-11-05", root=Path("/nonexistent-root"))
    assert out["activities"][0]["items"][0].get("input_context") is None
    assert out["activities"][0].get("input_context") is None


def test_late_transcript_still_links_from_archive(tmp_path):
    """latest.json 已轮转掉（只剩空），只有按日归档 → 仍能关联（晚到转写场景）。"""
    root = tmp_path
    days = root / "input-context" / "days"
    days.mkdir(parents=True)
    rows = [
        _mic(BASE, BASE + timedelta(seconds=1.5), "com.yetone.alma"),
        _front(BASE + timedelta(minutes=2), BASE + timedelta(minutes=3),
               "com.tencent.xinWeChat", idle=2.0),
    ]
    with (days / "2026-11-05.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    (root / "input-context" / "latest.json").write_text(json.dumps({"windows": [], "front_windows": []}),
                                                        encoding="utf-8")

    payload = {"activities": [{"items": [
        {"spoken_at": (BASE + timedelta(seconds=1)).isoformat(), "title": "对 Alma 说话"},
        {"spoken_at": (BASE + timedelta(minutes=2, seconds=30)).isoformat(), "title": "对微信说话"},
    ]}]}
    out = app_context.attach_to_payload(payload, date="2026-11-05", root=root)
    items = out["activities"][0]["items"]
    assert items[0]["input_context"]["kind"] == "dictation"
    assert items[1]["input_context"]["app_label"] == "微信"
    assert out["activities"][0]["input_context"]["app_label"] in {"微信", "Alma"}


def test_activity_summary_is_most_frequent_app():
    ctxs = [
        {"app_label": "微信", "kind": "front_app", "confidence": "medium", "meaning": "当时前台：微信"},
        {"app_label": "微信", "kind": "front_app", "confidence": "medium", "meaning": "当时前台：微信"},
        {"app_label": "Alma", "kind": "dictation", "confidence": "high", "meaning": "语音输入到 Alma"},
    ]
    summary = app_context.summarize(ctxs)
    assert summary["app_label"] == "微信"
    assert summary["item_count"] == 2


def test_messages_never_claim_delivery():
    mic = _mic(BASE, BASE + timedelta(seconds=1), "com.yetone.alma", idle=0.0)
    ctx = app_context.context_at(BASE, [mic])
    for banned in ("已发送", "发送给", "对方是", "已送达"):
        assert banned not in ctx["meaning"]


def test_prune_keeps_seven_days(tmp_path):
    days = tmp_path / "days"
    days.mkdir()
    for offset in range(0, 12):
        day = (datetime(2026, 11, 5, tzinfo=SH) - timedelta(days=offset)).strftime("%Y-%m-%d")
        (days / f"{day}.jsonl").write_text("{}\n", encoding="utf-8")
    removed = app_context.prune_day_files(days, keep_days=7, today="2026-11-05")
    kept = sorted(p.stem for p in days.glob("*.jsonl"))
    assert len(kept) == 8, kept          # 0..7 天（含当天）
    assert len(removed) == 4


def test_day_file_size_cap_blocks_appending(tmp_path):
    path = tmp_path / "2026-11-05.jsonl"
    path.write_text("x" * 100, encoding="utf-8")
    assert app_context.can_append(path, 10, max_bytes=120)
    assert not app_context.can_append(path, 50, max_bytes=120)
