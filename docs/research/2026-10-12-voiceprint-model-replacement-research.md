# 声纹归因提速：模型/运行时替换调研（2026-10-12）

调研目标：给 Life Logger 的逐段声纹归因（现用 `speech_campplus_sv_zh-cn_16k-common`，FunASR + PyTorch/MPS，批量 16）找到"明显更快、精度不明显降"的本地免费方案。
本报告区分【本机实测】【官方文档/论文数据】【第三方宣称】三类证据；拿不到的数据明确写"未测/未公开"。

---

## 0. 本机实测摘要（Apple M1 Pro 16GB，测试音频 = 20 段真实片段，共 189.4s）

测试素材：`output/real-2026-09-02-source-attributed-campp/source-review-pack-20260904-v1/clips/*.wav`（20 段，1.4–29.3s，合计 189.4s）。
注意：测试期间同机并行跑着其他 benchmark 子任务，单次数字有 1.5–2x 抖动，下表给区间。

| # | 路径 | 线程/设备 | 耗时 | RTF（=耗时/音频秒） | 说明 |
|---|------|-----------|------|------|------|
| 1 | **sherpa-onnx + 官方 CAM++ ONNX（28.3MB，模型与现用完全相同）** | CPU 4 线程 | 1.79–3.83s | **0.0094–0.0202（49–106x 实时）** | 【实测】重复 3 次；2 线程 2.40–5.35s；8 线程反而 6–12s（超订） |
| 2 | CoreML 转换件（FluidInference campplus-coreml，FP16） | CPU（`CPU_ONLY`） | 0.46s（另 preprocessor 0.05s） | **0.0024（约 413x）** | 【实测】`ALL/ANE` 2.86s、`CPU_AND_GPU` 8.26s，都更慢；见第 4 节精度告警 |
| 3 | 现状：FunASR `AutoModel` + CAM++ | MPS，batch16 | 3.33s（batch8 2.48s / batch2 10.0s） | 0.0176（隔离测试） | 【实测】隔离环境下 MPS 其实很快 |
| 4 | 现状：FunASR `AutoModel` + CAM++ | CPU，batch16 | 131–150s | **0.69–0.79** | 【实测】同模型 CPU 批量 16 有病理级开销；batch2 只有 1.68s（RTF 0.0089） |
| 5 | mlx-community 的 CAM++ MLX 移植 | — | **不可用** | — | 【实测】`weights.npz` 是 torch 原始 state_dict（键名 `xvector.*`），与仓库自带 `model.py`（模块名/结构不同、头 512 维 vs 官方 192 维）不匹配，`load_weights` 直接报"937 parameters not in model"；且卡片给的输入形状 (1,80,T) 与 MLX conv1d 语义 (N,L,C) 相反，前向必失败 |

**跨运行时一致性（同一批 20 段，两两余弦结构相关）**【实测】：
- sherpa-onnx vs FunASR：余弦相似度 min 0.67 / mean 0.85；相似度矩阵相关 **0.946**
- CoreML vs FunASR：mean 0.41；矩阵相关 0.72–0.76（差异明显）
- 结论：sherpa-onnx 与原路径"行为几乎一致"；CoreML 转换件"数值上偏离"，需复测。

**真实片段上的自/他判别抽查（9 段 POC 素材，sherpa-onnx）**【实测】：
- 本人（对 template.wav）：self-1..4 = 0.61–0.71，self-5（0.36s 太短）= 0.29
- 他人/媒体：other-call 0.45、media-news 0.13、media-game 0.11、media-english 0.24
- CoreML 同素材：self-1..5 = 0.42–0.54 但 other-call 0.56、media-english 0.52（判别明显变差）

**生产环境上下文**【实测自 job 产物】：
- 183 条 attribution 记录（>1s）：median 15s / mean 161s / max 4970.8s；仓库文档里的"平均 748s"应为另一子集口径。
- 最长 job（4970.8s）：14 个 30-min 窗口共约 78 分钟为窗口阶段；1408 段、7954s 语音；窗口级有效吞吐（含切片/特征/分类全部开销）按"补齐后的批时长"折算 RTF≈0.17，而隔离测试同模型 MPS 是 0.008–0.018 → **生产路径有 ~10–20x 的非模型开销或环境损耗**（疑似实际落在 CPU、或并发/内存压力，见第 5 节）。
- 该 job 的 MosFormer 分离阶段：61 次尝试 **全部 error**、救回 0 条；`local-config.json` 里 `speech_separation_model` 误指向 CAM++ 模型目录。

