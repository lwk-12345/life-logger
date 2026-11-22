# 变慢急救卡

> 结论：**没有「越跑越多」的内存/缓存**。实测 read_cache 14/32（有上限有淘汰）、
> transcript/utterance/search 缓存全 0、LocalWebServer 无无上限容器。
> 真正的「累积物」是**并发排队链**：读生产者只给 2 个名额、等 8s；抢不到 flight 还要先等 10s
> → 10+8=18s，链条一长就是 305s。重启能好，是因为它清空了等待链，不是「清了内存」。

## 一、5 秒判断「代码慢」还是「环境卡」

```bash
ps -p $(pgrep -f run_local_web) -o pid,%cpu,rss,etime          # CPU 高=环境抢；低=卡在等锁
PYTHONPATH=src .venv/bin/python -c "import sys;sys.path[:0]=['src','scripts'];import rerun_daily_synthesis as rr,time;app=rr._build_app();t=time.time();ev,led=rr._events_for(app,'2026-10-22');print(f'进程内 {time.time()-t:.2f}s events={len(ev)}')"
tail -5 output/local-web/launchd.stderr.log                      # 有没有模型超时在刷
```

| 现象 | 结论 |
|---|---|
| 进程内 <1s，HTTP >5s | **不是代码慢**，是排队/环境 |
| 进程内也 >5s | 真慢，查磁盘或事件量暴涨 |
| stderr 刷 `failed (timed out)` | 网关拖累 → **别重启服务**，先治网关 |

## 二、先看三个数字

```bash
curl -s -H "Authorization: Bearer $(python3 -c "import json;print(json.load(open('output/local-web/secrets.json'))['mobile_access_token'])")" http://127.0.0.1:8765/api/debug/read-cache | python3 -m json.tool | head -30
```
1. **thread_count** 正常 5~6；涨到几十 = 请求在排队（每次卡 10s/8s）
2. **rusage_maxrss_mb** 峰值，>800MB 才在意（实测峰值 708MB，偏高但未越线）
3. **read_cache.entries** 正常 12~20 / 上限 32；顶满且反复淘汰 = 命中率崩、冷算变多

## 三、该不该重启

**该重启（三条同时成立）**：`/api/daily` 连续 2 次 >3s；CPU 长期 >80% 且 stderr 无模型超时；entries 顶满且线程持续 >20。

**不该重启（重启会掩盖问题）**：
- 只是**单次**慢（冷启动本来 0.4s 级，一次样本不算）
- stderr 在刷模型超时 —— 重启只清等待链，磁盘/网关问题还在，几小时后照样 305s
- 正在跑日报生成 / 写账本 / 传 Watch 分段
- 只是「感觉慢」没有数字 —— 没前后对比的重启等于什么都没做

## 四、怎么确认重启真的好了

```bash
python3 scripts/ops_health.py --json > /tmp/pre.json
launchctl kickstart -k gui/$(id -u)/com.life-logger.local-web; sleep 3
python3 scripts/ops_health.py --json > /tmp/post.json
python3 -c "import json;a=json.load(open('/tmp/pre.json'));b=json.load(open('/tmp/post.json'));print('daily',a['api']['daily_yesterday']['elapsed_s'],'→',b['api']['daily_yesterday']['elapsed_s']);print('金额',[r.get('amount_cny') for r in a['ledger_baseline']],'→',[r.get('amount_cny') for r in b['ledger_baseline']])"
```
三件事同时成立才算好：daily <2s、金额不变（10-22 ¥10.0 / 10-21 ¥26.8 / 10-20 ¥75.0）、`/api/health` 的 build 等于磁盘代码。

## 五、看门狗

```bash
python3 scripts/perf_watchdog.py --once     # 采一次；退出码 2 = 报了警
python3 scripts/perf_watchdog.py            # 常驻（60s 一次）
```
告警落 `output/local-web/perf-alerts.jsonl`（**不是** `web/perf-alerts.jsonl`）；基线 `output/perf-baseline.json`；
退化判据＝比基线慢 5 倍且 >0.3s；起进程前 5 分钟不报警（`--cold-start-grace 300`）；
退出码 2=报过警 / 0=正常 / 其它=脚本自身出错。

## 六、怎么区分「一个请求慢」vs「所有请求都在排队」

```bash
TOK=$(python3 -c "import json;print(json.load(open('output/local-web/secrets.json'))['mobile_access_token'])")
B=http://127.0.0.1:8765
H="Authorization: Bearer $TOK"

# ① 连打同一接口 3 次：第 2、3 次明显变快 = 只是单请求慢（缓存生效）
for i in 1 2 3; do curl -s -o /dev/null -m 60 -w "same#$i %{time_total}s\n" -H "$H" "$B/api/daily?date=2026-10-22"; done

# ② 连打 3 个不同接口：一次比一次慢 = 在排队（名额被占）
for p in "/api/daily?date=2026-10-21" "/api/stats" "/api/dashboard/daily?date=2026-10-22"; do
  curl -s -o /dev/null -m 60 -w "$p %{time_total}s\n" -H "$H" "$B$p"; done

# ③ 直接看排队深度（最直接）
curl -s -H "$H" "$B/api/debug/read-inflight" | python3 -m json.tool
```

判读表：

| 现象 | 含义 |
|---|---|
| ① 递减、② 平稳、`waiting_for_slot=0` | **单个请求慢**（冷算/大日期），不是排队 |
| ① 不降、② 递增、`waiting_for_slot>0` | **全在排队**：名额被占满，等的人在累积 |
| `active_producers == producer_limit` 且 `waiting_for_slot` 持续 >0 | 已经堵住，等 1~2 分钟看它是否自解 |
| `inflight_entries` 远大于 `producer_limit` | 冷 key 太多（不同日期/区间一起打）→ 前端在并发轰炸 |

配合 watchdog（自动判定）：

```bash
python3 scripts/perf_watchdog.py --once                 # 采一次；exit 2 = 报警
python3 scripts/perf_watchdog.py --interval 30          # 常驻
```
- `queue_backlog` 两种触发：**rising**（连续 3 个采样点深度递增）或 **sustained**
  （`waiting_for_slot>0` 持续超过 `--queue-sustain-seconds`，默认 60s）
- 端点不存在（旧进程 404）时打印一行跳过，不崩、不误报
