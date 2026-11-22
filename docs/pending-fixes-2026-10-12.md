# Life Logger 待修清单（2026-10-12 夜，小麦 + 4 子系统体检汇总）

> 本文档是**待办台账**，不是已完成报告。每条都注明了现象、根因、修法、当前状态。
> 所有结论均有工具证据（job 产物 / 服务日志 / 实测），无推测。

---

## A. 已完成（本次会话）

| # | 事项 | 结果 |
|---|---|---|
| A1 | Kev 本地推理服务救活 | 补 `HF_HUB_OFFLINE=1`（缺它 → 推理前 HF 网络探测走 Clash fake-ip → CLOSE_WAIT 死锁 → `/health` 正常但 `/v1/systemone` 永久挂起）。**首次验证真实推理成功** |
| A2 | `scripts/start_kev_service.sh` 修缺口 | 补 `HF_HUB_OFFLINE / HF_DATASETS_OFFLINE / TRANSFORMERS_OFFLINE` |
| A3 | Kev 客户端超时 3s → 120s | `src/life_logger/contextual_extraction.py` `_annotate_kev` 调用处。原 3s 必然超时，且超时后 `kev_unreachable=True` 会**跳过本轮剩余全部 Kev 批次** → `annotated_count` 恒为 0。测试 21 项全绿，8765 已重启 |
| A4 | 8765 配置切 kev + 重启加载新代码 | `preferences.json` / `local-config.json` 改 `decision_provider=kev`（备份 `*.pre-kev-*`） |
| A5 | 决策增强暂时关闭 | `use_jev=false`，防止 Kev 未验证前每个 job 白等 |
| A6 | Kev-4B / 0.8B 质量实测 | 4B：8 分类 5/5；速度 26~51s/条。0.8B：0.3~2.5s/条（快约 100 倍），同口径 4/5 |

---

## B. 待修（按优先级）

### P0-1 Kev 切换 0.8B 并打开决策增强
- **现象**：决策增强现关闭；4B 太慢（19 条 ≈ 8~16 分钟），0.8B 快 100 倍（≈6 秒）
- **修法**：8000 端口服务换 `--run jaredpalmer/kev-0.8b`，打开 `use_jev=true`，跑一个真实 job 端到端验收
- **注意**：0.8B 错的题置信度都 < 0.4，可用阈值把不确定的交给链路里已有的 DeepSeek 复核
- **状态**：待用户决定

### P0-2 手表与 Mac 断联
- **现象**：`output/local-web/watch-sessions/device-status/watch-*.json` 最后通信 15:52:25，之后 >3 小时无上传无心跳
- **修法**：先确认手表是否在录、是否充电中；Mac 侧看不到"录了没传"
- **状态**：待用户现场确认

### P1-1 ASR 声明与实际不符
- **现象**：`status.json` 的 `processing_options.asr_model` 写 `gemini-asr`，实际 20/20 个 job 全部不符（实跑本地 turbo / 豆包 / funasr）
- **根因**：`SUPPORTED_ASR_MODELS` 与真实执行路径脱节；`asr/run-summary.json` 的 `model` 字段**写死 Fun-ASR-Nano**
- **修法**：让产物记录**实际生效**的 provider；UI/审计只读该字段
- **状态**：待修

### P1-2 ASR 退化重复（whisper-turbo 2.55%）
- **现象**：真实产物有 `'叮'×67`、`'花桃'×69`、`'就这一句'×127` 等整段报废输出；现有 `is_hallucinated_asr_noise` 拦不住
- **质量排名**：豆包 0% > gemini 0.08% > funasr 0.45% > **turbo 2.55%（最差）**
- **修法**：见 `## C. 调研中`（本地 SOTA ASR 模型调研）
- **状态**：调研中（3 子系统）

### P1-3 「假成功」状态机 —— 已修（2026-10-13）
- **现象**：job `status: completed` + `error: null`，但 `logs` 里有 Traceback 或产物缺失。已确认 3 个真·假成功
- **根因**：① `scripts/local_jobs.py:395` 文件标 completed 就整文件跳过，下游阶段失败不重跑；② `src/life_logger/local_web.py` `apply_file_outcomes` 只在**文件级** failed 时降级任务，**阶段级失败被忽略**
- **修法（已落地）**：
  1. 任务级真相不再只靠「最后一个事件说了什么」：新增 `status.json.stage_errors`（结构化，含 `severity` 致命/可降级 + `resolved`），并在 `job_finished` 时对着**阶段落盘日志 + 产物**核对（`apply_file_outcomes(state, job_dir=...)`）
  2. 终态三档：致命失败/产物缺失 → `partial`；只有可降级失败 → `degraded`；其余 `completed`
  3. 失败在源头留痕：阶段 `error`/`failures` 跨重跑保留（`stage_started` 不再抹掉历史），重跑成功带 `recovered_from`，`job_finished` 有文件失败时不再冒充 `completed`
  4. 审计工具：`scripts/scan_job_health.py`（dry-run 默认，`--apply` 回写并备份）；每日验收加了「任务健康」一节
- **盘上实况（142 个 job）**：0 个「产物缺失」型假成功；**2 个**阶段自称完成但日志无成功记录 → 应为 `degraded`（`job-20261009-191737-57990d6c`、`job-20261009-231933-50b43361`）；4 个曾崩溃后重跑成功（补 `stage_errors` 留痕）；其余 111 个干净
- **状态**：已修（代码 + 工具 + 测试）；历史状态用 `python3 scripts/scan_job_health.py --apply` 回写

