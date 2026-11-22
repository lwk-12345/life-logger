from __future__ import annotations

import io
import math
import wave
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from life_logger.audio_vad import (
    DEFAULT_MIN_SPEECH_DURATION_MS,
    DEFAULT_SILENCE_THRESHOLD_DBFS,
    DEFAULT_SPEECH_THRESHOLD_DBFS,
    calculate_audio_energy,
    load_audio_samples,
    should_skip_transcription,
)
from life_logger.watch_live import WatchLiveProcessor, _NoSpeechPreview
from life_logger.watch_stream import WatchSessionStore


def _generate_sine_wav_bytes(
    duration_sec: float,
    dbfs: float,
    freq_hz: float = 440.0,
    sample_rate: int = 16000,
) -> bytes:
    """Generate a mono 16-bit PCM WAV in bytes with specified duration and RMS dBFS."""
    num_samples = int(round(duration_sec * sample_rate))
    if dbfs <= -100.0 or duration_sec <= 0:
        samples = np.zeros(num_samples, dtype=np.int16)
    else:
        # RMS = 10 ** (dbfs / 20)
        # Sine wave peak amplitude = RMS * sqrt(2)
        rms = 10.0 ** (dbfs / 20.0)
        amplitude = min(1.0, rms * math.sqrt(2.0))
        t = np.linspace(0.0, duration_sec, num=num_samples, endpoint=False)
        waveform = amplitude * np.sin(2.0 * np.pi * freq_hz * t)
        samples = np.clip(waveform * 32767.0, -32768.0, 32767.0).astype(np.int16)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())
    return buffer.getvalue()


def _generate_wav_file(
    path: Path,
    duration_sec: float,
    dbfs: float,
    freq_hz: float = 440.0,
    sample_rate: int = 16000,
) -> Path:
    data = _generate_sine_wav_bytes(duration_sec, dbfs, freq_hz, sample_rate)
    path.write_bytes(data)
    return path


def test_pure_silence_detected_as_no_speech(tmp_path: Path) -> None:
    silent_path = _generate_wav_file(
        tmp_path / "silence.wav", duration_sec=1.0, dbfs=-100.0
    )
    result = calculate_audio_energy(silent_path)

    assert result["is_speech"] is False
    assert result["mean_rms_dbfs"] <= DEFAULT_SILENCE_THRESHOLD_DBFS
    assert result["speech_frames"] == 0
    assert result["duration_sec"] == pytest.approx(1.0, abs=0.01)
    assert should_skip_transcription(silent_path) is True


def test_low_noise_below_silence_floor(tmp_path: Path) -> None:
    # -50 dBFS is below -45 dBFS silence floor
    quiet_path = _generate_wav_file(
        tmp_path / "quiet.wav", duration_sec=1.0, dbfs=-50.0
    )
    result = calculate_audio_energy(quiet_path)

    assert result["is_speech"] is False
    assert result["mean_rms_dbfs"] < DEFAULT_SILENCE_THRESHOLD_DBFS
    assert should_skip_transcription(quiet_path) is True


def test_noise_between_silence_floor_and_speech_threshold(tmp_path: Path) -> None:
    # -42 dBFS is above silence floor (-45) but below speech threshold (-38)
    ambient_path = _generate_wav_file(
        tmp_path / "ambient.wav", duration_sec=1.0, dbfs=-42.0
    )
    result = calculate_audio_energy(ambient_path)

    assert result["is_speech"] is False
    assert result["speech_frames"] == 0
    assert should_skip_transcription(ambient_path) is True


