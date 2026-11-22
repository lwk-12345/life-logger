"""Publish a frozen, user-approved activity replay without changing the ledger."""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path


def build_payload(source: Path) -> dict:
    manifest = json.loads((source / "manifest.json").read_text())
    raw = (source / "input.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["input_sha256"]:
        raise ValueError("Frozen input hash mismatch")
    rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    index = {
        tuple(str(row.get(k, "")) for k in ("source_id", "segment_id", "row_id")): row
        for row in rows
    }
    result = json.loads((source / "results.json").read_text())
    summary = json.loads((source / "summary.json").read_text())
    if summary.get("status") != "complete" or summary.get("failed"):
        raise ValueError("Replay is incomplete or contains failed windows")
    activities = copy.deepcopy(result["activities"])
    corrections_path = source / "publication-corrections.json"
    corrections = {}
    if corrections_path.exists():
        corrections = json.loads(corrections_path.read_text())
        if corrections.get("results_sha256") != hashlib.sha256((source / "results.json").read_bytes()).hexdigest():
            raise ValueError("Publication corrections refer to a different replay")
    for change in corrections.get("items", []):
        matches = [item for activity in activities if activity.get("activity_id") == change["activity_id"]
                   for item in activity.get("items", []) if item.get("item_id") == change["item_id"]]
        if len(matches) != 1 or matches[0].get("amount") != change["original_amount"]:
            raise ValueError("Publication correction does not match its source item")
        item = matches[0]
        item["model_amount_before_review"] = item.get("amount")
        item["amount"] = None
        item.setdefault("uncertainties", []).append(change["reason"])
    for activity in activities:
        target, category = classify_activity_target_and_category(activity)
        activity.setdefault("speech_target", target)
        activity.setdefault("category", category)
        known_times = [item["occurred_at"] for item in activity.get("items", []) if item.get("occurred_at")]
        if not known_times:
            # A reported-at range cannot establish an unknown occurrence range.
            activity["occurrence_start"] = None
            activity["occurrence_end"] = None
        else:
            activity["occurrence_start"] = min(known_times, key=datetime.fromisoformat)
            activity["occurrence_end"] = max(known_times, key=datetime.fromisoformat)
    unassociated = {}
    conflicts = []
    for step in result.get("steps", []):
        response = step.get("response") or {}
        for item in response.get("unassociated_items", []):
            # Only a stable model ID and the same source coordinates permit upsert.
            key = (str(item.get("item_id")), tuple(sorted(
                tuple(str(e.get(k, "")) for k in ("source_id", "segment_id", "row_id"))
                for e in item.get("evidence", [])
            )))
            unassociated[key] = copy.deepcopy(item)
        conflicts.extend(copy.deepcopy(response.get("conflicts", [])))
    for item in [i for a in activities for i in a.get("items", [])] + list(unassociated.values()):
        if not item.get("evidence"):
            raise ValueError("Activity item has no source evidence")
        for evidence in item["evidence"]:
            key = tuple(str(evidence.get(k, "")) for k in ("source_id", "segment_id", "row_id"))
            row = index.get(key)
            if row is None or not evidence.get("quote") or evidence["quote"] not in row["text"]:
                raise ValueError(f"Invalid source reference: {key}")
    processed_sources = set()
    frozen_row_ids = {(row.get("recording_id"), row.get("segment_id"), row.get("text")) for row in rows}
    for path in (source / "frozen").rglob("transcript.json"):
        for row in json.loads(path.read_text()):
            if not isinstance(row, dict):
                continue
            if (row.get("recording_id"), row.get("segment_id"), row.get("text")) not in frozen_row_ids:
                continue
            instant = row.get("absolute_started_at")
            if not instant:
                continue
            if datetime.fromisoformat(instant.replace("Z", "+00:00")) > datetime.fromisoformat(manifest["snapshot_cutoff"]):
                continue
            identity = f"{row.get('recording_id') or ''}:{row.get('segment_id') or ''}:{str(row.get('start_offset_seconds') or 0.0)}:{str(row.get('text') or '').strip()}"
            processed_sources.add("src_" + hashlib.sha256(identity.encode()).hexdigest()[:16])
    return {
        "schema": "activity-timeline-v1", "date": manifest["date"],
        "timezone": "Asia/Shanghai", "cadence_minutes": 10, "context_minutes": 30,
        "route": manifest["route"], "status": "ready", "activities": activities,
        "unassociated_items": list(unassociated.values()), "conflicts": conflicts,
        "coverage": {"through": manifest["snapshot_cutoff"], "source_count": len(rows),
                     "basis": "processed-windows-not-all-utterances-are-events"},
        "errors": [], "source_manifest": str(source / "manifest.json"),
        "input_sha256": manifest["input_sha256"], "origin": "user-approved-replay",
        "publication_corrections": corrections.get("items", []),
        "metadata": {"origin": "user-approved-replay", "source_manifest": str(source / "manifest.json"),
                     "input_sha256": manifest["input_sha256"], "publication_corrections": corrections.get("items", []),
                     "processed_source_ids": sorted(processed_sources)},
    }


# ── 跨进程写锁 ────────────────────────────────────────────────────────────
# 与 activity_timeline_runtime.tick() **同一把锁、同一路径**：
#   <output_root>/activity-timeline/.runtime.lock  +  fcntl.flock(LOCK_EX)
# tick 是 LOCK_NB（拿不到就跳过本轮）；本脚本是「必须写完」的工具，所以带超时等待。
LOCK_TIMEOUT_SECONDS = 30.0


def acquire_runtime_lock(lock_file: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
    """拿 tick 的那把锁；超时抛 TimeoutError（调用方必须报错退出，不许静默继续）。"""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_file, "a+")
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError(f"等待 .runtime.lock 超过 {timeout:.1f}s")
            time.sleep(0.05)


def release_runtime_lock(handle) -> None:
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


def _evidence_key(item: dict) -> tuple:
    return (str(item.get("item_id")),
            tuple(sorted(tuple(str(e.get(k, "")) for k in ("source_id", "segment_id", "row_id"))
                         for e in item.get("evidence", []))))


def merge_into_day(fresh: dict, preview: dict) -> tuple[dict, dict]:
    """把 replay payload 合并进**锁内重新读到的最新** day payload。

    不整份覆盖：fresh 里被别人/被 tick 写进去的活动、未关联项、覆盖进度、metadata 全部保留；
    preview 的同 ID 活动/同证据未关联项做 upsert，其余追加。
    """
    merged = copy.deepcopy(fresh) if isinstance(fresh, dict) else {}
    merged.setdefault("schema", preview.get("schema", "activity-timeline-v1"))
    merged["date"] = preview.get("date") or merged.get("date")
    merged["route"] = preview.get("route") or merged.get("route")
    for key in ("timezone", "cadence_minutes"):
        if not merged.get(key) and preview.get(key) is not None:
            merged[key] = preview[key]

    fresh_acts = {str(a.get("activity_id")): a for a in merged.get("activities") or [] if isinstance(a, dict)}
    added = updated = 0
    for activity in preview.get("activities") or []:
        aid = str(activity.get("activity_id"))
        if aid in fresh_acts:
            updated += 1
        else:
            added += 1
        fresh_acts[aid] = copy.deepcopy(activity)
    merged["activities"] = list(fresh_acts.values())

    fresh_items = {_evidence_key(i): i for i in merged.get("unassociated_items") or [] if isinstance(i, dict)}
    items_added = 0
    for item in preview.get("unassociated_items") or []:
        key = _evidence_key(item)
        if key not in fresh_items:
            items_added += 1
        fresh_items[key] = copy.deepcopy(item)
    merged["unassociated_items"] = list(fresh_items.values())

    seen_conflicts = {json.dumps(c, ensure_ascii=False, sort_keys=True) for c in merged.get("conflicts") or []}
    for conflict in preview.get("conflicts") or []:
        blob = json.dumps(conflict, ensure_ascii=False, sort_keys=True)
        if blob not in seen_conflicts:
            seen_conflicts.add(blob)
            merged.setdefault("conflicts", []).append(copy.deepcopy(conflict))

    # 覆盖进度：取更晚的 through（不能因为 replay 回退）
    def _through(payload: dict):
        value = (payload.get("coverage") or {}).get("through")
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    fresh_cov = dict(merged.get("coverage") or {})
    prev_cov = dict(preview.get("coverage") or {})
    if _through(fresh) is None or (_through(preview) is not None and _through(preview) > _through(fresh)):
        merged["coverage"] = {**fresh_cov, **prev_cov}
    else:
        merged["coverage"] = {**prev_cov, **fresh_cov}

    merged.setdefault("errors", list(fresh.get("errors") or []))
    fresh_meta = dict(merged.get("metadata") or {})
    prev_meta = dict(preview.get("metadata") or {})
    processed = sorted({*fresh_meta.get("processed_source_ids", []), *prev_meta.get("processed_source_ids", [])})
    merged_meta = {**prev_meta, **fresh_meta}          # fresh 优先
    merged_meta["processed_source_ids"] = processed
    merged["metadata"] = merged_meta
    if merged.get("status") != "ready" and preview.get("status") == "ready":
        merged["status"] = "ready"
    for key in ("source_manifest", "input_sha256", "origin", "publication_corrections"):
        if key in preview and key not in merged:
            merged[key] = preview[key]
    stats = {"activities_added": added, "activities_updated": updated,
             "unassociated_added": items_added,
             "activities_total": len(merged["activities"]),
             "unassociated_total": len(merged["unassociated_items"])}
    return merged, stats


def classify_activity_target_and_category(activity: dict) -> tuple[str, str]:
    text = (str(activity.get("title", "")) + " " + str(activity.get("summary", "")) + " " +
            " ".join(str(i.get("action", "")) for i in activity.get("items", []))).lower()
    codex_keywords = [
        "codex", "worker", "大模型", "提示词", "agent", "bug", "代码", "重启", "接口",
        "时区", "测试结果", "重新提取", "双决策", "流水线", "切分", "重跑", "插件",
        "优化项目", "编程", "服务器室", "llm", "转写", "asr", "模型", "对比实验"
    ]
    if any(k in text for k in codex_keywords):
        return "Codex", "编程·项目优化"
    if any(k in text for k in ["到账", "收款", "微信收款", "支付宝到账"]):
        return "设备播报", "支付·播报"
    if any(k in text for k in ["点餐", "点一份", "炒饼丝", "瘦肉粉", "薯条", "牛肉面", "吃了一碗", "汉堡", "点单"]):
        return "商家/店员", "餐饮·消费"
    if any(k in text for k in ["观看", "复述视频", "口播", "视频", "新闻", "绝地潜伏"]):
        return "媒体内容", "浏览·视频"
    if any(k in text for k in ["锻炼", "健身", "慢走", "散步", "跑步", "练腿"]):
        return "自述/运动", "运动·健身"
    return "自述/日常", "生活·日常"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=LOCK_TIMEOUT_SECONDS,
                        help="等待 .runtime.lock 的上限秒数（默认 30；超时报错退出，不静默跳过）")
    args = parser.parse_args()
    payload = build_payload(args.source)
    result = {"date": payload["date"], "activities": len(payload["activities"]),
              "unassociated_items": len(payload["unassociated_items"]), "applied": args.apply}
    if args.apply:
        from life_logger.activity_timeline_runtime import ActivityTimelineRuntime
        runtime = ActivityTimelineRuntime(args.output_root, args.config)
        lock_file = args.output_root / "activity-timeline" / ".runtime.lock"
        started = time.monotonic()
        try:
            handle = acquire_runtime_lock(lock_file, args.lock_timeout)
        except TimeoutError as exc:
            # 明确失败：绝不「等不到就直接写」
            print(json.dumps({"error": "lock_timeout", "detail": str(exc),
                              "lock_file": str(lock_file), "applied": False},
                             ensure_ascii=False), file=sys.stderr)
            raise SystemExit(3)
        waited_ms = int((time.monotonic() - started) * 1000)
        try:
            # 关键：**锁内**重新 read（不是锁外的旧快照），再合并、再 publish。
            # [2026-10-29] 必须读**未降级**的原始 payload：read_day 默认 for_display=True
            # 会给历史标题做展示层 hedge（加 title_original/title_hedged、改写 title），
            # 那份结果一旦 merge+publish 就会把降级标题永久写进生产 jev。
            fresh = runtime.read_day(payload["date"], payload.get("route") or "jev",
                                     for_display=False)
            merged, stats = merge_into_day(fresh, payload)
            runtime.publish_day(payload["date"], merged)
            # [2026-10-29] 跨进程写：读侧靠内容指纹天然 miss；这里只清本进程缓存 +
            # 尽量清掉本进程可能持有的 web 读缓存（失败绝不影响发布）。
            try:
                from life_logger.daily_dashboard import invalidate_dashboard_caches

                invalidate_dashboard_caches(job_root=runtime.output_root)
            except Exception:  # noqa: BLE001
                pass
        finally:
            release_runtime_lock(handle)
        result.update({"waited_ms": waited_ms, **stats})
        # 调度状态（.scheduling-state.json）**不触碰**：它只记录 tick 的窗口推进，
        # replay 发布不改变「下一轮做 live 还是 backfill」的判定。
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
