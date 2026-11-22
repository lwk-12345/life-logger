# Life Logger 本地模型升级调查（MacBook Pro M1 Pro / 16GB）

**调查日期：2026-10-14（Asia/Shanghai）**  
**范围：** 对照 Life Logger 当前工作树、只读运行态快照、官方项目仓库与模型卡；不改业务代码、不做本机 benchmark。目标是判定本机可选升级方向及证据边界，不代表候选已经集成或验收。

## 结论摘要

这台 M1 Pro 16GB 足以搭建“常驻轻量流处理 + 片段级较强离线复核 + 低频本地文本模型抽取”的分层流水线。最值得优先验证的升级不是让一个大模型全程跑音频，而是：

1. **先解决“何时能看到文字”：** 当前 Watch 正式 ASR 是 60 秒分段后处理。若目标是录音过程中就看到 partial，优先做 sherpa-onnx OnlineRecognizer（中英 Zipformer / Paraformer）原型，同时必须把小块 PCM 提前送到 Mac；只换模型不会缩短 Watch 的封段等待。
2. **保留 MOSS 为最终转写基线：** 项目盘上配置选择 MOSS，官方卡显示 MOSS-Transcribe-Diarize 0.9B 支持长音频转写、匿名说话人分段、时间戳和声学事件。当前代码是逐个已封口段调用，不是把 90 分钟一次送入；也没有 M1 Pro 的本机峰值内存/延迟保证。
3. **补一个低成本在线 VAD：** Silero VAD ONNX 支持有状态分块，可用于端点检测和静音裁剪；先保留前滚/尾随缓冲，不能让 VAD 删除原音频，也不能把 VAD 当声音场景分类器。
4. **先不换说话人模型：** CAM++ 继续做本人声纹匹配，MOSS 的 `[S01]` 只代表单段内匿名说话人。2026-10-12 已有同权重 CAM++ 的 sherpa-onnx 本机快速运行时测量，适合先做运行时/阈值复核，不需要直接换大模型。
5. **声音属性分层加：** PANNs/AudioSet 做可选声景候选；emotion2vec+ base 可作为低频“声学情绪线索”，但不能当作真实情绪、心理状态或生活事件。先为声源/事件标签保存分数和片段时间，再决定是否进入事件抽取。
6. **让事件抽取可完全离线：** 这次快照里 Ollama 已有 Qwen2.5 3B Q4_K_M，但 Life Logger 没有 `local_llm` 配置，盘上抽取配置仍启用远程 DeepSeek。先把现有本地模型接入影子对照；若确需升级，再比较 Qwen3-4B 等 MLX 量化候选，不要仅凭参数更新就替换。

**建议优先级：** P0 运行态、延迟、峰值内存可观测；P1 sherpa 在线草稿 + Silero VAD；P2 保留 MOSS 最终转写并和 Qwen3-ASR / MLX Whisper 小样本盲测；P3 Ollama 本地文本抽取影子对照；P4 声景/情绪标签按需运行。先修正错误的分离模型配置；只有真实重叠人声导致 ASR 丢失时，再做分离模型实验。

## 当前 Life Logger 链路核对（2026-10-14）

这是对当前工作树和本机只读快照的描述，不能外推为已部署成功。当前基准为 `HEAD=6de9e059ecb2c69deb22e6a8bc14c87757459034 + 共享未提交 WIP`；以下核心文件在本轮核验的 SHA-256：`watch_formal_asr.py=5652f274da913c18f4260902a0aec66a3266d3a7ec2512bc8f613a07edc95ac5`、`moss_transcribe.py=e2816696967c3ae5891baa8cfef423021353c8af122c2cf1a51d2ae102a3bd31`、`run_cloud_asr_pipeline.py=928ff07a8f61cea9f641781af2c36aa09afd5c28740761ce74f81de89f12aca6`、`local-config.json=4b11925ccc793462b55672e09ddfafe12e80d28f8469cd2339b1ed27428a6304`。

