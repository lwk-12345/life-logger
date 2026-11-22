"""Re-apply source-attribution policy to stored CAM++ scores without audio inference."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from life_logger.source_attribution import (  # noqa: E402
    AttributionConfig,
    partition_attributions,
    reclassify_attribution_record,
)


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def reclassify_file(
    source: Path,
    output_dir: Path,
    *,
    self_threshold: float,
    unknown_threshold: float,
    short_review_seconds: float,
    short_review_score_floor: float,
) -> dict[str, int]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("source-attribution JSON must contain a list")
    config = AttributionConfig(
        self_threshold=self_threshold,
        unknown_threshold=unknown_threshold,
    )
    records = [
        reclassify_attribution_record(
            item,
            config,
            short_review_seconds=short_review_seconds,
            short_review_score_floor=short_review_score_floor,
        )
        for item in payload
    ]
    partitions = partition_attributions(records)
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(output_dir / "source-attribution.json", records)
    _atomic_write_json(output_dir / "self-transcript.json", partitions["self"])
    _atomic_write_json(output_dir / "non-self-transcript.json", partitions["non_self"])
    _atomic_write_json(output_dir / "unknown-review.json", partitions["unknown"])
    return {label: len(items) for label, items in partitions.items()}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reclassify-source-attribution")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--self-threshold", type=float, default=0.45)
    parser.add_argument("--unknown-threshold", type=float, default=0.25)
    parser.add_argument("--short-review-seconds", type=float, default=4.0)
    parser.add_argument("--short-review-score-floor", type=float, default=0.10)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        counts = reclassify_file(
            args.source.resolve(),
            args.output_dir.resolve(),
            self_threshold=args.self_threshold,
            unknown_threshold=args.unknown_threshold,
            short_review_seconds=args.short_review_seconds,
            short_review_score_floor=args.short_review_score_floor,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "completed", "counts": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
