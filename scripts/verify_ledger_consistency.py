#!/usr/bin/env python3
"""验钞机：账本 / 时间线一致性硬检查（机器判卷，不看 agent 自述）。

为什么要有它（2026-10-22 A0 派单 P-18）：
最近反复出现「金额不一致 / 时间线条数不一致」，而 agent 自检却说"没问题"。
所以不再让 agent 自评，改成脚本按**不变量**逐日判 PASS/FAIL，失败直接给期望值 vs 实际值。

五条不变量（逐日）：
  1. 日报内部自洽：`synthesis.expense_analysis.total_amount_cny`
     == `synthesis.expense_ledger.total_amount`
  2. 日报 == `/api/daily` 当日 `expense_ledger.total_amount`
  3. == `/api/expense-range` 该日 `by_day[].total_cny`
     （`source="missing"` 时必须三方一致地标缺失，不能一边 0 一边有值）
  4. 日报 `event_count` == `/api/daily` 事件数 == `event_view` 当日事件数
  5. 前端不许自己算金额：扫 `web/*.js` 的 `reduce(` / `+=`，
     命中 expense/amount 且不在白名单 → FAIL（报 文件:行）

用法：
    python3 scripts/verify_ledger_consistency.py --from 2026-10-01 --to 2026-10-19
    python3 scripts/verify_ledger_consistency.py --from ... --to ... --json
    python3 scripts/verify_ledger_consistency.py --from ... --to ... --inject-mismatch 2026-10-14
        （自测用：只在内存里把某天日报金额改错，验证本脚本真能抓到；不写盘）

退出码：0 = 全部 PASS（N/A 不算失败）；1 = 存在 FAIL。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

TOLERANCE = 0.005
#: 服务基址：与 verify_all.sh 一致读 LIFELOGGER_API_BASE。
#: 写死 8765 会让 gate_runner --port 对数据关失效 —— 配了端口却仍打真实服务，
#: 看起来在隔离验证，实际在拿生产数据下判断。
BASE_URL = os.environ.get("LIFELOGGER_API_BASE", "http://127.0.0.1:8765")

PASS, FAIL, WARN, NA, SLOW = "PASS", "FAIL", "WARN", "N/A", "SLOW"
#: [2026-11-06] 日报过期：差额全部来自「日报生成之后才进来的事件」。
#: 账本是对的、日报只是旧了 —— 不该算 FAIL，也不能算 PASS。
STALE = "STALE"

#: 规则 5 白名单：命中了以下特征的行算「允许的金额累加」，并附理由。
#: 约束：必须逐条给出为什么它不是"第二套算法"。
RULE5_ALLOWLIST = (
    ("web/expense-drilldown.js", None,
     "消费下钻视图：把账本 transactions 的金额相加仅用于展示同一份账本"),
    (None, "derivedExpenseDiagnostic",
     "产物自洽诊断值（命名含 Diagnostic，只写进对象、从不渲染）"),
    (None, "totalExpense += Number(day.expense",
     "多日汇总：加的是 /api/daily 已 hydrate 的 day.expense（账本值），属聚合非重算"),
    ("web/app.js", "expenseMissingDays",
     "统计缺账本天数计数器（整型天数，非金额累加）"),
)


# --------------------------------------------------------------------------
# 数据获取
# --------------------------------------------------------------------------
def _token() -> str:
    try:
        return str(json.loads((ROOT / "output" / "local-web" / "secrets.json").read_text())
                   .get("mobile_access_token") or "")
    except (OSError, ValueError):
        return ""


COMPUTING_STATUSES = {"computing", "read_busy", "pending", "warming"}
COMPUTING_ERRORS = {"read_busy", "computing", "producer_busy"}


def _is_computing(payload: object) -> bool:
    """服务端「还在算」的响应，而不是算完了的结果。"""
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().lower()
    error = str(payload.get("error") or "").strip().lower()
    if payload.get("computing") is True:
        return True
    return status in COMPUTING_STATUSES or error in COMPUTING_ERRORS


def _retry_after(payload: object, headers: object = None) -> float | None:
    """从响应体或 Retry-After 头取建议等待秒数。"""
    for source in (payload if isinstance(payload, dict) else {},):
        for key in ("retry_after", "retry_after_seconds", "retryAfter"):
            raw = source.get(key)
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    raw_header = None
    if headers is not None:
        try:
            raw_header = headers.get("Retry-After")  # type: ignore[union-attr]
        except Exception:
            raw_header = None
    if raw_header is not None:
        try:
            return float(raw_header)
        except (TypeError, ValueError):
            return None
    return None


def _get(path: str, token: str, timeout: float = 15.0) -> tuple[dict | None, str, str]:
    """返回 (payload, status, reason)；status ∈ {"ok", "computing", "timeout", "unavailable"}。

    为什么必须四态：原先只有 `is_timeout` 一个布尔，**连接被拒 / 连接重置 / HTTP 5xx /
    响应不是 JSON 全被当成 False**，于是 `daily = {}` → 金额读成 None → 判「数据不一致」。

    还必须单独识别 HTTP 202 computing：异步租约方案下，producer 没算完时服务会回
    `202 {"status": "computing", "error": "read_busy", "retry_after": N}`。
    这是**合法 JSON、也是 2xx**，若当 ok 处理，`expense_ledger` 缺失 → 金额读成 None
    → 立刻被误判成「账本不一致」。它表达的是「还没算完」，只能算 SLOW/等待。
    """
    try:
        request = Request(
            BASE_URL + path,
            headers={"Authorization": f"Bearer {token}", "Connection": "close"},
        )
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
            code = getattr(response, "status", None)
            if code is None:
                try:
                    code = response.getcode()
                except Exception:
                    code = None
            if code == 202 or _is_computing(payload):
                retry_after = _retry_after(payload, getattr(response, "headers", None))
                detail = f"仍在计算 retry_after={retry_after:g}s" if retry_after is not None else "仍在计算"
                if isinstance(payload, dict):
                    payload = {**payload, "_retry_after": retry_after}
                return payload, "computing", detail
            return payload, "ok", ""
    except TimeoutError:
        return None, "timeout", f"接口超时未返回（>{timeout:.0f}s）"
    except HTTPError as exc:
        # 202 也可能被某些库包成 HTTPError（非 2xx 判定差异），这里再兜一次。
        if getattr(exc, "code", None) == 202:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = {}
            retry_after = _retry_after(payload, getattr(exc, "headers", None))
            if isinstance(payload, dict):
                payload = {**payload, "_retry_after": retry_after}
            detail = f"仍在计算 retry_after={retry_after:g}s" if retry_after is not None else "仍在计算"
            return payload, "computing", detail
        return None, "unavailable", f"接口返回 HTTP {exc.code}"
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ConnectionResetError):
            return None, "unavailable", "接口连接被重置"
        return None, "unavailable", f"接口不可达（{reason}）"
    except OSError as exc:
        return None, "unavailable", f"接口连接中断（{type(exc).__name__}）"
    except ValueError as exc:
        return None, "unavailable", f"接口响应不可解析（{type(exc).__name__}）"


def _money(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _same(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < TOLERANCE


def _synthesis(day: str, inject: dict[str, float] | None = None) -> dict | None:
    path = ROOT / "output" / "local-web" / "daily-synthesis" / f"synthesis-{day}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if inject and day in inject:
        # 仅内存扰动（自测用），绝不写盘
        section = dict(payload.get("expense_analysis") or {})
        section["total_amount_cny"] = inject[day]
        payload["expense_analysis"] = section
    return payload


def _event_view_counts(days: list[str]) -> dict[str, int]:
    """与 /api/daily 同源的当日事件数（event_view + dictation 读层过滤）。"""
    from life_logger.event_view import build_event_view
    from life_logger.local_web import JobStore, LocalWebServer

    for name in ("_watch_live_loop", "_reminder_loop", "_auto_ingest_loop",
                 "_warm_vip_transcriber", "_run_watch_formal_asr_scan"):
        if hasattr(LocalWebServer, name):
            setattr(LocalWebServer, name, lambda self, *a, **k: None)
    app = LocalWebServer(("127.0.0.1", 0), JobStore(ROOT / "output" / "local-web", recover=False),
                         ROOT / "local-config.json")
    event_filter = app.dashboard_dictation_filter().filter_events

    counts: dict[str, int] = {}
    if not days:
        return counts
    payload = build_event_view(ROOT / "output" / "local-web", date_from=days[0],
                               date_to=days[-1], scope="provisional", view="display")
    events = [dict(e) for e in payload.get("events", []) if isinstance(e, dict)]
    filtered = event_filter(events)
    if isinstance(filtered, list):
        events = [dict(e) for e in filtered if isinstance(e, dict)]
    for event in events:
        day = str(event.get("effective_date") or "")
        if day:
            counts[day] = counts.get(day, 0) + 1
    return counts


# --------------------------------------------------------------------------
# 规则 6（WARN）：同一次录音 60s 内同商户多事件 —— 同一笔消费可能被切成多张卡
# --------------------------------------------------------------------------
def check_duplicate_purchase_windows(date_from: str, date_to: str) -> dict:
    """扫描区间内「同录音 + 60s 内 + 同商户（或同金额）」的可见事件簇。

    2026-10-21 实测：一句话被 ASR 切成 3 段（20:46:42/:44/:46），
    各自抽出一条「花了26.8点了一份 / 丑宝汉堡王套餐 / 三件套26.8」，
    页面出 3 张卡。金额账本是对的（只算 1 笔），所以规则 1~4 全绿也抓不到。
    这里只报 WARN，不算 FAIL：真实连买两笔（如两包烟）也长这样，需要人判断。
    """
    from life_logger.contextual_extraction import (
        _event_recording_key,
        _same_purchase_signature,
    )
    from life_logger.event_view import build_event_view
    from life_logger.timeutil import parse_ts

    payload = build_event_view(
        ROOT / "output" / "local-web",
        date_from=date_from,
        date_to=date_to,
        scope="provisional",
        view="display",
    )
    events = [dict(e) for e in payload.get("events", []) if isinstance(e, dict)]

    groups: dict[str, list[dict]] = {}
    for event in events:
        if event.get("suppressed"):
            continue
        key = _event_recording_key(event)
        if not key:
            continue
        groups.setdefault(key, []).append(event)

    clusters: list[dict] = []
    for key, members in groups.items():
        def _ts(event):
            time_info = event.get("time") if isinstance(event.get("time"), Mapping) else {}
            evidence = event.get("evidence") if isinstance(event.get("evidence"), Mapping) else {}
            return parse_ts(time_info.get("absolute") or evidence.get("reported_at"))

        ordered = sorted((e for e in members if _ts(e) is not None), key=_ts)
        window: list[dict] = []
        for event in ordered:
            if not window:
                window = [event]
                continue
            gap = (_ts(event) - _ts(window[-1])).total_seconds()
            if gap < 60.0 and _same_purchase_signature(window[-1], event):
                window.append(event)
                continue
            if len(window) > 1 and _window_is_suspicious(window):
                clusters.append(_describe_window(key, window))
            window = [event]
        if len(window) > 1 and _window_is_suspicious(window):
            clusters.append(_describe_window(key, window))

    return {
        "status": "WARN" if clusters else PASS,
        "cluster_count": len(clusters),
        "clusters": clusters,
    }


def _window_is_suspicious(window: list[dict]) -> bool:
    """只有「消费叙事」里、且金额不冲突的窗口才值得人看一眼。

    过滤掉实测噪声：两条不同金额的 POS 播报（¥21.8 / ¥15.9）、两条无金额的
    同类描述——它们不是「一笔被切碎」，只是同分钟的两次正常事件。
    """
    from life_logger.contextual_extraction import _event_amount, _event_relevant_expense

    amounts = [a for a in (_event_amount(e) for e in window) if a is not None]
    if not amounts:
        return False
    if len(amounts) > 1:
        return False  # 多条带金额 = 多笔支付，天然不是「一句话被切碎」
    return any(_event_relevant_expense(e) for e in window)


def _describe_window(recording_key: str, window: list[dict]) -> dict:
    from life_logger.contextual_extraction import _event_amount, _event_expense

    def _clock(event):
        time_info = event.get("time") if isinstance(event.get("time"), Mapping) else {}
        return str(time_info.get("clock") or time_info.get("absolute") or "")

    amounts = [a for a in (_event_amount(e) for e in window) if a is not None]
    expenses = [_event_expense(e) for e in window]
    merchants = sorted({str(x.get("merchant")) for x in expenses if x.get("merchant")})
    return {
        "recording": recording_key,
        "date": str(window[0].get("effective_date") or ""),
        "clocks": [_clock(e) for e in window],
        "event_ids": [str(e.get("event_id")) for e in window],
        "titles": [str(e.get("title") or "")[:40] for e in window],
        "merchants": merchants,
        "amounts": amounts,
        "amount_events": len(amounts),
    }


# --------------------------------------------------------------------------
def _event_created_at(event: Mapping[str, Any]) -> str | None:
    for key in ("created_at", "received_at", "ingested_at"):
        value = event.get(key) if isinstance(event, Mapping) else None
        if isinstance(value, str) and value:
            return value
    return None


def _late_events(synth: Mapping[str, Any] | None, daily_events: Sequence[Any]) -> list[dict[str, Any]]:
    """返回 /api/daily 里「created_at 晚于日报 generated_at」的事件。

    这些是「日报跑完之后用户才补录/才到货」的输入 —— 日报看不到它们不是数据错。
    缺 created_at 的旧事件不算「迟到」（历史产物没有这个字段），只跳过；
    但 STALE 是否成立仍要求「差额 == 迟到事件之和」，所以不会把真错误洗成 STALE。
    """
    generated = _as_dt((synth or {}).get("generated_at"))
    if generated is None:
        return []
    late: list[dict[str, Any]] = []
    for event in daily_events or []:
        if not isinstance(event, Mapping):
            continue
        created = _as_dt(_event_created_at(event))
        if created is None:
            continue           # 老产物没这个字段：不算迟到
        if created > generated:
            late.append(dict(event))
    return late


def _as_dt(value: Any) -> "datetime | None":
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _event_amount(event: Mapping[str, Any]) -> float:
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    expense = data.get("expense") if isinstance(data, Mapping) else None
    if isinstance(expense, Mapping):
        return _money(expense.get("amount")) or 0.0
    return 0.0


def _late_summary(late: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for event in late[:6]:
        title = str(event.get("title") or "未命名")[:18]
        amount = _event_amount(event)
        parts.append(f"{title} ¥{amount:g}" if amount else title)
    more = f" 等 {len(late)} 条" if len(late) > 6 else ""
    return "、".join(parts) + more


def _stale_explains(late: Sequence[Mapping[str, Any]], amount_gap: float | None, count_gap: int | None) -> bool:
    """差额是否完全由这些迟到事件解释。"""
    if not late:
        return False
    late_total = round(sum(_event_amount(e) for e in late), 2)
    if amount_gap is not None and abs(round(amount_gap, 2) - late_total) > 0.01:
        return False
    if count_gap is not None and int(count_gap) != len(late):
        return False
    return True


# 规则 5：前端自己算金额
# --------------------------------------------------------------------------
_AMOUNT_HINT = re.compile(r"expense|amount|total_cny|money|spend", re.IGNORECASE)
_ACCUMULATE = re.compile(r"\.reduce\s*\(|\+=")
_COMMENT = re.compile(r"^\s*(//|/\*|\*)")


def check_frontend_money() -> tuple[str, list[dict], list[dict]]:
    hits: list[dict] = []
    allowed: list[dict] = []
    for path in sorted((ROOT / "web").glob("*.js")):
        if path.name.endswith(".test.js"):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            if _COMMENT.match(line) or not _ACCUMULATE.search(line):
                continue
            window = " ".join(lines[max(0, index - 3): index + 2])
            if not _AMOUNT_HINT.search(window):
                continue
            rel = f"web/{path.name}"
            reason = None
            for file_key, snippet_key, why in RULE5_ALLOWLIST:
                if file_key and rel != file_key:
                    continue
                if snippet_key and snippet_key not in window:
                    continue
                reason = why
                break
            record = {"file": rel, "line": index, "code": line.strip()[:120]}
            if reason:
                allowed.append({**record, "reason": reason})
            else:
                hits.append(record)
    return (FAIL if hits else PASS), hits, allowed


# --------------------------------------------------------------------------
# 主检查
# --------------------------------------------------------------------------
def verify(date_from: str, date_to: str, *, inject: dict[str, float] | None = None) -> dict:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    days: list[str] = []
    cursor = start
    while cursor <= end:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)

    token = _token()
    range_payload, range_status, range_reason = _get(
        f"/api/expense-range?from={date_from}&to={date_to}", token
    )
    range_payload = range_payload or {}
    range_by_day = {
        str(row.get("date")): row for row in (range_payload.get("by_day") or [])
        if isinstance(row, dict)
    }
    ev_counts = _event_view_counts(days)

    rows: list[dict] = []
    warn_days: list[str] = []
    slow_days: list[dict] = []
    for day in days:
        synth = _synthesis(day, inject)
        daily, daily_status, daily_reason = _get(f"/api/daily?date={day}", token)
        daily = daily or {}
        daily_total = _money(((daily.get("expense_ledger") or {}).get("total_amount")))
        daily_events = len(daily.get("events") or []) if daily else None
        rng = range_by_day.get(day) or {}
        rng_total = _money(rng.get("total_cny"))
        rng_source = str(rng.get("source") or ("missing" if not rng else "ledger"))
        ev_count = ev_counts.get(day, 0)

        synth_analysis = _money(((synth or {}).get("expense_analysis") or {}).get("total_amount_cny"))
        synth_ledger = _money(((synth or {}).get("expense_ledger") or {}).get("total_amount"))
        synth_events = (synth or {}).get("event_count")
        # [2026-11-06] 迟到事件：日报生成之后才 created 的事件
        _late = _late_events(synth, daily.get("events") or [])
        _late_note = f"日报过期（生成后新增 {len(_late)} 条：{_late_summary(_late)}）" if _late else ""

        # 规则 1：日报内部自洽（纯离线文件，不依赖网络）
        if synth is None:
            rule1, note1 = NA, "无日报文件"
        elif _same(synth_analysis, synth_ledger):
            rule1, note1 = PASS, ""
        else:
            rule1 = FAIL
            note1 = f"日报金额={synth_analysis} vs 日报账本={synth_ledger}"

        # 规则 2：日报 == /api/daily
        if synth is None:
            rule2, note2 = NA, "无日报文件"
        elif daily_status != "ok":
            # 接口查不到 / 还在算 ≠ 数据不一致：只许记 SLOW，绝不许进「失败天数」。
            rule2, note2 = SLOW, f"/api/daily {daily_reason}"
            slow_days.append({
                "day": day, "endpoint": f"/api/daily?date={day}",
                "rule": "rule2", "kind": daily_status, "reason": daily_reason,
                "retry_after": (daily or {}).get("_retry_after"),
            })
        elif _same(synth_analysis, daily_total):
            rule2, note2 = PASS, ""
        elif _stale_explains(_late, (daily_total or 0.0) - (synth_analysis or 0.0), None):
            rule2, note2 = STALE, _late_note
        else:
            rule2 = FAIL
            note2 = f"日报={synth_analysis} vs /api/daily={daily_total}"

        # 规则 3：== /api/expense-range
        if range_status != "ok" or (daily_status != "ok" and synth is not None):
            if range_status != "ok":
                rule3, note3 = SLOW, f"/api/expense-range {range_reason}"
                slow_days.append({
                    "day": day, "endpoint": "/api/expense-range",
                    "rule": "rule3", "kind": range_status, "reason": range_reason,
                    "retry_after": (range_payload or {}).get("_retry_after"),
                })
            else:
                rule3, note3 = SLOW, f"/api/daily {daily_reason}"
                slow_days.append({
                    "day": day, "endpoint": f"/api/daily?date={day}",
                    "rule": "rule3", "kind": daily_status, "reason": daily_reason,
                    "retry_after": (daily or {}).get("_retry_after"),
                })
        elif rng_source == "missing":
            if synth_analysis in (None, 0.0) and (daily_total in (None, 0.0)) and not daily_events:
                rule3, note3 = PASS, "三方一致标缺失"
            else:
                rule3 = FAIL
                note3 = f"区间标 missing，但 日报={synth_analysis} /api_daily={daily_total}"
        elif synth is None:
            rule3 = NA
            note3 = f"无日报文件（当日账本=¥{rng_total}）"
            if rng_total:
                warn_days.append(day)
        elif _same(rng_total, synth_analysis) and _same(rng_total, daily_total):
            rule3, note3 = PASS, ""
        elif _stale_explains(_late, (rng_total or 0.0) - (synth_analysis or 0.0), None):
            rule3, note3 = STALE, _late_note
        elif _late and _same(rng_total, synth_analysis) and _stale_explains(
                _late, (daily_total or 0.0) - (synth_analysis or 0.0), None):
            # 区间接口自己也还停在旧数（和日报一样没算进迟到事件）→ 同一原因，也算过期
            rule3, note3 = STALE, _late_note + "（expense-range 也还没算进这些事件）"
        else:
            rule3 = FAIL
            note3 = f"expense-range={rng_total} vs 日报={synth_analysis} vs /api/daily={daily_total}"

        # 规则 4：条数三方一致
        if synth is None:
            rule4, note4 = NA, "无日报文件"
        elif daily_status != "ok":
            rule4, note4 = SLOW, f"/api/daily {daily_reason}"
        elif synth_events == daily_events == ev_count:
            rule4, note4 = PASS, ""
        elif _stale_explains(_late, None, (daily_events or 0) - (synth_events or 0)):
            rule4, note4 = STALE, _late_note
        else:
            rule4 = FAIL
            note4 = f"日报={synth_events} vs /api/daily={daily_events} vs event_view={ev_count}"

        rows.append({
            "date": day,
            "rule1": rule1, "rule1_note": note1,
            "rule2": rule2, "rule2_note": note2,
            "rule3": rule3, "rule3_note": note3,
            "rule4": rule4, "rule4_note": note4,
            "synthesis_total": synth_analysis,
            "daily_total": daily_total,
            "range_total": rng_total,
            "range_source": rng_source,
            "synthesis_events": synth_events,
            "daily_events": daily_events,
            "event_view_events": ev_count,
            # 本日 /api/daily 的可达性：ok / timeout / unavailable。
            # 非 ok 时上面 rule2~4 一律是 SLOW，绝不进 failed_days。
            "daily_status": daily_status,
        })

    # 同一天可能同时触发 rule2/3/4，但「SLOW N 天」的单位是天，不是规则条数：
    # 按天合并，避免 4 天坏服务被打印成「8 天」。
    merged_slow: dict[str, dict] = {}
    for item in slow_days:
        day = item["day"]
        entry = merged_slow.get(day)
        if entry is None:
            merged_slow[day] = {
                "day": day,
                "endpoint": item.get("endpoint"),
                "rules": [item.get("rule")],
                "kind": item.get("kind"),
                "reason": item.get("reason"),
                "retry_after": item.get("retry_after"),
            }
        else:
            if item.get("rule") not in entry["rules"]:
                entry["rules"].append(item.get("rule"))
            if entry.get("retry_after") is None and item.get("retry_after") is not None:
                entry["retry_after"] = item.get("retry_after")
    slow_days = list(merged_slow.values())

    rule5, hits5, allowed5 = check_frontend_money()
    try:
        rule6 = check_duplicate_purchase_windows(date_from, date_to)
    except Exception as error:  # 规则 6 是 WARN 级，扫描失败不能拖垮对账
        rule6 = {"status": "WARN", "cluster_count": 0, "clusters": [],
                 "note": f"扫描失败: {type(error).__name__}: {error}"}
    failed = [row["date"] for row in rows
              if FAIL in (row["rule1"], row["rule2"], row["rule3"], row["rule4"])]
    stale = [row["date"] for row in rows
             if STALE in (row["rule1"], row["rule2"], row["rule3"], row["rule4"])]
    slow = [row["date"] for row in rows
            if SLOW in (row["rule1"], row["rule2"], row["rule3"], row["rule4"])]
    if rule5 == FAIL:
        failed.append("frontend")

    verdict = FAIL if failed else (STALE if stale else (SLOW if slow else PASS))
    return {
        "from": date_from,
        "to": date_to,
        "rows": rows,
        "rule5": {"status": rule5, "violations": hits5, "allowed": allowed5},
        "rule6": rule6,
        "failed_days": [d for d in failed if d != "frontend"],
        "stale_days": stale,
        "slow_days": slow_days,
        # 接口可达性：用来区分「数据错」和「服务查不了」。
        # gate_runner 据此把纯接口故障归 FAIL_ENV，而不是 FAIL_DATA。
        "interface": {
            "expense_range_status": range_status,
            "expense_range_reason": range_reason,
            "daily_unreachable_days": [
                row["date"] for row in rows if row.get("daily_status") != "ok"
            ],
            "daily_unreachable_count": sum(
                1 for row in rows if row.get("daily_status") != "ok"
            ),
            # 「还在算」与「根本连不上」分开计数：前者等一会儿就好，后者是服务挂了。
            "daily_computing_days": [
                row["date"] for row in rows if row.get("daily_status") == "computing"
            ],
            "daily_computing_count": sum(
                1 for row in rows if row.get("daily_status") == "computing"
            ),
        },
        "warn_days": warn_days,
        "frontend_failed": rule5 == FAIL,
        "verdict": verdict,
    }


def _print_table(result: dict) -> None:
    print(f"{'日期':11} {'规则1':>6} {'规则2':>6} {'规则3':>6} {'规则4':>6} {'规则5':>6}  结论")
    for row in result["rows"]:
        has_fail = FAIL in (row["rule1"], row["rule2"], row["rule3"], row["rule4"])
        has_stale = STALE in (row["rule1"], row["rule2"], row["rule3"], row["rule4"])
        has_slow = SLOW in (row["rule1"], row["rule2"], row["rule3"], row["rule4"])
        tag = "FAIL" if has_fail else ("STALE" if has_stale else ("SLOW" if has_slow else "PASS"))
        print(f"{row['date']} {row['rule1']:>6} {row['rule2']:>6} {row['rule3']:>6} "
              f"{row['rule4']:>6} {result['rule5']['status']:>6}  {tag}")
        rule6 = result.get("rule6") or {}
        if rule6.get("cluster_count"):
            print(f"  ⚠️ 规则6（WARN，不算失败）：{rule6['cluster_count']} 处「同录音 60s 内同商户多事件」")
            for cluster in rule6.get("clusters", []):
                print(f"     {cluster.get('date')} {cluster.get('clocks')} "
                      f"{cluster.get('merchants') or '无商户'} "
                      f"金额={cluster.get('amounts')} ids={cluster.get('event_ids')}")
        for key in ("rule1", "rule2", "rule3", "rule4"):
            if row[key] == FAIL:
                print(f"    ↳ {key}: {row[key + '_note']}")
            elif row[key] == STALE:
                print(f"    🕒 {key} (STALE): {row[key + '_note']}")
            elif row[key] == SLOW:
                print(f"    ⏱ {key} (SLOW): {row[key + '_note']}")
    for item in result["rule5"]["violations"]:
        print(f"    ↳ 规则5 违规: {item['file']}:{item['line']}  {item['code']}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--inject-mismatch", default=None,
                        help="自测用：只改内存里该日日报金额，验证脚本能抓到（不写盘）")
    parser.add_argument("--verbose", action="store_true", help="打印规则5 白名单命中")
    args = parser.parse_args(argv)

    inject = {args.inject_mismatch: 999.99} if args.inject_mismatch else None
    result = verify(args.date_from, args.date_to, inject=inject)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_table(result)
        if args.verbose:
            for item in result["rule5"]["allowed"]:
                print(f"    ✓ 规则5 白名单: {item['file']}:{item['line']} — {item['reason']}")
        slow_day_count = len({item["day"] for item in result.get("slow_days", [])})
        unreachable = (result.get("interface") or {}).get("daily_unreachable_count", 0)
        stale_count = len(result.get("stale_days") or [])
        summary = (
            f"汇总: {len(result['rows'])} 天 | 失败 {len(result['failed_days'])} 天"
            f" | 日报过期 {stale_count} 天 | SLOW {slow_day_count} 天 | 接口不可达 {unreachable} 天"
            f" | 结论 {result['verdict']}"
        )
        print(f"\n{summary}")
    return 1 if result["verdict"] == FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
