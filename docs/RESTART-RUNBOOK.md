# 服务重启 Runbook

> 一句话：**重启是最后手段，不是常规操作。** 先跑 `python3 scripts/ops_health.py`，
> 按本文档的阈值判断；任何一项「绝对不能重启」的情况命中，就不要动。

最后核对：2026-10-23 03:2x（本机实测，服务 build `d8657ed5aae1`，PID 86295，02:48 启动）

---

## 0. 先记住三个坑（都是踩过的）

| # | 坑 | 事实 |
|---|---|---|
| 1 | `/api/health/summary` **不是服务健康** | 它返回的是**手表健康数据**（心率 / 步数 / 睡眠）：`{"summary":{"avg_heart_rate":109.4,...}}`。服务健康看 **`/api/health`**（`ok` / `server_build_id` / `server_started_at`） |
| 2 | 门禁报告文件名 | 实际是 **`output/gate-result.json`**，由 `verify_all.sh --json[=path]` 生成；`output/gate-report.json` 这个名字**不存在**（`ops_health.py` 两个都认） |
| 3 | 重启归属要说清 | 2026-10-22 深夜那次「305s → 0.47s」，是 **④4号 按用户指令重启**的，不是 A0 的 kickstart。别再靠记忆判断「谁修的」——本文档最后有归属记录表 |

---

## 1. 这台机器上到底有哪些常驻任务

`launchctl list | grep -i life-logger` 实测（2026-10-23 03:2x）：

| Label | 跑什么 | 频率 | 日志 | 单独停止 |
|---|---|---|---|---|
| `com.life-logger.local-web` | 本地 Web 服务（8765：网页端 + 全部 API） | 常驻（KeepAlive 失败重启） | `output/local-web/launchd.stdout.log` / `.stderr.log` | `launchctl bootout gui/$(id -u)/com.life-logger.local-web` |
| `com.life-logger.daily-synthesis` | 生成「前一天」日报 | 每日 **01:30** | `~/Library/Logs/life-logger-daily-synthesis.out.log` | `launchctl bootout …daily-synthesis` |
| `com.life-logger.backup` | 备份权威数据到 `backups/` | 每日 **03:00** | `~/Library/Logs/life-logger-backup.log` | `launchctl bootout …backup` |
| `com.life-logger.daily-acceptance` | 跑验收脚本 | 每日 **07:30** | `output/local-web/acceptance.stdout.log` | `launchctl bootout …daily-acceptance` |
| `com.life-logger.nightly-verify` | 夜间校验（含门禁） | 每日 **09:00** | `~/Library/Logs/life-logger-nightly-verify.log` | `launchctl bootout …nightly-verify` |
| `com.life-logger.bonjour` | mDNS 广播（局域网发现这台 Mac） | 常驻（KeepAlive） | `/tmp/life-logger-bonjour.log` | `launchctl bootout …bonjour` |
| `com.life-logger.tunnel-keep` | 保活公网隧道 | 每 **300s** | `~/Library/Logs/life-logger-tunnel-keep.out.log` | `launchctl bootout …tunnel-keep` |
| `com.life-logger.watch-profile-reminder` | 提醒更新 Watch 描述文件 | 每日 **10:00 / 16:00** | `/tmp/watch-profile-reminder.log` | `launchctl bootout …watch-profile-reminder` |
| `com.life-logger.airpassport-watch` | AirPassport BLE 录音落盘守护 | 常驻（失败重启） | `output/local-web/airpassport-watch.stdout.log` | `launchctl bootout …airpassport-watch` |
| `com.life-logger.decision-shadow` | 决策影子任务（每 30s） | **⛔ 未加载** | `output/local-web/decision-shadow/worker.stdout.log` | plist 在 `~/Library/LaunchAgents/`，`bootstrap` 过才会跑 |

**另外两个不属于 Life Logger、但会抢 CPU 的**（`launchctl list | grep -i dsh`）：

