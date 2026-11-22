"""Persist canonical and derived Life Logger artifacts locally."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from life_logger.domain import LifeEvent, RecordingSegment, TranscriptSegment

_CATEGORY_NAMES = {
    "routine": "作息",
    "food": "饮食",
    "expense": "消费",
    "exercise": "运动",
    "work_communication": "工作/沟通",
    "idea": "想法",
    "todo": "待办",
    "other": "其他",
}


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _markdown(summary: dict[str, Any]) -> str:
    date_label = summary.get("date") or "未校准日期"
    lines = [
        f"# Life Logger — {date_label}",
        "",
        f"- 时区：{summary['timezone']}",
        f"- 事件数：{summary['event_count']}",
        f"- 消费合计：¥{summary['expense_total_cny']:.2f}",
        "",
        "## 时间轴",
        "",
    ]
    for item in summary["timeline"]:
        event_time = item.get("time") or {}
        rel = event_time.get("relative_seconds")
        if event_time.get("absolute"):
            timestamp = event_time["absolute"]
        elif rel is not None:
            timestamp = f"+{float(rel):.1f}s"
        else:
            timestamp = "时间待定"
        categories = "、".join(
            _CATEGORY_NAMES.get(category, category) for category in item["categories"]
        )
        lines.append(f"- `{timestamp}` **{item['title']}** · {categories}")
        lines.append(f"  - 证据：{item['evidence'].get('quote', '')}")
    lines.extend(["", "## 分类摘要", ""])
    for category, item in summary["category_summary"].items():
        highlights = "、".join(item["highlights"])
        name = _CATEGORY_NAMES.get(category, category)
        lines.append(f"- **{name}**：{item['count']} 条；{highlights}")
    if summary["warnings"]:
        lines.extend(["", "## 警告", ""])
        lines.extend(f"- {warning}" for warning in summary["warnings"])
    return "\n".join(lines) + "\n"


def _transcript_markdown(transcripts: list[TranscriptSegment]) -> str:
    lines = ["# Life Logger 全文转写", ""]
    for item in transcripts:
        timestamp = item.absolute_started_at or f"+{item.start_offset_seconds:.1f}s"
        lines.append(f"- `{timestamp}` {item.text}")
    return "\n".join(lines) + "\n"


def _write_sqlite(
    path: Path,
    recordings: list[RecordingSegment],
    transcripts: list[TranscriptSegment],
    events: list[LifeEvent],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            create table if not exists recordings (
                recording_id text primary key,
                source_file text not null,
                payload_json text not null
            );
            create table if not exists transcript_segments (
                segment_id text primary key,
                recording_id text not null,
                payload_json text not null
            );
            create table if not exists events (
                event_id text primary key,
                payload_json text not null
            );
            """
        )
        connection.executemany(
            "insert or replace into recordings values (?, ?, ?)",
            [
                (
                    item.recording_id,
                    item.source_file,
                    json.dumps(item.to_dict(), ensure_ascii=False),
                )
                for item in recordings
            ],
        )
        connection.executemany(
            "insert or replace into transcript_segments values (?, ?, ?)",
            [
                (
                    item.segment_id,
                    item.recording_id,
                    json.dumps(item.to_dict(), ensure_ascii=False),
                )
                for item in transcripts
            ],
        )
        connection.executemany(
            "insert or replace into events values (?, ?)",
            [
                (item.event_id, json.dumps(item.to_dict(), ensure_ascii=False))
                for item in events
            ],
        )


def write_daily_outputs(
    output_dir: Path,
    *,
    date: str | None,
    recordings: list[RecordingSegment],
    transcripts: list[TranscriptSegment],
    events: list[LifeEvent],
    summary: dict[str, Any],
) -> dict[str, Path]:
    """Write all local artifacts and return their paths."""
    day_dir = output_dir / (date or "undated")
    day_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "recordings": day_dir / "recordings.json",
        "transcript": day_dir / "transcript.json",
        "transcript_markdown": day_dir / "transcript.md",
        "events": day_dir / "events.json",
        "summary": day_dir / "daily_summary.json",
        "markdown": day_dir / "daily.md",
        "database": output_dir / "life_logger.db",
    }
    _atomic_text(
        paths["recordings"],
        json.dumps(
            [item.to_dict() for item in recordings],
            ensure_ascii=False,
            indent=2,
        ),
    )
    _atomic_text(
        paths["transcript"],
        json.dumps(
            [item.to_dict() for item in transcripts],
            ensure_ascii=False,
            indent=2,
        ),
    )
    _atomic_text(paths["transcript_markdown"], _transcript_markdown(transcripts))
    _atomic_text(
        paths["events"],
        json.dumps([item.to_dict() for item in events], ensure_ascii=False, indent=2),
    )
    _atomic_text(
        paths["summary"],
        json.dumps(summary, ensure_ascii=False, indent=2),
    )
    _atomic_text(paths["markdown"], _markdown(summary))
    _write_sqlite(paths["database"], recordings, transcripts, events)
    return paths
