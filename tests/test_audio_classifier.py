import numpy as np

from life_logger.audio_classifier import (
    annotate_records_with_audio_classification,
    classify_audio_window,
)

SAMPLE_RATE = 16_000


def _signals():
    t = np.arange(SAMPLE_RATE * 10, dtype=np.float32) / SAMPLE_RATE
    music = (
        0.25 * np.sin(2 * np.pi * 440 * t)
        + 0.18 * np.sin(2 * np.pi * 660 * t)
        + 0.12 * np.sin(2 * np.pi * 880 * t)
    ).astype(np.float32)
    envelope = (
        ((t % 0.55) < 0.24).astype(np.float32)
        * (0.55 + 0.45 * np.sin(2 * np.pi * 1.3 * t) ** 2)
    )
    rng = np.random.default_rng(7)
    speech = (
        (
            0.28
            * np.sin(
                2
                * np.pi
                * (170 + 25 * np.sin(2 * np.pi * 2.2 * t))
                * t
            )
            + 0.08 * rng.normal(size=t.size)
        )
        * envelope
    ).astype(np.float32)
    noise = np.random.default_rng(11).normal(0, 0.18, t.size).astype(np.float32)
    stream = (0.55 * music + 0.9 * speech).astype(np.float32)
    return speech, music, noise, stream


def test_audio_classifier_distinguishes_speech_music_noise_and_stream():
    speech, music, noise, stream = _signals()

    speech_result = classify_audio_window(speech)
    music_result = classify_audio_window(music)
    noise_result = classify_audio_window(noise)
    stream_result = classify_audio_window(stream)

    assert speech_result["primary_class"] == "speech"
    assert speech_result["processing_priority"] == "high"
    assert music_result["primary_class"] == "music"
    assert music_result["processing_priority"] == "low"
    assert noise_result["primary_class"] == "noise"
    assert noise_result["processing_priority"] == "low"
    assert stream_result["primary_class"] == "stream"
    assert stream_result["processing_priority"] == "low"
    assert speech_result["priority_weight"] > music_result["priority_weight"]
    assert speech_result["priority_weight"] > stream_result["priority_weight"]


def test_audio_classifier_marks_silence_as_low_priority_noise():
    silence = np.zeros(SAMPLE_RATE * 10, dtype=np.float32)

    result = classify_audio_window(silence)

    assert result["primary_class"] == "noise"
    assert result["probabilities"]["noise"] >= 0.9
    assert result["processing_priority"] == "low"


def test_annotation_reuses_ten_second_windows_and_keeps_records():
    speech, music, _noise, _stream = _signals()
    waveform = np.concatenate([music, speech])
    records = [
        {
            "segment_id": "music-a",
            "start_offset_seconds": 1.0,
            "end_offset_seconds": 2.0,
            "source_label": "non_self",
        },
        {
            "segment_id": "music-b",
            "start_offset_seconds": 7.0,
            "end_offset_seconds": 8.0,
            "source_label": "unknown",
        },
        {
            "segment_id": "speech-a",
            "start_offset_seconds": 12.0,
            "end_offset_seconds": 13.0,
            "source_label": "self",
        },
    ]

    annotated, windows = annotate_records_with_audio_classification(
        records,
        waveform,
        window_start_seconds=0.0,
        sample_rate=SAMPLE_RATE,
    )

    assert len(annotated) == 3
    assert len(windows) == 2
    assert annotated[0]["audio_classifier_window_index"] == 0
    assert annotated[1]["audio_classifier_window_index"] == 0
    assert annotated[2]["audio_classifier_window_index"] == 1
    assert annotated[0]["audio_content_class"] == "music"
    assert annotated[2]["audio_content_class"] == "speech"
    assert annotated[0]["audio_processing_priority"] == "low"
    assert annotated[2]["audio_processing_priority"] == "high"
    assert annotated[0]["source_label"] == "non_self"
    assert annotated[1]["source_label"] == "unknown"
    assert annotated[2]["source_label"] == "self"
