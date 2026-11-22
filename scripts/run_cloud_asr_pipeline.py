"""Run cloud-backed ASR pipeline (Doubao / Gemini) with Life Logger standard artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from life_logger.audio_classifier import AUDIO_CLASSIFIER_RECORD_FIELDS
from life_logger.domain import RecordingSegment, TranscriptSegment
from life_logger.ingest import discover_recordings
from life_logger.local_pipeline import load_local_config
from life_logger.timeutil import local_clock, local_iso
from scripts.run_full_funasr_pipeline import _build_daily_outputs, _probe_duration


def _local_time_fields(value: Any) -> dict[str, str | None]:
    """Return human-readable Shanghai fields while leaving the UTC value intact."""
    return {
        "started_at_local": local_iso(value),
        "clock_local": local_clock(value),
    }


def _transcript_local_time_fields(
    transcript: TranscriptSegment,
) -> dict[str, str | None]:
    return _local_time_fields(transcript.absolute_started_at)


def _annotate_transcript_file_local_times(transcript_path: Path) -> int:
    """Add local readable time fields to the serialized daily transcript."""
    try:
        rows = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(rows, list):
        return 0
    annotated = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        fields = _local_time_fields(row.get("absolute_started_at"))
        changed = False
        for key, value in fields.items():
            if row.get(key) is None:
                row[key] = value
                changed = True
        if changed:
            annotated += 1
    if annotated:
        transcript_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return annotated


def _merge_attribution_into_transcript(
    transcript_path: Path,
    attributions: dict[str, dict[str, Any]],
) -> int:
    """把段级声纹证据并入日目录 transcript.json（下游 attribute 阶段读的就是它）。

    ``render.write_daily_outputs`` 只写 TranscriptSegment 的固定字段，会把
    voice_similarity/source_label/近场证据丢掉 —— 而 watch 正式 ASR 路径
    （watch_formal_asr）是把这些字段直接写进 transcript.json 的。这里补上同样
    的一步，否则 MOSS 转录出来的证据走不到声源归属阶段，下游只能退回默认 self。
    """
    try:
        rows = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(rows, list):
        return 0
    merged = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        attr = attributions.get(str(row.get("segment_id") or ""))
        if not isinstance(attr, dict):
            continue
        for key in (
            "source_label",
            "voice_similarity",
            "acoustic_proximity",
            "review_required",
            "decision_reason",
            "proximity_score",
            "proximity_confidence",
            "snr_proxy_db",
            "proximity_ratio_db",
            *AUDIO_CLASSIFIER_RECORD_FIELDS,
            # 【2026-10-14 补】CAM++ 声纹落地时新增的可观测字段。子代理当时因该文件有
            # 他人未提交改动而没动白名单 → 新字段只进了 stats["attributions"]，
            # 落不到 transcript.json，看板/排障看不到「用的是哪个后端、什么粒度、属于哪个说话人组」。
            # 这几个字段是排查「声纹到底跑没跑、判据从哪来」的唯一入口，必须落盘。
            "attribution_granularity",   # "speaker_group" | "span"
            "speaker_group_key",         # 该 span 所属的 (chunk, MOSS 说话人) 组键
            "speaker_group_spans",       # 组内 span 数
            "speaker_group_seconds",     # 组合并后的音频时长（秒）
        ):
            if key in attr:
                row[key] = attr[key]
        merged += 1
    if merged:
        transcript_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Cloud ASR pipeline")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timezone", type=str, default="Asia/Shanghai")
    parser.add_argument("--start-at", type=str, default=None)
    parser.add_argument("--provider", type=str, default="local_turbo")
    # Compatibility arguments
    parser.add_argument("--model-path", type=str, default="")
    parser.add_argument("--vad-model", type=str, default="")
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--include-events", action="store_true")

    args = parser.parse_args()

    audio_path = args.audio.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    config_path = REPO_ROOT / "local-config.json"
    config: dict[str, Any] = {}
    if config_path.exists():
        try:
            config = load_local_config(config_path)
        except Exception:
            pass

    raw_provider = str(args.provider or config.get("asr_provider", "local_turbo")).lower()
    if raw_provider in {"doubao", "volcano"}:
        provider = "doubao"
    elif raw_provider in {"moss", "moss_transcribe_diarize", "moss-td"}:
        provider = "moss"
    else:
        provider = "local_turbo"
    duration_s = round(_probe_duration(audio_path) or 0.0, 3)

    user_start = None
    if args.start_at:
        try:
            user_start = datetime.fromisoformat(args.start_at)
        except Exception:
            pass

    recordings = discover_recordings(
        audio_path,
        timezone_name=args.timezone,
        user_start=user_start,
    )
    if not recordings:
        recording = RecordingSegment(
            recording_id=f"rec_{audio_path.stem}",
            source_file=str(audio_path),
            started_at=args.start_at or datetime.now().astimezone().isoformat(),
            ended_at=None,
            duration_seconds=duration_s,
            sequence=1,
            time_source="user",
            time_precision="exact",
            ingestion_warnings=[],
        )
    else:
        recording = recordings[0]

    transcripts: list[TranscriptSegment] = []
    # MOSS 段级声纹归属（voice_similarity/source_label/近场…），按 segment_id 对齐。
    segment_attributions: dict[str, dict[str, Any]] = {}
    if provider == "doubao":
        from life_logger.transcribe import DoubaoASRTranscriber

        doubao_cfg = config.get("doubao_asr", {}) if isinstance(config.get("doubao_asr"), dict) else {}
        transcriber = DoubaoASRTranscriber(
            appid=doubao_cfg.get("appid", "5261675940"),
            access_token=doubao_cfg.get("access_token", "LNhV_nDmgO1SECuGpCwQk4Dx2RDZqBiy"),
            resource_id=doubao_cfg.get("resource_id", "volc.bigasr.auc_turbo"),
            enable_speaker_info=doubao_cfg.get("enable_speaker_info", True),
            ssd_version=doubao_cfg.get("ssd_version", "300"),
            enable_ddc=doubao_cfg.get("enable_ddc", True),
            enable_itn=doubao_cfg.get("enable_itn", True),
            enable_punc=doubao_cfg.get("enable_punc", True),
            enable_gender_detection=doubao_cfg.get("enable_gender_detection", True),
            enable_emotion_detection=doubao_cfg.get("enable_emotion_detection", True),
            voiceprints=doubao_cfg.get("voiceprints"),
            context=doubao_cfg.get("context"),
        )
        model_name = "Doubao-Flash-ASR"
        transcripts = transcriber.transcribe(recording)
    elif provider == "moss":
        print("使用 MOSS-Transcribe-Diarize 0.9B 转写（自带说话人 + 句级时间戳）...", flush=True)
        from datetime import timedelta as _td
        from dataclasses import replace
        from life_logger.moss_transcribe import transcribe_file as _moss_transcribe

        _hot = config.get("moss_hotwords")
        transcripts, _moss_stats = _moss_transcribe(
            audio_path,
            recording_id=recording.recording_id,
            hotwords=_hot if isinstance(_hot, str) and _hot.strip() else None,
        )
        # attributions 可能很大（每条转写一条），日志里只打摘要
        _moss_log = {
            key: value for key, value in _moss_stats.items() if key != "attributions"
        }
        print(f"  MOSS: {_moss_log}", flush=True)
        _attr_map = _moss_stats.get("attributions")
        if isinstance(_attr_map, dict):
            segment_attributions = {
                str(key): value for key, value in _attr_map.items() if isinstance(value, dict)
            }
        model_name = "MOSS-Transcribe-Diarize-0.9B"
        # 补绝对时间：以该 recording 的起点为基准
        _base = None
        try:
            _base = datetime.fromisoformat(str(recording.started_at).replace("Z", "+00:00"))
        except Exception:
            _base = None
        if _base is not None:
            _filled = []
            for _s in transcripts:
                _st = _base + _td(seconds=float(_s.start_offset_seconds or 0.0))
                _en = _base + _td(seconds=float(_s.end_offset_seconds or 0.0))
                _filled.append(replace(
                    _s,
                    absolute_started_at=_st.isoformat(),
                    absolute_ended_at=_en.isoformat(),
                ))
            transcripts = _filled
    else:
        print("使用本地 MLX Whisper Large-v3-Turbo 引擎转写...", flush=True)
        from life_logger.transcribe import get_shared_turbo_transcriber
        local_transcriber = get_shared_turbo_transcriber()
        model_name = "Local-Whisper-Turbo-MLX"
        transcripts = local_transcriber.transcribe(recording)

    transcript_records = []
    for t in transcripts:
        record = {
            "segment_id": t.segment_id,
            "recording_id": t.recording_id,
            "start_offset_seconds": t.start_offset_seconds,
            "end_offset_seconds": t.end_offset_seconds,
            "absolute_started_at": t.absolute_started_at,
            "absolute_ended_at": t.absolute_ended_at,
            "text": t.text,
            "source": t.source,
            "language": t.language,
            "transcription_confidence": t.transcription_confidence,
            "speaker": t.speaker,
            "gender": t.gender,
            "emotion": t.emotion,
            **_transcript_local_time_fields(t),
        }
        # 段级声纹证据并入记录：下游 attribute_funasr_transcript 见到
        # acoustic_proximity / source_label 会直接复用，不再退回默认 self。
        attr = segment_attributions.get(t.segment_id) or {}
        for attr_key in (
            "source_label",
            "voice_similarity",
            "acoustic_proximity",
            "review_required",
            "decision_reason",
            "proximity_score",
            "proximity_confidence",
            "snr_proxy_db",
            "proximity_ratio_db",
            *AUDIO_CLASSIFIER_RECORD_FIELDS,
            # 【2026-10-14 补】CAM++ 声纹落地时新增的可观测字段。子代理当时因该文件有
            # 他人未提交改动而没动白名单 → 新字段只进了 stats["attributions"]，
            # 落不到 transcript.json，看板/排障看不到「用的是哪个后端、什么粒度、属于哪个说话人组」。
            # 这几个字段是排查「声纹到底跑没跑、判据从哪来」的唯一入口，必须落盘。
            "attribution_granularity",   # "speaker_group" | "span"
            "speaker_group_key",         # 该 span 所属的 (chunk, MOSS 说话人) 组键
            "speaker_group_spans",       # 组内 span 数
            "speaker_group_seconds",     # 组合并后的音频时长（秒）
        ):
            if attr_key in attr:
                record[attr_key] = attr[attr_key]
        transcript_records.append(record)

    warnings: list[str] = list((_moss_stats.get("warnings") or []) if provider == "moss" else [])
    daily = _build_daily_outputs(
        output_dir=output_dir,
        source=audio_path,
        transcript_records=transcript_records,
        start_at=user_start,
        timezone_name=args.timezone,
        model_path=model_name,
        warnings=warnings,
        include_events=args.include_events,
    )

    # 段级声纹证据写回日目录 transcript.json（下游声源归属阶段直接复用）
    daily_transcript = (daily.get("paths") or {}).get("transcript")
    if daily_transcript:
        annotated = _annotate_transcript_file_local_times(Path(str(daily_transcript)))
        if annotated:
            print(
                f"  transcript.json 已补本地时间字段：{annotated} 段",
                flush=True,
            )
    if segment_attributions and daily_transcript:
        merged = _merge_attribution_into_transcript(
            Path(str(daily_transcript)), segment_attributions
        )
        if merged:
            print(f"  声纹归属已写入 transcript.json：{merged} 段", flush=True)
            warnings.append(
                "voiceprint attribution merged into "
                f"{Path(str(daily_transcript)).name}: {merged} segments"
            )

    summary = {
        "status": "completed",
        "source": str(audio_path),
        "model": model_name,
        "model_path": model_name,
        "duration_s": duration_s,
        "transcript_count": len(transcript_records),
        "warnings": warnings,
        "event_source_policy": daily["event_source_policy"],
        "daily_outputs": daily,
    }

    (output_dir / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "source": str(audio_path),
                "model_path": model_name,
                "duration_s": duration_s,
                "transcripts": transcript_records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
