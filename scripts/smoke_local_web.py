#!/usr/bin/env python3
"""Smoke-test the running Life Logger local Web service without mutating data."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRETS = ROOT / "output" / "local-web" / "secrets.json"
SHANGHAI = ZoneInfo("Asia/Shanghai")

PRIMARY_DOM_IDS = {
    "sidebar-service-status",
    "sidebar-status-dot",
    "search-form",
    "search-summary",
    "search-results",
    "stats-surface",
    "stats-status",
    "stats-empty",
    "patterns-surface",
    "patterns-status",
    "patterns-results",
    "passport-stream-sessions",
    "passport-receiver-summary",
    "passport-receiver-health",
}


class _IdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag
        node_id = dict(attrs).get("id")
        if node_id:
            self.ids.add(node_id)


def _load_local_token(path: Path) -> str:
    explicit = os.environ.get("LIFE_LOGGER_ACCESS_TOKEN", "").strip()
    if explicit:
        return explicit
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(
        payload.get("mobile_access_token")
        or payload.get("access_token")
        or ""
    ).strip()


def _get(
    base_url: str,
    path: str,
    *,
    token: str = "",
    timeout: float = 5.0,
) -> tuple[int, str, bytes]:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return (
            int(response.status),
            response.headers.get("Content-Type", ""),
            response.read(),
        )


def _json_get(
    base_url: str,
    path: str,
    *,
    token: str,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    status, _, body = _get(
        base_url,
        path,
        token=token,
        timeout=timeout,
    )
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not return a JSON object")
    return status, payload


def run_smoke(
    *,
    base_url: str,
    token: str,
    timeout: float,
) -> dict[str, Any]:
    root_status, content_type, root_body = _get(
        base_url,
        "/",
        timeout=timeout,
    )
    parser = _IdParser()
    parser.feed(root_body.decode("utf-8"))
    missing_dom_ids = sorted(PRIMARY_DOM_IDS - parser.ids)

    today = datetime.now(tz=SHANGHAI).date()
    start = today - timedelta(days=29)
    date_params = urllib.parse.urlencode(
        {"from": start.isoformat(), "to": today.isoformat()}
    )

    search_status, search_payload = _json_get(
        base_url,
        "/api/search?"
        + urllib.parse.urlencode(
            {"q": "Life Logger", "type": "all", "limit": "3"}
        ),
        token=token,
        timeout=timeout,
    )
    stats_status, stats_payload = _json_get(
        base_url,
        f"/api/stats?{date_params}",
        token=token,
        timeout=timeout,
    )
    patterns_status, patterns_payload = _json_get(
        base_url,
        f"/api/patterns?{date_params}",
        token=token,
        timeout=timeout,
    )
    passport_status, passport_payload = _json_get(
        base_url,
        "/api/passport-sessions",
        token=token,
        timeout=timeout,
    )

    receiver = passport_payload.get("receiver")
    if not isinstance(receiver, dict):
        receiver = {}

    patterns = patterns_payload.get("patterns")
    if patterns is None:
        patterns = patterns_payload.get("observations")
    if not isinstance(patterns, list):
        patterns = []

    result = {
        "ok": True,
        "base_url": base_url,
        "static": {
            "status": root_status,
            "content_type": content_type,
            "missing_dom_ids": missing_dom_ids,
        },
        "search": {
            "status": search_status,
            "result_count": len(search_payload.get("results") or []),
        },
        "stats": {
            "status": stats_status,
            "days_with_data": stats_payload.get("days_with_data"),
        },
        "patterns": {
            "status": patterns_status,
            "count": len(patterns),
        },
        "passport": {
            "status": passport_status,
            "session_count": len(passport_payload.get("sessions") or []),
            "receiver": {
                key: receiver.get(key)
                for key in (
                    "running",
                    "http_running",
                    "discovery_running",
                    "degraded",
                    "http_port",
                    "discovery_port",
                )
            },
        },
    }

    statuses = (
        root_status,
        search_status,
        stats_status,
        patterns_status,
        passport_status,
    )
    if any(status != 200 for status in statuses) or missing_dom_ids:
        result["ok"] = False
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test the running Life Logger local Web service."
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8765",
        help="Life Logger Web origin (default: %(default)s)",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Bearer token. Defaults to env/local secrets; never printed.",
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        default=DEFAULT_SECRETS,
        help="Local secrets.json path used when --token is omitted.",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    token = args.token.strip() or _load_local_token(args.secrets)
    try:
        result = run_smoke(
            base_url=args.base_url,
            token=token,
            timeout=args.timeout,
        )
    except Exception as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "base_url": args.base_url,
                    "error": f"{type(error).__name__}: {error}",
                    "token_present": bool(token),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    result["token_present"] = bool(token)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
