from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

from life_logger import asr_resident as ar


def _write_dummy_wav(path: Path, duration_seconds: float = 60.0, rate: int = 16000) -> None:
    frames = int(duration_seconds * rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * frames)


def test_collapse_payload_repetition_truncates_100_plus_single_char_segments() -> None:
    """复现 2026-11-07 03:57 真实故障：首句后连续 109 段 `[S02] 我`。"""
    raw_text = "[0.24][S01]你已经踏上了黄沙的征程[2.82]" + "".join(
        f"[{5.14 + i * 0.5:.2f}][S02]我[{5.64 + i * 0.5:.2f}]" for i in range(109)
    )
    segments = [
        {"start": 0.24, "end": 2.82, "text": "[S01] 你已经踏上了黄沙的征程", "speaker_id": "S01"},
        *[
            {
                "start": round(5.14 + i * 0.5, 2),
                "end": round(5.64 + i * 0.5, 2),
                "text": "[S02] 我",
                "speaker_id": "S02",
            }
            for i in range(109)
        ],
    ]
    cleaned, warnings = ar.collapse_payload_repetition({"text": raw_text, "segments": segments})
    assert len(cleaned["segments"]) == 1 + ar.DEFAULT_MAX_REPEAT
    assert cleaned["segments"][0]["start"] == 0.24
    assert cleaned["segments"][-1]["end"] == round(5.64 + (ar.DEFAULT_MAX_REPEAT - 1) * 0.5, 2)
    assert any(w.startswith("segment_repetition_truncated:我x109") for w in warnings)
    assert any(w.startswith("repetition_truncated:我x109") for w in warnings)


def test_collapse_payload_repetition_drops_empty_timestamp_loop() -> None:
    """复现 MOSS 只吐时间戳与 [S01] 不吐正文的死循环。"""
    segments = [
        {"start": 0.0, "end": 60.0, "text": "[0.12][S01][0.42][0.44][S01][0.74][0.76][S01][1.06]"},
        {"start": 17.02, "end": 17.84, "text": "[S01] 如果你跑步。"},
    ]
    cleaned, warnings = ar.collapse_payload_repetition({"text": "", "segments": segments})
    assert len(cleaned["segments"]) == 1
    assert cleaned["segments"][0]["text"] == "[S01] 如果你跑步。"
    assert "empty_timestamp_segments_dropped:x1" in warnings


def test_default_transcribe_retranscribes_in_20s_slices_and_preserves_timestamps(tmp_path: Path) -> None:
    """当 60s 整段触发复读时，自动切 20s 短片重转写并把偏移加回 start/end。"""
    wav = tmp_path / "seg18.wav"
    _write_dummy_wav(wav, duration_seconds=60.0)

    class _FakeOutput:
        def __init__(self, text: str, segments: list[dict[str, Any]]) -> None:
            self.text = text
            self.segments = segments

    class _FakeMoss:
        def __init__(self) -> None:
            self.calls: list[tuple[str, float | None]] = []

        def generate(self, audio_path: str, max_tokens: int = 4096, prompt: str | None = None, **kwargs: Any) -> _FakeOutput:
            self.calls.append((Path(audio_path).name, kwargs.get("repetition_penalty")))
            if Path(audio_path).name == "seg18.wav":
                return _FakeOutput(
                    text="[0.24][S01]你已经踏上了黄沙的征程[2.82]"
                    + "".join(f"[{5.14 + i * 0.5:.2f}][S02]我[{5.64 + i * 0.5:.2f}]" for i in range(109)),
                    segments=[
                        {"start": 0.24, "end": 2.82, "text": "[S01] 你已经踏上了黄沙的征程", "speaker_id": "S01"},
                        *[
                            {"start": 5.14 + i * 0.5, "end": 5.64 + i * 0.5, "text": "[S02] 我", "speaker_id": "S02"}
                            for i in range(109)
                        ],
                    ],
                )
            if "slice-000" in audio_path:
                return _FakeOutput(
                    text="[0.25][S01]请看我的床上。[1.84]",
                    segments=[{"start": 0.25, "end": 1.84, "text": "[S01] 请看我的床上。", "speaker_id": "S01"}],
                )
            if "slice-001" in audio_path:
                return _FakeOutput(
                    text="[8.34][S02]嗯。[9.34]",
                    segments=[{"start": 8.34, "end": 9.34, "text": "[S02] 嗯。", "speaker_id": "S02"}],
                )
            return _FakeOutput(
                text="[17.02][S01]如果你跑步。[17.84]",
                segments=[{"start": 17.02, "end": 17.84, "text": "[S01] 如果你跑步。", "speaker_id": "S01"}],
            )

    fake_moss = _FakeMoss()
    out = ar._default_transcribe(wav=wav, outdir=tmp_path, hotwords="OpenClaw", moss=fake_moss, campplus=None)
    assert "repetition_retranscribed_20s" in out["warnings"]
    assert [(s["start"], s["end"], s["text"]) for s in out["segments"]] == [
        (0.25, 1.84, "[S01] 请看我的床上。"),
        (28.34, 29.34, "[S02] 嗯。"),
        (57.02, 57.84, "[S01] 如果你跑步。"),
    ]


