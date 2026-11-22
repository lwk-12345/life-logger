"""事件解析/账本投影 + 日报时间线助手（S5 从 local_web.py 原样搬出）。"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from life_logger.web_common import DEGRADABLE_SEVERITY, FATAL_SEVERITY
from life_logger.web_health import collect_stage_errors
from life_logger.web_search import _canonical_search_category

_MEANINGFUL_WORK_EVENT_MARKERS = (
    "开会",
    "会议",
    "沟通",
    "电话",
    "汇报",
    "修复",
    "提交",
    "完成",
    "测试",
    "开发",
    "修改",
    "处理",
    "上线",
    "部署",
    "面试",
    "评审",
    "排查",
)

_FUNNEL_ASYNC_CACHE: dict[str, dict[str, Any]] = {}

_FUNNEL_ASYNC_INFLIGHT: set[str] = set()

_FUNNEL_ASYNC_LOCK = threading.Lock()

INTERLOCUTOR_RELATION_MAP: dict[str, str] = {
    "friend": "friend",
    "朋友": "friend",
    "colleague": "colleague",
    "同事": "colleague",
    "family": "family",
    "家人": "family",
    "business": "business",
    "商务": "business",
    "stranger": "stranger",
    "陌生人": "stranger",
    "other": "other",
    "其他": "other",
}

def _meaningful_event_payload(item: dict[str, Any]) -> bool:
    # 【2026-10-14 修】过滤「未提炼的原始疑问候选」，它们不是生活事件。
    # 背景：candidate_extractor.py（"Stage-1 … candidate extractor for OpenClaw"）
    # 产出 5 种候选，语义完全不同，必须分开对待（实测 job-20261014-031125）：
    #   question_candidate   ← 原始疑问，title 就是裸转写，无提炼。该类常在视频内容里
    #                          混入（用户 01:11~01:19 看的 AI 技术视频）。→ 挡
    #     实测垃圾样本：「乌冬面好吃吗」「中国为什么企业盈利期待」「你脑子进水了吗」
    #                   「不是你在后台干什么呢」「明白了吗」
    #   fact_candidate       ← 已归类的事实（category=expense/food…），有 quote/confidence。
    #                          如「乌冬面两包，一共二百克的面」。→ 留（走下面的类别分支）
    #   todo_candidate / decision_candidate / reasoning_candidate → 留（认知事件，有价值）
    # 只按 event_id 的 evt_cand_ 前缀判会误伤：实测 fact_candidate 的 id 无该前缀
    # （evt_3d4f9b9c77d5 等 3 条），而 question_candidate 全有该前缀。
    # 注意：只影响对外 payload 的可见性，产物里的候选数据照旧保留（数据不删）。
    if item.get("source_type") == "manual" or item.get("review_status") == "confirmed":
        return True
    evidence_for_candidate = item.get("evidence")
    candidate_evidence: dict[str, Any] = (
        evidence_for_candidate if isinstance(evidence_for_candidate, dict) else {}
    )
    data_for_candidate = item.get("data")
    raw_openclaw = (
        data_for_candidate.get("openclaw") if isinstance(data_for_candidate, dict) else None
    )
    openclaw_meta: dict[str, Any] = raw_openclaw if isinstance(raw_openclaw, dict) else {}
    candidate_type = str(
        candidate_evidence.get("candidate_type")
        or openclaw_meta.get("candidate_type")
        or ""
    ).strip()
    if candidate_type == "question_candidate":
        return False
    categories = {
        _canonical_search_category(value)
        for value in item.get("categories", [])
        if value
    }
    if categories.intersection({"routine", "expense", "food", "exercise", "todo"}):
        return True
    title = str(item.get("title") or "").strip()
    evidence = item.get("evidence")
    quote = str(evidence.get("quote") or "") if isinstance(evidence, dict) else ""
    text = f"{title} {quote}".strip()
    if "idea" in categories:
        normalized = "".join(
            char for char in title if char.isalnum() or "一" <= char <= "龥"
        )
        return len(normalized) >= 6
    if categories.intersection({"work_communication", "work", "communication"}):
        return any(marker in text for marker in _MEANINGFUL_WORK_EVENT_MARKERS)
    return False

def _capture_funnel_nonblocking(root: Path, date: str) -> dict[str, Any]:
    if not date:
        return {}
    with _FUNNEL_ASYNC_LOCK:
        cached = _FUNNEL_ASYNC_CACHE.get(date)
        if cached is not None:
            return dict(cached)
        if date in _FUNNEL_ASYNC_INFLIGHT:
            return {"date": date, "pending": True, "cached": False}
        _FUNNEL_ASYNC_INFLIGHT.add(date)

    def _worker() -> None:
        try:
            from life_logger.capture_funnel import compute_capture_funnel as _compute

            result = _compute(root, date)
        except Exception as exc:  # 指标失败不能影响任何读接口
            result = {"date": date, "error": f"{type(exc).__name__}: {exc}"}
        with _FUNNEL_ASYNC_LOCK:
            _FUNNEL_ASYNC_CACHE[date] = result
            _FUNNEL_ASYNC_INFLIGHT.discard(date)
            if len(_FUNNEL_ASYNC_CACHE) > 64:
                _FUNNEL_ASYNC_CACHE.pop(next(iter(_FUNNEL_ASYNC_CACHE)), None)

    threading.Thread(
        target=_worker, name=f"capture-funnel-{date}", daemon=True
    ).start()
    return {"date": date, "pending": True, "cached": False}

def _model_catalog(config: dict[str, Any]) -> dict[str, Any]:
    def entry(
        model_id: str,
        name: str,
        *,
        description: str,
        installed: bool,
        selectable: bool,
        recommended: bool = False,
        badge: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": model_id,
            "name": name,
            "description": description,
            "installed": installed,
            "selectable": selectable,
            "recommended": recommended,
            "badge": badge,
        }

    current_provider = str(config.get("asr_provider") or "local_turbo").lower()

    turbo_entry = entry(
        "mlx-whisper-turbo",
        "MLX Whisper Large-v3-Turbo",
        description="Apple Silicon MLX 本地加速 Whisper Large-v3-Turbo，纯本地推理，无需外部网络。",
        installed=True,
        selectable=True,
        recommended=current_provider != "moss",
        badge="推荐·本地",
    )
    moss_entry = entry(
        "moss",
        "MOSS-Transcribe-Diarize 0.9B",
        description="本地端到端模型：一次同时给出转写、说话人分离（说话人一/二…）和句级时间戳；噪声/铃声等退化场景零幻觉。",
        installed=True,
        selectable=True,
        recommended=current_provider == "moss",
        badge="推荐·带说话人",
    )
    return {
        "recommended_profile": "recommended-local",
        "asr": [
            moss_entry,
            turbo_entry,
        ],
        "speaker": [
            entry(
                "cam++",
                "CAM++",
                description="区分本人、他人和待复核声音，当前声源归属主力。",
                installed=True,
                selectable=True,
                recommended=True,
                badge="推荐",
            )
        ],
        "event": [
            {
                "id": "local-rules",
                "name": "本地事件提取",
                "description": "纯本地、无额外账号，作为默认事件提取层。",
                "recommended": True,
                "selectable": True,
            },
        ],
    }

def failed_file_summaries(state: dict[str, Any]) -> list[dict[str, Any]]:
    """列出任务里被判失败的文件，含卡住的阶段名。

    任务级的 ``status`` 只有在没有失败文件时才算 ``completed``；有失败文件时降为
    ``partial``，并把数量与清单写进状态，避免"失败被完成掩盖"（历史上有 9 个这样
    的任务，统计与列表都看不出来）。
    """
    summaries: list[dict[str, Any]] = []
    for item in state.get("files") or []:
        # 文件级 status 之外，还要认「阶段级失败」：曾出现文件标 completed、
        # 但 stages.jev.status == "failed"（下游阶段失败后再也不会重跑，
        # 因为 run_web_job 见到 completed+results 就整文件跳过），
        # 结果任务保持 completed 却缺产物 —— 即「假成功」。
        failed_stage_names = [
            name
            for name, value in (item.get("stages") or {}).items()
            if isinstance(value, dict) and value.get("status") == "failed"
        ]
        if item.get("status") != "failed" and not failed_stage_names:
            continue
        stage = next(
            (
                name
                for name, value in (item.get("stages") or {}).items()
                if value.get("status") == "failed"
            ),
            None,
        )
        summaries.append(
            {
                "file_id": item.get("file_id"),
                "name": item.get("original_name"),
                "stage": stage or item.get("current_stage"),
                "error": item.get("error"),
            }
        )
    return summaries

def apply_file_outcomes(
    state: dict[str, Any], *, job_dir: Path | None = None
) -> dict[str, Any]:
    """把文件级/阶段级失败汇总到任务状态上，返回被改写的状态。

    除了 ``failed_file_count`` / ``failed_files``，还把每个子步骤失败写成结构化
    的 ``stage_errors``（见 :func:`collect_stage_errors`），并按严重性决定任务终态：

    * 有 **致命** 失败（文件失败 / 阶段失败 / 承诺的产物缺失）→ ``partial``
    * 只有 **可降级** 失败（可选阶段失败后恢复、日志里留着失败证据）→ ``degraded``
    * 没有任何失败证据 → 保持 ``completed``

    传 ``job_dir`` 时会额外读阶段的落盘日志（``files/<id>/<stage>.log``）做证据核对：
    「阶段自称完成、但日志里最后一次失败之后再没有成功记录」这种状态会被降级，
    修复「只往 logs 里丢文字、任务照样报 completed」的假成功。
    """
    failed = failed_file_summaries(state)
    state["failed_file_count"] = len(failed)
    state["failed_files"] = failed
    stage_errors = collect_stage_errors(state, job_dir=job_dir)
    state["stage_errors"] = stage_errors
    if failed or any(item["severity"] == FATAL_SEVERITY for item in stage_errors):
        if state.get("status") in {"completed", "degraded"}:
            state["status"] = "partial"
            state["current_stage"] = "partial"
    elif any(
        item["severity"] == DEGRADABLE_SEVERITY and not item["resolved"]
        for item in stage_errors
    ):
        if state.get("status") == "completed":
            state["status"] = "degraded"
            state["current_stage"] = "degraded"
    return state

def _apply_interlocutor_fields(
    item: dict[str, Any],
    relation: str | None,
    name: str | None,
) -> None:
    if relation is not None:
        rel = str(relation).strip()
        if not rel:
            item.pop("interlocutor_relation", None)
        elif rel in INTERLOCUTOR_RELATION_MAP:
            item["interlocutor_relation"] = INTERLOCUTOR_RELATION_MAP[rel]
        elif len(rel) <= 50:
            item["interlocutor_relation"] = rel
        else:
            raise ValueError("无效的人际关系标签")
    if name is not None:
        nm = str(name).strip()
        if not nm:
            item.pop("interlocutor_name", None)
        elif len(nm) <= 100:
            item["interlocutor_name"] = nm
        else:
            raise ValueError("对谈人姓名过长")

def _reapply_activity_sidecars(
    brief: dict[str, Any],
    kept: Sequence[Mapping[str, Any]],
    hidden_ids: set[str],
) -> None:
    """[2026-11-04 P02] 把 dictation 过滤同步到派生产物（就地改 brief，只删不改）。

    `build_daily_brief()` 用**未过滤**事件先建好了 cards/links/presentation/
    purchase_sessions，读层只摘 events 时标题与原话仍留在里面。cards 按
    `source_event_id` 剔除（存活卡的账本引用逐字不变）；其余三者是事件的纯函数，
    用过滤后的事件重建，保证内部 stats 与内容一致，且只在 brief 原本有该键时才写。
    """
    if not isinstance(brief, dict):
        return
    cards = brief.get("activity_cards")
    if isinstance(cards, list):
        brief["activity_cards"] = [c for c in cards if not (
            isinstance(c, Mapping) and str(c.get("source_event_id") or "") in hidden_ids)]
    if not any(k in brief for k in ("activity_links", "activity_presentation", "purchase_sessions")):
        return
    from life_logger.activity_links import build_activity_links
    from life_logger.activity_presentation import (
        build_activity_presentation, build_purchase_session_summaries)

    if "activity_links" in brief:
        brief["activity_links"] = build_activity_links(kept)
    if "activity_presentation" in brief:
        brief["activity_presentation"] = build_activity_presentation(
            brief.get("activity_cards") or [], brief.get("activity_links")
        )
    if "purchase_sessions" in brief:
        projection = build_purchase_session_summaries(kept)
        brief["purchase_sessions"] = projection.get("sessions", [])
        for key, source in (("purchase_session_pending", "pending_candidates"),
                            ("purchase_session_pending_broadcast", "pending_broadcast_addons")):
            if key in brief:
                brief[key] = projection.get(source, [])