### P1-4 声纹归因是最慢阶段
- **现象**：`attribution` 平均 748s（12.5 分钟）、最长 4970s（82.8 分钟），占阶段耗时 **99.4%**
- **根因**：CAM++（3D-Speaker 的 `speech_campplus_sv_zh-cn_16k-common`）批量声纹计算
- **修法**：待评估（批量优化 / 并发 / 换轻量声纹）
- **状态**：待评估

### P1-5 「全部的流程检查一遍」类指令被丢
- **现象**：2026-10-12 07:49:50「全部的流程检查一遍」→ `semantic_status=no_event`，全链路无产出；而 07:29:43「检查一下我们目前项目开发的一个进度」正常成 todo
- **根因**：事件抽取对"元指令/抽象指令"判定为 no_event
- **修法**：把祈使句指令类语句（检查/汇报/记录/查询）加入强制捕获模式
- **状态**：待修

### P1-6 意图池停止更新
- **现象**：`output/active-intents.json` mtime 停在 10-12 01:43，42 条中最新一条创建于 **10-08**；10-09 之后各 job 里的 10+ 条 pending 意图一条都没进池
- **根因**：持久池与 job 级 `result/active-intents.json` 双轨，互不同步；UI 也没有只读接口展示
- **修法**：合并为单一来源 + 提供只读接口
- **状态**：待修

### P2-1 事件口径混乱（同一天 6 个数）
- **现象**：2026-10-12 事件数分别是 21（磁盘 job 产物）/ 15（磁盘 `display_timeline`）/ 26（前端事件轴）/ 34（`/api/daily-brief`）/ 36（`/api/daily`）
- **根因**：**前端不读**后端的 `display_timeline`，自己重算一套；两套筛选规则还不一致（服务端 `_TIMELINE_CONCRETE_CATEGORIES` 不含 `todo`，前端含 `todo`）
- **修法**：统一筛选规则到一处，前端消费后端同一份数据
- **状态**：待修

### P2-2 `laya_annotation` 旧字段残留
- **现象**：产物里 `laya_annotation` **392 处 / 69 文件**，`kev_annotation` **0 次**；改名只做了一半
- **剩余写入点**：`src/life_logger/watch_formal_asr.py:694 / 764`（硬编码 `{"status":"unavailable"}`）；`scripts/attribute_funasr_transcript.py:954/1401/1403`（读 + 用户可见警告文案）；`src/life_logger/cli.py:125`（报错文案）
- **修法**：清掉旧名写入点与文案
- **状态**：待修

### P2-3 2 单时间戳 ×1.99 坏数据
- **现象**：`job-20261008-132841`（19.6h 录音）与 `job-20261009-191737` 的 transcript 偏移 = 录音时长 ×1.99
- **根因**：豆包 sync 路径把绝对偏移加了两次（`sync_doubao_to_formal_asr.py` 写入已含绝对偏移 → materialize 再叠加）
- **修法**：修 sync 路径；已产出的坏单需重跑
- **状态**：待修

### P2-4 上传延迟双峰
- **现象**：近 7 天 5024 段，33% 秒级实时，**52% 晚于 2 小时**；典型是几小时甚至 2 天积压后一次性灌入
- **根因**：Watch 持久化 outbox（网络发送不算 ACK，SHA256 校验通过才删本地副本）→ 断网积压、恢复补传
- **修法**：属设计行为，但**服务重启会打断上传窗口**（今天重启 6 次）→ 减少重启
- **状态**：观察

### P3 其他
- `quick_captures.json` 存在**两套 ID 体系**（job 路径 `cap_xxx` vs Watch 实时 12 位 hex）
- `/api/dashboard/daily` 的 9 条待办 = 6 条磁盘 + 3 条现场派生（重复计数）
- 上传中有 50 次 PUT 失败（`Watch segment upload is incomplete/size invalid/interrupted`），均已重试成功

---

## C. 调研中

- **本地 SOTA ASR 模型调研**（3 个子系统并行，2026-10-12 夜发起）
  1. 2026 年开源中文 SOTA ASR（低退化率优先）
  2. Apple Silicon MLX/CoreML 方案对比 + whisper 复读问题缓解手段
  3. 本机已有 ASR 资产盘点

---

## D. 已验证的事实速查（避免重复排查）

- **配置优先级**：`output/local-web/preferences.json` > `local-config.json` > 代码默认值（显式值压过一切默认）
- **8765 无热加载**：改代码必须重启；进程启动时间早于文件 mtime 就是在跑旧代码
- **8765 重启**：`launchctl kickstart -k gui/501/com.life-logger.local-web`
- **8765 API token**：`output/local-web/secrets.json` 的 **`mobile_access_token`** 字段
- **`validate_jev_environment()` 只检查 key 存在性，不验证有效性** → 会产生 `fallback:false` 的假正常记录；真实 401 只掉在 job 的 `jev.log` 里
- **Kev 权重位置**：adapter 在 `~/.cache/huggingface/hub/models--jaredpalmer--kev-*`，底座在 `~/.cache/modelscope/models/Qwen--Qwen3.5-*`
- **判断 ASR 实际 provider**：看 `files/<hash>/asr/transcript.json` 每行的 `source` 字段
- **判断 job 真实健康**：读 `status.json` 的 `logs` 数组，别只看 `status`
