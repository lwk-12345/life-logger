#!/usr/bin/env python3
"""每日体验验收：确认生活日志是「干净可用」的。

检查四件事，输出一份 Markdown 报告到 output/local-web/acceptance-<date>.md：

  1. 三个近期日期的简报事件数（要能看到内容，不能是 0）
  2. 同一日期内是否有重复事件标题（重复说明多个任务在处理同一段音频）
  3. events.json 里是否残留 dictation 原文（voice coding 泄漏回生活事件）
  4. 是否有事件落在 undated/（日期判定失败，进不了当天简报）

用法：
    python3 scripts/verify_daily_experience.py            # 检查最近 3 天
    python3 scripts/verify_daily_experience.py --date 2026-09-29 --date ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "output" / "local-web"
JOBS_DIR = WEB_ROOT / "jobs"
SECRETS = WEB_ROOT / "secrets.json"
DEFAULT_BASE_URL = os.environ.get("LIFELOGGER_API_BASE", "http://127.0.0.1:8765")

# dictation（语音输入）的特征词。命中说明 voice coding 泄漏进了生活事件。
DICTATION_MARKERS = (
    "多代理并行",
    "不需要wifi",
    "不需要 wifi",
    "为什么不自动接入",
    "代理是工作流",
    "voice coding",
    "看一下这个项目",
    "这个项目我还需要留着吗",
)


def resolve_token() -> str:
    try:
        payload = json.loads(SECRETS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"读不到访问令牌: {error}")
    token = payload.get("mobile_access_token")
    if not token:
        raise SystemExit(f"{SECRETS} 里没有 mobile_access_token")
    return str(token)


def fetch_brief(base_url: str, token: str, day: str) -> dict[str, Any] | None:
    """取某天的简报（带重试与本地 build_daily_brief 直读兜底）。"""
    import http.client
    import time

    url = f"{base_url.rstrip('/')}/api/daily-brief?date={day}"
    for attempt in range(3):
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError:
            return None
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            http.client.HTTPException,
            ConnectionError,
            OSError,
        ):
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
    try:
        if str(ROOT / "src") not in sys.path:
            sys.path.insert(0, str(ROOT / "src"))
        from life_logger.daily_brief import build_daily_brief
        return build_daily_brief(WEB_ROOT, date=day)
    except Exception:
        return None


def collect_events() -> list[dict[str, Any]]:
    """扫描所有任务的 events.json，返回 (日期, 标题, 任务) 列表。"""
    rows: list[dict[str, Any]] = []
    for status_path in sorted(JOBS_DIR.glob("job-*/status.json")):
        try:
            state = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        job_id = str(state.get("job_id") or status_path.parent.name)
        for item in state.get("files") or []:
            file_id = str(item.get("file_id") or "")
            for events_path in (
                status_path.parent / "files" / file_id / "result"
            ).glob("*/events.json"):
                day = events_path.parent.name
                try:
                    payload = json.loads(events_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                events = payload.get("events") if isinstance(payload, dict) else payload
                for event in events or []:
                    rows.append(
                        {
                            "day": day,
                            "title": str(event.get("title") or ""),
                            "categories": event.get("categories") or [],
                            "job_id": job_id,
                        }
                    )
    return rows


def collect_job_health(jobs_dir: Path) -> dict[str, Any]:
    """审计所有任务：找出「自称完成、但子步骤失败/产物缺失」的假成功。

    回复用同一套判据（life_logger.local_web），所以这里看到的判决就是线上状态
    会怎么写。每日验收不看这一项时，26 个假完成也能全绿。
    """
    import sys

    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from life_logger.local_web import apply_file_outcomes

    false_success: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    scanned = 0
    for status_path in sorted(jobs_dir.glob("job-*/status.json")):
        try:
            state = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        scanned += 1
        claimed = state.get("status")
        if claimed not in {"completed", "degraded"}:
            continue
        verdict = json.loads(json.dumps(state, ensure_ascii=False))
        apply_file_outcomes(verdict, job_dir=status_path.parent)
        errors = verdict.get("stage_errors") or []
        unresolved = [item for item in errors if not item.get("resolved")]
        if unresolved:
            false_success.append(
                {
                    "job_id": str(state.get("job_id") or status_path.parent.name),
                    "claimed_status": claimed,
                    "verdict": verdict.get("status"),
                    "still_claims_completed": claimed == "completed",
                    "reasons": [
                        f"[{item.get('severity')}] {item.get('stage')} / "
                        f"{item.get('kind')}：{item.get('error')}"
                        for item in unresolved
                    ],
                }
            )
        elif errors:
            recovered.append(
                {
                    "job_id": str(state.get("job_id") or status_path.parent.name),
                    "reasons": [str(item.get("kind")) for item in errors],
                }
            )
    return {
        "scanned": scanned,
        "false_success": false_success,
        "recovered_history": recovered,
    }


def build_report(days: list[str], base_url: str, token: str) -> tuple[str, bool]:
    lines: list[str] = []
    ok = True
    lines.append(f"# 每日体验验收 · {datetime.now().astimezone():%Y-%m-%d %H:%M}")
    lines.append("")

    rows = collect_events()

    lines.append("## 0. 任务健康（假成功）")
    lines.append("")
    health = collect_job_health(JOBS_DIR)
    lying = [
        item for item in health["false_success"] if item["still_claims_completed"]
    ]
    already_marked = [
        item for item in health["false_success"] if not item["still_claims_completed"]
    ]
    if lying:
        # 只把「致命」当硬失败：产物缺失/阶段失败未解决。自称完成却做不到，
        # 后面几项再绿也不能说这天验收通过。
        fatal = [
            item
            for item in lying
            if any(reason.startswith("[fatal]") for reason in item["reasons"])
        ]
        lines.append(
            f"❌ {len(lying)} 个任务自称完成但仍有未解决的子步骤失败"
            f"（其中致命 {len(fatal)} 个）："
        )
        for item in lying[:10]:
            lines.append(
                f"  - {item['job_id']}（status={item['claimed_status']} → "
                f"应为 {item['verdict']}）"
            )
            for reason in item["reasons"][:3]:
                lines.append(f"    - {reason}")
            if len(item["reasons"]) > 3:
                lines.append(f"    - …还有 {len(item['reasons']) - 3} 条")
        if fatal:
            ok = False
    else:
        lines.append("✅ 没有任务在冒充干净完成")
    if already_marked:
        lines.append(
            f"ℹ️ {len(already_marked)} 个任务已如实标为 degraded / 降级"
            "（产物可用、过程有未解决的子步骤失败）："
        )
        for item in already_marked[:10]:
            reason = item["reasons"][0] if item["reasons"] else ""
            lines.append(f"  - {item['job_id']}（{item['claimed_status']}）：{reason}")
    if health["recovered_history"]:
        lines.append(
            f"⚠️ 另有 {len(health['recovered_history'])} 个任务曾失败后重跑成功"
            "（结果可用，记录已保留）"
        )
    lines.append("")

    lines.append("## 1. 简报事件数")
    lines.append("")
    lines.append("| 日期 | 简报事件数 | 磁盘事件数 | 判定 |")
    lines.append("|---|---|---|---|")
    for day in days:
        brief = fetch_brief(base_url, token, day)
        brief_count = brief.get("event_count") if brief else None
        disk_count = sum(1 for row in rows if row["day"] == day)
        if brief_count is None and disk_count == 0:
            # 接口对无数据的日期回 4xx，这是「这天没有录音」而不是故障。
            verdict = "— 该日无录音"
        elif brief_count is None:
            verdict = "❌ 磁盘有事件但简报读不到（日期目录不匹配）"
            ok = False
        elif brief_count == 0 and disk_count == 0:
            verdict = "— 该日无录音"
        elif brief_count == 0 and disk_count > 0:
            verdict = "❌ 磁盘有事件但简报读不到（日期目录不匹配）"
            ok = False
        else:
            verdict = "✅"
        lines.append(f"| {day} | {brief_count} | {disk_count} | {verdict} |")
    lines.append("")

    lines.append("## 2. 重复事件")
    lines.append("")
    duplicates = []
    for day in days:
        titles = Counter(row["title"] for row in rows if row["day"] == day and row["title"])
        dupes = {t: n for t, n in titles.items() if n > 1}
        if dupes:
            duplicates.append((day, dupes))
    if duplicates:
        ok = False
        for day, dupes in duplicates:
            lines.append(f"- ❌ **{day}** 有 {len(dupes)} 个标题重复：")
            for title, count in list(dupes.items())[:5]:
                lines.append(f"  - ×{count} 「{title[:60]}」")
    else:
        lines.append("✅ 没有重复标题")
    lines.append("")

    lines.append("## 3. dictation（语音输入）是否泄漏进生活事件")
    lines.append("")
    leaked = [
        row
        for row in rows
        if row["day"] in days
        and any(marker in row["title"] for marker in DICTATION_MARKERS)
    ]
    if leaked:
        ok = False
        lines.append(f"❌ 发现 {len(leaked)} 条 dictation 混在生活事件里：")
        for row in leaked[:8]:
            lines.append(f"  - [{row['day']}] 「{row['title'][:60]}」")
    else:
        lines.append("✅ 没有发现 dictation 泄漏")
    lines.append("")

    lines.append("## 4. undated 归类")
    lines.append("")
    undated = [row for row in rows if row["day"] == "undated"]
    if undated:
        lines.append(f"⚠️ 有 {len(undated)} 条事件落在 `undated/`，不会进任何一天简报：")
        for row in undated[:8]:
            lines.append(f"  - 「{row['title'][:60]}」")
        lines.append("")
        lines.append("（这些是历史产物；新任务已改为用片段绝对时间定日期）")
    else:
        lines.append("✅ 没有 undated 事件")
    lines.append("")

    lines.append("## 结论")
    lines.append("")
    lines.append("✅ **体验正常**" if ok else "❌ **存在问题，见上方各项**")
    lines.append("")
    return "\n".join(lines), ok


def default_today() -> date:
    """默认考察日：**北京时间今天**。

    [2026-11-04 UTC+8] 原来是 `date.today()`，跟随系统时区；UTC-8 的傍晚之后
    北京已是次日，考察窗口会整体错一天。
    """
    return datetime.now(timezone(timedelta(hours=8))).date()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每日体验验收")
    parser.add_argument("--date", action="append", default=None, help="要检查的日期，可重复")
    parser.add_argument("--days", type=int, default=3, help="缺省检查最近几天")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.date:
        days = sorted(str(value) for value in args.date)
    else:
        today = default_today()   # [2026-11-04 UTC+8] 默认考察日按北京时间，不跟随系统时区
        days = [
            (today - timedelta(days=offset)).isoformat()
            for offset in range(1, args.days + 1)
        ][::-1]

    token = resolve_token()
    report, ok = build_report(days, args.base_url, token)
    output = args.output or WEB_ROOT / f"acceptance-{date.today().isoformat()}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n报告已写入：{output}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
