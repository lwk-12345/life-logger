"""Replay preserved day transcripts; publish derived outputs only on explicit apply."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from life_logger.aggregate import build_daily_summary
from life_logger.contextual_extraction import _source_id, run_contextual_extraction
from life_logger.render import _markdown
from life_logger.source_attribution import apply_multi_evidence_policy
from scripts.attribute_funasr_transcript import is_dictation_record, load_dictation_windows
from life_logger.domain import EventTime, LifeEvent
from typing import Any


def _event_from_dict(item: dict[str, Any]) -> LifeEvent:
    return LifeEvent(
        event_id=item["event_id"],
        categories=list(item["categories"]),
        activity_type=item.get("activity_type"),
        title=item["title"],
        time=EventTime(**item["time"]),
        data=dict(item.get("data") or {}),
        capture_mode=item.get("capture_mode", "passive"),
        source_type=item.get("source_type", "audio"),
        confidence=float(item.get("confidence", 0.0)),
        evidence=dict(item.get("evidence") or {}),
        review_status=item.get("review_status", "unreviewed"),
    )


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 唯一临时名 + fsync + replace（统一走 life_logger.atomic）。
    # 本脚本在模块顶部已 sys.path.insert(ROOT/"src")，依赖必然可用 → 直连。
    from life_logger.atomic import write_json_atomic

    write_json_atomic(path, value, indent=2)


def discover(day: str, jobs: Path) -> tuple[list[dict], dict[str, dict]]:
    windows = load_dictation_windows(jobs.parent / "dictation-windows.jsonl")
    rows: list[dict] = []
    origins: dict[str, dict] = {}
    for path in sorted(jobs.glob("*/files/*/result/source-attribution.json")):
        source = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(source, list):
            continue
        # Replays must use the same near-field/clarity fusion as new runs,
        # without rewriting the preserved attribution artifact.
        source = apply_multi_evidence_policy(source)
        namespace = str(path.relative_to(jobs))
        prefix = hashlib.sha256(namespace.encode()).hexdigest()[:16]
        for record in source:
            if not isinstance(record, dict) or not str(record.get("absolute_started_at", "")).startswith(day):
                continue
            item = dict(record)
            item["segment_id"] = prefix + "/" + str(record.get("segment_id", ""))
            item["recording_id"] = prefix + "/" + str(record.get("recording_id", ""))
            item["is_dictation"] = is_dictation_record(record, windows)
            source_id = _source_id(item, day)
            rows.append(item)
            origins[source_id] = {
                "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "segment_id": record.get("segment_id"), "recording_id": record.get("recording_id"),
                "source_label": record.get("source_label", "unknown"),
            }
    return rows, origins


def publish(day: str, report: dict, origins: dict[str, dict], output: Path, jobs: Path) -> None:
    if any(batch.get("status") == "failed" for batch in report.get("batches", [])):
        raise RuntimeError("Failed batches remain; derived outputs were not published")
    for origin in origins.values():
        path = Path(origin["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != origin["sha256"]:
            raise RuntimeError("Source transcript changed during replay")
    grouped: dict[Path, set[str]] = {}
    for source_id, origin in origins.items():
        grouped.setdefault(Path(origin["path"]).parent, set()).add(source_id)
    for result_dir in grouped:
        job_dir = result_dir.parents[2]
        # Keep user decisions intact rather than silently invalidating old event IDs.
        correction = job_dir / "event-corrections.json"
        if correction.exists() and json.loads(correction.read_text()).get("corrections"):
            raise RuntimeError("Existing user corrections require reconciliation before publication")
    for result_dir, ids in grouped.items():
        job_dir = result_dir.parents[2]
        target = result_dir / day
        for name in ["events.json", "daily_summary.json", "summary.md"]:
            original = target / name
            backup = output / "previous-derived" / original.relative_to(jobs)
            if original.exists() and not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, backup)
        formal = [event for event in report["formal_events"] if event["evidence"]["source_id"] in ids]
        pending = [event for event in report["pending_events"] if event["evidence"]["source_id"] in ids]
        for event in formal + pending:
            origin = origins[event["evidence"]["source_id"]]
            event["evidence"].update({
                "original_segment_id": origin["segment_id"],
                "original_recording_id": origin["recording_id"],
                "source_artifact": str(Path(origin["path"]).relative_to(job_dir)),
            })
        write_json(target / "events.json", formal)
        summary = build_daily_summary(
            [_event_from_dict(event) for event in formal], date=day, timezone_name="Asia/Shanghai",
            warnings=["Contextual replay; pending candidates excluded from formal events."],
        )
        write_json(target / "daily_summary.json", summary)
        (target / "summary.md").write_text(_markdown(summary), encoding="utf-8")
        scoped = {**report, "scope_date": day, "formal_events": formal, "pending_events": pending,
                  "pending_sources": [source for source in report["pending_sources"] if source["source_id"] in ids]}
        scoped["counts"] = {**report["counts"], "input_records": len(ids), "formal_events": len(formal),
                            "pending_events": len(pending), "pending_sources": len(scoped["pending_sources"])}
        write_json(result_dir / "context-extraction.json", scoped)
        state_path = job_dir / "status.json"
        if state_path.exists():
            backup = output / "previous-derived" / state_path.relative_to(jobs)
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(state_path, backup)
            state = json.loads(state_path.read_text())
            artifact = str((result_dir / "context-extraction.json").relative_to(job_dir))
            state["results"] = list(dict.fromkeys([*state.get("results", []), artifact]))
            write_json(state_path, state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reuse-report", action="store_true")
    args = parser.parse_args()
    date.fromisoformat(args.date)
    jobs = ROOT / "output/local-web/jobs"
    rows, origins = discover(args.date, jobs)
    if not rows:
        raise RuntimeError("No preserved transcripts for this date")
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "context-extraction.json"
    if args.reuse_report:
        report = json.loads(report_path.read_text())
        saved = json.loads((args.output / "source-origins.json").read_text())
        if saved != origins:
            raise RuntimeError("Source inventory changed since replay")
    else:
        if report_path.exists():
            raise RuntimeError("Replay report already exists; use another output directory or --reuse-report")
        write_json(args.output / "source-origins.json", origins)
        secrets = json.loads((jobs.parent / "secrets.json").read_text())
        if not secrets.get("deepseek_enabled") or not secrets.get("deepseek_api_key"):
            raise RuntimeError("DeepSeek is not configured and enabled")
        print(f"Analyzing {len(rows)} preserved segments for {args.date}", flush=True)
        report = run_contextual_extraction(rows, deepseek_api_key=secrets["deepseek_api_key"],
                                          source_namespace=args.date, timeout_seconds=60)
        write_json(report_path, report)
    if args.apply:
        publish(args.date, report, origins, args.output, jobs)
        print("Published validated derived events; original transcripts/audio preserved", flush=True)
    print(json.dumps({"status": report["status"], "counts": report["counts"], "kev": report["kev"]["status"]}, ensure_ascii=False))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