def test_short_speech_burst_under_250ms_rejected(tmp_path: Path) -> None:
    # 150ms speech burst at -20 dBFS, rest 850ms silence
    sr = 16000
    total_len = sr * 1  # 1 second
    samples = np.zeros(total_len, dtype=np.int16)

    # 150ms of strong signal
    burst_len = int(0.15 * sr)
    rms = 10.0 ** (-20.0 / 20.0)
    amp = min(1.0, rms * math.sqrt(2.0))
    t = np.linspace(0.0, 0.15, num=burst_len, endpoint=False)
    burst = amp * np.sin(2.0 * np.pi * 500.0 * t)
    samples[1000 : 1000 + burst_len] = np.clip(
        burst * 32767.0, -32768.0, 32767.0
    ).astype(np.int16)

    burst_path = tmp_path / "short_burst.wav"
    with wave.open(str(burst_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sr)
        wav_file.writeframes(samples.tobytes())

    result = calculate_audio_energy(burst_path)
    assert result["is_speech"] is False
    assert result["max_contiguous_speech_sec"] < (
        DEFAULT_MIN_SPEECH_DURATION_MS / 1000.0
    )
    assert should_skip_transcription(burst_path) is True


def test_sustained_active_speech_accepted(tmp_path: Path) -> None:
    # 500ms active speech at -20 dBFS meets >= 250ms criterion
    speech_path = _generate_wav_file(
        tmp_path / "speech.wav", duration_sec=0.5, dbfs=-20.0
    )
    result = calculate_audio_energy(speech_path)

    assert result["is_speech"] is True
    assert result["mean_rms_dbfs"] >= DEFAULT_SPEECH_THRESHOLD_DBFS
    assert result["max_contiguous_speech_sec"] >= 0.25
    assert should_skip_transcription(speech_path) is False


def test_speech_with_leading_and_trailing_silence(tmp_path: Path) -> None:
    # 1.0s silence + 0.6s speech + 1.0s silence (total 2.6s)
    sr = 16000
    speech_data = _generate_sine_wav_bytes(
        0.6, dbfs=-22.0, freq_hz=300.0, sample_rate=sr
    )
    with wave.open(io.BytesIO(speech_data), "rb") as w:
        speech_pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    silence_pcm = np.zeros(sr * 1, dtype=np.int16)
    full_pcm = np.concatenate([silence_pcm, speech_pcm, silence_pcm])

    combined_path = tmp_path / "combined.wav"
    with wave.open(str(combined_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(full_pcm.tobytes())

    result = calculate_audio_energy(combined_path)
    assert result["is_speech"] is True
    assert result["max_contiguous_speech_sec"] >= 0.25
    assert should_skip_transcription(combined_path) is False


def test_bytes_input_support() -> None:
    wav_bytes = _generate_sine_wav_bytes(duration_sec=0.5, dbfs=-20.0)
    result = calculate_audio_energy(wav_bytes)
    assert result["is_speech"] is True
    assert should_skip_transcription(wav_bytes) is False

    silent_bytes = _generate_sine_wav_bytes(duration_sec=0.5, dbfs=-90.0)
    result_silent = calculate_audio_energy(silent_bytes)
    assert result_silent["is_speech"] is False
    assert should_skip_transcription(silent_bytes) is True


def test_empty_audio_bytes_and_file(tmp_path: Path) -> None:
    empty_bytes = b""
    result = calculate_audio_energy(empty_bytes)
    assert result["is_speech"] is False
    assert result["duration_sec"] == 0.0

    empty_wav = tmp_path / "empty.wav"
    with wave.open(str(empty_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"")

    assert should_skip_transcription(empty_wav) is True


def test_stereo_wav_downmixed_to_mono(tmp_path: Path) -> None:
    sr = 16000
    duration = 0.5
    num_samples = int(sr * duration)
    t = np.linspace(0.0, duration, num=num_samples, endpoint=False)
    ch1 = (0.5 * np.sin(2.0 * np.pi * 440.0 * t) * 32767.0).astype(np.int16)
    ch2 = (0.5 * np.sin(2.0 * np.pi * 880.0 * t) * 32767.0).astype(np.int16)
    interleaved = np.empty((num_samples * 2,), dtype=np.int16)
    interleaved[0::2] = ch1
    interleaved[1::2] = ch2

    stereo_path = tmp_path / "stereo.wav"
    with wave.open(str(stereo_path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(interleaved.tobytes())

    samples, out_sr = load_audio_samples(stereo_path, sample_rate=16000)
    assert samples.ndim == 1
    assert len(samples) == num_samples
    assert out_sr == 16000


def test_non_audio_fallback_does_not_skip_transcription(tmp_path: Path) -> None:
    # Corrupted / dummy data should not raise but fallback to False (do not skip)
    corrupted_path = tmp_path / "corrupt.m4a"
    corrupted_path.write_bytes(b"not a valid audio file header")

    assert should_skip_transcription(corrupted_path) is False


def test_watch_live_processor_silence_vad_bypass(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    # Verify integration with WatchLiveProcessor:
    # silent audio segment returns _NoSpeechPreview
    store = WatchSessionStore(tmp_path / "store")
    store.start_session(
        device_id="watch-vad",
        session_id="session-vad",
        started_at="2026-09-27T10:00:00+08:00",
    )

    silent_wav = _generate_sine_wav_bytes(duration_sec=2.0, dbfs=-80.0)
    import hashlib
    import json

    checksum = "sha256:" + hashlib.sha256(silent_wav).hexdigest()

    store.receive_segment(
        device_id="watch-vad",
        session_id="session-vad",
        sequence=0,
        start_offset_ms=0,
        duration_ms=2000,
        checksum=checksum,
        body=silent_wav,
    )

    # Convert segment-000000.m4a in directory to our silent wav
    seg_audio = (
        store.session_directory("watch-vad", "session-vad")
        / "segments"
        / "segment-000000.m4a"
    )
    seg_audio.write_bytes(silent_wav)

    # WatchLiveProcessor._process 会 load_local_config(config_path)，
    # 缺文件会抛「缺少本地配置文件」—— 本测试只关心静音→NoSpeech 分支，写一个最小配置。
    # validate_local_pipeline_config 要求 python/template 两个键存在
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "python": "/usr/bin/python3",
                "template": str(Path("work/watch-2026-10-01/template-from-watch-self.wav")),
            }
        ),
        encoding="utf-8",
    )
    # WatchLiveProcessor._process 会 load_local_config + validate：
    # 缺文件/缺必填键都会抛错。本测试只关心「静音 → NoSpeech」分支，给一份最小合法配置。
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "python": "/usr/bin/python3",
                "template": str(Path(repo_root).joinpath(
                    "work/watch-2026-10-01/template-from-watch-self.wav"
                )),
                "speaker": "local_campplus",
            }
        ),
        encoding="utf-8",
    )
    processor = WatchLiveProcessor(
        store=store,
        config_path=tmp_path / "config.json",
        stop=MagicMock(
            is_set=MagicMock(return_value=False), wait=MagicMock(return_value=False)
        ),
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    res = processor._process(seg_audio, out_dir, "2026-09-27T10:00:00+08:00")
    assert isinstance(res, _NoSpeechPreview)


# ─────────────────────────────────────────────────────────────────────────────
# [2026-11-07 Q96x] 静音门只跳过**真正安静**的段。
#   原来只看 Silero（默认 threshold=0.5）：吵的段（健身房背景噪）检不出人声就被当静音整段丢掉。
#   现在：整段能量 ≥ QUIET_SEGMENT_DBFS(-45) → 送转写；只有真安静的段才用低阈值(0.2)复核。
# ─────────────────────────────────────────────────────────────────────────────
import life_logger.audio_vad as audio_vad


def test_loud_segment_without_silero_speech_is_sent_to_transcription(tmp_path, monkeypatch):
    """吵（-30 dBFS）但是 Silero 默认阈值检不出人声 —— 这正是健身房那 50 段的形态。"""
    from life_logger.audio_vad import QUIET_SEGMENT_DBFS

    audio = _generate_wav_file(tmp_path / "gym-loud.wav", 2.0, -30.0, freq_hz=110.0)
    assert audio_vad.segment_mean_dbfs(audio_vad.load_audio_samples(audio)[0]) >= QUIET_SEGMENT_DBFS

    calls: list[float | None] = []

    def fake_silero(samples, sample_rate=16000, *, threshold=None):
        calls.append(threshold)
        return False          # 默认阈值（和低阈值都）没检出人声

    monkeypatch.setattr(audio_vad, "check_silero_vad_speech", fake_silero)
    assert audio_vad.is_segment_silence(audio) is False, "吵的段不许判静音"
    assert calls == [], "能量已判定为吵，不必再问模型（也不该被模型一票否决）"


def test_truly_quiet_segment_is_still_skipped(tmp_path, monkeypatch):
    """真安静（-70 dBFS）+ 低阈值也无人声 → 仍然跳过。"""
    audio = _generate_wav_file(tmp_path / "quiet.wav", 2.0, -70.0)
    monkeypatch.setattr(audio_vad, "check_silero_vad_speech",
                        lambda samples, sample_rate=16000, *, threshold=None: False)
    assert audio_vad.is_segment_silence(audio) is True


def test_quiet_segment_uses_low_threshold_and_keeps_speech(tmp_path, monkeypatch):
    """安静段的复核必须用低阈值 0.2：低阈值检出人声 → 送转写（不能因为"不够响"就丢）。"""
    audio = _generate_wav_file(tmp_path / "quiet-speech.wav", 2.0, -48.0)
    seen: list[float | None] = []

    def fake_silero(samples, sample_rate=16000, *, threshold=None):
        seen.append(threshold)
        return True           # 低阈值下检出人声

    monkeypatch.setattr(audio_vad, "check_silero_vad_speech", fake_silero)
    assert audio_vad.is_segment_silence(audio) is False
    assert seen == [audio_vad.SILERO_LOW_THRESHOLD] == [0.2]


def test_undecodable_audio_fails_open(tmp_path, monkeypatch):
    """解码失败 → 送转写（fail-open），绝不因为看不懂文件就丢音频。"""
    audio = tmp_path / "broken.m4a"
    audio.write_bytes(b"\x00\x01not-really-audio")
    monkeypatch.setattr(audio_vad, "check_silero_vad_speech",
                        lambda *a, **kw: pytest.fail("解不开的音频不该问模型"))
    assert audio_vad.is_segment_silence(audio) is False


def test_missing_file_is_not_silence(tmp_path):
    assert audio_vad.is_segment_silence(tmp_path / "nope.m4a") is False


def test_silero_unavailable_falls_back_to_energy(tmp_path, monkeypatch):
    """Silero 不可用（None）时回退能量判定：安静 → 跳过；吵 → 送转写。"""
    monkeypatch.setattr(audio_vad, "check_silero_vad_speech",
                        lambda *a, **kw: None)
    quiet = _generate_wav_file(tmp_path / "q.wav", 2.0, -70.0)
    loud = _generate_wav_file(tmp_path / "l.wav", 2.0, -30.0, freq_hz=110.0)
    assert audio_vad.is_segment_silence(quiet) is True
    assert audio_vad.is_segment_silence(loud) is False
