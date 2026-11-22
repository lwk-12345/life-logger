# Life Logger 全仓只读体检报告（2026-10-18 任务 / 2026-10-19 执行）

- 执行时间：2026-10-19 17:20–17:55 CST
- 执行范围：源码、运行中的本地服务（`127.0.0.1:8765`）、产出产物、测试
- 性质：**只读**。未修改任何业务代码/配置/产物，未重启服务，未调用写接口，未跑模型推理处理真实录音
- 服务基准：`server_build_id=5f74f9fb1c4e`，`server_started_at=2026-10-18T01:50:58+08:00`（进程 PID 46055，已连续运行 39h41m）

---

## 1. 结论摘要

1. **P0 隐私**：两条已脱敏替换词（`飞机杯`/`泡架`）在磁盘上仍有 **193 个文件**含原词；用户可见入口 `/api/events/search`、`/api/quick-captures`、`/api/reminders` 与产物 `daily.md`、`transcript.json/md` 都能直接搜到原文。
2. **P1 账目**：前端存在一套独立的兜底记账函数（`web/app.js:5207` `extractDayExpenseLedger`），与后端 `expense_ledger` **规则不同**；实测 2026-10-12 差 ¥14.00、2026-10-14 差 ¥33.60（含提现计入消费、双计、币种混算、正则从文字里抠金额）。
3. **P1 部署**：运行中的服务是 **10-18 01:50 的旧进程**，磁盘上至少 6 个后端文件更新至 10-19 02:06（`local_web.py`、`watch_formal_asr.py`、`contextual_extraction.py`、`pending_review.py`、`expense_policy.py`、`natural_correction.py`），前端静态文件却是实时读磁盘 → 前后端契约可能错配。
4. **P1 数据**：10-18 的 AI 日报缓存 `synthesis-2026-10-17.json` 记 `total_amount_cny=16.6`，而账本已是 **¥26.80**；日报与账本会互相打脸。
5. **3号疑点（¥149.x vs ¥202.41）**：**无法复现**。全仓 `--no-ignore` 搜索这两个字面量 0 命中；当前可复现的权威数字是 10-18 `provisional=¥319.41` / `settled=¥156.63`，两者差额 ¥162.78 由 7 笔「只在实时链」的消费构成（明细见 2.2）。

---

## 2. 缺陷清单（按严重度）

### P0-1 脱敏不完整：原词仍可通过 5 类展示入口读到

**现象**：`work/redaction-20261018/report.md` 记录只替换了 1 个 job 的 `result/*.json`（11 个文件），并明确声明 "Raw model responses, ASR/transcript files ... left untouched"。

**证据 A：磁盘残留（`rg --no-ignore`，排除备份目录）**

```
rg -l --no-ignore --hidden '飞机杯|泡架' --glob '!work/redaction-20261018/before/**' .
→ 193 个文件
```

其中包含（示例，全部为实际路径）：

| 文件 | 性质 |
|---|---|
| `output/local-web/jobs/job-20261018-095927-.../asr/transcript.json`、`asr/checkpoint.json` | 原始 ASR |
| `.../result/2026-10-18/transcript.json`、`transcript.md` | **可读转写正文** |
| `.../result/2026-10-18/daily.md` | **日报正文（5 处命中）** |
| `.../result/self-transcript.json`、`result/source-attribution.json` | 归属/本人转写 |
| `.../result/decision-variants/{jev,clef}/**`、`.context-extraction-batches/**` | 决策与批次缓存 |
| `output/local-web/quick_captures.json`、`reminders.json` | 全局存储 |
| `output/local-web/jobs/job-20261012-010044-.../result/2026-10-11/transcript.json` | **另一个更早的 job（10-11）** |
| `web/daily-timeline-prototype.html` | 原型页面 |

> 注：上一版审计若用 `rg` 直接搜 `output/` 会得到 0 命中，因为 `output/*` 在 `.gitignore` 里，`rg` 默认跳过。必须加 `--no-ignore` 才能看到真实残留。

