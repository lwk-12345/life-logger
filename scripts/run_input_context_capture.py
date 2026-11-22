"""Explicit, bounded metadata capture with atomic live snapshots; no daemon install."""
from __future__ import annotations

import copy
import signal
import time
from pathlib import Path

import mac_input_context as mic


def snapshot(collector, *, ended_at=None, reason="capturing"):
    # finalize closes an open window: apply it only to a detached state copy.
    view = copy.copy(collector)
    view.windows = list(collector.windows)
    view.open_window = copy.deepcopy(collector.open_window)
    payload = view.finalize(stop_reason=reason, ended_at=ended_at)
    payload["capture_state"] = "running" if reason == "capturing" else "stopped"
    return payload


def publish(collector, output: Path, *, reason="capturing"):
    return mic.write_atomic(output, snapshot(collector, reason=reason))


def main(argv=None):
    parser = mic._build_parser()
    parser.description = __doc__
    args = parser.parse_args(argv)
    if args.output is None:
        parser.error("--output is required; production location is never selected implicitly")
    interval = max(0.1, min(5.0, args.interval))
    front = mic.FixtureFrontSource(args.front_fixture) if args.front_fixture else mic.LiveFrontSource()
    source = mic._FixtureMicSource(mic.load_mic_fixture(args.mic_fixture)) if args.mic_fixture else mic.LiveMicSource()
    collector = mic.InputContextCollector(provider_pid=args.provider_pid,
        overlay_bundles=args.overlay_bundle, front_source=front, mic_source=source)
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    previous = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM)}
    deadline = time.monotonic() + args.duration
    reason = "duration"
    try:
        while time.monotonic() < deadline:
            if stopped:
                reason = "signal"
                break
            if args.stop_file and args.stop_file.exists():
                reason = "stop_file"
                break
            collector.sample_once()
            publish(collector, args.output)
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    finally:
        try:
            publish(collector, args.output, reason=reason if not stopped else "signal")
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            close = getattr(source, "close", None)
            if close:
                close()
    print(f"input_context_output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
