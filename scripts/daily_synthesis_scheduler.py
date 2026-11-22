#!/usr/bin/env python3
"""日终自动日报调度器（2026-10-19 P-3）。

职责：每天 **次日 01:30 为「前一天」生成日报**（daily synthesis），并满足四条工程要求：

> 为什么是次日 01:30 而不是当天 23:55：23:55 之后仍可能有录音写入，
> 当天跑会漏掉当晚最后几分钟。改为次日凌晨跑前一天，数据才完整。
> （2026-10-19 A0 批准变更）
1. **幂等**：`output/local-web/daily-synthesis/synthesis-<date>.json` 已存在就跳过，
   除非显式 `--force`。绝不重复烧模型。
2. **可观测**：每次运行向 `output/local-web/daily-scheduler.log` 追加一行 JSON，
   含 timestamp / date / result / elapsed_seconds / model_calls / tokens / reason。
3. **失败重试**：最多重试 2 次（共 3 次尝试）；仍失败写 `FAILED` + 原因，不静默。
4. **可演练**：`--dry-run` 只判断「该不该跑、为什么」，不调模型、不写产物。
5. **回补兜底**：`--backfill-days N`（上限 3）。主目标失败/跳过时，向回逐日补跑
   缺失的日报，每天独立重试，逐条写日志。默认 0（不补），由 launchd 传 3。
6. **可比对**：`--out-dir` 把产物写到别处，用于「先比对、后决定是否覆盖」。

用法：
    python3 scripts/daily_synthesis_scheduler.py --dry-run
    python3 scripts/daily_synthesis_scheduler.py                    # 默认跑「昨天」
    python3 scripts/daily_synthesis_scheduler.py --date 2026-10-19
    python3 scripts/daily_synthesis_scheduler.py --date 2026-10-19 --force
    python3 scripts/daily_synthesis_scheduler.py --backfill-days 3
    python3 scripts/daily_synthesis_scheduler.py --date 2026-10-19 --force --out-dir /tmp/verify

设计取舍：
- **不写 web/**，只碰 scripts/ 与 output/ 下的调度日志；
- **不重启服务**；构造 LocalWebServer 时把后台线程全部置空，保证只读；
- 事件集合与 `/api/daily` 同源（复用 `rerun_daily_synthesis._events_for`），
  否则日报条数又会和接口对不上。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import rerun_daily_synthesis as rr  # noqa: E402  复用同源事件集合 + usage 计数
from life_logger import day_facts  # noqa: E402

LOG_PATH = ROOT / "output" / "local-web" / "daily-scheduler.log"
SYNTH_DIR = ROOT / "output" / "local-web" / "daily-synthesis"
SHANGHAI = timezone(timedelta(hours=8))

MAX_RETRIES = 2          # 重试 2 次 → 总共最多 3 次尝试
RETRY_BACKOFF_SECONDS = 20


def _today_shanghai() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d")


def _yesterday_shanghai() -> str:
    return (datetime.now(SHANGHAI) - timedelta(days=1)).strftime("%Y-%m-%d")


# 生成目标目录；--out-dir 会覆盖它（用于「先比对、后决定是否覆盖」）
OUT_DIR: Path = SYNTH_DIR


def _synthesis_path(date: str) -> Path:
    return OUT_DIR / f"synthesis-{date}.json"


def _write_log(record: dict) -> None:
    """结构化一行 JSON 追加写；失败也不能影响主流程。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _decide(date: str, force: bool, events: list[dict], activities: list[dict] | None = None) -> tuple[str, str]:
    """返回 (decision, reason)。decision ∈ {skip-exists, skip-empty, run}

    [2026-10-29] 原来只判 `path.is_file()`：产物存在就 skip，完全不看事件是否被改过。
    用户纠正事件后，调度器仍跳过 → 日报长期停在旧摘要。现在与
    `generate_daily_cognitive_synthesis` 用**同一套事件指纹**判断：
    - 指纹一致 → skip-exists（真的没变化）
    - 指纹不一致 / 旧格式无指纹 → run（理由可读，便于日志排查）
    """
    path = _synthesis_path(date)
    if path.is_file() and not force:
        current_fp = rr.dcs.events_fingerprint(events)
        cached_fp = None
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                cached_fp = cached.get(rr.dcs.EVENTS_FINGERPRINT_FIELD)
        except (OSError, ValueError):
            return "run", f"{path.name} 无法解析，按未命中重建"
        if isinstance(cached_fp, str) and cached_fp == current_fp:
            return "skip-exists", f"{path.name} 已存在且事件未变化"
        if cached_fp is None:
            return "run", f"{path.name} 为旧格式（无事件指纹），需重建"
        return "run", f"{path.name} 事件已变更，需重建"
    if not events and not activities:
        return "skip-empty", "当天没有事件，不生成日报"
    return "run", "需要生成"