| Label | 说明 |
|---|---|
| `com.dsh.agent-loop` | DSH 调度循环（常驻，KeepAlive），日志 `~/.life-logger/agent-board/log/loop-launchd.out` |
| `com.lwk.dsh-remote-serve-sync` | 每 20s 同步远端服务配置，无日志文件 |

> ⚠️ 排查「服务变慢」时先看 dsh 那两个有没有在跑重活（今晚 305s 那次就是后台模型超时把
> `/api/daily` 的请求饿死了，不是 handler 逻辑慢——同样数据在进程内构建只要 0.23~0.34s）。

---

## 2. 重启命令与风险

```bash
# 标准重启（launchd 托管，会自动带环境变量）
launchctl kickstart -k gui/$(id -u)/com.life-logger.local-web
```

- **`-k` 的含义**：先 `SIGTERM` 杀掉当前实例，再重新拉起。**不是** reload、不是热更新。
- **风险 1**：正在处理的请求会被直接中断（页面 502/连接重置）。用户正在写数据时尤其危险。
- **风险 2**：如果被杀的那一刻正在写 `output/local-web/` 下的 JSON，理论上可能留下半截文件
  （写盘用的是原子替换 `os.replace`，风险低但非零）。
- **风险 3**：Watch 正在上传分段时被杀，可能丢一段（分段可重传，但会多一次往返）。
- **风险 4**：`launchctl list` 里该 label 的「上次退出码」会变成 `-15`（= SIGTERM）。
  **看到 `-15` 不要以为是崩溃**，那是被 `-k` 杀的痕迹（当前就是这么显示的）。

**不要用**：`kill -9 <pid>`（绕过 launchd，可能不会自动拉起）、`pkill -f run_local_web`（会杀掉调试用的临时实例）。

---

## 3. 判断该不该重启：四条硬指标

来自 ②2号 的实测口径。**必须是「连续」越线，不是单次：**

| 指标 | 阈值 | 连续判定 | 为什么要这个阈值 |
|---|---|---|---|
| `/api/daily` 响应时间 | **> 3s** | 连续 **2 次** | 正常实测 0.009~0.42s；>3s 说明已经影响使用 |
| 冷请求响应时间 | **> 10s** | 连续 **2 次** | 正常冷启动 0.4s 级；>10s 说明线程被饿死 |
| RSS | **> 800 MB** | 连续 **2 个采样** | 实测波动大（同一进程两次采样 314.7MB → 70.8MB），单次不可信 |
| 线程数 | **> 20** | 持续 **5 分钟** | 线程数只增不减通常是后台循环卡住（`ps -M -p <pid>` 数行数） |

配套命令：

```bash
python3 scripts/ops_health.py            # 一次采样，人看
python3 scripts/ops_health.py --json     # 机器看，便于连续两次比对
```

`ops_health.py` 只做**单次采样**，连续判定靠调用方（门禁 / nightly-verify / 你自己连跑两次）。

---

## 4. 重启前后必须做的三件事

### 重启前：留证（否则事后无法判断「重启有没有用」）

```bash
python3 scripts/ops_health.py --json > /tmp/pre-restart-$(date +%H%M%S).json
```
记录下：**PID / RSS / 线程数 / `/api/daily` 耗时**。没有这组数字，重启就只是「感觉快了」。

### 重启后立刻：打服务健康

```bash
launchctl kickstart -k gui/$(id -u)/com.life-logger.local-web
sleep 3
curl -s -H "Authorization: Bearer $(python3 -c "import json;print(json.load(open('output/local-web/secrets.json'))['mobile_access_token'])")" \
  http://127.0.0.1:8765/api/health | python3 -m json.tool | head -6
```
看三个字段：`ok: true`、`server_build_id`（应等于磁盘代码）、`server_started_at`（应是刚才）。

> ⚠️ **不要**打 `/api/health/summary` 当服务健康——那是手表数据（见第 0 节坑 1）。

### 重启后：打昨天日报，确认 < 2s

