"""手表录音 30 天滚动备份：不碰源文件、只清超期副本、空间不足会通知。"""
import importlib.util
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("backup_audio", ROOT / "scripts" / "backup_audio.py")
backup_audio = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup_audio)

DAY = 86400
GIB = 2**30


def _audio(root: Path, rel: str, age_days: float, body: bytes = b"audio") -> Path:
    path = root / "output" / "local-web" / "watch-sessions" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


def _run(root: Path, dest: Path, **kw):
    args = dict(days=30, warn_days=14, min_free_gb=0.0, dry_run=False, notify=False)
    args.update(kw)
    return backup_audio.run(root, dest, **args)


def test_copies_recent_audio_and_skips_old(tmp_path):
    _audio(tmp_path, "dev/s1/a.m4a", 1)
    _audio(tmp_path, "dev/s1/old.m4a", 40)
    report = _run(tmp_path, tmp_path / "b")
    assert report["copied"] == 1
    assert (tmp_path / "b/watch-sessions/dev/s1/a.m4a").read_bytes() == b"audio"
    assert not (tmp_path / "b/watch-sessions/dev/s1/old.m4a").exists()


def test_second_run_does_not_copy_again(tmp_path):
    _audio(tmp_path, "dev/s1/a.m4a", 1)
    _run(tmp_path, tmp_path / "b")
    report = _run(tmp_path, tmp_path / "b")
    assert report["copied"] == 0
    assert report["already_backed_up"] == 1


def test_prune_removes_only_expired_copies_and_never_the_source(tmp_path):
    src_old = _audio(tmp_path, "dev/s1/old.m4a", 40)
    _audio(tmp_path, "dev/s1/new.m4a", 1)
    expired = tmp_path / "b/watch-sessions/dev/s1/old.m4a"
    expired.parent.mkdir(parents=True)
    expired.write_bytes(b"audio")
    stamp = time.time() - 40 * DAY
    os.utime(expired, (stamp, stamp))
    report = _run(tmp_path, tmp_path / "b")
    assert report["pruned"] == 1
    assert not expired.exists()
    assert src_old.exists()
    assert (tmp_path / "b/watch-sessions/dev/s1/new.m4a").exists()


def test_backup_keeps_preserved_mtime_so_retention_counts_from_recording(tmp_path):
    src = _audio(tmp_path, "dev/s1/a.m4a", 5)
    _run(tmp_path, tmp_path / "b")
    copy = tmp_path / "b/watch-sessions/dev/s1/a.m4a"
    assert abs(copy.stat().st_mtime - src.stat().st_mtime) < 2


def test_dry_run_writes_nothing(tmp_path):
    _audio(tmp_path, "dev/s1/a.m4a", 1)
    report = _run(tmp_path, tmp_path / "b", dry_run=True)
    assert report["copied"] == 1
    assert not (tmp_path / "b").exists()


def test_refuses_destination_inside_source(tmp_path):
    _audio(tmp_path, "dev/s1/a.m4a", 1)
    dest = tmp_path / "output/local-web/watch-sessions/backup"
    try:
        _run(tmp_path, dest)
    except ValueError:
        return
    raise AssertionError("备份目录在源目录内必须拒绝")


def test_reports_same_disk_clone_does_not_protect_against_disk_failure(tmp_path):
    _audio(tmp_path, "dev/s1/a.m4a", 1)
    report = _run(tmp_path, tmp_path / "b")
    assert report["mode"] == "apfs_clone_same_disk"
    assert report["protects_against_disk_failure"] is False


def test_space_warning_when_days_left_is_short():
    warning, days_left = backup_audio.space_warning(10 * GIB, GIB, warn_days=14, min_free_gb=1)
    assert days_left == 10
    assert "10 天" in warning


def test_space_warning_when_free_is_below_floor():
    warning, _ = backup_audio.space_warning(5 * GIB, 0, warn_days=14, min_free_gb=10)
    assert "低于" in warning


def test_no_warning_when_space_is_fine():
    warning, _ = backup_audio.space_warning(100 * GIB, GIB, warn_days=14, min_free_gb=10)
    assert warning is None


def test_warning_triggers_notification(tmp_path, monkeypatch):
    _audio(tmp_path, "dev/s1/a.m4a", 1)
    calls = []
    monkeypatch.setattr(backup_audio, "send_notification", lambda root, title, body: calls.append(body) or {"mac": "sent"})
    report = _run(tmp_path, tmp_path / "b", min_free_gb=10**9, notify=True)
    assert calls and report["warning"]
    assert report["notified"] == {"mac": "sent"}


def _segment(root: Path, session: str, seq: int, age_days: float, status="completed") -> Path:
    path = _audio(root, f"dev/{session}/segments/segment-{seq:06d}.m4a", age_days, b"x" * 100)
    if status is not None:
        state = root / "output/local-web/watch-sessions" / f"dev/{session}/formal-asr/segments/{seq:06d}/state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"status": "%s"}' % status)
    return path


def _reclaim(root: Path, free_sequence, **kw):
    free = iter(free_sequence)
    source = root / "output/local-web/watch-sessions"
    return backup_audio.reclaim_oldest_audio(
        source, now=time.time(), floor_gb=5, protect_days=30, free_bytes=lambda: next(free), **kw
    )