def _run_day(date: str, *, force: bool, dry_run: bool, max_retries: int,
             stage: str = "primary", with_activities: bool = False) -> tuple[int, dict]:
    """跑单日。返回 (returncode, record)。dry_run 不写日志、不调模型。"""
    started = time.monotonic()
    base_record: dict = {
        "timestamp": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "date": date,
        "dry_run": bool(dry_run),
        "force": bool(force),
        "stage": stage,
        "out_dir": str(OUT_DIR),
    }

    # ---- 1) 读事件（与 /api/daily 同源），判断该不该跑 ----
    try:
        app = rr._build_app()
        events, ledger = rr._events_for(app, date)
        # 补日报：连活动一起读，金额按网页账本口径核对。
        activities = None
        if with_activities:
            activities = day_facts.activities_for_day(date) or []
            ledger = day_facts.day_expense_ledger(events, date)
    except Exception as exc:  # 读不到事件也要留痕，不能静默
        record = {**base_record, "result": "FAILED", "stage": f"{stage}:load-events",
                  "elapsed_seconds": round(time.monotonic() - started, 2),
                  "reason": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc(limit=3)}
        _write_log(record)
        return 1, record

    ledger_total = ledger.get("total_amount")
    decision, reason = _decide(date, force, events, activities)

    if dry_run:
        existing = _synthesis_path(date)
        old_amount = None
        if existing.is_file():
            try:
                old_amount = (json.loads(existing.read_text(encoding="utf-8"))
                              .get("expense_analysis") or {}).get("total_amount_cny")
            except Exception:
                old_amount = None
        record = {**base_record, "result": f"DRY-RUN:{decision}", "reason": reason,
                  "events": len(events), "ledger_total": ledger_total,
                  "existing_file": existing.name if existing.is_file() else None,
                  "existing_amount": old_amount,
                  "would_call_model": decision == "run",
                  "elapsed_seconds": round(time.monotonic() - started, 2)}
        return 0, record

    if decision != "run":
        record = {**base_record, "result": decision.upper().replace("SKIP-", "SKIP:"),
                  "reason": reason, "events": len(events), "ledger_total": ledger_total,
                  "model_calls": 0, "elapsed_seconds": round(time.monotonic() - started, 2)}
        _write_log(record)
        return 0, record

    # ---- 2) 真正生成，带重试 ----
    counter = rr.UsageCounter()
    original = rr.dcs.urllib.request.urlopen
    rr.dcs.urllib.request.urlopen = counter  # type: ignore[assignment]
    last_error: str | None = None
    synthesis: dict | None = None
    attempts = 0
    try:
        for attempt in range(max_retries + 1):
            attempts = attempt + 1
            try:
                synthesis = rr.dcs.generate_daily_cognitive_synthesis(
                    date, events, force=True, cache_dir=OUT_DIR, activities=activities
                )
                last_error = None
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < max_retries:
                    time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    finally:
        rr.dcs.urllib.request.urlopen = original  # type: ignore[assignment]

    elapsed = round(time.monotonic() - started, 2)
    if synthesis is None:
        record = {**base_record, "result": "FAILED", "attempts": attempts,
                  "reason": last_error or "unknown",
                  # 【2026-10-19 P-14】失败也要留 tier：三级链路全挂时这里是 none
                  "model_tier": "none",
                  "events": len(events), "ledger_total": ledger_total,
                  "model_calls": counter.calls, "tokens": counter.total_tokens,
                  "elapsed_seconds": elapsed}
        _write_log(record)
        return 1, record

    if synthesis.get("pending_synthesis") and _synthesis_path(date).is_file():
        record = {**base_record, "result": "PAUSED", "attempts": attempts,
                  "reason": "MODEL_TIERS_FAILED: 日报生成已暂停待补",
                  "model_tier": synthesis.get("model_tier"),
                  "pending_file": synthesis.get("pending_file"),
                  "notify_status": synthesis.get("notify_status"),
                  "events": len(events), "ledger_total": ledger_total,
                  "model_calls": counter.calls, "tokens": counter.total_tokens,
                  "elapsed_seconds": elapsed}
        _write_log(record)
        return 2, record

    if synthesis.get("persist_error"):
        record = {**base_record, "result": "FAILED", "attempts": attempts,
                  "reason": "PERSIST_ERROR: 日报写入失败",
                  "persist_error": synthesis.get("persist_error"),
                  "model_tier": synthesis.get("model_tier"),
                  "events": len(events), "ledger_total": ledger_total,
                  "model_calls": counter.calls, "tokens": counter.total_tokens,
                  "elapsed_seconds": elapsed}
        _write_log(record)
        return 1, record

    if synthesis.get("preserved_existing"):
        record = {**base_record, "result": "FAILED", "attempts": attempts,
                  "reason": "PRESERVED_EXISTING: 本次生成失败，保留已有日报",
                  "model_tier": synthesis.get("model_tier"),
                  "events": len(events), "ledger_total": ledger_total,
                  "model_calls": counter.calls, "tokens": counter.total_tokens,
                  "elapsed_seconds": elapsed}
        _write_log(record)
        return 1, record

    # 【2026-10-22 P-23】硬校验：**返回非 None ≠ 真的产出文件**。
    # 事故现场：2026-10-20 走了模型降级 fallback，函数返回了字典但没写盘，
    # 调度器只看 `synthesis is None` 就记 `OK`，于是"日志说成功、磁盘上没有文件"。
    # 现在：只要目标文件不存在，就一律 FAILED（并带上 enough 信息让 --backfill 重跑）。
    target_file = _synthesis_path(date)

    # 【2026-10-22】模型三级全挂 → 生成端会"暂停"（不落完整日报、只落 pending 标记）。
    # 这里必须区分两种失败：PAUSED（等模型恢复后补生成）≠ NOT_PERSISTED（代码 bug）。
    if not target_file.is_file():
        try:
            from life_logger.synthesis_pause import read_pending

            marker = read_pending(OUT_DIR.parent, date)
        except Exception:
            marker = None
        if marker:
            record = {**base_record, "result": "PAUSED", "attempts": attempts,
                      "reason": "MODEL_TIERS_FAILED: 三级模型全挂，日报已暂停待补生成",
                      "model_tier": "none",
                      "pending_file": f"pending-{date}.json",
                      "pending_attempts": marker.get("attempts"),
                      "pending_errors": marker.get("errors"),
                      "notify_status": (marker.get("notify") or {}).get("status"),
                      "events": len(events), "ledger_total": ledger_total,
                      "model_calls": counter.calls, "tokens": counter.total_tokens,
                      "elapsed_seconds": elapsed}
            _write_log(record)
            print(f"[scheduler] {date} 模型三级全挂 → 已暂停（pending-{date}.json），"
                  f"通知状态={record['notify_status']}", flush=True)
            return 2, record

    if not target_file.is_file():
        record = {**base_record, "result": "FAILED", "attempts": attempts,
                  "reason": "NOT_PERSISTED: 函数返回了结果但没有落盘文件 "
                            f"({target_file.name})",
                  "model_tier": synthesis.get("model_tier"),
                  "fallback_reason": synthesis.get("fallback_reason"),
                  "persist_error": synthesis.get("persist_error"),
                  "events": len(events), "ledger_total": ledger_total,
                  "model_calls": counter.calls, "tokens": counter.total_tokens,
                  "elapsed_seconds": elapsed}
        _write_log(record)
        return 1, record

    file_amount = (synthesis.get("expense_analysis") or {}).get("total_amount_cny")
    aligned = abs(float(file_amount or 0) - float(ledger_total or 0)) < 0.005
    record = {**base_record, "result": "OK", "attempts": attempts,
              # 【2026-10-19 P-14】降级可见：日报实际用了哪一档模型、为什么降级
              "model_tier": synthesis.get("model_tier"),
              "fallback_reason": synthesis.get("fallback_reason"),
              "events": len(events), "ledger_total": ledger_total,
              "file_amount": file_amount, "aligned_with_ledger": aligned,
              "fallback": bool(synthesis.get("fallback")),
              "model_calls": counter.calls, "tokens": counter.total_tokens,
              "prompt_tokens": counter.prompt_tokens,
              "completion_tokens": counter.completion_tokens,
              "elapsed_seconds": elapsed}
    # 【2026-10-22 P-23】最后一道自我保护：写日志前再断言一次
    # 「result == OK ⇒ 文件存在」，不满足就降级为 FAILED（绝不让假 OK 流出）。
    if record.get("result") == "OK" and not _synthesis_path(date).is_file():
        record["result"] = "FAILED"
        record["reason"] = "SELF_CHECK_FAILED: OK 但目标文件不存在，已降级为 FAILED"
        record["model_tier"] = record.get("model_tier") or "unknown"
        _write_log(record)
        return 1, record
    _write_log(record)
    return (0 if aligned else 3), record


