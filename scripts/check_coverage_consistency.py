#!/usr/bin/env python3
"""台账一致性自检：扫描所有日期，找出「coverage.through 声称 > 实际处理到」的日子。

背景（Q556/Q557/Q565）：`activity-timeline` 的 `coverage.through` 是**实时水位**，
它只在「成功窗口」或「本轮无待处理记录」时前推，**失败轮不前推也不回退**。
于是出现两类「假承诺」：

  1. `through` 已经推到 23:50，但 17:34–20:07 有 331 行从没被整理过（2026-11-01）；
  2. 凌晨 00:00–08:34 有 1655 行，而窗口从 17:11 才开始（2026-10-26）。

本工具用**可复现的证据**判定每一天真实处理到哪：

  · `metadata.processed_source_ids`：真正进过模型 prompt 的源（后端只在这时写入）；
  · `metadata.window_history[*].cutoff`：成功窗口的最晚时间点；
  · 全量 ASR 行的 `timestamp`：哪些行「本该处理」但没处理。

四分类（互斥、顺序即优先级）：

  consistent  一致        —— 该日没有「到期未处理」的行（或全部可被重转写解释）
  gap         明确漏行    —— 存在 timestamp <= through 且未被处理的真实行
  suspected   疑似漏      —— 当日 0 行但 through 已推进（**不可区分**：可能是无数据，也可能是漏）
  no_data     无数据      —— 当日 0 行且 through 未推进/不存在
  no_timeline 无产物      —— 没有 activity-timeline 文件

只读：不写、不修、不重算任何 output/ 数据。

用法：
    python3 scripts/check_coverage_consistency.py                 # 人读报告
    python3 scripts/check_coverage_consistency.py --json          # 机器读
    python3 scripts/check_coverage_consistency.py --json --fail-on-gap   # 接门禁：有 gap 则 exit 1
    python3 scripts/check_coverage_consistency.py --date 2026-11-01
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "output/local-web"
DEFAULT_ROUTE = "jev"


# ── 基础工具 ──────────────────────────────────────────────────────────────
def parse_dt(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING_TZ)
    return dt.astimezone(BEIJING_TZ)


def derive_source_id(recording_id: str, segment_id: str, start_offset: Any, text: str) -> str:
    """必须与 activity_timeline_runtime.scan_recent_asr_records 完全一致。"""
    ident = f"{recording_id}:{segment_id}:{start_offset}:{text}"
    return f"src_{hashlib.sha256(ident.encode('utf-8')).hexdigest()[:16]}"


# ── ASR 全量扫描（不截断：runtime 默认只扫 150 个文件，会漏掉老段）──────────
def scan_all_asr_rows(root: Path) -> list[dict[str, Any]]:
    watch_root = root / "watch-sessions"
    rows: list[dict[str, Any]] = []
    if not watch_root.is_dir():
        return rows
    for state_file in watch_root.glob("*/*/formal-asr/segments/*/state.json"):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict) or state.get("status") != "completed":
            continue
        state_rows = state.get("rows")
        if not isinstance(state_rows, list):
            continue
        for idx, row in enumerate(state_rows, 1):
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            started = row.get("absolute_started_at") or row.get("started_at_local")
            dt = parse_dt(str(started) if started else None)
            if dt is None:
                continue
            seg_id = str(row.get("segment_id") or f"seg_{idx}").strip()
            rec_id = str(row.get("recording_id") or "").strip()
            start_offset = str(row.get("start_offset_seconds") or 0.0)
            rows.append({
                "source_id": derive_source_id(rec_id, seg_id, start_offset, text),
                "recording_id": rec_id,
                "segment_id": seg_id,
                "row_id": idx,
                "timestamp": dt,
                "text": text,
            })
    rows.sort(key=lambda r: r["timestamp"])
    return rows


# ── 单日判定 ──────────────────────────────────────────────────────────────
def classify_day(date: str, timeline: dict[str, Any] | None,
                 rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "date": date,
        "rows_total": len(rows),
        "through": None,
        "last_success_cutoff": None,
        "source_count": None,
        "window_count": 0,
        "rows_processed": 0,
        "rows_after_through": 0,
        "rows_retranscribed": 0,
        "rows_unprocessed_due": 0,
        "through_crossed": False,
        "gap_count": 0,
        "gap_first": None,
        "gap_last": None,
        "gap_examples": [],
        "classification": None,
        "reason": "",
    }

    if timeline is None:
        result["classification"] = "no_timeline"
        result["reason"] = "没有 activity-timeline 产物"
        return result

    coverage = timeline.get("coverage") if isinstance(timeline.get("coverage"), dict) else {}
    through = parse_dt(coverage.get("through"))
    result["through"] = through.isoformat() if through else None
    result["source_count"] = coverage.get("source_count")

    meta = timeline.get("metadata") if isinstance(timeline.get("metadata"), dict) else {}
    processed = set(meta.get("processed_source_ids") or [])

    history = timeline.get("window_history") or []
    cutoffs = [parse_dt(w.get("cutoff")) for w in history if isinstance(w, dict)]
    cutoffs = [c for c in cutoffs if c is not None]
    result["window_count"] = len(history)
    result["last_success_cutoff"] = max(cutoffs).isoformat() if cutoffs else None
    last_cutoff = max(cutoffs) if cutoffs else None

    # 已处理的样本行（用于识别重转写）
    processed_keys: set[tuple[str, int]] = set()
    for r in rows:
        if r["source_id"] in processed:
            processed_keys.add((r["segment_id"], r["row_id"]))

    due: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    retranscribed: list[dict[str, Any]] = []
    processed_count = 0
    for r in rows:
        if r["source_id"] in processed:
            processed_count += 1
            continue
        # 未处理：先看是否已过 through（未到期的行不算漏）
        if through is not None and r["timestamp"] > through:
            after.append(r)
            continue
        # 未过期却没处理：能否用「重转写（同一 segment_id+row_id 换了 source_id）」解释？
        if (r["segment_id"], r["row_id"]) in processed_keys:
            retranscribed.append(r)
        else:
            due.append(r)

    result["rows_processed"] = processed_count
    result["rows_after_through"] = len(after)
    result["rows_retranscribed"] = len(retranscribed)
    result["rows_unprocessed_due"] = len(due) + len(retranscribed)
    # 「through 说谎」的正确定义：through 已经越过了这些行的时刻，却仍没处理它们。
    # （注意不能用 through > last_success_cutoff 判断：跨天时 through 与最后成功窗口
    #   可能同为次日 00:10，但当日 08:34 的行依然在 through 之前。）
    result["through_crossed"] = bool(through is not None and due)
    result["gap_count"] = len(due)
    if due:
        result["gap_first"] = due[0]["timestamp"].isoformat()
        result["gap_last"] = due[-1]["timestamp"].isoformat()
        result["gap_examples"] = [
            {"timestamp": r["timestamp"].isoformat(), "segment_id": r["segment_id"],
             "row_id": r["row_id"], "text": r["text"][:24]}
            for r in due[:3]
        ]

    # ── 分类（顺序即优先级）──
    if not rows:
        if through is None:
            result["classification"] = "no_data"
            result["reason"] = "当日 ASR 行数为 0，且 through 不存在"
        elif last_cutoff is not None and through <= last_cutoff:
            result["classification"] = "no_data"
            result["reason"] = f"当日 ASR 行数为 0，through={through.strftime('%H:%M')} 未超出最后成功窗口"
        else:
            result["classification"] = "suspected"
            result["reason"] = (
                f"当日 ASR 行数为 0，但 through 已推进到 {through.strftime('%H:%M')}"
                + (f"（最后成功窗口 {last_cutoff.strftime('%H:%M')}）" if last_cutoff else "")
                + " → 无法区分『本来就没有数据』与『漏处理』"
            )
        return result

    if due:
        result["classification"] = "gap"
        span = f"{due[0]['timestamp'].strftime('%m-%d %H:%M:%S')} → {due[-1]['timestamp'].strftime('%H:%M:%S')}"
        if result["through_crossed"]:
            through_txt = through.strftime("%m-%d %H:%M")
            last_txt = last_cutoff.strftime("%m-%d %H:%M") if last_cutoff else "（无成功窗口）"
            result["reason"] = (
                f"漏 {len(due)} 行（{span}）；through 声称到 {through_txt}，"
                f"而最后成功窗口只到 {last_txt}"
            )
        else:
            result["reason"] = (
                f"未处理 {len(due)} 行（{span}）；through 缺失"
                "（尚未宣称处理到任何位置 → 不构成『through 说谎』，只是没做完）"
            )
        return result

    result["classification"] = "consistent"
    if retranscribed:
        result["reason"] = f"无到期未处理行；{len(retranscribed)} 行属重转写（segment_id+row_id 已处理过，source_id 变化）"
    elif after:
        result["reason"] = f"无到期未处理行；{len(after)} 行晚于 through，尚未到期"
    else:
        result["reason"] = "全部行均已处理"
    return result


# ── 主流程 ────────────────────────────────────────────────────────────────
def analyse(root: Path, route: str, only_date: str | None = None) -> dict[str, Any]:
    tl_dir = root / "activity-timeline" / route
    timelines: dict[str, dict[str, Any]] = {}
    if tl_dir.is_dir():
        for path in sorted(tl_dir.glob("*.json")):
            if path.name.endswith(".corrections.json"):
                continue
            date = path.stem
            # 只认 YYYY-MM-DD（排除 .scheduling-state 等非日期产物）
            if len(date) != 10 or date[4] != "-" or date[7] != "-":
                continue
            if only_date and date != only_date:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                timelines[date] = data

    rows = scan_all_asr_rows(root)
    by_date: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_date.setdefault(r["timestamp"].date().isoformat(), []).append(r)

    dates = sorted(set(timelines) | set(by_date))
    if only_date:
        dates = [d for d in dates if d == only_date]

    days = [classify_day(d, timelines.get(d), by_date.get(d, [])) for d in dates]
    summary: dict[str, int] = {"consistent": 0, "gap": 0, "suspected": 0, "no_data": 0, "no_timeline": 0}
    for d in days:
        summary[d["classification"]] = summary.get(d["classification"], 0) + 1

    return {
        "generated_at": datetime.datetime.now(tz=BEIJING_TZ).isoformat(timespec="seconds"),
        "root": str(root),
        "route": route,
        "asr_rows_scanned": len(rows),
        "timeline_files": len(timelines),
        "summary": summary,
        "days": days,
    }


def render_text(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        "=== coverage.through 一致性自检 ===",
        f"根目录 : {report['root']}  (route={report['route']})",
        f"扫描   : {report['asr_rows_scanned']} 条 ASR 行 / {report['timeline_files']} 个 timeline 文件",
        f"分类   : 一致 {s.get('consistent',0)} | 明确漏行 {s.get('gap',0)} | "
        f"疑似漏 {s.get('suspected',0)} | 无数据 {s.get('no_data',0)} | 无产物 {s.get('no_timeline',0)}",
        "",
    ]
    order = {"gap": 0, "suspected": 1, "consistent": 2, "no_data": 3, "no_timeline": 4}
    mark = {"gap": "❌ 明确漏行", "suspected": "⚠️  疑似漏", "consistent": "✅ 一致",
            "no_data": "·  无数据", "no_timeline": "·  无产物"}
    for day in sorted(report["days"], key=lambda d: (order.get(d["classification"], 9), d["date"])):
        lines.append(f"{mark[day['classification']]:12} {day['date']}  行={day['rows_total']:5d} "
                     f"已处理={day['rows_processed']:5d} 重转写={day['rows_retranscribed']:4d} "
                     f"窗口={day['window_count']:3d}  through={str(day['through'])[11:16] or '—'}")
        if day["classification"] in ("gap", "suspected"):
            lines.append(f"              └ {day['reason']}")
    gaps = [d for d in report["days"]
            if d["classification"] == "gap" and d.get("through_crossed")]
    incomplete = [d for d in report["days"]
                  if d["classification"] == "gap" and not d.get("through_crossed")]
    if incomplete:
        lines += ["", "=== 未做完但未说谎（through 未越过最后成功窗口）==="]
        for d in sorted(incomplete, key=lambda x: x["date"]):
            lines.append(f"  {d['date']}: 未处理 {d['gap_count']} 行  through={d['through']}")
    if gaps:
        lines += ["", "=== 说谎的日期（through 声称 > 实际处理）==="]
        total = 0
        for d in sorted(gaps, key=lambda x: -x["gap_count"]):
            total += d["gap_count"]
            lines.append(f"  {d['date']}: 漏 {d['gap_count']} 行  "
                         f"[{d['gap_first']} → {d['gap_last']}]  through={d['through']}")
        lines.append(f"  合计漏行：{total}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="coverage.through 与实际处理进度的一致性自检")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="output/local-web 根目录")
    parser.add_argument("--route", default=DEFAULT_ROUTE, help="activity-timeline 路由（默认 jev）")
    parser.add_argument("--date", default=None, help="只检查某一天（YYYY-MM-DD）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--fail-on-gap", action="store_true",
                        help="存在明确漏行时以 exit 1 结束（可接门禁）")
    args = parser.parse_args(argv)

    report = analyse(Path(args.root).expanduser().resolve(), args.route, args.date)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))

    if args.fail_on_gap and report["summary"].get("gap", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
