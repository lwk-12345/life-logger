"""Lightweight data flywheel for capturing user feedback without prompt bloat.

Stores user corrections, confirmations, and dismissed events into durable disk
records (output/local-web/flywheel/). Strictly caps dynamically retrieved hints
to at most 2 items (<100 chars) to prevent prompt bloat and attention distraction.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FLYWHEEL_ROOT = PROJECT_ROOT / "output" / "local-web" / "flywheel"


def get_flywheel_root() -> Path:
    env_dir = os.environ.get("LIFE_LOGGER_FLYWHEEL_ROOT")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_FLYWHEEL_ROOT


def record_dismissed_event(
    event: Mapping[str, Any],
    source_text: str = "",
    reason: str = "user_dismissed",
    root: Path | None = None,
) -> Path | None:
    """Save an event dismissed by the user as a negative example."""
    target_root = (root or get_flywheel_root()) / "negatives"
    target_root.mkdir(parents=True, exist_ok=True)
    title = str(event.get("title") or "")
    quote = str((event.get("evidence") or {}).get("quote") or "")
    if not title and not quote:
        return None
    key = hashlib.sha256(f"{title}:{quote}:{reason}".encode("utf-8")).hexdigest()[:16]
    record_file = target_root / f"{key}.json"
    payload = {
        "schema_version": "flywheel-negative-v1",
        "created_at": time.time(),
        "title": title,
        "quote": quote,
        "source_text": source_text,
        "category": (event.get("categories") or ["unknown"])[0],
        "reason": reason,
    }
    record_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return record_file


def record_user_correction(
    original_event: Mapping[str, Any],
    corrected_event: Mapping[str, Any],
    correction_text: str = "",
    root: Path | None = None,
) -> Path | None:
    """Save an event corrected by the user as a positive correction example."""
    target_root = (root or get_flywheel_root()) / "corrections"
    target_root.mkdir(parents=True, exist_ok=True)
    orig_id = str(original_event.get("event_id") or "event")
    key = hashlib.sha256(f"{orig_id}:{time.time()}".encode("utf-8")).hexdigest()[:16]
    record_file = target_root / f"{key}.json"
    payload = {
        "schema_version": "flywheel-correction-v1",
        "created_at": time.time(),
        "original": dict(original_event),
        "corrected": dict(corrected_event),
        "correction_text": correction_text,
    }
    record_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return record_file


def feed_batch_review_decisions(
    job_id: str,
    artifact_path: str,
    decisions: list[dict[str, Any]],
    source_records: list[dict[str, Any]],
    root: Path | None = None,
) -> dict[str, int]:
    """Feed human review decisions (Shift+3 noise/media, Shift+1 self) into Flywheel."""
    target_root = root or get_flywheel_root()
    counts = {"media_noise": 0, "self_confirmed": 0, "other_person": 0}
    by_seg_id = {
        r.get("segment_id"): r
        for r in source_records
        if isinstance(r, dict) and r.get("segment_id")
    }

    for dec in decisions:
        seg_id = dec.get("segment_id")
        action = dec.get("source_decision")
        source_row = by_seg_id.get(seg_id) or {}
        text = str(source_row.get("text") or dec.get("corrected_text") or "").strip()
        if not text:
            continue

        if action in {"non_speech", "media"}:
            # Human explicitly confirmed: this is media / noise!
            record_dismissed_event(
                {
                    "title": text[:30],
                    "categories": ["media_noise"],
                    "evidence": {
                        "quote": text,
                        "segment_id": seg_id,
                        "job_id": job_id,
                        "voice_similarity": source_row.get("voice_similarity"),
                        "audio_content_class": source_row.get(
                            "audio_content_class"
                        ),
                    },
                },
                source_text=text,
                reason="human_review_media_noise",
                root=target_root,
            )
            counts["media_noise"] += 1

        elif action == "self":
            # Human confirmed: this is self speech!
            voice_dir = target_root / "voiceprints"
            voice_dir.mkdir(parents=True, exist_ok=True)
            voice_key = hashlib.sha256(f"{seg_id}:{text}".encode()).hexdigest()[:16]
            v_payload = {
                "schema_version": "flywheel-self-voice-v1",
                "created_at": time.time(),
                "segment_id": seg_id,
                "text": text,
                "job_id": job_id,
                "voice_similarity": source_row.get("voice_similarity"),
                "acoustic_proximity": source_row.get("acoustic_proximity"),
            }
            (voice_dir / f"{voice_key}.json").write_text(
                json.dumps(v_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            counts["self_confirmed"] += 1

        elif action == "other_person":
            counts["other_person"] += 1

    return counts


def get_active_negative_hints(
    batch_records: list[Mapping[str, Any]],
    root: Path | None = None,
    max_hints: int = 2,
    max_total_chars: int = 100,
) -> list[str]:
    """Retrieve at most max_hints relevant negative guidelines, capped strictly by character count.

    Never blows up the prompt or exceeds context budget.
    """
    target_root = (root or get_flywheel_root()) / "negatives"
    if not target_root.is_dir():
        return []
    combined_text = " ".join(str(r.get("text") or "") for r in batch_records)
    if not combined_text:
        return []

    hints = []
    total_chars = 0
    # Read available negative examples
    for path in sorted(target_root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        quote = str(item.get("quote") or "").strip()
        title = str(item.get("title") or "").strip()
        category = str(item.get("category") or "").strip()
        # Only suggest if relevant keywords overlap with current text
        overlap = any(kw in combined_text for kw in [quote[:4], title[:4]] if len(kw) >= 2)
        if overlap:
            hint = f"避坑: ‘{quote or title}’非生活事件(分类:{category})"
            if total_chars + len(hint) <= max_total_chars:
                hints.append(hint)
                total_chars += len(hint)
                if len(hints) >= max_hints:
                    break
    return hints
