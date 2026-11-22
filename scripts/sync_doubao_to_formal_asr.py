#!/usr/bin/env python3
"""Sync cloud Doubao ASR transcripts back into formal-asr and associated jobs."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for p in (ROOT, SRC):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))



def sync_session_to_formal(session_dir: Path, sid: str) -> tuple[int, int, int]:
    manifest_p = session_dir / "manifest.json"
    if not manifest_p.exists():
        return 0, 0, 0
    with manifest_p.open(encoding="utf-8") as f:
        manifest = json.load(f)

    started_at_str = manifest.get("started_at")
    started_dt = datetime.fromisoformat(started_at_str)

    cloud_segs_dir = session_dir / "cloud-doubao-asr" / "segments"
    formal_segs_dir = session_dir / "formal-asr" / "segments"
    if not cloud_segs_dir.exists():
        return 0, 0, 0

    manifest_segs = {int(s["sequence"]): s for s in manifest.get("segments", [])}

    total_synced = 0
    speech_count = 0
    no_speech_count = 0

    for seq, seg_info in sorted(manifest_segs.items()):
        cloud_p = cloud_segs_dir / f"{seq:06d}.json"
        if not cloud_p.exists():
            continue
        with cloud_p.open(encoding="utf-8") as f:
            cdata = json.load(f)

        start_offset_ms = int(seg_info.get("start_offset_ms", 0))
        base_seg_dt = started_dt + timedelta(milliseconds=start_offset_ms)

        utterances = cdata.get("utterances", [])
        if not utterances and cdata.get("text", "").strip():
            utterances = [
                {
                    "start_s": 0.0,
                    "end_s": round(int(seg_info.get("duration_ms", 60000)) / 1000.0, 2),
                    "text": cdata.get("text", "").strip(),
                }
            ]

        rows = []
        for idx, u in enumerate(utterances, 1):
            rel_start = float(u.get("start_s", 0))
            rel_end = float(u.get("end_s", rel_start))
            if rel_end < rel_start:
                rel_end = rel_start
            start_sec = rel_start + (start_offset_ms / 1000.0)
            end_sec = rel_end + (start_offset_ms / 1000.0)
            text = str(u.get("text", "")).strip()
            if not text:
                continue
            rows.append(
                {
                    "segment_id": f"watch_{seq:06d}_{idx:06d}",
                    "recording_id": f"rec_doubao_{sid[:8]}",
                    "start_offset_seconds": round(start_sec, 3),
                    "end_offset_seconds": round(end_sec, 3),
                    "absolute_started_at": (
                        base_seg_dt + timedelta(seconds=rel_start)
                    ).isoformat(),
                    "absolute_ended_at": (
                        base_seg_dt + timedelta(seconds=rel_end)
                    ).isoformat(),
                    "text": text,
                    "source": "doubao_asr",
                    "language": "zh",
                    "transcription_confidence": None,
                }
            )

        status = "completed" if rows else "no_speech"
        if status == "completed":
            speech_count += 1
        else:
            no_speech_count += 1

        target_dir = formal_segs_dir / f"{seq:06d}"
        target_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "attempt": 1,
            "error": None,
            "rows": rows,
            "sequence": seq,
            "source_sha256": seg_info.get("sha256"),
            "status": status,
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        with (target_dir / "state.json").open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        total_synced += 1

    return total_synced, speech_count, no_speech_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--watch-root",
        type=Path,
        default=ROOT / "output" / "local-web" / "watch-sessions",
        help="Path to watch-sessions directory",
    )
    args = parser.parse_args()

    for device_dir in args.watch_root.iterdir():
        if not device_dir.is_dir():
            continue
        for session_dir in device_dir.iterdir():
            if not session_dir.is_dir():
                continue
            cloud_dir = session_dir / "cloud-doubao-asr"
            if not cloud_dir.exists():
                continue
            sid = session_dir.name
            total, comp, nospeech = sync_session_to_formal(session_dir, sid)
            print(f"Synced {session_dir.name}: total={total}, completed={comp}, no_speech={nospeech}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
