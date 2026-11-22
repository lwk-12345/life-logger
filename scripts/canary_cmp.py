#!/usr/bin/env python3
"""Compare complete canary snapshots. Exit 0 = match; exit 1 = difference/error."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from canary_collect import FORMAT, PROFILE, kind, profile_hash


def diff_values(old, new, path="$"):
    if kind(old) != kind(new):
        return [{"path": path, "old": old, "new": new, "kind": "type"}]
    if isinstance(old, dict):
        result = []
        for key in sorted(old.keys() | new.keys()):
            field = path + "." + key
            if key not in old or key not in new:
                result.append({"path": field, "old": old.get(key, "<missing>"),
                               "new": new.get(key, "<missing>"), "kind": "missing"})
            else:
                result.extend(diff_values(old[key], new[key], field))
        return result
    if isinstance(old, list):
        result = []
        if len(old) != len(new):
            result.append({"path": path + ".length", "old": len(old), "new": len(new)})
        for index, (before, after) in enumerate(zip(old, new)):
            result.extend(diff_values(before, after, f"{path}[{index}]"))
        return result
    return [] if old == new else [{"path": path, "old": old, "new": new}]


def compare_documents(base, target):
    errors = []
    expected = {(item["method"], item["path"]): item for item in PROFILE}
    def validate(document, label):
        indexed = {}
        if not isinstance(document, dict):
            errors.append({"path": label, "old": "snapshot object", "new": type(document).__name__})
            return indexed
        for field, wanted in (("format", FORMAT), ("profile_sha256", profile_hash()), ("complete", True)):
            if document.get(field) != wanted:
                errors.append({"path": label + "." + field, "old": wanted, "new": document.get(field)})
        rows = document.get("routes")
        if not isinstance(rows, list):
            errors.append({"path": label + ".routes", "old": "array", "new": rows})
            return indexed
        for row in rows:
            if not isinstance(row, dict):
                errors.append({"path": label + ".routes", "old": "route object", "new": row})
                continue
            key = (row.get("method"), row.get("path"))
            if key in indexed:
                errors.append({"path": label + ".duplicate_key", "old": "unique method + full query path", "new": list(key)})
            indexed[key] = row
            policy = expected.get(key)
            snapshot = row.get("snapshot")
            if not policy or row.get("error") or not isinstance(snapshot, dict):
                errors.append({"path": label + ".invalid_route", "old": "complete configured route", "new": list(key)})
                continue
            for field in ("mode", "class"):
                if row.get(field) != policy[field]:
                    errors.append({"path": label + "." + field, "old": policy[field], "new": row.get(field)})
            if snapshot.get("status") != 200:
                errors.append({"path": label + ".status", "old": 200, "new": snapshot.get("status")})
            wanted_keys = {"status", "content_type", "schema"} | ({"facts"} if policy["mode"] == "facts" else set())
            if set(snapshot) != wanted_keys:
                errors.append({"path": label + ".snapshot_keys", "old": sorted(wanted_keys), "new": sorted(snapshot)})
        if set(indexed) != set(expected):
            errors.append({"path": label + ".coverage", "old": [list(key) for key in sorted(expected)],
                           "new": [list(key) for key in sorted(indexed)]})
        return indexed
    before = validate(base, "base")
    after = validate(target, "target")
    if errors:
        return errors
    for key in sorted(expected):
        errors.extend(diff_values(before[key]["snapshot"], after[key]["snapshot"],
                                  f"routes[{key[0]} {key[1]}]"))
    return errors


def load(path):
    path = Path(path)
    if path.is_dir():
        path /= "manifest.json"
    return json.loads(path.read_text(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON constant")))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--max-diffs", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        base, target = load(args.base), load(args.target)
        for label, document in (("base", base), ("target", target)):
            if not isinstance(document, dict):
                continue
            rows = document.get("routes", [])
            failures = [row for row in rows if isinstance(row, dict) and row.get("error_kind") == "ENV"] if isinstance(rows, list) else []
            if document.get("failure_kind") == "ENV" or failures:
                for row in failures:
                    print(f"ENV {label} {row.get('method')} {row.get('path')}: {row.get('error')}")
                print(f"CANARY ENV: {label} snapshot has environment failures; comparison not performed; exit=1")
                return 1
        differences = compare_documents(base, target)
    except Exception as error:
        print(f"DIFF snapshot: {type(error).__name__}: {error}")
        return 1
    for item in differences[:max(1, args.max_diffs)]:
        old = json.dumps(item["old"], ensure_ascii=False)
        new = json.dumps(item["new"], ensure_ascii=False)
        print(f"DIFF {item['path']}: old={old} new={new}")
    print(f"CANARY {'DIFF' if differences else 'MATCH'}: {len(PROFILE)} routes, {len(differences)} differences")
    if len(differences) > max(1, args.max_diffs):
        print(f"Additional differences omitted from display: {len(differences) - max(1, args.max_diffs)}")
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())