| 流程层 | 当前可核对状态 | 对升级方案的影响 |
|---|---|---|
| Watch 音频到 Mac | 正式路径逐个扫描已接收、校验过的 Watch 音频段；Watch 段长为 60 秒。转录在段封口后运行。 | 当前是“每分钟左右增量处理”，不是说话过程中逐字流式。要 partial，必须让录音 PCM 在 60 秒封口前到达 Mac，或显著缩短并重叠滚动片段。 |
| Watch 正式 ASR | 磁盘配置的 `asr_provider=moss`；MOSS 由独立 Python 环境通过 `mlx_audio.stt.generate` 子进程调用。解析结果含时间戳和匿名 `[S01]` 等说话人标记。 | MOSS 已是本地最终 ASR 选择，先保留为质量基线。模型卡称 0.9B、支持 50+ 语言、最长 90 分钟一次处理并能输出声学事件；项目实际每次送单段，不自动继承模型卡所说的 90 分钟路径。 |
| Web 通用 ASR | `local_web.py` 的默认是 `mlx-whisper-turbo`；兼容别名 `fun-asr-nano` 被改写到 `mlx-whisper-turbo`。 | 这条通用 Web/录音导入路径与 Watch 的 MOSS 正式 ASR 不是同一条链。页面标签或旧别名不能作为实际模型身份依据。 |
| 声纹身份 | CAM++ 用于本人/他人匹配；MOSS 的 `[S01]` 是片段内部匿名编号，不能直接映射到“本人”。 | 保留 CAM++ 的身份层。已有 10 月 12 日报告实测：同一 CAM++ ONNX 权重 + sherpa-onnx CPU 4 线程，在 189.4 秒真实片段集上耗时 1.79–3.83 秒（RTF 0.0094–0.0202）；这是旧的本机测量，非本轮复测，也未证明生产已切到该运行时。 |
| VAD 与声景 | 正式 MOSS 路径没有使用轻量项目 VAD 做预筛；现有 `audio_vad.py` 是 RMS 阈值启发式，主要接在 `watch_live.py` 预览路径。`audio_classifier.py` 是 NumPy 手工声学特征/规则分类；`audio_scene.py` 的 PANNs 是显式调用、独立环境的可选功能，未发现生产调用。 | Silero VAD 可作比较候选；PANNs 与当前规则分类是能力补充，不是替换同一个模型。不要默认常驻加载多个声音模型。 |
| 文本事件抽取 | `local_llm_extract.py` 有本机 Ollama 适配，但 `local-config.json` 没有 `local_llm` 段；磁盘配置启用远程 DeepSeek。只读 Ollama 快照列出 `qwen2.5:3b Q4_K_M`（权重约 1.93 GB）等模型。 | 当前“全本地”链路没有闭环。优先把已有本地 Qwen 做离线影子对照，检查漏事件、编造、原文引用、JSON 和耗时，再决定换成 Qwen3 4B 或继续用 3B。 |
| 本机运行态 | MOSS 模型目录与独立 venv 存在，但快照中没有 MOSS 推理进程；Kev API 当时加载 `jaredpalmer/kev-0.8b`、MPS/MLX，但磁盘配置 `enabled_by_default=false`、`event_gate=false`。8765 Web 进程来自另一 checkout，`/health` 返回 404。 | “文件在磁盘”“服务监听”“模型已加载”“当前代码被该进程加载”是四个不同事实。本轮不能认定 MOSS 正在服务、Kev 参与正式抽取，或当前工作树已部署。 |
| 内存闸门 | 本地 ASR slot 默认并发 2，内存门槛是约 4.6 GB，代码注释/估算来自旧 FunASR 工作负载。 | 16GB 是 CPU/GPU 共用的统一内存。每个模型需按真实进程 RSS、memory pressure 和并发重新校准；不能把磁盘权重大小当峰值内存，也不应同时加载 MOSS、Qwen、说话人和声景大模型。 |

**当前结论：** 先补齐“实际进程加载了哪份源码/模型、每段 ASR 延迟、峰值内存”证据，再做模型比较。今天能提出明确改进方向，但不能声称新模型已经部署、性能更快或效果更好。