**证据 B：API 实测（带 Bearer token）**

```
GET /api/events/search?q=飞机杯 → 200，5 条
   [utterance] job-20261018-095927-9735adbd
     artifact=files/9a256b.../result/2026-10-18/transcript.json
     text=然后花了五十四买了一个飞机杯。
   [event] job-20261012-010044-9586eff4
     text=便签：购买清单（买一双跑鞋；再买个什么呢?买个飞机杯）
GET /api/events/search?q=泡架 → 200，2 条
GET /api/search?q=飞机杯 → 200，5 条
GET /api/quick-captures → 命中 3 处
GET /api/reminders → 命中 1 处
GET /api/daily?date=2026-10-18 → 0 命中（这一层被正确遮蔽）
GET /api/daily-brief?date=2026-10-18 → 0 命中
```

**影响面**：用户明确要求脱敏的内容，仍能通过搜索、待办提醒、快速抓取、转写阅读器（`/api/jobs/{id}/artifact?path=...` 直接读 `transcript.json`）看到原文。

**建议**（只记录不修）：脱敏要覆盖 transcript/checkpoint/daily.md/自建索引/全局 store；或建立"脱敏词表"在**读层**统一过滤（一次覆盖所有入口），而不是逐文件改产物。

---

### P1-1 前后端两套记账规则（"两个数打架"的结构性来源）

**现象**：`web/app.js:5207` 有 `extractDayExpenseLedger(day)`：当 `day.expense_ledger.has_records` 为假时，**前端自己重算**消费总额。它与后端 `expense_ledger` 规则不同：

- 只要 title 含「消费/支出/购买」就算消费（后端看 category/契约）
- `amount` 缺失时用正则从 title/quote 抠 `(\d+(?:\.\d+)?)\s*元`（会**凭空造出金额**）
- 不做去重、不做币种分桶、不排除提现/转账

**证据（把该 JS 函数逻辑等价复刻后跑真实 `/api/daily` 数据）**

| 日期 | 后端 ledger | 前端兜底 | 差额 |
|---|---|---|---|
| 2026-10-12 | ¥6.00 | ¥20.00 | **+14.00** |
| 2026-10-14 | ¥26.60 | ¥60.20 | **+33.60** |
| 其余 8 天 | — | — | 0 |

2026-10-14 逐条（`/api/daily?date=2026-10-14`）：

```
¥None  '便签：购入一包桂圆，24元'      → fallback 正则抠出 24.0
¥7     '花费 7 美元购买 OpenDesign 订阅' → 被当 CNY 计入
¥2.6   '购入维他命柠檬茶一瓶，2.6元'
¥26.6  '本次采买合计26.6元（桂圆24元+柠檬茶2.6元）' → 与上面两笔重复
```

2026-10-12 逐条（含提现）：

```
¥10.0  '提个10块钱就行了 提现'   → 提现被当消费
¥3 / ¥3 '购买油条花费3元' / '购买油条3元' → 同一笔两条
```

**影响面**：同一个日期，走 `/api/daily`（后端账本）与走 job 时间轴（前端兜底）会得到不同金额，最大实测偏差 +126%（10-14）。

---

### P1-2 服务是旧 build（stale）

**证据**：进程启动 `2026-10-18 01:50:57`，而磁盘上这些后端文件更新更晚：

```
10-18 02:12  src/life_logger/local_web.py
10-18 02:34  src/life_logger/watch_formal_asr.py          ← 会话级聚合接线在此文件
10-19 01:51  src/life_logger/contextual_extraction.py     ← 1M 上下文预算/batch 40 在此文件
10-19 02:04  src/life_logger/pending_review.py
10-19 02:06  src/life_logger/expense_policy.py
```

`web/*` 是**静态文件实时读磁盘**（实测 `/app.js?v=...`、`/style.css?v=...`、`/timeline-progress.js`、`/timeline-data.js`、`/journal_logic.js` 全部 200），所以"前端新、后端旧"。

