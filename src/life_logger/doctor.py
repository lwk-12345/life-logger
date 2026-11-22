"""Environment self-checks for the local Life Logger stack."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import URLError
from urllib.request import Request, urlopen

DEFAULT_HEALTH_URL = "http://127.0.0.1:8765/api/health"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LEGACY_REQUIRED_RESOURCE_PATHS = ("python", "asr", "vad", "template")
_MODERN_REQUIRED_RESOURCE_PATHS = ("python", "template")
_REQUIRED_WORKER_IMPORTS = ("funasr", "funasr_onnx")
_LOCAL_TURBO_WORKER_IMPORTS = ("mlx_whisper", "faster_whisper", "numpy")
_MOSS_WORKER_IMPORTS = ("mlx_audio", "mlx_audio.stt")
# Keep these defaults in sync with life_logger.moss_transcribe.  Reading the
# constants from that module would import the audio pipeline (and, on some
# hosts, native libraries) just to run a health check; doctor stays lightweight.
_DEFAULT_MOSS_PYTHON = "$HOME/.life-logger/moss-venv/bin/python"
_DEFAULT_MOSS_MODEL = (
    "~/.cache/modelscope/models/OpenMOSS--MOSS-Transcribe-Diarize/snapshots/master"
)
_LOCAL_FEATURE_MODULES = (
    "life_logger.local_reminders",
    "life_logger.patterns",
)


def _check(
    key: str,
    label: str,
    ok: bool,
    detail: str,
    fix: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ok": bool(ok),
        "detail": detail,
        "fix": fix if not ok else None,
    }


def _read_config(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, _check(
            "config",
            "本地配置",
            False,
            f"找不到 {path}",
            "复制 local-config.example.json 为 local-config.json，并填写本机资源路径。",
        )
    except (OSError, json.JSONDecodeError) as error:
        return {}, _check(
            "config",
            "本地配置",
            False,
            f"配置无法读取：{error}",
            "修复 local-config.json，使其成为可读取的 JSON 对象。",
        )
    if not isinstance(payload, dict):
        return {}, _check(
            "config",
            "本地配置",
            False,
            "配置根节点不是 JSON 对象",
            "把 local-config.json 根节点改成 JSON 对象。",
        )
    return payload, _check("config", "本地配置", True, str(path.resolve()))


def _resolve_asr_provider(config: Mapping[str, Any]) -> str:
    """Return the provider doctor should validate.

    An absent ``asr_provider`` keeps the historical Fun-ASR checks so existing
    installations do not silently lose validation.  Explicit providers are
    normalized with the same aliases used by ``local_pipeline``; unknown values
    fall back to ``local_turbo`` exactly like production instead of inventing a
    blanket set of requirements.
    """
    raw = str(config.get("asr_provider") or "").strip().lower()
    if not raw:
        return "legacy"
    if raw in {"doubao", "doubao-asr", "volcano"}:
        return "doubao"
    if raw in {"moss", "moss_transcribe_diarize", "moss-td"}:
        return "moss"
    return "local_turbo"


def _moss_runtime(config: Mapping[str, Any]) -> tuple[Path, Path]:
    python = str(
        config.get("moss_python")
        or os.environ.get("LIFE_LOGGER_MOSS_PYTHON")
        or _DEFAULT_MOSS_PYTHON
    ).strip()
    model = str(
        config.get("moss_model")
        or os.environ.get("LIFE_LOGGER_MOSS_MODEL")
        or _DEFAULT_MOSS_MODEL
    ).strip()
    return Path(python).expanduser(), Path(model).expanduser()


def _resource_checks(
    config: Mapping[str, Any], *, provider: str
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    required_paths = (
        _LEGACY_REQUIRED_RESOURCE_PATHS
        if provider == "legacy"
        else _MODERN_REQUIRED_RESOURCE_PATHS
    )
    for key in required_paths:
        value = str(config.get(key) or "").strip()
        path = Path(value).expanduser() if value else None
        ok = bool(path and path.exists())
        checks.append(
            _check(
                f"resource_{key}",
                f"资源 {key}",
                ok,
                str(path.resolve()) if ok and path else (value or "未配置"),
                f"在 local-config.json 中填写存在的 {key} 路径。",
            )
        )

    if provider == "moss":
        _, moss_model = _moss_runtime(config)
        checks.append(
            _check(
                "resource_moss_model",
                "MOSS 模型",
                moss_model.exists(),
                str(moss_model),
                "设置 moss_model 或 LIFE_LOGGER_MOSS_MODEL 指向存在的 MOSS 权重目录。",
            )
        )

    speaker = str(config.get("speaker") or "").strip()
    checks.append(
        _check(
            "resource_speaker",
            "声纹模型",
            bool(speaker),
            speaker or "未配置",
            "在 local-config.json 中设置 speaker，例如 cam++。",
        )
    )

    separation = str(config.get("speech_separation_model") or "").strip()
    if separation:
        separation_path = Path(separation).expanduser()
        checks.append(
            _check(
                "resource_speech_separation",
                "语音分离模型",
                separation_path.exists(),
                str(separation_path),
                "修正 speech_separation_model 路径，或留空以禁用该可选层。",
            )
        )
    return checks


def _worker_check(
    config: Mapping[str, Any],
    *,
    provider: str,
    runner: Callable[..., Any],
) -> dict[str, Any]:
    worker = str(config.get("python") or "").strip()
    if not worker:
        return _check(
            "worker_python",
            "Worker Python",
            False,
            "未配置 python",
            "在 local-config.json 中填写 ASR worker Python 路径。",
        )

    if provider == "doubao":
        return _check(
            "worker_python",
            "Worker Python（云端 ASR）",
            True,
            f"{worker}（Doubao 使用云 API，无本地推理依赖）",
        )

    if provider == "moss":
        moss_python, _ = _moss_runtime(config)
        worker_path = moss_python
        imports = _MOSS_WORKER_IMPORTS
        label = "MOSS Worker Python"
        fix = (
            "在 moss_python 或 LIFE_LOGGER_MOSS_PYTHON 指向的 Python 中"
            "安装 mlx-audio；不要改用主 worker venv。"
        )
    elif provider == "local_turbo":
        worker_path = Path(worker).expanduser()
        imports = _LOCAL_TURBO_WORKER_IMPORTS
        label = "Worker Python（MLX Whisper）"
        fix = (
            "在配置的 worker Python 中安装/修复 mlx_whisper、faster_whisper、numpy；"
            "不要改用系统 Python。"
        )
    else:
        worker_path = Path(worker).expanduser()
        imports = _REQUIRED_WORKER_IMPORTS
        label = "Worker Python"
        fix = (
            "在配置的 worker Python 中安装/修复 "
            f"{', '.join(imports)}；不要改用系统 Python。"
        )

    imports_text = ", ".join(imports)
    code = f"import {imports_text}; print('worker-ok')"
    try:
        result = runner(
            [str(worker_path), "-c", code],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return _check(
            "worker_python",
            label,
            False,
            f"{worker_path}: {error}",
            fix,
        )
    ok = result.returncode == 0
    detail = (
        str(worker_path)
        if ok
        else (result.stderr.strip() or result.stdout.strip() or str(worker_path))
    )
    return _check("worker_python", label, ok, detail, fix)


def _binary_check(name: str, finder: Callable[[str], str | None]) -> dict[str, Any]:
    path = finder(name)
    return _check(
        f"binary_{name}",
        name,
        bool(path),
        path or "未找到",
        f"安装 {name} 并确保它出现在 PATH 中。",
    )


def _local_feature_modules_check() -> dict[str, Any]:
    failures: list[str] = []
    for module_name in _LOCAL_FEATURE_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as error:
            failures.append(f"{module_name}: {error}")
    return _check(
        "local_feature_modules",
        "本地功能模块",
        not failures,
        "reminders / patterns 可导入"
        if not failures
        else "; ".join(failures),
        "使用项目 uv 环境运行；不要把这些本地模块安装到 Fun-ASR worker venv。",
    )


def _read_mobile_access_token(job_root: Path) -> str:
    path = Path(job_root) / "secrets.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("mobile_access_token") or "").strip()


def _service_health_check(
    *,
    web_url: str,
    web_health: Mapping[str, Any] | None,
    access_token: str,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    if web_health is not None:
        payload = dict(web_health)
    else:
        try:
            headers = (
                {"Authorization": f"Bearer {access_token}"}
                if access_token
                else {}
            )
            request = Request(web_url, headers=headers)
            with opener(request, timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, json.JSONDecodeError) as error:
            return _check(
                "service_health",
                "本地 Web 服务",
                False,
                f"{web_url}: {error}",
                "启动或重启 Life Logger 本地服务，并确认 127.0.0.1 端口可访问。",
            )
    ok = bool(payload.get("ok"))
    detail = (
        f"build={payload.get('server_build_id', 'unknown')} "
        f"started={payload.get('server_started_at', 'unknown')}"
        if ok
        else "健康接口未返回 ok=true"
    )
    return _check(
        "service_health",
        "本地 Web 服务",
        ok,
        detail,
        "重启 Life Logger 本地服务后再次运行 doctor。",
    )


def _recent_job_stats(job_root: Path, *, limit: int = 10) -> dict[str, Any]:
    jobs_dir = Path(job_root) / "jobs"
    states: list[dict[str, Any]] = []
    malformed = 0
    if jobs_dir.is_dir():
        for path in jobs_dir.glob("job-*/status.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                malformed += 1
                continue
            if isinstance(payload, dict):
                states.append(payload)
            else:
                malformed += 1
    states.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    recent = states[:limit]
    counts = Counter(str(item.get("status") or "unknown") for item in states)
    return {
        "root": str(Path(job_root).resolve()),
        "total": len(states),
        "malformed": malformed,
        "status_counts": dict(sorted(counts.items())),
        "recent": [
            {
                "job_id": item.get("job_id"),
                "status": item.get("status"),
                "updated_at": item.get("updated_at"),
                "current_stage": item.get("current_stage"),
            }
            for item in recent
        ],
    }


def run_doctor(
    config_path: Path,
    *,
    job_root: Path,
    web_url: str = DEFAULT_HEALTH_URL,
    web_health: Mapping[str, Any] | None = None,
    runner: Callable[..., Any] = subprocess.run,
    finder: Callable[[str], str | None] = shutil.which,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Run all local-stack checks without mutating the worker environment."""

    config_path = Path(config_path)
    job_root = Path(job_root)
    access_token = _read_mobile_access_token(job_root)
    config, config_check = _read_config(config_path)
    provider = _resolve_asr_provider(config)
    checks = [config_check]
    checks.extend(_resource_checks(config, provider=provider))
    checks.append(_worker_check(config, provider=provider, runner=runner))
    checks.append(_binary_check("ffmpeg", finder))
    checks.append(_binary_check("ffprobe", finder))
    checks.append(_local_feature_modules_check())
    checks.append(
        _service_health_check(
            web_url=web_url,
            web_health=web_health,
            access_token=access_token,
            opener=opener,
        )
    )
    job_stats = _recent_job_stats(job_root)
    jobs_ok = job_stats["malformed"] == 0
    counts = job_stats["status_counts"]
    status_text = (
        ", ".join(f"{key}={value}" for key, value in counts.items()) or "无任务"
    )
    checks.append(
        _check(
            "recent_jobs",
            "最近任务",
            jobs_ok,
            f"total={job_stats['total']} · {status_text}",
            "检查 output/local-web/jobs 中无法解析的 status.json。",
        )
    )
    return {
        "ok": all(item["ok"] for item in checks),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_path": str(config_path.resolve()),
        "asr_provider": provider,
        "checks": checks,
        "job_stats": job_stats,
    }


def render_doctor_report(report: Mapping[str, Any]) -> str:
    """Render a terminal-friendly report with actionable per-item status."""

    lines = ["Life Logger doctor"]
    for item in report.get("checks", []):
        mark = "✅" if item.get("ok") else "❌"
        lines.append(f"{mark} {item.get('label')}: {item.get('detail')}")
        if not item.get("ok") and item.get("fix"):
            lines.append(f"   修复: {item['fix']}")
    stats = report.get("job_stats") or {}
    recent = stats.get("recent") or []
    if recent:
        lines.append("最近任务:")
        for item in recent[:5]:
            lines.append(
                "  "
                f"{item.get('job_id')} · {item.get('status')} · "
                f"{item.get('current_stage') or '—'}"
            )
    lines.append("结果: " + ("全部通过" if report.get("ok") else "存在需要修复的项目"))
    return "\n".join(lines)
