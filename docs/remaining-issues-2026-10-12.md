# Life Logger 当前剩余问题审计

检查日期：2026-10-12T16:29:19.304664+08:00。执行者：Codex，三分支只读审阅后由协调者复核。

## 当前事实与范围

- 用户确认当前手表为V56；本轮未操作设备、构建、安装。
- Kev已在8000通过旧laya命名兼容路线接入。近期产物有超时，不能再说“未接通”。
- ASR配置local_turbo；当前源码优先MLX Whisper Large-v3-Turbo与Silero VAD，逐窗归属用NumPy声纹池与近讲特征。整场处理仍保留另一套归属路径。
- HEAD为6de9e059ecb2c69deb22e6a8bc14c87757459034加共享WIP；源码持续变化。此报告只对应下方hash快照，不能证明运行实例已加载全部源码。
- 本轮只读源码、现存服务日志、产物元数据；未播放录音、未调用模型、未运行测试、未重启或回写业务数据。只改审计说明与规划台账。

## 优先处理的问题

| 优先级 | 问题 | 证据与影响 |
|---|---|---|
| P1 | 正常文字会被静默过滤 | transcribe.py:803–819把“我吃了一口”“我看你”等生活用语列为幻觉marker，944–945按子串整条continue。代码可确定会拒绝这些合法句式，尚未统计真实误删数量。 |
| P1 | 声源未知可升级为本人已审核 | transcribe.py:773–779在声纹缺失/失败时，near直接标self且免复核；watch_formal_asr.py:576–578只排除non_self，memo/query合成物689–700/760–770硬写self与reviewed。近讲应提高记录优先级，不能单独确认身份。 |
| P1 | 句级试听时间被窗口时间替代 | transcribe.py:896–924关闭MLX timestamps，各条输出都设为整个VAD窗口的起止时间；多句可能同时间同长度。已有原段试听对齐修复仍无法恢复这一层丢失的句级定位。 |
| P1 | 事件报告时间被当发生时间 | watch_formal_asr.py:552–559把缺少time.absolute的事件用reported_at填充并标inferred；它覆盖上下文抽取器对“发生时间未知”的保留，会把回忆过去事件排到录音时刻。 |
| P1 | 跨午夜的本人转写日期漏入索引 | local_web.py:5176–5198仅集合session_date与事件日期，不加入self_transcripts日期；web/app.js:3243–3274直接采用此集合。93F4会话10/08本人205条、10/09本人245条、events=0，API日期只含10/08。不是所有跨午夜录音都触发。 |
| P1 | 部分抽取失败可能覆盖已显示事件 | watch_formal_asr.py:539重新从空列表开始，789将当轮结果覆盖events.json，未保留上轮成功事件；无key/部分batch失败也可能写入不完整列表。context_auto_retry.py:40–71只接收全失败，degraded不会被该持久重试接管；新段可触发重跑，但末尾失败无新段时没有保证。尚无旧events快照证明哪条已实际消失。 |
| P1 | 处理与正式审核仍明显迟到 | 今日下午15D7会话65段：64个SHA匹配且attempt=1的终态记录，当前received_at→terminal updated_at中位22.0分钟、最大36.6分钟；最后记录收到15:52:24，正式任务16:22:31创建。local_web.py:7318–7321空闲收口阈值1800秒；该路径确实额外等待约30分钟，不代表手表明确complete也必须等30分钟。 |
| P2 | Kev与DeepSeek共用3秒超时 | contextual_extraction.py:1162默认3秒，334/1396传给DeepSeek，1354/1383首次Kev超时后本轮剩余Kev批次被跳过。最新5份报告Kev annotated_count均0；D4A当前72批中DeepSeek有1批超时。不能将延迟归结为余额耗尽，也不能说所有事件都没提取。 |

## 其他代码质量问题

