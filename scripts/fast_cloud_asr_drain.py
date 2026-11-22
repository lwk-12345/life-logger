"""Fast parallel cloud ASR drain for Watch sessions.

Processes uploaded Watch audio segments concurrently using Doubao Cloud ASR
without blocking on local hardware limits or slow single-thread loops.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from life_logger.watch_formal_asr import WatchFormalASRProcessor
from life_logger.watch_stream import WatchSessionStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fast_asr_drain")


def drain_session(
    device_id: str,
    session_id: str,
    *,
    concurrency: int = 4,
    watch_continuous: bool = True,
) -> None:
    store = WatchSessionStore(REPO_ROOT / "output" / "local-web" / "watch-sessions")
    stop_event = threading.Event()
    config_path = REPO_ROOT / "local-config.json"

    processor = WatchFormalASRProcessor(
        store,
        config_path,
        stop_event,
        slot_acquirer=lambda owner: {"token": "cloud-batch", "owner": owner},
        slot_releaser=lambda slot: None,
    )

    logger.info(
        f"Starting fast cloud ASR drain for session {session_id} (concurrency={concurrency})"
    )

    while not stop_event.is_set():
        try:
            manifest = store.load_session(device_id, session_id)
        except Exception as err:
            logger.warning(f"Failed to load session manifest: {err}")
            time.sleep(2)
            continue

        session_state = manifest.get("state", "receiving")
        segments = sorted(
            manifest.get("segments", []),
            key=lambda item: int(item.get("sequence", -1)),
        )

        pending_segments = []
        completed_count = 0
        for seg in segments:
            seq = int(seg.get("sequence", -1))
            state_file = (
                store.session_directory(device_id, session_id)
                / "formal-asr"
                / "segments"
                / f"{seq:06d}"
                / "state.json"
            )
            if state_file.is_file():
                try:
                    state_data = json.loads(state_file.read_text(encoding="utf-8"))
                    if state_data.get("status") in {"completed", "no_speech"}:
                        completed_count += 1
                        continue
                except Exception:
                    pass
            pending_segments.append(seg)

        logger.info(
            f"Progress: {completed_count}/{len(segments)} segments done. "
            f"Pending: {len(pending_segments)} (session state: {session_state})"
        )

        if not pending_segments:
            if session_state == "complete":
                logger.info("All segments finished and session is complete! Exiting.")
                break
            if not watch_continuous:
                break
            time.sleep(3)
            continue

        def process_one(seg: dict) -> tuple[int, bool]:
            seq = int(seg.get("sequence", -1))
            try:
                ok = processor._process_segment(device_id, session_id, manifest, seg)
                return seq, ok
            except Exception as e:
                logger.error(f"Error processing segment {seq}: {e}")
                return seq, False

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(process_one, seg) for seg in pending_segments]
            for future in concurrent.futures.as_completed(futures):
                seq, ok = future.result()
                if ok:
                    logger.info(f"Segment {seq:06d} processed successfully.")

        if not watch_continuous and session_state != "receiving":
            break
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fast Watch Cloud ASR Drain")
    parser.add_argument(
        "--device-id",
        type=str,
        default="watch-41aa6b87347f4f0fb5238b559f705c4d",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default="9A584561C69F4AD493492B756F5DBE27",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    drain_session(
        args.device_id,
        args.session_id,
        concurrency=args.concurrency,
        watch_continuous=not args.once,
    )


if __name__ == "__main__":
    main()
