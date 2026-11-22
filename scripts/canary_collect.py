#!/usr/bin/env python3
"""Collect scoped business canaries and schema-only operational observations.

No retries. No global timestamp regex. No source/API mutation.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import zipfile

FORMAT = "life-logger-canary-v1"
ROOT = Path(__file__).resolve().parents[1]
VOLATILE_SHA256 = "ac746b1d80e54e9201d51c43ec39d5fc62b6042cfd048f218b8c9a78009ed06e"


def rule(path, category, fields=(), strip=(), nested=None, maps=()):
    return {"method": "GET", "path": path, "class": category,
            "mode": "facts" if fields else "schema", "fields": list(fields),
            "strip": list(strip), "nested": nested or {}, "dynamic_maps": list(maps)}


# Only five A routes with business facts. Four fixed-input B projections retain
# their business values; operational fields are never promoted to facts.
PROFILE = [
    rule("/api/timeline-scenes?date=2026-10-21", "A", ("ok", "date", "scenes", "action_groups", "read_contract")),
    rule("/api/quick-captures?date=2026-10-21", "A", ("ok", "captures")),
    rule("/api/watch-live-events?date=2026-10-21", "A", ("ok", "sessions", "read_contract")),
    rule("/api/nutrition/library", "A", ("ok", "count", "seeded", "entries")),
    rule("/api/stats?from=2026-10-21&to=2026-10-22", "A", (
        "schema_version", "date_from", "date_to", "scope", "view", "accounting_scope", "read_contract",
        "event_count", "category_counts", "exercise_count", "routine_distribution", "hourly_distribution",
        "daily_event_counts", "daily_expenses", "expense_total_cny", "expense_currency_totals",
        "expense_currency_counts", "expense_transaction_count", "expense_unknown_amount_count")),
    rule("/api/daily-brief?date=2026-10-21", "B", (
        "schema_version", "date", "timezone", "event_count", "category_summary", "todos", "warnings",
        "high_confidence_points", "projection_event_count", "expense_total", "expense_total_cny", "read_contract"),
         ("$.generated_at",)),
    rule("/api/daily?date=2026-10-21", "B", ("date", "read_contract", "events", "expense_ledger"),
         ("$.daily_summary.generated_at",)),
    rule("/api/capture-funnel?date=2026-10-21", "B", ("ok", "capture_funnel"),
         ("$.capture_funnel.computed_at",),
         {"capture_funnel": ["date", "said_sentences", "said_chars", "said_segments"]}),
    rule("/api/expense-range?from=2026-10-21&to=2026-10-22", "B", (
        "ok", "from", "to", "scope", "view", "total_cny", "by_day", "missing_days", "days", "source"),
         ("$.generated_at",)),
    rule("/api/dashboard/daily?date=2026-10-21", "B", strip=("$.dashboard.updated_at",)),
    rule("/api/doctor", "B", strip=("$.generated_at",)),
    rule("/api/data-health", "B", strip=("$.checked_at",)),
    rule("/api/auto-ingest", "B", strip=("$.last_scan_at", "$.last_scan_started_at")),
    rule("/api/watch-reception", "B", strip=("$.sessions[*].activity_age_seconds",)),
    rule("/api/passport-sessions", "B", strip=("$.sessions[*].activity_age_seconds",)),
    rule("/api/reminders", "B", strip=("$.reminders[*].updated_at",)),
    rule("/api/pending-sources", "B", strip=("$.generated_at",)),
    rule("/api/daily-health?date=2026-10-21", "B", strip=("$.generated_at",)),
    rule("/api/debug/read-cache", "C", maps=("$.threads",)),
    rule("/api/passport/cards", "D"),
    rule("/api/access-log", "D"),
    rule("/api/backup", "D"),
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def profile_hash():
    return hashlib.sha256(canonical({"format": FORMAT, "rules": PROFILE,
                                    "schema": "typed-path-union-v1"}).encode()).hexdigest()


def validate_profile(profile):
    seen = set()
    for item in profile:
        key = (item["method"], item["path"])
        if key in seen:
            raise ValueError(f"duplicate request key: {key}")
        if item["method"] != "GET" or not item["path"].startswith("/api/"):
            raise ValueError(f"invalid read-only request: {key}")
        seen.add(key)


def kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise ValueError("non-JSON value")


def strip_paths(value, paths):
    """Validate approved clock values before removal; never remove by key name."""
    result = deepcopy(value)
    def remove(node, tokens, full):
        token = tokens[0]
        if token.endswith("[*]"):
            key = token[:-3]
            if not isinstance(node, dict) or not isinstance(node.get(key), list):
                raise ValueError(f"expected array for {full}")
            for item in node[key]:
                remove(item, tokens[1:], full)
            return
        if not isinstance(node, dict) or token not in node:
            raise ValueError(f"missing clock path {full}")
        if len(tokens) > 1:
            remove(node[token], tokens[1:], full)
            return
        clock = node[token]
        if clock is not None:
            if isinstance(clock, bool):
                raise ValueError(f"invalid clock type: {full}")
            if isinstance(clock, (int, float)):
                if not math.isfinite(clock):
                    raise ValueError(f"invalid clock number: {full}")
            elif isinstance(clock, str):
                try:
                    datetime.fromisoformat(clock.replace("Z", "+00:00"))
                except ValueError as error:
                    raise ValueError(f"invalid clock text: {full}") from error
            else:
                raise ValueError(f"invalid clock type: {full}")
        del node[token]
    for path in paths:
        if not path.startswith("$."):
            raise ValueError("unsupported clock path")
        remove(result, path[2:].split("."), path)
    return result


def select_fields(value, fields):
    if not isinstance(value, dict):
        raise ValueError("expected object for facts")
    missing = set(fields) - value.keys()
    if missing:
        raise ValueError("missing fact fields: " + ", ".join(sorted(missing)))
    return {key: deepcopy(value[key]) for key in fields}


def schema_keys(value, dynamic_maps=()):
    """Typed key paths; array positions and explicitly dynamic map keys use *.

    Empty arrays contribute their container type, not invented item fields.
    """
    result = {}
    def visit(node, path):
        result.setdefault(path, set()).add(kind(node))
        if isinstance(node, dict):
            for key, child in node.items():
                visit(child, path + ("{*}" if path in dynamic_maps else "." + key))
        elif isinstance(node, list):
            for child in node:
                visit(child, path + "[*]")
    visit(value, "$")
    return {path: sorted(types) for path, types in sorted(result.items())}


def project(body, item):
    if item["path"] == "/api/backup":
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            info = archive.getinfo("manifest.json")
            if info.file_size > 2 * 1024 * 1024:
                raise ValueError("backup manifest too large")
            manifest = json.loads(archive.read(info))
            if not isinstance(manifest, dict):
                raise ValueError("backup manifest must be an object")
            return {"schema": {"$": ["zip"], **{
                "$.manifest." + key: [kind(value)] for key, value in sorted(manifest.items())}}}
    data = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON constant")))
    if not isinstance(data, dict):
        raise ValueError("API response must be a JSON object")
    normalized = strip_paths(data, item["strip"])
    if item["mode"] == "facts":
        if data.get("ok") is False:
            raise ValueError("business API returned ok=false")
        normalized = select_fields(normalized, item["fields"])
        for key, fields in item["nested"].items():
            normalized[key] = select_fields(normalized[key], fields)
        return {"schema": schema_keys(normalized), "facts": normalized}
    return {"schema": schema_keys(normalized, item["dynamic_maps"])}


class CanaryEnvironmentError(ValueError):
    """Transport failure; keeps the previous fetch ValueError compatibility."""


def fetch(base_url, token, path, timeout):
    # Token is passed via stdin, never in command arguments, logs or golden files.
    config = "header = " + json.dumps("Authorization: Bearer " + token) + "\n"
    try:
        process = subprocess.run([
            "curl", "-sS", "-m", str(timeout), "--connect-timeout", "3", "--retry", "0",
            "--max-filesize", "8388608", "--config", "-", "-w", "\n__CANARY__%{http_code}|%{content_type}",
            base_url.rstrip("/") + path,
        ], input=config.encode(), capture_output=True, timeout=timeout + 2)
    except (subprocess.TimeoutExpired, OSError) as error:
        raise CanaryEnvironmentError(f"request transport unavailable: {type(error).__name__}") from error
    body, marker, trailer = process.stdout.rpartition(b"\n__CANARY__")
    if process.returncode in {5, 6, 7, 18, 28, 35, 52, 55, 56, 60, 92}:
        raise CanaryEnvironmentError(f"curl transport failed: exit={process.returncode}")
    if process.returncode or not marker:
        raise ValueError(f"curl failed: exit={process.returncode}")
    status, content_type = trailer.decode().split("|", 1)
    return int(status), content_type.split(";", 1)[0], body


def validate_history(rows):
    facts = {row["path"]: row["snapshot"].get("facts") for row in rows}
    day = facts["/api/daily?date=2026-10-21"]
    brief = facts["/api/daily-brief?date=2026-10-21"]
    stats = facts["/api/stats?from=2026-10-21&to=2026-10-22"]
    ledger = facts["/api/expense-range?from=2026-10-21&to=2026-10-22"]
    if len(day["events"]) != 4 or day["expense_ledger"]["total_amount"] != 26.8:
        raise ValueError("approved 2026-10-21 business baseline is not 4 events / CNY 26.8")
    if brief["event_count"] != 4 or brief["expense_total_cny"] != 26.8:
        raise ValueError("daily brief disagrees with approved facts")
    if stats["expense_total_cny"] != ledger["total_cny"]:
        raise ValueError("stats and expense-range disagree")
    stat_day = next(row for row in stats["daily_expenses"] if row["date"] == "2026-10-21")
    ledger_day = next(row for row in ledger["by_day"] if row["date"] == "2026-10-21")
    if stat_day["amount"] != 26.8 or ledger_day["total_cny"] != 26.8 or ledger_day["event_count"] != 4:
        raise ValueError("range projections disagree with approved daily ledger")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "golden/canary-20261023"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--token-file", default=str(ROOT / "output/local-web/secrets.json"))
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--max-seconds", type=float, default=180, help="Hard collection budget; each HTTP call is also bounded")
    parser.add_argument("--after", help="Previous complete manifest; wait at least min-gap after its last response")
    parser.add_argument("--min-gap", type=float, default=60)
    args = parser.parse_args(argv)
    try:
        validate_profile(PROFILE)
        url = urlsplit(args.base_url)
        if url.scheme not in {"http", "https"} or url.hostname not in {"localhost", "127.0.0.1", "::1"} or url.username or url.query:
            raise ValueError("base URL must be a loopback HTTP origin")
        if not 0 < args.timeout <= 30 or not 0 <= args.min_gap <= 60 or args.max_seconds <= 0:
            raise ValueError("timeout must be in (0,30], min-gap in [0,60], collection budget positive")
        output = Path(args.out).resolve()
        if (output / "manifest.json").exists():
            raise ValueError("refusing to overwrite an existing snapshot")
        token = str(json.loads(Path(args.token_file).read_text())["mobile_access_token"])
        if not token or "\n" in token or "\r" in token:
            raise ValueError("invalid local access token")
        if args.after:
            previous = json.loads(Path(args.after).read_text())
            if not previous.get("complete") or previous.get("profile_sha256") != profile_hash():
                raise ValueError("--after requires a complete snapshot with the same profile")
            if not isinstance(previous.get("finished_epoch"), (int, float)) or not math.isfinite(previous["finished_epoch"]) or previous["finished_epoch"] > time.time() + 5:
                raise ValueError("invalid previous sample time")
            remaining = previous["finished_epoch"] + args.min_gap - time.time()
            while remaining > 0:
                print(f"WAIT {remaining:.1f}s before sampling; no HTTP requests yet", flush=True)
                time.sleep(min(10, remaining))
                remaining = previous["finished_epoch"] + args.min_gap - time.time()
        os.umask(0o077)
        output.mkdir(parents=True, exist_ok=True)
        document = {"format": FORMAT, "profile_sha256": profile_hash(), "profile": PROFILE,
                    "volatile_report_sha256": VOLATILE_SHA256, "base_url": args.base_url,
                    "started_epoch": time.time(), "complete": False, "routes": []}
        errors = []
        deadline = time.monotonic() + args.max_seconds
        for item in PROFILE:
            row = {key: item[key] for key in ("method", "path", "class", "mode")}
            row["started_epoch"] = time.time()
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CanaryEnvironmentError("collection hard deadline exhausted; no request sent")
                status, content_type, body = fetch(args.base_url, token, item["path"], min(args.timeout, remaining))
                if status in {401, 403, 429} or status >= 500:
                    raise CanaryEnvironmentError(f"service unavailable: HTTP {status}")
                if status != 200:
                    raise ValueError(f"unexpected HTTP {status}")
                row["snapshot"] = {"status": status, "content_type": content_type, **project(body, item)}
                print(f"OK {item['class']} {item['mode']:6} GET {item['path']}", flush=True)
            except Exception as error:
                row["error"] = f"{type(error).__name__}: {error}"
                row["error_kind"] = "ENV" if isinstance(error, CanaryEnvironmentError) else "DATA"
                errors.append(item["path"])
                print(f"{row['error_kind']} GET {item['path']}: {row['error']}", flush=True)
            row["finished_epoch"] = time.time()
            document["routes"].append(row)
            if row.get("error_kind") == "ENV":
                break
        if not errors:
            try:
                validate_history(document["routes"])
            except Exception as error:
                errors.append(f"business invariant: {error}")
        failure_kind = "ENV" if any(row.get("error_kind") == "ENV" for row in document["routes"]) else ("DATA" if errors else None)
        document.update(finished_epoch=time.time(), complete=not errors, errors=errors, failure_kind=failure_kind,
                        expected_routes=len(PROFILE), recorded_routes=len(document["routes"]))
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output, prefix=".manifest-", delete=False) as temporary:
            json.dump(document, temporary, ensure_ascii=False, indent=2, allow_nan=False)
            temporary.write("\n")
            pending = temporary.name
        os.replace(pending, output / "manifest.json")
        if failure_kind == "ENV":
            print(f"COLLECT ENV: {len(document['routes'])}/{len(PROFILE)} routes recorded; incomplete snapshot, exit=1", flush=True)
        else:
            print(f"COLLECT {'PASS' if document['complete'] else 'FAIL'}: {len(PROFILE)} unique GETs, 5 A business routes, 4 B fact projections, 13 schema monitors", flush=True)
        return 0 if document["complete"] else 1
    except Exception as error:
        print(f"COLLECT FAIL: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
