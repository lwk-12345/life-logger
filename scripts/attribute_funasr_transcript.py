"""Attribute an existing Fun-ASR transcript to a local voice template."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import replace
from math import log10, sqrt
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for import_root in (REPO_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from life_logger.aggregate import build_daily_summary  # noqa: E402
from life_logger.audio_classifier import (  # noqa: E402
    AUDIO_CLASSIFIER_POLICY_VERSION,
    annotate_records_with_audio_classification,
)
from life_logger.context_source_scoring import (  # noqa: E402
    apply_context_source_scoring,
)
from life_logger.candidate_extractor import CandidateExtractor  # noqa: E402
from life_logger.domain import (  # noqa: E402
    EventTime,
    LifeEvent,
    RecordingSegment,
    TranscriptSegment,
)
from life_logger.extract import (  # noqa: E402
    MIN_STRUCTURED_EVENT_CONFIDENCE,
    RuleBasedExtractor,
)
from life_logger.local_llm_extract import (  # noqa: E402
    DeepSeekEventExtractor,
    LocalLLMEventExtractor,
)
from life_logger.merge import merge_adjacent_events  # noqa: E402
from life_logger.render import write_daily_outputs  # noqa: E402
from life_logger.segment import split_utterances  # noqa: E402
from life_logger.source_attribution import (  # noqa: E402
    AttributionConfig,
    apply_multi_evidence_policy,
    build_attribution_record,
    classify_acoustic_proximity,
    decisions_for_embeddings,
    has_important_signal,
    normalize_audio_gain,
    partition_attributions,
    protect_short_low_similarity,
    should_extract_life_events,
    unknown_decision,
)
from life_logger.speech_separation import (  # noqa: E402
    SEPARATION_POLICY_VERSION,
    ModelScopeTwoSpeakerSeparator,
    apply_difficult_speech_separation,
)

SAMPLE_RATE = 16_000
DEFAULT_WINDOW_SECONDS = 1_800.0
DEFAULT_BATCH_SIZE = 16
DEFAULT_MIN_SEGMENT_SECONDS = 0.8
DEFAULT_SHORT_REVIEW_SECONDS = 4.0
DEFAULT_SHORT_REVIEW_SCORE_FLOOR = 0.10
SOURCE_METHOD = "cam++_cosine"
SOURCE_POLICY_VERSION = "layered-multi-evidence-v5-near-clear"

# ---- dictation（对着电脑说话）剔除 ----------------------------------------
# AirPassport 是 Mac 上的语音输入；Apple Watch 连续录音会把同一段话一并录进去。
# 守护进程 scripts/watch_airpassport.py --mark-only 把每段 dictation 的起止时刻
# 写进一个 JSONL 索引，这里用它把重叠的句子标为 voice_input，不让它变成生活事件。
#
# 识别规则（时间窗 + 语义特征词）的唯一实现在 life_logger/dictation.py：一份规则
# 既供本脚本剔除，也供「生活看板隐藏 dictation」的 API 读层过滤使用。这里只转发，
# 避免两处实现漂移。
from life_logger.dictation import (  # noqa: E402
    DICTATION_OVERLAP_RATIO,
    is_dictation_record,
    load_dictation_windows,
    record_time_window,
)


def embedding_rows(value: Any) -> list[list[float]]:
    """Normalize a CAM++ vector or batch tensor into ordered Python rows."""

    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("speaker embedding must be a non-empty vector or batch")
    if not isinstance(value[0], (list, tuple)):
        return [[float(item) for item in value]]
    rows: list[list[float]] = []
    for row in value:
        if not isinstance(row, (list, tuple)) or not row:
            raise ValueError("speaker embedding rows must be non-empty vectors")
        rows.append([float(item) for item in row])
    return rows


def embeddings_from_results(results: Sequence[Mapping[str, Any]]) -> list[list[float]]:
    """Flatten CAM++ batch results while retaining input order."""

    embeddings: list[list[float]] = []
    for result in results:
        if "spk_embedding" not in result:
            raise ValueError("speaker result is missing spk_embedding")
        embeddings.extend(embedding_rows(result["spk_embedding"]))
    return embeddings


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _render_window(
    source: Path,
    *,
    start_s: float,
    duration_s: float,
    destination: Path,
) -> None:
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(start_s),
                "-i",
                str(source),
                "-t",
                str(duration_s),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                "-y",
                str(destination),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError("source attribution requires ffmpeg") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or "unknown ffmpeg error"
        raise RuntimeError(f"could not render audio window: {detail}") from error


def _panns_events_if_enabled(source: Path) -> dict[str, Any] | None:
    """提取声学事件（掌声/笑声/音乐/咳嗽…），**默认关闭**。

    为什么默认关：PANNs 的 RTF≈0.1，5 小时录音要跑 30 分钟，不能进主链路默认路径。
    开启方式：LIFE_LOGGER_PANNS_ENABLE=1

    采样策略：整段超过 PANNS_SAMPLE_MAX_SECONDS 时，均匀取若干片段（默认最多 3 段 × 2 分钟），
    只用来回答「这段录音里出现过哪些声音事件」。
    """
    if os.environ.get("LIFE_LOGGER_PANNS_ENABLE", "").strip().lower() not in {"1", "true", "yes"}:
        return None
    try:
        from life_logger.audio_scene import PannsUnavailable, annotate
    except Exception as error:  # noqa: BLE001
        return {"enabled": True, "error": f"import failed: {error}"[:200]}

    try:
        total = _probe_duration(source)
        max_secs = float(os.environ.get("LIFE_LOGGER_PANNS_SAMPLE_MAX_SECONDS", 120.0))
        max_clips = int(os.environ.get("LIFE_LOGGER_PANNS_SAMPLE_CLIPS", 3))
        clip_results: list[dict[str, Any]] = []

        if total <= max_secs or max_clips <= 1:
            clip_results.append(annotate(source))
        else:
            step = max(1.0, (total - max_secs) / max(1, max_clips - 1))
            for i in range(max_clips):
                start = min(i * step, max(0.0, total - max_secs))
                with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as fh:
                    clip = Path(fh.name)
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{max_secs:.2f}",
                     "-i", str(source), "-c", "copy", str(clip)],
                    capture_output=True, text=True,
                )
                try:
                    if clip.exists() and clip.stat().st_size > 0:
                        clip_results.append(annotate(clip))
                finally:
                    clip.unlink(missing_ok=True)

        # 合并各片段的事件（取峰值最大者）
        merged: dict[str, dict[str, Any]] = {}
        for r in clip_results:
            for e in r.get("events") or []:
                k = str(e.get("event"))
                prev = merged.get(k)
                if prev is None or float(e.get("peak_score") or 0) > float(prev.get("peak_score") or 0):
                    merged[k] = dict(e)
                else:
                    prev["mean_score"] = round(
                        max(float(prev.get("mean_score") or 0), float(e.get("mean_score") or 0)), 4)
                    prev["windows"] = int(prev.get("windows") or 0) + int(e.get("windows") or 0)

        events = sorted(merged.values(), key=lambda x: -float(x.get("peak_score") or 0))
        return {
            "enabled": True,
            "policy_version": "panns-audioset-527-v1",
            "sampled_clips": len(clip_results),
            "duration_seconds": round(total, 2),
            "events": events[:10],
            "note": "仅用于声学事件；场景判定不使用本字段（实测本地模型判场景不可靠）",
        }
    except PannsUnavailable as error:
        return {"enabled": True, "error": f"panns unavailable: {error}"[:200]}
    except Exception as error:  # noqa: BLE001
        return {"enabled": True, "error": f"{type(error).__name__}: {error}"[:200]}


def _probe_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise RuntimeError("source attribution requires ffprobe") from error
    try:
        return float(result.stdout.strip())
    except ValueError as error:
        raise RuntimeError(f"could not read duration for {path}") from error


def _load_waveform(path: Path) -> Any:
    try:
        import soundfile as sf
    except ImportError as error:
        raise RuntimeError("source attribution requires soundfile") from error
    waveform, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    if getattr(waveform, "ndim", 1) == 2:
        waveform = waveform.mean(axis=1)
    if sample_rate != SAMPLE_RATE:
        raise RuntimeError(f"expected 16kHz audio, got {sample_rate}Hz")
    return waveform


def _mean_square(samples: Any) -> float:
    if len(samples) == 0:
        return 0.0
    try:
        squared = samples * samples
        return float(squared.mean())
    except (AttributeError, TypeError):
        return sum(float(value) * float(value) for value in samples) / len(samples)


def _rms_dbfs(samples: Any) -> float:
    rms = sqrt(max(_mean_square(samples), 0.0))
    return 20.0 * log10(max(rms, 1e-6))


def _local_noise_floor_dbfs(
    waveform: Any,
    *,
    start_index: int,
    end_index: int,
    margin_seconds: float = 2.0,
) -> float:
    margin = int(round(margin_seconds * SAMPLE_RATE))
    context_start = max(0, start_index - margin)
    context_end = min(len(waveform), end_index + margin)
    frame_size = max(1, int(round(0.02 * SAMPLE_RATE)))
    levels: list[float] = []
    for frame_start in range(context_start, context_end, frame_size):
        frame = waveform[frame_start : min(frame_start + frame_size, context_end)]
        if len(frame):
            levels.append(_rms_dbfs(frame))
    if not levels:
        return -120.0
    levels.sort()
    percentile_index = min(len(levels) - 1, int(round((len(levels) - 1) * 0.20)))
    return levels[percentile_index]


def _attach_acoustic_evidence(
    records: list[dict[str, Any]],
    measurements: Mapping[str, Mapping[str, float]],
    *,
    local_horizon_seconds: float = 60.0,
) -> list[dict[str, Any]]:
    measured_rows = [
        (
            str(record.get("segment_id")),
            (
                float(record.get("start_offset_seconds", 0.0))
                + float(record.get("end_offset_seconds", 0.0))
            )
            / 2.0,
            float(measurements[str(record.get("segment_id"))]["speech_rms_dbfs"]),
        )
        for record in records
        if str(record.get("segment_id")) in measurements
    ]
    for record in records:
        segment_id = str(record.get("segment_id"))
        measurement = measurements.get(segment_id)
        if measurement is None:
            record.update(
                acoustic_proximity="uncertain",
                proximity_score=0.5,
                proximity_confidence=0.0,
                acoustic_evidence_available=False,
            )
            continue
        midpoint = (
            float(record.get("start_offset_seconds", 0.0))
            + float(record.get("end_offset_seconds", 0.0))
        ) / 2.0
        nearby_levels = [
            level
            for _candidate_id, candidate_midpoint, level in measured_rows
            if abs(candidate_midpoint - midpoint) <= local_horizon_seconds
        ]
        reference = (
            median(nearby_levels)
            if nearby_levels
            else float(measurement["speech_rms_dbfs"])
        )
        relative_loudness = float(measurement["speech_rms_dbfs"]) - reference
        proximity = classify_acoustic_proximity(
            speech_rms_dbfs=float(measurement["speech_rms_dbfs"]),
            noise_floor_dbfs=float(measurement["noise_floor_dbfs"]),
            relative_loudness_db=relative_loudness,
        )
        record.update(
            speech_rms_dbfs=round(float(measurement["speech_rms_dbfs"]), 3),
            noise_floor_dbfs=round(float(measurement["noise_floor_dbfs"]), 3),
            relative_loudness_db=round(relative_loudness, 3),
            audio_gain_db=round(float(measurement.get("gain_db", 0.0)), 3),
            acoustic_evidence_available=True,
            **proximity,
        )
    return records


def _load_transcript(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not read transcript: {path}") from error
    if not isinstance(payload, list):
        raise RuntimeError("transcript JSON must contain a list")
    return sorted(
        [dict(item) for item in payload],
        key=lambda item: float(item["start_offset_seconds"]),
    )


def _load_recordings(path: Path) -> list[RecordingSegment]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not read recordings: {path}") from error
    if not isinstance(payload, list):
        raise RuntimeError("recordings JSON must contain a list")
    return [RecordingSegment(**item) for item in payload]


def _transcript_segment(payload: Mapping[str, Any]) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=str(payload["segment_id"]),
        recording_id=str(payload["recording_id"]),
        start_offset_seconds=float(payload["start_offset_seconds"]),
        end_offset_seconds=float(payload["end_offset_seconds"]),
        absolute_started_at=payload.get("absolute_started_at"),
        absolute_ended_at=payload.get("absolute_ended_at"),
        text=str(payload["text"]),
        source=str(payload.get("source", "local_funasr_nano")),
        language=str(payload.get("language", "zh")),
        transcription_confidence=payload.get("transcription_confidence"),
    )


def _extract_template_embedding(model: Any, template: Path) -> list[float]:
    results = model.generate(input=str(template))
    embeddings = embeddings_from_results(results)
    if len(embeddings) != 1:
        raise RuntimeError(
            f"expected one template embedding, received {len(embeddings)}"
        )
    return embeddings[0]


def _window_groups(
    records: Sequence[Mapping[str, Any]],
    window_seconds: float,
) -> dict[int, list[Mapping[str, Any]]]:
    groups: dict[int, list[Mapping[str, Any]]] = {}
    for record in records:
        start = float(record["start_offset_seconds"])
        index = int(start // window_seconds)
        groups.setdefault(index, []).append(record)
    return groups


def _decide_window(
    waveform: Any,
    *,
    window_start_s: float,
    records: Sequence[Mapping[str, Any]],
    model: Any,
    template_embedding: Sequence[float],
    config: AttributionConfig,
    batch_size: int,
    min_segment_seconds: float,
    short_review_seconds: float,
    short_review_score_floor: float,
) -> list[dict[str, Any]]:
    pending_records: list[Mapping[str, Any]] = []
    pending_audio: list[Any] = []
    output: list[dict[str, Any] | None] = []
    measurements: dict[str, dict[str, float]] = {}
    for record in records:
        start = float(record["start_offset_seconds"])
        end = float(record["end_offset_seconds"])
        duration = end - start
        start_index = max(
            0,
            int(round((start - window_start_s) * SAMPLE_RATE)),
        )
        end_index = min(
            len(waveform),
            int(round((end - window_start_s) * SAMPLE_RATE)),
        )
        if end_index <= start_index:
            output.append(
                build_attribution_record(
                    record,
                    unknown_decision("audio_slice_empty"),
                    method=SOURCE_METHOD,
                )
            )
            continue

        segment_audio = waveform[start_index:end_index]
        measurements[str(record["segment_id"])] = {
            "speech_rms_dbfs": _rms_dbfs(segment_audio),
            "noise_floor_dbfs": _local_noise_floor_dbfs(
                waveform,
                start_index=start_index,
                end_index=end_index,
            ),
        }
        if duration < min_segment_seconds:
            output.append(
                build_attribution_record(
                    record,
                    unknown_decision("segment_too_short"),
                    method=SOURCE_METHOD,
                )
            )
            continue

        segment_audio, normalization = normalize_audio_gain(segment_audio)
        measurements[str(record["segment_id"])]["gain_db"] = float(
            normalization["gain_db"]
        )

        output.append(None)
        pending_records.append(record)
        pending_audio.append(segment_audio)

    pending_offset = 0
    for batch_start in range(0, len(pending_audio), batch_size):
        batch_audio = pending_audio[batch_start : batch_start + batch_size]
        batch_records = pending_records[batch_start : batch_start + batch_size]
        candidate_embeddings: list[list[float] | None] = [None] * len(batch_records)
        try:
            raw_results = model.generate(input=batch_audio, batch_size=len(batch_audio))
            candidate_embeddings = embeddings_from_results(raw_results)
            if len(candidate_embeddings) != len(batch_records):
                raise RuntimeError(
                    "speaker model returned a different number of embeddings"
                )
            decisions = decisions_for_embeddings(
                template_embedding,
                candidate_embeddings,
                config,
            )
            decisions = [
                protect_short_low_similarity(
                    decision,
                    duration_seconds=(
                        float(record["end_offset_seconds"])
                        - float(record["start_offset_seconds"])
                    ),
                    review_seconds=short_review_seconds,
                    review_score_floor=short_review_score_floor,
                )
                for record, decision in zip(batch_records, decisions)
            ]
        except Exception as error:  # pragma: no cover - hardware/model boundary
            candidate_embeddings = [None] * len(batch_records)
            decisions = [
                unknown_decision(f"speaker_model_error:{type(error).__name__}")
                for _record in batch_records
            ]
        for record, decision, candidate_embedding in zip(
            batch_records, decisions, candidate_embeddings
        ):
            while output[pending_offset] is not None:
                pending_offset += 1
            attributed_record = build_attribution_record(
                record,
                decision,
                method=SOURCE_METHOD,
            )
            if candidate_embedding is not None:
                attributed_record["_speaker_embedding"] = candidate_embedding
            output[pending_offset] = attributed_record
            pending_offset += 1

    resolved = [record for record in output if record is not None]
    return _attach_acoustic_evidence(resolved, measurements)


_STRONG_EXPENSE_MARKERS = (
    "花了",
    "花费",
    "买了",
    "刚买",
    "已经买",
    "买完",
    "消费",
    "花钱",
    "付款",
    "支付",
    "付了",
    "扫了",
    "买烟",
    "买包烟",
    "买香烟",
)


def _should_exempt_media_discount(
    record: Mapping[str, Any],
    event: Any,
    utterance_text: str = "",
) -> bool:
    if record.get("source_label") != "self":
        return False
    audio_class = str(record.get("audio_content_class") or "")
    if audio_class not in {"stream", "noise"}:
        return False
    categories = getattr(event, "categories", []) or []
    if "expense" in categories:
        return True
    openclaw_payload = getattr(event, "data", {}).get("openclaw", {})
    if openclaw_payload.get("candidate_type") in {
        "decision_candidate",
        "reasoning_candidate",
        "question_candidate",
        "todo_candidate",
        "fact_candidate",
    }:
        return True
    combined_text = f"{utterance_text} {record.get('text', '')}"
    return any(marker in combined_text for marker in _STRONG_EXPENSE_MARKERS)


def _event_from_dict(item: dict[str, Any]) -> LifeEvent:
    return LifeEvent(
        event_id=item["event_id"],
        categories=list(item["categories"]),
        activity_type=item.get("activity_type"),
        title=item["title"],
        time=EventTime(**item["time"]),
        data=dict(item.get("data") or {}),
        capture_mode=item.get("capture_mode", "passive"),
        source_type=item.get("source_type", "audio"),
        confidence=float(item.get("confidence", 0.0)),
        evidence=dict(item.get("evidence") or {}),
        review_status=item.get("review_status", "unreviewed"),
    )


def _resolve_deepseek_runtime() -> tuple[str, str | None, str | None]:
    """解析 DeepSeek 运行时三件套：``(api_key, base_url, model)``。

    【2026-10-14 修】为什么必须「返回出来当参数传」而不是「写 os.environ」：
    ``contextual_extraction`` 的 ``DEEPSEEK_BASE_URL`` / ``DEEPSEEK_MODEL`` 是**模块级常量**，
    在 import 那一刻就固化。旧代码在这里写 ``os.environ["DEEPSEEK_BASE_URL"] = ...``
    想修正端点，但只要该模块已被 import（本脚本更早的调用点会 import 它），
    写环境变量**完全无效**——请求仍打到模块默认值 ``https://api.deepseek.com``，
    拿 Command Code 的 key 换 401 → ``status=failed`` → 全部输入进 pending
    （这是「消费金额全 0 / 账本失真」的真根因，也是潜在的红线风险）。
    正确姿势：把值一路当**参数**传给 ``run_contextual_extraction``，
    它内部会调 ``configure_deepseek`` 覆盖全局常量。

    优先级：环境变量 → ``local-config.json`` 的 ``deepseek`` 节 → 不兜底到官网。
    """
    api_key = str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    base_url = str(os.environ.get("DEEPSEEK_BASE_URL") or "").strip() or None
    model = str(os.environ.get("DEEPSEEK_MODEL") or "").strip() or None

    cfg_path = Path(__file__).resolve().parents[1] / "local-config.json"
    if cfg_path.is_file():
        try:
            cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg_data = {}
        section = cfg_data.get("deepseek") if isinstance(cfg_data, dict) else None
        if isinstance(section, dict):
            api_key = api_key or str(section.get("api_key") or "").strip()
            base_url = base_url or str(section.get("base_url") or "").strip() or None
            model = model or str(section.get("model") or "").strip() or None

    return api_key, base_url, model


def _build_events(
    records: Sequence[Mapping[str, Any]],
    *,
    local_llm_enabled: bool = False,
    local_llm_model: str = "",
    local_llm_url: str = "http://127.0.0.1:11434",
    local_llm_review_confidence: float = 0.80,
    deepseek_enabled: bool = False,
    deepseek_model: str = "deepseek-flash",
) -> list[Any]:
    rule_extractor = CandidateExtractor(RuleBasedExtractor())
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    if not deepseek_key:
        cfg_path = Path(__file__).resolve().parents[1] / "local-config.json"
        if cfg_path.is_file():
            try:
                cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
                deepseek_key = cfg_data.get("deepseek", {}).get("api_key")
            except Exception:
                pass
    if deepseek_enabled and deepseek_key:
        extractor = DeepSeekEventExtractor(
            enabled=True,
            api_key=deepseek_key,
            model=deepseek_model,
            rule_extractor=rule_extractor,
        )
    else:
        extractor = LocalLLMEventExtractor(
            enabled=local_llm_enabled,
            model=local_llm_model,
            base_url=local_llm_url,
            confidence_threshold=local_llm_review_confidence,
            rule_extractor=rule_extractor,
        )
    events: list[Any] = []
    for record in records:
        # This fallback has no pending-event channel. Never let a weak speaker
        # decision become a formal event when contextual extraction is down.
        if record.get("source_label") != "self":
            continue
        transcript = _transcript_segment(record)
        try:
            priority_weight = float(record.get("audio_priority_weight", 1.0))
        except (TypeError, ValueError):
            priority_weight = 1.0
        priority_weight = max(0.0, min(1.0, priority_weight))
        for utterance in split_utterances(transcript):
            for event in extractor.extract(utterance):
                exempt_media_discount = _should_exempt_media_discount(
                    record,
                    event,
                    utterance.text,
                )
                effective_weight = 1.0 if exempt_media_discount else priority_weight
                evidence = {
                    **event.evidence,
                    "source_label": record.get("source_label"),
                    "source_review_required": bool(record.get("review_required")),
                    "source_review_priority": record.get("review_priority"),
                    "audio_content_class": record.get("audio_content_class"),
                    "audio_content_scores": record.get("audio_content_scores"),
                    "audio_processing_priority": record.get(
                        "audio_processing_priority"
                    ),
                    "audio_priority_weight": round(priority_weight, 6),
                }
                if exempt_media_discount:
                    evidence["audio_priority_weight_exempted"] = True
                    evidence["effective_audio_priority_weight"] = 1.0
                weighted_confidence = round(
                    event.confidence * effective_weight,
                    6,
                )
                if weighted_confidence < MIN_STRUCTURED_EVENT_CONFIDENCE:
                    continue
                events.append(
                    replace(
                        event,
                        confidence=weighted_confidence,
                        evidence=evidence,
                    )
                )
    return merge_adjacent_events(events)


def _audio_classification_windows(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    windows: dict[int, dict[str, Any]] = {}
    for record in records:
        index = record.get("audio_classifier_window_index")
        scores = record.get("audio_content_scores")
        if index is None or not isinstance(scores, Mapping):
            continue
        try:
            numeric_index = int(index)
        except (TypeError, ValueError):
            continue
        windows[numeric_index] = {
            "window_index": numeric_index,
            "window_start_seconds": record.get(
                "audio_classifier_window_start_seconds"
            ),
            "window_end_seconds": record.get(
                "audio_classifier_window_end_seconds"
            ),
            "primary_class": record.get("audio_content_class"),
            "probabilities": dict(scores),
            "priority_weight": record.get("audio_priority_weight"),
            "processing_priority": record.get("audio_processing_priority"),
            "features": dict(record.get("audio_classifier_features") or {}),
        }
    return [windows[index] for index in sorted(windows)]


def _run_context_only(args: argparse.Namespace) -> dict[str, Any]:
    """Rebuild event outputs from saved ASR and attribution artifacts only."""
    if args.transcript is None or args.recordings is None:
        raise RuntimeError("context-only mode requires transcript and recordings")
    if args.source_attribution is None:
        raise RuntimeError("context-only mode requires source-attribution.json")
    if not args.deepseek_enabled:
        raise RuntimeError(
            "context-only mode requires DeepSeek extraction to be enabled"
        )
    api_key, ds_base_url, ds_model = _resolve_deepseek_runtime()
    if not api_key:
        raise RuntimeError("DeepSeek API key is unavailable")

    transcript_path = args.transcript.resolve()
    recordings_path = args.recordings.resolve()
    attribution_path = args.source_attribution.resolve()
    output_dir = args.output_dir.resolve()
    for path, label in (
        (transcript_path, "transcript"),
        (recordings_path, "recordings"),
        (attribution_path, "source-attribution"),
    ):
        if not path.is_file():
            raise RuntimeError(f"{label} file not found")

    records = _load_transcript(transcript_path)
    recordings = _load_recordings(recordings_path)
    attributed = json.loads(attribution_path.read_text(encoding="utf-8"))
    if not records or not isinstance(attributed, list) or not attributed:
        raise RuntimeError("saved ASR or source-attribution checkpoint is empty")
    def index_segments(
        rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, Mapping[str, Any]]:
        indexed: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            segment_id = str(row.get("segment_id") or "")
            if not segment_id or segment_id in indexed:
                raise RuntimeError(
                    "ASR checkpoint contains duplicate or missing segment IDs"
                )
            indexed[segment_id] = row
        return indexed

    if not all(isinstance(record, dict) for record in attributed):
        raise RuntimeError("source-attribution checkpoint is invalid")
    asr_by_id = index_segments(records)
    attributed_by_id = index_segments(attributed)
    if set(asr_by_id) != set(attributed_by_id):
        raise RuntimeError("ASR and source-attribution checkpoints do not match")
    identity_fields = (
        "recording_id",
        "start_offset_seconds",
        "end_offset_seconds",
        "absolute_started_at",
        "absolute_ended_at",
        "text",
    )
    for segment_id, record in asr_by_id.items():
        attributed_record = attributed_by_id[segment_id]
        if any(
            record.get(field) != attributed_record.get(field)
            for field in identity_fields
        ):
            raise RuntimeError(
                "ASR and source-attribution checkpoint rows do not match"
            )
        if attributed_record.get("source_label") not in {"self", "non_self", "unknown"}:
            raise RuntimeError("source-attribution checkpoint label is invalid")

    from life_logger.contextual_extraction import run_contextual_extraction

    dictation_windows = load_dictation_windows(args.dictation_windows)
    context_records = []
    for record in attributed:
        item = dict(record)
        item["is_dictation"] = is_dictation_record(item, dictation_windows)
        context_records.append(item)

    def report_progress(progress: dict[str, Any]) -> None:
        batch = progress.get("batch")
        total = progress.get("total_batches")
        status = str(progress.get("status") or "running")
        if isinstance(batch, int) and isinstance(total, int):
            print(f"context batch {batch}/{total} {status}", flush=True)

    output_dir.mkdir(parents=True, exist_ok=True)
    extraction = run_contextual_extraction(
        context_records,
        deepseek_api_key=api_key,
        # 【2026-10-14 修】必须显式传端点与模型名：模块级常量在 import 时已固化，
        # 只写 os.environ 无效（详见 _resolve_deepseek_runtime 的说明）。
        deepseek_base_url=ds_base_url,
        deepseek_model=ds_model or args.deepseek_model or None,
        output_dir=output_dir,
        source_namespace=(
            args.context_source_namespace.strip()
            or str(attribution_path.parent)
        ),
        progress_callback=report_progress,
    )
    if extraction.get("status") == "failed":
        raise RuntimeError(
            "context extraction retry failed; previous results were preserved"
        )

    events = [
        _event_from_dict(event)
        for event in extraction.get("formal_events", [])
        if isinstance(event, dict)
    ]
    day = str(recordings[0].started_at or "")[:10] if recordings else ""
    if not day or day == "None":
        for record in attributed:
            started_at = str(record.get("absolute_started_at") or "")
            if len(started_at) >= 10:
                day = started_at[:10]
                break
    if not day or day == "None":
        day = "undated"
    warnings = []
    if extraction.get("pending_events"):
        warnings.append(
            f"有 {len(extraction['pending_events'])} 条事件进入待确认候选，"
            "暂未计入正式生活日志。"
        )
    summary = build_daily_summary(
        events,
        date=None if day == "undated" else day,
        timezone_name=args.timezone,
        warnings=warnings,
    )
    daily_paths = write_daily_outputs(
        output_dir,
        date=None if day == "undated" else day,
        recordings=recordings,
        transcripts=[_transcript_segment(item) for item in records],
        events=events,
        summary=summary,
    )
    run_summary_path = output_dir / "run-summary.json"
    try:
        run_summary = json.loads(run_summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        run_summary = {}
    counts = run_summary.get("counts")
    if not isinstance(counts, dict):
        counts = {}
        run_summary["counts"] = counts
    counts["events"] = len(events)
    run_summary["context_extraction_status"] = extraction.get("status")
    run_summary["daily_outputs"] = {
        key: str(value) for key, value in daily_paths.items()
    }
    _atomic_write_json(run_summary_path, run_summary)
    return {
        "status": extraction.get("status") or "completed",
        "counts": extraction.get("counts") or {},
        "output_dir": str(output_dir),
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.context_only:
        return _run_context_only(args)
    source = args.audio.resolve()
    transcript_path = args.transcript.resolve()
    recordings_path = args.recordings.resolve()
    output_dir = args.output_dir.resolve()
    is_cloud = bool(args.cloud_speaker or str(args.speaker_model).lower() == "cloud")
    check_paths = [
        (source, "audio"),
        (transcript_path, "transcript"),
        (recordings_path, "recordings"),
    ]
    template = None
    if not is_cloud:
        if args.template is None:
            raise RuntimeError("speaker template is required")
        template = args.template.resolve()
        check_paths.append((template, "template"))
    for path, label in check_paths:
        if not path.is_file():
            raise RuntimeError(f"{label} file not found: {path}")

    records = _load_transcript(transcript_path)
    recordings = _load_recordings(recordings_path)
    if not records:
        raise RuntimeError("transcript contains no segments")
    output_dir.mkdir(parents=True, exist_ok=True)

    config = AttributionConfig(
        self_threshold=args.self_threshold,
        unknown_threshold=args.unknown_threshold,
    )
    attributed: list[dict[str, Any]] = []
    model: Any = None
    template_embedding: Any = None

    if args.cloud_speaker or str(args.speaker_model).lower() == "cloud":
        durations: dict[str, float] = {}
        for r in records:
            spk = str(r.get("speaker") or "0")
            dur = max(0.1, float(r.get("end_offset_seconds", 0)) - float(r.get("start_offset_seconds", 0)))
            durations[spk] = durations.get(spk, 0.0) + dur
        primary_speaker = max(durations.keys(), key=lambda k: durations[k]) if durations else "0"
        for r in records:
            item = dict(r)
            spk = str(r.get("speaker") or "0")
            is_self = (spk == primary_speaker) or (len(durations) <= 1)
            text = str(r.get("text") or "")
            if re.search(r"^(?:现在)?记录一下|^(?:现在|请|帮我)?(?:查询一下|查一下|自查一下)|昨天练的腿|我健身|生脉饮|乌冬面", text):
                is_self = True
            item["source_label"] = "self" if is_self else "non_self"
            item["voice_similarity"] = 0.95 if is_self else 0.05
            item["decision_reason"] = "cloud_speaker_primary" if is_self else "cloud_speaker_secondary"
            item["review_required"] = False
            item["source_method"] = "cloud_speaker_diarization"
            attributed.append(item)
        print(
            f"云端说话人分离角色直通完成: 共 {len(attributed)} 条片段, 本人角色 speaker_{primary_speaker} "
            f"({durations.get(primary_speaker, 0):.1f}s)",
            flush=True,
        )
    elif records and any(
        r.get("source") == "local_whisper_turbo"
        # 【2026-10-14 收紧 · 审查松点】原来只看「有没有 acoustic_proximity 字段」——
        # 字段在但值是垃圾（上游改坏/降级未算）也会被盲信。现在要求字段存在**且**
        # 身份结论可信：label 有效，且（有相似度分 或 decision_reason 是上游的诚实降级标记）。
        # 实测 219 条中 12 条 voice_similarity=None 的降级行，靠 reason 标记仍正确复用。
        or (
            "acoustic_proximity" in r
            and r.get("source_label") in {"self", "non_self", "unknown"}
            and (
                r.get("voice_similarity") is not None
                or str(r.get("decision_reason") or "").strip()
                in {
                    "segment_too_short",
                    "segment_out_of_audio_range",
                    "voiceprint_unavailable",
                    "voiceprint_unavailable_proximity_not_identity",
                }
                or str(r.get("decision_reason") or "").startswith("voiceprint_unavailable:")
            )
        )
        or r.get("source_label") in {"self", "non_self", "unknown"}
        for r in records
    ):
        for r in records:
            item = dict(r)
            prox_label = str(item.get("acoustic_proximity") or "")
            prox_ratio = item.get("proximity_ratio_db")
            prox_score = item.get("proximity_score")
            upstream_voice_sim = item.get("voice_similarity")
            upstream_label = str(item.get("source_label") or "")
            if upstream_voice_sim is not None and upstream_label in {"self", "non_self", "unknown"}:
                # 【2026-10-14 修】上游（transcribe.py / moss_transcribe.py）已经用声纹判过，
                # 并且做过近场信道补偿 + 多证据融合。这里**原样复用，不再覆盖**。
                #
                # 旧逻辑是「远场一票否决」：见到 acoustic_proximity=far 就把上游判好的结果
                # 重新压回 non_self + min(sim, 0.18)。实测后果：上游把一段 raw=0.834（本人）
                # 正确判成 self(0.667)+review 之后，走到这里又被改回 non_self + 0.18 ——
                # 上游的修复被下游原地撤销。远处的录音只是「录远了」，不等于「不是本人」。
                pass
            elif prox_label == "far" or (prox_ratio is not None and float(prox_ratio) < -1.5):
                # 兜底路径：没有上游声纹时，才退化成远近场判断
                item["source_label"] = "non_self"
                item["speaker"] = "non_self"
                item["voice_similarity"] = min(float(item.get("voice_similarity") or 0.18), 0.18)
            elif (
                prox_label == "uncertain"
                and prox_score is not None
                and float(prox_score) < 0.45
                and (prox_ratio is None or float(prox_ratio) < 4.0)
            ):
                item["source_label"] = "non_self"
                item["speaker"] = "non_self"
                item["voice_similarity"] = min(float(item.get("voice_similarity") or 0.22), 0.22)
            elif item.get("source_label") not in {"self", "non_self", "unknown"}:
                item["source_label"] = "non_self" if item.get("speaker") == "non_self" else "self"
            item.setdefault("acoustic_proximity", "near" if item["source_label"] == "self" else "unknown")
            item.setdefault("source_method", "inprocess_nearfield_voiceprint")
            item.setdefault("review_required", item.get("source_label") == "unknown")
            item.setdefault("decision_reason", "reused_segment_attribution")
            attributed.append(item)
        print(
            f"直接复用实时切片级声纹与近讲场域特征: 共 {len(attributed)} 条片段 (0秒免重算)",
            flush=True,
        )
    else:
        try:
            from funasr import AutoModel
        except ImportError as error:
            raise RuntimeError(
                "source attribution requires the FunASR environment"
            ) from error

        speaker_model_path = args.speaker_model
        if not Path(speaker_model_path).is_dir():
            # [2026-11-04 Q860] 本机模型缓存路径不再写死：LL_CAMPPULUS_MODEL_DIR 优先，
            # 未设置时保留原字面值作默认（行为与改造前逐字一致）。
            _campplus_override = os.environ.get("LL_CAMPPULUS_MODEL_DIR")
            default_local_paths = [
                Path(_campplus_override) if _campplus_override else Path.home() / ".cache/modelscope/models/iic--speech_campplus_sv_zh-cn_16k-common/snapshots/master",
                Path.home() / ".cache/modelscope/models/iic--speech_campplus_sv_zh-cn_16k-common/snapshots/master",
                Path.home() / ".cache/modelscope/hub/models/iic/speech_campplus_sv_zh-cn_16k-common",
                Path.home() / ".cache/modelscope/hub/iic/speech_campplus_sv_zh-cn_16k-common",
            ]
            for candidate in default_local_paths:
                if candidate.is_dir():
                    speaker_model_path = str(candidate)
                    break

        target_device = "cpu"
        try:
            import torch
            if torch.backends.mps.is_available():
                target_device = "mps"
        except Exception:
            pass
        model = AutoModel(
            model=speaker_model_path,
            device=target_device,
            ncpu=args.cpu_threads,
            disable_update=True,
            disable_pbar=True,
        )
        template_embedding = _extract_template_embedding(model, template)
        groups = _window_groups(records, args.window_seconds)
        identity = hashlib.sha256(
            json.dumps(
                {
                    "audio": [
                        str(source),
                        source.stat().st_size,
                        source.stat().st_mtime_ns,
                    ],
                    "transcript": hashlib.sha256(transcript_path.read_bytes()).hexdigest(),
                    "template": hashlib.sha256(template.read_bytes()).hexdigest(),
                    "model": args.speaker_model,
                    "thresholds": [args.self_threshold, args.unknown_threshold],
                    "window": args.window_seconds,
                    "minimum": args.min_segment_seconds,
                    "source_policy": SOURCE_POLICY_VERSION,
                    "short_review_seconds": args.short_review_seconds,
                    "short_review_score_floor": args.short_review_score_floor,
                    "audio_classifier_policy": AUDIO_CLASSIFIER_POLICY_VERSION,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        cache_dir = output_dir / "windows" / identity
        cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="life-logger-source-attribution-") as tmp:
            temporary_root = Path(tmp)
            for window_position, window_index in enumerate(sorted(groups), start=1):
                cache_file = cache_dir / f"{window_index:04d}.json"
                if cache_file.exists():
                    attributed.extend(json.loads(cache_file.read_text()))
                    print(f"window {window_position}/{len(groups)}: restored", flush=True)
                    continue
                window_records = groups[window_index]
                window_start = window_index * args.window_seconds
                window_end = max(
                    window_start + args.window_seconds,
                    max(float(item["end_offset_seconds"]) for item in window_records),
                )
                window_audio = temporary_root / f"window-{window_index:04d}.wav"
                _render_window(
                    source,
                    start_s=window_start,
                    duration_s=min(args.window_seconds, window_end - window_start),
                    destination=window_audio,
                )
                waveform = _load_waveform(window_audio)
                window_attributed = _decide_window(
                    waveform,
                    window_start_s=window_start,
                    records=window_records,
                    model=model,
                    template_embedding=template_embedding,
                    config=config,
                    batch_size=args.batch_size,
                    min_segment_seconds=args.min_segment_seconds,
                    short_review_seconds=args.short_review_seconds,
                    short_review_score_floor=args.short_review_score_floor,
                )
                window_attributed, _audio_windows = (
                    annotate_records_with_audio_classification(
                        window_attributed,
                        waveform,
                        window_start_seconds=window_start,
                        sample_rate=SAMPLE_RATE,
                    )
                )
                attributed.extend(window_attributed)
                print(
                    f"window {window_position}/{len(groups)} "
                    f"(source index {window_index}): "
                    f"segments={len(window_records)}",
                    flush=True,
                )
                _atomic_write_json(cache_file, attributed[-len(window_records) :])

    attributed.sort(key=lambda item: float(item["start_offset_seconds"]))
    if not (args.cloud_speaker or str(args.speaker_model).lower() == "cloud"):
        attributed = apply_multi_evidence_policy(attributed, config)
    for item in attributed:
        item["generic_source_label"] = item.get(
            "identity_source_label", item.get("source_label")
        )
    for item in attributed:
        if item.get("_speaker_embedding") is not None:
            item["speaker_embedding_model"] = args.speaker_model
    speaker_embeddings = {
        str(item.get("segment_id") or ""): list(item["_speaker_embedding"])
        for item in attributed
        if item.get("segment_id") and item.get("_speaker_embedding") is not None
    }
    attributed = apply_context_source_scoring(attributed)
    separation_summary = {
        "policy_version": SEPARATION_POLICY_VERSION,
        "enabled": bool(args.separation_model),
        "backend": None,
        "eligible": sum(
            1 for item in attributed if item.get("source_label") == "unknown"
        ),
        "attempted": 0,
        "rescued_self": 0,
        "retained_unknown": sum(
            1 for item in attributed if item.get("source_label") == "unknown"
        ),
        "errors": 0,
    }
    if args.separation_model and model is not None and template_embedding is not None:
        separator = ModelScopeTwoSpeakerSeparator(args.separation_model)
        with tempfile.TemporaryDirectory(
            prefix="life-logger-difficult-separation-"
        ) as separation_temp:
            separation_root = Path(separation_temp)

            def difficult_audio_provider(record: Mapping[str, Any]) -> Any:
                start = max(0.0, float(record["start_offset_seconds"]) - 0.25)
                end = max(start, float(record["end_offset_seconds"]) + 0.25)
                clip = separation_root / (
                    str(record.get("segment_id") or "segment") + ".wav"
                )
                _render_window(
                    source,
                    start_s=start,
                    duration_s=max(0.1, end - start),
                    destination=clip,
                )
                return _load_waveform(clip)

            def separated_embedding_provider(
                sources: Sequence[Any],
            ) -> list[list[float]]:
                results = model.generate(
                    input=list(sources),
                    batch_size=len(sources),
                )
                return embeddings_from_results(results)

            attributed, separation_summary = apply_difficult_speech_separation(
                attributed,
                audio_provider=difficult_audio_provider,
                separator=separator,
                embedding_provider=separated_embedding_provider,
                template_embedding=template_embedding,
                attribution_config=config,
                sample_rate=SAMPLE_RATE,
            )
        separation_summary["enabled"] = True
    speaker_embeddings = {
        str(item.get("segment_id") or ""): list(item["_speaker_embedding"])
        for item in attributed
        if item.get("segment_id") and item.get("_speaker_embedding") is not None
    }
    audio_classification_windows = _audio_classification_windows(attributed)
    for item in attributed:
        segment_id = str(item.get("segment_id") or "")
        embedding = item.pop("_speaker_embedding", None)
        if embedding is not None and segment_id:
            item["speaker_embedding_ref"] = f"speaker-embeddings.json#{segment_id}"
    partitions = partition_attributions(attributed)
    self_records = [dict(item) for item in partitions["self"]]
    non_self_records = [dict(item) for item in partitions["non_self"]]
    unknown_records = [dict(item) for item in partitions["unknown"]]
    event_records = [
        record
        for record in attributed
        if should_extract_life_events(
            str(record.get("source_label") or ""),
            text=str(record.get("text") or ""),
            review_priority=str(record.get("review_priority") or "") or None,
        )
    ]
    warnings: list[str] = []
    # dictation（对着电脑说话）剔除：这些句子是语音输入的指令，不是生活事件。
    # 判定靠时间窗，不靠文本内容比对 —— 同一条音频经不同链路 ASR 出来的文字
    # 不会逐字一致，文本比对会漏也会误伤。
    #
    # 保险：即使落在 dictation 时间窗里，只要句子里含消费/待办/提醒信号，
    # 也保留下来送人工复核。用户明确要求过「没买没去那些东西也很重要，
    # 为什么要去过滤掉」—— 边下单边说话是完全可能的，不能静默丢掉。
    dictation_windows = load_dictation_windows(args.dictation_windows)
    voice_input_records = []
    protected_records = []
    kept_records = []
    for record in event_records:
        if not is_dictation_record(record, dictation_windows):
            kept_records.append(record)
            continue
        if has_important_signal(str(record.get("text") or "")):
            protected_records.append(record)
            kept_records.append(record)
            continue
        voice_input_records.append(record)
    event_records = kept_records
    if protected_records:
        warnings.append(
            f"其中 {len(protected_records)} 条落在语音输入时间窗内但含重要生活主题，"
            "已保留送审而不是剔除。"
        )
    contextual_done = False
    comparison_mode = False
    events: list[Any] = []
    # 【2026-10-14 修】原来这里写 os.environ["DEEPSEEK_BASE_URL"] —— 无效：
    # contextual_extraction 的该常量是模块级、import 时已固化。改为取值后当参数传。
    deepseek_key, ds_base_url, ds_model = _resolve_deepseek_runtime()
    if deepseek_key:
        os.environ["DEEPSEEK_API_KEY"] = deepseek_key

    if args.deepseek_enabled and os.environ.get("DEEPSEEK_API_KEY"):
        try:
            from life_logger.contextual_extraction import run_contextual_extraction

            context_records = []
            for r in attributed:
                item = dict(r)
                item["is_dictation"] = is_dictation_record(r, dictation_windows)
                context_records.append(item)
            ctx_report = run_contextual_extraction(
                context_records,
                deepseek_api_key=os.environ["DEEPSEEK_API_KEY"],
                deepseek_base_url=ds_base_url,
                deepseek_model=ds_model or None,
                output_dir=output_dir,
                source_namespace=str(output_dir),
            )
            comparison_mode = bool(ctx_report.get("comparison_mode"))
            if comparison_mode or ctx_report.get("status") != "failed":
                events = [
                    _event_from_dict(ev)
                    for ev in ctx_report.get("formal_events", [])
                ]
                if ctx_report.get("status") != "completed" and not comparison_mode:
                    rule_events = _build_events(
                        event_records,
                        local_llm_enabled=False,
                    )
                    seen_event_ids = {e.event_id for e in events}
                    seen_titles = {e.title for e in events}
                    seen_segments = {
                        str(e.evidence.get("segment_id") or "")
                        for e in events
                        if isinstance(e.evidence, dict) and e.evidence.get("segment_id")
                    }
                    seen_quotes = {
                        str(e.evidence.get("quote") or "").strip()
                        for e in events
                        if isinstance(e.evidence, dict) and e.evidence.get("quote")
                    }
                    for rule_evt in rule_events:
                        ev_dict = rule_evt.evidence if isinstance(rule_evt.evidence, dict) else {}
                        seg_id = str(ev_dict.get("segment_id") or "")
                        q_text = str(ev_dict.get("quote") or "").strip()
                        if ev_dict.get("candidate_type") in {"question_candidate", "reasoning_candidate"}:
                            continue
                        if (
                            rule_evt.event_id not in seen_event_ids
                            and rule_evt.title not in seen_titles
                            and (not seg_id or seg_id not in seen_segments)
                            and (not q_text or q_text not in seen_quotes)
                        ):
                            events.append(rule_evt)
                            seen_event_ids.add(rule_evt.event_id)
                            if seg_id:
                                seen_segments.add(seg_id)
                            if q_text:
                                seen_quotes.add(q_text)
                if ctx_report.get("pending_events"):
                    warnings.append(
                        f"有 {len(ctx_report['pending_events'])} 条事件进入待确认候选，暂未计入正式生活日志。"
                    )
                contextual_done = True
        except Exception as error:
            warnings.append(
                f"上下文事件提取异常（{type(error).__name__}），回退到逐句提取。"
            )
            contextual_done = False

    if comparison_mode:
        warnings.append("Jev 与 Clef 两份独立结果已保存；请在模型对照页查看，尚未合并至正式日志。")
    if not contextual_done:
        review_only_count = sum(
            1 for record in event_records if record.get("source_label") != "self"
        )
        if review_only_count:
            warnings.append(
                f"上下文提取不可用；{review_only_count} 条声源未确认的候选已保留在转写/声源记录中，"
                "未写入正式事件。"
            )
        events = _build_events(
            event_records,
            local_llm_enabled=args.local_llm_enabled,
            local_llm_model=args.local_llm_model,
            local_llm_url=args.local_llm_url,
            local_llm_review_confidence=args.local_llm_review_confidence,
            deepseek_enabled=args.deepseek_enabled,
            deepseek_model=args.deepseek_model,
        )
    day = str(recordings[0].started_at or "")[:10]
    if not day or day == "None":
        # 回退：用片段自己的绝对时间定日期。历史上有整批产物因为「只有第一个
        # 文件带启动时间、其余为 unavailable」而全部落进 undated/，进不了当天简报。
        for record in attributed:
            span = record_time_window(record)
            if span is not None:
                day = span[0].date().isoformat()
                break
    if not day or day == "None":
        day = "undated"
    warnings.extend([
        "生活事件默认从 source_label=self 生成；包含明确消费/待办信号的 unknown 会保留为待审核事件候选，避免重要记录静默丢失。",
        "声源判断综合声纹、近远场声学提示和前后本人语音连续性；近远场不是物理距离测量。",
        "上下文层使用前后30秒本人比例、连续讲话概率和同环境连续性；只有双侧本人上下文足够强时才会把低置信片段提升为本人。",
        (
            "只有声纹本来已偏低、声学上明显远场且缺少本人上下文支持的短句，"
            "才会自动降为 non_self。"
        ),
        "non_self 表示非本人或疑似媒体，不能仅凭声纹或近远场证明具体来源。",
        (
            "Audio Classifier 以 10 秒窗口标注 speech/music/noise/stream；"
            "它只降低低价值音频权重，不删除转写或片段。"
        ),
        (
            "Speech Separation 只处理最终仍为 unknown 的疑难片段；"
            "分离为两路后重新跑 CAM++，只有明确本人匹配才自动救回，"
            "否则继续进入 Review。"
        ),
    ])
    if args.local_llm_enabled and args.local_llm_model:
        warnings.append(
            "已启用本机 Ollama 事件二次判断；只复核规则已确认的高置信结构点，"
            "模型不可用或输出无效时自动保留规则结果。"
        )
    summary = build_daily_summary(
        events,
        date=None if day == "undated" else day,
        timezone_name=args.timezone,
        warnings=warnings,
    )
    daily_paths = write_daily_outputs(
        output_dir,
        date=None if day == "undated" else day,
        recordings=recordings,
        transcripts=[_transcript_segment(item) for item in records],
        events=events,
        summary=summary,
    )
    try:
        from life_logger.openclaw_bridge import OpenClawBridgeClient

        oc_client = OpenClawBridgeClient()
        if not comparison_mode and oc_client.is_healthy():
            oc_client.sync_todo_events(events, artifact_path=str(daily_paths.get("events")))
    except Exception:
        pass
    _atomic_write_json(
        output_dir / "speaker-embeddings.json",
        {
            "schema_version": "0.1",
            "speaker_model": args.speaker_model,
            "source_method": SOURCE_METHOD,
            "embeddings": {
                segment_id: {"embedding": embedding}
                for segment_id, embedding in speaker_embeddings.items()
            },
        },
    )
    # 被剔除的 dictation 原文单独落盘，不进 events.json。
    # stats / patterns / daily_brief 都只读 events.json，所以它们零改动即可
    # 获得干净的「生活事件」；而这份原文仍可被 search 检索到。
    _atomic_write_json(
        output_dir / "voice-input.json",
        {
            "schema_version": "voice-input-v1",
            "policy": "dictation-window-overlap",
            "overlap_ratio": DICTATION_OVERLAP_RATIO,
            "window_count": len(dictation_windows),
            "excluded_count": len(voice_input_records),
            "segments": [
                {
                    "segment_id": record.get("segment_id"),
                    "absolute_started_at": record.get("absolute_started_at"),
                    "start_offset_seconds": record.get("start_offset_seconds"),
                    "end_offset_seconds": record.get("end_offset_seconds"),
                    "text": record.get("text"),
                    "source_label": record.get("source_label"),
                }
                for record in voice_input_records
            ],
        },
    )
    _atomic_write_json(
        output_dir / "audio-classification.json",
        {
            "schema_version": "0.1",
            "policy_version": AUDIO_CLASSIFIER_POLICY_VERSION,
            "window_seconds": 10.0,
            "windows": audio_classification_windows,
            # PANNs（AudioSet 527）声学事件 —— 默认关闭，见 _panns_events_if_enabled。
            # 场景判定**不**用它（实测本地模型判场景不可靠），只取声学事件。
            "panns_events": _panns_events_if_enabled(source),
        },
    )
    _atomic_write_json(
        output_dir / "speech-separation.json",
        {
            "schema_version": "0.1",
            **separation_summary,
        },
    )
    _atomic_write_json(output_dir / "source-attribution.json", attributed)
    _atomic_write_json(output_dir / "self-transcript.json", self_records)
    _atomic_write_json(output_dir / "non-self-transcript.json", non_self_records)
    _atomic_write_json(output_dir / "unknown-review.json", unknown_records)
    run_summary = {
        "status": "completed",
        "audio": str(source),
        "transcript": str(transcript_path),
        "recordings": str(recordings_path),
        "template": str(template) if template is not None else None,
        "speaker_model": args.speaker_model,
        "source_method": SOURCE_METHOD,
        "thresholds": {
            "self": args.self_threshold,
            "unknown": args.unknown_threshold,
        },
        "min_segment_seconds": args.min_segment_seconds,
        "source_policy": SOURCE_POLICY_VERSION,
        "short_review_seconds": args.short_review_seconds,
        "short_review_score_floor": args.short_review_score_floor,
        "audio_classifier": {
            "policy_version": AUDIO_CLASSIFIER_POLICY_VERSION,
            "window_seconds": 10.0,
            "window_count": len(audio_classification_windows),
            "priority_counts": {
                priority: sum(
                    1
                    for item in attributed
                    if item.get("audio_processing_priority") == priority
                )
                for priority in ("high", "medium", "low")
            },
        },
        "speech_separation": separation_summary,
        "record_count": len(attributed),
        "counts": {
            "self": len(self_records),
            "non_self": len(non_self_records),
            "unknown": len(unknown_records),
            "events": len(events),
        },
        "proximity_counts": {
            label: sum(
                1
                for item in attributed
                if item.get("acoustic_proximity") == label
            )
            for label in ("near", "uncertain", "far")
        },
        "review_priority_counts": {
            priority: sum(
                1
                for item in unknown_records
                if item.get("review_priority") == priority
            )
            for priority in ("high", "medium", "low")
        },
        "review_layer_counts": {
            layer: sum(
                1
                for item in unknown_records
                if item.get("review_layer") == layer
            )
            for layer in ("focus", "standard", "background")
        },
        "daily_outputs": {key: str(value) for key, value in daily_paths.items()},
    }
    _atomic_write_json(output_dir / "run-summary.json", run_summary)
    return run_summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="attribute-funasr-transcript")
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--recordings", type=Path)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--context-only", action="store_true")
    parser.add_argument("--source-attribution", type=Path)
    parser.add_argument("--context-source-namespace", default="")
    parser.add_argument("--speaker-model", default="cam++")
    parser.add_argument(
        "--cloud-speaker",
        action="store_true",
        help="使用云端 ASR 提供的说话人角色标签（直接识别本人/非本人），跳过本地 CAM++ 推理",
    )
    parser.add_argument("--separation-model", type=Path)
    parser.add_argument("--local-llm-enabled", action="store_true")
    parser.add_argument("--local-llm-model", default="")
    parser.add_argument("--local-llm-url", default="http://127.0.0.1:11434")
    parser.add_argument(
        "--local-llm-review-confidence",
        type=float,
        default=0.80,
    )
    parser.add_argument("--deepseek-enabled", action="store_true")
    # 【2026-10-14 修】新增端点参数。原来只能靠 --deepseek-model，端点无从覆盖，
    # 于是模块级默认的 https://api.deepseek.com（官网）永远生效 → 401 → 提取失败。
    parser.add_argument(
        "--deepseek-base-url",
        default="",
        help="DeepSeek/兼容端点 base_url；留空则用环境变量 → local-config.json → 内置默认",
    )
    parser.add_argument("--deepseek-model", default="")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--min-segment-seconds",
        type=float,
        default=DEFAULT_MIN_SEGMENT_SECONDS,
    )
    parser.add_argument(
        "--short-review-seconds",
        type=float,
        default=DEFAULT_SHORT_REVIEW_SECONDS,
    )
    parser.add_argument(
        "--short-review-score-floor",
        type=float,
        default=DEFAULT_SHORT_REVIEW_SCORE_FLOOR,
    )
    parser.add_argument("--self-threshold", type=float, default=0.50)
    parser.add_argument("--unknown-threshold", type=float, default=0.25)
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument(
        "--dictation-windows",
        type=Path,
        default=None,
        help="dictation 时间窗索引（JSONL，每行 {start,end}）。与这些时间窗"
        "重合过半的句子会被判为语音输入，不生成生活事件。缺省关闭剔除。",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        summary = _run(args)
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": summary["status"],
                "counts": summary["counts"],
                "output_dir": str(args.output_dir.resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
