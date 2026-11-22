"""Command-line entry points for Life Logger."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from life_logger.daily_brief import (
    build_daily_brief,
    default_outbox_dir,
    deliver_daily_brief,
    external_agent_brief,
    outbound_options,
)
from life_logger.doctor import DEFAULT_HEALTH_URL, render_doctor_report, run_doctor
from life_logger.local_pipeline import load_local_config, run_local_product_pipeline
from life_logger.patterns import build_pattern_observations
from life_logger.pipeline import run_demo, run_process


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="life-logger")
    subparsers = parser.add_subparsers(dest="command")

    demo = subparsers.add_parser("demo", help="run the bundled transcript fixture")
    demo.add_argument("--output", type=Path, default=Path("output"))
    demo.add_argument("--timezone", default="Asia/Shanghai")

    process = subparsers.add_parser(
        "process",
        help="process a file or directory of audio",
    )
    process.add_argument("input", type=Path)
    process.add_argument("--output", type=Path, default=Path("output"))
    process.add_argument("--timezone", default="Asia/Shanghai")
    process.add_argument(
        "--engine",
        choices=("product", "legacy"),
        default="product",
        help="product uses the same Fun-ASR/CAM++ chain as the local web UI",
    )
    process.add_argument(
        "--config",
        type=Path,
        default=Path("local-config.json"),
        help="local model/runtime configuration for the product engine",
    )
    process.add_argument(
        "--transcriber",
        choices=("local", "auto", "demo"),
        default="local",
        help="legacy engine only",
    )
    process.add_argument("--start-at", type=datetime.fromisoformat)
    process.add_argument("--chunk-seconds", type=float, default=300.0)
    process.add_argument("--cpu-threads", type=int, default=2)
    brief = subparsers.add_parser(
        "brief",
        help="generate the external Agent brief and write the local outbox",
    )
    brief.add_argument("--date")
    brief.add_argument("--timezone", default="Asia/Shanghai")
    brief.add_argument("--job-root", type=Path, default=Path("output/local-web"))
    brief.add_argument("--config", type=Path, default=Path("local-config.json"))
    brief.add_argument("--outbox", type=Path)
    brief.add_argument("--webhook")
    patterns = subparsers.add_parser(
        "patterns",
        help="build evidence-backed multi-day pattern observations (not diagnoses)",
    )
    patterns.add_argument("--from", dest="date_from", required=True)
    patterns.add_argument("--to", dest="date_to", required=True)
    patterns.add_argument("--job-root", type=Path, default=Path("output/local-web"))
    doctor = subparsers.add_parser(
        "doctor",
        help="check local configuration, worker runtime, service, and recent jobs",
    )
    doctor.add_argument("--config", type=Path, default=Path("local-config.json"))
    doctor.add_argument("--job-root", type=Path, default=Path("output/local-web"))
    doctor.add_argument("--web-url", default=DEFAULT_HEALTH_URL)
    doctor.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _show_transcription_progress(current: int, total: int) -> None:
    if total > 1:
        print(f"transcribing chunks: {current}/{total}", flush=True)


def _run_product_command(
    input_path: Path,
    output_dir: Path,
    *,
    config_path: Path,
    timezone_name: str,
    user_start: datetime | None,
    cpu_threads: int,
) -> int:
    config = dict(load_local_config(config_path))
    config["cpu_threads"] = cpu_threads
    results = run_local_product_pipeline(
        input_path,
        output_dir,
        config=config,
        timezone_name=timezone_name,
        user_start=user_start,
    )
    print(f"processed files: {len(results)}")
    for result in results:
        print(f"source: {result.source}")
        print(f"result dir: {result.result_dir}")
        for artifact in result.artifacts:
            print(f"artifact: {artifact}")
    return 0


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command is None:
            return _run_product_command(
                Path("input"),
                Path("output"),
                config_path=Path("local-config.json"),
                timezone_name="Asia/Shanghai",
                user_start=None,
                cpu_threads=2,
            )
        elif args.command == "brief":
            target_date = args.date or datetime.now(
                tz=ZoneInfo(args.timezone)
            ).date().isoformat()
            config: dict[str, object] = {}
            if args.config.is_file():
                loaded = json.loads(args.config.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    config = loaded
            configured_outbox, configured_webhook = outbound_options(config)
            brief = external_agent_brief(
                build_daily_brief(args.job_root, date=target_date)
            )
            delivery = deliver_daily_brief(
                brief,
                outbox_dir=(
                    args.outbox
                    or configured_outbox
                    or default_outbox_dir(args.job_root)
                ),
                webhook_url=args.webhook or configured_webhook,
            )
            print(
                json.dumps(
                    {"brief": brief, "delivery": delivery},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            webhook = delivery.get("webhook")
            return 1 if isinstance(webhook, dict) and not webhook.get("ok") else 0
        elif args.command == "patterns":
            payload = build_pattern_observations(
                args.job_root,
                date_from=args.date_from,
                date_to=args.date_to,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "doctor":
            report = run_doctor(
                args.config,
                job_root=args.job_root,
                web_url=args.web_url,
            )
            if args.as_json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(render_doctor_report(report))
            return 0 if report["ok"] else 1
        elif args.command == "demo":
            result = run_demo(args.output, timezone_name=args.timezone)
        elif args.engine == "product":
            return _run_product_command(
                args.input,
                args.output,
                config_path=args.config,
                timezone_name=args.timezone,
                user_start=args.start_at,
                cpu_threads=args.cpu_threads,
            )
        else:
            result = run_process(
                args.input,
                args.output,
                timezone_name=args.timezone,
                transcriber_name=args.transcriber,
                user_start=args.start_at,
                chunk_seconds=args.chunk_seconds,
                cpu_threads=args.cpu_threads,
                progress_callback=_show_transcription_progress,
            )
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}")
        return 2

    print(f"events: {result.events_path}")
    print(f"daily summary: {result.markdown_path}")
    print(f"database: {result.database_path}")
    print(f"event count: {result.event_count}")
    if result.warnings:
        print(f"warnings: {len(result.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