---

## 1. 为什么"换更大声纹模型"不是答案：3D-Speaker 家族对比

CAM++ 在自家家族里已是**算力最省**的一档，往上换模型只会更慢。

| 模型（3D-Speaker 官方仓库） | 参数量 | FLOPs(3s) | VoxCeleb1-O EER | CN-Celeb EER | 来源 |
|---|---|---|---|---|---|
| CAM++（现用） | 7.2M | **1.72G** | 0.65%（LM） | 6.30% | 3D-Speaker README / 论文 Table III |
| ERes2Net-base | 6.61M | 5.16G | 0.84–0.96% | 6.11% | 同上 |
| ResNet34 | 6.34M | 2.65G | 1.05% | 6.92% | 同上 |
| ECAPA-TDNN | 20.8M | 5.64G | 0.86% | 7.67–8.01% | 同上 |
| ERes2NetV2 | 17.8M | 12.6G | 0.61% | 6.04% | ERes2NetV2 论文（Interspeech 2024 系列）|
| ERes2Net-large | 22.46M | —（更大） | 0.52% | 6.17% | 3D-Speaker README |

来源链接：https://github.com/modelscope/3D-Speaker ；论文表 https://arxiv.org/html/2403.19971v3 ；ERes2NetV2 https://arxiv.org/html/2406.02167
→ ERes2NetV2 比 CAM++ 多 7 倍算力换 0.1 个点 EER，对"每 60s 一段的自/他二分类"毫无必要。**结论：3D-Speaker 家族内没有"更快"的升级项；要提速只能换运行时/导出格式或换更小的模型。**

## 2. 主流声纹候选总表

| 模型 | 来源 | 参数量 | EER（来源口径） | 速度证据 | ONNX | CoreML | MLX | 许可 |
|---|---|---|---|---|---|---|---|---|
| **CAM++ zh-cn-16k-common（同款）ONNX** | sherpa-onnx 官方发布；HF 镜像 csukuangfj/speaker-embedding-models | 7.2M（28.3MB 文件）| 与现用完全同一权重 | 【本机实测】RTF 0.009–0.028（CPU） | ✅ 现成 | 见下 | 见下 | Apache-2.0（ModelScope 页面）|
| CAM++ coreml（FluidInference） | HF FluidInference/campplus-coreml | 7.2M（FP16 约 16MB） | 卡片称 AISHELL-1 EER 0.48%（自测口径） | 【本机实测】RTF 0.0024（CPU_ONLY）；但判别结构偏离（见上） | — | ✅ 现成 | — | 上游许可（FunASR，Apache-2.0）|
| CAM++ MLX（mlx-community） | HF mlx-community/speech_campplus_sv_zh-cn_16k-common（含 q4） | 6.93M（npz 实测 937 张量） | 无 | 【本机实测】**跑不起来**（见第 0 节 #5） | — | — | ❌（现有包坏）| 卡片未标 |
| ReDimNet2 B0–B6 | PalabraAI/redimnet2（Interspeech 2026 论文） | 1.1–12.3M；0.33–13 GMACs | B0 1.04% → B6 0.29%（Vox1-O，LM）| 无速度实测（GMACs 参考：B0 比 CAM++ 少 3.5 倍算力）| ❌ 无现成 | ❌ | ❌ | MIT |
| ReDimNet B0–B6（2024 原版）| IDRnD/RedimNet | 1–15M；0.43–20 GMACs | B2 0.57% / B6 0.40% | 同上 | ❌ | ❌ | ❌ | MIT |
| WeSpeaker **CAM++ / ECAPA512 / ResNet34 / SimAM-34/100**（含中文 CN-Celeb 版） | wenet-e2e/wespeaker | CAM++ 7.2M；ECAPA512 6.19M/1.04G；ResNet34 6.63M/4.55G；SimAM-R34 25.2M | vox1-O-clean：CAM++≈0.7；ECAPA512 1.07→0.73（LM+ASNorm）；ResNet34 0.87→0.64；SimAM-R34 0.42；CN-Celeb（中文）：ResNet34 6.12% | 未公开 RTF | ✅ 官方 onnx 导出（含 zh-CN 模型 26MB）| ❌ | ❌ | Apache-2.0 仓库；VoxCeleb 模型 CC-BY-4.0 |
| NeMo TitaNet-Large / Small | NVIDIA NeMo/NGC | L 25.3M（101MB ONNX）/ S 6.4M（40MB ONNX）| L 0.68%；S 1.08%（NGC 卡片）| 【官方文档 demo】TitaNet-small 使 diarization 整条管线 RTF 0.297→0.119（单线程 CPU，非 M1）| ✅ 现成（sherpa 发布）| 第三方有 TitaNetSmall CoreML（Otosaku/NeMoSpeaker-iOS，非官方）| ❌ | NVIDIA 模型卡（多为 CC-BY-4.0，使用前确认）|
| pyannote / Wespeaker ResNet34-LM | pyannote/wespeaker-voxceleb-resnet34-LM | 6.6M（256 维）| 卡片口径 0.7–0.9% | 【本机未测】；【第三方】mlx 移植卡片称"余弦差异 ≤2.4%" | ✅（WeSpeaker 导出）| ✅（FluidAudio/SpeakerKit 用了同源模型）| ✅ mlx-community/wespeaker-voxceleb-resnet34-LM（MIT 标记，129 下载）| 模型 CC-BY-4.0 |
| SpeakerKit（Argmax）CoreML 全套 | HF argmaxinc/speakerkit-coreml（pyannote-v3 embedder + PLDA，W16A16）| — | 卡片称与 pyannote 同级 | 【第三方宣称】iPhone 上 4 分钟音频约 1 秒 | — | ✅ | — | 模型 CC-BY-4.0；框架 MIT；Pro 版商用 |
| FluidAudio（Swift/CoreML）| FluidInference/FluidAudio + HF 模型 | pyannote 分割 + wespeaker_v2 embedding | AMI DER 17.7%（其自测）| 【第三方宣称】RTFx 最高 150x（M2 ANE）；【本机未测】 | — | ✅ | — | 模型 MIT/Apache-2.0（Community-1 集为 CC-BY-4.0）|
| ECAPA2（ECAPA 变体）| Jenthe/ECAPA2 | 27.1M / 187 GMACs | 0.44% | 算力巨大，不适合 | ❌ | ❌ | ❌ | — |
| i-vector / GMM-UBM（传统）| Kaldi 生态 | — | 短音频/噪声下明显差于神经网络方案 | 部署成本高 | — | — | — | 各式 |

