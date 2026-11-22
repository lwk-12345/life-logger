#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Q837 方案 B（退役 dated 分档表）的可逆 **dry-run** 计划生成器。

⚠️ 本脚本**只打印计划，绝不写任何文件**：
   * 不删 / 不改 `web/activity-buckets*.js`
   * 不改 `web/daily-timeline-live.html`
   * 不改 `data/feedback/*.jsonl`、不改历史数据
   * 末尾会用 sha256 自证「跑前 == 跑后」

它做四件事：
  1. **盘点**：有哪些 dated 表文件、聚合表里有哪几天；
  2. **读者清单**：扫出所有还在读 dated 表的代码位置（文件:行号 + 角色）；
  3. **影响量化**：逐日对比「表里的分档」vs「后端 read_day 的三态」，说明退役后哪些卡片会变；
  4. **diff 建议**：把计划中的改动在内存里生成 unified diff 预览（可人工审阅），
     并给出每个断点的替代读法。

用法：
    python3 scripts/retire_dated_tables_dryrun.py
    python3 scripts/retire_dated_tables_dryrun.py --json      # 机器可读
    python3 scripts/retire_dated_tables_dryrun.py --max-days 3
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
sys.path.insert(0, str(ROOT / "src"))

PAGE = WEB / "daily-timeline-live.html"
AGGREGATE = WEB / "activity-buckets.js"
DATE_GLOB = "activity-buckets-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].js"

# 每个读取点 → 退役后该读什么（dry-run 的核心产出）
REPLACEMENTS: dict[str, str] = {
    "<script src=\"/activity-buckets.js\">":
        "删除这个 script 标签（以及上方注释）；后端三态已在 payload.activities[].lifecycle 里，无需额外数据源。",
    "bucketTableFor":
        "删除。日期键查表没有替代者：分档结论来自后端 lifecycle，不再需要本地表。",
    "bucketSetsFor":
        "删除（只为查表存在）。",
    "manualActivityBucket":
        "删除。人工覆盖改读 data/feedback（user_confirmed 已有 ⓪ 优先级，见 feedback_store.lifecycle_override）。",
    "activityBucket(manual 分支)":
        "删除 `var manual = manualActivityBucket(...)` 及其回退；后端 lifecycle 已是第一优先级。",
    "__isMediaEvent(manual 分支)":
        "删除 manualActivityBucket 调用；保留 speech_target / categories 两条（它们来自后端字段，不是本地表）。",
    "classify(card)(manual 回退)":
        "删除回退；统计未就绪时返回 \"live\"（宁可不折，也不按旧表折）。",
    "tests/activity-bucket-single-source.test.mjs":
        "整个文件随表退役一并移除（它守的是\"表是唯一来源\"这个不再成立的前提）。",
    "work/buckets-to-file-20261103/gen_buckets.py":
        "归档到 work/dated-table-retire-plan/archive/，不再运行。",
    "work/bucket-gen/gen_buckets.py":
        "保留但降级为「历史重建工具」，输出只写 work/，不再写 web/。",
}

SCAN_PATTERNS = [
    ("ACTIVITY_BUCKETS", "读聚合表"),
    ("activity-buckets", "引用表文件/路径"),
    ("bucketTableFor", "按日期查表"),
    ("bucketSetsFor", "把表转成 Set"),
    ("manualActivityBucket", "查单条 id 的人工分档"),
    ("activity-buckets-", "dated 表文件名模式"),
]

SCAN_ROOTS = [WEB, ROOT / "tests", ROOT / "scripts", ROOT / "src"]
SCAN_SUFFIX = {".html", ".js", ".mjs", ".py", ".json"}


# ── 工具 ────────────────────────────────────────────────────────────────────
def sha_tree(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        try:
            h.update(p.name.encode())
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:16]


def dated_files() -> list[Path]:
    return sorted(WEB.glob(DATE_GLOB))


def aggregate_keys() -> list[str]:
    if not AGGREGATE.is_file():
        return []
    text = AGGREGATE.read_text(encoding="utf-8")
    return sorted({m.group(1) for m in re.finditer(r'ACTIVITY_BUCKETS\["([0-9-]{10})"\]', text)})


def table_for(day: str) -> dict[str, list[str]]:
    """从聚合表里抠出某天的 live/media 两组 id（纯读）。"""
    if not AGGREGATE.is_file():
        return {"live": [], "media": []}
    text = AGGREGATE.read_text(encoding="utf-8")
    m = re.search(
        r'ACTIVITY_BUCKETS\["' + re.escape(day) + r'"\]\s*=\s*\{(.*?)\n\};', text, re.S
    )
    if not m:
        return {"live": [], "media": []}
    body = m.group(1)
    out = {}
    for key in ("live", "media"):
        km = re.search(key + r":\s*\[(.*?)\]", body, re.S)
        out[key] = re.findall(r'"(act_[^"]+)"', km.group(1)) if km else []
    return out


