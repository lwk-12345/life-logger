# 输入至输出检查与日常三线决策对照

核查/实现：Codex，2026-10-16。本轮在十一维接入基础上继续，保留共享WIP、Watch安装基准及历史原始产物。

## 实际链路与运行边界

Watch分段/ACK → ASR与声源归属 → Kev 0.8B决策 → 本地Qwen事件抽取 → 原文/金额/时间/状态校验 → 正式事件/待确认 → 实时会话与正式任务产物 → 简报/网页。

新增旁线：Kev实际请求及原始回答 → 本地持久样本 → 独立进程同时调用Jev与Clef → 各自保存结果与失败历史 → summary.json统计。主路不等待云端，不为旁线重跑本地Kev，不改变正式事件。

当前抽取配置deepseek端点仍是本地Qwen；Jev/Clef是云端决策模型，不是DeepSeek通用抽取API。本次没有自动切换正式决策到云端，也未加入DeepSeek疑难升级。

本轮未重启Web PID3526。现有3625条转写的长任务仍在旧attribution子进程中运行；新磁盘代码、已安装旁线worker和当前Web进程加载状态必须分开。新代码完全加载到正式服务仍待安全窗口，不能宣称日常新输入已全部使用新版。

## 输入完整性证据

命令：python3 work/input-output-audit-20261016/verify_input_files.py。

- 127个会话、8313个分段、2,303,102,125字节。
- 文件存在/大小与8313个SHA-256全部符合manifest/ACK记录，0缺失/大小错误/哈希错误。
- 7个旧会话有超出expected_segments的后来分段，均无序号缺失，日期集中在9月28/29和10月5日。这是旧收口元数据异常，不等于音频文件消失；没有自动改写这些历史manifest或重算历史结果。
- 本轮未解码全部音频、未人工听辨ASR、未证明手表采集无断点；ACK哈希验证只证明Mac现存文件与收件记录一致。
- 完整列表：同目录input-hashes.json。旧任务健康只读审计job-health.json：161个目录，未发现已完成新任务存在致命产物缺失；两条历史任务已如实标degraded，不能因审计bucket名字再称它们假成功。

## 已复现并修复

1. **模型漏答后无法补齐**：新增逐来源覆盖修复。真实请求默认启用；对遗漏ID仅重试一次单条，不重问已经回答的内容。部分缓存可继续补漏，不能永久当完整缓存。原始批次响应、单条修复响应及成功数量都保留；仍失败的来源继续pending，不伪装成功。测试注入器可显式repair_missing_sources=True覆盖同一路径。
2. **实时层伪造发生时间**：原来将event.time.absolute=null替换成evidence.reported_at，并改precision=inferred。现在保留未知发生时间，排序仍可使用报告时间。
3. **实时口令绕过身份审核**：原来unknown说话人的“记录一下”可能被兜底代码合成为source_label=self的正式事件。现在兜底只接受上游已确认self；unknown原文及上下文候选继续供复核。
4. **旁线没有与新版正式问题统一且会覆盖数据**：旧sidecar使用V15、日期文件覆盖，没有在本轮发现持续调度。新旁线复用实际生产请求，不另造一套问题；两个云端独立保存，不因Jev全失败丢掉Clef结果，也不把None/漏答当成功。
5. **宿主配置污染旧测试**：旧上下文测试显式隔离legacy配置与意图池，不依赖本机当前生产模式、不向共享意图池写测试计划。

## 三线对照的保存与调度

- 样本：output/local-web/decision-shadow/samples/<input_hash>.json。包含state/questions、schema、创建时间、本地requested/served model、原始answers和耗时。相同输入/问题去重，旧V15 JSONL保留在原处。
- 旁线：results/<input_hash>.jev.json与.clef.json。记录model、原始answers、missing_questions、耗时、usage（若返回）、状态、尝试数、下一次重试及失败历史。请求不完整记incomplete，缺凭据记unavailable，不计成功。
- 汇总：summary.json。按问题版本分组记录一致数/比较数、未知回答数、线路状态、成功样本延迟。**与Kev一致率不是准确率**；目前没有附人工真值，不据此自动替换主模型。
- 凭据只在worker读取现有secrets/指定环境变量及已有.hermes/.env；不落入样本、结果或日志。不关闭TLS证书校验。
- 本地配置decision_shadow.enabled=true；示例默认false。实际provider hook只记录匹配local_endpoint的本地服务请求，避免测试服务器混入正式队列。
- 独立LaunchAgent：~/Library/LaunchAgents/com.life-logger.decision-shadow.plist；每30秒运行scripts/run_decision_shadow.py --limit 4；跨进程文件锁防重复处理；两个线程同时处理云端请求。最多3次失败退避，已成功线路不重跑。
- 安装/启动验证：plutil -lint通过，launchctl bootstrap gui/501 exit0，launchctl list显示LastExitStatus=0。它不重启正式Web服务。停止方式：launchctl bootout gui/501/com.life-logger.decision-shadow；配置enabled=false也可停止后续入队和worker处理。

## 测试与实测

- 定向测试先复现模型漏答、未知时间被替换、unknown口令被升级；修复后通过。
- 扩展Python回归：272 passed in 55.71s，最终verification.exit=0；覆盖接收/ASR/决策/抽取/恢复/待复核/简报/产物/Web API。首次前台命令触及60秒工具上限，保留verification-time-limit.log，随后后台完整运行通过，不把超时当通过。
- 页面逻辑：58 passed/0 failed，日志node-verification.log。未冒充真实浏览器点击或真机验收。
- 后续新增旁线汇总测试：test_decision_shadow.py共6项通过；该汇总逻辑不改变主链提取。
- 新模块Ruff/格式、关键Python文件编译及范围diff-check通过。
- 隔离实测同一输入哈希625f02ece8c58a69b5de2d2e0a159a8309961d2c73ea67b77adaa8133b123631：Kev已记录，Jev completed，Clef completed。数据在work/input-output-audit-20261016/shadow-smoke。
- Jev前两次URLError，第三次同请求成功；Clef首次已成功且未重跑。另一次隔离对照也出现过Clef瞬时连接失败。不能据此归因Python环境；系统/虚拟环境对照未证明环境必现因果。失败状态及原数据完整保留。

## 尚未宣称解决的边界

- Web/当前长子进程尚未重载新代码；旁线worker已启动，但正式旧进程不能因文件修改自动拥有新版入队逻辑。
- 本地推理吞吐仍偏慢，旧任务处理3625条来源；没有在活跃任务上强制重启、杀进程或宣称延迟达标。
- 7个旧收口异常及历史漏答未直接覆盖修复，需隔离回放后再验收。
- 未建立人工真值，不能证明ASR/十一维/事件语义的总体准确率。
- 未实现云端DeepSeek接手、共享历史意图检索和持久等待上下文调度。

下一步：正式任务与接收队列空闲时安全重载Web，按同一条真实新录音核验ACK、ASR、十一维、事件、待复核、网页和两条旁线的输入哈希；保护当前录音和历史修改。