- 声纹池在最终归属判定前更新（transcribe.py:746–751），只按相似度/近讲门槛加入history（source_attribution.py:265–290），缺少本人已确认的更新门禁，存在误认后污染后续判断的路径。当前是否已污染未核实。
- transcription_confidence填入voice_similarity（transcribe.py:973）；声纹相似度不能当文字识别置信度。
- 历史快照订正：检查途中曾发现CPU fallback追加整段可能重叠；交付前共享WIP已删除这段fallback，此项撤回为当前缺陷，保留审计历史。当前没有验证最新无回退路径的异常恢复效果。
- 情绪方法恒返回NEUTRAL（781–783），daily_dashboard.py:147–157却计入calm/neutral统计。这个值不是当前情绪模型的观察结果。
- Kev服务队列没有显式断连取消/deadline处理；客户端超时后请求可能继续计算。现存日志无逐请求时刻/ID/排队时长，尚不能证明近期超时与某次200的对应或实际积队。
- 实时日记/仪表盘已有15秒轮询（web/app.js:6222–6236），但Watch接收明细刷新未在该timer内；常驻明细页可能陈旧，不能说整个网页没轮询。
- 原先确认的Passport提示卡片到显示端链路仍未完成；无线麦/现有待办BLE是另一路。模型设置与结果的旧命名也容易造成误判。

## 今日产物证据

元数据读取根：output/local-web/watch-sessions/watch-41aa6b87347f4f0fb5238b559f705c4d。

- D4A1A35CC987444AB86DD64B0E34411C：08:11–13:41录音，331段；attempt=1且SHA匹配的终态307段，当前received_at→terminal updated_at中位205.4分钟、最大404.6分钟。16:11:57 live-context报告864来源、72批（71完成/1失败）、4formal/2pending事件、12pending_sources、Kev标注0。
- 15D7FBC2008247F8A09550C76AF00599：14:47–15:52录音，65段，64段首次终态统计如上；16:06:33已有live-context，26来源、3批完成、5formal/2pending、Kev标注0；正式任务约16:22才创建。实时日记与正式审核进度不同。
- received_at会被重复上传重写（watch_stream.py:582–589），所以它不是首次到达证据；以上指标是相对当前记录receipt的差值。attempt>1已排除，无法由覆盖后的checkpoint还原第一次完成时间。不得把这些数称为精确首次传输或纯模型计算耗时。
- 93F4CA27458E449FB323F2103EC9EAA8：10/08 19:56开始，API可接受的本人转写日期计数10/08=205、10/09=245；无events。只打印日期/数量，未输出录音文字。

## 建议下一步

先修“原文保留与拒绝原因可追溯、未知声源保持待审、句级时间及跨日索引”；同时为Kev/DeepSeek分离超时并对失败批次持久恢复，避免降级结果覆盖已确认记录。之后用用户已反馈的跨午夜、健身、消费和英语录音逐项验收；修复尚未实施，本轮没有把发现标成已解决。

## 源码快照

- src/life_logger/transcribe.py: `69ebd664f12dec0424554aa83f0d3634dff76c1dd2f00197bf55ba7e1549bfbf`
- src/life_logger/source_attribution.py: `ba527936589f1e011dcd79308527b79c631509434363625ef622ea351742985d`
- src/life_logger/watch_formal_asr.py: `9ce79a986ee63b34281bc0ba31dfc70a704b0044bc5bb59e2e2aaebfa46137fa`
- src/life_logger/local_web.py: `0298efbb18fa063cd0f4ea106e6b316d0b6ddd085bdb51bc7d5bb390d9ecbb3c`
- src/life_logger/watch_stream.py: `33a9ea41621981c4fb270afccb592f6656c6f0be0a69a7b740f98841b222bd0d`
- src/life_logger/contextual_extraction.py: `51bb18986156a033e2745601254c4c3b20da3de3e1e8d3f16b49cfe617b8f274`
- web/app.js: `dbbfd8a1d1879af4c96bcc91ec88f46cc969767e2c7aa34cb223616b6f34bf61`
- .kev/kev/serve.py: `edac8cacb81b9f476b00c8e4b0327fcac2ac8b63b422ddf1fee00fd302e2ad9b`

