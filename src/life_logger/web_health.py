"""从 local_web.py 机械搬出的 web_health 部分，行为不变；分组依据见 work/split-prep-localweb-20261104/。"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import Mapping
from typing import Sequence
import json
from life_logger.web_common import DEGRADABLE_SEVERITY, FATAL_SEVERITY, _ERROR_LINE_PATTERN, _TRACEBACK_MARKER, _missing_stage_artifact, _stage_error, _stage_log_text, _success_evidence_marker, stage_severity


def build_daily_health(root: Path, days: int = 14) -> dict[str, Any]:
    """【2026-10-19 P-14】日报健康度：让「哪天缺日报 / 哪天降级」一眼可见。

    设计要点：
    1. **不重新生成、不调模型**，只读已有产物与调度日志，所以可以随时调；
    2. `has_synthesis=False` 的日期明确标出，这是原来完全看不见的信号；
    3. `aligned_with_ledger` 用 **synthesis 文件自带的 expense_ledger** 比对，
       不重算账本，保证这个端点足够轻（14 天也能毫秒级返回）；
    4. `last_attempt` 取自 `output/local-web/daily-scheduler.log`（JSONL），
       记录调度器最后一次对该日期尝试的结果，失败原因一并带出。
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZoneInfo

    days = max(1, min(90, int(days or 14)))
    tz = _ZoneInfo("Asia/Shanghai")
    today = _dt.now(tz=tz).date()

    synth_dir = root / "daily-synthesis"
    log_path = root / "daily-scheduler.log"

    # 调度日志：每个日期只保留最后一次尝试
    last_attempt: dict[str, dict[str, Any]] = {}
    if log_path.is_file():
        try:
            for raw in log_path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                d = row.get("date")
                if not d or row.get("stage") == "backfill-summary":
                    continue
                last_attempt[str(d)] = {
                    "timestamp": row.get("timestamp"),
                    "result": row.get("result"),
                    "reason": row.get("reason"),
                    "model_calls": row.get("model_calls"),
                    "stage": row.get("stage"),
                }
        except OSError:
            last_attempt = {}

    entries: list[dict[str, Any]] = []
    missing: list[str] = []
    degraded: list[str] = []
    pending_days: list[str] = []
    no_data_days: list[str] = []
    for offset in range(days):
        date_str = (today - __import__("datetime").timedelta(days=offset)).isoformat()
        synth_file = synth_dir / f"synthesis-{date_str}.json"
        has = synth_file.is_file()
        tier = None
        reason = None
        aligned = None
        event_count = None
        if has:
            try:
                data = json.loads(synth_file.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            tier = data.get("model_tier")
            reason = data.get("fallback_reason")
            event_count = data.get("event_count")
            amount = (data.get("expense_analysis") or {}).get("total_amount_cny")
            ledger_amount = (data.get("expense_ledger") or {}).get("total_amount")
            if amount is not None and ledger_amount is not None:
                try:
                    aligned = abs(float(amount) - float(ledger_amount)) < 0.005
                except (TypeError, ValueError):
                    aligned = None
            # 老产物没有 model_tier 字段：标成 unknown，不要假装是 primary
            if tier is None:
                tier = "unknown"
        if tier in {"tier2", "tier3", "local-fallback"}:
            degraded.append(date_str)

        # 无 synthesis 时不能只凭「文件不存在」判缺失：0 事件的一天本来就不需要日报。
        # 只在缺失日读事件视图，避免把正常生成日也全量重算。
        event_facts_error = None
        report_required = True
        ledger_total = None
        if has:
            ledger_total = ledger_amount
        else:
            try:
                from life_logger.event_view import build_event_view
                from life_logger.day_facts import day_expense_ledger

                view = build_event_view(
                    root, date_from=date_str, date_to=date_str,
                    scope="provisional", view="display",
                )
                view_events = list(view.get("events") or [])
                event_count = len(view_events)
                # [2026-11-04 Q809 补位] 事件轨 0 条时不能直接归零：活动轨里可能记着消费
                # （11-01 实测：事件 0 条，活动轨「十块烟 + 六块九水」= 16.9）。
                ledger_total = float(
                    day_expense_ledger(view_events, date_str, root=root).get("total_amount") or 0.0
                )
                if event_count == 0 and abs(ledger_total) < 0.005:
                    no_data_days.append(date_str)
                    report_required = False
            except Exception as error:
                event_facts_error = f"{type(error).__name__}: {error}"

        # 【2026-10-22】模型三级全挂 → 生成端会「暂停」并落 pending-<date>.json。
        # 这种日子必须显式显示「日报未生成（模型失败）」，不能被当成普通缺失或正常。
        pending_marker = None
        try:
            from life_logger.synthesis_pause import read_pending

            pending_marker = read_pending(root, date_str)
        except Exception:
            pending_marker = None
        if pending_marker:
            pending_days.append(date_str)
            report_required = True
            status = "paused_model_failure"
            status_text = "日报未生成（模型失败，已暂停待补）"
        elif not has and not report_required:
            status = "no_data"
            status_text = "当天无事件，无需生成日报"
        elif has:
            status = "ok" if (aligned is not False) else "amount_mismatch"
            status_text = "日报已生成" if aligned is not False else "日报金额与账本不一致"
        else:
            status = "missing"
            status_text = "该日无日报"
            if date_str not in no_data_days:
                missing.append(date_str)
        entries.append({
            "date": date_str,
            "has_synthesis": has,
            "model_tier": tier,
            "fallback_reason": reason,
            "aligned_with_ledger": aligned,
            "event_count": event_count,
            "ledger_total": ledger_total,
            "report_required": report_required,
            "event_facts_error": event_facts_error,
            "last_attempt": last_attempt.get(date_str),
            "status": status,
            "status_text": status_text,
            "pending_synthesis": bool(pending_marker),
            "pending_reason": (pending_marker or {}).get("reason"),
            "pending_attempts": (pending_marker or {}).get("attempts"),
            "pending_errors": (pending_marker or {}).get("errors"),
            "notify_status": ((pending_marker or {}).get("notify") or {}).get("status"),
        })

    return {
        "days": days,
        "date_from": entries[-1]["date"] if entries else None,
        "date_to": entries[0]["date"] if entries else None,
        "generated_at": _dt.now(tz=tz).isoformat(),
        "missing_count": len(missing),
        "degraded_count": len(degraded),
        "missing_dates": sorted(missing),
        "degraded_dates": sorted(degraded),
        # 【2026-10-22】模型失败的日期单独成类：既不是"正常"也不是"只是没生成"
        "no_data_count": len(no_data_days),
        "no_data_dates": sorted(no_data_days),
        "no_data_hint": "当天 0 事件、账本 0 元、无日报；这是无需日报，不是缺失或失败",
        "pending_count": len(pending_days),
        "pending_dates": sorted(pending_days),
        "pending_hint": "模型三级全挂 → 日报已暂停，等模型恢复后补生成"
                        "（python3 scripts/rerun_daily_synthesis.py --date <日期>）",
        "days_detail": entries,
        "notes": [
            "status=no_data 表示当天 0 事件/账本 0 元，无需日报；不计入 missing_count",
            "has_synthesis=false 且 status=missing 表示有事件但没有日报，仍属异常",
            "model_tier=unknown 表示产物早于 2026-10-19，未记录模型档位",
            "aligned_with_ledger=null 表示产物缺金额或账本字段，无法判定",
        ],
    }


def _device_offline_summary(
    devices: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """把多台设备的失联状态压成一条给前端/健康快照看的最小摘要。

    口径只在 ``watch_stream._verify_device_status`` 定义一次；这里只挑最严重的一台，
    不把设备自报的几十个字段塞进 1.7MB 的覆盖响应。
    """
    rank = {"alert": 2, "warn": 1}
    known = [
        d for d in (devices or [])
        if isinstance(d, Mapping) and str(d.get("device_id") or "")
    ]
    items = [d for d in known if str(d.get("offline") or "") in rank]
    if not items:
        # [2026-11-03 恢复路径] 曾经失联的设备重新上报后，必须给出**明确的 ok**，
        # 而不是继续让前端自己猜"字段为空 = 没事"。ok 里带 recovered_at，前端据此撤红。
        # 契约不变：**完全没有设备状态**时仍然返回 level=None（不知道 ≠ 正常）。
        if not known:
            return {
                "level": None, "silent_seconds": None, "silent_hours": None,
                "last_seen": None, "last_seen_clock": None,
                "pending_completion_count": None, "message": None,
                "recovered_at": None, "recovered_devices": [],
            }
        recovered_devices = []
        for d in known:
            rec = str(d.get("recovered_at") or "")
            if rec:
                recovered_devices.append({
                    "device_id": d.get("device_id"),
                    "recovered_at": rec,
                    "last_evidence_kind": d.get("last_evidence_kind"),
                })
        recovered_devices.sort(key=lambda item: str(item.get("recovered_at") or ""), reverse=True)
        silents = [d.get("silent_seconds") for d in known if isinstance(d.get("silent_seconds"), (int, float))]
        worst_silence = max(silents) if silents else None
        return {
            "level": "ok",
            "silent_seconds": worst_silence,
            "silent_hours": round(float(worst_silence) / 3600.0, 1) if worst_silence is not None else None,
            "last_seen": recovered_devices[0]["recovered_at"] if recovered_devices else None,
            "last_seen_clock": (recovered_devices[0]["recovered_at"] or "")[11:16] or None,
            "pending_completion_count": 0,
            "message": None,
            "recovered_at": recovered_devices[0]["recovered_at"] if recovered_devices else None,
            "recovered_devices": recovered_devices,
        }
    worst = max(items, key=lambda d: rank[str(d.get("offline"))])
    silent = worst.get("silent_seconds")
    try:
        hours = round(float(silent) / 3600.0, 1)
    except (TypeError, ValueError):
        hours = None
    try:
        pending = int(worst.get("pending_completion_count") or 0)
    except (TypeError, ValueError):
        pending = 0
    clock = worst.get("offline_since_clock")
    if hours is not None:
        message = f"手表已 {hours} 小时未上报"
        if clock:
            message += f"（上次 {clock}）"
    else:
        message = "手表从未上报状态"
    if pending > 0:
        message += f"；离开时还有 {pending} 段未确认完成"
    return {
        "level": worst.get("offline"),
        "silent_seconds": silent,
        "silent_hours": hours,
        "last_seen": worst.get("offline_since"),
        "last_seen_clock": clock,
        "pending_completion_count": pending,
        "message": message,
        # 仍在失联 → 没有恢复时刻
        "recovered_at": None,
        "recovered_devices": [],
    }


def _synthesis_staleness(
    brief: Mapping[str, Any] | None,
    synthesis: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compare canonical event count with the saved daily synthesis.

    The daily summary's ``generated_at`` is a read/build time, not a canonical
    event snapshot clock, so staleness is decided by the durable count mismatch.
    Timestamps are exposed for operators but never fabricated or inferred.
    """
    canonical = brief.get("event_count") if isinstance(brief, Mapping) else None
    synth_count = synthesis.get("event_count") if isinstance(synthesis, Mapping) else None
    try:
        canonical_count = int(canonical) if canonical is not None else None
    except (TypeError, ValueError):
        canonical_count = None
    try:
        synth_event_count = int(synth_count) if synth_count is not None else None
    except (TypeError, ValueError):
        synth_event_count = None

    canonical_generated_at = (
        brief.get("generated_at") if isinstance(brief, Mapping) else None
    )
    synthesis_generated_at = (
        synthesis.get("generated_at") if isinstance(synthesis, Mapping) else None
    )
    synthesis_basis = (
        synthesis.get("event_count_basis") if isinstance(synthesis, Mapping) else None
    )
    status: dict[str, Any] = {
        "stale": None,
        "known": False,
        "reason": "synthesis_missing" if synthesis is None else "missing_event_count",
        "canonical_event_count": canonical_count,
        "synthesis_event_count": synth_event_count,
        "count_mismatch": None,
        "canonical_generated_at": canonical_generated_at,
        "synthesis_generated_at": synthesis_generated_at,
        "canonical_event_count_basis": "event_view(scope, view)",
        "synthesis_event_count_basis": synthesis_basis,
    }
    if synthesis is None:
        return {
            "synthesis_stale": None,
            "synthesis_stale_reason": status["reason"],
            "synthesis_status": status,
        }
    if canonical_count is None or synth_event_count is None:
        return {
            "synthesis_stale": None,
            "synthesis_stale_reason": status["reason"],
            "synthesis_status": status,
        }
    mismatch = canonical_count != synth_event_count
    status.update(
        {
            "stale": bool(mismatch),
            "known": True,
            "reason": "count_mismatch" if mismatch else None,
            "count_mismatch": bool(mismatch),
        }
    )
    return {
        "synthesis_stale": bool(mismatch),
        "synthesis_stale_reason": status["reason"],
        "synthesis_status": status,
    }


def _failure_evidence(text: str) -> tuple[int, str] | None:
    """在阶段日志里找最后一次失败证据，返回 (行号, 摘要)。"""
    last: tuple[int, str] | None = None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if _TRACEBACK_MARKER in stripped or _ERROR_LINE_PATTERN.match(stripped):
            detail = stripped
            # Traceback 的真正结论在最后一行；把结尾的异常行也带上更可读。
            for follower in lines[index + 1 : index + 40]:
                candidate = follower.strip()
                if candidate and _ERROR_LINE_PATTERN.match(candidate):
                    detail = candidate
                    break
            last = (index, detail)
    return last


def collect_stage_errors(
    state: dict[str, Any], *, job_dir: Path | None = None
) -> list[dict[str, Any]]:
    """把「子步骤失败」汇总成结构化记录，而不是只躺在 logs 的文字里。

    每条记录带 ``severity``（fatal / degradable）与 ``resolved``（是否已经恢复），
    任务终态由它们推出：

    * ``stage_failed``       阶段被判失败 —— 致命
    * ``missing_result``     任务承诺的产物不存在 —— 致命
    * ``stage_unverified``   阶段自称完成，但落盘日志最后一次失败之后再没有成功
                             记录 —— 产物在就是可降级，产物没了就是致命
    * ``stage_retry``        阶段失败过、重跑成功（信息保留，任务不倒）
    * ``log_traceback``      日志里留着 Traceback 但之后有成功记录（信息保留）

    传 ``job_dir`` 时会读 ``files/<id>/<stage>.log`` 核对，能发现「状态说完成、
    日志其实是崩的」这一类假成功。
    """
    errors: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, str, str]] = set()

    def record(entry: dict[str, Any]) -> None:
        key = (entry["file_id"], entry["stage"], entry["kind"], entry["error"])
        if key in seen:
            return
        seen.add(key)
        errors.append(entry)

    for item in state.get("files") or []:
        if not isinstance(item, dict):
            continue
        stages = item.get("stages") or {}
        for stage, stage_state in stages.items():
            if not isinstance(stage_state, dict):
                continue
            severity = stage_severity(stage)
            failures = stage_state.get("failures")
            last_error = str(stage_state.get("error") or "")
            if stage_state.get("status") == "failed":
                record(
                    _stage_error(
                        item=item,
                        stage=stage,
                        kind="stage_failed",
                        severity=severity,
                        error=last_error or item.get("error") or f"{stage} 阶段失败",
                        resolved=False,
                    )
                )
                continue
            if isinstance(failures, list) and failures:
                first = failures[0] if isinstance(failures[0], dict) else {}
                detail = last_error or str(first.get("error") or "")
                if not detail:
                    detail = f"{stage} 曾失败后重跑成功"
                record(
                    _stage_error(
                        item=item,
                        stage=stage,
                        kind="stage_retry",
                        severity=DEGRADABLE_SEVERITY,
                        error=detail,
                        resolved=True,
                    )
                )
            elif last_error:
                record(
                    _stage_error(
                        item=item,
                        stage=stage,
                        kind="stage_retry",
                        severity=DEGRADABLE_SEVERITY,
                        error=last_error,
                        resolved=True,
                    )
                )
            # 落盘日志证据：只对「真的在本次任务里跑过」的阶段做核对
            # （watch 切片复用的 asr 阶段日志可能是别的运行留下的）。
            if stage_state.get("source"):
                continue
            if job_dir is None:
                continue
            text = _stage_log_text(job_dir, item, str(stage))
            if not text:
                continue
            evidence = _failure_evidence(text)
            if evidence is None:
                continue
            missing = _missing_stage_artifact(job_dir, item, str(stage))
            if _success_evidence_marker(text) > evidence[0] and not missing:
                record(
                    _stage_error(
                        item=item,
                        stage=stage,
                        kind="log_traceback",
                        severity=DEGRADABLE_SEVERITY,
                        error=evidence[1],
                        resolved=True,
                    )
                )
                continue
            record(
                _stage_error(
                    item=item,
                    stage=stage,
                    kind="stage_unverified",
                    severity=FATAL_SEVERITY if missing else DEGRADABLE_SEVERITY,
                    error=(
                        f"{stage} 阶段日志最后一次失败之后没有成功记录"
                        + (
                            f"；并且缺少产物 {missing}"
                            if missing
                            else "；产物仍在，完成度不可全信"
                        )
                        + f"：{evidence[1]}"
                    ),
                    resolved=False,
                )
            )
        # 承诺的产物必须真的在盘上（目录也算 —— 老产物里 results 记过日期目录）。
        if job_dir is not None and item.get("status") == "completed":
            file_id = str(item.get("sha256") or item.get("file_id") or "")
            file_root = Path(job_dir) / "files" / file_id
            for relative in item.get("results") or []:
                text_value = str(relative)
                candidate = Path(text_value)
                if candidate.is_absolute() or ".." in candidate.parts:
                    continue
                if (Path(job_dir) / candidate).exists():
                    continue
                if (file_root / candidate).exists():
                    continue
                if "/" not in text_value:
                    # 老产物里出现过裸名字（如 "2026-10-09"）当结果项；它不是路径。
                    # 产物在 result/<裸名> 下就算在，否则只是记录对不上，不判致命。
                    bases = ("result", "asr", "")
                    if any((file_root / base / text_value).exists() for base in bases):
                        continue
                    record(
                        _stage_error(
                            item=item,
                            stage=item.get("current_stage"),
                            kind="dangling_result",
                            severity=DEGRADABLE_SEVERITY,
                            error=f"产物记录指向不存在的条目：{text_value}",
                            resolved=False,
                        )
                    )
                    continue
                record(
                    _stage_error(
                        item=item,
                        stage=item.get("current_stage"),
                        kind="missing_result",
                        severity=FATAL_SEVERITY,
                        error=f"任务声明的产物不存在：{text_value}",
                        resolved=False,
                    )
                )
        # 状态级/文件级日志里的 Traceback：弱证据，只记录不单独降级。
        # 这类日志没有阶段归属（回调只带文件维度），所以 stage 留空。
        logs = item.get("logs") or []
        if isinstance(logs, list):
            for line in logs:
                text_line = str(line or "")
                if _TRACEBACK_MARKER in text_line or _ERROR_LINE_PATTERN.match(
                    text_line.strip()
                ):
                    record(
                        _stage_error(
                            item=item,
                            stage=None,
                            kind="log_traceback",
                            severity=DEGRADABLE_SEVERITY,
                            error=text_line.strip(),
                            resolved=True,
                        )
                    )
                    break
        # 检查上下文事件抽取产物：Jev/Clef 各自独立无主备容灾合并假象。
        # 任意一路模型发生熔断中止且事件全空，或抽取标记为 failed，
        # 绝不能报 completed 假成功，必须独立记录 stage_error 并降级为 partial。
        # 若已存在的产物文件读取/解析失败或损坏，必须明确记录 stage_error，不得静默放过；
        # 缺产物文件按原契约走，不影响未启用抽取场景。
        if job_dir is not None:
            file_id = str(item.get("sha256") or item.get("file_id") or "")
            ce_path = Path(job_dir) / "files" / file_id / "result" / "context-extraction.json"
            if ce_path.is_file():
                try:
                    ce_text = ce_path.read_text(encoding="utf-8")
                except OSError as err:
                    record(
                        _stage_error(
                            item=item,
                            stage="attribution",
                            kind="context_extraction_corrupt",
                            severity=FATAL_SEVERITY,
                            error=f"上下文抽取产物读取失败: {err}",
                            resolved=False,
                        )
                    )
                    ce_text = None
                if ce_text is not None:
                    try:
                        ce_data = json.loads(ce_text)
                    except json.JSONDecodeError as err:
                        record(
                            _stage_error(
                                item=item,
                                stage="attribution",
                                kind="context_extraction_corrupt",
                                severity=FATAL_SEVERITY,
                                error=f"上下文抽取产物损坏(非合法JSON): {err}",
                                resolved=False,
                            )
                        )
                        ce_data = None
                    if isinstance(ce_data, dict):
                        variants = ce_data.get("variants") if (ce_data.get("comparison_mode") and isinstance(ce_data.get("variants"), dict)) else None
                        targets = variants.items() if variants else [("default", ce_data)]
                        for provider, p_data in targets:
                            if not isinstance(p_data, dict):
                                record(
                                    _stage_error(
                                        item=item,
                                        stage="attribution",
                                        kind="context_extraction_corrupt",
                                        severity=FATAL_SEVERITY,
                                        error=f"Provider({provider})数据格式异常(非对象)",
                                        resolved=False,
                                    )
                                )
                                continue
                            p_status = str(p_data.get("status") or "").lower()
                            p_counts = p_data.get("counts") if isinstance(p_data.get("counts"), dict) else {}
                            try:
                                p_aborted = int(p_counts.get("aborted_batches") or 0)
                                p_formal = int(p_counts.get("formal_events") or 0)
                                p_passed = int(p_counts.get("kev_gate_passed") or 0)
                            except (TypeError, ValueError) as err:
                                record(
                                    _stage_error(
                                        item=item,
                                        stage="attribution",
                                        kind="context_extraction_corrupt",
                                        severity=FATAL_SEVERITY,
                                        error=f"Provider({provider})计数类型错误: {err}",
                                        resolved=False,
                                    )
                                )
                                continue
                            if p_status == "failed" or (p_aborted > 0 and p_formal == 0 and p_passed > 0):
                                prefix = f"Provider({provider})" if variants else "上下文抽取"
                                record(
                                    _stage_error(
                                        item=item,
                                        stage="attribution",
                                        kind="context_extraction_aborted",
                                        severity=FATAL_SEVERITY,
                                        error=(
                                            f"{prefix}熔断中止(status={p_status}, "
                                            f"aborted_batches={p_aborted}, kev_passed={p_passed}, formal_events={p_formal})"
                                        ),
                                        resolved=False,
                                    )
                                )
                    elif ce_data is not None:
                        record(
                            _stage_error(
                                item=item,
                                stage="attribution",
                                kind="context_extraction_corrupt",
                                severity=FATAL_SEVERITY,
                                error="上下文抽取产物顶层非对象",
                                resolved=False,
                            )
                        )
    return errors