### MOSS 当前需要守住的运行边界

当前工作树中的 `moss_transcribe.py` 已有针对长音频截断和重复生成的限界改动：整文件路径默认每 10 分钟切块；送给 MLX CLI 的生成上限从 32768 降到 4096；注释记录了 13 分钟拼接音频曾丢失一句、60 秒段能识别，以及超大 token 上限导致某段重复生成并占用 GPU 数分钟。这里的“实测”来自共享 WIP 源码注释，本轮没有重新播放相同音频，故应作为待复核的事故线索/修复意图，而不是本轮验证结果。Watch 正式路径仍以 60 秒封口段调用 MOSS，避免把长文件能力和实时延迟混为一谈。

## 候选矩阵

| 层 | 候选 | 流式能力 / Apple Silicon 证据 | 16GB 适配判断 | 适用边界 / 优先级 |
|---|---|---|---|---|
| 增量 ASR（建议首选） | sherpa-onnx OnlineRecognizer：中英 Zipformer transducer / CTC；online Paraformer | 官方 `OnlineRecognizer` 逐块接收采样并可取 partial；模型清单和示例含中英流式 Zipformer/Paraformer，也有 macOS arm64 发布物。 | **最适合作为当前第一原型。** 可走 CPU/ONNX，避免和 MLX 争同一个 MPS 队列；但模型有大小差异，必须按小模型、2–4 线程开始测。无 M1 Pro 端到端延迟/RAM 官方基准。 | partial 会修订；要分别落盘 draft/final，并做好标点、端点和热词。要先解决 Watch PCM 提前到 Mac。模型权重许可证独立核对。 |
| 增量 ASR（实验候选） | `mlx-community/nemotron-3.5-asr-streaming-0.6b` | 社区 MLX 转换的 0.6B BF16、模型卡约 1.28GB，覆盖 zh-CN 等 40 locale；Python `mlx-audio` 卡片要求从 Git main 安装。2026-09 的 Python `mlx-audio` issue #944 显示 generic realtime handler 当时缺 `create_streaming_session()`；Swift 发行记录中有同名增量麦克风 PR，但那不能证明 Python 路径已可用。 | **可放入第二轮实验，不建议先上生产。** 16GB 权重规模看似可行，但真实峰值与 M1 Pro 延迟未测，依赖也较新。 | 权重使用 NVIDIA Open Model License。先固定 `mlx-audio` commit 并验证逐块麦克风输入、断流/重置、partial 修订和中文表现；不把“cache-aware streaming”直接等同项目可接入的 live API。 |
| ASR 方言/离线对照 | Qwen3-ASR-0.6B / 1.7B | 官方 Qwen3-ASR 支持普通话、多种中文方言、英语和多语言；官方文档说 streaming 当前只在 vLLM backend 提供，且 streaming 不支持 batch 或时间戳。 | **适合封口段 A/B，不作为 M1 实时第一选择。** 无本机 M1 Pro 性能实测。 | 若生活口语/方言明显优于 MOSS，再评估本机量化运行时；流式 vLLM 条件、时间戳缺失是项目集成限制。 |
| 当前最终 ASR 基线 | MOSS-Transcribe-Diarize 0.9B（项目已选配置） | 官方卡标出 50+ 语言、时间戳、匿名说话人、声学事件输出及最长 90 分钟单次推理；Life Logger 代码实际逐个处理已封口段。 | **保留比较基线。** 本机已有约 1.7GB 模型目录，但本轮没有实际加载/测峰值；统一内存还要给 MLX runtime、Web 服务和其它任务留空间。 | 当前集成依赖 `mlx_audio.stt.generate`，不是 live streaming；speaker 标签不是“本人”身份。模型卡能力不证明当前数据提示已要求/解析声学事件。 |
| ASR 最终复核候选 | mlx-whisper / MLX 转换 Whisper Turbo、small、medium 等 | MLX 官方示例面向 Apple silicon，提供文件/波形转写接口、时间戳等；此处查到的接口不是实时流 API。 | **可与 MOSS 做相同音频 A/B；先试当前已有 Turbo 路径，避免再常驻第二个大音频模型。** 解码中间态及系统进程都会占用统一内存。 | 对 Watch 正式路径属于可选复核候选，不应取代现有 MOSS 基线，除非真实语料证明有收益。 |
| ASR 备选运行时 | whisper.cpp + Metal（可选 Core ML encoder） | 项目官方仓库有 Metal/Core ML 支持及 M1 Pro 基准。历史 v1.6.0 基准在 M1 Pro 报告 tiny/base/small/medium encoder 分别约 39/71/217/597 ms，开启 flash attention 后约 31/60/184/518 ms；这些是 encoder benchmark，不等于整段实时转写。 | **能跑，medium 处理相对重。** 本机 exact 16GB、具体模型、当前 OS 的 end-to-end 内存/RTF 未由官方基准证明。 | 如果当前项目已有 whisper.cpp 集成，优先复用并按新版本测；否则先与 MLX 与 sherpa 做同音频 A/B。Core ML/ANE 性能会受 OS/模型转换影响，留 Metal 回退。 |
| VAD | Silero VAD v6 ONNX/JIT | 官方版本表截至 v6.2（2025-12），主模型约 260K 参数并标记 Streaming=Yes；FAQ 说明按有状态 chunk 顺序调用。sherpa 也提供 Silero VAD 集成和 macOS 示例。 | **非常适合常驻。** 模型规模很小；实际 CPU 占用仍应在本机计时。 | VAD 只判断语音活动，不判定某个声音是什么；要保存一定 hangover/pre-roll，避免切掉辅音和语气词。优先级 A。Silero 仓库为 MIT；模型包/转换版本仍应留存来源。 |
| 说话人分段 | sherpa-onnx speaker diarization；pyannote audio community-1 | sherpa-onnx 声明支持本地 diarization；有分段 + embedding 示例。pyannote FAQ 明确其管线开箱不支持 streaming，diart 是近似流式方案。pyannote community-1 可本地推理，官方给出的速度数字是 H100，不可外推到 M1 Pro。 | **异步短片段可尝试；全程常驻不应默认。** sherpa 轻量分段+embedding 更便于 CPU 管线；pyannote 社区管线可能更重，必须单独测峰值内存。 | 分段/聚类是说话人标签，不是身份识别。切得太碎会损害 embedding；先按较长语音段后处理。pyannote 模型 gated 且需接受使用条件。优先级 C。 |
| 声音事件/场景属性 | sherpa-onnx AudioTagging（Zipformer small / CED mini） | 官方示例明确提供两种标签模型及运行脚本；属离线 tagging API，没有查到官方声明可直接逐帧 streaming。 | **更适合采样片段后台运行。** 标签模型名字有 small/mini，但官方未提供 M1 Pro 内存/延迟数字。 | 标签体系覆盖由模型训练标签决定，AudioSet 标签不是“语义理解”；对 Life Logger 只作为候选 evidence feature。优先级 C。 |
| 声景标签 | PANNs AudioSet（527 类）或 sherpa-onnx AudioTagging/CED mini | PANNs 原论文仓库报告 AudioSet 527 类，用于 audio tagging / sound event detection；Life Logger 已有未接入生产的 PANNs `audio_scene.py` 路径。 | **按间隔抽样异步跑。** 没有本机 M1 Pro RTF/RAM，建议先取 1–3 秒小窗评估。 | 输出“狗叫/音乐/交通声”一类的候选线索，不可直接推出谁在场或生活事件。优先级 P4。 |
| 声学情绪线索 | ModelScope `iic/emotion2vec_plus_base` | 官方模型卡：约 90M 参数、Apache-2.0、9 个输出类（含 neutral / other / unknown），模型包约 1.12GB。 | **作为可选的封口段低频标签候选。** 体积不等于内存承诺；模型训练伪标签、中文生活录音的适用性和 M1 Pro 速度都未验证。 | 只存声学线索、类别、分数与片段时间；不能写成用户真实情绪、心理状态或健康结论。优先级 P4。 |
| 去噪/增强 | sherpa-onnx GTCRN（离线 speech enhancement） | sherpa 官方示例含 GTCRN ONNX 离线去噪。 | **按需运行。** 需测试模型线程、片段长度、实时因子；“本地可跑”不等于低延迟。 | 尽量保留原音，只对 ASR 分支使用增强音频；不要覆盖原始音频或把增强结果当原始证据。优先级 D。 |
| 声源分离 | sherpa-onnx source separation（Spleeter/UVR 等所列实现）或 Demucs | sherpa 声明支持 separation；Demucs 官方仓库称音乐源分离，原维护者说明不再积极维护。未找到可直接据此确认适合实时对话分离的官方 M1 Pro 基准。 | **不建议常开。** 对 16GB 机器，离线分离会与 ASR/LLM 争统一内存/算力；需要实际量测。 | Demucs 面向音乐 stems，不等于通用双人对话分离。只有重叠说话造成可复现损失时再做专门候选比较。优先级 D。 |
| 事件抽取 LLM | 当前已有 Ollama Qwen2.5 3B Q4_K_M；可自行量化的候选 Qwen3-4B | 本机快照列出 Qwen2.5 3B Q4_K_M（磁盘约 1.93GB），但尚未连入 Life Logger 配置。Qwen3-4B 官方卡描述其指令/多语能力；MLX-LM 提供 Apple Silicon 量化运行路径。 | **先接入已有模型做影子对照，再判断是否升级。** Qwen3-4B 4-bit 可作为实验候选，但需固定所选权重的来源/许可；上下文、KV cache、音频模型和服务共用 16GB 统一内存。 | 不让 LLM负责听音频或补全缺失事件；输入经 ASR 的原文与来源，输出 schema JSON + evidence spans + unknown。按“漏报/幻觉/原文证据/稳定性”评估，不凭通用 benchmark 替代项目评估。优先级 P3。 |
| CAM++ 声纹运行时 | 现有 CAM++ 权重 + sherpa-onnx ONNX CPU 运行时 | 2026-10-12 本机比较使用同一 CAM++ 权重、4 线程，20 段真实样本 1.79–3.83 秒（RTF 0.0094–0.0202）。 | **无需升大模型；优先评估是否把同权重运行时接入生产。** 本轮未复测。 | 换运行时后必须用本机模板与真实语料重校阈值；与样本相似度不是用户身份准确率。 |

