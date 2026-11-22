#!/usr/bin/env python3
"""Zero-config acceptance check for the bundled Life Logger Demo tier."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from life_logger.daily_brief import (  # noqa: E402
    deliver_daily_brief,
    external_agent_brief,
)
from life_logger.pipeline import run_demo  # noqa: E402

EXPECTED_CATEGORIES = {"routine", "food", "expense", "exercise", "todo"}
TRANSCRIPT_ONLY_TEXTS = {
    "上午和同事沟通了项目方向。",
    "我觉得生活日志应该先做音频主链路。",
}


def _block_network() -> tuple[Any, Any]:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def blocked_connect(self: socket.socket, address: object) -> None:
        del self, address
        raise RuntimeError("Demo tier attempted a network connection")

    def blocked_connect_ex(self: socket.socket, address: object) -> int:
        del self, address
        raise RuntimeError("Demo tier attempted a network connection")

    socket.socket.connect = blocked_connect
    socket.socket.connect_ex = blocked_connect_ex
    return original_connect, original_connect_ex


def _restore_network(originals: tuple[Any, Any]) -> None:
    socket.socket.connect, socket.socket.connect_ex = originals


def _payload_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys.update(_payload_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_payload_keys(child))
    return keys


def verify_demo(output_dir: Path) -> dict[str, object]:
    fixture = PROJECT_ROOT / "fixtures" / "demo_transcript.json"
    if not fixture.is_file():
        raise RuntimeError(f"missing bundled demo fixture: {fixture}")

    originals = _block_network()
    try:
        result = run_demo(output_dir, timezone_name="Asia/Shanghai")
    finally:
        _restore_network(originals)

    events = json.loads(result.events_path.read_text(encoding="utf-8"))
    transcript_path = result.events_path.with_name("transcript.json")
    transcript_markdown = result.events_path.with_name("transcript.md")
    transcripts = json.loads(transcript_path.read_text(encoding="utf-8"))
    categories = {
        category
        for event in events
        for category in event.get("categories", [])
    }
    missing_categories = sorted(EXPECTED_CATEGORIES - categories)
    if result.event_count != 5:
        raise RuntimeError(
            f"expected 5 high-confidence demo events, got {result.event_count}"
        )
    if missing_categories:
        raise RuntimeError(
            "demo fixture missed categories: " + ", ".join(missing_categories)
        )
    if not transcripts or any(item.get("source") != "demo" for item in transcripts):
        raise RuntimeError("demo transcript did not stay on the fixture adapter")
    if len(transcripts) != 7:
        raise RuntimeError(
            f"expected all 7 transcript segments, got {len(transcripts)}"
        )
    transcript_texts = {str(item.get("text") or "") for item in transcripts}
    if not TRANSCRIPT_ONLY_TEXTS.issubset(transcript_texts):
        raise RuntimeError(
            "demo full transcript lost transcript-only conversational text"
        )
    if not transcript_markdown.is_file():
        raise RuntimeError("demo did not write transcript.md")
    markdown_text = transcript_markdown.read_text(encoding="utf-8")
    if any(text not in markdown_text for text in TRANSCRIPT_ONLY_TEXTS):
        raise RuntimeError("transcript.md is missing transcript-only text")
    event_text = "\n".join(
        f"{event.get('title', '')}\n{event.get('evidence', {}).get('quote', '')}"
        for event in events
    )
    if any(text in event_text for text in TRANSCRIPT_ONLY_TEXTS):
        raise RuntimeError(
            "conversational transcript-only text was fabricated into an event"
        )
    expense_events = [
        event for event in events if "expense" in event.get("categories", [])
    ]
    expense_amount = (
        expense_events[0].get("data", {}).get("expense", {}).get("amount")
        if expense_events
        else None
    )
    if expense_amount != 4.9:
        raise RuntimeError("demo clear expense amount was not structured")
    if not any("todo" in event.get("categories", []) for event in events):
        raise RuntimeError("demo clear reminder command was not structured")
    if "faster_whisper" in sys.modules or "funasr" in sys.modules:
        raise RuntimeError("Demo tier loaded a local ASR runtime unexpectedly")

    summary_path = result.events_path.with_name("daily_summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    raw_brief = {
        "schema_version": "daily-brief-v1",
        "date": summary.get("date"),
        "timezone": summary.get("timezone", "Asia/Shanghai"),
        "event_count": len(events),
        "category_summary": summary.get("category_summary", {}),
        "todos": [
            {
                "task_key": event.get("event_id"),
                "event_id": event.get("event_id"),
                "title": event.get("title"),
                "status": "pending",
            }
            for event in events
            if "todo" in event.get("categories", [])
        ],
        "warnings": summary.get("warnings", []),
        "events": events,
        "source_daily_summaries": [str(summary_path.resolve())],
    }
    agent_brief = external_agent_brief(raw_brief)
    delivery = deliver_daily_brief(
        agent_brief,
        outbox_dir=output_dir / "agent-outbox",
    )
    outbox_path = Path(str(delivery["outbox_path"]))
    outbound = json.loads(outbox_path.read_text(encoding="utf-8"))
    forbidden_keys = {
        "evidence",
        "source_daily_summaries",
        "source_file",
        "audio_path",
        "recording_id",
        "segment_id",
        "transcript",
        "quote",
        "raw_quote",
    }
    leaked_keys = sorted(_payload_keys(outbound) & forbidden_keys)
    if leaked_keys:
        raise RuntimeError(
            "demo Agent brief leaked private fields: " + ", ".join(leaked_keys)
        )
    serialized_outbound = json.dumps(outbound, ensure_ascii=False)
    if ".m4a" in serialized_outbound or "demo audio placeholder" in serialized_outbound:
        raise RuntimeError("demo Agent brief leaked raw audio references")
    if outbound.get("schema_version") != "agent-brief-v1":
        raise RuntimeError("demo Agent brief did not use the external contract")

    return {
        "ok": True,
        "mode": "demo",
        "fixture": str(fixture.relative_to(PROJECT_ROOT)),
        "event_count": result.event_count,
        "transcript_count": len(transcripts),
        "transcript_only_count": len(TRANSCRIPT_ONLY_TEXTS),
        "categories": sorted(categories),
        "network_allowed": False,
        "local_asr_required": False,
        "events_path": str(result.events_path),
        "transcript_path": str(transcript_path),
        "transcript_markdown": str(transcript_markdown),
        "daily_markdown": str(result.markdown_path),
        "agent_outbox_path": str(outbox_path),
        "agent_brief_schema": outbound.get("schema_version"),
        "agent_brief_point_count": len(outbound.get("high_confidence_points", [])),
        "agent_brief_audio_leak": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the bundled fixture Demo tier without config, model paths, "
            "ASR runtimes, or network access."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional directory to keep generated Demo artifacts",
    )
    args = parser.parse_args()

    if args.output is not None:
        payload = verify_demo(args.output.resolve())
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    with tempfile.TemporaryDirectory(prefix="life-logger-demo-") as directory:
        payload = verify_demo(Path(directory) / "output")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