另：WeSpeaker 已集成 ReDimNet2B6_LM 检查点（无 ONNX），另支持"U³-xi 不确定度打分"；其官方模型页：https://github.com/wenet-e2e/wespeaker/blob/master/docs/pretrained.md

## 3. 关键工程发现（比"换模型"更重要）

1. **FunASR 批处理有病理级开销**：同一模型，CPU batch16 实测 RTF 0.69–0.79，而 batch2/8 只有 0.009–0.023；batch 越大、段越长越糟（内部把整批补齐到最长段，16×60s 的补齐把有效计算量放大 4–5 倍，且大张量 CPU 效率骤降）。窗口实测显示"补齐后时长/真实语音时长"达 4–5x。
2. **生产路径比隔离测试慢 10–20x**：隔离测试 MPS batch16 RTF 0.018，而最长 job 的窗口阶段折算 ≈0.17。可能原因（按可能性）：① 生产进程实际未用上 MPS（回退 CPU）② 与 ASR/其他任务并发导致统一内存压力（本机 16GB，Kev 已有前车之鉴）③ MPS 在长音频大 batch 下抖动。**建议改模型之前先在 prod 路径打点：打印实际 device、分阶段计时（切片/噪声底/embedding/音频分类）。**
3. **分离（MossFormer）阶段当前 100% 失败**：61/61 errors，救回 0；`local-config.json` 的 `speech_separation_model` 指向 CAM++ 目录（疑似配错路径）。它目前不是耗时主因，但功能等于空转。
4. 换运行时后**阈值必须重校准**：不同前端的归一化不同（三者 embedding 范数 12.6/13.1/16.4），自/他分数分布会整体平移。现网 self=0.45–0.50 / unknown=0.25 需要用自己的标注片段重定。
5. 传统方法（i-vector/GMM-UBM）没有提速价值：部署重、短句/噪声下精度明显差；"用 CAM++ 中间层特征+余弦"也不省算力（同一前向）。真正省算力的替代是更小的模型（ReDimNet2-B0 1.1M/0.33GMAC，比 CAM++ 省 3.5 倍算力）——但需要自己导出 ONNX。

