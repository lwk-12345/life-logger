#!/usr/bin/env python3
"""交付前自检：运行中的服务是不是「磁盘上这份代码」？队列能不能安全重启？

为什么需要它（2026-10-19 事故）：
服务连续跑了 40 小时，磁盘上 9 个后端文件早已更新，但界面上看不出来，
于是「做了功能却没生效」被反复误判成「功能有 bug」，一轮轮往上叠。

用法：
    python3 scripts/check_deploy_state.py            # 人类可读
    python3 scripts/check_deploy_state.py --json     # 机器可读（CI/hook）

退出码：0 = 可以交付；1 = 存在未部署代码或状态异常。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BASE = "http://127.0.0.1:8765"


def _token() -> str:
    secrets = ROOT / "output" / "local-web" / "secrets.json"
    try:
        return str(json.loads(secrets.read_text()).get("mobile_access_token") or "")
    except (OSError, ValueError):
        return ""


def _get(path: str, token: str) -> dict:
    request = Request(BASE + path, headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    token = _token()
    report: dict[str, object] = {"ok": False}
    try:
        health = _get("/api/health", token)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        report["error"] = f"服务不可达: {type(error).__name__}: {error}"
        print(json.dumps(report, ensure_ascii=False, indent=2) if as_json else report["error"])
        return 1

    raw_version = health.get("version")
    version = raw_version if isinstance(raw_version, dict) else {}
    raw_unloaded = version.get("unloaded_files")
    unloaded = list(raw_unloaded) if isinstance(raw_unloaded, list) else []
    version_valid = (
        isinstance(version.get("app_version"), str)
        and bool(version.get("app_version"))
        and isinstance(version.get("code_fingerprint"), str)
        and bool(version.get("code_fingerprint"))
        and isinstance(version.get("deployed_at"), str)
        and bool(version.get("deployed_at"))
        and version.get("deploy_state") in {"current", "stale"}
        and isinstance(raw_unloaded, list)
    )
    report.update(
        {
            "app_version": version.get("app_version"),
            "code_fingerprint": version.get("code_fingerprint"),
            "git_commit": version.get("git_commit"),
            "deployed_at": version.get("deployed_at"),
            "server_started_at": health.get("server_started_at"),
            "deploy_state": version.get("deploy_state"),
            "version_valid": version_valid,
            "unloaded_files": unloaded,
        }
    )
    if not version_valid:
        report["version_error"] = "运行服务未提供完整版本信息（可能仍是旧版）"


    active = None
    try:
        jobs = _get("/api/jobs", token)
        items = jobs.get("jobs") if isinstance(jobs, dict) else jobs
        if not isinstance(items, list):
            raise ValueError("任务列表格式无效")
        active = [j for j in items if j.get("status") in ("running", "queued")]
        report["active_jobs"] = len(active)
    except (HTTPError, URLError, TimeoutError, OSError, TypeError, ValueError):
        report["active_jobs"] = None

    ok = (
        health.get("ok") is True
        and version_valid
        and version.get("deploy_state") == "current"
        and not unloaded
    )
    report["ok"] = ok
    try:
        activity_status = _get("/api/activity-timeline/status", token)
        report["activity_timeline_busy"] = activity_status.get("busy") is True
    except HTTPError as error:
        # Older services do not yet have an activity worker.
        report["activity_timeline_busy"] = False if error.code == 404 else None
    except (URLError, TimeoutError, OSError, TypeError, ValueError):
        report["activity_timeline_busy"] = None
    report["can_restart_safely"] = report.get("active_jobs") == 0 and report["activity_timeline_busy"] is False

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"版本      : v{report['app_version']}  指纹 {report['code_fingerprint']}  "
              f"git {report['git_commit'] or 'n/a'}")
        print(f"部署时间  : {report['deployed_at']}")
        print(f"活动任务  : {report['active_jobs']}")
        if not version_valid:
            print(f"❌ 无法确认运行版本：{report['version_error']}")
        elif unloaded or version.get("deploy_state") != "current":
            print(f"❌ 未部署  : {len(unloaded)} 个后端文件比进程新")
            for name in unloaded:
                print(f"     · {name}")
            print("   建议：确认队列空闲后执行")
            print("     launchctl kickstart -k gui/$(id -u)/com.life-logger.local-web")
        else:
            print("✅ 部署状态: 运行代码 == 磁盘代码")
        if report.get("active_jobs") is None:
            print("⚠️ 活动任务数未知，不能判定可安全重启")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
