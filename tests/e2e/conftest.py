"""Pytest fixtures and test harnesses for the E2E test suite."""

from __future__ import annotations

import importlib
import io
import json
import math
import struct
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Generator
import threading

import pytest

from life_logger.local_web import JobStore


# --- Module Import Helpers for Progressive Testability ---

def try_import_module(module_name: str) -> Any:
    """Attempts to import a module; returns None if not yet implemented."""
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError:
        return None


@pytest.fixture
def audio_vad_module() -> Any:
    """Provides life_logger.audio_vad or skips if pending M1."""
    mod = try_import_module("life_logger.audio_vad")
    if mod is None:
        pytest.skip("life_logger.audio_vad not yet implemented (Milestone M1 in progress)")
    return mod


@pytest.fixture
def ambient_anchors_module() -> Any:
    """Provides life_logger.ambient_anchors or skips if pending M1."""
    mod = try_import_module("life_logger.ambient_anchors")
    if mod is None:
        pytest.skip("life_logger.ambient_anchors not yet implemented (Milestone M1 in progress)")
    return mod


@pytest.fixture
def speaker_registry_module() -> Any:
    """Provides life_logger.speaker_registry or skips if pending M1."""
    mod = try_import_module("life_logger.speaker_registry")
    if mod is None:
        pytest.skip("life_logger.speaker_registry not yet implemented (Milestone M1 in progress)")
    return mod


@pytest.fixture
def intent_pool_module() -> Any:
    """Provides life_logger.intent_pool or skips if pending M2."""
    mod = try_import_module("life_logger.intent_pool")
    if mod is None:
        pytest.skip("life_logger.intent_pool not yet implemented (Milestone M2 in progress)")
    return mod


@pytest.fixture
def natural_correction_module() -> Any:
    """Provides life_logger.natural_correction or skips if pending M3."""
    mod = try_import_module("life_logger.natural_correction")
    if mod is None:
        pytest.skip("life_logger.natural_correction not yet implemented (Milestone M3 in progress)")
    return mod


# --- Audio Synthesis Helpers ---

