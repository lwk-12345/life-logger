"""模型失败 → 暂停 + 通知 + 待补标记（2026-10-22，用户明确要求）。

用户原话：「模型失败的话，能不能就把任务暂停在这里？向我通知说报告异常情况，
模型坏了就不用了，能不能停在这里」——选的是「停下来 + 发微信通知」。

设计要点（与 ⑤5号 的 F1 facts/narrative 改造**解耦**）：
1. 这里只负责「失败检测 → 落 pending 标记 → 通知 → 待补」这条链路，
   不碰日报内容结构，也不改 fallback 的字段；
2. pending 标记是**唯一真相**：它在 ⇒ 当天日报没有真正生成，页面上必须显示
   「日报未生成（模型失败）」，而不是空白或绿色；
3. 通知失败**绝不阻塞**：notify.sh 挂了就写本地队列 `notify-queue.jsonl`，等通道恢复补发；
4. 补生成成功后调用 `clear_pending()` 清掉标记（手动重跑 / 定时补跑都走同一条路径）。
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SHANGHAI_TZ = "Asia/Shanghai"

#: 通知脚本：**仓库内唯一实现** `scripts/notify.sh`（严格语义，见该脚本头注释）。
#: 之前默认指向仓库外的 `"$HOME/.life-logger/agent-board/notify.sh"`，那份脚本
#: 不管成败都 exit 0，于是"通知失败"会被记成成功。现在默认走仓库内、且带 `--json`
#: 解析真实状态（sent / failed / skipped）；环境变量仍可覆盖（测试注入用）。
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_NOTIFY_SCRIPT = str(_REPO_ROOT / "scripts" / "notify.sh")
NOTIFY_TIMEOUT_SECONDS = 15.0


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def synthesis_dir(root: Path) -> Path:
    return Path(root) / "daily-synthesis"


def pending_path(root: Path, date_str: str) -> Path:
    return synthesis_dir(root) / f"pending-{date_str}.json"


def queue_path(root: Path) -> Path:
    return Path(root) / "notify-queue.jsonl"


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """同目录**唯一**临时名 → flush+fsync → 原子 os.replace。

    【Q175 / F7-S1】原来固定 `xxx.tmp`：两个进程（Web POST / 定时器 / 重跑脚本 / 队列任务）
    同时写同一天日报时会互相覆盖同一个 tmp，A 的 `os.replace` 可能把 B 的内容落成正式文件。
    这里把临时名改成 `.<name>.<pid>.<uuid>.tmp`（同目录，保证 `os.replace` 仍是同文件系统原子操作），
    写完 fsync 再替换；失败时清理自己的 tmp，绝不碰正式文件。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))


def record_pending(
    root: Path,
    date_str: str,
    *,
    errors: Iterable[dict[str, Any]] = (),
    event_count: int | None = None,
    expense_total: float | None = None,
    notify: dict[str, Any] | None = None,
) -> Path:
    """落「待补生成」标记（幂等：重复失败只累加 attempts，不覆盖首次失败信息）。"""
    path = pending_path(root, date_str)
    previous: dict[str, Any] = {}
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
    attempts = int(previous.get("attempts") or 0) + 1
    payload = {
        "schema_version": "synthesis-pending-v1",
        "date": date_str,
        "state": "pending",
        "paused": True,
        "reason": "model_tiers_failed",
        "message": "模型三级全挂：日报已暂停，等模型恢复后补生成",
        "first_failed_at": previous.get("first_failed_at") or _now_iso(),
        "last_failed_at": _now_iso(),
        "attempts": attempts,
        "errors": list(errors) or previous.get("errors") or [],
        "event_count": event_count,
        "expense_total": expense_total,
        "notify": notify or previous.get("notify"),
        "next_action": "模型恢复后执行 python3 scripts/rerun_daily_synthesis.py "
                       f"--date {date_str}（或等 scheduler 补跑）",
    }
    _atomic_json(path, payload)
    return path


def clear_pending(root: Path, date_str: str, *, reason: str = "synthesis_ok") -> bool:
    """日报真正生成成功后清理标记；同时留一行审计记录，不清空历史。"""
    path = pending_path(root, date_str)
    if not path.is_file():
        return False
    audit = synthesis_dir(root) / "pending-resolved.jsonl"
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    try:
        audit.parent.mkdir(parents=True, exist_ok=True)
        with audit.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "date": date_str,
                "resolved_at": _now_iso(),
                "reason": reason,
                "attempts": previous.get("attempts"),
                "first_failed_at": previous.get("first_failed_at"),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 审计写不进去不影响主流程
    try:
        path.unlink()
        return True
    except OSError:
        return False


def read_pending(root: Path, date_str: str) -> dict[str, Any] | None:
    path = pending_path(root, date_str)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"date": date_str, "state": "pending", "reason": "unreadable_marker"}
    return payload if isinstance(payload, dict) else None


