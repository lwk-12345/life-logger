"""「当时前台是工作软件」——口令词抑制的**唯一**判据实现处。

用户规则（2026-11-06 确认）：说话那一刻 Mac 前台是**工作软件**时，
语音口令词（查询 / 备忘 / 待办增删完成）不触发，按普通工作对话处理。
手表「捏两下」(double_pinch) 60 秒窗口内的显式指令**照常触发**，不受影响。

三条硬约束：
1. **未知不压制**：拿不到时间、时间没有时区、那一刻没有窗口证据 —— 一律当作未知，
   维持原行为（照常触发）。宁可多一条口令，也不能把真口令吞掉。
2. **一处定义**：`WORK_APP_BUNDLES` 只在这里出现，其它模块一律导入，不许各自抄一份。
3. **留痕不删话**：抑制只写 `suppressed_reason` / `app_label`，原话与事件都保留。

数据来源与 `/api/daily` 的活动投影完全相同：`app_context.load_windows` + `context_at`。
另外兼容**抽取时已经解析好**的 `record["input_context"]`（watch 管线 `attach_watch_input_context`
写的那种），避免为了判定再读一遍窗口。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")

#: 用户确认的「工作软件」bundle（**唯一**定义处，别处只能导入）。
#: 明确**不算**的几个：Magpie、Hermes、微信、浏览器。
WORK_APP_BUNDLES: frozenset[str] = frozenset({
    "com.yetone.alma",                    # Alma
    "com.apple.terminal",                 # 终端（pi / DeepSeek Harness 在里面跑）
    "com.openai.codex",                   # Codex
    "com.apple.dt.xcode",                 # Xcode
    "com.google.antigravity",             # Antigravity
    "com.lbjlaq.antigravity-tools-lite",  # Antigravity Tools Lite
})

#: 人类可读名（大小写不敏感）—— 只有 bundle 拿不到、只剩 app_label 时才用。
WORK_APP_LABELS: frozenset[str] = frozenset({
    "alma", "terminal", "codex", "xcode",
    "antigravity", "antigravity tools", "antigravity-tools-lite", "antigravity tools lite",
})

#: 抑制标记（写进事件/条目，供审计；也用于前端/OpenClaw 过滤）。
SUPPRESSED_REASON = "work_app_front"


#: [2026-11-07] 窗口 + `WindowIndex` 的进程内缓存：键 = (watch 根, day)，
#: 值 = (指纹, windows, index)。手表每 10 分钟就多一个窗口、latest.json 一直在变，
#: 所以指纹取 `days/<day>.jsonl` 与 `latest.json` 的 (mtime_ns, size)，一变就整组重建。
_WINDOWS_CACHE: dict[tuple[str, str], tuple[str, list[dict[str, Any]], Any]] = {}
_WINDOWS_CACHE_LIMIT = 8


def _windows_fingerprint(*paths: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            digest.update(f"{path}|missing".encode("utf-8"))
            continue
        digest.update(f"{path}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8"))
    return digest.hexdigest()


def _windows_and_index(watch_root: Path, day: str) -> tuple[list[dict[str, Any]], Any]:
    """一天的窗口 + `WindowIndex`（同一指纹失效，索引只绑定返回的那份列表）。"""
    from life_logger import app_context

    day_file = watch_root / "input-context" / "days" / f"{day}.jsonl"
    latest_file = watch_root / "input-context" / "latest.json"
    fingerprint = _windows_fingerprint(day_file, latest_file)
    key = (str(watch_root), day)
    cached = _WINDOWS_CACHE.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1], cached[2]
    windows = app_context.load_windows(root=watch_root, day=day)
    index = app_context.build_window_index(windows)
    if len(_WINDOWS_CACHE) >= _WINDOWS_CACHE_LIMIT:
        _WINDOWS_CACHE.clear()
    _WINDOWS_CACHE[key] = (fingerprint, windows, index)
    return windows, index


def _context_at(app_context: Any, moment: Any, windows: list[dict[str, Any]], index: Any) -> Any:
    """带索引调用 `context_at`；对方不接受 `index`（老版本/测试替身）就退回原调用。"""
    if index is not None:
        try:
            return app_context.context_at(moment, windows, index=index)
        except TypeError:
            pass
    return app_context.context_at(moment, windows)


def _norm(value: Any) -> str:
    return str(value or "").strip()


def normalize_bundle(value: Any) -> str | None:
    """bundle 归一：去空白、转小写（大小写在不同 app 版本里会飘）。空 → None。"""
    text = _norm(value).lower()
    return text or None


def is_work_app_bundle(value: Any) -> bool:
    bundle = normalize_bundle(value)
    return bool(bundle) and bundle in WORK_APP_BUNDLES


def is_work_app_label(value: Any) -> bool:
    label = _norm(value).lower()
    if not label:
        return False
    if label in WORK_APP_LABELS:
        return True
    # 「Antigravity Tools Lite」这类带空格的写法
    return label.replace(" ", "-") in WORK_APP_LABELS


def is_work_app(*, bundle: Any = None, label: Any = None) -> bool:
    """bundle 优先；只有 bundle 缺失时才看 label（不互相猜）。"""
    if normalize_bundle(bundle):
        return is_work_app_bundle(bundle)
    return is_work_app_label(label)


def _parse_moment(moment: Any) -> datetime | None:
    if isinstance(moment, datetime):
        return moment if moment.tzinfo is not None else None
    text = _norm(moment)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def front_app_at(moment: Any, *, root: Path | str) -> dict[str, Any] | None:
    """某一时刻的前台应用（`app_context` 同一套窗口判定）。未知返回 None。"""
    at = _parse_moment(moment)
    if at is None:
        return None                      # 无时区/解析不了 → 未知，绝不猜
    day = at.astimezone(SHANGHAI).date().isoformat()
    try:
        from life_logger import app_context

        windows, index = _windows_and_index(Path(root) / "watch-sessions", day)
        context = _context_at(app_context, moment, windows, index)
    except Exception as error:  # noqa: BLE001 - 判不出来只能是未知，不能拖垮调用方
        logger.warning("前台应用判定失败（已按未知处理）: %s: %s", type(error).__name__, error)
        return None
    return normalize_context(context)


def normalize_context(context: Any) -> dict[str, Any] | None:
    """把两种历史形态归一成 `{app_label, target_bundle, kind}`。

    * `app_context.context_at()` 形态：`{app_label, target_bundle, kind, confidence, ...}`
    * watch 管线 `resolve_input_context()` 形态：`{status: "resolved", target_bundle, app_label?, ...}`
    """
    if not isinstance(context, Mapping):
        return None
    status = _norm(context.get("status")).lower()
    if status and status != "resolved":
        return None                      # uncertain / unknown 一律当未知
    bundle = _norm(context.get("target_bundle") or context.get("target_app")) or None
    label = _norm(context.get("app_label")) or None
    if label is None and bundle:
        try:
            from life_logger.input_context import bundle_to_app_label

            label = bundle_to_app_label(bundle)
        except Exception:  # noqa: BLE001
            label = None
    if not bundle and not label:
        return None
    kind = _norm(context.get("kind")) or None
    if kind is None and status == "resolved":
        # watch 管线形态没有 kind：resolved 只由麦克风窗口（mic_event）产生；
        # 前台采样（front_sample）窗口 complete=false，不会 resolved。
        provider = context.get("provider")
        source = _norm(provider.get("source")) if isinstance(provider, Mapping) else ""
        kind = "front_app" if source == "front_sample" else "dictation"
    return {"app_label": label, "target_bundle": bundle, "kind": kind}


def work_app_front(
    *,
    context: Any = None,
    moment: Any = None,
    root: Path | str | None = None,
) -> dict[str, Any] | None:
    """判定「说话那一刻前台是工作软件」。

    优先用调用方已经解析好的 `context`（零 I/O）；没有就按 `moment + root` 查窗口。
    **任何一环缺失都返回 None（= 未知，调用方必须维持原行为）。**
    """
    app = normalize_context(context)
    if app is None and moment is not None and root is not None:
        app = front_app_at(moment, root=root)
    if app is None:
        return None
    if not is_work_app(bundle=app.get("target_bundle"), label=app.get("app_label")):
        return None
    return app


def suppression_mark(app: Mapping[str, Any]) -> dict[str, Any]:
    """写进产物/条目的审计标记（不删原话，不改其它字段）。"""
    return {
        "suppressed_reason": SUPPRESSED_REASON,
        "app_label": _norm(app.get("app_label")) or None,
        "target_bundle": _norm(app.get("target_bundle")) or None,
    }


def should_suppress(
    *,
    context: Any = None,
    moment: Any = None,
    root: Path | str | None = None,
    physical_pin: str | None = None,
    pin_age_seconds: float | None = None,
    pin_window_seconds: float = 60.0,
) -> dict[str, Any] | None:
    """口令词是否该被抑制。返回审计标记或 None（None = 照常触发）。

    「捏两下」60 秒窗口内的显式指令**永不被抑制**（用户已明确）。
    """
    if physical_pin == "double_pinch":
        try:
            age = float(pin_age_seconds) if pin_age_seconds is not None else None
        except (TypeError, ValueError):
            age = None
        if age is not None and 0 <= age <= pin_window_seconds:
            return None
    app = work_app_front(context=context, moment=moment, root=root)
    if app is None:
        return None
    # 只认「语音输入到工作软件」(kind=dictation)。前台在 Alma/终端不代表话是对它说的：
    # 用户整天开着 Alma，对手表说「提醒我理发」不能被压掉。
    if app.get("kind") != "dictation":
        return None
    return suppression_mark(app)
