# Life Logger 当前架构与接力说明：Jev / Clef 双路

> 本文记录2026-10-16用户最新确认的方向，覆盖之前的“Kev主路+Jev/Clef旁线”方案。旧文档与结果保留作为历史，不再作为当前目标。

## 用户已确认的目标

- 不再使用本地Kev 0.8B做正式决策或比较基线。
- 同一份输入分别由Jev、Clef云端决策，各接一遍抽取，得到两份独立结果。
- 暂不选择单一模型，暂不合并两路事件/金额/待办；后续根据真实结果再决定保留一个还是两个。
- 继续使用本地Qwen2.5 7B；保留云端DeepSeek API能力。本次不能把“Jev/Clef决策API”误写成“DeepSeek抽取API”，当前deepseek命名端点实际指向本地Ollama。
- 原录音、ASR、声源归属共用，不复制跑两次ASR。
- 原“检查输入到输出并修问题”的任务仍继续；更新文档不会替代实施。

## 处理流程

```text
Watch密封分段 → 持久队列与ACK → Mac原音 → ASR → 声源归属
                                                   ↓ 共用同一原文/上下文/目标/历史快照
                                     ┌─────────────┴─────────────┐
                                     ↓                           ↓
                                Jev十一维决策                Clef十一维决策
                                     ↓                           ↓
                                Qwen抽取A                    Qwen抽取B
                                     ↓                           ↓
                             独立事件/候选/意图池          独立事件/候选/意图池
                                     └─────────────┬─────────────┘
                                                   ↓
                                      模型对照页，并列查看和导出
```

云端决策并行。本机仅一块GPU，Qwen两份提取串行；提取缓存也隔离，不能因两路判断相同而只提取一次。

十一维保留：media/self/novelty/people/goal/health/expense/amount/quantity/location/timeline。其中self拆说话人与事件主体；novelty拆信息价值/历史关系/完整性/表达性质，共15个子问题。

## 不得破坏的约束

1. 原文优先；低置信候选明确待核实，不授权身份、金额、时间、健康诊断、去重或计划完成。
2. unknown声音不能因为“记录一下”升级为self；发生时间未知不能用reported_at代替。
3. 同一输入的两条云端使用完全相同的问题与上下文。历史快照冻结，不能先把Jev新计划写入共享池再给Clef使用。
4. 两路缓存、意图池、事件和待确认项隔离。某一路失败保留另一份，禁止回退Kev或自动混合两份输出。
5. 三线旧数据与之前软件WIP保留；不reset/clean，不覆写人工修改。
6. 密钥只从既有本机secrets/env读取，不写进交接、样本或日志；不关闭TLS验证。
7. 一致率不是准确率。没有人工真值，不宣称语义准确率。

## 文件与接口

| 文件 | 职责 |
| --- | --- |
| src/life_logger/decision_comparison.py | Jev/Clef并行决策、串行抽取、隔离产物、只读列表 |
| src/life_logger/contextual_extraction.py | 公共入口分派双路；_run_contextual_extraction_single保留原单路执行器，供两路各自调用 |
| src/life_logger/decision_dimensions.py | 十一组/十五题契约、未知/低置信处理、紧凑抽取线索 |
| src/life_logger/decision_shadow.py | 复用已验证的Jev/Clef HTTPS传输与现有凭据读取；旧三线旁线功能保留但配置已关闭 |
| src/life_logger/watch_formal_asr.py | 实时双路结果不进入共享events，不再走口令兜底混入账目 |
| scripts/attribute_funasr_transcript.py | 正式双路不回退规则合并，不同步两份候选到OpenClaw共享待办 |
| src/life_logger/local_web.py | GET /api/decision-comparisons，沿用现有API认证 |
| web/comparison.html/.css/.js | 两路并列结果、待确认、耗时、导出；主导航增加“模型对照” |
| tests/test_decision_comparison.py | 禁止Kev调用、相同输入、并行决策、独立提取/缓存、一路失败保留另一路 |

每个提取目录有decision-comparison.json及context-extraction.json索引。独立分支：

```text
result/decision-variants/jev/{decisions.json,context-extraction.json,events.json,pending-events.json,active-intents.json}
result/decision-variants/clef/{同上}
```

每路/extraction/保存内部提取产物；.context-extraction-batches缓存留在各路目录中。重复处理前将上一版索引与结果归档到comparison-history/。录音和归属检查点不重复复制。

根索引formal_events为空是“等待选择、禁止混账”的设计，不是零事件。查看variants中的两份结果或模型对照页，不能因根数组为空重写一套提取器。

## 当前状态（必须继续更新）

- **已实现**：上述双路编排、模式分派、隔离缓存/意图池/结果、同输入哈希、只读接口及页面草案。
- **已配置**：local-config.json的decision_comparison.enabled=true、providers=[jev,clef]；decision_shadow.enabled=false；decision_provider及网页偏好为jev（兼容旧字段，实际为双路模式）。
- **已测**：新双路+漏答+维度+实时完整性定向测试14项通过；两路输入相同，真正各提取一遍，无Kev调用。此前输入输出扩展回归272项、页面逻辑58项通过，但它们是双路最新改动之前的结果，不能冒充当前全量回归。
- **真实双路实测进行中**：work/jev-clef-dual-20261016/real_smoke.py，输出real-smoke.log和real-smoke/。必须读取真实结果，不能假设已完成。
- **未部署**：Web原进程此前PID3526仍加载旧代码；旧长任务job-20261016-133913-101e20d5的attribution子进程此前PID60288处理3625条来源。PID会变，行动前重新核实。
- **服务迁移待做**：旧Kev LaunchAgent与旧decision-shadow LaunchAgent还需停止/禁用自动重启；配置关闭不等于服务已停止。不得删除权重/旧结果。Web重载和旧任务迁移必须保留输入及检查点。
- **还需验证**：双路全量回归、真实调用、实际API/页面、正式任务迁移、比较页空态/失败态。长录音结果可能需后台继续处理，不能将功能上线与全量历史处理完成混为一谈。

## 接手顺序

1. 读仓根AGENTS.md和.planning/life-logger/task_plan.md顶部基准，再读本文件和findings/progress最新段。
2. 查git status/diff及本轮日志；不要以HEAD代表有大量共享WIP的完整版本。
3. 检查real-smoke.log/decision-comparison.json及活动进程，完成现有新代码验证，不重写已通过模块。
4. 通过后停止旧Kev/三线worker，安全加载新Web，按同一条录音验收API两份产物与页面。
5. 更新三件套、记录源码hash与部署证据，运行bash .planning/tools/planning_check.sh --strict。

## 上游检查与保留的问题

- work/input-output-audit-20261016/input-hashes.json：127会话/8313分段/2.3GB，存在、大小、SHA256全部通过；不代表设备采集连续性或ASR语义准确。
- 7个旧会话expected_segments小于后来收到的分段数，文件未丢，未自动改历史。
- 已修：批次漏答单条补提取、部分缓存补漏、实时未知时间被改成录音时间、unknown口令升级身份。
- 未实现：人工真值评测、共享历史意图检索、DeepSeek自动接管、持久等待上下文调度。后续按优先级扩展，不能误报已完成。
