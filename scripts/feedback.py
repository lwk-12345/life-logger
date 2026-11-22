#!/usr/bin/env python3
"""标注回灌 CLI：把人工判定写进 data/feedback/，并让它立刻生效。

    python3 scripts/feedback.py migrate [--dry-run]
    python3 scripts/feedback.py record --day 2026-11-03 --target lifecycle \\
        --id act_012 --after media --basis "凌晨在放英文赛事解说" [--before live]
    python3 scripts/feedback.py list --day 2026-11-03
    python3 scripts/feedback.py overrides --day 2026-11-03 --target lifecycle
    python3 scripts/feedback.py fewshot --limit 5
    python3 scripts/feedback.py explain --day 2026-11-03 --id act_012
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import feedback_store as fs  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="标注回灌（数据飞轮）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("migrate", help="把历史上散落的人工表搬进 data/feedback/")
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--with-basis", action="store_true", help="顺带用审计 TSV 的依据补全 basis")
    m.add_argument("--audit", action="store_true",
                   help="[Q826] 迁移前后都打印忠实来源统计（各级多少、多少冲突）")

    r = sub.add_parser("record", help="记一条人工判定")
    r.add_argument("--day", required=True)
    r.add_argument("--target", default="lifecycle", choices=fs.TARGETS)
    r.add_argument("--id", required=True, dest="ident")
    r.add_argument("--after", required=True)
    r.add_argument("--before", default=None)
    r.add_argument("--basis", default="")
    r.add_argument("--source", default="manual")

    l = sub.add_parser("list", help="列出记录")
    l.add_argument("--day", default=None)
    l.add_argument("--limit", type=int, default=50)

    o = sub.add_parser("overrides", help="看某天某维度的最终覆盖表")
    o.add_argument("--day", required=True)
    o.add_argument("--target", default="lifecycle", choices=fs.TARGETS)

    f = sub.add_parser("fewshot", help="渲染 few-shot 样例块（路径 B）")
    f.add_argument("--limit", type=int, default=5)
    f.add_argument("--day", default=None)

    cj = sub.add_parser("emit-category-js", help="把 category 判定反向生成 web/activity-category-override.js")
    cj.add_argument("--dry-run", action="store_true")

    au = sub.add_parser("audit", help="[Q826] 忠实来源统计：各级多少、多少冲突")
    au.add_argument("--day", default=None)
    au.add_argument("--target", default="lifecycle", choices=fs.TARGETS)

    e = sub.add_parser("explain", help="解释某条活动最终为什么是这个判定")
    e.add_argument("--day", required=True)
    e.add_argument("--id", required=True, dest="ident")

    a = ap.parse_args()

    if a.cmd == "migrate":
        before = fs.audit_provenance() if a.audit else None
        stats = fs.migrate_legacy(ROOT, dry_run=a.dry_run)
        if a.with_basis:
            stats = {**stats, **{"basis": fs.backfill_basis_from_audits(ROOT, dry_run=a.dry_run)}}
        payload = {"dry_run": a.dry_run, **stats}
        if a.audit:
            payload["audit_before"] = before
            payload["audit_after"] = fs.audit_provenance()
        print(json.dumps(payload, ensure_ascii=False, indent=1 if a.audit else None))
        return 0

    if a.cmd == "audit":
        payload = fs.audit_provenance()
        if a.day:
            payload["day_stats"] = fs.resolution_stats(a.day, a.target)
            payload["day_conflicts"] = {
                k: v for k, v in fs.resolve(a.day, a.target).items() if v["conflict"]}
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0

    if a.cmd == "record":
        rec = fs.record({"day": a.day, "target": a.target, "activity_id": a.ident,
                         "after": a.after, "before": a.before, "basis": a.basis,
                         "source": a.source})
        print(json.dumps(rec, ensure_ascii=False))
        return 0

    if a.cmd == "list":
        rows = list(fs.iter_records(a.day))[-a.limit:]
        for rec in rows:
            print("%s\t%s\t%s\t%s\t%s" % (rec.get("day"), rec.get("target"),
                                          rec.get("activity_id"), rec.get("after"),
                                          (rec.get("basis") or "")[:60]))
        print("-- %d 条（days: %s）" % (len(rows), ",".join(fs.days()) or "无"))
        return 0

    if a.cmd == "overrides":
        ov = fs.overrides(a.day, a.target)
        print(json.dumps(ov, ensure_ascii=False, indent=1))
        return 0

    if a.cmd == "fewshot":
        ex = fs.few_shot_examples(a.day, limit=a.limit)
        print(fs.render_few_shot_block(ex) or "(没有可用样例：basis 太短或没有判定)")
        return 0

    if a.cmd == "emit-category-js":
        res = fs.emit_category_js(ROOT)
        print(json.dumps(res, ensure_ascii=False))
        return 0

    if a.cmd == "explain":
        info = fs.resolve(a.day, "lifecycle").get(a.ident) or {}
        confirmed = fs.lifecycle_override(a.day, a.ident)
        weak = fs.lifecycle_weak(a.day, a.ident)
        print(json.dumps({
            "day": a.day, "activity_id": a.ident,
            "records": info.get("records", 0),
            "values": info.get("values", []),
            "provenance": info.get("provenance"),
            "conflict": bool(info.get("conflict")),
            "⓪_采用（仅 user_confirmed）": confirmed or "否",
            "⑦_弱信号（链尾 tiebreaker）": weak or "无",
        }, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