```bash
time curl -s -o /dev/null -w "HTTP %{http_code} %{time_total}s\n" \
  -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:8765/api/daily?date=$(date -v-1d +%F)"
```
**必须 < 2s**（实测基线 0.009~0.42s）。如果还是慢，说明问题不在服务进程，去看 dsh 后台 / 模型网关。

---

## 5. 重启之后的验证清单

### 5.1 金额对不对（最近三天账本基准，2026-10-23 实测）

| 日期 | 事件数 | 金额 | pending |
|---|---|---|---|
| 2026-10-22 | 17 | **¥10.0** | 0 |
| 2026-10-21 | 4 | **¥26.8** | 1 |
| 2026-10-20 | 35 | **¥75.0** | 0 |

```bash
python3 scripts/ops_health.py | grep -A4 "账本基准"
```
三个数对不上任何一天 → **不要继续**，先查是不是重启把数据写坏了（`backups/` 有每日备份可回滚，
见 `scripts/restore_data.sh`）。

### 5.2 其余三项

```bash
python3 scripts/ops_health.py            # 结论应为 PASS，四条指标都没越线
bash scripts/verify_all.sh --fast        # 代码/测试门禁应 PASS
git status --short                       # 应为空（重启不该产生文件变更）
```

---

## 6. 什么情况**绝对不能**自己重启

| 情况 | 怎么判断 | 为什么 |
|---|---|---|
| **正在跑日报生成** | `launchctl list \| grep daily-synthesis` PID 不是 `-`；或 `~/Library/Logs/life-logger-daily-synthesis.out.log` 3 分钟内有新行 | 会中断模型调用，浪费一次完整调用（实测 1 次 ≈ 5k~27k tokens） |
| **正在写账本 / 正在上传 Watch 分段** | `output/local-web/watch-sessions/**/segments/*.m4a` 最近 3 分钟有新文件 | 可能丢分段，用户录音不可再生 |
| **用户没说、也没人要求** | — | 重启是变更，不是维护；**没有授权就不动** |
| **只是在「感觉慢」** | `ops_health.py` 结论 PASS | 单次慢通常是冷启动，连测 2 次再判断 |
| **门禁正在跑** | `pgrep -f verify_all.sh` 有输出 | 门禁自己会打接口，重启会让它误判 FAIL |

拍照留证的一句话模板（写进交接）：

> `2026-10-23 03:2x 由 ④4号 按用户指令重启 local-web：重启前 PID 64228 / RSS 314.7MB /
> daily 120s 超时 → 重启后 PID 86295 / RSS 70.8MB / daily 0.387s。`

---

## 7. 归属记录（谁重启的、为什么、好了没）

| 时间 | 谁 | 为什么 | 结果 |
|---|---|---|---|
| 2026-10-22 19:45 | 其他窗口 | 加载后端新代码 | 运行代码 == 磁盘代码 |
| 2026-10-22 23:54 | 其他窗口 | 加载 merge/前端新代码 | 冷请求一度 100~305s（后台模型超时，非代码） |
| **2026-10-23 02:48** | **④4号（按用户指令）** | 冷请求 305s → 需要把后台超时清掉 | **daily 305s → 0.47s**；实测 03:2x daily 0.387s |

> 记这一栏的用意：别再出现「A0 以为是自己 kickstart 修好的」这类误会。
> 每次重启都往这里加一行，**先写前因后果再动手**。

---

## 8. 常用命令速查

```bash
# 谁在跑
launchctl list | grep -i life-logger
launchctl list | grep -i dsh

# 单个任务详情（含上次退出码、stdout 路径）
launchctl print gui/$(id -u)/com.life-logger.local-web

# 体检
python3 scripts/ops_health.py
python3 scripts/ops_health.py --json

# 重启（确认过第 6 节后）
launchctl kickstart -k gui/$(id -u)/com.life-logger.local-web

# 停止某个定时任务（不是删除）
launchctl bootout gui/$(id -u)/com.life-logger.backup
# 恢复
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.life-logger.backup.plist

# 回滚数据（若重启后发现数据坏了）
scripts/restore_data.sh backups/life-logger-<ts>.tar.gz --yes
```