def list_pending(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(synthesis_dir(root).glob("pending-*.json")):
        date_str = path.stem.replace("pending-", "", 1)
        marker = read_pending(root, date_str)
        if marker:
            marker.setdefault("date", date_str)
            out.append(marker)
    return out


def enqueue_notification(root: Path, payload: dict[str, Any]) -> None:
    """通知发送失败时的本地队列：微信通道恢复后可以原样补发。"""
    path = queue_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 队列都写不进去也不能抛：通知永远不阻塞主流程


def _parse_notify_json(text: str) -> dict[str, Any] | None:
    """从 notify.sh 输出里取最后一行 JSON 状态（找不到返回 None）。"""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "status" in payload:
            return payload
    return None


def notify(script: str | None, title: str, message: str) -> dict[str, Any]:
    """调用 `scripts/notify.sh --json`。**永不抛异常**，返回真实状态供上层留痕。

    状态口径（与 notify.sh 一致，严格传播，不回退成"看起来成功"）：
      * ``sent``          rc=0 且 JSON.status=sent（或旧脚本 rc=0 且输出含 Sent via）
      * ``skipped``       rc=3 / JSON.status=skipped（未配置通道、显式禁用、无 openclaw）
      * ``failed``        其它（含 JSON.status=failed、rc≠0、输出自证失败、无法解析）
      * ``timeout``      超过 ``NOTIFY_TIMEOUT_SECONDS``
      * ``script_missing`` 脚本不存在
      * ``error``         OSError 等
    """
    script_path = Path(os.environ.get("LIFE_LOGGER_NOTIFY_SCRIPT") or script or DEFAULT_NOTIFY_SCRIPT)
    result: dict[str, Any] = {
        "at": _now_iso(),
        "script": str(script_path),
        "title": title,
        "message": message,
    }
    if not script_path.is_file():
        result.update(status="script_missing", detail=str(script_path))
        return result
    try:
        # 默认路径可能带空格（历史 agent-board 路径），一律用参数数组，不拼 shell
        proc = subprocess.run(
            [str(script_path), "--json", title, message],
            capture_output=True,
            timeout=NOTIFY_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        result.update(status="timeout", detail=f"notify.sh 超时（>{NOTIFY_TIMEOUT_SECONDS:g}s）")
        partial = (error.output or b"")
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        result["stdout"] = str(partial)[-500:]
        return result
    except (OSError, subprocess.SubprocessError) as error:
        result.update(status="error", detail=f"{type(error).__name__}: {error}")
        return result

    stdout = (proc.stdout or b"").decode("utf-8", "replace").strip()
    stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
    payload = _parse_notify_json(stdout)
    rc = proc.returncode
    result.update(returncode=rc, stdout=stdout[-500:], stderr=stderr[-500:])

    if payload is not None:
        declared = str(payload.get("status") or "").strip().lower()
        result["notify_payload"] = payload
        result["reason"] = payload.get("reason")
        # JSON 说 sent 但 rc≠0 或显式 ok=false → 以 rc/证据为准，判 failed
        if declared == "sent" and rc == 0 and payload.get("ok", True) is not False:
            result["status"] = "sent"
            return result
        if declared == "skipped":
            result.update(status="skipped", detail=str(payload.get("reason") or "skipped"))
            return result
        result.update(
            status="failed",
            detail=str(payload.get("reason") or stderr or f"exit {rc}")[-500:],
        )
        return result

    # 旧式子脚本（不支持 --json）：保留兼容，但要求有送达证据
    combined = f"{stdout}\n{stderr}"
    if rc == 0 and "Sent via" in combined and "prepare failed" not in combined.lower():
        result.update(status="sent", detail="legacy notify（无 JSON，输出含 Sent via）")
        return result
    if rc == 3:
        result.update(status="skipped", detail=(stderr or stdout or "skipped")[-500:])
        return result
    result.update(
        status="failed",
        detail=(stderr or stdout or f"exit {rc}")[-500:],
    )
    return result


def announce_failure(
    root: Path,
    date_str: str,
    *,
    errors: Iterable[dict[str, Any]] = (),
    event_count: int | None = None,
    expense_total: float | None = None,
    script: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """失败后的完整动作：发通知（失败入队）→ 落 pending 标记。"""
    title = f"日报生成失败 {date_str}"
    message = "需要你判断：模型三级全挂，已暂停，等恢复后补生成"
    notify_result = notify(script, title, message)
    if notify_result.get("status") != "sent":
        enqueue_notification(root, {
            "kind": "daily_synthesis_failed",
            "date": date_str,
            "title": title,
            "message": message,
            "queued_at": _now_iso(),
            "last_error": notify_result.get("detail") or notify_result.get("status"),
        })
    path = record_pending(
        root,
        date_str,
        errors=errors,
        event_count=event_count,
        expense_total=expense_total,
        notify=notify_result,
    )
    return path, notify_result


__all__ = [
    "announce_failure",
    "clear_pending",
    "enqueue_notification",
    "list_pending",
    "notify",
    "pending_path",
    "queue_path",
    "read_pending",
    "record_pending",
    "synthesis_dir",
]
