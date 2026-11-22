#!/usr/bin/env python3
"""每日体检脚本（只读）：把“账准不准、用户要操心几次”变成每天能算的数。

用法：
  python3 scripts/daily_health_check.py --from 2026-11-06 --to 2026-11-09
  python3 scripts/daily_health_check.py --from 2026-11-06 --to 2026-11-09 --json

硬规矩：
  - 只通过 http://127.0.0.1:8765 的已有 GET 接口或只读打开 output/ 下文件取数；
  - 绝不写 output/，不调用任何写接口，不发微信，不重启服务；
  - 绝不打印或输出令牌（output/local-web/secrets.json）。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_ROOT = REPO_ROOT / "output" / "local-web"
DEFAULT_BASE_URL = "http://127.0.0.1:8765"

# 口述复盘/补报的启发式识别词（仅用于“区分不了”补充提示，解释录音内口述补报被混入自动账的现象）
_SPOKEN_RECAP_PATTERNS = re.compile(
    r"(补报|口述补|补充一下.*消费|三个消费|计算一下今天的一个总摄入|今天.*点的时候去.*买|当时是没带手表|回顾昨天|回顾刚才|刚才那会儿我已经|_report_|_recap_)"
)


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING)
    return dt.astimezone(BEIJING)


def _beijing_date_str(value: Any) -> str | None:
    dt = _parse_iso(value)
    return dt.date().isoformat() if dt else None


def _date_range(start_str: str, end_str: str) -> list[str]:
    start = date.fromisoformat(start_str)
    end = date.fromisoformat(end_str)
    if end < start:
        raise ValueError(f"--to ({end_str}) 不能早于 --from ({start_str})")
    days: list[str] = []
    cur = start
    while cur <= end:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def _read_json_safe(path: Path) -> Any:
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _read_token_silently(root: Path) -> str | None:
    """只读读取本地 token 供请求头使用，绝不打印或写入返回体。"""
    payload = _read_json_safe(root / "secrets.json")
    if isinstance(payload, Mapping):
        token = (
            payload.get("mobile_access_token")
            or payload.get("access_token")
            or payload.get("local_api_token")
            or payload.get("token")
        )
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def _http_get_json(
    base_url: str,
    path_and_query: str,
    *,
    token: str | None = None,
    timeout: float = 5.0,
) -> tuple[int | None, Any, str | None]:
    """只读调用本地 GET 接口。返回 (status_code, json_payload, error_message)。"""
    url = base_url.rstrip("/") + "/" + path_and_query.lstrip("/")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw), None
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
            err_json = json.loads(err_body)
            if isinstance(err_json, Mapping) and err_json.get("error"):
                err_body = str(err_json["error"])
        except Exception:
            pass
        return exc.code, None, f"HTTP {exc.code}: {err_body or exc.reason}"
    except Exception as exc:
        return None, None, str(exc)


def _collect_manual_events_by_day(root: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """收集 output/local-web/jobs/job-*/manual-events.json 里的手动/判断队列补录事件。"""
    by_day: dict[str, dict[str, dict[str, Any]]] = {}
    jobs_dir = root / "jobs"
    if not jobs_dir.is_dir():
        return by_day
    for path in sorted(jobs_dir.glob("job-*/manual-events.json")):
        payload = _read_json_safe(path)
        entries = (
            payload.get("events", [])
            if isinstance(payload, Mapping)
            else (payload if isinstance(payload, list) else [])
        )
        for ev in entries:
            if not isinstance(ev, Mapping):
                continue
            eid = str(ev.get("event_id") or "").strip()
            if not eid:
                continue
            artifact = str(ev.get("artifact_path") or "")
            day = str(ev.get("restored_date") or "")[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                day = artifact[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                day = _beijing_date_str((ev.get("time") or {}).get("absolute")) or ""
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                by_day.setdefault(day, {})[eid] = dict(ev)
    return by_day


def _load_ledger_review_decisions(root: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json_safe(root / "ledger-review.json")
    if isinstance(payload, Mapping) and isinstance(payload.get("decisions"), Mapping):
        return {
            str(k): dict(v)
            for k, v in payload["decisions"].items()
            if isinstance(v, Mapping)
        }
    return {}


def _load_jev_items_by_id(root: Path, day: str) -> dict[str, dict[str, Any]]:
    """读取 activity-timeline/jev/<day>.json 里的原始抽取条目，供核对 pending_reason 等。"""
    payload = _read_json_safe(root / "activity-timeline" / "jev" / f"{day}.json")
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, Mapping):
        return result
    for act in payload.get("activities") or []:
        if not isinstance(act, Mapping):
            continue
        aid = str(act.get("id") or act.get("activity_id") or "").strip()
        for idx, it in enumerate(act.get("items") or [], start=1):
            if not isinstance(it, Mapping):
                continue
            iid = str(it.get("id") or it.get("item_id") or f"it{idx}").strip()
            composite_hash = f"{aid}#{iid}"
            composite_colon = f"activity:{day}:{aid}:{iid}"
            info = {**dict(it), "_activity_id": aid, "_item_id": iid, "_activity": dict(act)}
            result[composite_hash] = info
            result[composite_colon] = info
            result[iid] = info
    return result


def _load_day_corrections(root: Path, day: str) -> list[dict[str, Any]]:
    """读取当天 activity-timeline/jev/<day>.corrections.json 及 broadcast-decisions/<day>.json。"""
    mods: list[dict[str, Any]] = []
    corr_path = root / "activity-timeline" / "jev" / f"{day}.corrections.json"
    corr_payload = _read_json_safe(corr_path)
    if isinstance(corr_payload, Mapping):
        items = corr_payload.get("items") or corr_payload.get("corrections") or {}
        if isinstance(items, Mapping):
            for target_id, detail in items.items():
                mods.append({
                    "id": str(target_id),
                    "ledger": "activity_timeline",
                    "source": f"activity-timeline/jev/{day}.corrections.json",
                    "action": str((detail or {}).get("action") or "correct") if isinstance(detail, Mapping) else "correct",
                })
        elif isinstance(items, list):
            for entry in items:
                if isinstance(entry, Mapping):
                    mods.append({
                        "id": str(entry.get("item_id") or entry.get("activity_id") or "unknown"),
                        "ledger": "activity_timeline",
                        "source": f"activity-timeline/jev/{day}.corrections.json",
                        "action": str(entry.get("action") or "correct"),
                    })

    bcast_path = root / "broadcast-decisions" / f"{day}.json"
    bcast_payload = _read_json_safe(bcast_path)
    if isinstance(bcast_payload, Mapping):
        decs = bcast_payload.get("decisions")
        if isinstance(decs, Mapping):
            for eid, detail in decs.items():
                if not isinstance(detail, Mapping):
                    continue
                action = str(detail.get("action") or detail.get("decision") or "")
                if action in {"exclude", "dismiss", "accept", "confirm"}:
                    mods.append({
                        "id": str(eid),
                        "ledger": "expense",
                        "source": f"broadcast-decisions/{day}.json",
                        "action": action,
                    })
    return mods


def _load_ledgers_for_day(
    root: Path,
    day: str,
    *,
    day_manual_events: list[dict[str, Any]] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    token: str | None = None,
    offline: bool = False,
) -> tuple[dict[str, Any], str]:
    """优先从 GET /api/daily?date=YYYY-MM-DD 取四本账，离线或失败时回退到 day_facts 只读计算。"""
    if not offline:
        status, payload, _ = _http_get_json(
            base_url,
            f"/api/daily?date={urllib.parse.quote(day)}",
            token=token,
        )
        if status == 200 and isinstance(payload, Mapping) and payload.get("expense_ledger") is not None:
            return dict(payload), f"GET /api/daily?date={day}"

    try:
        from life_logger.day_facts import day_expense_ledger, domain_ledgers
        from life_logger.judgment_queue import skipped_calorie_event_ids

        events = list(day_manual_events or [])
        exp = day_expense_ledger(events, day, root=root)
        skipped_ids = skipped_calorie_event_ids(root, day)
        dom = domain_ledgers(
            events,
            day,
            root=root,
            nutrition_options={"skipped_event_ids": skipped_ids} if skipped_ids else None,
        )
        return (
            {
                "date": day,
                "expense_ledger": exp,
                "nutrition_ledger": dom.get("nutrition") or {"food_records": [], "other_records": [], "unweighed_items": []},
                "fitness_ledger": dom.get("fitness") or {"workouts": []},
                "smoking_ledger": dom.get("smoking") or {"sessions": []},
            },
            "life_logger.day_facts (offline fallback)",
        )
    except Exception:
        return (
            {
                "date": day,
                "expense_ledger": {"transactions": []},
                "nutrition_ledger": {"food_records": [], "other_records": [], "unweighed_items": []},
                "fitness_ledger": {"workouts": []},
                "smoking_ledger": {"sessions": []},
            },
            "empty_fallback",
        )


def _is_spoken_recap_text(*texts: Any) -> bool:
    blob = " ".join(str(t or "") for t in texts if t)
    return bool(_SPOKEN_RECAP_PATTERNS.search(blob))


def _analyze_ledgers_for_day(
    root: Path,
    day: str,
    daily_payload: Mapping[str, Any],
    manual_events_by_day: Mapping[str, Mapping[str, dict[str, Any]]],
    ledger_review_decisions: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    """分析单日四本账的：1.自动入账、2.用户手动补录、3.用户改过/删过的自动记录。"""
    day_manuals = manual_events_by_day.get(day, {})
    jev_items = _load_jev_items_by_id(root, day)

    auto_counts = {"expense": 0, "nutrition": 0, "fitness": 0, "smoking": 0}
    manual_counts = {"expense": 0, "nutrition": 0, "fitness": 0, "smoking": 0}
    modified_counts = {"expense": 0, "nutrition": 0, "fitness": 0, "smoking": 0}
    spoken_recap_counts = {"expense": 0, "nutrition": 0, "fitness": 0, "smoking": 0}

    auto_details: list[dict[str, Any]] = []
    manual_details: list[dict[str, Any]] = []
    modified_details: list[dict[str, Any]] = []
    spoken_recap_details: list[dict[str, Any]] = []

    # 1) 消费账 (expense_ledger.transactions)
    exp_ledger = daily_payload.get("expense_ledger") or {}
    transactions = exp_ledger.get("transactions") if isinstance(exp_ledger, Mapping) else []
    for tx in transactions if isinstance(transactions, list) else []:
        if not isinstance(tx, Mapping):
            continue
        eid = str(tx.get("event_id") or "").strip()
        title = str(tx.get("title") or tx.get("item") or "")
        quote = str(tx.get("quote") or "")
        amount = tx.get("amount")
        decision = str(tx.get("decision") or "")

        is_ui_manual = (
            eid.startswith("manual_")
            or eid in day_manuals
            or str(tx.get("source_type") or "") in {"manual", "user_supplement"}
            or decision == "pending_broadcast_accept"
        )
        if is_ui_manual:
            manual_counts["expense"] += 1
            manual_details.append({
                "ledger": "expense",
                "event_id": eid,
                "title": title,
                "amount": amount,
                "basis": "manual_event_id_or_source_type",
            })
        else:
            auto_counts["expense"] += 1
            auto_details.append({
                "ledger": "expense",
                "event_id": eid,
                "title": title,
                "amount": amount,
                "quote": quote,
            })
            jev_meta = (jev_items.get(eid) or {}).get("_activity") or {}
            if _is_spoken_recap_text(
                title,
                quote,
                jev_meta.get("title"),
                jev_meta.get("summary"),
                eid if "report" in eid else "",
            ):
                spoken_recap_counts["expense"] += 1
                spoken_recap_details.append({
                    "ledger": "expense",
                    "event_id": eid,
                    "title": title,
                    "amount": amount,
                    "quote": quote,
                })

    # 检查 ledger-review.json 中对当天自动消费记录的确认修改/排除
    for eid, dec in sorted(ledger_review_decisions.items()):
        dec_date = str(dec.get("date") or "")[:10]
        if dec_date != day:
            continue
        # 仅统计针对自动记录（非 manual_evt_*）的修改或删除
        if eid.startswith("manual_") or eid in day_manuals:
            continue
        action = str(dec.get("action") or "")
        if action in {"confirm", "exclude"}:
            modified_counts["expense"] += 1
            jev_orig = jev_items.get(eid) or {}
            modified_details.append({
                "ledger": "expense",
                "event_id": eid,
                "action": action,
                "label": dec.get("label"),
                "amount": dec.get("amount"),
                "original_pending_reason": jev_orig.get("pending_reason"),
                "source": "output/local-web/ledger-review.json",
                "decided_at": dec.get("decided_at"),
            })

    # 2) 饮食账 (nutrition_ledger.food_records & other_records)
    nut_ledger = daily_payload.get("nutrition_ledger") or {}
    food_records = nut_ledger.get("food_records") if isinstance(nut_ledger, Mapping) else []
    other_records = nut_ledger.get("other_records") if isinstance(nut_ledger, Mapping) else []

    for rec in food_records if isinstance(food_records, list) else []:
        if not isinstance(rec, Mapping) or not rec.get("is_consumed"):
            continue
        eid = str(rec.get("event_id") or "").strip()
        item_name = str(rec.get("item") or "")
        title = str(rec.get("title") or "")
        quote = str(rec.get("quote") or "")
        origin = str(rec.get("origin") or "")
        est_basis = str(rec.get("estimate_basis") or "")
        nut_src = str(rec.get("nutrition_source") or "")
        user_confirmed = bool(rec.get("user_confirmed"))
        recap_of = str(rec.get("recap_of") or "")

        is_ui_or_recap_manual = (
            user_confirmed
            or est_basis in {"user_adopted_candidate", "user_weight"}
            or nut_src in {"user_judgment", "user_statement"}
            or eid.startswith("manual_")
            or eid.startswith("recap:")
            or bool(recap_of)
            or eid in day_manuals
            or (origin == "event" and not eid.startswith("activity:"))
        )
        if is_ui_or_recap_manual:
            manual_counts["nutrition"] += 1
            manual_details.append({
                "ledger": "nutrition",
                "event_id": eid,
                "item": item_name,
                "estimate_basis": est_basis,
                "nutrition_source": nut_src,
                "user_confirmed": user_confirmed,
            })
        else:
            auto_counts["nutrition"] += 1
            auto_details.append({
                "ledger": "nutrition",
                "event_id": eid,
                "item": item_name,
                "calories_kcal": rec.get("calories_kcal"),
                "estimate_basis": est_basis,
                "quote": quote,
            })
            if _is_spoken_recap_text(title, quote, (jev_items.get(eid) or {}).get("_activity", {}).get("summary")):
                spoken_recap_counts["nutrition"] += 1
                spoken_recap_details.append({
                    "ledger": "nutrition",
                    "event_id": eid,
                    "item": item_name,
                    "quote": quote,
                })

    # 检查饮食账中被用户采纳替换 (superseded_by_user_adoption) 或点“不算”跳过 (skipped_by_user) 的自动记录
    seen_nut_mod_ids: set[str] = set()
    for rec in list(food_records or []) + list(other_records or []):
        if not isinstance(rec, Mapping):
            continue
        eid = str(rec.get("event_id") or "").strip()
        if not eid or eid in seen_nut_mod_ids:
            continue
        if rec.get("superseded_by_user_adoption") or rec.get("skipped_by_user"):
            seen_nut_mod_ids.add(eid)
            modified_counts["nutrition"] += 1
            modified_details.append({
                "ledger": "nutrition",
                "event_id": eid,
                "item": rec.get("item"),
                "action": "superseded_by_user_adoption" if rec.get("superseded_by_user_adoption") else "skipped_by_user",
                "source": "nutrition_ledger.other_records / review-questions.sqlite3",
            })

    # 3) 运动账 (fitness_ledger.workouts)
    fit_ledger = daily_payload.get("fitness_ledger") or {}
    workouts = fit_ledger.get("workouts") if isinstance(fit_ledger, Mapping) else []
    for wk in workouts if isinstance(workouts, list) else []:
        if not isinstance(wk, Mapping):
            continue
        eid = str(wk.get("event_id") or "").strip()
        origin = str(wk.get("origin") or "")
        title = str(wk.get("title") or "")
        quote = str(wk.get("quote") or "")
        is_manual = (
            eid.startswith("manual_")
            or eid in day_manuals
            or (origin != "activity" and not eid.startswith("activity:"))
        )
        if is_manual:
            manual_counts["fitness"] += 1
            manual_details.append({
                "ledger": "fitness",
                "event_id": eid,
                "title": title,
            })
        else:
            auto_counts["fitness"] += 1
            auto_details.append({
                "ledger": "fitness",
                "event_id": eid,
                "title": title,
                "quote": quote,
            })
            if _is_spoken_recap_text(title, quote):
                spoken_recap_counts["fitness"] += 1
                spoken_recap_details.append({
                    "ledger": "fitness",
                    "event_id": eid,
                    "title": title,
                    "quote": quote,
                })

    # 4) 吸烟账 (smoking_ledger.sessions where action == "smoke")
    smk_ledger = daily_payload.get("smoking_ledger") or {}
    sessions = smk_ledger.get("sessions") if isinstance(smk_ledger, Mapping) else []
    for sess in sessions if isinstance(sessions, list) else []:
        if not isinstance(sess, Mapping):
            continue
        if str(sess.get("action") or "smoke") != "smoke":
            continue
        eid = str(sess.get("event_id") or "").strip()
        origin = str(sess.get("origin") or "")
        title = str(sess.get("title") or "")
        quote = str(sess.get("quote") or "")
        is_manual = (
            eid.startswith("manual_")
            or eid in day_manuals
            or (origin != "activity" and not eid.startswith("activity:"))
        )
        if is_manual:
            manual_counts["smoking"] += 1
            manual_details.append({
                "ledger": "smoking",
                "event_id": eid,
                "title": title,
            })
        else:
            auto_counts["smoking"] += 1
            auto_details.append({
                "ledger": "smoking",
                "event_id": eid,
                "title": title,
                "quote": quote,
            })
            if _is_spoken_recap_text(title, quote):
                spoken_recap_counts["smoking"] += 1
                spoken_recap_details.append({
                    "ledger": "smoking",
                    "event_id": eid,
                    "title": title,
                    "quote": quote,
                })

    # 加上当天 *.corrections.json / broadcast-decisions/<day>.json 的纠偏记录
    extra_mods = _load_day_corrections(root, day)
    for mod in extra_mods:
        mod_id = mod["id"]
        if any(d.get("event_id") == mod_id for d in modified_details):
            continue
        ledger_key = mod["ledger"] if mod["ledger"] in modified_counts else "expense"
        modified_counts[ledger_key] += 1
        modified_details.append({
            "ledger": ledger_key,
            "event_id": mod_id,
            "action": mod["action"],
            "source": mod["source"],
        })

    return {
        "auto_entries": {
            **auto_counts,
            "total": sum(auto_counts.values()),
            "spoken_recap_in_auto": {
                **spoken_recap_counts,
                "total": sum(spoken_recap_counts.values()),
                "items": spoken_recap_details,
            },
            "items": auto_details,
        },
        "manual_entries": {
            **manual_counts,
            "total": sum(manual_counts.values()),
            "items": manual_details,
            "distinguishability": {
                "overall": "部分区分不了（UI手动/判断队列补录可区分，但手表录音内的复盘口述与自动抓取完全混同，缺字段区分不了）",
                "expense": "账本条目的 source_type / evidence.source / user_confirmed 在 expense_policy._record_to_transaction 中被剥离；仅能靠 event_id 前缀(manual_evt_*)或反查 jobs/*/manual-events.json 识别界面补录；录音内复盘口述（如 11-08 22:17 补报买烟、11-09 00:25 口述补充三笔消费）区分不了。",
                "nutrition": "界面/待判断补重保留了 user_confirmed=True、estimate_basis in {user_adopted_candidate, user_weight}、nutrition_source in {user_judgment, user_statement}，隔日口述复盘有 recap_of=next_day_statement；但同日手表录音里的主动口述汇总（如 11-08 22:14 集中口述三项全天饮食）仍是 origin=activity、user_confirmed=False，区分不了。",
                "fitness": "build_fitness_ledger 未保留 source_type / evidence.source；界面手动事件仅能靠 event_id=manual_evt_* 及无 origin=activity 区分；录音内口述回顾（如 11-06 22:11 回顾昨天练胸）区分不了。",
                "smoking": "build_smoking_ledger 未保留 source_type / evidence.source；界面手动事件仅能靠 event_id=manual_evt_* 及无 origin=activity 区分；录音内口述回顾（如 11-06 回顾刚才抽了三根烟）区分不了。",
                "missing_fields_to_add": [
                    "所有四本账输出条目统一保留 entry_origin: 'auto_live' | 'spoken_recap' | 'ui_manual' | 'judgment_queue'",
                    "activity-timeline 抽取层 (jev/<day>.json item) 增加 reporting_mode: 'live_scene' | 'retrospective_recap' 及 occurred_date",
                    "expense_policy._Record 与 _record_to_transaction 保留 source_type、evidence.source、user_confirmed、review_action",
                    "daily_cognitive_session.build_fitness_ledger / build_smoking_ledger 保留 source_type、evidence.source、user_confirmed",
                ],
            },
        },
        "modified_or_deleted_auto": {
            **modified_counts,
            "total": sum(modified_counts.values()),
            "items": modified_details,
        },
    }


def _load_review_question_states_ro(root: Path) -> dict[str, dict[str, Any]]:
    """只读打开 review-questions.sqlite3 读取 question_state。"""
    db_path = root / "review-questions" / "review-questions.sqlite3"
    if not db_path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT source_id, state, reason, updated_at FROM question_state"
            ).fetchall()
            return {
                str(sid): {"state": str(st or ""), "reason": str(rsn or ""), "updated_at": upd}
                for sid, st, rsn, upd in rows
            }
        finally:
            conn.close()
    except Exception:
        return {}


def _analyze_judgment_queue(
    root: Path,
    days: list[str],
    daily_payloads: Mapping[str, Mapping[str, Any]],
    *,
    base_url: str = DEFAULT_BASE_URL,
    token: str | None = None,
    offline: bool = False,
) -> dict[str, dict[str, Any]]:
    """计算每天待判断队列：还有多少条 (open_count)、当天新进了多少条 (new_count)。"""
    api_status = "offline"
    if not offline:
        code, payload, err = _http_get_json(base_url, "/api/judgment-queue", token=token)
        if code == 200 and isinstance(payload, Mapping):
            api_status = "ok"
        else:
            api_status = err or f"HTTP {code}"

    rq_states = _load_review_question_states_ro(root)

    try:
        from life_logger import judgment_queue, pending_review
        from life_logger.activity_prompt_v3 import _A123_TOOL, is_agent_or_work_todo_text

        records = pending_review.load_review_states(root)["records"]
        all_items_map, _, _ = pending_review._collect_pending_items(root, records)
        all_items = list(all_items_map.values())
        target_days = set(days)
        resolved_sids, resolved_quotes = judgment_queue._resolved_media_or_work_evidence(root, target_days)
    except Exception:
        all_items = []
        resolved_sids, resolved_quotes = set(), {}

        def is_agent_or_work_todo_text(_t: str) -> bool:
            return False

        _A123_TOOL = re.compile(r"$^")

    by_day_items: dict[str, list[dict[str, Any]]] = {d: [] for d in days}
    for it in all_items:
        d = str(it.get("date") or "")
        if d in by_day_items:
            by_day_items[d].append(dict(it))

    results: dict[str, dict[str, Any]] = {}
    for d in days:
        day_all = by_day_items.get(d, [])
        raw_needs_review = [it for it in day_all if it.get("group") == "needs_review"]
        batch_failures = [it for it in day_all if it.get("group") == "batch_failure"]

        # 按 judgment_queue._open_items 完全一致的去噪规则筛选当天进入判断队列的条目
        # 注意：为了算“当天新进了多少条”，我们不仅看 state=="open"，也包含后来已被 resolved/answered 的条目
        quote_counts = Counter(
            (d, str(it.get("quote") or "").strip())
            for it in raw_needs_review
            if len(str(it.get("quote") or "").strip()) >= 6
        )
        repeated_loop_quotes = {dq for dq, cnt in quote_counts.items() if cnt >= 3}

        entered_needs_review: list[dict[str, Any]] = []
        open_needs_review: list[dict[str, Any]] = []

        for item in raw_needs_review:
            try:
                from life_logger.judgment_queue import SCENE, TODO, VOICEPRINT, purpose_for_reason

                purpose = purpose_for_reason(item.get("reason"))
            except Exception:
                purpose = "scene"
                TODO, SCENE, VOICEPRINT = "todo", "scene", "voiceprint"

            quote = str(item.get("quote") or "").strip()
            sid = str(item.get("source_id") or "").strip()
            if purpose == TODO and is_agent_or_work_todo_text(quote):
                continue
            if purpose in {SCENE, VOICEPRINT, TODO}:
                if (d, quote) in repeated_loop_quotes:
                    continue
                if is_agent_or_work_todo_text(quote) or bool(_A123_TOOL.search(quote)):
                    continue
                if sid and sid in resolved_sids:
                    continue
                if len(quote) >= 4 and any(
                    mq in quote or quote in mq for mq in resolved_quotes.get(d, ())
                ):
                    continue

            entered_needs_review.append(item)
            pr_open = str(item.get("state") or "open") == "open"
            rq_state = (rq_states.get(sid) or {}).get("state", "")
            if pr_open and rq_state not in {"answered", "muted"}:
                open_needs_review.append(item)

        # 热量待定候选（来自当天 nutrition_ledger.unweighed_items 且带有效候选千卡，或已被用户在 cal|<day>|* 回答过）
        nut_ledger = (daily_payloads.get(d) or {}).get("nutrition_ledger") or {}
        unweighed = nut_ledger.get("unweighed_items") if isinstance(nut_ledger, Mapping) else []
        open_cal_ids: set[str] = set()
        entered_cal_ids: set[str] = set()
        for row in unweighed if isinstance(unweighed, list) else []:
            if not isinstance(row, Mapping):
                continue
            cand = row.get("portion_estimate_candidate")
            if not isinstance(cand, Mapping):
                continue
            try:
                kcal = float(cand.get("calories_kcal"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(kcal) or kcal <= 0:
                continue
            ev_id = str(row.get("event_id") or "").strip()
            if not ev_id:
                continue
            cal_sid = f"cal|{d}|{ev_id}"
            entered_cal_ids.add(cal_sid)
            rq_state = (rq_states.get(cal_sid) or {}).get("state", "")
            if rq_state not in {"answered", "muted"}:
                open_cal_ids.add(cal_sid)

        # 如果有当天已被回答/跳过的 cal|<day>|*（已从 unweighed_items 移除），也计入“当天新进”
        cal_prefix = f"cal|{d}|"
        for sid, st_info in rq_states.items():
            if sid.startswith(cal_prefix):
                entered_cal_ids.add(sid)
                if st_info.get("state") not in {"answered", "muted"}:
                    open_cal_ids.add(sid)

        open_count = len(open_needs_review) + len(open_cal_ids)
        new_count = len(entered_needs_review) + len(entered_cal_ids)

        results[d] = {
            "open_count": open_count,
            "new_count": new_count,
            "open_needs_review": len(open_needs_review),
            "open_calories": len(open_cal_ids),
            "new_needs_review": len(entered_needs_review),
            "new_calories": len(entered_cal_ids),
            "raw_needs_review_before_noise_filter": len(raw_needs_review),
            "batch_failure_count": len(batch_failures),
            "api_status": api_status,
        }
    return results


def _analyze_recording_and_asr(
    root: Path,
    days: list[str],
    *,
    base_url: str = DEFAULT_BASE_URL,
    token: str | None = None,
    offline: bool = False,
) -> dict[str, dict[str, Any]]:
    """统计每天录音时长（分钟）、转写段数、转写失败/空段数。"""
    target_days = set(days)
    ws_dir = root / "watch-sessions"

    # 1) 优先尝试 GET /api/watch-coverage?date=YYYY-MM-DD 获取去重覆盖时长
    api_covered_minutes: dict[str, float] = {}
    if not offline:
        for d in days:
            code, payload, _ = _http_get_json(
                base_url,
                f"/api/watch-coverage?date={urllib.parse.quote(d)}",
                token=token,
            )
            if code == 200 and isinstance(payload, Mapping):
                cov = payload.get("coverage") if isinstance(payload.get("coverage"), Mapping) else payload
                if isinstance(cov, Mapping) and isinstance(cov.get("covered_minutes"), (int, float)):
                    api_covered_minutes[d] = round(float(cov["covered_minutes"]), 1)

    # 2) 扫描 watch-sessions/*/*/manifest.json 与 formal-asr/segments/*/state.json
    day_intervals: dict[str, list[tuple[datetime, datetime]]] = {d: [] for d in days}
    seg_stats: dict[str, Counter[str]] = {d: Counter() for d in days}

    if ws_dir.is_dir():
        min_target = date.fromisoformat(days[0]) - timedelta(days=2)
        max_target = date.fromisoformat(days[-1]) + timedelta(days=2)

        for manifest_path in sorted(ws_dir.glob("*/*/manifest.json")):
            manifest = _read_json_safe(manifest_path)
            if not isinstance(manifest, Mapping):
                continue
            s_dt = _parse_iso(manifest.get("started_at"))
            if s_dt is None:
                continue

            raw_segments = [
                item for item in (manifest.get("segments") or []) if isinstance(item, Mapping)
            ]
            if raw_segments:
                max_end_ms = max(
                    int(item.get("start_offset_ms") or 0) + int(item.get("duration_ms") or 0)
                    for item in raw_segments
                )
                e_dt = s_dt + timedelta(milliseconds=max(0, max_end_ms))
            else:
                e_dt = _parse_iso(manifest.get("ended_at"))

            if e_dt and (e_dt.date() < min_target or s_dt.date() > max_target):
                continue

            session_dir = manifest_path.parent
            seg_root = session_dir / "formal-asr" / "segments"

            if raw_segments:
                for item in raw_segments:
                    try:
                        seq = int(item.get("sequence"))
                        offset_ms = int(item.get("start_offset_ms") or 0)
                        dur_ms = int(item.get("duration_ms") or 0)
                    except (TypeError, ValueError):
                        continue
                    seg_s = s_dt + timedelta(milliseconds=max(0, offset_ms))
                    seg_e = seg_s + timedelta(milliseconds=max(0, dur_ms))
                    if seg_e > seg_s:
                        c_day = seg_s.date()
                        while c_day <= seg_e.date():
                            d_str = c_day.isoformat()
                            if d_str in target_days:
                                w_s = datetime.combine(c_day, time.min, tzinfo=BEIJING)
                                w_e = w_s + timedelta(days=1)
                                clip_s = max(seg_s, w_s)
                                clip_e = min(seg_e, w_e)
                                if clip_e > clip_s:
                                    day_intervals[d_str].append((clip_s, clip_e))
                            c_day += timedelta(days=1)

                    seg_day = seg_s.date().isoformat()
                    if seg_day not in target_days:
                        continue
                    state_path = seg_root / f"{seq:06d}" / "state.json"
                    st = _read_json_safe(state_path)
                    status = (
                        str(st.get("status") or "pending").strip()
                        if isinstance(st, Mapping)
                        else "pending"
                    )
                    seg_stats[seg_day]["total"] += 1
                    if status == "completed":
                        seg_stats[seg_day]["completed"] += 1
                    elif status == "no_speech":
                        seg_stats[seg_day]["empty"] += 1
                    elif status == "failed":
                        seg_stats[seg_day]["failed"] += 1
                    elif status in {"pending", "processing"}:
                        seg_stats[seg_day]["pending"] += 1
                    else:
                        seg_stats[seg_day]["other"] += 1
            else:
                if e_dt and e_dt > s_dt:
                    cur_day = s_dt.date()
                    while cur_day <= e_dt.date():
                        d_str = cur_day.isoformat()
                        if d_str in target_days:
                            w_start = datetime.combine(cur_day, time.min, tzinfo=BEIJING)
                            w_end = w_start + timedelta(days=1)
                            clip_s = max(s_dt, w_start)
                            clip_e = min(e_dt, w_end)
                            if clip_e > clip_s:
                                day_intervals[d_str].append((clip_s, clip_e))
                        cur_day += timedelta(days=1)

                if seg_root.is_dir():
                    for state_path in sorted(seg_root.glob("*/state.json")):
                        st = _read_json_safe(state_path)
                        if not isinstance(st, Mapping):
                            continue
                        seg_day = _beijing_date_str(
                            st.get("started_at") or st.get("ended_at") or manifest.get("started_at")
                        )
                        if seg_day not in target_days:
                            continue
                        status = str(st.get("status") or "unknown").strip()
                        seg_stats[seg_day]["total"] += 1
                        if status == "completed":
                            seg_stats[seg_day]["completed"] += 1
                        elif status == "no_speech":
                            seg_stats[seg_day]["empty"] += 1
                        elif status == "failed":
                            seg_stats[seg_day]["failed"] += 1
                        elif status in {"pending", "processing"}:
                            seg_stats[seg_day]["pending"] += 1
                        else:
                            seg_stats[seg_day]["other"] += 1

    results: dict[str, dict[str, Any]] = {}
    for d in days:
        intervals = sorted(day_intervals.get(d, []), key=lambda x: x[0])
        raw_sec = sum((e - s).total_seconds() for s, e in intervals)
        merged: list[list[datetime]] = []
        for s, e in intervals:
            if not merged or s > merged[-1][1]:
                merged.append([s, e])
            else:
                merged[-1][1] = max(merged[-1][1], e)
        merged_sec = sum((e - s).total_seconds() for s, e in merged)
        raw_min = round(raw_sec / 60.0, 1)
        cov_min = api_covered_minutes.get(d, round(merged_sec / 60.0, 1))

        cnt = seg_stats[d]
        total_segs = cnt["total"]
        completed_segs = cnt["completed"]
        failed_segs = cnt["failed"]
        empty_segs = cnt["empty"]
        pending_segs = cnt["pending"]

        results[d] = {
            "recording_minutes": raw_min if raw_min > 0 else cov_min,
            "covered_minutes": cov_min,
            "segment_total": total_segs,
            "segment_completed": completed_segs,
            "segment_failed": failed_segs,
            "segment_empty": empty_segs,
            "segment_pending": pending_segs,
            "failed_or_empty_total": failed_segs + empty_segs,
            "source": (
                f"GET /api/watch-coverage?date={d} + watch-sessions/*/*/formal-asr/segments/*/state.json"
                if d in api_covered_minutes
                else "watch-sessions/*/*/manifest.json + formal-asr/segments/*/state.json"
            ),
        }
    return results


def compute_daily_health(
    root: Path | str = DEFAULT_ROOT,
    *,
    date_from: str,
    date_to: str,
    base_url: str = DEFAULT_BASE_URL,
    offline: bool = False,
) -> dict[str, Any]:
    """主计算入口：返回完整的每日体检数据结构（供 --json 与表格渲染共用）。"""
    root_path = Path(root)
    days = _date_range(date_from, date_to)
    token = None if offline else _read_token_silently(root_path)

    manual_events_by_day = _collect_manual_events_by_day(root_path)
    ledger_review_decisions = _load_ledger_review_decisions(root_path)

    daily_payloads: dict[str, dict[str, Any]] = {}
    daily_sources: dict[str, str] = {}
    for d in days:
        payload, src = _load_ledgers_for_day(
            root_path,
            d,
            day_manual_events=list(manual_events_by_day.get(d, {}).values()),
            base_url=base_url,
            token=token,
            offline=offline,
        )
        daily_payloads[d] = payload
        daily_sources[d] = src

    jq_by_day = _analyze_judgment_queue(
        root_path, days, daily_payloads, base_url=base_url, token=token, offline=offline
    )
    asr_by_day = _analyze_recording_and_asr(
        root_path, days, base_url=base_url, token=token, offline=offline
    )

    rows: list[dict[str, Any]] = []
    for d in days:
        ledger_info = _analyze_ledgers_for_day(
            root_path, d, daily_payloads[d], manual_events_by_day, ledger_review_decisions
        )
        jq_info = jq_by_day[d]
        asr_info = asr_by_day[d]

        c1_auto = ledger_info["auto_entries"]["total"]
        c2_manual = ledger_info["manual_entries"]["total"]
        c3_mod = ledger_info["modified_or_deleted_auto"]["total"]
        c4_new = jq_info["new_count"]

        denom = c1_auto + c2_manual
        num = c2_manual + c3_mod
        ratio = round(num / denom, 4) if denom > 0 else None
        ratio_str = f"{ratio * 100:.1f}% ({num}/{denom})" if ratio is not None else f"N/A ({num}/0)"
        worry_count = c2_manual + c3_mod + c4_new

        # 补充：如果把录音内口述复盘补报 (spoken_recap_in_auto) 从 1(自动) 移入 2(手动补录) 的体感指标
        recap_cnt = ledger_info["auto_entries"]["spoken_recap_in_auto"]["total"]
        num_with_recap = (c2_manual + recap_cnt) + c3_mod
        ratio_with_recap = round(num_with_recap / denom, 4) if denom > 0 else None
        ratio_with_recap_str = (
            f"{ratio_with_recap * 100:.1f}% ({num_with_recap}/{denom})"
            if ratio_with_recap is not None
            else f"N/A ({num_with_recap}/0)"
        )
        worry_with_recap = (c2_manual + recap_cnt) + c3_mod + c4_new

        rows.append({
            "date": d,
            "ledger_source": daily_sources[d],
            "auto_entries": ledger_info["auto_entries"],
            "manual_entries": ledger_info["manual_entries"],
            "modified_or_deleted_auto": ledger_info["modified_or_deleted_auto"],
            "judgment_queue": jq_info,
            "recording_and_asr": asr_info,
            "derived": {
                "correction_ratio": ratio,
                "correction_ratio_display": ratio_str,
                "worry_count": worry_count,
                "correction_ratio_with_spoken_recap": ratio_with_recap,
                "correction_ratio_with_spoken_recap_display": ratio_with_recap_str,
                "worry_count_with_spoken_recap": worry_with_recap,
            },
        })

    return {
        "schema": "daily-health-check-v1",
        "timezone": "Asia/Shanghai",
        "from": date_from,
        "to": date_to,
        "days": rows,
    }


def format_table(report: Mapping[str, Any]) -> str:
    """将体检结果格式化为中文对齐表格与可读说明。"""
    lines: list[str] = []
    lines.append(
        f"=== 每日体检报告（北京时间 {report.get('from')} 至 {report.get('to')}） ==="
    )
    header = (
        "| 日期 | 1.自动入账(消/饮/运/烟=合) | 2.手动补录(UI可识别 / 录音口述难分) "
        "| 3.改/删自动账 | 4.待确认(剩余/新进) | 5.录音与转写(时长/总段/有效/失败/空段) "
        "| 被纠正比例 (2+3)/(1+2) | 操心次数 2+3+4新进 |"
    )
    sep = (
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
    )
    lines.append(header)
    lines.append(sep)

    for row in report.get("days") or []:
        d = row["date"]
        a = row["auto_entries"]
        m = row["manual_entries"]
        mod = row["modified_or_deleted_auto"]
        jq = row["judgment_queue"]
        asr = row["recording_and_asr"]
        drv = row["derived"]

        c1_str = f"{a['expense']}/{a['nutrition']}/{a['fitness']}/{a['smoking']}={a['total']}笔"
        recap_n = a["spoken_recap_in_auto"]["total"]
        c2_str = f"{m['total']}笔 (另有口述补报{recap_n}笔区分不了)"
        c3_str = f"{mod['total']}笔"
        c4_str = f"剩{jq['open_count']} / 新{jq['new_count']}"
        if jq.get("batch_failure_count", 0) > 0:
            c4_str += f" (另整批失败{jq['batch_failure_count']})"
        c5_str = (
            f"{asr['recording_minutes']:.1f}m / {asr['segment_total']}段 "
            f"(有效{asr['segment_completed']}/败{asr['segment_failed']}/空{asr['segment_empty']})"
        )
        ratio_str = drv["correction_ratio_display"]
        if recap_n > 0:
            ratio_str += f" [含口述:{drv['correction_ratio_with_spoken_recap_display']}]"
        worry_str = f"{drv['worry_count']}次"
        if recap_n > 0:
            worry_str += f" [含口述:{drv['worry_count_with_spoken_recap']}次]"

        lines.append(
            f"| {d} | {c1_str} | {c2_str} | {c3_str} | {c4_str} | {c5_str} | {ratio_str} | {worry_str} |"
        )

    lines.append("")
    lines.append("【口径与缺失说明】")
    lines.append(
        "- 1 & 2 自动 vs 手动区分现状：仅 UI 手动录入/待判断入账（event_id=manual_evt_*、nutrition 的 user_confirmed=True）能靠现有结构识别；"
        "手表录音里的「主动复盘口述/口头补报」（如 11-08 晚集中口述三笔饮食+买烟、11-09 凌晨口述补充三笔消费）在 jev/<day>.json 中全部是 origin=activity，缺 reporting_mode/entry_origin 字段，与被动自动入账【区分不了】。"
    )
    lines.append(
        "- 3 改/删自动账：汇总自 output/local-web/ledger-review.json、nutrition_ledger(superseded_by_user_adoption/skipped_by_user)、activity-timeline/jev/*.corrections.json、broadcast-decisions/*.json。"
    )
    lines.append(
        "- 4 待你确认：汇总自 pending_review(needs_review 经 judgment_queue._open_items 过滤后) + judgment_queue.calorie_items + review-questions.sqlite3。"
    )
    if any(
        str((r.get("judgment_queue") or {}).get("api_status") or "").startswith("HTTP 400")
        for r in (report.get("days") or [])
    ):
        api_err = next(
            (r["judgment_queue"]["api_status"] for r in report["days"] if str(r["judgment_queue"].get("api_status")).startswith("HTTP 400")),
            "",
        )
        lines.append(
            f"- [告警] 线上 GET /api/judgment-queue 当前返回 {api_err}（因 2026-11-04 活动条目 amount='一百八十二亿' 触发 float() 未捕获异常），本脚本已自动回退只读解析本地文件。"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每日体检只读统计脚本")
    parser.add_argument(
        "--from",
        dest="date_from",
        default="2026-11-06",
        help="起始日期 YYYY-MM-DD（默认 2026-11-06）",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        default=datetime.now(BEIJING).date().isoformat(),
        help="结束日期 YYYY-MM-DD（默认今天，北京时间）",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="输出 JSON 格式",
    )
    parser.add_argument(
        "--root",
        dest="root",
        default=str(DEFAULT_ROOT),
        help="local-web 数据目录路径（默认 output/local-web）",
    )
    parser.add_argument(
        "--base-url",
        dest="base_url",
        default=DEFAULT_BASE_URL,
        help="本地只读服务地址（默认 http://127.0.0.1:8765）",
    )
    parser.add_argument(
        "--offline",
        dest="offline",
        action="store_true",
        help="仅只读打开本地文件计算，不请求 HTTP 接口",
    )
    args = parser.parse_args(argv)

    report = compute_daily_health(
        Path(args.root),
        date_from=args.date_from,
        date_to=args.date_to,
        base_url=args.base_url,
        offline=args.offline,
    )
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_table(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
