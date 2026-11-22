"""Read historical decision artifacts without clients, configuration or writes."""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from life_logger.timeutil import LOCAL_TZ, local_date


def read_kev_section(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read legacy extraction status, including the old Laya field."""
    if not isinstance(payload, Mapping):
        return {}
    section = payload.get("kev")
    if not isinstance(section, Mapping):
        section = payload.get("laya")
    return dict(section) if isinstance(section, Mapping) else {}


def read_kev_annotation(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read historical annotations, without generating or applying them."""
    if not isinstance(payload, Mapping):
        return {}
    annotation = payload.get("kev_annotation")
    if not isinstance(annotation, Mapping):
        annotation = payload.get("laya_annotation")
    return dict(annotation) if isinstance(annotation, Mapping) else {}


def collect_comparisons(web_root: Path, date: str = "", limit: int = 10) -> dict:
    paths = list(web_root.glob("jobs/*/files/*/result/decision-comparison.json"))
    paths += list(
        web_root.glob(
            "watch-sessions/*/*/formal-asr/live-context/decision-comparison.json"
        )
    )
    items = []
    for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if date and date not in path.as_posix():
            mtime_day = local_date(datetime.fromtimestamp(path.stat().st_mtime, tz=LOCAL_TZ))
            if mtime_day != date:
                continue
        variants = {
            provider: {
                key: result.get(key)
                for key in (
                    "status",
                    "counts",
                    "formal_events",
                    "pending_events",
                    "pending_sources",
                    "decision_elapsed_seconds",
                    "extraction_elapsed_seconds",
                )
            }
            for provider, result in report.get("variants", {}).items()
        }
        items.append(
            {
                "artifact": path.relative_to(web_root).as_posix(),
                "recorded_at": report.get("recorded_at"),
                "status": report.get("status"),
                "variants": variants,
            }
        )
        if len(items) >= limit:
            break
    return {
        "mode": "decision-history", "enabled": False, "read_only": True,
        "status": "disabled", "label": "已停用", "items": items,
    }
