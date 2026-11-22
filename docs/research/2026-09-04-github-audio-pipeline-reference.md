# GitHub 音频接入与后处理参考

检索基准：2026-09-04。本文只使用各项目官方 GitHub 仓库的 README、根目录/源码树，以及 Scriberr 源码树中与本题直接相关的文件；未使用 issues、PR、博客或第三方评测。

“未见”表示本次查到的材料没有足够证据，不等于项目绝对不支持。

## 结论先行

- **文件夹接入、持续监听、可恢复任务队列：优先参考 Scriberr。** `internal/dropzone` 递归监听 `data/dropzone`，启动时扫描已有文件；复制到上传目录后创建数据库任务并入队。`internal/queue` 有有界 channel（200）、按 CPU/环境变量配置 worker、启动恢复 pending 任务、状态更新和取消/杀进程路径。CLI `watch` 是单层目录监听，并带 2 秒 debounce。
- **本地音频能力的组合参考：FunASR。** 官方树中同时出现离线/在线 VAD 示例、`libtorch`/`onnxruntime` 音频前端、`postprocess_utils`、标点/说话人相关测试和 WebSocket/runtime 目录，适合做 ASR → VAD → 后处理/说话人模块的本地组合；它不是文件夹 watcher 或持久任务编排器。
- **识别器与时间轴参考：WhisperX、Fun-ASR、Qwen3-ASR。** WhisperX 的官方定位直接强调 word-level timestamps 与 diarization；Qwen3-ASR 的官方定位强调 language detection 与 timestamp prediction；Fun-ASR 更像识别模型/运行时家族，提供 FunASR、vLLM、streaming、llama.cpp 等运行路径。
- **实时输入参考：WhisperLiveKit、Meetily。** 两者的官方仓库定位偏实时/会议应用和本地处理；本次已查材料没有显示它们具备 Scriberr 那样明确的递归 dropzone + 可恢复持久队列，因此不应作为 Life Logger 文件导入主链路的直接替代。
- **Qwen3-ASR-Toolkit 是 API/批处理工具，不应默认当作离线引擎。** 已查到的官方定位是并行高吞吐、长音频、多采样率的 Qwen3-ASR API toolkit；本次材料没有形成本地文件夹监听、VAD、说话人识别或离线约束的证据。

## 横向比较