# ── 1. 盘点 ─────────────────────────────────────────────────────────────────
def section_inventory() -> dict:
    files = dated_files()
    print("═" * 78)
    print("① 盘点：dated 分档表")
    print("═" * 78)
    print(f"  聚合入口 : {AGGREGATE.relative_to(ROOT)}"
          f"  {'（存在）' if AGGREGATE.is_file() else '（缺失）'}")
    keys = aggregate_keys()
    print(f"  聚合表天数: {len(keys)} → {', '.join(keys) if keys else '—'}")
    print(f"  dated 源文件: {len(files)}")
    for p in files:
        txt = p.read_text(encoding="utf-8")
        n_live = len(re.findall(r'"(act_[^"]+)"', (re.search(r"live:\s*\[(.*?)\]", txt, re.S) or [None, ""])[1] if re.search(r"live:\s*\[(.*?)\]", txt, re.S) else ""))
        n_media = len(re.findall(r'"(act_[^"]+)"', (re.search(r"media:\s*\[(.*?)\]", txt, re.S) or [None, ""])[1] if re.search(r"media:\s*\[(.*?)\]", txt, re.S) else ""))
        print(f"    - {p.name:<44} {p.stat().st_size:>6} B   live={n_live:<3} media={n_media}")
    return {"aggregate": str(AGGREGATE), "days": keys, "files": [p.name for p in files]}


