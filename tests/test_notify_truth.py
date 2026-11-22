"""告警链路真实性（2026-10-24 P1）：notify.sh / synthesis_pause / tunnel_keep 三处的严格语义。

背景（Q162）：`synthesis_pause.py` 默认调仓库外旧 notify，`tunnel_keep.sh` 用
`zsh 旧notify.sh … || true` 且紧跟一句「✅ 微信通知发送已完成」——通知失败也打印成功。
本文件用**全 stub**（不连真实通道）锁住：

* `scripts/notify.sh --json` 的 SENT / FAILED / SKIPPED 三态与退出码；
* `synthesis_pause.notify()` 解析 JSON、严格传播，超时/缺脚本/未配置都不算成功；
* `tunnel_keep.sh` 的 `notify_send()` 只在真送达时说成功，失败返回 1（不被 `|| true` 吞）。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


from life_logger import synthesis_pause

REPO = Path(__file__).resolve().parents[1]
NOTIFY = REPO / "scripts" / "notify.sh"
TUNNEL = REPO / "scripts" / "tunnel_keep.sh"


def _stub_bin(tmp_path: Path, body: str, name: str = "openclaw") -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_notify(tmp_path: Path, openclaw: Path, *args: str, env: dict | None = None):
    base_env = dict(os.environ)
    base_env.update({
        "LL_OPENCLAW": str(openclaw),
        "LL_NOTIFY_TARGET": "stub-target",
        "LL_NOTIFY_ACCOUNT": "stub-account",
        "LL_NOTIFY_DOC": str(tmp_path / "no-such-doc.md"),
        "LL_NOTIFY_MODE": "auto",
        "LL_NOTIFY_TIMEOUT": "5",
    })
    base_env.update(env or {})
    return subprocess.run(["bash", str(NOTIFY), "--json", *args],
                          capture_output=True, text=True, timeout=60, env=base_env)


def _status(proc) -> dict:
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no json status in stdout={proc.stdout!r} stderr={proc.stderr!r}")


# ---------------------------------------------------------------- notify.sh
def test_notify_sh_sent_when_output_has_delivery_evidence(tmp_path):
    oc = _stub_bin(tmp_path, 'echo "Sent via openclaw-weixin"\nexit 0\n')
    proc = _run_notify(tmp_path, oc, "结论", "补充")
    assert proc.returncode == 0, proc.stderr
    payload = _status(proc)
    assert payload["status"] == "sent" and payload["ok"] is True


def test_notify_sh_failed_when_ret_minus_two_even_if_rc_zero(tmp_path):
    """rc=0 但输出 ret=-2 / prepare failed → 必须 FAILED，不能假成功。"""
    oc = _stub_bin(tmp_path, 'echo "sendMessage ret=-2 errmsg=prepare failed"\nexit 0\n')
    proc = _run_notify(tmp_path, oc, "结论")
    assert proc.returncode == 1
    payload = _status(proc)
    assert payload["status"] == "failed" and payload["ok"] is False
    assert "ret=-2" in payload["reason"] or "prepare failed" in payload["reason"]


def test_notify_sh_failed_when_rc_zero_but_no_evidence(tmp_path):
    oc = _stub_bin(tmp_path, 'echo "ok-ish"\nexit 0\n')
    proc = _run_notify(tmp_path, oc, "结论")
    assert proc.returncode == 1
    assert _status(proc)["status"] == "failed"


def test_notify_sh_failed_on_nonzero_exit(tmp_path):
    oc = _stub_bin(tmp_path, 'echo "boom" >&2\nexit 2\n')
    proc = _run_notify(tmp_path, oc, "结论")
    assert proc.returncode == 1
    assert _status(proc)["status"] == "failed"


def test_notify_sh_skipped_when_channel_unconfigured(tmp_path):
    oc = _stub_bin(tmp_path, 'echo "Sent via x"\nexit 0\n')
    proc = _run_notify(tmp_path, oc, "结论",
                       env={"LL_NOTIFY_TARGET": "", "LL_NOTIFY_ACCOUNT": "",
                            "LL_NOTIFY_DOC": str(tmp_path / "missing.md")})
    assert proc.returncode == 3
    payload = _status(proc)
    assert payload["status"] == "skipped" and payload["ok"] is False


def test_notify_sh_skipped_when_disabled(tmp_path):
    oc = _stub_bin(tmp_path, 'echo "Sent via x"\nexit 0\n')
    proc = _run_notify(tmp_path, oc, "结论", env={"LL_NOTIFY_MODE": "disabled"})
    assert proc.returncode == 3
    assert _status(proc)["status"] == "skipped"


def test_notify_sh_timeout_is_failure_not_success(tmp_path):
    oc = _stub_bin(tmp_path, 'sleep 5\necho "Sent via late"\nexit 0\n')
    proc = _run_notify(tmp_path, oc, "结论", env={"LL_NOTIFY_TIMEOUT": "1"})
    assert proc.returncode == 1
    payload = _status(proc)
    assert payload["status"] == "failed"
    assert "超时" in payload["reason"]


def test_notify_sh_dry_run_never_sends(tmp_path):
    """[Q174] dry-run 没有真发出去 → 必须是 skipped/rc=3，不能自称 sent。"""
    marker = tmp_path / "sent.marker"
    oc = _stub_bin(tmp_path, f'touch "{marker}"\necho "Sent via dry"\nexit 0\n')
    env = dict(os.environ, LL_NOTIFY_MODE="auto", LL_OPENCLAW=str(oc), LL_NOTIFY_TARGET="t", LL_NOTIFY_ACCOUNT="a",
               LL_NOTIFY_DOC=str(tmp_path / "missing.md"))
    proc = subprocess.run(["bash", str(NOTIFY), "--json", "--dry-run", "结论"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 3, proc.stdout
    status = _status(proc)
    assert status["status"] == "skipped"
    assert status["ok"] is False
    assert "未真实发送" in status["reason"]


# ------------------------------------------------------- synthesis_pause.notify
def test_synthesis_pause_defaults_to_repo_notify_script():
    assert synthesis_pause.DEFAULT_NOTIFY_SCRIPT == str(NOTIFY)
    assert Path(synthesis_pause.DEFAULT_NOTIFY_SCRIPT).is_file()


def _stub_notify_script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "notify-stub.sh"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_synthesis_pause_propagates_sent(tmp_path, monkeypatch):
    stub = _stub_notify_script(
        tmp_path,
        'echo \'{"schema":"notify-status-v1","status":"sent","ok":true,"rc":0,"reason":"ok"}\'\n'
        'exit 0\n',
    )
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", str(stub))
    result = synthesis_pause.notify(None, "标题", "内容")
    assert result["status"] == "sent"
    assert result["notify_payload"]["status"] == "sent"


def test_synthesis_pause_propagates_skipped_and_queues(tmp_path, monkeypatch):
    stub = _stub_notify_script(
        tmp_path,
        'echo \'{"schema":"notify-status-v1","status":"skipped","ok":false,"rc":3,"reason":"未配置通道"}\'\n'
        'exit 3\n',
    )
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", str(stub))
    _marker, result = synthesis_pause.announce_failure(tmp_path, "2026-10-22", event_count=1)
    assert result["status"] == "skipped"
    assert synthesis_pause.queue_path(tmp_path).exists(), "跳过也要入队补发"


def test_synthesis_pause_propagates_failed_even_if_declared_sent(tmp_path, monkeypatch):
    """JSON 说 sent 但 rc≠0 → 判 failed（以可验证证据为准）。"""
    stub = _stub_notify_script(
        tmp_path,
        'echo \'{"status":"sent","ok":true,"rc":0,"reason":"ok"}\'\nexit 1\n',
    )
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", str(stub))
    assert synthesis_pause.notify(None, "t", "m")["status"] == "failed"


def test_synthesis_pause_timeout_is_not_success(tmp_path, monkeypatch):
    stub = _stub_notify_script(tmp_path, 'sleep 5\nexit 0\n')
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", str(stub))
    monkeypatch.setattr(synthesis_pause, "NOTIFY_TIMEOUT_SECONDS", 0.5)
    result = synthesis_pause.notify(None, "t", "m")
    assert result["status"] == "timeout"
    assert "超时" in result["detail"]


def test_synthesis_pause_missing_script_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", str(tmp_path / "nope.sh"))
    assert synthesis_pause.notify(None, "t", "m")["status"] == "script_missing"


def test_synthesis_pause_legacy_script_without_json(tmp_path, monkeypatch):
    ok = _stub_notify_script(tmp_path, 'echo "Sent via openclaw-weixin"\nexit 0\n')
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", str(ok))
    assert synthesis_pause.notify(None, "t", "m")["status"] == "sent"

    bad = _stub_notify_script(tmp_path, 'echo "Sent via x"\nexit 0\n')
    monkeypatch.setenv("LIFE_LOGGER_NOTIFY_SCRIPT", str(bad))
    # 同名文件被覆盖没关系：第二次写入的是无证据输出
    Path(bad).write_text('#!/bin/sh\necho "nothing"\nexit 0\n', encoding="utf-8")
    assert synthesis_pause.notify(None, "t", "m")["status"] == "failed"


# ------------------------------------------------------------ tunnel_keep.sh
def _source_tunnel(tmp_path: Path, notify_script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'source "{TUNNEL}"; notify_send "标题" "内容"'],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, LL_TUNNEL_KEEP_LIB_ONLY="1", LL_NOTIFY_SH=str(notify_script)),
    )


def test_tunnel_keep_reports_real_sent(tmp_path):
    stub = _stub_notify_script(
        tmp_path, 'echo \'{"status":"sent","ok":true,"rc":0,"reason":"ok"}\'\nexit 0\n')
    proc = _source_tunnel(tmp_path, stub)
    assert proc.returncode == 0, proc.stderr
    assert "已送达" in proc.stdout
    assert "已完成" not in proc.stdout


def test_tunnel_keep_failure_returns_nonzero_and_never_says_success(tmp_path):
    stub = _stub_notify_script(
        tmp_path, 'echo \'{"status":"failed","ok":false,"rc":1,"reason":"ret=-2"}\'\nexit 1\n')
    proc = _source_tunnel(tmp_path, stub)
    assert proc.returncode == 1, proc.stdout
    assert "❌" in proc.stdout and "ret=-2" in proc.stdout
    assert "✅" not in proc.stdout


def test_tunnel_keep_skipped_is_honest_not_success(tmp_path):
    stub = _stub_notify_script(
        tmp_path, 'echo \'{"status":"skipped","ok":false,"rc":3,"reason":"未配置通道"}\'\nexit 3\n')
    proc = _source_tunnel(tmp_path, stub)
    assert proc.returncode == 0
    assert "⏭" in proc.stdout and "✅" not in proc.stdout


def test_tunnel_keep_missing_notify_script_fails(tmp_path):
    proc = _source_tunnel(tmp_path, tmp_path / "absent.sh")
    assert proc.returncode == 1
    assert "❌" in proc.stdout


def test_tunnel_keep_source_has_no_swallowed_notify():
    source = TUNNEL.read_text(encoding="utf-8")
    # 注释里可以提到历史错误做法，但**可执行行**里不许再有
    code_lines = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert "$HOME/.life-logger/agent-board/notify.sh" not in code
    assert "微信通知发送已完成" not in code
    assert "notify_send" in code
    # 通知调用行不得再挂 `|| true`（那会把失败吞成 0）
    for line in source.splitlines():
        if "notify_send " in line and "()" not in line and "! notify_send" not in line:
            assert "|| true" not in line, line
    assert 'NOTIFY_SH="${LL_NOTIFY_SH:-$REPO_ROOT/scripts/notify.sh}"' in source


# ---------------------------------------------------------------- Q174 回归
# 两处 FAIL：① tunnel_keep 对 JSON status=sent 但 rc!=0 仍报成功；② notify.sh --dry-run 自称 sent。


def _tunnel_with(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    stub = _stub_notify_script(tmp_path, body)
    return _source_tunnel(tmp_path, stub)


def test_q174_tunnel_sent_with_nonzero_rc_is_failure(tmp_path):
    """status=sent 但 rc=1 → 必须失败，不能打印 ✅。"""
    proc = _tunnel_with(tmp_path, 'echo \'{"status":"sent","ok":true,"rc":0,"reason":"ok"}\'\nexit 1\n')
    assert proc.returncode == 1, proc.stdout
    assert "✅" not in proc.stdout
    assert "❌" in proc.stdout and "退出码 1" in proc.stdout


def test_q174_tunnel_sent_with_ok_false_is_failure(tmp_path):
    proc = _tunnel_with(tmp_path, 'echo \'{"status":"sent","ok":false,"rc":0,"reason":"x"}\'\nexit 0\n')
    assert proc.returncode == 1, proc.stdout
    assert "✅" not in proc.stdout
    assert "ok=false" in proc.stdout


def test_q174_tunnel_sent_requires_rc0_and_ok_true(tmp_path):
    proc = _tunnel_with(tmp_path, 'echo \'{"status":"sent","ok":true,"rc":0,"reason":"ok"}\'\nexit 0\n')
    assert proc.returncode == 0, proc.stdout
    assert "✅" in proc.stdout


def test_q174_tunnel_legacy_script_with_sent_via_is_success(tmp_path):
    """没有 JSON 的旧脚本：rc=0 且输出含 Sent via 才算成功。"""
    proc = _tunnel_with(tmp_path, 'echo "Sent via openclaw-weixin"\nexit 0\n')
    assert proc.returncode == 0, proc.stdout
    assert "✅" in proc.stdout and "legacy" in proc.stdout


def test_q174_tunnel_legacy_rc0_without_evidence_is_failure(tmp_path):
    proc = _tunnel_with(tmp_path, 'echo "nothing"\nexit 0\n')
    assert proc.returncode == 1, proc.stdout
    assert "✅" not in proc.stdout


def test_q174_tunnel_legacy_ret_minus2_rc0_is_failure(tmp_path):
    """历史形态：输出 ret=-2 却 exit 0 → 必须失败。"""
    proc = _tunnel_with(tmp_path, 'echo "sendMessage ret=-2 errmsg=prepare failed"\nexit 0\n')
    assert proc.returncode == 1, proc.stdout
    assert "✅" not in proc.stdout


def test_q174_tunnel_skipped_semantics_unchanged(tmp_path):
    proc = _tunnel_with(tmp_path, 'echo \'{"status":"skipped","ok":false,"rc":3,"reason":"未配置通道"}\'\nexit 3\n')
    assert proc.returncode == 0, proc.stdout
    assert "⏭" in proc.stdout and "✅" not in proc.stdout


def test_q174_notify_dry_run_is_skipped_never_sent(tmp_path):
    """--dry-run 没有真发出去：status=skipped、rc=3、ok=false、理由写明"未真实发送"。"""
    oc = _stub_bin(tmp_path, 'echo "would send"\nexit 0\n')
    proc = _run_notify(tmp_path, oc, "--dry-run", "标题", "内容")
    status = _status(proc)
    assert status["status"] == "skipped", status
    assert status["ok"] is False, status
    assert status["rc"] == 3, status
    assert "未真实发送" in status["reason"], status
    assert status["dry_run"] is True
    assert proc.returncode == 3, proc.returncode


def test_q174_notify_dry_run_command_failure_is_failed(tmp_path):
    oc = _stub_bin(tmp_path, 'echo "boom"\nexit 7\n')
    proc = _run_notify(tmp_path, oc, "--dry-run", "标题")
    status = _status(proc)
    assert status["status"] == "failed", status
    assert status["ok"] is False
    assert proc.returncode == 1


def test_q174_notify_dry_run_never_claims_sent_even_with_sent_via(tmp_path):
    """dry-run 即使底层打印 Sent via 也不许变成 sent（dry-run 就是没发）。"""
    oc = _stub_bin(tmp_path, 'echo "Sent via openclaw-weixin"\nexit 0\n')
    proc = _run_notify(tmp_path, oc, "--dry-run", "标题")
    status = _status(proc)
    assert status["status"] != "sent", status
    assert status["ok"] is not True


# ---------------------------------------------------------------- Q181 回归
# ① tunnel_keep.notify_send 必须有硬超时（通道卡死不能挂住巡检）
# ② notify.sh 全角邻接变量不得 unbound（已有代码修复，补测试锁定）


def _source_tunnel_env(tmp_path: Path, notify_script: Path, **env) -> subprocess.CompletedProcess:
    base = dict(os.environ, LL_TUNNEL_KEEP_LIB_ONLY="1", LL_NOTIFY_SH=str(notify_script))
    base.update(env)
    return subprocess.run(
        # tunnel_keep 带 set -e：失败时 `bash -c` 会在 notify_send 处直接退出，
        # 所以先 set +e 再取 $?，否则测不到内部判定。
        # 放在 if 里调用：既保留真实退出码，又不会被 set -e 提前中止
        ["bash", "-c",
         f'source "{TUNNEL}"; if notify_send "标题" "内容"; then echo "EXIT:0"; else echo "EXIT:1"; fi'],
        capture_output=True, text=True, timeout=60, env=base,
    )


def test_q181_tunnel_notify_has_hard_timeout(tmp_path):
    """通知脚本睡死时，notify_send 必须自己超时判 failed（不能无限挂住）。"""
    import time

    stub = _stub_notify_script(tmp_path, 'sleep 30\necho \'{"status":"sent","ok":true}\'\n')
    started = time.monotonic()
    proc = _source_tunnel_env(tmp_path, stub, LL_NOTIFY_TIMEOUT="2")
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, proc.stderr  # bash -c 自身成功退出，判定在 EXIT 行
    assert "EXIT:1" in proc.stdout, proc.stdout
    assert "超时" in proc.stdout, proc.stdout
    assert "已送达" not in proc.stdout
    assert elapsed < 15, f"内部超时没生效，耗时 {elapsed:.1f}s"


def test_q181_tunnel_notify_timeout_fires_before_outer_watchdog(tmp_path):
    """外层 8s 看门狗不该被触发：内部 20s 默认超时前应已返回（这里压到 3s 验证顺序）。"""
    import time

    stub = _stub_notify_script(tmp_path, 'sleep 30\necho \'{"status":"sent","ok":true}\'\n')
    started = time.monotonic()
    proc = _source_tunnel_env(tmp_path, stub, LL_NOTIFY_TIMEOUT="3")
    elapsed = time.monotonic() - started
    assert "EXIT:1" in proc.stdout, proc.stdout
    assert elapsed < 8, f"内部超时慢于外层看门狗（{elapsed:.1f}s）"


def test_q181_tunnel_notify_normal_sent_still_works(tmp_path):
    """加了超时包装后，正常 sent 仍必须是 rc0（防回归）。"""
    stub = _stub_notify_script(tmp_path, 'echo \'{"status":"sent","ok":true}\'\nexit 0\n')
    proc = _source_tunnel_env(tmp_path, stub, LL_NOTIFY_TIMEOUT="10")
    assert "EXIT:0" in proc.stdout, proc.stdout
    assert "已送达" in proc.stdout


def test_q181_notify_sh_fullwidth_message_has_no_unbound(tmp_path):
    """中文全角标题/正文（紧邻变量）在 set -u 下不得 unbound，且 dry-run 仍 skipped/3。"""
    oc = _stub_bin(tmp_path, 'echo "[dry-run] would run send"\nexit 0\n')
    proc = _run_notify(tmp_path, oc, "--dry-run", "结论：隧道地址已更新", "补充：请在浏览器打开：")
    combined = proc.stdout + proc.stderr
    assert "unbound variable" not in combined, combined
    payload = _status(proc)
    assert payload["status"] == "skipped"
    assert proc.returncode == 3


def test_q181_notify_sh_fullwidth_real_send_path_has_no_unbound(tmp_path):
    """非 dry-run 路径同样不得 unbound（用 stub openclaw 返回送达证据）。"""
    oc = _stub_bin(tmp_path, 'echo "Sent via openclaw-weixin"\nexit 0\n')
    proc = _run_notify(tmp_path, oc, "结论：隧道已更新", "补充：地址如下：")
    combined = proc.stdout + proc.stderr
    assert "unbound variable" not in combined, combined
    payload = _status(proc)
    assert payload["status"] == "sent", payload
    assert proc.returncode == 0


def _fallback_env(tmp_path, openclaw_body):
    fake_oc = tmp_path / "openclaw"
    fake_oc.write_text("#!/bin/sh\n" + openclaw_body + "\n")
    fake_oc.chmod(0o755)
    log = tmp_path / "mac.log"
    fake_osa = tmp_path / "osascript"
    fake_osa.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    fake_osa.chmod(0o755)
    env = dict(os.environ, LL_NOTIFY_MODE="auto", LL_OPENCLAW=str(fake_oc), LL_OSASCRIPT=str(fake_osa), LL_NOTIFY_TARGET="t",
               LL_NOTIFY_ACCOUNT="a", LL_NOTIFY_MAC_FALLBACK="1", LL_NOTIFY_TIMEOUT="1")
    return env, log


def _run_fallback(env):
    return subprocess.run(["bash", str(NOTIFY), "--json", "校验失败", "看一下"], capture_output=True, text=True, env=env)


def test_wechat_failure_falls_back_to_mac_but_still_reports_failed(tmp_path):
    env, log = _fallback_env(tmp_path, "echo 'error: prepare failed'; exit 1")
    result = _run_fallback(env)
    assert result.returncode == 1
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["status"] == "failed" and payload["ok"] is False
    assert "校验失败" in log.read_text()
    assert "Mac" in payload["reason"]


def test_wechat_timeout_falls_back_to_mac(tmp_path):
    env, log = _fallback_env(tmp_path, "sleep 5")
    assert _run_fallback(env).returncode == 1
    assert "校验失败" in log.read_text()


def test_successful_wechat_sends_no_mac_notification(tmp_path):
    env, log = _fallback_env(tmp_path, "echo 'Sent via openclaw-weixin'")
    assert _run_fallback(env).returncode == 0
    assert not log.exists()


def test_fallback_can_be_disabled(tmp_path):
    env, log = _fallback_env(tmp_path, "exit 1")
    env["LL_NOTIFY_MAC_FALLBACK"] = "0"
    assert _run_fallback(env).returncode == 1
    assert not log.exists()


def test_test_suite_never_sends_real_notifications():
    """曾有测试走到日报失败分支，真的往微信发了“日报生成失败 2026-10-09”。"""
    import os

    assert os.environ.get("LL_NOTIFY_MODE") == "disabled"


def test_notify_finds_node_next_to_openclaw_under_launchd_path(tmp_path):
    """launchd 的 PATH 很短，openclaw 依赖同目录的 node。"""
    bin_dir = tmp_path / "nodebin"
    bin_dir.mkdir()
    node = bin_dir / "node"
    node.write_text('#!/bin/sh\necho "Sent via openclaw-weixin"\n', encoding="utf-8")
    node.chmod(0o755)
    oc = bin_dir / "openclaw"
    oc.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    oc.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LL_OPENCLAW": str(oc), "LL_NOTIFY_MODE": "auto",
           "LL_NOTIFY_TARGET": "t", "LL_NOTIFY_ACCOUNT": "a", "LL_NOTIFY_DOC": str(tmp_path / "none.md"),
           "LL_NOTIFY_MAC_FALLBACK": "0"}
    proc = subprocess.run(["bash", str(NOTIFY), "--json", "结论"], capture_output=True, text=True, timeout=60, env=env)
    assert _status(proc)["status"] == "sent", proc.stdout + proc.stderr
