"""Batch transcribe watch session audio segments using Volcano Doubao Flash ASR."""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_APPID = "5261675940"
DEFAULT_TOKEN = "LNhV_nDmgO1SECuGpCwQk4Dx2RDZqBiy"
DEFAULT_RESOURCE_ID = "volc.bigasr.auc_turbo"
FLASH_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"


def transcribe_single_audio(
    audio_path: Path,
    *,
    appid: str,
    token: str,
    resource_id: str = DEFAULT_RESOURCE_ID,
    max_retries: int = 3,
) -> dict[str, Any]:
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    fmt = audio_path.suffix.lstrip(".").lower()
    if fmt not in {"wav", "mp3", "m4a", "ogg", "opus"}:
        fmt = "m4a"

    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

    for attempt in range(max_retries):
        req_id = str(uuid.uuid4())
        headers = {
            "Content-Type": "application/json",
            "X-Api-App-Key": appid,
            "X-Api-Access-Key": token,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": req_id,
            "X-Api-Sequence": "-1",
        }
        request_payload = {
            "model_name": "bigmodel",
            "enable_punc": True,
            "enable_itn": True,
            "enable_ddc": True,
            "enable_speaker_info": True,
            "ssd_version": "300",
            "enable_gender_detection": True,
            "enable_emotion_detection": True,
            "show_utterances": True,
        }
        body = json.dumps({
            "user": {"uid": "batch_worker"},
            "audio": {"format": fmt, "data": audio_b64},
            "request": request_payload,
        }).encode("utf-8")

        req = urllib.request.Request(FLASH_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data
        except urllib.error.HTTPError as error:
            error_body = error.read().decode("utf-8", errors="replace")
            if attempt == max_retries - 1:
                raise RuntimeError(f"HTTP {error.code}: {error_body}") from error
            time.sleep(1.0 * (2 ** attempt))
        except Exception as error:
            if attempt == max_retries - 1:
                raise RuntimeError(f"Request failed: {error}") from error
            time.sleep(1.0 * (2 ** attempt))

    raise RuntimeError("exhausted retries")


def process_segment_task(
    seg_path: Path,
    out_dir: Path,
    appid: str,
    token: str,
    resource_id: str,
    seq: int,
    base_time: datetime | None,
) -> dict[str, Any]:
    item_json_path = out_dir / "segments" / f"{seq:06d}.json"
    if item_json_path.exists():
        try:
            cached = json.loads(item_json_path.read_text(encoding="utf-8"))
            if cached.get("status") == "success":
                return cached
        except Exception:
            pass

    try:
        raw_result = transcribe_single_audio(
            seg_path,
            appid=appid,
            token=token,
            resource_id=resource_id,
        )
        res = raw_result.get("result", {})
        full_text = res.get("text", "").strip()
        raw_utterances = res.get("utterances", [])

        # Start offset for this segment in seconds (assuming 60s segments or probe duration)
        seg_offset_s = seq * 60.0

        normalized_utterances = []
        for u in raw_utterances:
            u_start_s = round(seg_offset_s + (u.get("start_time", 0) / 1000.0), 3)
            u_end_s = round(seg_offset_s + (u.get("end_time", 0) / 1000.0), 3)
            u_text = str(u.get("text", "")).strip()
            additions = u.get("additions") or {}
            speaker = additions.get("speaker") or u.get("speaker_id") or u.get("speaker") or "speaker_0"
            gender = additions.get("gender") or u.get("gender")
            emotion = additions.get("emotion") or u.get("emotion")

            abs_start = None
            abs_end = None
            if base_time is not None:
                abs_start = (base_time + timedelta(seconds=u_start_s)).isoformat()
                abs_end = (base_time + timedelta(seconds=u_end_s)).isoformat()

            normalized_utterances.append({
                "start_s": u_start_s,
                "end_s": u_end_s,
                "absolute_start": abs_start,
                "absolute_end": abs_end,
                "text": u_text,
                "speaker": speaker,
                "gender": gender,
                "emotion": emotion,
            })

        out_record = {
            "status": "success",
            "sequence": seq,
            "filename": seg_path.name,
            "text": full_text,
            "utterances": normalized_utterances,
            "raw_result": raw_result,
        }
    except Exception as e:
        out_record = {
            "status": "error",
            "sequence": seq,
            "filename": seg_path.name,
            "text": "",
            "utterances": [],
            "error": str(e),
        }

    item_json_path.parent.mkdir(parents=True, exist_ok=True)
    item_json_path.write_text(json.dumps(out_record, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_record


def run_batch_transcribe(
    session_dir: Path,
    *,
    appid: str = DEFAULT_APPID,
    token: str = DEFAULT_TOKEN,
    resource_id: str = DEFAULT_RESOURCE_ID,
    workers: int = 6,
    limit: int | None = None,
) -> None:
    session_dir = Path(session_dir).resolve()
    manifest_path = session_dir / "manifest.json"
    manifest: dict[str, Any] = {}
    base_time: datetime | None = None
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("started_at"):
            try:
                base_time = datetime.fromisoformat(manifest["started_at"])
            except Exception:
                pass

    seg_dir = session_dir / "segments"
    if not seg_dir.exists():
        raise FileNotFoundError(f"segments directory not found in {session_dir}")

    all_segments = sorted(seg_dir.glob("segment-*.m4a"))
    if not all_segments:
        all_segments = sorted(seg_dir.glob("segment-*.wav"))

    if limit is not None and limit > 0:
        all_segments = all_segments[:limit]

    total = len(all_segments)
    print(f"[*] Starting Cloud Doubao ASR for {session_dir.name}")
    print(f"[*] Total segments to process: {total} with {workers} parallel workers")

    out_dir = session_dir / "cloud-doubao-asr"
    (out_dir / "segments").mkdir(parents=True, exist_ok=True)

    completed = 0
    errors = 0
    start_wall = time.time()

    all_records: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for seg in all_segments:
            # extract seq number from segment-000123.m4a
            stem = seg.stem
            try:
                seq = int(stem.split("-")[-1])
            except ValueError:
                seq = 0
            f = pool.submit(
                process_segment_task,
                seg,
                out_dir,
                appid,
                token,
                resource_id,
                seq,
                base_time,
            )
            futures[f] = seq

        for future in as_completed(futures):
            res = future.result()
            all_records.append(res)
            completed += 1
            if res.get("status") == "error":
                errors += 1

            if completed % 20 == 0 or completed == total:
                elapsed = time.time() - start_wall
                rate = completed / elapsed if elapsed > 0 else 0
                remaining = (total - completed) / rate if rate > 0 else 0
                print(
                    f"[{completed}/{total} - {completed*100/total:.1f}%] "
                    f"Speed: {rate:.1f} seg/s | Elapsed: {elapsed:.0f}s | "
                    f"ETA: {remaining:.0f}s | Errors: {errors}",
                    flush=True,
                )

    all_records.sort(key=lambda r: r.get("sequence", 0))

    # Compile combined files
    combined_utterances = []
    text_lines = []
    md_lines = [f"# {session_dir.name} 全天完整云端识别记录 (豆包大模型)\n"]
    if base_time:
        md_lines.append(f"**录音开始时间**: {base_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n---\n")

    for rec in all_records:
        seq = rec.get("sequence", 0)
        for u in rec.get("utterances", []):
            combined_utterances.append(u)
            time_label = f"[{u['start_s']:.1f}s - {u['end_s']:.1f}s]"
            if u.get("absolute_start"):
                try:
                    dt = datetime.fromisoformat(u["absolute_start"])
                    time_label = f"[{dt.strftime('%H:%M:%S')}]"
                except Exception:
                    pass
            line = f"{time_label} {u['text']}"
            text_lines.append(line)
            md_lines.append(f"- **{time_label}** {u['text']}\n")

    combined_json_path = out_dir / "combined_transcript.json"
    combined_json_path.write_text(
        json.dumps(combined_utterances, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    combined_txt_path = out_dir / "combined_transcript.txt"
    combined_txt_path.write_text("\n".join(text_lines), encoding="utf-8")

    combined_md_path = out_dir / "combined_transcript.md"
    combined_md_path.write_text("".join(md_lines), encoding="utf-8")

    total_time = time.time() - start_wall
    print(f"\n[✓] Cloud Doubao ASR 完成！总计 {total} 个切片，耗时 {total_time:.1f} 秒。")
    print(f"    - 完整转写文本: {combined_txt_path}")
    print(f"    - Markdown 带时间戳排版: {combined_md_path}")
    print(f"    - 结构化 JSON: {combined_json_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch Doubao Cloud ASR")
    parser.add_argument(
        "--session-dir",
        type=Path,
        default=Path("output/local-web/watch-sessions/watch-41aa6b87347f4f0fb5238b559f705c4d/5C34927FE4E0493D8F7F2C0E02A05773"),
        help="Watch session directory containing segments/",
    )
    parser.add_argument("--appid", type=str, default=DEFAULT_APPID)
    parser.add_argument("--token", type=str, default=DEFAULT_TOKEN)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    run_batch_transcribe(
        args.session_dir,
        appid=args.appid,
        token=args.token,
        workers=args.workers,
        limit=args.limit,
    )