# ── 2. 读者清单 ─────────────────────────────────────────────────────────────
def section_readers() -> list[dict]:
    print()
    print("═" * 78)
    print("② 读者清单：退役后会断的地方（文件:行号）")
    print("═" * 78)
    hits: list[dict] = []
    for base in SCAN_ROOTS:
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in SCAN_SUFFIX:
                continue
            if "work" in path.parts or "__pycache__" in path.parts:
                continue
            if path.name.startswith("activity-buckets-") or path.name == "activity-buckets.js":
                continue                      # 表本身，单独在①里列
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                for needle, role in SCAN_PATTERNS:
                    if needle in line:
                        hits.append({
                            "file": str(path.relative_to(ROOT)),
                            "line": lineno,
                            "role": role,
                            "code": line.strip()[:110],
                        })
                        break
    # 去重（同文件同行多关键词只留一条）
    seen, uniq = set(), []
    for h in hits:
        k = (h["file"], h["line"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(h)

    for h in uniq:
        print(f"  {h['file']}:{h['line']}")
        print(f"      {h['role']}  |  {h['code']}")
    print(f"\n  合计 {len(uniq)} 处")
    return uniq


# ── 3. 影响量化 ─────────────────────────────────────────────────────────────
def section_impact(max_days: int) -> dict:
    print()
    print("═" * 78)
    print("③ 影响量化：退役后哪些卡片会变（表 vs 后端三态）")
    print("═" * 78)
    try:
        from life_logger.activity_timeline_runtime import ActivityTimelineRuntime
    except Exception as exc:                                  # noqa: BLE001
        print(f"  ⚠️ 无法导入 runtime（{type(exc).__name__}: {exc}），跳过量化")
        return {}

    rt = ActivityTimelineRuntime(ROOT / "output" / "local-web", ROOT / "local-config.json")
    report: dict[str, dict] = {}
    for day in aggregate_keys()[:max_days]:
        tbl = table_for(day)
        if not (tbl["live"] or tbl["media"]):
            print(f"\n  {day}: 空表（无 live/media 条目）")
            continue
        try:
            payload = rt.read_day(day)
        except Exception as exc:                              # noqa: BLE001
            print(f"\n  {day}: read_day 失败（{type(exc).__name__}）")
            continue
        acts = {str(a.get("activity_id")): a for a in payload.get("activities") or []}
        in_table = set(tbl["live"]) | set(tbl["media"])
        rows = {"same": [], "media→unknown": [], "media→live": [], "live→media": [],
                "live→unknown": [], "表里有但后端没有": [], "表说 media 后端 live/unknown(前5)": []}
        for aid in sorted(in_table):
            tb = "media" if aid in set(tbl["media"]) else "live"
            act = acts.get(aid)
            if act is None:
                rows["表里有但后端没有"].append(aid)
                continue
            be = str(act.get("lifecycle") or "").lower() or "unknown"
            if tb == be:
                rows["same"].append(aid)
            else:
                key = f"{tb}→{be}"
                rows.setdefault(key, []).append(aid)
        backend_extra = sorted(set(acts) - in_table)
        print(f"\n  {day}  表 {len(in_table)} 条 / 后端 {len(acts)} 条"
              f"（表里没登记、靠后端兜底的 {len(backend_extra)} 条）")
        for key in ("same", "media→unknown", "media→live", "live→media", "live→unknown",
                    "表里有但后端没有"):
            if rows.get(key):
                sample = ", ".join(rows[key][:5])
                print(f"     {key:<22} {len(rows[key]):>4}   例: {sample}")
        report[day] = {k: len(v) for k, v in rows.items() if v}
    return report


# ── 4. diff 建议 ────────────────────────────────────────────────────────────
def planned_page_text() -> tuple[str, str]:
    """在内存里算出"退役后"的页面文本（不写盘）。"""
    text = PAGE.read_text(encoding="utf-8")
    orig = text

    # (a) 去掉 script 标签及其注释块
    text = re.sub(
        r"\n[ \t]*<!--[^>]*?activity-buckets\.js[\s\S]*?-->\n\s*<script src=\"/activity-buckets\.js\"></script>\n",
        "\n", text)
    text = text.replace('\n<script src="/activity-buckets.js"></script>\n', "\n")

    # (b) 去掉查表三件套（bucketTableFor / bucketSetsFor / manualActivityBucket）
    for fname in ("bucketTableFor", "bucketSetsFor", "manualActivityBucket"):
        m = re.search(r"\n[ \t]*(?:var _BUCKET_SET_CACHE = \{\};\n[ \t]*)?function " + fname + r"\(", text)
        if not m:
            continue
        start = m.start()
        if "_BUCKET_SET_CACHE" in text[max(0, start - 80):start]:
            start = text.rindex("var _BUCKET_SET_CACHE", 0, start)
            start = text.rindex("\n", 0, start)
        i = text.index("{", m.start() + len(m.group(0)))
        depth, j = 1, i + 1
        while j < len(text) and depth:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        text = text[:start] + text[j:]

    # (c) activityBucket 里的 manual 分支
    text = re.sub(
        r"\n[ \t]*var manual = manualActivityBucket\(id, dateKey\);\n[ \t]*if \(manual\) return \{ bucket: manual, source: \"manual\" \};",
        "", text)

    # (d) __isMediaEvent 里的 manual 调用
    text = re.sub(
        r"\n[ \t]*if \(aid && manualActivityBucket\(aid, \(state && state\.date\) \|\| \"\"\) === \"media\"\) return true;",
        "", text)

    # (e) classify() 的 manual 回退
    text = re.sub(
        r"\n[ \t]*// 统计尚未就绪[^\n]*\n[ \t]*var manual = manualActivityBucket\(id, \(state && state\.date\) \|\| \"\"\);\n[ \t]*if \(manual\) return manual;\n",
        "\n    // [dry-run] 表退役后：统计未就绪时宁可**不折**（返回 live），也不按旧表折。\n", text)
    return orig, text


def section_diff() -> str:
    print()
    print("═" * 78)
    print("④ diff 建议（只在内存里生成，**没有写盘**）")
    print("═" * 78)
    if not PAGE.is_file():
        print("  ⚠️ 找不到页面文件，跳过")
        return ""
    orig, planned = planned_page_text()
    diff = list(difflib.unified_diff(
        orig.splitlines(keepends=True), planned.splitlines(keepends=True),
        fromfile="web/daily-timeline-live.html (现状)",
        tofile="web/daily-timeline-live.html (退役后计划)",
        n=2))
    if not diff:
        print("  （没有可生成的差异 —— 页面已经不含 dated 表读取点）")
        return ""
    head = 200
    for line in diff[:head]:
        print("  " + line.rstrip("\n"))
    if len(diff) > head:
        print(f"  … 还有 {len(diff) - head} 行（完整 diff 未输出，避免刷屏）")
    print(f"\n  计划改动：{sum(1 for l in diff if l.startswith('-') and not l.startswith('---'))} 行删除 / "
          f"{sum(1 for l in diff if l.startswith('+') and not l.startswith('+++'))} 行新增")
    return "".join(diff)


def section_replacements() -> None:
    print()
    print("═" * 78)
    print("⑤ 每个断点的替代读法")
    print("═" * 78)
    for key, value in REPLACEMENTS.items():
        print(f"  · {key}")
        print(f"      → {value}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Q837 方案 B 退役 dated 表的 dry-run（只打印，不写盘）")
    ap.add_argument("--json", action="store_true", help="输出机器可读结果")
    ap.add_argument("--max-days", type=int, default=8, help="影响量化最多算几天（默认 8）")
    args = ap.parse_args(argv)

    watched = [PAGE, AGGREGATE] + dated_files()
    before = sha_tree([p for p in watched if p.is_file()])

    inventory = section_inventory()
    readers = section_readers()
    impact = section_impact(max(1, args.max_days))
    diff_text = section_diff()
    section_replacements()

    after = sha_tree([p for p in watched if p.is_file()])
    print()
    print("═" * 78)
    print("⑥ 安全性自证")
    print("═" * 78)
    print(f"  受监视文件 {len([p for p in watched if p.is_file()])} 个，sha256(汇总) 跑前={before} 跑后={after}")
    print(f"  结果：{'✅ 未改动任何文件（dry-run 成立）' if before == after else '❌ 有文件被改动！'}")
    print("  本单未执行任何退役动作：未删表、未改页面、未改 feedback、未改历史数据、未重启。")

    if args.json:
        print()
        print(json.dumps({
            "inventory": inventory,
            "readers": readers,
            "impact": impact,
            "diff_lines": diff_text.count("\n"),
            "integrity": {"before": before, "after": after, "unchanged": before == after},
        }, ensure_ascii=False, indent=1))
    return 0 if before == after else 1


if __name__ == "__main__":
    sys.exit(main())