**影响面**：前端若依赖新后端字段/新 API 语义，会出现"页面有入口、后端不认"的错配；本报告第 4 节的测试红也部分源于此。

---

### P1-3 日报缓存与账本不一致

**证据**：

```
output/local-web/daily-synthesis/synthesis-2026-10-17.json → expense_analysis.total_amount_cny = 16.6
GET /api/daily?date=2026-10-17 → expense_ledger.total_amount = 26.8
```

`/api/daily` 直接把磁盘上的 `synthesis` 原样返回（`local_web.py` 中读取 `daily-synthesis/synthesis-{date}.json`），没有校验它是否与当前账本一致。

**影响面**：账户修复（10-17 从 16.6 修正到 26.8）之后，日报文本仍是旧数，用户会看到"总结说 16.6、明细说 26.8"。

---

### P1-4 今天（10-19）没有任何录音数据，且设备有 15 个会话卡在"等完成信号"

**证据**：

```
/api/daily?date=2026-10-19        → events=0, expense=0
/api/capture-funnel?date=2026-10-19 → said_sentences=0, files_scanned=0
/api/watch-reception（17:30 上报）:
  pending_segment_count=0, pending_segment_bytes=0, sync_state=idle,
  pending_completion_count=15
  asr_counts: pending=31, processing=1, completed=3642, no_speech=4681, failed=0
watch-sessions 中 >= 10-18 的会话只有 2 个（都在 10-18，最后一段结束 10-18 19:33）
```

**影响面**：10-18 19:33 之后无新音频入库（约 22 小时）；同时 15 个会话缺完成回执、31 个 ASR 待处理，说明"采集→收尾"链路有积压。

---

### P1-5 历史 `deepseek_batch_failed` 积压 6529 条（未增长，但也未处理）

**证据**：

```
GET /api/pending-sources?include_batch_failed=1
  totals: all=6711, needs_review=143, batch_failure=6529,
          resolved=5, undated=693, open_actionable=139
  reasons: deepseek_batch_failed=6529, low_confidence=106,
           model_no_event_but_directive=37, missing_source_response=32,
           invalid_category=4, invalid_event_state=2, quote_not_grounded=1
```

与 10-17 审计记录的 6529 **完全一致** → 说明是历史残留、当前没继续产生（10-18 那次 job 的批次全部 completed）。

---

### P2 级（不影响正确性，但会拖累体验/维护）

| # | 问题 | 证据 |
|---|---|---|
| P2-1 | 单个接口返回体过大，无分页/压缩 | `/api/jobs` **3,634,662 B / 59 ms**；`/api/watch-coverage` 2,464,012 B；`/api/backup` 857,016 B；`/api/decision-comparisons` 576,356 B |
| P2-2 | `/api/mobile-access` 只支持 POST，GET 返回 404 | `GET /api/mobile-access → 404`；源码 `local_web.py:7907` 在 POST 分支内 |
| P2-3 | `web/timeline-progress.js` 未被 git 跟踪 | `git status --porcelain → ?? web/timeline-progress.js`（部署/备份会漏） |
| P2-4 | 首页存在 `src="about:blank"` 的 iframe | CDP 实测 `iframes: ["/daily-timeline-live.html?...", "about:blank"]` |
| P2-5 | `pending_completion_count=15` 无收敛入口 | 见 P1-4 |

---

## 3. 逐项核查结果

### 3.1 数据一致性（五处口径）

| 口径 | 10-18 | 10-19 | 备注 |
|---|---|---|---|
| `/api/daily`（provisional） | 22 事件 / **¥319.41** | 0 / ¥0.00 | `expense-policy-v2`，`totals_partial=true`，`pending_count=1`(amount_unknown) |
| `/api/stats`（provisional） | 22 事件 / **¥319.41** | 0 / ¥0.00 | 与 daily 完全一致 ✓ |
| `/api/daily-brief` | 11,105 B，事件数一致 | — | 未见漂移 ✓ |
| `/api/daily?scope=settled` | 7 事件 / **¥156.63** | 0 | 只认正式产物 |
| `/api/stats?scope=settled` | 7 事件 / **¥156.63** | 0 | 与 daily-settled 一致 ✓ |
| 瀑布图 `/api/timeline-scenes` | 7 scenes + 7 action_groups | 0 | — |

