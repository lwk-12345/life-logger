"""Silence VAD pre-filtering on audio ingest using sliding-frame RMS calculation."""

from __future__ import annotations

import io
import math
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np

# Standard VAD defaults as specified in PROJECT.md and Milestone M1:
# Silence floor: -45 dBFS
# Active speech threshold: -38 dBFS
# Minimum speech duration: 250 ms
DEFAULT_SILENCE_THRESHOLD_DBFS = -45.0
DEFAULT_SPEECH_THRESHOLD_DBFS = -38.0
DEFAULT_MIN_SPEECH_DURATION_MS = 250.0
DEFAULT_FRAME_DURATION_MS = 30.0
DEFAULT_FRAME_STEP_MS = 10.0
DEFAULT_SAMPLE_RATE = 16000

# ── 静音门（is_segment_silence）用的两个阈值 ────────────────────────────
# 【2026-11-07 Q96x】原来的静音门**只看 Silero**（faster-whisper VadOptions 默认
# threshold=0.5）：只要它没检出人声就整段判 no_speech，**完全不看这段有多吵**。
# 11-05 18:30–19:40 健身房 71 段里 50 段被判 no_speech（没有 asr.log），而那些段整段
# RMS 约 -29～-34 dBFS —— 根本不是安静，是持续背景噪把 VAD 骗了。用 SenseVoice 补测其中
# 14 段，至少 4 段有本人语音（18:36 / 18:47 / 18:49 / 18:51），健身时报的组数、重量全丢了。
#
# 现在的判据拆成两步：**先看能量，再看模型**。
#: 整段视为「真安静」的能量上限。直接沿用本仓既定常量 DEFAULT_SILENCE_THRESHOLD_DBFS = -45
#: （PROJECT.md / M1 的 silence floor）：低于它只有底噪，不可能有可辨识的人声。
QUIET_SEGMENT_DBFS = DEFAULT_SILENCE_THRESHOLD_DBFS
#: 安静段的**复核**阈值：0.5 太保守（在持续背景噪下会把人声一起判没），降到 0.2 再问一次。
#: 放宽只影响"本来就很安静"的段（更容易被判成有人声 → 更少跳过），不会把吵的段变成静音。
SILERO_LOW_THRESHOLD = 0.2


