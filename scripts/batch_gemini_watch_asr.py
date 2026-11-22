"""Batch process all remaining segments in a Watch session using local MLX Whisper Large-v3-Turbo."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from life_logger.domain import RecordingSegment
from life_logger.transcribe import get_shared_turbo_transcriber
from life_logger.voice_triggers import QuickCaptureStore, scan_voice_triggers
from life_logger.local_pipeline import load_local_config


def _atomic_json(target: Path, payload: Any) -> None:
    temp = target.with_suffix(f"{target.suffix}.tmp.{os.getpid()}")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)


def _probe_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return 60.0


def _default_session_dir() -> Path:
    """默认会话目录：仓库根 / output/local-web/... （可用 LL_BATCH_SESSION_DIR 覆盖）。"""
    override = os.environ.get("LL_BATCH_SESSION_DIR")
    if override:
        return Path(override)
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / "output" / "local-web" / "watch-sessions" / (
        "watch-41aa6b87347f4f0fb5238b559f705c4d" ) / "901BE63F70EF4937B42E836033B1A8F6"


def main() -> None:
    parser = argparse.ArgumentParser()
    # [2026-11-04 Q860] 仓库路径不再写死：按脚本位置推导（scripts/ 的上一级）；
    # LL_BATCH_SESSION_DIR 可覆盖，默认值等价于改造前的绝对路径。
    parser.add_argument("--session-dir", type=Path,
                        default=_default_session_dir())
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--max-segments", type=int, default=None)
    args = parser.parse_args()

    session_dir = args.session_dir.resolve()
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"Manifest not found: {manifest_path}")
        return

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    segments = manifest.get("segments", [])
    started_at_str = str(manifest.get("started_at") or "")
    session_start = datetime.fromisoformat(started_at_str)

    config_path = REPO_ROOT / "local-config.json"
    _ = load_local_config(config_path)

    transcriber = get_shared_turbo_transcriber()
    capture_store = QuickCaptureStore(REPO_ROOT / "output" / "local-web" / "quick_captures.json")

    formal_dir = session_dir / "formal-asr" / "segments"
    formal_dir.mkdir(parents=True, exist_ok=True)

    sorted_segments = sorted(segments, key=lambda s: int(s.get("sequence", 0)))
    total = len(sorted_segments)
    print(f"Total segments in session: {total}")

    processed_count = 0
    skipped_count = 0

    for idx, seg in enumerate(sorted_segments):
        if args.max_segments and processed_count >= args.max_segments:
            print(f"Reached max segments limit: {args.max_segments}")
            break

        seq = int(seg.get("sequence", 0))
        seg_dir = formal_dir / f"{seq:06d}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        state_path = seg_dir / "state.json"
        checkpoint_path = seg_dir / "checkpoint.json"

        # Check existing state
        if state_path.is_file():
            try:
                state_data = json.loads(state_path.read_text(encoding="utf-8"))
                if (state_data.get("status") in {"completed", "no_speech"} and
                    state_data.get("source_sha256") == seg.get("sha256")):
                    skipped_count += 1
                    continue
            except Exception:
                pass

        audio_path = session_dir / "segments" / f"segment-{seq:06d}.m4a"
        if not audio_path.is_file():
            print(f"[{idx+1}/{total}] Audio file missing: {audio_path}")
            continue

        start_offset_ms = int(seg.get("start_offset_ms", 0))
        seg_start_time = session_start + timedelta(milliseconds=start_offset_ms)
        duration = _probe_duration(audio_path)

        rec_segment = RecordingSegment(
            recording_id=f"rec_watch_{seq:06d}",
            source_file=str(audio_path),
            started_at=seg_start_time.isoformat(),
            ended_at=(seg_start_time + timedelta(seconds=duration)).isoformat(),
            duration_seconds=duration,
            sequence=seq,
            time_source="metadata",
            time_precision="exact",
            ingestion_warnings=[],
        )

        t0 = time.time()
        used_source = "local_whisper_turbo"
        transcripts = transcriber.transcribe(rec_segment)

        elapsed = time.time() - t0

        rows = []
        for i, t in enumerate(transcripts, start=1):
            rel_start = float(t.start_offset_seconds)
            rel_end = float(t.end_offset_seconds)
            abs_start = (seg_start_time + timedelta(seconds=rel_start)).isoformat()
            abs_end = (seg_start_time + timedelta(seconds=rel_end)).isoformat()
            total_start_s = (start_offset_ms / 1000.0) + rel_start
            total_end_s = (start_offset_ms / 1000.0) + rel_end

            row = {
                "segment_id": f"watch_{seq:06d}_{i:06d}",
                "recording_id": f"watch_{seq:06d}",
                "start_offset_seconds": round(total_start_s, 3),
                "end_offset_seconds": round(total_end_s, 3),
                "absolute_started_at": abs_start,
                "absolute_ended_at": abs_end,
                "text": t.text,
                "source": used_source,
                "language": t.language or "zh",
                "speaker": t.speaker,
                "gender": None,
                "emotion": t.emotion,
                "transcription_confidence": t.transcription_confidence,
                "words": None,
            }
            rows.append(row)

            # Voice triggers scan
            try:
                triggers = scan_voice_triggers(t.text, timestamp=abs_start)
                if triggers:
                    capture_store.append(triggers)
            except Exception:
                pass

        checkpoint = {
            "status": "completed",
            "source": str(audio_path),
            "model_path": "Local-Whisper-Turbo-MLX",
            "duration_s": round(duration, 3),
            "transcripts": [
                {
                    "segment_id": f"watch_{seq:06d}_seg_{i:04d}",
                    "recording_id": f"watch_{seq:06d}",
                    "start_offset_seconds": t.start_offset_seconds,
                    "end_offset_seconds": t.end_offset_seconds,
                    "absolute_started_at": (seg_start_time + timedelta(seconds=t.start_offset_seconds)).isoformat(),
                    "absolute_ended_at": (seg_start_time + timedelta(seconds=t.end_offset_seconds)).isoformat(),
                    "text": t.text,
                    "source": used_source,
                    "language": "zh",
                    "transcription_confidence": t.transcription_confidence,
                    "speaker": t.speaker,
                    "gender": None,
                    "emotion": t.emotion,
                }
                for i, t in enumerate(transcripts, start=1)
            ],
            "window_summaries": [],
            "warnings": [],
        }
        _atomic_json(checkpoint_path, checkpoint)

        state = {
            "attempt": 1,
            "error": None,
            "rows": rows,
            "sequence": seq,
            "source_sha256": seg.get("sha256"),
            "status": "completed" if rows else "no_speech",
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        _atomic_json(state_path, state)

        text_preview = " / ".join(r["text"] for r in rows[:2]) if rows else "(no speech)"
        print(f"[{idx+1}/{total}] Seg {seq:06d} [{used_source}]: {state['status']} ({len(rows)} rows in {elapsed:.2f}s) -> {text_preview[:80]}")
        processed_count += 1

    print(f"\nDone batch run! Processed: {processed_count}, Skipped (already done): {skipped_count}, Total: {total}")

    # Check if all segments are completed in the session
    all_done = True
    for seg in sorted_segments:
        s_seq = int(seg.get("sequence", 0))
        st_p = formal_dir / f"{s_seq:06d}" / "state.json"
        if not st_p.is_file():
            all_done = False
            break
        try:
            st_d = json.loads(st_p.read_text(encoding="utf-8"))
            if st_d.get("status") not in {"completed", "no_speech"}:
                all_done = False
                break
        except Exception:
            all_done = False
            break

    if all_done:
        print("\n🎉 All 399 segments are completed! Triggering recording assembly & full ingestion...")
        try:
            from life_logger.watch_session import WatchSessionStore
            from life_logger.job_store import JobStore
            wss = WatchSessionStore(session_dir.parent)
            dev_id = str(manifest.get("device_id") or "watch-41aa6b87347f4f0fb5238b559f705c4d")
            sess_id = str(manifest.get("session_id") or "901BE63F70EF4937B42E836033B1A8F6")
            print("Assembling recording.m4a...")
            wss.assemble_recording(dev_id, sess_id)
            print("Assembled! Importing watch recording with full speaker verification...")
            js = JobStore()
            imported = js.import_watch_recording(
                session_dir,
                {**manifest, "complete": True},
                config_path,
            )
            print("Import successfully finished:", imported.get("job_id"))
        except Exception as err:
            import traceback
            traceback.print_exc()
            print("Auto-assembly error:", err)


if __name__ == "__main__":
    main()