- **provisional 与 settled 的差额是可解释的**：¥162.78 = 7 笔"只在实时链"的消费
  （79 鞋 / 30 咖啡豆 / 22 POS / 8.98 毛豆 / 7 / 13 / 2.80 昆布汁）。
- **0 元交易**：未发现（`totals_by_currency` 只有正数）。
- **提现混入**：后端未见（`withdrawal_total_by_currency={}`）；**前端兜底会混入**（见 P1-1 的 10-12 ¥10 提现）。
- **跨日错归**：`/api/daily?date=2026-10-18` 事件最早 04:12、最晚 20:49，无 10-17/10-19 事件串入 ✓。
- **同额重复**：10-18 有两笔 ¥15.90，但分别是"止汗露"与"冷冻菠菜"（quote 不同），不是重复 ✓。

**3 号疑点（¥149.x / ¥202.41）结论**：无法复现。

```
rg --no-ignore '202\.41|149\.[0-9]' . → 0 命中（源码/产物/工作目录/日志/验收文件）
逐日现状：10-12 ¥6.00 | 10-14 ¥26.60 | 10-17 ¥26.80 | 10-18 ¥319.41(prov)/¥156.63(settled)
```

能产生"同一天两个数"的**已知**机制有三个，均已在上面举证：
1. `scope=provisional` vs `settled`（差 ¥162.78）；
2. 前端 `extractDayExpenseLedger` 兜底（10-14 会多出 ¥33.60）；
3. 日报 `synthesis` 缓存（10-17 是 16.6 vs 26.8）。

不排除 3 号当时看到的是上述某两个数字在某一时刻的快照，但**没有留下可追溯痕迹**，因此不认定为"账本算错"。

### 3.2 接口层（30 个端点实测）

全部 200，除 `/api/mobile-access`（GET 404，见 P2-2）。耗时分布：

```
/api/health 36ms · /api/doctor 469ms · /api/data-health 863ms · /api/settings 282ms
/api/jobs 59ms(3.6MB) · /api/tasks 47ms · /api/reminders 54ms · /api/habits 16ms
/api/stats 222ms · /api/daily 179ms · /api/daily-brief 177ms
/api/dashboard/daily 1984ms（最慢） · /api/timeline-scenes 359ms
/api/pending-sources 586ms · /api/search 263ms · /api/events/search 358ms
/api/watch-coverage 88ms(2.4MB) · /api/decision-comparisons 235ms(576KB)
/api/passport/cards 153ms · /api/openclaw/health 30ms · /api/access-log 15ms
```

无 5xx。旧 build 问题见 P1-2。

### 3.3 前端层

- **控制台**：Chrome headless + CDP 实测首页与瀑布图页，`Runtime.exceptionThrown` / `Log.entryAdded(error|warning)` / `console.error|warn` 均 **0 条**。仅有两条浏览器弃用提示（password field 不在 form 内；`apple-mobile-web-app-capable` 已弃用）。
- **异常字样扫描**（DOM 全文）：`undefined` 0、`NaN` 0、`[object DocumentFragment]` 0、`[object Object]` 0、`◉` 0、`null` 0。
- **静态资源**：`/app.js`、`/style.css`、`/journal_logic.js`、`/timeline-data.js`、`/timeline-progress.js` 全部 200，字节数与磁盘一致。
- **`web/timeline-progress.js`（任务点名）**：语法 `node --check` 通过；被 `web/daily-timeline-live.html:1517` 引入；页面在 `:1961` 调 `LifeTimelineProgress.timelineProgress(...)`、`:2622` 调 `compactTimelineEpisodes(...)`；脚本导出到 `root.LifeTimelineProgress`；页面内无同名函数冲突。**结论：接线正确，未造成瀑布图异常**。
- **移动端 390px**：`Emulation.setDeviceMetricsOverride(390x844)` 后 `scrollWidth == clientWidth == 390`，**页面级无横向溢出**；但有元素右边界超出视口（`BUTTON.day-chip`、其内 `SMALL`），它们位于横向滚动的日期条内（不是布局破损）。

