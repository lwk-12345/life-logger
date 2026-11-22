"""Lightweight 10-second audio-content classification and priority weighting."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite
from typing import Any, Mapping, Sequence

AUDIO_CLASSIFIER_POLICY_VERSION = "audio-content-v1"
AUDIO_CLASSES = ("speech", "music", "noise", "stream")
# 每条记录上由本模块写入的音频分类字段（单一事实来源）。
# 上游流水线（MOSS chunk / Whisper VAD 窗 / 实时切片）复用这些字段名把分类结果
# 带进 transcript.json，attribute 阶段再按同样的字段名重建 audio-classification.json。
AUDIO_CLASSIFIER_RECORD_FIELDS = (
    "audio_classifier_policy",
    "audio_content_class",
    "audio_content_scores",
    "audio_classifier_features",
    "audio_priority_weight",
    "audio_processing_priority",
    "audio_classifier_window_index",
    "audio_classifier_window_start_seconds",
    "audio_classifier_window_end_seconds",
)


@dataclass(frozen=True)
class AudioClassifierConfig:
    window_seconds: float = 10.0
    high_priority_threshold: float = 0.62
    medium_priority_threshold: float = 0.44

    def __post_init__(self) -> None:
        if not isfinite(float(self.window_seconds)) or self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if not 0.0 <= self.medium_priority_threshold <= 1.0:
            raise ValueError("medium_priority_threshold must be between 0 and 1")
        if not 0.0 <= self.high_priority_threshold <= 1.0:
            raise ValueError("high_priority_threshold must be between 0 and 1")
        if self.medium_priority_threshold > self.high_priority_threshold:
            raise ValueError("medium priority threshold must not exceed high threshold")


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _normalize(scores: Mapping[str, float]) -> dict[str, float]:
    cleaned = {key: max(0.0, float(scores.get(key, 0.0))) for key in AUDIO_CLASSES}
    total = sum(cleaned.values())
    if total <= 0.0:
        return {"speech": 0.0, "music": 0.0, "noise": 1.0, "stream": 0.0}
    return {key: cleaned[key] / total for key in AUDIO_CLASSES}


def _frame_features(
    samples: Any,
    sample_rate: int,
) -> tuple[float, float, float, float, float]:
    """Return rms, envelope-CV, ZCR, spectral-flatness and tonality."""

    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover - runtime dependency of ASR stack
        raise RuntimeError("audio classifier requires numpy") from error

    waveform = np.asarray(samples, dtype=np.float32)
    if waveform.ndim > 1:
        waveform = waveform.mean(axis=1)
    waveform = waveform.reshape(-1)
    if waveform.size == 0:
        return 0.0, 0.0, 0.0, 1.0, 0.0
    waveform = np.nan_to_num(waveform, nan=0.0, posinf=0.0, neginf=0.0)

    rms = float(np.sqrt(np.mean(np.square(waveform), dtype=np.float64)))
    frame_length = max(64, int(round(sample_rate * 0.025)))
    hop = max(32, int(round(sample_rate * 0.010)))
    if waveform.size < frame_length:
        padded = np.zeros(frame_length, dtype=np.float32)
        padded[: waveform.size] = waveform
        waveform = padded

    starts = np.arange(0, waveform.size - frame_length + 1, hop)
    if starts.size > 600:
        starts = starts[np.linspace(0, starts.size - 1, 600, dtype=int)]
    frames = np.stack([waveform[start : start + frame_length] for start in starts])
    frame_rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-12)
    envelope_cv = float(np.std(frame_rms) / max(float(np.mean(frame_rms)), 1e-6))

    signs = frames >= 0
    zcr = float(np.mean(signs[:, 1:] != signs[:, :-1]))

    spectral_frame = max(256, min(2048, int(round(sample_rate * 0.064))))
    spectral_hop = spectral_frame
    if waveform.size < spectral_frame:
        padded = np.zeros(spectral_frame, dtype=np.float32)
        padded[: waveform.size] = waveform
        waveform_for_fft = padded
    else:
        waveform_for_fft = waveform
    spectral_starts = np.arange(
        0, waveform_for_fft.size - spectral_frame + 1, spectral_hop
    )
    if spectral_starts.size > 80:
        spectral_starts = spectral_starts[
            np.linspace(0, spectral_starts.size - 1, 80, dtype=int)
        ]
    window = np.hanning(spectral_frame).astype(np.float32)
    flatness_values: list[float] = []
    tonality_values: list[float] = []
    for start in spectral_starts:
        frame = waveform_for_fft[start : start + spectral_frame] * window
        power = np.abs(np.fft.rfft(frame)) ** 2 + 1e-12
        mean_power = float(np.mean(power))
        flatness_values.append(
            float(np.exp(np.mean(np.log(power))) / max(mean_power, 1e-12))
        )
        sorted_power = np.sort(power)
        top_bins = sorted_power[-max(1, min(8, sorted_power.size)) :]
        tonality_values.append(
            float(np.sum(top_bins) / max(float(np.sum(power)), 1e-12))
        )

    spectral_flatness = (
        float(sum(flatness_values) / len(flatness_values))
        if flatness_values
        else 1.0
    )
    tonality = (
        float(sum(tonality_values) / len(tonality_values))
        if tonality_values
        else 0.0
    )
    return rms, envelope_cv, zcr, spectral_flatness, tonality


def classify_audio_window(
    samples: Any,
    *,
    sample_rate: int = 16_000,
    config: AudioClassifierConfig | None = None,
) -> dict[str, Any]:
    """Classify one audio window into speech/music/noise/stream probabilities.

    This is deliberately lightweight and local.  It estimates content from
    temporal dynamics plus spectral flatness/tonality.  The four scores are
    priority hints, not deletion decisions.
    """

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    config = config or AudioClassifierConfig()
    rms, envelope_cv, zcr, flatness, tonality = _frame_features(samples, sample_rate)

    if rms < 0.0025:
        probabilities = {
            "speech": 0.03,
            "music": 0.01,
            "noise": 0.95,
            "stream": 0.01,
        }
    else:
        dynamic_support = _clip01((envelope_cv - 0.18) / 0.80)
        speech_zcr = 1.0 - _clip01(abs(zcr - 0.09) / 0.16)
        speech_evidence = (
            0.62 * dynamic_support
            + 0.23 * speech_zcr
            + 0.15 * _clip01((0.55 - tonality) / 0.45)
        )

        tonal_support = _clip01((tonality - 0.18) / 0.62)
        low_flatness = _clip01((0.32 - flatness) / 0.30)
        steady_support = 1.0 - _clip01((envelope_cv - 0.22) / 0.95)
        music_evidence = (
            0.50 * tonal_support
            + 0.30 * low_flatness
            + 0.20 * steady_support
        )

        noise_flatness = _clip01((flatness - 0.28) / 0.50)
        noisy_zcr = _clip01((zcr - 0.14) / 0.26)
        noise_evidence = 0.78 * noise_flatness + 0.22 * noisy_zcr

        interaction = speech_evidence * music_evidence
        stream_evidence = _clip01((interaction - 0.07) / 0.24)
        if stream_evidence > 0:
            speech_evidence *= 1.0 - 0.50 * stream_evidence
            music_evidence *= 1.0 - 0.50 * stream_evidence

        probabilities = _normalize(
            {
                "speech": speech_evidence + 0.03,
                "music": music_evidence + 0.03,
                "noise": noise_evidence + 0.02,
                "stream": stream_evidence + 0.02,
            }
        )

    primary = max(AUDIO_CLASSES, key=lambda label: probabilities[label])
    speech = probabilities["speech"]
    music = probabilities["music"]
    stream = probabilities["stream"]
    noise = probabilities["noise"]
    priority_weight = _clip01(
        speech
        + 0.34 * stream
        + 0.18 * music
        + 0.12 * noise
    )
    if priority_weight >= config.high_priority_threshold:
        priority = "high"
    elif priority_weight >= config.medium_priority_threshold:
        priority = "medium"
    else:
        priority = "low"

    return {
        "policy_version": AUDIO_CLASSIFIER_POLICY_VERSION,
        "primary_class": primary,
        "probabilities": {
            label: round(probabilities[label], 6) for label in AUDIO_CLASSES
        },
        "priority_weight": round(priority_weight, 6),
        "processing_priority": priority,
        "features": {
            "rms": round(rms, 6),
            "envelope_cv": round(envelope_cv, 6),
            "zero_crossing_rate": round(zcr, 6),
            "spectral_flatness": round(flatness, 6),
            "tonality": round(tonality, 6),
        },
    }


def _apply_classification(
    record: dict[str, Any],
    classification: Mapping[str, Any],
) -> None:
    """写入一个 10 秒窗的分类结果（字段名唯一来源 AUDIO_CLASSIFIER_RECORD_FIELDS）。"""

    record.update(
        audio_classifier_policy=classification["policy_version"],
        audio_content_class=classification["primary_class"],
        audio_content_scores=dict(classification["probabilities"]),
        audio_classifier_features=dict(classification["features"]),
        audio_priority_weight=classification["priority_weight"],
        audio_processing_priority=classification["processing_priority"],
        audio_classifier_window_index=classification["window_index"],
        audio_classifier_window_start_seconds=classification["window_start_seconds"],
        audio_classifier_window_end_seconds=classification["window_end_seconds"],
    )


def annotate_records_with_audio_classification(
    records: Sequence[Mapping[str, Any]],
    waveform: Any,
    *,
    window_start_seconds: float,
    sample_rate: int = 16_000,
    config: AudioClassifierConfig | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Attach one 10-second content classification to each transcript record."""

    config = config or AudioClassifierConfig()
    output = [dict(record) for record in records]
    if not output:
        return output, []

    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("audio classifier requires numpy") from error
    samples = np.asarray(waveform, dtype=np.float32).reshape(-1)
    window_samples = max(1, int(round(config.window_seconds * sample_rate)))
    classifications: dict[int, dict[str, Any]] = {}

    for record in output:
        midpoint = (
            float(record.get("start_offset_seconds", 0.0))
            + float(record.get("end_offset_seconds", 0.0))
        ) / 2.0
        relative_midpoint = max(0.0, midpoint - window_start_seconds)
        local_index = int(relative_midpoint // config.window_seconds)
        if local_index not in classifications:
            start_index = local_index * window_samples
            end_index = min(samples.size, start_index + window_samples)
            chunk = samples[start_index:end_index]
            classification = classify_audio_window(
                chunk,
                sample_rate=sample_rate,
                config=config,
            )
            absolute_window_start = (
                window_start_seconds + local_index * config.window_seconds
            )
            classifications[local_index] = {
                **classification,
                "window_index": int(
                    absolute_window_start // config.window_seconds
                ),
                "window_start_seconds": round(absolute_window_start, 3),
                "window_end_seconds": round(
                    absolute_window_start + config.window_seconds, 3
                ),
            }

        classification = classifications[local_index]
        _apply_classification(record, classification)

    windows = [classifications[index] for index in sorted(classifications)]
    return output, windows


def annotate_records_with_window_classification(
    records: Sequence[Mapping[str, Any]],
    signal: Any,
    *,
    spans: Sequence[tuple[float, float]] | None = None,
    signal_start_seconds: float = 0.0,
    sample_rate: int = 16_000,
    config: AudioClassifierConfig | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Classify records against a *longer* decoded signal (upstream by-product path).

    ``annotate_records_with_audio_classification`` needs the caller to hand it one
    window of audio.  Upstream pipelines (MOSS chunks, Whisper VAD windows) hold a
    longer decoded signal in memory instead: this variant slices the 10-second
    window that contains each record's midpoint out of that signal, so a single
    decode serves both voiceprint scoring and audio-content classification.

    ``spans`` (optional) is the ``(start_seconds, end_seconds)`` pair for every
    record on the same timeline as ``signal_start_seconds``; it is required for
    records that do not carry ``start_offset_seconds`` / ``end_offset_seconds``
    themselves.  ``signal_start_seconds`` is where ``signal`` begins on that
    timeline.  Records whose window lies outside ``signal`` are left untouched
    (no fields written), so callers never get a classification built from
    audio they do not have.
    """

    config = config or AudioClassifierConfig()
    output = [dict(record) for record in records]
    if not output or signal is None:
        return output, []
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover - runtime dependency of ASR stack
        raise RuntimeError("audio classifier requires numpy") from error

    samples = np.asarray(signal, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        return output, []

    if spans is not None and len(spans) != len(output):
        raise ValueError("spans must align one-to-one with records")

    resolved_spans: list[tuple[float, float] | None] = []
    for position, record in enumerate(output):
        if spans is not None:
            start_seconds, end_seconds = spans[position]
        else:
            start_seconds = record.get("start_offset_seconds")
            end_seconds = record.get("end_offset_seconds")
            if start_seconds is None or end_seconds is None:
                resolved_spans.append(None)
                continue
        resolved_spans.append((float(start_seconds), float(end_seconds)))

    # 只对「信号里真实存在的那一段窗」做判定：交集短于 0.2 秒就不写字段
    # （与声纹侧 VOICEPRINT_MIN_SAMPLES 同一口径），避免用 1 个采样点凑出一个
    # 假窗口混进看板的场景统计。
    minimum_samples = max(1, int(round(0.2 * sample_rate)))
    classifications: dict[int, dict[str, Any]] = {}
    for position, span in enumerate(resolved_spans):
        if span is None:
            continue
        midpoint = (span[0] + span[1]) / 2.0
        window_index = int(floor(midpoint / config.window_seconds))
        if window_index not in classifications:
            window_start_seconds = window_index * config.window_seconds
            window_end_seconds = window_start_seconds + config.window_seconds
            start_index = int(
                round((window_start_seconds - signal_start_seconds) * sample_rate)
            )
            end_index = int(
                round((window_end_seconds - signal_start_seconds) * sample_rate)
            )
            start_index = max(0, min(start_index, samples.size))
            end_index = max(0, min(end_index, samples.size))
            if end_index - start_index < minimum_samples:
                continue
            classification = classify_audio_window(
                samples[start_index:end_index],
                sample_rate=sample_rate,
                config=config,
            )
            classifications[window_index] = {
                **classification,
                "window_index": window_index,
                "window_start_seconds": round(window_start_seconds, 3),
                "window_end_seconds": round(window_end_seconds, 3),
            }
        _apply_classification(output[position], classifications[window_index])

    windows = [classifications[index] for index in sorted(classifications)]
    return output, windows


def annotate_record_with_window_classification(
    record: Mapping[str, Any],
    signal: Any,
    *,
    start_seconds: float,
    end_seconds: float,
    signal_start_seconds: float = 0.0,
    sample_rate: int = 16_000,
    config: AudioClassifierConfig | None = None,
) -> dict[str, Any]:
    """Single-record wrapper over the batch API above."""

    annotated, _windows = annotate_records_with_window_classification(
        [record],
        signal,
        spans=[(start_seconds, end_seconds)],
        signal_start_seconds=signal_start_seconds,
        sample_rate=sample_rate,
        config=config,
    )
    return annotated[0]


def shift_window_classification(
    record: Mapping[str, Any],
    offset_seconds: float,
    *,
    window_seconds: float = 10.0,
) -> dict[str, Any]:
    """Move a classified record onto another timeline (e.g. live segment → session).

    实时切片链路先按「段内时间轴」做完分类，随后整段 row 的时间戳会被加上段起点
    （``_read_segment_result``）。分类字段必须跟着平移，否则各段窗口都从 0 开始，
    聚合后所有段共享同一批 window_index，窗口会被错误地去重合并。
    """

    shifted = dict(record)
    start = shifted.get("audio_classifier_window_start_seconds")
    end = shifted.get("audio_classifier_window_end_seconds")
    if start is None or end is None:
        return shifted
    try:
        new_start = float(start) + float(offset_seconds)
        new_end = float(end) + float(offset_seconds)
    except (TypeError, ValueError):
        return shifted
    shifted["audio_classifier_window_start_seconds"] = round(new_start, 3)
    shifted["audio_classifier_window_end_seconds"] = round(new_end, 3)
    shift = float(offset_seconds)
    if shift and shifted.get("audio_classifier_window_index") is not None:
        try:
            shifted["audio_classifier_window_index"] = int(
                floor((float(start) + shift) / window_seconds)
            )
        except (TypeError, ValueError):
            pass
    return shifted