def _run_shadow(date: str) -> None:
    """夜间顺手累积影子对比（docs/adr/0005 P2）。不调模型，失败不影响日报。"""
    out = ROOT / "work" / "p2-shadow-20261104" / date
    try:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "shadow_facts.py"), "--from", date, "--to", date,
                        "--out", str(out)], check=True, capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        _write_log({"timestamp": datetime.now(SHANGHAI).isoformat(timespec="seconds"), "date": date,
                    "stage": "shadow", "result": "FAILED", "reason": f"{type(exc).__name__}: {exc}"})


def main(argv: list[str]) -> int:
    global OUT_DIR
    parser = argparse.ArgumentParser(description="日终自动日报调度器（次日 01:30 跑前一天）")
    parser.add_argument("--date", default=None,
                        help="目标日期 YYYY-MM-DD；默认「昨天」（因为调度在次日 01:30 运行）")
    parser.add_argument("--today", action="store_true", help="显式指定跑今天（覆盖默认的昨天）")
    parser.add_argument("--force", action="store_true", help="已存在也重跑（会烧模型）")
    parser.add_argument("--dry-run", action="store_true", help="只判断该不该跑，不调模型不写产物")
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES)
    parser.add_argument("--backfill-days", type=int, default=0,
                        help="主目标失败/跳过时，向回最多回补 N 天（上限 3）。默认 0。")
    parser.add_argument("--out-dir", default=None,
                        help="产物写到别处（用于「先比对、后决定是否覆盖」），默认正式目录")
    parser.add_argument("--events-only", action="store_true",
                        help="只读事件，不读活动时间线（默认两者都读，金额按网页账本核对）")
    args = parser.parse_args(argv)

    backfill = max(0, min(3, int(args.backfill_days or 0)))
    if args.out_dir:
        OUT_DIR = Path(args.out_dir).expanduser().resolve()
        OUT_DIR.mkdir(parents=True, exist_ok=True)

    date = args.date or (_today_shanghai() if args.today else _yesterday_shanghai())

    # ---- 主目标 ----
    rc, record = _run_day(date, force=args.force, dry_run=args.dry_run,
                          max_retries=args.max_retries, stage="primary",
                          with_activities=not args.events_only)
    print(json.dumps(record, ensure_ascii=False, indent=2))

    # ---- 回补兜底：主目标没拿到 OK 才触发，最多 3 天，逐年独立 ----
    backfilled: list[dict] = []
    # 只有「真的失败」才回补。SKIP:EXISTS 代表当天已成功，不该触发回补——
    # 否则会无谓地给更早的历史日期补出新文件（2026-10-19 实测踩过：生成了 10-15）。
    if not args.dry_run and backfill > 0 and record.get("result") == "FAILED":
        base = datetime.strptime(date, "%Y-%m-%d")
        for offset in range(1, backfill + 1):
            prev = (base - timedelta(days=offset)).strftime("%Y-%m-%d")
            if _synthesis_path(prev).is_file():
                backfilled.append({"date": prev, "result": "SKIP:EXISTS"})
                continue
            brc, brec = _run_day(prev, force=False, dry_run=False, with_activities=not args.events_only,
                                 max_retries=args.max_retries,
                                 stage=f"backfill-{offset}")
            backfilled.append({"date": prev, "result": brec.get("result"),
                               "file_amount": brec.get("file_amount"),
                               "model_calls": brec.get("model_calls")})
            if brec.get("result") == "OK":
                break  # 补上一天就够，不一次补一大批
        if backfilled:
            summary = {"timestamp": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
                       "date": date, "stage": "backfill-summary",
                       "requested_days": backfill, "attempts": backfilled}
            _write_log(summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not args.dry_run and not args.out_dir and "PYTEST_CURRENT_TEST" not in os.environ:
        _run_shadow(date)
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