### 3.4 管道层

见 P1-4 / P1-5。补充：

- `amount_not_grounded`：全仓 523 处（历史），**当前 job（10-18）已不再产生**；抽查 5 条历史样本后，用现在的 `_deepseek_amount_is_grounded()` 复跑全部返回 `True`：
  ```
  grounded=True  amount=4.9   quote='饮料钱是一瓶这个外星人电解质水四块九'
  grounded=True  amount=1.8   quote='还有一瓶一块八的可可口可乐有糖版的'
  grounded=True  amount=9.57  quote='...总共是九块五吧，九块五毛七。'
  grounded=True  amount=54    quote='然后花了五十四买了一个飞机杯。'
  ```
  → 属**旧版本误杀**，现版本已修；这批记录仍停留在 pending 里，未被回溯。

### 3.5 安全 / 隐私

- **鉴权**（`/api/daily` 实测）：无 token 401、错 token 401、非 ASCII token 401、空 Bearer 401、正确 token 200 —— 行为正确 ✓
- **脱敏**：见 P0-1（不完整）。
- **secrets 是否进前端/日志**：`web/` 目录下未检索到 `mobile_access_token` / `sk-` / `magpie` 明文；服务把 token 保存在 `output/local-web/secrets.json`（不在 web 静态目录内）✓。日志中未见完整 token 打印（抽查 `output/local-web/launchd.stdout.log`）。

### 3.6 测试健康度

```
PYTHONPATH 方式：python3 -m pytest -q -o addopts=''  →  2 failed, 1343 passed in 79.57s
node --test tests/*.mjs                              →  存在失败（断言文本契约）
```

失败逐条判断（均为**测试契约过期**，不是功能回归）：

| 测试 | 失败点 | 判断 |
|---|---|---|
| `test_natural_correction_frontend_wiring.py::test_update_without_existing_events_json_is_relayed` | 期望 `{"status":"error","error":"...events.json..."}`，实际返回 `{"status":"needs_review", "review_reason":"transaction_evidence_missing", "expense_contract":{...}}`（HTTP 仍 400） | 契约升级为结构化待复核载荷，测试未跟进 |
| `test_web_job_artifacts.py::test_app_js_loads_journal_entries_based_on_artifacts_not_status` | 期望字面量 `if (!summaryPaths.length && (job.files || []).length)`；实际是 `if (!summaryPaths.length && (job.files \|\| []).length && jobHistoryLocalDate(job) === focusDate)`（`web/app.js:3760`） | 逻辑仍在，多了一个日期条件；断言写死旧字面量 |
| Node：`PENDING_REVIEW_SCOPE_HINT` 文案断言 | 期望旧文案「标记只影响列表，不会自动重跑模型。」，实际 `web/app.js:6118` 已改为「…不会入账、不会自动重跑模型。」 | 文案更新，测试未跟进 |

---

## 4. 已确认无问题的部分（下次不必重复查）

1. `/api/daily` 与 `/api/stats` 在 provisional、settled 两种 scope 下**完全一致**（事件数、金额、accounting_scope）。
2. `provisional − settled` 的每一笔差额都能对应到具体的"仅实时链"事件，无隐形漏单。
3. 鉴权 4 种失败形态均 401，正确 token 200。
4. 首页与瀑布图 **无 JS 报错**；无 `undefined/NaN/[object Object]/◉` 异常文本。
5. `web/timeline-progress.js` 语法正确、被正确引入并调用、无同名冲突。
6. 390px 下**无页面级横向溢出**。
7. `deepseek_batch_failed` 数量与 10-17 审计一致（6529），当前批次全部 completed，**没有继续恶化**。
8. `/api/daily` 与 `/api/daily-brief` 已不含脱敏词（读层这一层是干净的）。