## 重要实测边界（不要误读）

- **Life Logger 的 MOSS“已配置”不等于“当前服务正在使用”。** 本轮只读快照看到模型目录和 venv 存在，但没有 MOSS 推理进程；Web 监听来自另一 checkout，不能据此证明本工作树已部署。
- **目前最缺的是当前链路的端到端测量。** 没有本轮 MOSS segment latency / peak RSS、当前 ASR 并发、连续运行 memory pressure，也没有真实手表样本上的 MOSS 与候选模型同集比较。
- **已有 CAM++ 数据来自 2026-10-12 的独立测量，不是本轮复测。** 它支持先试“同权重换运行时”，不支持声称已部署该运行时或阈值无需重校准。证据报告：[声纹归因提速调研](2026-10-12-voiceprint-model-replacement-research.md)。
- **Nemotron Python 支持变化很快。** 0.6B MLX 卡片说模型支持 cache-aware streaming，但 Python `mlx-audio` 的 generic realtime handler issue 曾记录缺少 live session adapter；不要把 Swift 版增量会话发布记录当成 Python API 已发布的证明。
- **Qwen3-ASR 的“streaming”不等于 Mac 流式方案。** 官方仓库说它当前 streaming 只在 vLLM backend，且不能返回 timestamps；可把它作为封口段质量对照，不能把官方能力说明等同于 M1 Pro 已能直接接入。
- **16GB 是统一内存。** MLX 使用 Apple Silicon 统一内存；权重、激活、KV cache、音频队列、MPS/CPU任务及日常应用会竞争同一预算。权重文件大小只说明磁盘占用的一部分。
- **精确 M1 Pro 硬件数据存在，但很旧且窄。** whisper.cpp 官方 v1.6.0 发布帖列出 M1 Pro Metal encoder/decoder 数据；可证明该机型能运行这些模型和 Metal 路径，不能证明当前软件栈、整段录音 RTF、并发功耗或 16GB 峰值内存。
- **目前没有找到官方 sherpa-onnx 在线 ASR 的 M1 Pro 延迟与 RAM 基准。** 推荐它是依据 API 的增量能力和 macOS arm64 支持；必须按 Life Logger 实际音频采样率、模型、线程数测端到端 partial 延迟、CPU、RSS/峰值、长时间热状态。
- **MLX-whisper 文档所称 Whisper 模型大小/4-bit 转换不是设备内存承诺。** 解码时激活、音频缓存、MLX allocator 和当前程序其他模块都算在内。
- **pyannote 发布的 community-1 速度数据来自 NVIDIA H100。** 不可拿来承诺 M1 Pro 的处理速度。
- **Apple Speech API 可作为低成本对照项。** Apple 官方 Speech API 支持 partial results，request 可要求音频留在设备上；但具体 recognizer 是否支持本机语言/离线识别，要检查 `supportsOnDeviceRecognition` 等运行时能力，不等价于自带统一的离线模型或项目可控模型版本。

