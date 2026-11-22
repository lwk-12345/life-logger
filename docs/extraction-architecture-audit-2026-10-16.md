# Life Logger 三模型与提取架构核查

核查：Codex，2026-10-16。范围为现有源码、白名单配置、Ollama只读API及已有提取产物；未调用任何模型推理，未改业务代码、配置或历史数据。源码有共享未提交WIP；HEAD不能独立代表当前实现。

## 当前路径

Watch → MOSS/ASR → 声源归属 → contextual_extraction.run_contextual_extraction → 单个OpenAI兼容端点 → 原文/金额/状态校验 → 正式事件或待确认。

- 本机Web PID 3526的cwd为本仓（lsof验证）。
- 配置 deepseek.base_url 实际为 http://127.0.0.1:11434/v1，model=qwen2.5:7b；近期产物endpoint/model一致。字段叫DeepSeek不代表发往云端。
- local_llm.enabled=true，batch_size=6，timeout_seconds=360，max_tokens=4000。
- kev.event_gate=false；Kev服务存在不等于该门控参与。不可用decision_source空值推断未接通。
- Ollama /api/ps返回qwen2.5:7b及bge-m3，均context_length=4096。只证明采样时已加载及运行窗口，不证明模型最大能力。
- 在所查主提取路径中未发现Qwen疑难/漏答后升级云端DeepSeek的逻辑。其它问答/复盘模块可能调用远程服务，本报告不把它们等同于提取协作。

## 已复核症状

最新任务 job-20261016-150847-0113c97f：47输入，28 self/8 non_self/11 unknown；8批次completed；2正式事件、2待确认事件、4 pending_sources。逐批解析raw_response并比较source_ids，独立确认漏答4条。

实时会话 B8D493FA71A34348ACB877CD2458B145：32输入，6批次completed，13条missing_source_response。另一实时会话49C7195F9E5E4958BED43DDF197CC718：162输入，27批次completed，漏答14条。live/final可能重叠，不能累加成总体漏答率；不能以正式事件少推断大量漏记，没有人工真值。

较旧产物另有TimeoutError、invalid_schema；10-15台账记载本地批次预算已修过，当前新产物的漏答与此前全批超时应区分。

只读复核命令：python3 /tmp/life_logger_extraction_audit.py。2026-10-16运行exit 1，断言Saved model responses omit input source_ids despite completed batches。这是已有产物一致性检查，未重新推理，不证明当前版本重跑一定复现。

## 确定的架构限制

1. 单端点配置承载主提取，本地Qwen替换了原远程抽取器；尚未形成决策/本地/云端协作路由。
2. contextual_extraction.py在漏答时设置pending_sources.retryable=true，但context_auto_retry.py只接受status=failed且每批均为瞬时网络失败；degraded与missing_source_response不符合该自动恢复条件。人工恢复及未来新片段触发重跑是另外的机制。
3. _deepseek_request固定提示词在添加输入前已4715字符；要求多个领域、复杂数据结构、营养估计、计划关系及证据规则。结合当前4096 token运行窗口存在预算风险；本轮未精确tokenize或复现截断，不能认定这是漏答根因。
4. _chunks按记录数及原文本字符分批，不按完整提示词token预算；批次无通用重叠窗口/会话摘要。跨批配料问题已有营养后处理补偿，不代表所有跨批语义都已覆盖。
5. _pending_intent_context只在显式提供intent_pool时启用；所查Watch实时和正式attribute入口调用run_contextual_extraction均未传共享intent_pool。因此已有计划关系能力在这些主入口未获得共享历史计划。代码会建立output-local池，但enabled参数仍为false。
6. Kev当前门控提示词把未完成计划、AI指令等归other，而主抽取提示词要求保留待办/主动记录/查询。直接打开门控可能提前排除这些内容；本轮未运行门控，不能声称发生过该误杀。
7. run_contextual_extraction逐批顺序处理；Watch不同会话可分别起线程，尚不是统一管理本地LLM预算的调度器。只作为潜在吞吐问题，本轮未测排队耗时。

## 按收益排序的改造建议（尚未实施）

1. 完整性：每个输入ID必须有显式处置结果；漏答/无效结构拆成小批重试，重复失败提交云端复核，保留原始响应、来源和修改边界。
2. 分离local_extract和cloud_extract两个配置及调用器，明确actual_provider/model；不得再用同一deepseek字段覆盖二者。统一实时与正式调用入口，防止两条链各修各的。
3. Qwen使用短提示词做宽召回与基础结构化；营养数值交给成分库与程序；复杂否定/纠正/跨段引用/冲突等提交DeepSeek。云端需要相关原文，不能只拿Qwen结论。
4. Kev做有限选项的路由或关系判断，保留unknown，不把other当不可恢复的丢弃决定；事实/计划/想法分开，避免挡住待办。
5. 按完整请求token预算打包，结合语义会话及小范围重叠；显式传入相关共享意图，检索选择而非机械塞前20条。
6. 同一批有人工真值的连续录音比较现状与协作版：逐来源覆盖、重要事件召回、错误事实/金额/完成状态、延迟、用户纠正次数及云端成本。

## 版本锚点

- HEAD：7751e61e804daa361a47db56d9a725e79e6cc590 + 共享WIP。
- contextual_extraction.py SHA-256：cdc8f27464dbf46cf5f88156364214ca27468b83e406743493f1221713eed673。
- attribute_funasr_transcript.py SHA-256：bfc928cff0f0607fe8d68dcd17e82ab45171a7b13922d32b557e0f04a20bf479。
- watch_formal_asr.py SHA-256：aeaae1f162a98cf6befc32489cf2fb3aa6c822022a72807b2fd48aef4ebae151。
- 最新任务context-extraction.json SHA-256：aa0b2951421a885e3c2ba438804acb3723b0a98d9b6fa1dfaa7623da7432338c。

本轮完成架构核查及保存响应的漏答复核；漏答确切模型原因、截断、真实云端可用性及新协作设计的效果仍待实验验证。
