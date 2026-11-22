"""Process Apple Watch session with local MLX Whisper Large-v3-Turbo."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from life_logger.domain import RecordingSegment, TranscriptSegment
from life_logger.transcribe import LocalTurboCompositeTranscriber
from life_logger.voice_triggers import scan_voice_triggers

SESSION_ID = "C1E493876E0C484E8A8060E6D970A090"
SESSION_DIR = REPO_ROOT / f"output/local-web/watch-sessions/watch-41aa6b87347f4f0fb5238b559f705c4d/{SESSION_ID}"
FORMAL_DIR = SESSION_DIR / "formal-asr"
SEGMENTS_DIR = SESSION_DIR / "segments"


def main():
    FORMAL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"=== 启动录音会话 [{SESSION_ID}] 本地 MLX Whisper Large-v3-Turbo 处理流水线 ===")
    local_composite = LocalTurboCompositeTranscriber()

    manifest_file = SESSION_DIR / "manifest.json"
    if not manifest_file.is_file():
        print("错误: manifest.json 不存在")
        return

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    segments = manifest.get("segments", [])
    print(f">>> 当前共有 {len(segments)} 个录音切片待处理")

    quick_captures_path = SESSION_DIR / "quick_captures.json"
    existing_captures = []
    if quick_captures_path.is_file():
        try:
            existing_captures = json.loads(quick_captures_path.read_text(encoding="utf-8"))
        except Exception:
            existing_captures = []

    success_count = 0
    fallback_count = 0

    for idx, seg_meta in enumerate(segments):
        seq = seg_meta["sequence"]
        seg_file_name = seg_meta["file"]
        m4a_path = SEGMENTS_DIR / seg_file_name
        if not m4a_path.is_file():
            continue

        seq_dir = FORMAL_DIR / "segments" / f"{seq:06d}"
        seq_state_file = seq_dir / "state.json"

        # Check if already completed
        if seq_state_file.is_file():
            try:
                state_data = json.loads(seq_state_file.read_text(encoding="utf-8"))
                if state_data.get("status") == "completed" and state_data.get("rows"):
                    continue
            except Exception:
                pass

        seq_dir.mkdir(parents=True, exist_ok=True)
        rec = RecordingSegment(
            recording_id=f"rec_{SESSION_ID[:8]}_{seq:06d}",
            source_file=str(m4a_path),
            started_at=seg_meta.get("received_at"),
            ended_at=None,
            duration_seconds=float(seg_meta.get("duration_ms", 60000)) / 1000.0,
            sequence=seq,
            time_source="watch",
            time_precision="exact",
            ingestion_warnings=[],
        )

        t0 = time.time()
        engine_used = "local_turbo_composite"
        transcripts: list[TranscriptSegment] = local_composite.transcribe(rec)

        cost = time.time() - t0
        success_count += 1

        rows = [t.to_dict() for t in transcripts]
        state_payload = {
            "status": "completed",
            "engine": engine_used,
            "duration_s": rec.duration_seconds,
            "cost_seconds": round(cost, 2),
            "rows": rows,
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        seq_state_file.write_text(json.dumps(state_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        # 实时嗅探语音微指令 (记账 / 待办 / 灵感)
        for t in transcripts:
            text = t.text
            if text:
                captures = scan_voice_triggers(text)
                for cap in captures:
                    existing_captures.append({
                        "session_id": SESSION_ID,
                        "sequence": seq,
                        "timestamp": t.absolute_started_at,
                        "text": text,
                        "trigger_type": cap.trigger_type,
                        "raw_text": cap.raw_text,
                        "extracted_content": cap.extracted_content,
                    })

        preview = " / ".join(r.get("text", "") for r in rows)
        if len(preview) > 60:
            preview = preview[:57] + "..."
        print(f"[{idx+1}/{len(segments)}] Seg {seq:06d} [{engine_used}]: {len(rows)} 句 ({cost:.2f}s) -> {preview or '(无语音)'}")

    # 写回语音微指令
    quick_captures_path.write_text(json.dumps(existing_captures, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== 今日录音处理完毕! 成功处理 {success_count} 个切片 (本地兜底 {fallback_count} 次), 捕获微指令 {len(existing_captures)} 条 ===")


if __name__ == "__main__":
    main()