| 项目 | 音频/文件夹导入与监听 | 队列/任务模型 | 标准化与 VAD | 时间戳 | 说话人识别 | 后处理 | 离线/本地约束 |
|---|---|---|---|---|---|---|---|
| [Scriberr](https://github.com/rishikanthc/Scriberr/blob/bdb8838b8b9e4a58e74297f6ed2d0acb4c341c4f/README.md) | 上传、内置录音；`dropzone` 递归监听并扫描既有文件；CLI `watch` 监听单层目录 | SQLite 任务记录 + 有界 channel；worker 数量按 CPU/环境变量；启动恢复 pending；支持取消 | `audio/merger.go` 用 ffmpeg 做多轨 offset/gain/pan/amix，输出 MP3 192k；这是混音/派生转换，不是明确的统一 PCM/采样率规范；本次未见独立 VAD 入口 | README 明确写 word-level timing | README 的 smart speaker detection；树中有 WhisperX、PyAnnote、Sortformer 适配器 | 任务状态、SSE、notes、summary、LLM chat；可从转写继续生成摘要 | README 声称 completely offline；同时保留可选 OpenAI 兼容入口。dropzone 成功后会删除原文件，不能直接当 raw archive |
| [Meetily](https://github.com/Zackriya-Solutions/meetily/blob/0281737d87d26352fb0adc78c8c0975f691b23d1/README.md) | 树体现 `backend/whisper.cpp`、Python backend、Tauri frontend；本次未见明确文件夹 watcher/dropzone 入口 | 本次未见可复用的持久转写队列证据 | 本次未形成统一音频规范或独立 VAD 的证据 | 本次未形成可引用的时间戳细节 | 官方项目定位包含 speaker diarization | 有 `transcript_processor` 与 summary workflow 路径 | 官方定位强调 100% local；更像本地会议应用壳，非文件导入编排层 |
| [WhisperX](https://github.com/m-bain/whisperX/blob/2cfd7b7c5c7bba144954364db747319b50e8232b/README.md) | ASR/对齐/diarization building block；本次未见 watcher 或持久导入队列 | 未见 | 本次材料未形成统一标准化/VAD 入口证据 | 官方定位明确 word-level timestamps | 官方定位明确 diarization | 重点是对齐与说话人标注，不是生活事件抽取 | 适合作为本地模型处理组件；本次未据此推断完整离线产品约束 |
| [WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit/blob/b781ce9334c8085131b2b7a146a61d4e22ba5af1/README.md) | 官方定位是 real-time/streaming speech-to-text；本次未见文件夹导入主路径 | 未见持久批处理队列；实时连接/流式会话不是 durable job queue | 本次未形成统一标准化/VAD 细节证据 | 以实时 transcript segment 为主；本次未形成离线 word-timestamp 细节证据 | 官方定位明确 speaker diarization | streaming、translation、OpenAI/Deepgram-compatible API | 官方定位明确 local；适合实时链路，不是当前 Life Logger 的文件优先主路径 |
| [Qwen3-ASR-Toolkit](https://github.com/QwenLM/Qwen3-ASR-Toolkit/blob/c1976f5f3402e6c4258eca88be6f5dbb1ee32433/README.md) | 面向 Qwen3-ASR API 的 Python toolkit；长音频/多采样率/并发处理有定位，本次未见 watcher | API 并行吞吐不等于本地持久队列；本次未见 job recovery/status 证据 | README 定位包含 multi-sample-rate；本次未见 VAD | 本次未形成明确时间戳输出证据 | 未见 | 重点是 API 调用、长音频与吞吐，不是语义后处理 | 不默认视为 offline/local；使用前需单独验证模型与 API 的本地部署边界 |
| [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR/blob/7c6daf77a2421100f5fb066495372c00129d39ff/README.md) | 模型/推理层；本次未见文件夹接入、watcher、队列 | 未见 | 本次未见 VAD 证据 | 官方定位明确 timestamp prediction，并支持 language detection | 本次未见说话人识别证据 | 本次未见生活事件级后处理证据 | 作为模型仓库评估；不把模型能力等同于完整离线产品管线 |
| [Fun-ASR](https://github.com/QwenAudio/Fun-ASR/blob/903f2135e0a83ce1e4af5cff5690aa3876f92657/README.md) | 识别模型/运行时家族；README/树体现 FunASR、vLLM、streaming、llama.cpp 路径；未见 watcher/队列 | 未见持久任务队列 | 本次未形成独立 VAD 入口证据；标准化也未形成明确规范 | 本次未形成足够的官方 README/树证据 | 本次未形成足够证据 | 运行时/推理层为主，非生活事件合并层 | 提供本地运行时选项；适合接入现有本地 pipeline，而不是替代 ingest/orchestration |
| [FunASR](https://github.com/modelscope/FunASR/blob/6ee14b1a3283aa5a24e684bcd6ba68058e64adb0/README.md) | toolkit/runtime、HTTP/WebSocket 示例；本次未见文件夹 watcher | 未见面向文件导入的 durable queue | 树中有 `demo_vad_offline.py`、`demo_vad_online.py`、音频 frontend、`postprocess_utils.py` | 本次未形成统一时间戳格式结论 | 官方定位/树中有 speaker-diarization 相关能力与测试路径 | 标点、后处理、两遍/流式 runtime 等路径较丰富 | 树中有 libtorch/onnxruntime、offline/online VAD、WebSocket；适合本地组合，但仍需由 Life Logger 自己保留 raw 与 checkpoint |

## 对 Life Logger 的落地建议（基于上表的推断）

1. **沿用“接入层”和“识别层”分离。** 参考 Scriberr 的 `dropzone → job record → queue`，但不要照搬其成功后删除原文件的策略：先把 recorder 原文件做不可变归档，再生成标准化工作副本。
2. **标准化应成为独立、可重跑的派生层。** 目标可固定为统一容器/采样率/声道/PCM 格式，并记录原文件 hash、时长、采样率和转换命令；不要把 Scriberr 的 MP3 混音输出当作原始证据。
3. **VAD 与时间戳由 ASR 前后两侧共同负责。** 用 FunASR 的 offline VAD/frontend/postprocess 目录作为模块边界参考；用 WhisperX/Qwen3-ASR 的时间轴能力作为识别器候选，但保留原始 ASR、VAD 边界和模型版本。
4. **说话人/来源门控必须先于生活事件抽取。** 对 Life Logger，`source attribution → event extraction` 比“ASR 直接进事件规则”更重要；`unknown` 应进入人工复核，不能因为有 diarization 就直接认定是本人。
5. **实时项目只作为未来入口。** WhisperLiveKit/Meetily 可参考实时会话或会议 UI，但当前文件夹优先、离线优先的主链路先以可恢复批处理为验收标准。

## 已查仓库与源码入口

- Scriberr 源码入口：[dropzone.go](https://github.com/rishikanthc/Scriberr/blob/bdb8838b8b9e4a58e74297f6ed2d0acb4c341c4f/internal/dropzone/dropzone.go)、[queue.go](https://github.com/rishikanthc/Scriberr/blob/bdb8838b8b9e4a58e74297f6ed2d0acb4c341c4f/internal/queue/queue.go)、[watch.go](https://github.com/rishikanthc/Scriberr/blob/bdb8838b8b9e4a58e74297f6ed2d0acb4c341c4f/internal/cli/watch.go)、[merger.go](https://github.com/rishikanthc/Scriberr/blob/bdb8838b8b9e4a58e74297f6ed2d0acb4c341c4f/internal/audio/merger.go)。
- Meetily：[源码树](https://github.com/Zackriya-Solutions/meetily/tree/0281737d87d26352fb0adc78c8c0975f691b23d1)。
- WhisperX：[源码树](https://github.com/m-bain/whisperX/tree/2cfd7b7c5c7bba144954364db747319b50e8232b)。
- WhisperLiveKit：[源码树](https://github.com/QuentinFuxa/WhisperLiveKit/tree/b781ce9334c8085131b2b7a146a61d4e22ba5af1)。
- Qwen3-ASR-Toolkit：[源码树](https://github.com/QwenLM/Qwen3-ASR-Toolkit/tree/c1976f5f3402e6c4258eca88be6f5dbb1ee32433)。
- Qwen3-ASR：[源码树](https://github.com/QwenLM/Qwen3-ASR/tree/7c6daf77a2421100f5fb066495372c00129d39ff)。
- Fun-ASR：[源码树](https://github.com/QwenAudio/Fun-ASR/tree/903f2135e0a83ce1e4af5cff5690aa3876f92657)。
- FunASR：[源码树](https://github.com/modelscope/FunASR/tree/6ee14b1a3283aa5a24e684bcd6ba68058e64adb0)。