def test_collapse_payload_repetition_truncates_multi_sentence_cycle_1108_seg37() -> None:
    """多句循环复读（2~6 句循环重复 ≥3 轮）保留首轮；若命中提示词泄漏正则则直接整条丢弃。"""
    cycle_lines = [
        "[S01] 今天早上先去楼下便利店买瓶水。",
        "[S01] 然后回来把桌面上的线整理一下。",
        "[S01] 接着打开电脑看一下昨天的日志。",
        "[S01] 最后确认一下下午三点的会议安排。",
    ]
    segments = [
        {"start": 0.0, "end": 1.0, "text": "[S01] 好的，我听到了。"},
        *[
            {"start": float(i), "end": float(i + 1), "text": cycle_lines[i % 4]}
            for i in range(4 * 35)
        ],
    ]
    cleaned, warnings = ar.collapse_payload_repetition({"text": "", "segments": segments})
    assert len(cleaned["segments"]) == 1 + 4
    assert any(w.startswith("multi_segment_cycle_truncated:") for w in warnings)

    leak_lines = [
        "[S01] 比如，它是否支持全频段传输？",
        "[S01] 以及它的响应速度如何调整以提高准确度。",
        "[S01] 然后我们再进行一个简单的测试。",
        "[S01] 首先，我们要确定这个设备的性能和精度。",
    ]
    leak_segments = [
        {"start": 0.0, "end": 1.0, "text": "[S01] 好的，我听到了。"},
        *[
            {"start": float(i), "end": float(i + 1), "text": leak_lines[i % 4]}
            for i in range(4 * 35)
        ],
    ]
    leak_cleaned, leak_warnings = ar.collapse_payload_repetition({"text": "", "segments": leak_segments})
    assert len(leak_cleaned["segments"]) == 1
    assert any(w.startswith("prompt_leak_hallucination_dropped:") for w in leak_warnings)


def test_user_corrected_activity_title_is_not_hedged() -> None:
    """用户人工订正后的活动标题（user_corrected=True）不得被二次加上『录音中疑似：』。"""
    from life_logger.title_hedge import hedge_activity

    act = {
        "activity_id": "act_cola_purchase_1108_L038",
        "title": "想买冰杯嫌远放弃，就近买一瓶可乐并支付",
        "title_source": "user_correction",
        "user_corrected": True,
        "items": [
            {
                "action": "就近购买一瓶可乐并操作支付（1.8元）",
                "identity_safe": {"identity": "unknown"},
            }
        ],
    }
    res = hedge_activity(act)
    assert res["title"] == "想买冰杯嫌远放弃，就近买一瓶可乐并支付"
    assert not res.get("title_hedged")

