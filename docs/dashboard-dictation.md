# 生活看板隐藏「电脑前说话」(dictation)

2026-10-13 root 拍板：**数据全留、看板看不到**。dictation（对着电脑说话）的内容一条都不
从产物里删，只在 API 读层过滤，并且可以一键恢复原样。

实测（2026-10-13）：`GET /api/daily?date=2026-10-13` 事件 12 条 → 6 条，5 条「设计沟通」调试
话从看板消失；`output/local-web/` 下的产物文件（events.json / context-extraction.json /
dictation-windows.jsonl）mtime 与 sha256 完全未变。

## 开关（`local-config.json`）

| 配置项 | 默认 | 作用 |
|---|---|---|
| `hide_dictation_from_dashboard` | `true` | 写 `false` 即完全恢复原样 |
| `dictation_windows_extra` | `[]` | 手工补的 dictation 时间窗，形如 `[{"start": "...", "end": "..."}]`。用于 PTT（AirPassport）没记到的「电脑前说话」；不改任何产物，删掉即恢复 |
| `dictation_windows` | `output/local-web/dictation-windows.jsonl` | 时间窗索引路径（既有约定） |

配置每次请求现读：**改配置不需要重启服务**；改代码需要重启（本仓库 8765 由 launchd
`com.life-logger.local-web` 托管）。

## 隐藏规则（唯一实现：`src/life_logger/dictation.py`）

一条事件在看板上隐藏，当且仅当命中下面任一条，且没被保留网救回：

1. **上游已标记**：`is_dictation=true` 或 `evidence.dictation_context=true`；
2. **读时按绝对时间比对**：事件的录音绝对时间（`evidence.reported_at` / `time.absolute`
   作为绝对起点，时长取 evidence 的 `end_offset_seconds - start_offset_seconds`；若自带
   `absolute_started_at/absolute_ended_at` 则走 `record_time_window`）与
   `dictation-windows.jsonl` + `dictation_windows_extra` 的窗口重合 > 50%
   （`is_dictation_record(..., include_semantic=False)`，与
   `scripts/attribute_funasr_transcript.py` 共用同一份实现，不重复写）。

**保留网**（不隐藏）：

- 显式口令产物：`pinned_memo`（「记录一下…」）、`query_dispatch`（「查询一下…」）——
  用户主动要求记下来的，上游策略里也是 dictation 的豁免口；
- 生活保护词：吃了/买了/抽烟/记录一下/查询一下…（`_PERSONAL_LIFE_EVENT_GUARDS`）。

**不做**「文本里出现技术词」的独立判断；时间戳取不到时不隐藏（宁可不剔）。

## 影响的读接口

| 接口 | 过滤内容 |
|---|---|
| `GET /api/daily` | events，以及由事件派生的 `event_count` / `category_summary` / `todos` / 各台账 / `pinned_memos`（数字与列表一致） |
| `GET /api/daily-brief` | 同一份简报的 `agent-brief-v1` 投影（小苏 / 微信只读口径） |
| `GET /api/dashboard/daily` | `timeline_scenes` 中整段落在 dictation 窗口内的场景、即时抓取卡片 |
| `GET /api/timeline-scenes` | 同上（场景轴） |
| `GET /api/reminders` | 能定位到 dictation 事件的待办（只在返回层；OpenClaw 任务池 / reminders.json / 产物不动） |

**不动**：`/api/pending-sources`（待确认区）、`/api/search`、`/api/stats`、`/api/patterns`、
任务产物（events.json / context-extraction.json / 转写 / 录音）、OpenClaw 事实源。

## 恢复原样

```json
// local-config.json
"hide_dictation_from_dashboard": false
```

或者删掉 `dictation_windows_extra` 里手工补的窗口（只影响那一段，其余开关照旧）。

## 已知边界

- 若 PTT 没记录到「电脑前说话」（例如直接和 AI 语音对话而没按 PTT），上游不会打
  dictation 标记、也没有时间窗 —— 需要在 `dictation_windows_extra` 里手工补窗口，
  或以后改进上游识别（关键词/声学）。
- 上游时间窗文件是守护进程 `scripts/watch_airpassport.py --mark-only` 维护的，新增窗口
  下一次请求即生效。
