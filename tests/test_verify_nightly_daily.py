"""verify_nightly_daily.sh 的通知语义回归（P0：不许假成功）。

四条路径：
  1) 验证 PASS + 通知成功        → 退出码 0，输出含「✅ …（notify rc=0）」
  2) 验证 PASS + 通知失败        → 退出码 4，输出必须含「❌ 微信通知失败」，**不得出现任何「成功/已发送」字样**
  3) 验证 FAIL + 通知成功        → 退出码 1，输出含「✅ 微信告警通知已发送」
  4) 验证 FAIL + 通知失败        → 退出码 1，输出含「❌ 微信通知失败」
另外：--dry-run 不调用通知（stub 不被执行、不留痕）。

为什么用 LL_FORCE_VERDICT：A/B/C 判据依赖真实业务数据（某天是否有产物、日志窗口、账本一致性），
在单测里不可能稳定复现 PASS 与 FAIL 两种情形；因此脚本提供了**仅测试用**的判决覆盖开关，
生产环境不设该变量即走真实判据。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_nightly_daily.sh"


def _stub(dirpath: Path, rc: int, output: str | None = None, sleep: float = 0.0) -> Path:
    """写一个假的 notify.sh：把参数落到 marker 文件，再按指定 rc / 输出退出。

    output 默认在 rc==0 时打印 "Sent via openclaw-weixin"——新语义要求「成功必须有送达证据」，
    只给 rc=0 不再算成功（这正是 P1 假成功的核心）。
    """
    marker = dirpath / "notify-called.txt"
    stub = dirpath / "notify_stub.sh"
    text = output if output is not None else ("Sent via openclaw-weixin" if rc == 0 else "")
    body = "#!/bin/zsh\n"
    body += f'echo "head=$1" >> "{marker}"\n'
    body += f'echo "body=$2" >> "{marker}"\n'
    if sleep:
        body += f"sleep {sleep}\n"
    if text:
        body += f"echo {text!r}\n"
    body += f"exit {rc}\n"
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _run(tmp_path: Path, verdict: str, notify_rc: int | None, dry_run: bool = False,
         stub_output: str | None = None, timeout_seconds: str | None = None,
         extra_env: dict[str, str] | None = None, no_notify_sh: bool = False):
    env = dict(os.environ)
    env.pop("LL_NOTIFY_SH", None)
    env["LL_FORCE_VERDICT"] = verdict
    if notify_rc is None:
        if not no_notify_sh:
            env["LL_NOTIFY_SH"] = str(tmp_path / "does-not-exist.sh")
    else:
        sleep = 5.0 if timeout_seconds else 0.0
        env["LL_NOTIFY_SH"] = str(_stub(tmp_path, notify_rc, stub_output, sleep))
    if timeout_seconds:
        # 直接走仓库内 notify.sh（它才有超时逻辑），用 LL_OPENCLAW 指向慢 stub
        env.pop("LL_NOTIFY_SH", None)
        slow = tmp_path / "oc_slow"
        slow.write_text("#!/bin/sh\nsleep 5\necho 'Sent via openclaw-weixin'\n", encoding="utf-8")
        slow.chmod(0o755)
        env.update({"LL_OPENCLAW": str(slow), "LL_NOTIFY_TIMEOUT": timeout_seconds,
                    "LL_NOTIFY_TARGET": "t", "LL_NOTIFY_ACCOUNT": "a"})
    for k, v in (extra_env or {}).items():
        env[k] = v
    if "LL_OPENCLAW" in env:  # 走假 openclaw 才放开发送；conftest 默认禁止真实通知
        env["LL_NOTIFY_MODE"] = "auto"
    args = ["/bin/bash", str(SCRIPT), "--date", "2026-10-22"]
    if dry_run:
        args.append("--dry-run")
    proc = subprocess.run(args, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
    return proc


SUCCESS_WORDS = ("✅ 微信通知已发送", "✅ 微信告警通知已发送")


def _assert_no_false_success(out: str) -> None:
    for word in SUCCESS_WORDS:
        assert word not in out, f"通知失败时不允许出现成功字样，但输出里有：{word}\n---\n{out}"


def test_pass_plus_notify_ok_exits_0(tmp_path: Path):
    proc = _run(tmp_path, "pass", notify_rc=0)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "✅ 微信通知已发送（Sent via 已确认）" in proc.stdout
    assert (tmp_path / "notify-called.txt").is_file(), "应当真的调用了通知脚本"


def test_pass_plus_notify_fail_exits_4_and_says_failed(tmp_path: Path):
    proc = _run(tmp_path, "pass", notify_rc=1)
    assert proc.returncode == 4, f"验证 PASS 但通知失败必须 exit 4，实际 {proc.returncode}\n{proc.stdout}"
    assert "❌ 微信通知失败" in proc.stdout
    assert "rc=1" in proc.stdout
    _assert_no_false_success(proc.stdout)
    assert "退出码 4" in proc.stdout


def test_pass_plus_notify_missing_exits_4(tmp_path: Path):
    proc = _run(tmp_path, "pass", notify_rc=None)  # 通知脚本不存在 → rc=127 路径
    assert proc.returncode == 4, proc.stdout
    assert "❌ 微信通知失败" in proc.stdout
    assert "rc=127" in proc.stdout
    _assert_no_false_success(proc.stdout)


def test_verify_fail_plus_notify_ok_exits_1(tmp_path: Path):
    proc = _run(tmp_path, "fail", notify_rc=0)
    assert proc.returncode == 1, proc.stdout
    assert "✅ 微信告警通知已发送（Sent via 已确认）" in proc.stdout
    # 验证失败时，普通「已发送」也不算数（语义上只有告警那条）
    assert "✅ 微信通知已发送" not in proc.stdout


def test_verify_fail_plus_notify_fail_still_exits_1(tmp_path: Path):
    proc = _run(tmp_path, "fail", notify_rc=1)
    assert proc.returncode == 1, f"验证 FAIL 时退出码必须是 1（通知成败不能改成 0），实际 {proc.returncode}"
    assert "❌ 微信通知失败" in proc.stdout
    _assert_no_false_success(proc.stdout)


def test_dry_run_never_calls_notify(tmp_path: Path):
    proc = _run(tmp_path, "pass", notify_rc=0, dry_run=True)
    assert proc.returncode == 0, proc.stdout
    assert "[DRY-RUN] 跳过微信推送" in proc.stdout
    assert not (tmp_path / "notify-called.txt").exists(), "--dry-run 不许调用通知脚本"
    _assert_no_false_success(proc.stdout)


def test_dry_run_on_fail_keeps_exit_1(tmp_path: Path):
    proc = _run(tmp_path, "fail", notify_rc=0, dry_run=True)
    assert proc.returncode == 1, proc.stdout
    assert not (tmp_path / "notify-called.txt").exists()
    _assert_no_false_success(proc.stdout)


def test_unknown_force_verdict_is_rejected(tmp_path: Path):
    proc = _run(tmp_path, "bogus", notify_rc=0)
    assert proc.returncode == 2, proc.stdout
    assert "未知 LL_FORCE_VERDICT" in proc.stderr


# ---------------------------------------------------------------------------
# P1（2026-10-24）：假成功的真正来源——notify.sh 明明输出 ret=-2，却 exit 0
# ---------------------------------------------------------------------------

def test_rc0_with_prepare_failed_is_not_success(tmp_path: Path):
    """rc=0 + `sendMessage ret=-2 errmsg=prepare failed` → 必须判失败，不许打印成功。"""
    proc = _run(tmp_path, "fail", notify_rc=0,
                stub_output="sendMessage ret=-2 errmsg=prepare failed")
    assert proc.returncode == 1, proc.stdout
    _assert_no_false_success(proc.stdout)
    assert "❌ 微信通知失败" in proc.stdout
    assert ("ret=-2" in proc.stdout) or ("prepare failed" in proc.stdout), proc.stdout


def test_rc0_with_prepare_failed_on_pass_path_exits_4(tmp_path: Path):
    proc = _run(tmp_path, "pass", notify_rc=0,
                stub_output="sendMessage ret=-2 errmsg=prepare failed")
    assert proc.returncode == 4, proc.stdout
    _assert_no_false_success(proc.stdout)


def test_rc0_without_sent_via_is_not_success(tmp_path: Path):
    """rc=0 但没有任何送达证据（缺 Sent via）→ 仍算失败。"""
    proc = _run(tmp_path, "pass", notify_rc=0, stub_output="queued")
    assert proc.returncode == 4, proc.stdout
    _assert_no_false_success(proc.stdout)


def test_error_line_with_rc0_is_failure(tmp_path: Path):
    proc = _run(tmp_path, "pass", notify_rc=0, stub_output="error: upstream 502")
    assert proc.returncode == 4, proc.stdout
    _assert_no_false_success(proc.stdout)


def test_notify_timeout_is_failure(tmp_path: Path):
    """走仓库内 notify.sh 的超时路径：LL_NOTIFY_TIMEOUT=1 + 慢 stub。"""
    proc = _run(tmp_path, "pass", notify_rc=0, timeout_seconds="1")
    assert proc.returncode == 4, proc.stdout + proc.stderr
    _assert_no_false_success(proc.stdout)
    assert "超时" in proc.stdout, proc.stdout


def test_unconfigured_channel_is_skipped_not_success(tmp_path: Path):
    """未配置通道 → 显式 SKIPPED，既不算成功也不改 PASS 的退出码。"""
    proc = _run(tmp_path, "pass", notify_rc=None, no_notify_sh=True,
                extra_env={"LL_NOTIFY_MODE": "disabled"})
    assert proc.returncode == 0, proc.stdout
    _assert_no_false_success(proc.stdout)
    assert "跳过" in proc.stdout or "SKIPPED" in proc.stdout, proc.stdout


def test_unconfigured_channel_on_fail_path_still_exits_1(tmp_path: Path):
    proc = _run(tmp_path, "fail", notify_rc=None, no_notify_sh=True,
                extra_env={"LL_NOTIFY_MODE": "disabled"})
    assert proc.returncode == 1, proc.stdout
    _assert_no_false_success(proc.stdout)
    assert "跳过" in proc.stdout or "SKIPPED" in proc.stdout, proc.stdout


def test_abnormal_exit_code_is_failure(tmp_path: Path):
    proc = _run(tmp_path, "pass", notify_rc=137, stub_output="killed")
    assert proc.returncode == 4, proc.stdout
    _assert_no_false_success(proc.stdout)
    assert "137" in proc.stdout, proc.stdout


def test_error_reason_is_sanitized(tmp_path: Path):
    """原始错误要保留，但手机号/token/target 必须脱敏。"""
    proc = _run(tmp_path, "pass", notify_rc=1,
                stub_output="boom 13800138000 sk-abcdefghijklmnop target=wxid_secret")
    assert proc.returncode == 4, proc.stdout
    assert "13800138000" not in proc.stdout, "手机号不得明文"
    assert "sk-abcdefghijklmnop" not in proc.stdout, "token 不得明文"
    assert "wxid_secret" not in proc.stdout, "target 不得明文"


# ---------------------------------------------------------------------------
# scripts/notify.sh 自身的 JSON 契约
# ---------------------------------------------------------------------------

NOTIFY_SH = ROOT / "scripts" / "notify.sh"


def _notify(tmp_path: Path, *args: str, env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    env.pop("LL_NOTIFY_SH", None)
    env.update({"LL_NOTIFY_TARGET": "t", "LL_NOTIFY_ACCOUNT": "a"})
    env["LL_NOTIFY_MODE"] = "auto"
    env.update(env_extra or {})
    return subprocess.run(["/bin/bash", str(NOTIFY_SH), *args], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=120)


def _oc_stub(tmp_path: Path, output: str, rc: int = 0) -> Path:
    p = tmp_path / f"oc_{abs(hash(output)) % 9999}"
    p.write_text(f"#!/bin/sh\necho {output!r}\nexit {rc}\n", encoding="utf-8")
    p.chmod(0o755)
    return p


def test_notify_json_sent_contract(tmp_path: Path):
    import json as _json
    oc = _oc_stub(tmp_path, "Sent via openclaw-weixin")
    proc = _notify(tmp_path, "--json", "标题", "补充", env_extra={"LL_OPENCLAW": str(oc)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    status = _json.loads(proc.stdout.strip().splitlines()[-1])
    assert status["status"] == "sent" and status["ok"] is True and status["rc"] == 0


def test_notify_json_failed_contract_on_ret_minus2(tmp_path: Path):
    import json as _json
    oc = _oc_stub(tmp_path, "sendMessage ret=-2 errmsg=prepare failed")
    proc = _notify(tmp_path, "--json", "标题", env_extra={"LL_OPENCLAW": str(oc)})
    assert proc.returncode == 1, proc.stdout
    status = _json.loads(proc.stdout.strip().splitlines()[-1])
    assert status["status"] == "failed" and status["ok"] is False
    assert "prepare failed" in status["reason"] or "ret=-2" in status["reason"]


def test_notify_json_skipped_contract_when_disabled(tmp_path: Path):
    import json as _json
    proc = _notify(tmp_path, "--json", "标题", env_extra={"LL_NOTIFY_MODE": "disabled"})
    assert proc.returncode == 3, proc.stdout
    status = _json.loads(proc.stdout.strip().splitlines()[-1])
    assert status["status"] == "skipped" and status["ok"] is False


def test_notify_json_skipped_when_no_channel(tmp_path: Path):
    import json as _json
    env = {"LL_NOTIFY_DOC": "/nonexistent"}
    proc = subprocess.run(["/bin/bash", str(NOTIFY_SH), "--json", "标题"], cwd=str(ROOT),
                          env={**{k: v for k, v in os.environ.items()
                                  if k not in ("LL_NOTIFY_TARGET", "LL_NOTIFY_ACCOUNT")}, **env},
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 3, proc.stdout
    status = _json.loads(proc.stdout.strip().splitlines()[-1])
    assert status["status"] == "skipped"


def _run_real_verdict(tmp_path: Path, ledger_body: str):
    """走真实判据，只把账本校验那一步换成桩；其余 python 调用交给当前解释器。"""
    wrapper = tmp_path / "python_stub"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case "$*" in *verify_ledger_consistency*)\n'
        f"{ledger_body}\n"
        ";; esac\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env = dict(os.environ)
    env.pop("LL_FORCE_VERDICT", None)
    env["LL_PYTHON"] = str(wrapper)
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), "--date", "2026-10-22", "--dry-run"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180,
    )


def test_ledger_script_crash_is_not_reported_as_inconsistency(tmp_path):
    proc = _run_real_verdict(
        tmp_path, "echo \"ModuleNotFoundError: No module named 'numpy'\"; exit 1"
    )
    assert proc.returncode == 1
    assert "校验脚本没跑起来" in proc.stdout
    assert "numpy" in proc.stdout
    assert "账本一致性校验失败" not in proc.stdout


def test_real_ledger_mismatch_still_reported(tmp_path):
    proc = _run_real_verdict(
        tmp_path, "echo '2026-10-22 FAIL'; echo '汇总: 1 天 | 失败 1 天 | 结论 FAIL'; exit 1"
    )
    assert proc.returncode == 1
    assert "账本一致性校验失败" in proc.stdout
    assert "校验脚本没跑起来" not in proc.stdout


def test_ledger_pass_passes_criterion_c(tmp_path):
    proc = _run_real_verdict(tmp_path, "echo '汇总: 1 天 | 失败 0 天 | 结论 PASS'; exit 0")
    assert "判据 C (账本一致性 5条全过): PASS" in proc.stdout
