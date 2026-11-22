#!/usr/bin/env python3
"""Run the AI Passport LAN receiver and import sessions into Life Logger."""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path

from life_logger.local_web import DEFAULT_JOB_ROOT, PROJECT_ROOT, JobStore
from life_logger.passport_stream import (
    PASSPORT_DISCOVERY_PORT,
    start_passport_receiver,
)


def _lan_bind(value: str) -> str:
    host = str(value or "").strip()
    if host == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "监听地址必须是 localhost、0.0.0.0 或局域网 IPv4"
        ) from error
    if address.version != 4:
        raise argparse.ArgumentTypeError("当前 AI Passport 接收器只支持 IPv4")
    if address.is_loopback or address.is_unspecified or address.is_private:
        return str(address)
    raise argparse.ArgumentTypeError(
        "拒绝公网监听地址；AI Passport 通道只用于同一局域网"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Receive AI Passport PCM chunks on the local LAN and import complete WAVs."
        )
    )
    parser.add_argument("--host", type=_lan_bind, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--discovery-port", type=int, default=PASSPORT_DISCOVERY_PORT)
    parser.add_argument("--job-root", type=Path, default=DEFAULT_JOB_ROOT)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "local-config.json",
    )
    parser.add_argument(
        "--no-auto-start",
        action="store_true",
        help="Import complete sessions as draft jobs without starting ASR.",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if not 1 <= args.discovery_port <= 65535:
        parser.error("discovery-port must be between 1 and 65535")

    store = JobStore(args.job_root, recover=False)
    stream_root = store.root / "passport-stream"

    def import_complete(session_dir: Path, summary: dict[str, object]) -> str | None:
        result = store.import_passport_recording(
            session_dir,
            summary,
            args.config,
            auto_start=not args.no_auto_start,
        )
        return str(result["job_id"])

    try:
        runtime = start_passport_receiver(
            host=args.host,
            port=args.port,
            discovery_port=args.discovery_port,
            output_root=stream_root,
            on_complete=import_complete,
        )
    except OSError as error:
        parser.error(f"could not start Passport receiver: {error}")
    print(f"AI Passport receiver: http://{args.host}:{runtime.server.server_port}")
    print(f"AI Passport discovery: UDP {args.host}:{runtime.discovery_port}")
    print(
        "Passport endpoint: "
        f"http://<THIS-MAC-LAN-IP>:{runtime.server.server_port}/v1/audio/chunk"
    )
    print(f"Raw sessions: {stream_root}")
    print(
        "Complete sessions are imported into the normal Life Logger job pipeline."
        if not args.no_auto_start
        else (
            "Complete sessions are imported as draft Life Logger jobs "
            "(--no-auto-start)."
        )
    )
    try:
        runtime.http_thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