def _resample_linear(samples: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Resample 1D float array using linear interpolation."""
    if orig_sr == target_sr or len(samples) == 0:
        return samples
    duration = len(samples) / float(orig_sr)
    target_len = int(round(duration * target_sr))
    if target_len == 0:
        return np.empty(0, dtype=np.float32)
    orig_times = np.linspace(0.0, duration, num=len(samples), endpoint=False)
    target_times = np.linspace(0.0, duration, num=target_len, endpoint=False)
    return np.interp(target_times, orig_times, samples).astype(np.float32)


def load_audio_samples(
    audio_path_or_bytes: str | bytes | Path,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> tuple[np.ndarray, int]:
    """Decode audio to a 1D float32 numpy array normalized to [-1.0, 1.0].

    Supports WAV containers, M4A/AAC / MP4 audio containers, and raw PCM.
    Returns (samples, sample_rate).
    """
    if isinstance(audio_path_or_bytes, (str, Path)):
        path = Path(audio_path_or_bytes)
        if not path.is_file():
            raise FileNotFoundError(f"Audio file not found: {path}")

        # Try wave module directly for fast WAV decoding without external processes
        if path.suffix.lower() == ".wav":
            try:
                with wave.open(str(path), "rb") as wav_file:
                    n_channels = wav_file.getnchannels()
                    sampwidth = wav_file.getsampwidth()
                    framerate = wav_file.getframerate()
                    frames = wav_file.readframes(wav_file.getnframes())

                    if sampwidth == 2:
                        raw = (
                            np.frombuffer(frames, dtype=np.int16).astype(np.float32)
                            / 32768.0
                        )
                    elif sampwidth == 1:
                        raw = (
                            np.frombuffer(frames, dtype=np.uint8).astype(np.float32)
                            - 128.0
                        ) / 128.0
                    elif sampwidth == 4:
                        raw = (
                            np.frombuffer(frames, dtype=np.int32).astype(np.float32)
                            / 2147483648.0
                        )
                    else:
                        raise ValueError(f"Unsupported WAV sample width: {sampwidth}")

                    if n_channels > 1:
                        raw = raw.reshape(-1, n_channels).mean(axis=1)

                    if framerate != sample_rate:
                        raw = _resample_linear(raw, framerate, sample_rate)
                    return raw.astype(np.float32), sample_rate
            except Exception:
                # Fall through to ffmpeg
                pass

        # Use ffmpeg for M4A, AAC, and other formats
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required to decode non-WAV audio files")

        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "-",
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                        timeout=900,
        )
        except OSError as error:
            raise RuntimeError(f"Failed to execute ffmpeg: {error}") from error

        if result.returncode != 0:
            error_msg = result.stderr.decode("utf-8", errors="ignore").strip()
            raise ValueError(f"ffmpeg failed to decode audio from {path}: {error_msg}")

        samples = (
            np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        )
        return samples, sample_rate

    if isinstance(audio_path_or_bytes, bytes):
        data = audio_path_or_bytes
        if not data or len(data) < 44:
            return np.empty(0, dtype=np.float32), sample_rate

        # Check for WAV header (RIFF)
        if data.startswith(b"RIFF") and b"WAVE" in data[:16]:
            try:
                with wave.open(io.BytesIO(data), "rb") as wav_file:
                    n_channels = wav_file.getnchannels()
                    sampwidth = wav_file.getsampwidth()
                    framerate = wav_file.getframerate()
                    frames = wav_file.readframes(wav_file.getnframes())

                    if sampwidth == 2:
                        raw = (
                            np.frombuffer(frames, dtype=np.int16).astype(np.float32)
                            / 32768.0
                        )
                    elif sampwidth == 1:
                        raw = (
                            np.frombuffer(frames, dtype=np.uint8).astype(np.float32)
                            - 128.0
                        ) / 128.0
                    elif sampwidth == 4:
                        raw = (
                            np.frombuffer(frames, dtype=np.int32).astype(np.float32)
                            / 2147483648.0
                        )
                    else:
                        raise ValueError(f"Unsupported WAV sample width: {sampwidth}")

                    if n_channels > 1:
                        raw = raw.reshape(-1, n_channels).mean(axis=1)

                    if framerate != sample_rate:
                        raw = _resample_linear(raw, framerate, sample_rate)
                    return raw.astype(np.float32), sample_rate
            except Exception:
                pass

        # Try ffmpeg pipe
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                "pipe:1",
            ]
            try:
                result = subprocess.run(
                    cmd,
                    input=data,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                                timeout=900,
            )
                if result.returncode == 0 and len(result.stdout) > 0:
                    samples = (
                        np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)
                        / 32768.0
                    )
                    return samples, sample_rate
            except Exception:
                pass

        # Fallback: if data length is even, check if raw 16-bit PCM
        if len(data) % 2 == 0:
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            return samples, sample_rate

        raise ValueError("Could not decode audio from bytes")

    raise TypeError(
        f"audio_path_or_bytes must be str, Path, or bytes; got "
        f"{type(audio_path_or_bytes)}"
    )


def calculate_audio_energy(
    audio_path_or_bytes: str | bytes | Path,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    silence_threshold_dbfs: float = DEFAULT_SILENCE_THRESHOLD_DBFS,
    speech_threshold_dbfs: float = DEFAULT_SPEECH_THRESHOLD_DBFS,
    min_speech_duration_ms: float = DEFAULT_MIN_SPEECH_DURATION_MS,
    frame_duration_ms: float = DEFAULT_FRAME_DURATION_MS,
    frame_step_ms: float = DEFAULT_FRAME_STEP_MS,
) -> dict[str, Any]:
    """Calculate sliding-frame energy/RMS metrics and determine speech presence.

    Returns dictionary containing:
        - is_speech: bool
        - mean_rms_dbfs: float
        - speech_frames: int
        - duration_sec: float
        - silence_threshold_dbfs: float
        - speech_threshold_dbfs: float
        - speech_duration_sec: float
        - max_contiguous_speech_sec: float
    """
    samples, sr = load_audio_samples(audio_path_or_bytes, sample_rate=sample_rate)
    total_samples = len(samples)

    if total_samples == 0:
        return {
            "is_speech": False,
            "mean_rms_dbfs": -100.0,
            "speech_frames": 0,
            "duration_sec": 0.0,
            "silence_threshold_dbfs": float(silence_threshold_dbfs),
            "speech_threshold_dbfs": float(speech_threshold_dbfs),
            "speech_duration_sec": 0.0,
            "max_contiguous_speech_sec": 0.0,
        }

    duration_sec = total_samples / float(sr)

    # Overall RMS calculation
    mean_sq = float(np.mean(samples**2))
    overall_rms = math.sqrt(max(mean_sq, 0.0))
    if overall_rms > 1e-6:
        mean_rms_dbfs = round(20.0 * math.log10(overall_rms), 1)
    else:
        mean_rms_dbfs = -100.0

    frame_size = int(round(sr * (frame_duration_ms / 1000.0)))
    step_size = int(round(sr * (frame_step_ms / 1000.0)))

    if frame_size <= 0:
        frame_size = 1
    if step_size <= 0:
        step_size = 1

    # If audio is shorter than a single frame
    if total_samples < frame_size:
        is_speech = (
            mean_rms_dbfs >= speech_threshold_dbfs
            and (duration_sec * 1000.0) >= min_speech_duration_ms
        )
        speech_frames = 1 if mean_rms_dbfs >= speech_threshold_dbfs else 0
        speech_duration_sec = duration_sec if speech_frames else 0.0
        return {
            "is_speech": is_speech,
            "mean_rms_dbfs": mean_rms_dbfs,
            "speech_frames": speech_frames,
            "duration_sec": round(duration_sec, 4),
            "silence_threshold_dbfs": float(silence_threshold_dbfs),
            "speech_threshold_dbfs": float(speech_threshold_dbfs),
            "speech_duration_sec": round(speech_duration_sec, 4),
            "max_contiguous_speech_sec": round(speech_duration_sec, 4),
        }

    # Sliding-window RMS computation
    # Vectorized frame extraction:
    num_frames = (total_samples - frame_size) // step_size + 1
    frame_indices = (
        np.arange(num_frames)[:, None] * step_size + np.arange(frame_size)[None, :]
    )
    frames = samples[frame_indices]  # shape: (num_frames, frame_size)

    frame_ms = np.mean(frames**2, axis=1)
    frame_rms = np.sqrt(np.maximum(frame_ms, 1e-12))
    frame_dbfs = 20.0 * np.log10(np.maximum(frame_rms, 1e-6))

    # An active speech frame must exceed speech_threshold_dbfs
    speech_mask = frame_dbfs >= speech_threshold_dbfs
    speech_frames = int(np.sum(speech_mask))

    # Calculate contiguous active speech blocks
    max_contiguous_frames = 0
    current_run = 0
    for is_active in speech_mask:
        if is_active:
            current_run += 1
            if current_run > max_contiguous_frames:
                max_contiguous_frames = current_run
        else:
            current_run = 0

    frame_step_sec = step_size / float(sr)
    max_contiguous_speech_sec = max_contiguous_frames * frame_step_sec
    total_speech_sec = speech_frames * frame_step_sec

    # Min speech duration criterion (e.g. 250ms):
    # Segment contains active speech if contiguous speech meets min duration
    # OR total speech meets min duration with at least 50% contiguous stability.
    min_speech_sec = min_speech_duration_ms / 1000.0
    if mean_rms_dbfs < silence_threshold_dbfs:
        is_speech = False
    else:
        is_speech = bool(
            (max_contiguous_speech_sec >= min_speech_sec)
            or (
                total_speech_sec >= min_speech_sec
                and max_contiguous_speech_sec >= (min_speech_sec * 0.6)
            )
        )

    return {
        "is_speech": is_speech,
        "mean_rms_dbfs": mean_rms_dbfs,
        "speech_frames": speech_frames,
        "duration_sec": round(duration_sec, 4),
        "silence_threshold_dbfs": float(silence_threshold_dbfs),
        "speech_threshold_dbfs": float(speech_threshold_dbfs),
        "speech_duration_sec": round(total_speech_sec, 4),
        "max_contiguous_speech_sec": round(max_contiguous_speech_sec, 4),
    }


def check_silero_vad_speech(
    samples: np.ndarray,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    *,
    threshold: float | None = None,
) -> bool | None:
    """Use Silero VAD v6 ONNX to detect whether active speech intervals exist.

    Returns True if active speech is detected, False if 100% silent, None if unavailable.

    `threshold` 不给就用 faster-whisper 的默认值（0.5）。静音门复核时会传
    `SILERO_LOW_THRESHOLD`（0.2）来提高召回。
    """
    if len(samples) == 0:
        return False
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        options = VadOptions() if threshold is None else VadOptions(threshold=float(threshold))
        timestamps = get_speech_timestamps(samples, vad_options=options)
        return len(timestamps) > 0
    except Exception:
        return None


def segment_mean_dbfs(samples: np.ndarray) -> float | None:
    """整段平均能量（dBFS）。空音频返回 None。

    与 `calculate_audio_energy()` 的 mean_rms_dbfs 同一口径（20*log10(rms)），
    这里直接对整段算，避免为了取一个数再解一次音频。
    """
    if samples is None or len(samples) == 0:
        return None
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
    return 20.0 * math.log10(max(rms, 1e-6))


def should_skip_transcription(
    audio_path: str | Path | bytes,
    silence_threshold_dbfs: float = DEFAULT_SILENCE_THRESHOLD_DBFS,
    speech_threshold_dbfs: float = DEFAULT_SPEECH_THRESHOLD_DBFS,
    min_speech_duration_ms: float = DEFAULT_MIN_SPEECH_DURATION_MS,
    use_silero: bool = False,
) -> bool:
    """Determines whether an audio segment can bypass FunASR/MOSS/MPS transcription.

    Returns True if audio is silent or contains no active speech.
    Returns False if active speech is detected or if decoding fails.
    """
    if isinstance(audio_path, (str, Path)):
        p = Path(audio_path)
        if not p.is_file():
            raise FileNotFoundError(f"Audio file not found: {p}")

    try:
        if use_silero:
            try:
                samples, sr = load_audio_samples(audio_path, sample_rate=DEFAULT_SAMPLE_RATE)
                silero_result = check_silero_vad_speech(samples, sample_rate=sr)
                if silero_result is not None:
                    return not silero_result
            except Exception:
                pass

        # 回退到既有 RMS 能量滑动窗判断
        energy = calculate_audio_energy(
            audio_path,
            silence_threshold_dbfs=silence_threshold_dbfs,
            speech_threshold_dbfs=speech_threshold_dbfs,
            min_speech_duration_ms=min_speech_duration_ms,
        )
        return not bool(energy.get("is_speech", False))
    except Exception:
        # Fallback: if audio cannot be decoded (e.g. dummy bytes in tests),
        # do not skip transcription.
        return False


def is_segment_silence(audio_path: str | Path) -> bool:
    """静音门：**只有真正安静的段**才允许跳过转写。

    判据（顺序不能反）：
      ① 解码失败 / 文件不存在 → **送转写**（fail-open，宁可多转一段也不丢话）；
      ② 整段平均能量 ≥ `QUIET_SEGMENT_DBFS`（-45 dBFS）→ **送转写**。
         这一条是本次修复的核心：有背景声的段（健身房、街道、餐厅）即使 Silero
         默认阈值没检出人声，也**不许**判静音；
      ③ 确实是安静段 → 用 Silero **低阈值**（0.2）复核一次：检出人声 → 送转写；
         检不出 → 判 no_speech（跳过）；
      ④ Silero 不可用（返回 None）→ 回退到 RMS 能量滑窗判定。

    Returns True 表示"可以跳过转写"。
    """
    try:
        p = Path(audio_path)
        if not p.is_file():
            return False
        samples, sr = load_audio_samples(p, sample_rate=DEFAULT_SAMPLE_RATE)
        if len(samples) == 0:
            return True

        # ② 先看能量：吵的段一律送转写（不依赖模型判断）
        dbfs = segment_mean_dbfs(samples)
        if dbfs is not None and dbfs >= QUIET_SEGMENT_DBFS:
            return False

        # ③ 安静段：用低阈值复核，避免 0.5 把轻声说话一起判没
        silero_active = check_silero_vad_speech(
            samples, sample_rate=sr, threshold=SILERO_LOW_THRESHOLD
        )
        if silero_active is not None:
            return not silero_active

        # ④ 模型不可用 → RMS 能量滑窗兜底
        return should_skip_transcription(p, use_silero=False)
    except Exception:
        return False