## 4. 推荐 Top 3（在 M1 Pro 16GB 上最值得试）

1. **sherpa-onnx + 官方 CAM++ ONNX（同模型换运行时）**——首选、风险最低。
   - 实测 RTF 0.009–0.028（CPU，4 线程；小片段逐条更稳），相比现状窗口级 0.17 有 ~10x+ 余量；
   - 与 FunASR 输出相似度矩阵相关 0.946，判别抽查正常；
   - `pip install sherpa-onnx`，模型 28MB 现成（GitHub release / HF csukuangfj）；零训练、零转换。
   - 配套：单段截到 ≤10s、按长度分桶、batch 小、日志打印实际 device；同一调用方式也适用于 `wespeaker_zh_cnceleb_resnet34.onnx`（若想换中文模型）。
   - 复用本次产物：`~/.hermes/cache/scratch/venv-sherpa`、`.../models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`、`bench_sherpa.py`（24h 内有效，重要的话先拷贝）。
2. **CoreML 版 CAM++（FluidInference campplus-coreml）**——最快的实测路径（RTF 0.0024，413x），但**先复测精度再上**。
   - 优点：FP16/ANE 优化、16MB、Python 用 `coremltools.CompiledMLModel` 即可跑（本机已验证）；输入限制：音频 ≥0.5s、特征 64–6000 帧（≤60s，超长要切块）。
   - 风险：与 FunASR embedding 的相似度矩阵相关只 0.72–0.76，小样本上 other-call/media 的余弦反而高于本人片段 → 在换掉之前，用自己的"本人/他人/媒体"标注各 20+ 段复核一遍 EER/阈值。
3. **模型换代候选：ReDimNet2-B0/B2 或 WeSpeaker 中文模型**——下一轮再评估。
   - ReDimNet2-B0：1.1M 参数/0.33 GMACs（比 CAM++ 省 3.5x 算力）、Vox1-O 1.04%，MIT；但**没有现成 ONNX/CoreML/MLX**，要用 torch.hub 权重自行导出（工程日 0.5–1 天）。
   - WeSpeaker `wespeaker_zh_cnceleb_resnet34(_LM).onnx`（CN-Celeb 6.12% 口径，官方 ONNX，即拿即用）作为"中文场景另一条腿"。
   - MLX 方向：mlx-community 的 CAM++ 包目前是坏的（可修复但属于自研工程）；`mlx-community/wespeaker-voxceleb-resnet34-LM` 未被验证，优先级低于 1/2。

## 5. 附：证据链接

- 3D-Speaker: https://github.com/modelscope/3D-Speaker · 论文 https://arxiv.org/html/2403.19971v3 · ERes2NetV2 https://arxiv.org/html/2406.02167
- WeSpeaker: https://github.com/wenet-e2e/wespeaker · 模型清单 https://github.com/wenet-e2e/wespeaker/blob/master/docs/pretrained.md · 结果 https://github.com/wenet-e2e/wespeaker/blob/master/examples/voxceleb/v2/README.md
- ReDimNet2: https://github.com/PalabraAI/redimnet2 · 论文 https://arxiv.org/html/2603.11841v1 · 原版 https://github.com/IDRnD/RedimNet（分支 master）
- sherpa-onnx: https://github.com/k2-fsa/sherpa-onnx · 声纹模型发布 https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models · diarization demo 数字 https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html
- NeMo TitaNet: https://catalog.ngc.nvidia.com/orgs/nvidia/teams/nemo/models/titanet_large · titanet_small · 导出脚本 https://github.com/k2-fsa/sherpa-onnx/blob/master/scripts/nemo/speaker-verification/README.md
- CoreML: https://huggingface.co/FluidInference/campplus-coreml · https://huggingface.co/argmaxinc/speakerkit-coreml · https://github.com/FluidInference/FluidAudio
- MLX: https://huggingface.co/mlx-community/speech_campplus_sv_zh-cn_16k-common（坏）· https://huggingface.co/mlx-community/wespeaker-voxceleb-resnet34-LM（未验证）
- pyannote: https://huggingface.co/pyannote/embedding（MIT）· https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM（CC-BY-4.0）
- ModelScope 模型页（许可 Apache-2.0）: https://www.modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common
