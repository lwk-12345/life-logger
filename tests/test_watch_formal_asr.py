from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import life_logger.watch_formal_asr as formal_asr_module
from life_logger.local_web import JobStore
from life_logger.watch_formal_asr import WatchFormalASRProcessor
from life_logger.watch_stream import WatchSessionStore


def _receive_one_segment(store: WatchSessionStore, *, device: str, session: str) -> bytes:
    started_at = "2026-10-06T10:00:00+08:00"
    store.start_session(
        device_id=device,
        session_id=session,
        started_at=started_at,
    )
    audio = b"sealed and checksum-verified Watch segment"
    store.receive_segment(
        device_id=device,
        session_id=session,
        sequence=0,
        start_offset_ms=0,
        duration_ms=2_000,
        checksum="sha256:" + hashlib.sha256(audio).hexdigest(),
        body=audio,
    )
    return audio


def test_watch_formal_asr_checkpoints_segments_and_materializes_without_second_asr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_store = WatchSessionStore(tmp_path / "watch-sessions")
    audio = _receive_one_segment(
        session_store,
        device="watch-v47",
        session="session-formal-asr",
    )
    asr_script = (
        "import json,pathlib,sys; "
        "root=pathlib.Path(sys.argv[1]); root.mkdir(parents=True,exist_ok=True); "
        "rows=[{'start_offset_seconds':0.25,'end_offset_seconds':1.5,"
        "'text':'这一段正式ASR已经落盘','source':'funasr','language':'zh'},"
        "{'start_offset_seconds':1.75,'text':'结束时间缺省时安全回落到起点'}]; "
        "(root/'run-summary.json').write_text(json.dumps({'status':'completed',"
        "'transcript_count':2}),encoding='utf-8'); "
        "(root/'transcript.json').write_text(json.dumps(rows,ensure_ascii=False),encoding='utf-8'); "
        "(root/'recordings.json').write_text(json.dumps([{'recording_id':'segment'}]),encoding='utf-8')"
    )
    monkeypatch.setattr(formal_asr_module, "load_local_config", lambda _path: {})
    monkeypatch.setattr(formal_asr_module, "validate_local_pipeline_config", lambda _config: None)
    monkeypatch.setattr(formal_asr_module, "local_pipeline_env", lambda _config: os.environ.copy())
    monkeypatch.setattr(
        formal_asr_module,
        "build_asr_command",
        lambda **kwargs: [sys.executable, "-c", asr_script, str(kwargs["output_dir"])],
    )
    processor = WatchFormalASRProcessor(
        session_store,
        tmp_path / "config.json",
        threading.Event(),
        slot_acquirer=lambda owner: {"owner": owner},
        slot_releaser=lambda _slot: None,
    )

    assert processor.scan_once() is True
    segment_state_path = (
        session_store.session_directory("watch-v47", "session-formal-asr")
        / "formal-asr/segments/000000/state.json"
    )
    segment_state = json.loads(segment_state_path.read_text(encoding="utf-8"))
    assert segment_state["status"] == "completed"
    assert segment_state["source_sha256"] == "sha256:" + hashlib.sha256(audio).hexdigest()
    assert segment_state["rows"][0]["text"] == "这一段正式ASR已经落盘"
    assert segment_state["rows"][0]["start_offset_seconds"] == 0.25
    assert segment_state["rows"][1]["end_offset_seconds"] == 1.75
    assert segment_state["rows"][1]["absolute_ended_at"].endswith("10:00:01.750000+08:00")

    # A fresh processor instance resumes from the durable checkpoint instead of rerunning ASR.
    restarted = WatchFormalASRProcessor(
        session_store,
        tmp_path / "config.json",
        threading.Event(),
        slot_acquirer=lambda _owner: pytest.fail("completed segment must not acquire an ASR slot"),
        slot_releaser=lambda _slot: None,
    )
    assert restarted.scan_once() is False
    progress = restarted.summary_for_session("watch-v47", "session-formal-asr")
    assert progress["counts"]["completed"] == 1
    assert progress["transcript_count"] == 2
    assert progress["transcripts"][0]["text"] == "这一段正式ASR已经落盘"

    session_store.complete_session(
        device_id="watch-v47",
        session_id="session-formal-asr",
        expected_segments=1,
        ended_at="2026-10-06T10:00:02+08:00",
    )
    recording_path = tmp_path / "assembled-recording.m4a"
    recording_path.write_bytes(b"assembled full session")
    monkeypatch.setattr(
        formal_asr_module,
        "discover_recordings",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                to_dict=lambda: {
                    "recording_id": "full-session-recording",
                    "duration_seconds": 2.0,
                    "filename": "recording.m4a",
                }
            )
        ],
    )
    target_asr_dir = tmp_path / "job" / "asr"
    assert restarted.materialize_for_job(
        "watch-v47",
        "session-formal-asr",
        recording_path,
        target_asr_dir,
        "Asia/Shanghai",
    ) is True
    transcript = json.loads((target_asr_dir / "transcript.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((target_asr_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert transcript[0]["recording_id"] == "full-session-recording"
    assert transcript[0]["text"] == "这一段正式ASR已经落盘"
    assert checkpoint["progress_unit"] == "segments"
    assert checkpoint["completed_windows"] == checkpoint["total_windows"] == 1


def test_hung_segment_asr_times_out_and_persists_retryable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_store = WatchSessionStore(tmp_path / "watch-sessions")
    _receive_one_segment(
        session_store,
        device="watch-v47",
        session="session-asr-timeout",
    )
    monkeypatch.setattr(formal_asr_module, "load_local_config", lambda _path: {})
    monkeypatch.setattr(formal_asr_module, "validate_local_pipeline_config", lambda _config: None)
    monkeypatch.setattr(formal_asr_module, "local_pipeline_env", lambda _config: os.environ.copy())
    monkeypatch.setattr(
        formal_asr_module,
        "build_asr_command",
        lambda **_kwargs: ["fake-funasr"],
    )

    class HungProcess:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def wait(self, timeout: float | None = None) -> int:
            if self.returncode is None:
                raise subprocess.TimeoutExpired("fake-funasr", timeout)
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    hung = HungProcess()
    monkeypatch.setattr(formal_asr_module.subprocess, "Popen", lambda *_args, **_kwargs: hung)
    released: list[dict] = []
    processor = WatchFormalASRProcessor(
        session_store,
        tmp_path / "config.json",
        threading.Event(),
        process_timeout_seconds=0.01,
        slot_acquirer=lambda owner: {"owner": owner},
        slot_releaser=released.append,
    )

    assert processor.scan_once() is True
    state = json.loads(
        (
            session_store.session_directory("watch-v47", "session-asr-timeout")
            / "formal-asr/segments/000000/state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["status"] == "failed"
    assert "timed out" in state["error"]
    assert state["retry_at"] > 0
    assert hung.returncode == -15
    assert len(released) == 1


def test_failed_segment_is_retried_when_backoff_is_due_and_can_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_store = WatchSessionStore(tmp_path / "watch-sessions")
    _receive_one_segment(
        session_store,
        device="watch-v47",
        session="session-retry-after-backoff",
    )
    now = [1_800_000_000.0]
    monkeypatch.setattr(formal_asr_module.time, "time", lambda: now[0])
    monkeypatch.setattr(formal_asr_module, "load_local_config", lambda _path: {})
    validation = {"fails": True}

    def validate(_config: dict) -> None:
        if validation["fails"]:
            raise RuntimeError("temporary configuration failure")

    monkeypatch.setattr(formal_asr_module, "validate_local_pipeline_config", validate)
    processor = WatchFormalASRProcessor(
        session_store,
        tmp_path / "config.json",
        threading.Event(),
        slot_acquirer=lambda owner: {"owner": owner},
        slot_releaser=lambda _slot: None,
    )

    assert processor.scan_once() is True
    state_path = (
        session_store.session_directory("watch-v47", "session-retry-after-backoff")
        / "formal-asr/segments/000000/state.json"
    )
    failed_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert failed_state["status"] == "failed"
    assert failed_state["attempt"] == 1
    summary = processor.summary_for_session("watch-v47", "session-retry-after-backoff")
    assert summary["automatic_retry"] == {
        "failed_segments": 1,
        "waiting_segments": 1,
        "next_retry_in_seconds": 30,
    }

    validation["fails"] = False
    monkeypatch.setattr(formal_asr_module, "local_pipeline_env", lambda _config: os.environ.copy())
    success_script = (
        "import json,pathlib,sys; root=pathlib.Path(sys.argv[1]); "
        "(root/'run-summary.json').write_text(json.dumps({'status':'completed','transcript_count':1})); "
        "(root/'transcript.json').write_text(json.dumps([{'text':'retry succeeded'}])); "
        "(root/'recordings.json').write_text('[]')"
    )
    monkeypatch.setattr(
        formal_asr_module,
        "build_asr_command",
        lambda **kwargs: [sys.executable, "-c", success_script, str(kwargs["output_dir"])],
    )
    now[0] = failed_state["retry_at"]

    assert processor.scan_once() is True
    completed_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert completed_state["status"] == "completed"
    assert completed_state["attempt"] == 2
    assert completed_state["rows"][0]["text"] == "retry succeeded"
    summary = processor.summary_for_session("watch-v47", "session-retry-after-backoff")
    assert summary["automatic_retry"] == {
        "failed_segments": 0,
        "waiting_segments": 0,
        "next_retry_in_seconds": None,
    }


def test_corrupt_utf8_segment_state_is_treated_as_pending(tmp_path: Path) -> None:
    session_store = WatchSessionStore(tmp_path / "watch-sessions")
    _receive_one_segment(
        session_store,
        device="watch-v47",
        session="session-corrupt-state",
    )
    state_path = (
        session_store.session_directory("watch-v47", "session-corrupt-state")
        / "formal-asr/segments/000000/state.json"
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(b"\xff\xfe invalid utf-8")
    processor = WatchFormalASRProcessor(
        session_store,
        tmp_path / "config.json",
        threading.Event(),
    )

    progress = processor.summary_for_session("watch-v47", "session-corrupt-state")
    assert progress["counts"]["pending"] == 1
    assert progress["automatic_retry"] == {
        "failed_segments": 0,
        "waiting_segments": 0,
        "next_retry_in_seconds": None,
    }


def test_transcript_summary_sorts_invalid_offsets_after_finite_offsets(tmp_path: Path) -> None:
    session_store = WatchSessionStore(tmp_path / "watch-sessions")
    _receive_one_segment(
        session_store,
        device="watch-v47",
        session="session-malformed-offsets",
    )
    manifest = session_store.load_session("watch-v47", "session-malformed-offsets")
    state_path = (
        session_store.session_directory("watch-v47", "session-malformed-offsets")
        / "formal-asr/segments/000000/state.json"
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "status": "completed",
            "source_sha256": manifest["segments"][0]["sha256"],
            "rows": [
                {"segment_id": "invalid-text", "start_offset_seconds": "not-a-number", "text": "x"},
                {"segment_id": "later", "start_offset_seconds": 2.0, "text": "x"},
                {"segment_id": "invalid-infinity", "start_offset_seconds": "Infinity", "text": "x"},
                {"segment_id": "earlier", "start_offset_seconds": 1.0, "text": "x"},
            ],
        }),
        encoding="utf-8",
    )
    processor = WatchFormalASRProcessor(
        session_store,
        tmp_path / "config.json",
        threading.Event(),
    )

    progress = processor.summary_for_session("watch-v47", "session-malformed-offsets")
    assert [row["segment_id"] for row in progress["transcripts"]] == [
        "earlier",
        "later",
        "invalid-infinity",
        "invalid-text",
    ]


def test_watch_import_waits_for_formal_asr_materialization_and_reuses_reserved_job(
    tmp_path: Path,
) -> None:
    session_store = WatchSessionStore(tmp_path / "watch-sessions")
    _receive_one_segment(
        session_store,
        device="watch-v47",
        session="session-retry",
    )
    session_store.complete_session(
        device_id="watch-v47",
        session_id="session-retry",
        expected_segments=1,
        ended_at="2026-10-06T10:00:02+08:00",
    )
    session_dir = session_store.session_directory("watch-v47", "session-retry")
    (session_dir / "recording.m4a").write_bytes(b"assembled full session")
    manifest = session_store.load_session("watch-v47", "session-retry")
    jobs = JobStore(tmp_path / "jobs", recover=False)
    reserved_job_id = jobs.create_watch_pending_job(
        device_id="watch-v47",
        session_id="session-retry",
        start_at=manifest["started_at"],
        timezone_name="Asia/Shanghai",
    )

    class DeferredMaterializer:
        store = session_store
        ready = False

        def materialize_for_job(
            self,
            _device: str,
            _session: str,
            _recording: Path,
            target: Path,
            _timezone: str,
        ) -> bool:
            if not self.ready:
                return False
            target.mkdir(parents=True, exist_ok=True)
            transcript = [{
                "recording_id": "full-session",
                "segment_id": "full-session_seg_000001",
                "start_offset_seconds": 0.0,
                "end_offset_seconds": 1.0,
                "text": "正式转写完成",
            }]
            artifacts = {
                "transcript.json": transcript,
                "recordings.json": [{"recording_id": "full-session"}],
                "checkpoint.json": {
                    "status": "completed",
                    "completed_windows": 1,
                    "total_windows": 1,
                    "progress_unit": "segments",
                    "transcripts": transcript,
                },
                "run-summary.json": {"status": "completed", "transcript_count": 1},
            }
            for name, content in artifacts.items():
                (target / name).write_text(
                    json.dumps(content, ensure_ascii=False), encoding="utf-8"
                )
            return True

    materializer = DeferredMaterializer()
    with pytest.raises(ValueError, match="保留等待重试"):
        jobs.import_watch_recording(
            session_dir,
            {**manifest, "complete": True},
            tmp_path / "config.json",
            auto_start=False,
            formal_asr_processor=materializer,
            reserved_job_id=reserved_job_id,
        )

    waiting = jobs.read(reserved_job_id)
    assert waiting["status"] == "draft"
    assert len(waiting["files"]) == 1
    assert "asr" not in waiting["files"][0]["stages"]
    assert waiting["watch_pending_session"]["session_id"] == "session-retry"

    materializer.ready = True
    imported = jobs.import_watch_recording(
        session_dir,
        {**manifest, "complete": True},
        tmp_path / "config.json",
        auto_start=False,
        formal_asr_processor=materializer,
        reserved_job_id=reserved_job_id,
    )
    assert imported["job_id"] == reserved_job_id
    assert imported["reused"] is True
    ready = jobs.read(reserved_job_id)
    assert ready["files"][0]["stages"]["asr"]["status"] == "completed"
    assert ready["files"][0]["stages"]["asr"]["source"] == "watch_segment_checkpoints"
    assert "watch_pending_session" not in ready


def test_watch_formal_asr_skips_pure_silent_segment_via_vad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_store = WatchSessionStore(tmp_path / "watch-sessions")
    _receive_one_segment(
        session_store,
        device="watch-v47",
        session="session-vad-silence",
    )
    monkeypatch.setattr(formal_asr_module, "load_local_config", lambda _path: {})
    monkeypatch.setattr(formal_asr_module, "validate_local_pipeline_config", lambda _config: None)

    # 模拟 VAD 识别出纯静音段
    monkeypatch.setattr(formal_asr_module, "is_segment_silence", lambda _audio: True)

    slot_acquired = []
    processor = WatchFormalASRProcessor(
        session_store,
        tmp_path / "config.json",
        threading.Event(),
        slot_acquirer=lambda owner: slot_acquired.append(owner) or {"owner": owner},
        slot_releaser=lambda _slot: None,
    )

    # 处理纯静音段：应该直接返回 True，但决不占用 ASR 槽位
    assert processor.scan_once() is True
    assert len(slot_acquired) == 0  # 绝未申请槽位

    segment_state_path = (
        session_store.session_directory("watch-v47", "session-vad-silence")
        / "formal-asr/segments/000000/state.json"
    )
    segment_state = json.loads(segment_state_path.read_text(encoding="utf-8"))
    assert segment_state["status"] == "no_speech"
    assert segment_state["rows"] == []
    assert segment_state["error"] is None




# ─────────────────────────────────────────────────────────────────────────────
# [2026-11-06 Q958] 积压修复的两条护栏。
#   ① 盘上已有完成产物（run-summary=completed + transcript 齐全）→ **不得重跑 ASR**
#   ② 失败到上限 → 标死并让队列继续，不得反复占用槽位
# 这两条在修复前都会失败（老代码会去调 build_asr_command，用例里它一被调用就 fail）。
# ─────────────────────────────────────────────────────────────────────────────

def _make_processor(tmp_path: Path, session_store: WatchSessionStore, monkeypatch):
    monkeypatch.setattr(formal_asr_module, "load_local_config", lambda _path: {})
    monkeypatch.setattr(formal_asr_module, "validate_local_pipeline_config", lambda _config: None)
    monkeypatch.setattr(formal_asr_module, "local_pipeline_env", lambda _config: os.environ.copy())
    return WatchFormalASRProcessor(
        session_store,
        tmp_path / "config.json",
        threading.Event(),
        slot_acquirer=lambda owner: {"owner": owner},
        slot_releaser=lambda _slot: None,
    )


def _write_completed_artifacts(segment_dir: Path) -> None:
    segment_dir.mkdir(parents=True, exist_ok=True)
    (segment_dir / "run-summary.json").write_text(
        json.dumps({"status": "completed", "transcript_count": 1,
                    "source": str(segment_dir.parents[1] / "segments" / "segment-000000.m4a")}),
        encoding="utf-8")
    (segment_dir / "transcript.json").write_text(
        json.dumps([{"start_offset_seconds": 0.5, "end_offset_seconds": 1.5,
                     "text": "这一段之前已经转好了", "source": "moss"}], ensure_ascii=False),
        encoding="utf-8")
    (segment_dir / "recordings.json").write_text(json.dumps([{"recording_id": "segment"}]),
                                                 encoding="utf-8")


def test_completed_artifacts_are_adopted_instead_of_rerunning_asr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """state 停在 processing、但产物已完整 → 直接收尾，不再跑一遍 ASR（000031 的病）。"""
    session_store = WatchSessionStore(tmp_path / "watch-sessions")
    audio = _receive_one_segment(session_store, device="watch-v47", session="session-adopt")
    source_hash = "sha256:" + hashlib.sha256(audio).hexdigest()
    segment_dir = (session_store.session_directory("watch-v47", "session-adopt")
                   / "formal-asr/segments/000000")
    _write_completed_artifacts(segment_dir)
    # 上一轮把 state 写成 processing（attempt=3），产物却是完整的
    (segment_dir / "state.json").write_text(json.dumps({
        "status": "processing", "sequence": 0, "source_sha256": source_hash, "attempt": 3,
        "updated_at": "2026-11-06T22:57:14+08:00"}), encoding="utf-8")

    monkeypatch.setattr(formal_asr_module, "build_asr_command",
                        lambda **_kw: pytest.fail("已有完成产物时不得重跑 ASR"))
    processor = _make_processor(tmp_path, session_store, monkeypatch)

    assert processor.scan_once() is True
    state = json.loads((segment_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    assert state["adopted_existing_artifacts"] is True
    assert state["rows"][0]["text"] == "这一段之前已经转好了"
    assert state["attempt"] == 3, "复用产物不该把 attempt 继续往上加"


def test_attempt_limit_marks_failed_and_does_not_block_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败到上限 → 标 failed+exhausted，之后不再占槽位（否则队首永远卡在这一段）。"""
    session_store = WatchSessionStore(tmp_path / "watch-sessions")
    audio = _receive_one_segment(session_store, device="watch-v47", session="session-exhaust")
    source_hash = "sha256:" + hashlib.sha256(audio).hexdigest()
    segment_dir = (session_store.session_directory("watch-v47", "session-exhaust")
                   / "formal-asr/segments/000000")
    segment_dir.mkdir(parents=True, exist_ok=True)
    (segment_dir / "state.json").write_text(json.dumps({
        "status": "failed", "sequence": 0, "source_sha256": source_hash,
        "attempt": formal_asr_module.MAX_SEGMENT_ATTEMPTS,
        "error": "ASR segment timed out", "retry_at": 0}), encoding="utf-8")
    # 没有可用产物 → 走到上限判定
    monkeypatch.setattr(formal_asr_module, "build_asr_command",
                        lambda **_kw: pytest.fail("到上限后不得再跑 ASR"))
    processor = _make_processor(tmp_path, session_store, monkeypatch)

    assert processor.scan_once() is True, "到上限要结案（让扫描继续），而不是静默跳过"
    state = json.loads((segment_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["exhausted"] is True
    assert "retry limit" in state["error"]

    # 第二次扫描：不再碰这一段（不拿槽位、不重跑）
    blocking = WatchFormalASRProcessor(
        session_store, tmp_path / "config.json", threading.Event(),
        slot_acquirer=lambda _owner: pytest.fail("已结案的段不得再占用 ASR 槽位"),
        slot_releaser=lambda _slot: None,
    )
    monkeypatch.setattr(formal_asr_module, "load_local_config", lambda _path: {})
    assert blocking.scan_once() is False


def test_scan_once_processes_oldest_session_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """会话顺序必须是先旧后新：新会话不能把老会话（白天那段 525 段）饿死。"""
    session_store = WatchSessionStore(tmp_path / "watch-sessions")
    _receive_one_segment(session_store, device="watch-v47", session="session-old")
    # 老会话整体挪到更早的时间点
    manifest_path = session_store.session_directory("watch-v47", "session-old") / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["started_at"] = "2026-11-06T08:15:00+08:00"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    # 新会话也必须有真实分段，否则 list_session_summaries 里根本看不到它，用例会假通过
    _receive_one_segment(session_store, device="watch-v47", session="session-new")
    manifest_new = session_store.session_directory("watch-v47", "session-new") / "manifest.json"
    data_new = json.loads(manifest_new.read_text(encoding="utf-8"))
    data_new["started_at"] = "2026-11-06T21:51:00+08:00"
    manifest_new.write_text(json.dumps(data_new, ensure_ascii=False), encoding="utf-8")

    processor = _make_processor(tmp_path, session_store, monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(processor, "_process_segment",
                        lambda device, session, manifest, segment: order.append(session) or True)
    processor.scan_once()
    assert order and order[0] == "session-old", f"应当先处理更早的会话，实际：{order}"
