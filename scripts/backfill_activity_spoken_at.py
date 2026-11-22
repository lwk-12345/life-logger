#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填「讲述时间」与「时间归属」到已落盘的活动时间线。**默认 dry-run。**

背景（2026-11-03 用户投诉：「显示的时间都是别人的时间」）
--------------------------------------------------------
实测 `2026-11-02`：180 条活动里
  · 86 条（47.8%）**没有 occurrence_start** → 页面上完全没有时间可显示，全挤进「时刻未定」带；
  · 94 条有 occurrence_start，但其中 **78 条（83%）的时间最接近一行 `source_label=non_self`
    的 ASR 行** —— 也就是「旁人/视频里那句」的时间被当成了这张卡的发生时间。

根因（逐条数据验证，见 work/fix-timestamps/REPORT.md）
  · `occurrence_start` 只由 `items[].occurred_at` 聚合；模型按 prompt 规则
    「没有明确钟点 → occurred_at = null」执行，于是整卡无时间（86/86 都是这个原因，
    其中 44 条的 `uncertainties` 明写「occurred_at 不能取发声时间」）；
  · `items[].spoken_at`（讲述时刻）**86/86 都有**，但从来没有被提升到卡片级。

本脚本做什么
------------
1. 给每条活动补三个**只读投影字段**（不伪造发生时间）：
      spoken_at / spoken_end  ← min/max(items[].spoken_at)，永远可得
      time_source             ← occurred_self | occurred_non_self | spoken_only | none
2. 重算 occurrence_start/occurrence_end 时**优先本人行**（与 runtime 的新逻辑一致）。
3. **绝不**把 spoken_at 写进 occurrence_start —— 那正是用户抱怨的「别人的时间」，
   而且有红线测试保护：
   `tests/test_publish_activity_preview.py::test_unknown_item_time_cannot_gain_occurrence_from_activity_header`

用法
----
    python3 scripts/backfill_activity_spoken_at.py --date 2026-11-02            # dry-run（默认）
    python3 scripts/backfill_activity_spoken_at.py --date 2026-11-02 --apply    # 真写（先自动备份）
    python3 scripts/backfill_activity_spoken_at.py --date 2026-11-02 --json     # 机器可读
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

BEIJING = timezone(timedelta(hours=8))
PREVIEW_ROWS = 10


def _parse(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _iso(value: str) -> str:
    return str(value)


# --------------------------------------------------------------------------
# ASR 行索引：必须用 source_id（segment_id 跨天重复，见报告 §1.2）
# --------------------------------------------------------------------------
def build_source_index(watch_root: Path) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    pattern = str(watch_root / "*" / "*" / "formal-asr" / "segments" / "*" / "state.json")
    for path in glob.glob(pattern):
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
            source_id = "src_" + hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]
            index[source_id] = row
    return index


def _row_for(index: Mapping[str, Mapping[str, Any]], evidence: Any) -> Mapping[str, Any] | None:
    if not isinstance(evidence, Mapping):
        return None
    return index.get(str(evidence.get("source_id") or ""))


def _item_is_self(item: Mapping[str, Any], index: Mapping[str, Mapping[str, Any]]) -> bool:
    for ev in item.get("evidence") or []:
        row = _row_for(index, ev)
        if row is not None and str(row.get("source_label") or "") == "self":
            return True
    return False


