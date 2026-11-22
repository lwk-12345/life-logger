"""模型调用用量记录（按天、按步骤，追加写 JSONL）。

为什么需要
----------
`work/llm-audit-20261106/report.md` 的结论：**只有事件抽取记 usage**，
活动整理 / 汇总卡 / 问答 / 纠错 / VIP 全都不记 —— 全站成本算不出来。

本模块只做一件事：**拿到响应后，把 usage 数字落盘**。
它不参与任何业务判断，任何异常都被吞掉（记录失败绝不影响主流程）。

不记录什么（硬性）
------------------
* 提示词、原话、模型输出正文 —— 一律不写（只写 token 数与模型名）；
* API key、Authorization 头；
* 完整 URL（只记 `endpoint_kind`，如 `chat.completions` / `gemini.generateContent`）。

落盘
----
`output/local-web/model-usage/YYYY-MM-DD.jsonl`（**UTC+8 日期**，用
`life_logger.atomic.append_json_line` 原子追加 + flock，多进程安全）。

开关
----
* `LL_MODEL_USAGE_LOG=0|false|off` → 关闭；
* `LL_MODEL_USAGE_DIR=/some/dir` → 改落盘目录（测试用，避免污染生产数据）。
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "model-usage-v1"
UTC8 = _dt.timezone(_dt.timedelta(hours=8))
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "output" / "local-web" / "model-usage"

#: 步骤名 → 说明（报告脚本按这个顺序展示）
STEPS: dict[str, str] = {
    "activity_timeline": "活动整理（activity_timeline_runtime）",
    "activity_v3_shadow": "活动 v3 影子（不发布）",
    "activity_v3_live": "活动 v3（显式 live）",
    "activity_digest_summary": "汇总卡（activity_digest.call_model）",
    "activity_digest_category": "模型分类（activity_digest.call_categories）",
    "memory_qa_gemini": "问答 · Gemini（memory_rag）",
    "memory_qa_deepseek": "问答 · DeepSeek 备用（memory_rag）",
    "natural_correction": "语音纠错（natural_correction）",
    "vip_fast_track": "VIP 语音助手（vip_fast_track）",
}


def _enabled() -> bool:
    if str(os.environ.get("LL_MODEL_USAGE_LOG", "1")).strip().lower() in {"0", "false", "off", "no"}:
        return False
    # [2026-11-06] 跑测试时默认**不写生产目录**：大量既有用例会用假响应调用
    # call_model / call_categories / vip_fast_track，若不拦，测试会把假记录写进
    # output/local-web/model-usage/（实测一次全量 pytest 写进 24 条假数据，污染真实成本账）。
    # 需要测记录的用例请显式设 LL_MODEL_USAGE_DIR 指向 tmp（本仓库 tests/test_model_usage.py 即如此）。
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("LL_MODEL_USAGE_DIR"):
        return False
    return True


def usage_dir() -> Path:
    override = os.environ.get("LL_MODEL_USAGE_DIR")
    if override:
        return Path(override)
    # [2026-11-07] 隔离实例（配置路径被覆盖）的用量写到它自己的数据根，
    # 不再污染生产 output/local-web/model-usage/。
    try:
        from life_logger import llm_config

        if llm_config.is_overridden():
            return llm_config.data_root() / "model-usage"
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_DIR


def _now(now: _dt.datetime | None = None) -> _dt.datetime:
    return (now or _dt.datetime.now(tz=UTC8)).astimezone(UTC8)


def day_path(day: str | None = None, *, now: _dt.datetime | None = None) -> Path:
    """`output/local-web/model-usage/YYYY-MM-DD.jsonl`（UTC+8）。"""
    date = day or _now(now).date().isoformat()
    return usage_dir() / f"{date}.jsonl"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def extract_usage(payload: Any) -> dict[str, Any]:
    """从响应体里抽 usage —— 兼容 OpenAI/Magpie 形状与 Gemini 形状。

    OpenAI:  ``{"usage": {"prompt_tokens":…, "completion_tokens":…,
             "prompt_tokens_details": {"cached_tokens":…},
             "completion_tokens_details": {"reasoning_tokens":…}}}``
    Gemini:  ``{"usageMetadata": {"promptTokenCount":…, "candidatesTokenCount":…,
             "cachedContentTokenCount":…, "thoughtsTokenCount":…}}``

    只返回值，不读也不返回任何文本内容。
    """
    if not isinstance(payload, Mapping):
        return {}
    out: dict[str, Any] = {}

    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        prompt_details = usage.get("prompt_tokens_details")
        completion_details = usage.get("completion_tokens_details")
        out["prompt_tokens"] = _as_int(usage.get("prompt_tokens"))
        out["completion_tokens"] = _as_int(usage.get("completion_tokens"))
        out["total_tokens"] = _as_int(usage.get("total_tokens"))
        out["cached_tokens"] = _as_int(
            (prompt_details or {}).get("cached_tokens") if isinstance(prompt_details, Mapping) else None
        )
        out["reasoning_tokens"] = _as_int(
            (completion_details or {}).get("reasoning_tokens")
            if isinstance(completion_details, Mapping) else None
        )

    meta = payload.get("usageMetadata")
    if isinstance(meta, Mapping) and "prompt_tokens" not in out:
        out["prompt_tokens"] = _as_int(meta.get("promptTokenCount"))
        out["completion_tokens"] = _as_int(meta.get("candidatesTokenCount"))
        out["total_tokens"] = _as_int(meta.get("totalTokenCount"))
        out["cached_tokens"] = _as_int(meta.get("cachedContentTokenCount"))
        out["reasoning_tokens"] = _as_int(meta.get("thoughtsTokenCount"))

    return {k: v for k, v in out.items() if v is not None}


def build_record(
    step: str,
    *,
    payload: Any = None,
    model: str | None = None,
    elapsed_ms: float | int | None = None,
    ok: bool = True,
    error_kind: str | None = None,
    endpoint_kind: str | None = None,
    extra: Mapping[str, Any] | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """组装一条记录（不含任何提示词/原话/密钥/URL）。"""
    when = _now(now)
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "ts": when.isoformat(timespec="seconds"),
        "date": when.date().isoformat(),
        "step": str(step),
        "ok": bool(ok),
        "model": (str(model) if model else None),
        "endpoint_kind": (str(endpoint_kind) if endpoint_kind else None),
        "elapsed_ms": (round(float(elapsed_ms), 1) if elapsed_ms is not None else None),
    }
    record.update(extract_usage(payload))
    if error_kind:
        record["error_kind"] = str(error_kind)[:80]
    if isinstance(extra, Mapping):
        for key, value in extra.items():
            key_str = str(key)
            # 防御：绝不把可能的正文/凭据写进日志
            if key_str in {"prompt", "content", "text", "quote", "url", "api_key", "authorization"}:
                continue
            if isinstance(value, (int, float, bool)) or value is None:
                record[key_str] = value
            else:
                record[key_str] = str(value)[:80]
    return record


def record(
    step: str,
    *,
    payload: Any = None,
    model: str | None = None,
    elapsed_ms: float | int | None = None,
    ok: bool = True,
    error_kind: str | None = None,
    endpoint_kind: str | None = None,
    extra: Mapping[str, Any] | None = None,
    now: _dt.datetime | None = None,
    path: Path | str | None = None,
) -> dict[str, Any] | None:
    """追加一条用量记录。**永不抛异常**（失败返回 None）。

    返回值 = 实际写入的 dict（测试与调试用）。
    """
    if not _enabled():
        return None
    try:
        record_obj = build_record(
            step,
            payload=payload,
            model=model,
            elapsed_ms=elapsed_ms,
            ok=ok,
            error_kind=error_kind,
            endpoint_kind=endpoint_kind,
            extra=extra,
            now=now,
        )
        target = Path(path) if path else day_path(record_obj["date"])
        target.parent.mkdir(parents=True, exist_ok=True)
        from life_logger.atomic import append_json_line

        append_json_line(target, record_obj, lock=True)
        return record_obj
    except Exception:  # noqa: BLE001 - 记录失败绝不影响主流程
        return None


def elapsed_ms_since(started: float, *, monotonic=__import__("time").monotonic) -> int:
    """把 `time.monotonic()` 起点换算成毫秒（四舍五入）。"""
    return int(round((monotonic() - started) * 1000))


__all__ = [
    "SCHEMA", "STEPS", "DEFAULT_DIR", "usage_dir", "day_path",
    "extract_usage", "build_record", "record", "elapsed_ms_since",
]
