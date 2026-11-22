#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 `source_label / acoustic_proximity / speaker` 回填进活动产物的每一条 evidence。

为什么需要它
------------
2026-11-04 实测 `2026-11-02.json`：**1868 条 evidence，0 条带 source_label**。
键只有 `{source_id, segment_id, row_id, quote}` —— 因为 prompt 的 evidence schema
就只要求这四个（不是写入时被白名单过滤掉的）。

而 `source_label`（本人 / 非本人 / 未知）是**机器能不能自动分清「本人」与「视频」的唯一依据**。
没有它，每一张卡都得人读原话。

运行时已经改成在 `_verify_and_sanitize_v2_response` 里确定性补写（见
`activity_timeline_runtime.enrich_evidence_source`）。本脚本负责**把历史产物补齐**，
口径与运行时逐字一致（直接 import 同一个函数）。

用法
----
    python3 scripts/backfill_evidence_source_label.py --date 2026-11-02           # dry-run（默认）
    python3 scripts/backfill_evidence_source_label.py --date 2026-11-02 --apply   # 写盘（自动备份）
    python3 scripts/backfill_evidence_source_label.py --all-days --apply                     # 所有日期
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.activity_timeline_runtime import enrich_evidence_source  # noqa: E402

BEIJING = timezone(timedelta(hours=8))


def build_source_index(watch_root: Path) -> dict[str, Mapping[str, Any]]:
    """source_id → ASR 行。键必须用 source_id（`segment_id` 跨天重复，会串日期）。"""
    index: dict[str, Mapping[str, Any]] = {}
    for path in glob.glob(str(watch_root / "*" / "*" / "formal-asr" / "segments" / "*" / "state.json")):
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict) or state.get("status") != "completed":
            continue
        for row in state.get("rows") or []:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            ident = (
                f"{row.get('recording_id')}:{row.get('segment_id')}:"
                f"{row.get('start_offset_seconds') or 0.0}:{text}"
            )
            index["src_" + hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]] = row
    return index


def _iter_items(payload: Mapping[str, Any]):
    for activity in payload.get("activities") or []:
        if isinstance(activity, Mapping):
            for item in activity.get("items") or []:
                if isinstance(item, Mapping):
                    yield item
    for item in payload.get("unassociated_items") or []:
        if isinstance(item, Mapping):
            yield item


def enrich_payload(payload: dict[str, Any], index: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    before = Counter()
    after = Counter()
    total = 0
    for item in _iter_items(payload):
        for ev in item.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            total += 1
            before[str(ev.get("source_label") or "(缺失)")] += 1
            enrich_evidence_source(ev, index.get(str(ev.get("source_id") or "")))
            after[str(ev.get("source_label"))] += 1
    return {"evidence_total": total, "before": dict(before), "after": dict(after)}


def _process(day_file: Path, index, apply: bool, preview: int) -> dict[str, Any]:
    payload = json.loads(day_file.read_text(encoding="utf-8"))
    stats = enrich_payload(payload, index)
    stats["file"] = str(day_file)
    if preview:
        shown = 0
        for item in _iter_items(payload):
            for ev in item.get("evidence") or []:
                if shown >= preview:
                    break
                shown += 1
                print(f"  {str(ev.get('source_id')):22s} label={str(ev.get('source_label')):9s} "
                      f"prox={str(ev.get('acoustic_proximity')):9s} speaker={str(ev.get('speaker'))[:12]:12s} "
                      f":: {str(ev.get('quote'))[:40]}")
    if apply:
        stamp = datetime.now(BEIJING).strftime("%Y%m%d-%H%M%S")
        backup = day_file.with_name(f"{day_file.name}.bak-evlabel-{stamp}")
        shutil.copy2(day_file, backup)
        tmp = day_file.with_name(f".{day_file.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, day_file)
        stats["backup"] = backup.name
    stats["applied"] = apply
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回填 evidence 的 source_label/proximity/speaker（默认 dry-run）")
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--all-days", action="store_true", help="处理该 route 下所有日期")
    ap.add_argument("--route", default="jev")
    ap.add_argument("--output-root", default=str(ROOT / "output" / "local-web"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--preview", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    out_root = Path(args.output_root)
    route_dir = out_root / "activity-timeline" / args.route
    if args.all_days:
        files = sorted(p for p in route_dir.glob("*.json") if ".bak" not in p.name and "corrections" not in p.name)
    elif args.date:
        files = [route_dir / f"{args.date}.json"]
    else:
        print("ERROR: 需要 --date 或 --all-days", file=sys.stderr)
        return 2
    files = [f for f in files if f.is_file()]
    if not files:
        print(f"ERROR: 没有可处理的文件（{route_dir}）", file=sys.stderr)
        return 2

    index = build_source_index(out_root / "watch-sessions")
    if not index:
        print("WARN: 没有索引到 ASR 行，全部会写成 unknown", file=sys.stderr)

    results = []
    for f in files:
        st = _process(f, index, args.apply, args.preview if len(files) == 1 else 0)
        results.append(st)
        if not args.json:
            print(f"{f.name}: evidence {st['evidence_total']:5d}  before={st['before']}  →  after={st['after']}")

    totals_after = Counter()
    totals_before_missing = 0
    for st in results:
        totals_after.update(st["after"])
        totals_before_missing += st["before"].get("(缺失)", 0)

    summary = {
        "files": len(results),
        "asr_rows_indexed": len(index),
        "evidence_missing_before": totals_before_missing,
        "after": dict(totals_after),
        "applied": bool(args.apply),
        "details": [{"file": Path(s["file"]).name, "evidence": s["evidence_total"],
                     "before": s["before"], "after": s["after"]} for s in results],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print()
        print(f"共 {summary['files']} 个文件，修复前缺 source_label 的 evidence: {totals_before_missing}")
        print(f"修复后分布: {summary['after']}")
        print("\n已写盘（每个文件都有 .bak-evlabel-* 备份）" if args.apply else "\nDRY-RUN：未写盘。加 --apply 落盘。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