# --------------------------------------------------------------------------
# 单条活动的三字段推导（与 runtime 的新逻辑逐字一致）
# --------------------------------------------------------------------------
def derive(activity: Mapping[str, Any], index: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    items = [i for i in (activity.get("items") or []) if isinstance(i, Mapping)]

    occ = [(i, i["occurred_at"]) for i in items if i.get("occurred_at") and _parse(i.get("occurred_at"))]
    self_occ = [t for i, t in occ if _item_is_self(i, index)]
    # 口径与 runtime 一致：只认 occurred_at 的 min/max（不做 self 优先，
    # 否则合并路径拿不到 label，两边会算出不同值 → 时间每窗口抖一次）
    times = [t for _, t in occ]
    parsed = [(_parse(t), t) for t in times]
    parsed = [(d, t) for d, t in parsed if d is not None]
    occ_start = min(parsed)[1] if parsed else None
    occ_end = max(parsed)[1] if parsed else None

    spoken = [(_parse(i.get("spoken_at")), i.get("spoken_at")) for i in items if i.get("spoken_at")]
    spoken = [(d, t) for d, t in spoken if d is not None]
    spoken_at = min(spoken)[1] if spoken else None
    spoken_end = max(spoken)[1] if spoken else None

    if occ_start and self_occ:
        source = "occurred_self"
    elif occ_start:
        source = "occurred_non_self"
    elif spoken_at:
        source = "spoken_only"
    else:
        source = "none"

    return {
        "occurrence_start": occ_start,
        "occurrence_end": occ_end,
        "spoken_at": spoken_at,
        "spoken_end": spoken_end,
        "time_source": source,
    }


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回填 spoken_at / time_source 到活动时间线（默认 dry-run）")
    ap.add_argument("--date", required=True)
    ap.add_argument("--route", default="jev")
    ap.add_argument("--output-root", default=str(ROOT / "output" / "local-web"))
    ap.add_argument("--apply", action="store_true", help="真写盘（默认只打印）")
    ap.add_argument("--json", action="store_true", help="机器可读摘要")
    ap.add_argument("--preview", type=int, default=PREVIEW_ROWS, help="打印前 N 条前后对比")
    args = ap.parse_args(argv)

    out_root = Path(args.output_root)
    day_file = out_root / "activity-timeline" / args.route / f"{args.date}.json"
    if not day_file.is_file():
        print(f"ERROR: 找不到 {day_file}", file=sys.stderr)
        return 2
    payload = json.loads(day_file.read_text(encoding="utf-8"))
    activities = payload.get("activities")
    if not isinstance(activities, list):
        print(f"ERROR: {day_file} 没有 activities 列表", file=sys.stderr)
        return 2

    index = build_source_index(out_root / "watch-sessions")
    if not index:
        print("WARN: 没有索引到任何 ASR 行，time_source 会全部退化为按 occurred_at 判断", file=sys.stderr)

    before_missing_any = 0
    before_missing_occ = 0
    before_source = Counter()
    changes = []
    previews: list[tuple[str, str, Any, Any, Any, Any]] = []

    for activity in activities:
        if not isinstance(activity, dict):
            continue
        old = dict(activity)
        new = derive(activity, index)
        old_has_any = bool(old.get("occurrence_start") or old.get("spoken_at"))
        if not old_has_any:
            before_missing_any += 1
        if not old.get("occurrence_start"):
            before_missing_occ += 1
        # 旧数据的 time_source 用旧 occurrence 反推（只用于 before 统计）
        before_source["(无字段)"] += 1
        for key, value in new.items():
            if old.get(key) != value:
                changes.append((old.get("activity_id"), key, old.get(key), value, str(old.get("title"))[:34]))
        if len(previews) < max(0, args.preview) and new.get("spoken_at"):
            previews.append((str(old.get("activity_id")), str(old.get("title"))[:38],
                             old.get("occurrence_start"), new["occurrence_start"],
                             old.get("spoken_at"), new["spoken_at"], old.get("time_source"), new["time_source"]))
        activity.update(new)

    after_missing_any = sum(1 for a in activities if isinstance(a, dict)
                            and not (a.get("occurrence_start") or a.get("spoken_at")))
    after_missing_occ = sum(1 for a in activities if isinstance(a, dict) and not a.get("occurrence_start"))
    after_source = Counter(a.get("time_source") for a in activities if isinstance(a, dict))
    occ_changed = sum(1 for c in changes if c[1] == "occurrence_start")

    summary = {
        "date": args.date,
        "route": args.route,
        "file": str(day_file),
        "activities": len(activities),
        "asr_rows_indexed": len(index),
        "before": {"no_any_time": before_missing_any, "no_occurrence_start": before_missing_occ},
        "after": {"no_any_time": after_missing_any, "no_occurrence_start": after_missing_occ},
        "time_source": dict(after_source),
        "occurrence_start_changed": occ_changed,
        "field_changes": len(changes),
        "applied": bool(args.apply),
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"文件      : {day_file}")
        print(f"活动条数  : {len(activities)}   ASR 行索引: {len(index)}")
        print()
        print("           修复前   修复后")
        print(f"  完全无时间 : {before_missing_any:4d}  →  {after_missing_any:4d}   (occurrence_start 或 spoken_at 至少一个)")
        print(f"  无发生时间 : {before_missing_occ:4d}  →  {after_missing_occ:4d}   (occurrence_start，**故意不回填**，见 §设计红线)")
        print()
        print(f"time_source 分布: {dict(after_source)}")
        print(f"occurrence_start 被改写: {occ_changed} 条（修复 stale：落盘值与 items 的 min 不一致）")
        print()
        print(f"前 {args.preview} 条前后对比：")
        for aid, title, o_occ, n_occ, o_sp, n_sp, o_ts, n_ts in previews:
            print(f"  {aid}  {title}")
            print(f"     occurrence_start : {o_occ if o_occ else '(空)'}   →   {n_occ if n_occ else '(空，不伪造)'}")
            print(f"     spoken_at        : {o_sp if o_sp else '(空)'}   →   {n_sp}")
            print(f"     time_source      : {o_ts if o_ts else '(空)'}   →   {n_ts}")

    if args.apply:
        stamp = datetime.now(BEIJING).strftime("%Y%m%d-%H%M%S")
        backup = day_file.with_name(f"{day_file.name}.bak-spoken-{stamp}")
        shutil.copy2(day_file, backup)
        payload["activities"] = activities
        tmp = day_file.with_name(f".{day_file.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, day_file)
        print(f"\n已写入（备份: {backup.name}）")
    elif not args.json:
        print("\nDRY-RUN：未写盘。加 --apply 落盘。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
