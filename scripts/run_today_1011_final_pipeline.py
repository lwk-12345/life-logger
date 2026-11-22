"""Run full pipeline for 10-11 session: transcribe failed segments and import to Job."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from life_logger.local_web import JobStore
from life_logger.transcribe import LocalTurboCompositeTranscriber, RecordingSegment
from life_logger.watch_formal_asr import _atomic_json, _read_json

DEV_ID = "watch-41aa6b87347f4f0fb5238b559f705c4d"
SESS_ID = "C1E493876E0C484E8A8060E6D970A090"

def main():
    session_dir = REPO_ROOT / f"output/local-web/watch-sessions/{DEV_ID}/{SESS_ID}"
    manifest_path = session_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    segments = sorted(manifest.get("segments", []), key=lambda x: int(x["sequence"]))
    formal_asr_dir = session_dir / "formal-asr/segments"
    config_path = REPO_ROOT / "local-config.json"

    print(f"=== 开始处理今日 (10月11日) 会话 [{SESS_ID}]，总计 {len(segments)} 个分段 ===")
    
    transcriber = LocalTurboCompositeTranscriber()
    
    started_at_str = manifest.get("started_at")
    session_start_dt = datetime.fromisoformat(started_at_str)

    retranscribed = 0
    skipped = 0

    for seg in segments:
        seq = int(seg["sequence"])
        seg_dir = formal_asr_dir / f"{seq:06d}"
        state_file = seg_dir / "state.json"
        audio_file = session_dir / "segments" / seg["file"]
        
        state = _read_json(state_file, {}) if state_file.exists() else {}
        
        if state.get("status") in {"completed", "no_speech"}:
            skipped += 1
            continue

        print(f"正在转录分段 {seq:06d} ({seg['file']})...", flush=True)
        seg_dir.mkdir(parents=True, exist_ok=True)
        
        start_ms = seg.get("start_offset_ms", 0)
        dur_ms = seg.get("duration_ms", 60000)
        
        start_sec = start_ms / 1000.0
        dur_sec = dur_ms / 1000.0
        
        rec_start_iso = datetime.fromtimestamp(session_start_dt.timestamp() + start_sec, tz=timezone.utc).isoformat()
        rec_end_iso = datetime.fromtimestamp(session_start_dt.timestamp() + start_sec + dur_sec, tz=timezone.utc).isoformat()
        recording = RecordingSegment(
            recording_id=f"rec_watch_{seq:06d}",
            source_file=str(audio_file),
            started_at=rec_start_iso,
            ended_at=rec_end_iso,
            duration_seconds=dur_sec,
            sequence=seq,
            time_source="user",
            time_precision="exact",
            ingestion_warnings=[],
        )

        t_res = transcriber.transcribe(recording)
        
        rows = []
        for idx, item in enumerate(t_res, start=1):
            rows.append({
                "segment_id": f"watch_{seq:06d}_{idx:06d}",
                "recording_id": item.recording_id,
                "start_offset_seconds": round(start_sec + float(item.start_offset_seconds), 3),
                "end_offset_seconds": round(start_sec + float(item.end_offset_seconds), 3),
                "absolute_started_at": item.absolute_started_at,
                "absolute_ended_at": item.absolute_ended_at,
                "text": item.text,
                "source": "local_whisper_turbo",
                "language": item.language,
                "transcription_confidence": item.transcription_confidence,
                "speaker": item.speaker,
                "emotion": getattr(item, "emotion", "NEUTRAL"),
                "timestamp_type": "hardware_clock_epoch",
            })

        new_status = "completed" if rows else "no_speech"
        new_state = {
            "sequence": seq,
            "status": new_status,
            "source_sha256": seg.get("sha256"),
            "rows": rows,
            "updated_at": datetime.now().isoformat(),
            "asr_provider": "local_turbo",
        }
        _atomic_json(state_file, new_state)
        retranscribed += 1

    # 全部切片完成，先拼接完整音频 recording.m4a
    from life_logger.watch_stream import WatchSessionStore
    watch_store = WatchSessionStore(REPO_ROOT / "output/local-web/watch-sessions")
    print("正在拼装 10月11日 完整音频 recording.m4a...", flush=True)
    watch_store.assemble_recording(DEV_ID, SESS_ID)

    # 全部切片完成，执行最终 Job 物化与事件生成
    store = JobStore(REPO_ROOT / "output/local-web")
    print("正在为 10月11日 会话执行 Job 物化与 DeepSeek 事件抽取...")
    job_id = "job-20261011-180926-331be28c"
    print(f"=== 开始执行 10月11日 今日录音全量事件与日记抽取作业 [{job_id}] ===", flush=True)

    # 运行该 Job
    job_dir = store.job_path(job_id)
    secrets_path = REPO_ROOT / "output/local-web/secrets.json"
    store.prepare_for_start(job_id)
    store._worker(job_id, job_dir, config_path, secrets_path)
    print(f"=== 10月11日 作业 [{job_id}] 运行完毕！当前状态: {store.read(job_id).get('status')} ===")

if __name__ == "__main__":
    main()