---

## 5. 未能验证的项（明确说明原因）

| 项 | 为什么没验证 |
|---|---|
| ¥149.x / ¥202.41 的原始出处 | 全仓 `--no-ignore` 搜索 0 命中；无当时的截图/日志留存；当前不能复现。已给出当前权威数字与三种可能机制，但**不下结论** |
| 记忆问答（`/api/memory/ask`）是否泄露脱敏词 | 该端点是 POST 且会触发模型推理；受"只读、不跑模型推理"约束未执行。间接证据：`/api/search`、`/api/quick-captures`、`/api/reminders` 已确认泄露，检索层大概率同源 |
| 390px 下的像素级遮挡/重叠 | 只做了 `scrollWidth/clientWidth` 与元素右边界测量（结论：无页面溢出），未做逐元素视觉确认（无截图基线） |
| 前端与旧后端 build 的实际错配点 | 需要重启服务才能对比"新前端 + 新后端"行为；本任务禁止重启 |
| 15 个 `pending_completion_count` 的具体会话 | `/api/watch-reception` 只给聚合计数，逐会话需读 manifest 全集；未展开逐一核对 |

---

## 6. 证据索引（可复现命令）

```bash
# 脱敏残留（必须 --no-ignore，output/ 在 .gitignore 里）
rg -l --no-ignore --hidden '飞机杯|泡架' --glob '!work/redaction-20261018/before/**' .
# 搜索接口实测
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8765/api/events/search?q=飞机杯'
# 记账双口径
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8765/api/daily?date=2026-10-14'
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8765/api/daily?date=2026-10-14&scope=settled'
# 旧 build
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8765/api/health' | python3 -c 'import json,sys;print(json.load(sys.stdin)["server_started_at"])'
find src/life_logger web -newermt '2026-10-18 01:50' \( -name '*.py' -o -name '*.js' \)
# 日报与账本不一致
python3 -c "import json;print(json.load(open('output/local-web/daily-synthesis/synthesis-2026-10-17.json'))['expense_analysis']['total_amount_cny'])"
# 待确认积压
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8765/api/pending-sources?include_batch_failed=1'
# 管道积压
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8765/api/watch-reception'
```
---

## 7. 补查（前端细节，第二轮）

### P2-6 任务看板永久「正在读取」假加载态（真实缺陷）

**证据**：`web/app.js:4822 renderKanban()` 在 `journalDays` 为空时：

```js
if (!journalDays.length) {
  root.setAttribute("aria-busy", "false");
  root.innerHTML = '<div class="kanban-empty ...">还没有可用的日期数据…</div>';
  return;                      // ← 没有更新 #kanban-status
}
```

`#kanban-status` 的初始文本是 `正在读取任务看板……`（`web/index.html`），只有 `setStatus(...)` 才会替换；上述分支直接 `return`，因此**没有任何日期数据时该行永远停在「正在读取…」**，与已经渲染出来的「还没有可用的日期数据」自相矛盾。

**影响**：用户会以为看板还在加载、在等一个永远不会来的请求。

### 已排除的「假加载」误报

- 「正在读取今日随口记录……」：由 `loadDashboard(dateStr)`（`web/app.js:8291`，成功后 `:8407` 替换文本）负责，而 `loadDashboard` 在进入仪表盘视图时才调用（`:739`）。首页初始 DOM 里含这段占位属于**隐藏分区的懒加载**，不是永久加载态。
- 静态引用可达性：`index.html` + `daily-timeline-live.html` 共 9 个本地引用，逐个 curl **全部 200**（含 `timeline-data.js`、`timeline-progress.js`）。
