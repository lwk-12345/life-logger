"""Transcript sources: demo, sidecar files, and optional local Whisper."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

from life_logger.audio_classifier import (
    annotate_record_with_window_classification,
)
from life_logger.domain import RecordingSegment, TranscriptSegment

# 【2026-10-14 · codex 审查 P1】混合语音归属的置信门槛（秒）。
#
# `LocalTurboCompositeTranscriber` 把 VAD span 合并成**最长 15 秒**的窗（间隔 ≤1.2s 即并入），
# 整窗**只算一次声纹**，再把同一个归属复制给窗内每个 ASR 段。
# 窗越长、越可能同时包含本人和别人（外放视频、同房间的对话），
# 此时「整窗一个归属」就不是可靠的身份结论 —— 别人的话会被贴成本人的，且看不出异常。
#
# 为什么不能拆成说话人轮次：这条路径走 whisper，**没有 diarization**，拆不出来。
# 所以选择「诚实标注」：超过本阈值即强制 `review_required=True`，把不确定交给人工。
MIXED_SPEAKER_WINDOW_SECONDS = 5.0


class Transcriber(Protocol):
    def transcribe(
        self,
        recording: RecordingSegment,
        sidecar_path: Path | None = None,
    ) -> list[TranscriptSegment]:
        """Return timestamped transcript segments for one recording."""


def _absolute_time(recording: RecordingSegment, offset: float) -> str | None:
    if recording.started_at is None:
        return None
    start = datetime.fromisoformat(recording.started_at)
    return (start + timedelta(seconds=offset)).isoformat()


def _segments_from_payload(
    recording: RecordingSegment,
    payload: Any,
    *,
    source: str,
    offset_seconds: float = 0.0,
    segment_index_offset: int = 0,
) -> list[TranscriptSegment]:
    result: list[TranscriptSegment] = []
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        raw_items = payload.get("segments", []) or payload.get("transcript", [])
    else:
        raw_items = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("transcript") or item.get("sentence") or "").strip()
        if not text:
            continue
        local_start = float(item.get("start", 0.0))
        local_end = max(float(item.get("end", local_start)), local_start)
        start = offset_seconds + local_start
        end = offset_seconds + local_end
        result.append(
            TranscriptSegment(
                segment_id=(
                    f"{recording.recording_id}_seg_{segment_index_offset + index:04d}"
                ),
                recording_id=recording.recording_id,
                start_offset_seconds=start,
                end_offset_seconds=end,
                absolute_started_at=_absolute_time(recording, start),
                absolute_ended_at=_absolute_time(recording, end),
                text=text,
                source=source,
                language=str(item.get("language", "zh")),
                transcription_confidence=item.get("confidence"),
            )
        )
    return result


class DemoTranscriber:
    def __init__(self, fixture_path: Path) -> None:
        self.fixture_path = fixture_path

    def transcribe(
        self,
        recording: RecordingSegment,
        sidecar_path: Path | None = None,
    ) -> list[TranscriptSegment]:
        del sidecar_path
        payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        return _segments_from_payload(recording, payload, source="demo")


class JsonSidecarTranscriber:
    def transcribe(
        self,
        recording: RecordingSegment,
        sidecar_path: Path | None = None,
    ) -> list[TranscriptSegment]:
        path = sidecar_path or Path(recording.source_file).with_suffix(".json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            payload = {"segments": payload}
        return _segments_from_payload(recording, payload, source="sidecar")


def _parse_clock(value: str) -> float:
    normalized = value.strip().replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return float(minutes) * 60 + float(seconds)
    hours, minutes, seconds = parts
    return float(hours) * 3600 + float(minutes) * 60 + float(seconds)


class SrtVttSidecarTranscriber:
    _cue_pattern = re.compile(
        r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3})"
        r"\s*-->\s*"
        r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3})"
    )

    def transcribe(
        self,
        recording: RecordingSegment,
        sidecar_path: Path | None = None,
    ) -> list[TranscriptSegment]:
        path = sidecar_path or Path(recording.source_file).with_suffix(".srt")
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        cues: list[tuple[float, float, str]] = []
        current: tuple[float, float] | None = None
        text_lines: list[str] = []

        def flush() -> None:
            nonlocal current, text_lines
            if current and text_lines:
                cues.append((current[0], current[1], " ".join(text_lines).strip()))
            current = None
            text_lines = []

        for line in lines + [""]:
            match = self._cue_pattern.search(line)
            if match:
                flush()
                current = (
                    _parse_clock(match.group("start")),
                    _parse_clock(match.group("end")),
                )
                continue
            if not line.strip():
                flush()
                continue
            if current is not None and not line.strip().isdigit():
                text_lines.append(line.strip())
        payload = {
            "segments": [
                {"start": start, "end": end, "text": text} for start, end, text in cues
            ]
        }
        return _segments_from_payload(recording, payload, source="sidecar")


class TextSidecarTranscriber:
    def transcribe(
        self,
        recording: RecordingSegment,
        sidecar_path: Path | None = None,
    ) -> list[TranscriptSegment]:
        path = sidecar_path or Path(recording.source_file).with_suffix(".txt")
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
        payload = {
            "segments": [
                {"start": index, "end": index + 1, "text": line}
                for index, line in enumerate(line for line in lines if line)
            ]
        }
        return _segments_from_payload(recording, payload, source="sidecar")


def select_sidecar_transcriber(path: Path) -> Transcriber:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return JsonSidecarTranscriber()
    if suffix in {".srt", ".vtt"}:
        return SrtVttSidecarTranscriber()
    if suffix == ".txt":
        return TextSidecarTranscriber()
    raise ValueError(f"unsupported sidecar format: {path.suffix}")


def _probe_duration(path: str | Path) -> float | None:
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
                timeout=60,
    )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _render_audio_chunk(
    source: str,
    start: float,
    duration: float,
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
                str(start),
                "-i",
                source,
                "-t",
                str(duration),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(destination),
            ],
            check=True,
            capture_output=True,
            text=True,
                timeout=600,
    )
    except FileNotFoundError as error:
        raise RuntimeError("chunked local ASR requires ffmpeg on PATH") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or "unknown ffmpeg error"
        raise RuntimeError(f"could not render audio chunk: {detail}") from error


def _transcript_from_dict(payload: dict[str, Any]) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=payload["segment_id"],
        recording_id=payload["recording_id"],
        start_offset_seconds=float(payload["start_offset_seconds"]),
        end_offset_seconds=float(payload["end_offset_seconds"]),
        absolute_started_at=payload.get("absolute_started_at"),
        absolute_ended_at=payload.get("absolute_ended_at"),
        text=payload["text"],
        source=payload["source"],
        language=payload.get("language", "zh"),
        transcription_confidence=payload.get("transcription_confidence"),
    )


def _write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _load_checkpoint(
    path: Path,
    *,
    recording: RecordingSegment,
    model_name: str,
    chunk_seconds: float,
) -> tuple[int, list[TranscriptSegment]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, []
    if (
        payload.get("recording_id") != recording.recording_id
        or payload.get("source_file") != recording.source_file
        or payload.get("model_name") != model_name
        or payload.get("chunk_seconds") != chunk_seconds
    ):
        return 0, []
    segments = [_transcript_from_dict(item) for item in payload.get("segments", [])]
    return int(payload.get("completed_chunks", 0)), segments


class LocalWhisperTranscriber:
    def __init__(
        self,
        *,
        model_name: str,
        device: str = "cpu",
        compute_type: str = "int8",
        require_dependency: bool = True,
        chunk_seconds: float = 300.0,
        cpu_threads: int = 2,
        checkpoint_dir: Path | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        if chunk_seconds <= 0:
            raise ValueError("chunk_seconds must be greater than zero")
        if cpu_threads <= 0:
            raise ValueError("cpu_threads must be greater than zero")
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.require_dependency = require_dependency
        self.chunk_seconds = chunk_seconds
        self.cpu_threads = cpu_threads
        self.checkpoint_dir = checkpoint_dir
        self.progress_callback = progress_callback
        self._model = None

    def _load_model(self, *, force_missing: bool = False):
        if force_missing:
            raise RuntimeError(
                "Local ASR requires faster-whisper; install with "
                "python -m pip install -e '.[local-asr]'"
            )
        try:
            from faster_whisper import WhisperModel
        except ImportError as error:
            if self.require_dependency:
                raise RuntimeError(
                    "Local ASR requires faster-whisper; install with "
                    "python -m pip install -e '.[local-asr]'"
                ) from error
            return None
        self._model = WhisperModel(
            self.model_name,
            device=self.device,
            compute_type=self.compute_type,
            cpu_threads=self.cpu_threads,
        )
        return self._model

    def _transcribe_file(
        self,
        model: Any,
        recording: RecordingSegment,
        path: str | Path,
        *,
        offset_seconds: float = 0.0,
        segment_index_offset: int = 0,
    ) -> list[TranscriptSegment]:
        segments, _info = model.transcribe(
            str(path),
            language="zh",
            vad_filter=True,
        )
        payload = {
            "segments": [
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                    "confidence": None,
                }
                for segment in segments
            ]
        }
        return _segments_from_payload(
            recording,
            payload,
            source="local_whisper",
            offset_seconds=offset_seconds,
            segment_index_offset=segment_index_offset,
        )

    def _checkpoint_path(self, recording: RecordingSegment) -> Path | None:
        if self.checkpoint_dir is None:
            return None
        return self.checkpoint_dir / f"{recording.recording_id}.json"

    def _transcribe_in_chunks(
        self,
        model: Any,
        recording: RecordingSegment,
        duration: float,
    ) -> list[TranscriptSegment]:
        total_chunks = max(1, math.ceil(duration / self.chunk_seconds))
        checkpoint_path = self._checkpoint_path(recording)
        completed_chunks = 0
        result: list[TranscriptSegment] = []
        if checkpoint_path is not None and checkpoint_path.exists():
            completed_chunks, result = _load_checkpoint(
                checkpoint_path,
                recording=recording,
                model_name=self.model_name,
                chunk_seconds=self.chunk_seconds,
            )
            completed_chunks = min(completed_chunks, total_chunks)
        if self.progress_callback is not None:
            self.progress_callback(completed_chunks, total_chunks)

        with tempfile.TemporaryDirectory(prefix="life-logger-chunk-") as directory:
            for chunk_index in range(completed_chunks, total_chunks):
                start = chunk_index * self.chunk_seconds
                length = min(self.chunk_seconds, duration - start)
                destination = Path(directory) / f"chunk-{chunk_index:06d}.wav"
                _render_audio_chunk(
                    recording.source_file,
                    start,
                    length,
                    destination,
                )
                chunk_segments = self._transcribe_file(
                    model,
                    recording,
                    destination,
                    offset_seconds=start,
                    segment_index_offset=len(result),
                )
                result.extend(chunk_segments)
                completed_chunks = chunk_index + 1
                if checkpoint_path is not None:
                    _write_checkpoint(
                        checkpoint_path,
                        {
                            "recording_id": recording.recording_id,
                            "source_file": recording.source_file,
                            "model_name": self.model_name,
                            "chunk_seconds": self.chunk_seconds,
                            "completed_chunks": completed_chunks,
                            "segments": [item.to_dict() for item in result],
                        },
                    )
                if self.progress_callback is not None:
                    self.progress_callback(completed_chunks, total_chunks)
        return result

    def transcribe(
        self,
        recording: RecordingSegment,
        sidecar_path: Path | None = None,
    ) -> list[TranscriptSegment]:
        del sidecar_path
        model = self._model or self._load_model()
        if model is None:
            raise RuntimeError("local Whisper model could not be loaded")
        duration = _probe_duration(recording.source_file)
        if duration is None:
            raise RuntimeError("local ASR requires ffprobe to determine audio duration")
        if duration > self.chunk_seconds:
            return self._transcribe_in_chunks(model, recording, duration)
        return self._transcribe_file(model, recording, recording.source_file)


class DoubaoASRTranscriber:
    """Cloud ASR transcriber powered by Volcano Doubao Flash Speech API."""

    def __init__(
        self,
        *,
        appid: str = "5261675940",
        access_token: str = "LNhV_nDmgO1SECuGpCwQk4Dx2RDZqBiy",
        resource_id: str = "volc.bigasr.auc_turbo",
        endpoint: str = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
        enable_speaker_info: bool = True,
        ssd_version: str = "300",
        enable_ddc: bool = True,
        enable_itn: bool = True,
        enable_punc: bool = True,
        enable_gender_detection: bool = True,
        enable_emotion_detection: bool = True,
        voiceprints: list[dict[str, str]] | None = None,
        context: Any = None,
    ) -> None:
        self.appid = appid
        self.access_token = access_token
        self.resource_id = resource_id
        self.endpoint = endpoint
        self.enable_speaker_info = enable_speaker_info
        self.ssd_version = ssd_version
        self.enable_ddc = enable_ddc
        self.enable_itn = enable_itn
        self.enable_punc = enable_punc
        self.enable_gender_detection = enable_gender_detection
        self.enable_emotion_detection = enable_emotion_detection
        self.voiceprints = voiceprints
        self.context = context

    def transcribe(
        self,
        recording: RecordingSegment,
        sidecar_path: Path | None = None,
    ) -> list[TranscriptSegment]:
        del sidecar_path
        import base64
        import urllib.request
        import uuid

        audio_path = Path(recording.source_file)
        fmt = audio_path.suffix.lstrip(".").lower()
        if fmt not in {"wav", "mp3", "m4a", "ogg", "opus"}:
            fmt = "m4a"

        audio_bytes = audio_path.read_bytes()
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

        req_id = str(uuid.uuid4())
        headers = {
            "Content-Type": "application/json",
            "X-Api-App-Key": self.appid,
            "X-Api-Access-Key": self.access_token,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": req_id,
            "X-Api-Sequence": "-1",
        }
        request_payload: dict[str, Any] = {
            "model_name": "bigmodel",
            "enable_itn": self.enable_itn,
            "enable_punc": self.enable_punc,
            "enable_ddc": self.enable_ddc,
            "enable_speaker_info": self.enable_speaker_info,
            "show_utterances": True,
        }
        if self.enable_speaker_info:
            request_payload["ssd_version"] = self.ssd_version
        if self.enable_gender_detection:
            request_payload["enable_gender_detection"] = True
        if self.enable_emotion_detection:
            request_payload["enable_emotion_detection"] = True
        if self.context:
            request_payload["context"] = self.context
        if self.voiceprints:
            request_payload["voiceprints"] = self.voiceprints

        body = json.dumps({
            "user": {"uid": "life_logger_user"},
            "audio": {"format": fmt, "data": audio_b64},
            "request": request_payload,
        }).encode("utf-8")

        req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        res = data.get("result", {})
        raw_utterances = res.get("utterances", [])

        result: list[TranscriptSegment] = []
        if raw_utterances:
            for index, u in enumerate(raw_utterances, start=1):
                text = str(u.get("text", "")).strip()
                if not text:
                    continue
                start_s = float(u.get("start_time", 0)) / 1000.0
                end_s = max(float(u.get("end_time", start_s)) / 1000.0, start_s)
                additions = u.get("additions") or {}
                speaker_id = additions.get("speaker") or u.get("speaker_id") or u.get("speaker")
                gender = additions.get("gender") or u.get("gender")
                emotion = additions.get("emotion") or u.get("emotion")
                words = u.get("words")
                result.append(
                    TranscriptSegment(
                        segment_id=f"{recording.recording_id}_doubao_{index:04d}",
                        recording_id=recording.recording_id,
                        start_offset_seconds=start_s,
                        end_offset_seconds=end_s,
                        absolute_started_at=_absolute_time(recording, start_s),
                        absolute_ended_at=_absolute_time(recording, end_s),
                        text=text,
                        source="doubao_asr",
                        language="zh",
                        transcription_confidence=None,
                        speaker=str(speaker_id) if speaker_id is not None else None,
                        gender=str(gender) if gender is not None else None,
                        emotion=str(emotion) if emotion is not None else None,
                        words=words,
                    )
                )
        else:
            fallback_text = str(res.get("text", "")).strip()
            if fallback_text:
                duration_s = float(data.get("audio_info", {}).get("duration", 0)) / 1000.0
                if duration_s <= 0:
                    duration_s = _probe_duration(recording.source_file) or 0.0
                result.append(
                    TranscriptSegment(
                        segment_id=f"{recording.recording_id}_doubao_0001",
                        recording_id=recording.recording_id,
                        start_offset_seconds=0.0,
                        end_offset_seconds=duration_s,
                        absolute_started_at=_absolute_time(recording, 0.0),
                        absolute_ended_at=_absolute_time(recording, duration_s),
                        text=fallback_text,
                        source="doubao_asr",
                        language="zh",
                        transcription_confidence=None,
                    )
                )
        return result


class LocalTurboCompositeTranscriber:
    """Composite local ASR engine:
    1. Primary ASR: MLX Whisper Large-v3-Turbo (mlx-community/whisper-large-v3-turbo)
    2. Speaker & proximity scoring: acoustic proximity + rolling voiceprint pool
    """

    def __init__(
        self,
        *,
        whisper_model_path: str = "mlx-community/whisper-large-v3-turbo",
        sensevoice_model_path: str = "",
        campplus_model_path: str = "",
        enable_emotion: bool = False,
        enable_speaker: bool = True,
        cpu_threads: int = 4,
    ) -> None:
        self.whisper_model_path = whisper_model_path
        self.sensevoice_model_path = sensevoice_model_path
        self.campplus_model_path = campplus_model_path
        self.enable_emotion = enable_emotion
        self.enable_speaker = enable_speaker
        self.cpu_threads = cpu_threads
        self._rolling_pool = None
        self._last_segment_attributions: dict[str, dict[str, Any]] = {}
        import threading
        self._infer_lock = threading.Lock()

    @staticmethod
    def _extract_numpy_voiceprint(wav_arr: Any) -> list[float] | None:
        """Pure NumPy 48-dim log-spectrum + cepstral + harmonic voiceprint (zero FunASR dependency)."""
        import numpy as np
        arr = np.asarray(wav_arr, dtype=np.float32).reshape(-1)
        if len(arr) < 3200:
            return None
        # Pre-emphasis (1 - 0.97 z^-1) to remove universal human glottal tilt (-6 dB/oct)
        arr = np.append(arr[0], arr[1:] - 0.97 * arr[:-1]).astype(np.float32)
        frame_len = 512
        hop = 256
        if len(arr) < frame_len:
            return None
        n_frames = 1 + (len(arr) - frame_len) // hop
        idx = np.arange(frame_len)[None, :] + np.arange(n_frames)[:, None] * hop
        frames = arr[idx]
        energy = np.mean(frames * frames, axis=1)
        voiced = frames[energy > max(float(np.percentile(energy, 35)), 1e-7)]
        if len(voiced) == 0:
            voiced = frames
        win = np.hanning(frame_len).astype(np.float32)
        spec = np.abs(np.fft.rfft(voiced * win, axis=1)) + 1e-7
        log_spec = np.log(spec)
        # 32 log-frequency bands (80Hz - 7600Hz) + higher-order cepstral coefficients (C2..C17)
        n_bins = log_spec.shape[1]
        band_edges = np.linspace(2, n_bins - 1, 33, dtype=int)
        bands = [
            np.mean(log_spec[:, band_edges[i] : max(band_edges[i] + 1, band_edges[i + 1])], axis=1)
            for i in range(32)
        ]
        band_mat = np.stack(bands, axis=1)
        mean_vec = np.mean(band_mat, axis=0)
        # Remove linear tilt across bands so only formant peaks/valleys remain
        x_axis = np.linspace(-1.0, 1.0, 32, dtype=np.float32)
        slope = float(np.dot(mean_vec, x_axis) / np.dot(x_axis, x_axis))
        detrended = mean_vec - np.mean(mean_vec) - slope * x_axis
        cep = np.fft.irfft(detrended, n=64)[2:18]
        feat = np.concatenate([detrended, cep * 2.0]).astype(np.float32)
        norm = float(np.linalg.norm(feat))
        if norm <= 1e-7:
            return None
        return (feat / norm).tolist()

    def _get_rolling_pool(self):
        if self._rolling_pool is None and self.enable_speaker:
            try:
                from faster_whisper.audio import decode_audio
                from life_logger.source_attribution import RollingVoiceprintPool
                repo_root = Path(__file__).resolve().parents[2]
                template_path = repo_root / "work/watch-2026-10-01/template-from-watch-self.wav"
                cfg_path = repo_root / "local-config.json"
                if cfg_path.is_file():
                    try:
                        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                        if cfg.get("template"):
                            template_path = Path(str(cfg["template"]))
                    except Exception:
                        pass
                if template_path.is_file():
                    tpl_wav = decode_audio(str(template_path), sampling_rate=16000)
                    emb = self._extract_numpy_voiceprint(tpl_wav)
                    if emb is not None:
                        self._rolling_pool = RollingVoiceprintPool.from_template(emb)
            except Exception:
                self._rolling_pool = None
        return self._rolling_pool

    def _score_window_speaker_and_proximity(
        self, chunk_wav: Any, noise_floor_dbfs: float
    ) -> dict[str, Any]:
        import math
        import numpy as np
        from life_logger.source_attribution import (
            AttributionConfig,
            apply_multi_evidence_policy,
            classify_acoustic_proximity,
            classify_similarity,
            proximity_adjusted_similarity,
            protect_short_low_similarity,
        )

        arr = np.asarray(chunk_wav, dtype=np.float32)
        duration_s = len(arr) / 16000.0
        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) > 0 else 0.0
        speech_rms_dbfs = 20.0 * math.log10(max(rms, 1e-6))
        snr_db = speech_rms_dbfs - noise_floor_dbfs

        # Proximity Effect low-frequency (80-320Hz vs 1000-3500Hz) resonance ratio
        proximity_ratio_db = 0.0
        if len(arr) >= 1600:
            spec = np.abs(np.fft.rfft(arr)) ** 2
            freqs = np.fft.rfftfreq(len(arr), d=1.0 / 16000.0)
            low_mask = (freqs >= 80.0) & (freqs <= 320.0)
            mid_mask = (freqs >= 1000.0) & (freqs <= 3500.0)
            low_e = float(np.mean(spec[low_mask])) if np.any(low_mask) else 1e-9
            mid_e = float(np.mean(spec[mid_mask])) if np.any(mid_mask) else 1e-9
            proximity_ratio_db = 10.0 * math.log10(max(low_e, 1e-9) / max(mid_e, 1e-9)) - 6.0

        prox_info = classify_acoustic_proximity(
            speech_rms_dbfs=speech_rms_dbfs,
            noise_floor_dbfs=noise_floor_dbfs,
            relative_loudness_db=snr_db - 10.0,
            proximity_ratio_db=proximity_ratio_db,
        )

        similarity: float | None = None
        review_required_forced = False
        pool = self._get_rolling_pool()
        if pool is not None and len(arr) >= 3200:
            try:
                cand_emb = self._extract_numpy_voiceprint(arr)
                if cand_emb is not None:
                    raw_sim = float(pool.score_candidate(cand_emb))
                    prox_label = str(prox_info.get("acoustic_proximity") or "uncertain")
                    # 远场只降权 + 标复核，不再封顶 0.18（封顶 = 远场一票否决身份）
                    similarity, review_required_forced = proximity_adjusted_similarity(
                        raw_sim,
                        prox_label=prox_label,
                        proximity_ratio_db=proximity_ratio_db,
                    )
                    if prox_label == "near" and proximity_ratio_db >= 3.0:
                        pool.maybe_update(
                            cand_emb, similarity, proximity_ratio_db=proximity_ratio_db
                        )
            except Exception:
                similarity = None

        cfg = AttributionConfig()
        if similarity is not None:
            dec = protect_short_low_similarity(
                classify_similarity(similarity, cfg),
                duration_seconds=duration_s,
            )
            raw_record = {
                "segment_id": "win",
                "start_offset_seconds": 0.0,
                "end_offset_seconds": duration_s,
                "source_label": dec.label,
                "voice_similarity": round(similarity, 6),
                "review_required": bool(
                    dec.is_review_required or review_required_forced
                ),
                "decision_reason": dec.reason,
                **prox_info,
            }
            fused = apply_multi_evidence_policy([raw_record], cfg)[0]
            return fused
        # 【2026-10-14 修 · codex 审查 P2】声纹不可用时**不再按远近场判身份**。
        #
        # 旧逻辑：
        #   "source_label": "self" if prox == "near" else ("non_self" if prox == "far" else "unknown")
        #   "review_required": prox == "uncertain"
        # → 近场直接判 `self` 且**不要求复核**；远场直接判 `non_self`。
        # 两个问题：
        #   ① 远近场回答的是「离麦多远」，不是「是不是本人」—— 用户原话：
        #      「判断是不是本人只有两个，一个是 KEV，一个是声纹」。外放视频人声同样满足
        #      「近场 + 低频强」，于是会被判成本人，还不进复核队列（静默污染生活流）。
        #   ② 与 `apply_multi_evidence_policy` 自己宣称的「绝不单靠远近场判 self」自相矛盾。
        #
        # 新逻辑：声纹不可用 = **身份未知**。一律 `unknown` + `review_required=True`，
        # 远近场只作为证据记录（`acoustic_proximity` 等字段照常保留），不参与身份判定。
        # 这与 `source_attribution.proximity_adjusted_similarity` 的分工一致：
        # 那个函数只把「录远了」与「不像本人」分开，身份结论永远由声纹阈值做。
        return {
            "source_label": "unknown",
            "voice_similarity": None,
            # 声纹缺失时一律要求复核 —— 宁可让人过一眼，也不静默猜身份。
            "review_required": True,
            "decision_reason": "voiceprint_unavailable_proximity_not_identity",
            **prox_info,
        }

    def _extract_emotion_tag(self, audio_input: Any) -> str:
        del audio_input
        return "NEUTRAL"

    def transcribe(
        self,
        recording: RecordingSegment,
        sidecar_path: Path | None = None,
    ) -> list[TranscriptSegment]:
        del sidecar_path
        with self._infer_lock:
            return self._transcribe_locked(recording)

    def _transcribe_locked(
        self,
        recording: RecordingSegment,
    ) -> list[TranscriptSegment]:
        audio_path = str(recording.source_file)
        self._last_segment_attributions = {}

        # 1. Primary Whisper Large-v3-Turbo transcription (Silero VAD + Apple MLX GPU)
        whisper_segments = []
        lang_detected = "zh"
        _HALLUCINATION_MARKERS = (
            "请不吝点赞",
            "订阅 转发",
            "打赏支持",
            "明镜与点点",
            "中文字幕志愿者",
            "字幕由Amara",
            "感谢观看",
            "谢谢观看",
            "我吃了一口",
            "我可以吃一口",
            "我先放在這裡",
            "我現在在這裡有一個大小的",
            "我看你一眼",
            "我看你",
            "Bismillah",
        )
        import math
        import numpy as np
        import mlx_whisper
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        wav = decode_audio(audio_path, sampling_rate=16000)
        speech_ts = get_speech_timestamps(
            wav,
            VadOptions(
                threshold=0.42,
                min_speech_duration_ms=250,
                min_silence_duration_ms=500,
                speech_pad_ms=250,
            ),
        )
        if not speech_ts:
            return []

        # Estimate noise floor from 20th percentile of 100ms frame RMS across the 60s window
        frame_len = 1600
        n_frames = len(wav) // frame_len
        if n_frames > 0:
            frames = np.asarray(wav[: n_frames * frame_len], dtype=np.float32).reshape(n_frames, frame_len)
            frame_rms = np.sqrt(np.mean(frames * frames, axis=1))
            p20_rms = float(np.percentile(frame_rms, 20))
            noise_floor_dbfs = 20.0 * math.log10(max(p20_rms, 1e-6))
        else:
            noise_floor_dbfs = -48.0

        merged_ts: list[dict[str, Any]] = []
        for item in speech_ts:
            s_idx, e_idx = int(item["start"]), int(item["end"])
            if (
                merged_ts
                and (s_idx - merged_ts[-1]["end"]) <= int(16000 * 1.2)
                and (e_idx - merged_ts[-1]["start"]) <= int(16000 * 15.0)
            ):
                merged_ts[-1]["end"] = e_idx
                merged_ts[-1]["spans"].append((s_idx, e_idx))
            else:
                merged_ts.append({"start": s_idx, "end": e_idx, "spans": [(s_idx, e_idx)]})

        local_mlx_snap = Path(
            "$HOME/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/snapshots/a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"
        )
        mlx_model_ref = (
            str(local_mlx_snap)
            if (local_mlx_snap / "weights.safetensors").is_file()
            else "mlx-community/whisper-large-v3-turbo"
        )

        for window in merged_ts:
            chunk_wav = wav[window["start"] : window["end"]]
            if len(chunk_wav) < 3200:  # < 0.2s
                continue
            speech_only_wav = (
                np.concatenate([wav[sp_s:sp_e] for sp_s, sp_e in window["spans"]])
                if window.get("spans")
                else chunk_wav
            )
            if len(speech_only_wav) < 3200:
                continue
            win_attr = self._score_window_speaker_and_proximity(speech_only_wav, noise_floor_dbfs)
            # 【2026-10-14 修 · codex 审查 P1】混合语音的归属不能当成确定结论。
            #
            # 问题：VAD span 只要间隔 ≤1.2s 就并入同一窗，窗最长可达 15s（行 893-894），
            # 而归属**只按整窗算一次**，再把同一个 `win_attr` 复制给窗内每个 ASR 段（行 961）。
            # 于是「窗里同时有本人和外放视频/别人说话」时，别人的话会被贴上本人的归属 ——
            # 静默污染生活流，而且看不出半点异常。
            #
            # 为什么不能直接改成「按说话人轮次打分」：这条路径走的是 whisper，
            # **没有 diarization**，拿不到说话人轮次；把一个窗的文本拆给多个说话人在此无解。
            # 所以这里做的是**诚实标注**而不是假装精确：
            #   1. 永远记下这个归属实际代表多长的窗（`attribution_window_seconds`）与粒度，
            #      让下游/排障能看出「这不是逐句判的」；
            #   2. 窗长超过 `MIXED_SPEAKER_WINDOW_SECONDS` 时**强制复核** ——
            #      窗越长，越可能混入第二个说话人，此时不该静默当成确定结论。
            win_seconds = (window["end"] - window["start"]) / 16000.0
            win_attr["attribution_window_seconds"] = round(win_seconds, 3)
            win_attr["attribution_granularity"] = "window"
            if win_seconds > MIXED_SPEAKER_WINDOW_SECONDS:
                win_attr["review_required"] = True
                existing_reason = str(win_attr.get("decision_reason") or "").strip()
                win_attr["decision_reason"] = (
                    f"{existing_reason}+window_too_long_for_identity"
                    if existing_reason
                    else "window_too_long_for_identity"
                )
            snr_val = float(win_attr.get("snr_proxy_db") or 0.0)
            if snr_val < 4.0 and win_attr.get("acoustic_proximity") == "far":
                continue
            speech_dur_s = len(speech_only_wav) / 16000.0
            max_tokens = min(128, max(20, int(speech_dur_s * 6.5)))
            use_prompt = (
                "以下是普通话日常语音记录。"
                if (win_attr.get("acoustic_proximity") == "near" or snr_val >= 10.0)
                else None
            )
            offset_sec = window["start"] / 16000.0
            end_sec = window["end"] / 16000.0
            # 【2026-10-14 修】音频分类原来只在 attribute 阶段的「重算分支」里做：
            # 本文件（Whisper 路径）和 moss_transcribe 都只算声纹，attribute 走
            # 「复用上游声纹」分支时拿不到任何 audio_content_* 字段 →
            # audio-classification.json 的 windows 恒为 []（看板场景统计读它）。
            # 这里用**已经解码在内存**的整段 wav 顺手切 10 秒窗分类（零额外解码），
            # 字段随 transcript.json 的 row 流到 attribute 阶段的复用分支。
            win_attr = annotate_record_with_window_classification(
                win_attr,
                wav,
                start_seconds=offset_sec,
                end_seconds=end_sec,
            )
            mlx_res = mlx_whisper.transcribe(
                chunk_wav,
                path_or_hf_repo=mlx_model_ref,
                language="zh",
                temperature=0.0,
                condition_on_previous_text=False,
                no_speech_threshold=0.5,
                logprob_threshold=-1.0,
                compression_ratio_threshold=2.4,
                initial_prompt=use_prompt,
                without_timestamps=True,
                sample_len=max_tokens,
            )
            window_segs = []
            for seg in mlx_res.get("segments", []):
                if float(seg.get("no_speech_prob", 0.0) or 0.0) > 0.65:
                    continue
                if float(seg.get("avg_logprob", 0.0) or 0.0) < -1.0:
                    continue
                if float(seg.get("compression_ratio", 0.0) or 0.0) > 2.4:
                    continue
                class _Seg:
                    pass
                s = _Seg()
                s.start = offset_sec
                s.end = end_sec
                s.text = str(seg.get("text", ""))
                window_segs.append(s)
            if window_segs:
                win_emotion = self._extract_emotion_tag(speech_only_wav)
                for s in window_segs:
                    s.emotion = win_emotion
                    s.attribution = win_attr
                    whisper_segments.append(s)
            lang_detected = mlx_res.get("language") or "zh"

        if not whisper_segments:
            return []

        # 2. Construct TranscriptSegments
        result: list[TranscriptSegment] = []
        for idx, seg in enumerate(whisper_segments, start=1):
            text = seg.text.strip()
            if not text:
                continue
            if any(marker in text for marker in _HALLUCINATION_MARKERS):
                continue
            if re.search(r"(.{1,24}?)\1{3,}", text):
                continue
            if "气泡水" in text or "威士忌" in text or "微视剧" in text:
                text = (
                    text.replace("一杯海报", "一杯嗨棒")
                    .replace("一杯嗨堡", "一杯嗨棒")
                    .replace("一杯汉堡", "一杯嗨棒")
                    .replace("微视剧", "威士忌")
                )
            start_off = float(seg.start)
            end_off = float(seg.end)
            seg_emotion = getattr(seg, "emotion", None) or "NEUTRAL"
            seg_attr = getattr(seg, "attribution", None) or {}
            seg_id = f"{recording.recording_id}_seg_{idx:04d}"
            if seg_attr:
                self._last_segment_attributions[seg_id] = seg_attr
            src_label = str(seg_attr.get("source_label") or "")
            speaker_tag = "self" if src_label == "self" else ("non_self" if src_label == "non_self" else "Speaker_0")
            result.append(
                TranscriptSegment(
                    segment_id=seg_id,
                    recording_id=recording.recording_id,
                    start_offset_seconds=start_off,
                    end_offset_seconds=end_off,
                    absolute_started_at=_absolute_time(recording, start_off),
                    absolute_ended_at=_absolute_time(recording, end_off),
                    text=text,
                    source="local_whisper_turbo",
                    language=lang_detected,
                    transcription_confidence=seg_attr.get("voice_similarity"),
                    speaker=speaker_tag,
                    emotion=seg_emotion,
                )
            )
        return result


_SHARED_TURBO_TRANSCRIBER: LocalTurboCompositeTranscriber | None = None
_SHARED_TURBO_LOCK = None


def get_shared_turbo_transcriber() -> LocalTurboCompositeTranscriber:
    """Return a process-wide warm singleton of LocalTurboCompositeTranscriber."""
    global _SHARED_TURBO_TRANSCRIBER, _SHARED_TURBO_LOCK
    import threading
    if _SHARED_TURBO_LOCK is None:
        _SHARED_TURBO_LOCK = threading.Lock()
    with _SHARED_TURBO_LOCK:
        if _SHARED_TURBO_TRANSCRIBER is None:
            _SHARED_TURBO_TRANSCRIBER = LocalTurboCompositeTranscriber()
        return _SHARED_TURBO_TRANSCRIBER