## 交付前源码漂移订正


### 2026-10-12T16:32:20.354545+08:00 — [网页端] 交付前共享WIP漂移订正

- 第一次最终门禁exit1，原因是他人继续修改pipeline.py/local_pipeline.py并使台账mtime落后；未改门禁。重新读实际diff和ASR关键路径后更新台账并复跑。当前hash：src/life_logger/transcribe.py=69ebd664f12dec0424554aa83f0d3634dff76c1dd2f00197bf55ba7e1549bfbf；src/life_logger/watch_formal_asr.py=9ce79a986ee63b34281bc0ba31dfc70a704b0044bc5bb59e2e2aaebfa46137fa；src/life_logger/local_web.py=0298efbb18fa063cd0f4ea106e6b316d0b6ddd085bdb51bc7d5bb390d9ecbb3c；src/life_logger/pipeline.py=ffc6aa74f41044b1456df85b1ab8e824080050a4be5b85ff9c8078a7f8b36cc3；src/life_logger/local_pipeline.py=a50f1e224bde81e5d65ed9b93208cac73284eb58fa50cc926022de06bfc2c154。
- transcribe.py最新删除CPU fallback，前一快照的“CPU追加造成重复”已不适用于当前源码，保留为历史发现并撤回当前问题项。当前MLX黑名单、近讲=self、关闭句级timestamps、voice_similarity填transcription_confidence、恒NEUTRAL依然存在，分别为约806/773/907/973/783行；watch_formal_asr时间回填与未知memo/query升级仍存在。报告已同步为最新快照，已核验范围不外推到所有新WIP。

### 2026-10-12T16:34:02.575495+08:00 — [网页端] 最终只读快照

- 共享编辑持续进行，重新以实际文件更新hash和定位：src/life_logger/transcribe.py=69ebd664f12dec0424554aa83f0d3634dff76c1dd2f00197bf55ba7e1549bfbf；src/life_logger/source_attribution.py=ba527936589f1e011dcd79308527b79c631509434363625ef622ea351742985d；src/life_logger/watch_formal_asr.py=9ce79a986ee63b34281bc0ba31dfc70a704b0044bc5bb59e2e2aaebfa46137fa；src/life_logger/local_web.py=0bb9c2ca452cb01d5593514664884b6218bd517d619dea76482b1598358faa2e；src/life_logger/pipeline.py=ffc6aa74f41044b1456df85b1ab8e824080050a4be5b85ff9c8078a7f8b36cc3；src/life_logger/local_pipeline.py=a50f1e224bde81e5d65ed9b93208cac73284eb58fa50cc926022de06bfc2c154；src/life_logger/contextual_extraction.py=ca4f212013e21eac3b1b6cd56c686cb8e8e30ed12d29110106121cdd22477ef2；web/app.js=5afc4698fcc4311383970216e03c67d99a354abc418307f7852454c824ae930c。
- 当前关键问题定位：src/life_logger/transcribe.py:827 "我吃了一口"；src/life_logger/transcribe.py:921 without_timestamps=True；src/life_logger/transcribe.py:987 transcription_confidence=seg_attr；src/life_logger/transcribe.py:791 "decision_reason": "near_field_fallback"；src/life_logger/watch_formal_asr.py:569 time_obj["absolute"] = ev_evidence["reported_at"]；src/life_logger/watch_formal_asr.py:590 for idx, r in enumerate(rows):；src/life_logger/local_web.py:5136 event_dates = {sess_date}。最新近讲声纹更新增加了近讲门槛，但仍在最终身份判定之前更新；无声纹near兜底仍self且免复核。最近两轮移除CPU回退，因此该历史项已撤回。
- 16:32:20严格台账检查exit0，20/26阶段完成、三件套mtime合格；16:30:11那次exit1为共享源码更新时间导致，不是业务测试失败。当前审计发现没有软件修复交付，本轮不把未知实例加载状态写成已部署。

上文行号是审查时各快照定位；持续共享编辑使行号移动，本节列最终关键锚点。