def test_reclaim_does_nothing_above_the_floor(tmp_path):
    old = _segment(tmp_path, "s", 0, 60)
    result = _reclaim(tmp_path, [6 * GIB])
    assert result["removed"] == 0
    assert old.exists()


def test_reclaim_removes_only_the_oldest_week(tmp_path):
    oldest = _segment(tmp_path, "s", 0, 60)
    same_week = _segment(tmp_path, "s", 1, 55)
    next_week = _segment(tmp_path, "s", 2, 50)
    result = _reclaim(tmp_path, [1 * GIB, 6 * GIB])
    assert result["removed"] == 2
    assert not oldest.exists() and not same_week.exists()
    assert next_week.exists()


def test_reclaim_stops_after_two_weeks(tmp_path):
    first = _segment(tmp_path, "s", 0, 80)
    second = _segment(tmp_path, "s", 1, 70)
    third = _segment(tmp_path, "s", 2, 60)
    result = _reclaim(tmp_path, [GIB, GIB, GIB, GIB])
    assert not first.exists() and not second.exists()
    assert third.exists()
    assert result["weeks"] == 2


def test_reclaim_never_touches_the_last_30_days(tmp_path):
    recent = _segment(tmp_path, "s", 0, 29)
    result = _reclaim(tmp_path, [GIB, GIB])
    assert recent.exists()
    assert result["removed"] == 0
    assert result["blocked"] is True


def test_reclaim_keeps_audio_that_has_no_finished_transcript(tmp_path):
    for seq, status in enumerate((None, "pending", "processing", "failed")):
        _segment(tmp_path, "s", seq, 60, status=status)
    result = _reclaim(tmp_path, [GIB, GIB, GIB])
    assert result["removed"] == 0
    assert result["skipped_no_transcript"] == 4
    assert result["blocked"] is True


def test_reclaim_accepts_no_speech_as_finished(tmp_path):
    silent = _segment(tmp_path, "s", 0, 60, status="no_speech")
    _reclaim(tmp_path, [GIB, 6 * GIB])
    assert not silent.exists()


def test_reclaim_leaves_transcripts_on_disk(tmp_path):
    _segment(tmp_path, "s", 0, 60)
    state = tmp_path / "output/local-web/watch-sessions/dev/s/formal-asr/segments/000000/state.json"
    _reclaim(tmp_path, [GIB, 6 * GIB])
    assert state.exists()


def test_reclaim_keeps_merged_recording_while_segments_are_unfinished(tmp_path):
    merged = _audio(tmp_path, "dev/s/recording.m4a", 60)
    _segment(tmp_path, "s", 0, 60, status="pending")
    _reclaim(tmp_path, [GIB, GIB, GIB])
    assert merged.exists()


def test_reclaim_removes_merged_recording_when_segments_are_finished(tmp_path):
    merged = _audio(tmp_path, "dev/s/recording.m4a", 60)
    _segment(tmp_path, "s", 0, 60)
    _reclaim(tmp_path, [GIB, 6 * GIB])
    assert not merged.exists()


def test_reclaim_dry_run_deletes_nothing(tmp_path):
    old = _segment(tmp_path, "s", 0, 60)
    result = _reclaim(tmp_path, [GIB, GIB, GIB], dry_run=True)
    assert old.exists()
    assert result["removed"] == 1


def test_run_reports_and_notifies_after_reclaim(tmp_path, monkeypatch):
    _segment(tmp_path, "s", 0, 60)
    calls = []
    monkeypatch.setattr(backup_audio, "send_notification", lambda root, title, body: calls.append(body) or {})
    report = _run(tmp_path, tmp_path / "b", notify=True, floor_gb=10**9, min_free_gb=0.0)
    assert report["reclaim"]["removed"] == 1
    assert any("删除" in body for body in calls)


def _job(root: Path, job_id: str, status="completed", transcript=True):
    job = root / "output/local-web/jobs" / job_id
    job.mkdir(parents=True)
    (job / "status.json").write_text(json.dumps({"status": status}))
    if transcript:
        result = job / "files/h/result/2026-10-01"
        result.mkdir(parents=True)
        (result / "transcript.json").write_text("{}")


def _manifest(root: Path, session: str, job_id: str):
    path = root / "output/local-web/watch-sessions" / f"dev/{session}/manifest.json"
    path.write_text(json.dumps({"job_id": job_id}))


def test_reclaim_accepts_audio_transcribed_by_a_finished_job(tmp_path):
    old = _segment(tmp_path, "s", 0, 60, status=None)
    _job(tmp_path, "job-1")
    _manifest(tmp_path, "s", "job-1")
    _reclaim(tmp_path, [GIB, 6 * GIB])
    assert not old.exists()


def test_reclaim_keeps_audio_when_job_has_no_transcript_or_failed(tmp_path):
    a = _segment(tmp_path, "a", 0, 60, status=None)
    _job(tmp_path, "job-a", transcript=False)
    _manifest(tmp_path, "a", "job-a")
    b = _segment(tmp_path, "b", 0, 60, status=None)
    _job(tmp_path, "job-b", status="failed")
    _manifest(tmp_path, "b", "job-b")
    _reclaim(tmp_path, [GIB, GIB, GIB])
    assert a.exists() and b.exists()