## 推荐的分阶段数据流

下图是建议目标形态，不是本机当前已运行的拓扑。当前确认的路径是 60 秒封口段 -> MOSS 正式 ASR -> 后续声纹/事件处理；实时分支尚未接入。

```text
Watch 原始录音永久保留 -> 每 60 秒封口/上传（当前）
   ├── 当前正式路径：MOSS逐段最终转写 + 时间戳/匿名说话人
   │      └── CAM++本人/他人归属 -> 带 source_id 的转写证据
   ├── 目标实时路径：短 PCM 块提前到 Mac -> Silero VAD -> sherpa Online ASR partial
   │      └── partial 独立保存；MOSS 对封口段给最终转写，不覆盖原音/草稿历史
   ├── 异步声音属性：响度/静音/基频等统计 + 可选 PANNs/AudioTagging
   │      └── emotion2vec 仅输出带分数的声学线索（默认可关闭）
   └── 有稳定文本后：本机 Qwen 影子抽取 -> 事件 JSON + 原文 evidence spans
          ├── 与当前远程抽取器离线同样本对照，确认后再切主链
          └── 仅在失败样本发现重叠人声时单独评估增强/分离
```

对于 Life Logger 的证据可追溯要求，建议每个派生产物保存模型名+revision/hash、运行时版本、音频片段起止、转写版本、时间戳、来源和分数。在线 ASR partial 必须作为“未稳定稿”保存或仅作 UI 草稿，句段封口后写正式转写，避免 partial 修订覆盖原始录音或成为无来源的事实。