def generate_pcm_wav(
    duration_sec: float,
    amplitude: float = 0.0,
    frequency: float = 440.0,
    sample_rate: int = 16000,
) -> bytes:
    """Generates standard 16-bit mono PCM WAV bytes.
    
    amplitude: 0.0 to 1.0 (1.0 = 0 dBFS, 0.0 = -inf dBFS)
    """
    total_samples = int(duration_sec * sample_rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        if amplitude <= 0.0:
            raw_frames = b"\x00\x00" * total_samples
        else:
            frames = []
            for i in range(total_samples):
                val = int(amplitude * 32767.0 * math.sin(2.0 * math.pi * frequency * i / sample_rate))
                val = max(-32768, min(32767, val))
                frames.append(struct.pack("<h", val))
            raw_frames = b"".join(frames)
        wf.writeframes(raw_frames)
    return buffer.getvalue()


def write_test_wav(
    path: Path,
    duration_sec: float,
    amplitude: float = 0.0,
    frequency: float = 440.0,
    sample_rate: int = 16000,
) -> Path:
    """Writes a WAV file to disk."""
    data = generate_pcm_wav(duration_sec, amplitude, frequency, sample_rate)
    path.write_bytes(data)
    return path


@pytest.fixture
def synthetic_silent_wav(tmp_path: Path) -> Path:
    """Fixture providing a 1.0s pure silence WAV file (-inf dBFS)."""
    p = tmp_path / "silence.wav"
    return write_test_wav(p, duration_sec=1.0, amplitude=0.0)


@pytest.fixture
def synthetic_speech_wav(tmp_path: Path) -> Path:
    """Fixture providing a 1.0s speech tone WAV file (~ -14 dBFS, 0.2 amplitude)."""
    p = tmp_path / "speech.wav"
    return write_test_wav(p, duration_sec=1.0, amplitude=0.2, frequency=440.0)


# --- Mock OpenClaw Daemon Fixture ---

class _MockEveningAlignmentHandler(BaseHTTPRequestHandler):
    received_payloads: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        if self.path in {
            "/v1/reconciliation/evening-alignment",
            "/v1/tasks",
        }:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body) if body else {}
            self.__class__.received_payloads.append(data)
            resp = json.dumps({
                "status": "success",
                "card_id": f"card_align_{len(self.__class__.received_payloads)}",
                "dispatched_to": "wechat",
                "received": data,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return
        self.send_error(404, "Not Found")

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress HTTP server output in test runner


@pytest.fixture
def mock_openclaw_daemon() -> Generator[tuple[str, list[dict[str, Any]]], None, None]:
    """Spins up a lightweight mock OpenClaw daemon for alignment dispatch testing."""
    _MockEveningAlignmentHandler.received_payloads.clear()
    server = HTTPServer(("127.0.0.1", 0), _MockEveningAlignmentHandler)
    host, port = server.server_address
    url = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield url, _MockEveningAlignmentHandler.received_payloads
    finally:
        server.shutdown()
        server.server_close()


# --- Local Web Test Server Fixture ---

@pytest.fixture
def test_job_store(tmp_path: Path) -> JobStore:
    """Fixture providing an isolated JobStore."""
    store_dir = tmp_path / "local-web"
    store_dir.mkdir(parents=True, exist_ok=True)
    return JobStore(store_dir, recover=False)


# --- 2026-10-02 Historical Benchmark Data Path Fixture ---

@pytest.fixture
def benchmark_data_paths() -> dict[str, Path]:
    """Returns verified paths to 2026-10-02 historical benchmark artifacts."""
    base = Path(__file__).resolve().parents[2]
    return {
        "context_extraction": base / "output/local-web/contextual-replay-2026-10-02/context-extraction.json",
        "evening_job_events": base / "output/local-web/jobs/job-20261002-213508-b7ad222d/files/2c09f72073eaf58025631b167c78c87977195a3f7e4d6e475ba87914a2e009b5/result/2026-10-02/events.json",
        "evening_source_attribution": base / "output/local-web/jobs/job-20261002-213508-b7ad222d/files/2c09f72073eaf58025631b167c78c87977195a3f7e4d6e475ba87914a2e009b5/result/source-attribution.json",
        "afternoon_job_events": base / "output/local-web/jobs/job-20261002-191159-ed737b15/files/5d819ac8cbe0120d491f48042a56732460f3b5291289ff807d07fd44df5bf6cd/result/2026-10-02/events.json",
    }


@pytest.fixture
def test_job_with_events(test_job_store: JobStore) -> tuple[str, str]:
    """Creates a job with a valid events.json on disk so manual events and corrections can attach cleanly."""
    job_id = test_job_store.create_job()
    job_dir = test_job_store.job_path(job_id)
    date_dir = job_dir / "2026-10-02"
    date_dir.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "event_id": "evt_test_001",
            "artifact_path": "2026-10-02/events.json",
            "categories": ["expense"],
            "activity_type": "purchase",
            "title": "超市购买",
            "time": {"absolute": "2026-10-02T18:17:15+08:00", "relative_seconds": None, "precision": "exact"},
            "data": {"expense": {"amount": 140.0, "currency": "CNY"}},
            "capture_mode": "passive",
            "source_type": "audio",
            "confidence": 0.95,
            "evidence": {},
            "review_status": "confirmed",
        },
        {
            "event_id": "evt_test_smoke",
            "artifact_path": "2026-10-02/events.json",
            "categories": ["expense"],
            "activity_type": "purchase",
            "title": "买烟",
            "time": {"absolute": "2026-10-02T20:35:31+08:00", "relative_seconds": None, "precision": "exact"},
            "data": {"expense": {"amount": 9.0, "currency": "CNY"}},
            "capture_mode": "passive",
            "source_type": "audio",
            "confidence": 0.95,
            "evidence": {},
            "review_status": "confirmed",
        },
    ]
    (date_dir / "events.json").write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    return job_id, "2026-10-02/events.json"