## 建议的本机验证设计

这份研究不运行测试或 benchmark。下一步如果选定原型，固定 20–30 个代表性片段比较当前 MOSS/CAM++ 与候选：安静独白、普通中文夹英文、常见方言、远场/噪声、重叠讲话、长静音、Watch 段首段尾、本人/他人切换。ASR 与本地事件抽取使用同一批片段和同一人工核对文本。对每条链同时记录：

1. 中文 CER、英文 WER/人工纠错数；partial 首字延迟、稳定结果延迟、封口段最终结果延迟分别统计。
2. 生命周期分解：`segment_closed -> ASR_started -> first_partial -> final_transcript -> attribution -> event_extract`，区分模型时间与队列等待/重试时间。
3. CPU/GPU 活跃度、每进程 RSS、系统 memory pressure、swap 和模型加载/卸载；分别测单模型和两项并发，不在 16GB 上直接并行加载所有模型。
4. VAD 漏字/误切；说话人归属人工错误；AudioSet/情绪标签 precision 与覆盖率；事件抽取漏报、编造、原文 evidence span 命中率、JSON 合法率。
5. 模型/运行时 revision、权重 hash、线程数、量化、音频采样率与增益、失败回退；连续热机运行和原始音频可恢复性。

建议顺序：① 先记录当前 MOSS 从封段到最终句子的时间和 RSS；② 用同一批段比较在线 sherpa 草稿与 MOSS final；③ 单独比较已有 Qwen2.5 本地抽取与当前远端输出；④ 最后才加 VAD、场景或情绪标签。这样能知道每次升级带来的增益和新负担。

## 许可证与部署注意

- `sherpa-onnx` 仓库源码标 Apache-2.0，但**官方仓库提醒模型许可需要逐模型核查**；2026-09 的官方讨论中，维护者对 ONNX 转换权重的许可澄清建议询问原作者。不要把框架许可当成每个权重的许可。
- Silero VAD 仓库标 MIT。仍应固定所用权重版本与文件 hash。
- `pyannote-audio` 源码为 MIT，但 `community-1` 模型需接受 Hugging Face 模型卡条件；代码 license 不自动替代模型条款。
- OpenAI Whisper 预训练模型使用 MIT license（以 Whisper 官方仓库为准）；MLX 转换仓库有独立软件许可，社区转换 checkpoint 也应查其 model card。
- Demucs 仓库是 MIT，但项目 README 明确其不再积极维护。权重和数据集派生条款仍需核查。
- MLX / mlx-lm 的代码许可、具体 LLM 权重许可、量化后模型分发许可是三件事。

## 一手来源

1. [Apple Speech framework: SFSpeechRecognitionRequest](https://developer.apple.com/documentation/speech/sfspeechrecognitionrequest) — partial result 与设备内处理请求属性。
2. [MLX examples: Whisper README](https://github.com/ml-explore/mlx-examples/blob/main/whisper/README.md) — mlx-whisper transcribe API、timestamps、4-bit conversion。
3. [MLX Whisper transcribe implementation](https://github.com/ml-explore/mlx-examples/blob/main/whisper/mlx_whisper/transcribe.py) — 模型接口/参数。
4. [MLX framework](https://github.com/ml-explore/mlx) — Apple Silicon ML framework 官方仓库。
5. [whisper.cpp v1.6.0 official release discussion](https://github.com/ggml-org/whisper.cpp/discussions/2155) — M1 Pro Metal benchmark（旧版 encoder/decoder 数据）。
6. [whisper.cpp v1.7.5 official release discussion](https://github.com/ggml-org/whisper.cpp/discussions/2995) — Core ML/Metal 支持与更新说明。
7. [sherpa-onnx official repository](https://github.com/k2-fsa/sherpa-onnx) — 能力清单、macOS/arm64 平台与本地运行说明。
8. [sherpa-onnx online recognizer API](https://github.com/k2-fsa/sherpa/blob/master/docs/source/onnx/javascript-api/examples/api_streaming_asr.rst) — 音频到达时的 partial result API。
9. [sherpa-onnx pretrained models index](https://github.com/k2-fsa/sherpa/blob/master/docs/source/onnx/pretrained_models/index.rst) — online transducer/paraformer、VAD、audio tagging 等模型入口。
10. [sherpa-onnx Node examples](https://github.com/k2-fsa/sherpa-onnx/blob/master/nodejs-addon-examples/README.md) — 在线中英 Zipformer、AudioTagging、speaker diarization、GTCRN 示例。
11. [sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases) — 截至调查日可见 v1.13.8（2026-09）以及 macOS arm64 发布产物。
12. [Silero VAD streaming FAQ](https://github.com/snakers4/silero-vad/wiki/FAQ) 与 [version/model table](https://github.com/snakers4/silero-vad/wiki/Version-history-and-Available-Models) — 状态式 chunk/streaming 说明与 v6 系列状态。
13. [pyannote streaming FAQ](https://github.com/pyannote/pyannote-audio/blob/main/questions/streaming.question.md) — 官方明确不原生支持 streaming diarization，提及 diart。
14. [pyannote-audio README](https://github.com/pyannote/pyannote-audio) — community-1 本地执行、模型门控和 H100 benchmark 环境。
15. [pyannote FAQ](https://github.com/pyannote/pyannote-audio/blob/main/FAQ.md) — 离线使用 gated model 的说明、streaming 问题链接。
16. [sherpa-onnx licensing clarification discussion](https://github.com/k2-fsa/sherpa-onnx/discussions/3939) — 维护者称 ONNX 权重许可应向原模型作者确认。
17. [Demucs official repository](https://github.com/adefossez/demucs) — 音乐源分离用途与维护状态说明。
18. [MLX-LM official repository](https://github.com/ml-explore/mlx-lm) and [package metadata](https://github.com/ml-explore/mlx-lm/blob/main/pyproject.toml) — Apple Silicon 定位及 MIT 软件许可；另见 [quantized KV cache docs](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md) for memory controls.
19. [OpenAI Whisper package metadata](https://github.com/openai/whisper/blob/main/pyproject.toml) — Whisper package declares MIT; check the corresponding model weights/license notice when packaging a specific model.
20. [MOSS-Transcribe-Diarize model card](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize) — 0.9B、50+语言、时间戳、匿名speaker标签、声学事件和模型卡评测集；不是本机性能数据。
21. [sherpa-onnx Online ASR API](https://github.com/k2-fsa/sherpa-onnx/blob/master/sherpa-onnx/c-api/docs/online-asr.dox) — 在线识别器/流式API；另见 [双语流式Zipformer模型文档](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/)。
22. [Nemotron 3.5 ASR MLX model card](https://huggingface.co/mlx-community/nemotron-3.5-asr-streaming-0.6b) — 0.6B、40 locales、1.28GB、当前从 `mlx-audio` main 安装的说明和 NVIDIA Open Model License。
23. [`mlx-audio` Python realtime adapter issue #944](https://github.com/Blaizzy/mlx-audio/issues/944) — 记录 cache-aware generation 与 generic live-input session API 的差别；应复核issue当前关闭/合并状态后再决定实现。
24. [Qwen3-ASR official repository](https://github.com/QwenLM/Qwen3-ASR) — 中英/方言覆盖；streaming 当前 vLLM-only 且无 timestamps 的限制。
25. [emotion2vec+ base ModelScope card](https://www.modelscope.cn/models/iic/emotion2vec_plus_base) — ~90M、9类输出、Apache-2.0 和模型大小信息。
26. [PANNs AudioSet tagging repository](https://github.com/qiuqiangkong/audioset_tagging_cnn) — 527类 AudioSet 训练标签与声音事件用途。
27. [Qwen3-4B official model card](https://huggingface.co/Qwen/Qwen3-4B) — 4B参数、多语/指令能力和Apache-2.0；不是 Life Logger 抽取准确率基准。
28. [MLX unified memory guide](https://github.com/ml-explore/mlx/blob/main/docs/src/usage/unified_memory.rst) — Apple Silicon CPU/GPU 内存使用说明；权重体积不等于进程峰值。

## 假设与未确定项

- 按目标设备为 **MacBook Pro M1 Pro、16GB 统一内存**。本轮未重新检查 macOS 版本、GPU 核数、磁盘余量或系统 memory pressure；只使用 2026-10-14 只读快照中的服务/模型状态。
- 在线 Zipformer 中英模型适合做流式基线是工程判断；官方 API/模型示例确认有增量能力，但中文自然口语、Life Logger Watch 音频效果和该机型性能尚未验证。
- 本地 Qwen2.5 3B 能否达到 Life Logger 的“少漏记、不编造、逐条保留来源”标准未知；更大的 Qwen3-4B 不自动更适合，也未在当前环境测试。
- “声音属性”拆成客观声学统计、声音类别标签、说话人线索三类；情绪/心理状态不应只靠声学标签推定。
- 文中没有承诺某方案一定实时、准确或低内存。关键候选仍需对当前真实音频做本机 A/B，并记录模型 revision、RSS、partial latency 和人工纠错结果。
