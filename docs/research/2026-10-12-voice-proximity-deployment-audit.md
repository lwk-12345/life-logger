# 人声属性与声音距离部署核查

核查：Codex，2026-10-12。只读检查代码、安装元数据、资源、既有录音产物及LaunchAgent；未模型推理、测试、配置修改或重启。

## 结论

| 组件 | 安装/资源证据 | 实际接线与产物 |
| --- | --- | --- |
| 声音近远场 | 本仓transcribe.py:710和source_attribution.py:395；NumPy 1.26.4在配置指定的外部运行环境中 | 当前源码计算RMS、底噪、低中频能量比并调用加权评分；上午归档ASR864行，863行有acoustic_proximity；逐段331份state为104completed、227no_speech，存在snr_proxy_db/proximity_ratio_db/proximity_score。证明运行过；不证明所有产物由最新修订源码生成 |
| Silero VAD | faster_whisper 1.2.1；内置silero_vad_v6.onnx 1245151字节；vad.py:13注明改编自https://github.com/snakers4/silero-vad | transcribe.py调用get_speech_timestamps切出语音；负责有无语音/区间，不在近远场函数中估计米或厘米 |
| SpeechBrain | 配置的Python环境中speechbrain 1.1.1 dist-info实存 | 当前src/scripts/docs检索未找到调用，不能将安装等同声音距离部署，也未定位到用户所指的独立GitHub估距项目 |
| CAM++相关资源 | 配置的speaker=local_campplus、模板及speech_separation_model路径存在 | 当前逐段源码为NumPy48维声纹池；CAM++整场选项仍保留。资源实存不能证明当前逐段调用CAM++ |
| 场景模块 | scene_classifier.py存在；生产目录检索只有定义与feature flag | 未找到生产音频调用，不证明网页场景已经模型识别 |
| 本地情绪 | LocalTurbo._extract_emotion_tag丢弃输入直接返回NEUTRAL；既有行有NEUTRAL | 当前函数为占位值，不能当作真实情绪模型判断；保留另一路云端能力与本地主路径区分 |

## 近远场算法细节

- 100ms帧RMS的第20百分位估计底噪；语音窗RMS减底噪得到snr_proxy_db。
- 实时路径relative_loudness_db=snr_db−10，并非独立传感量；归档重新量测路径用邻接60秒语音音量中位数作参考，二者定义不同。
- 低频80–320Hz与中频1000–3500Hz能量均值比转换为dB，再减6。
- 加权评分：音量0.20、SNR0.30、相对响度0.20、频谱比0.30；near≥0.65，far≤0.35，其余uncertain。
- 这是近远场启发式评分，没有distance_m/距离回归模型调用或独立模型权重配置；代码15cm与unforgeable注释不能作为实际测距及身份保证。proximity_confidence由分数距0.5的距离计算，不是经过真实标注校准的准确率。
- watch_formal_asr后续far/ratio<−1.5身份覆盖仍存在。保留已有有效媒体排除，用正常佩戴样本校准；无需仅因用户记得GitHub项目就重复安装模型。

## 实例与验证边界

本轮launchctl只读查询成功：state=running、pid=2279、runs=99，与上轮台账PID52022不同，须订正旧PID。不能仅凭新PID证明最新磁盘源码已载入。本轮没有重启。ps因沙箱operation not permitted，改用launchctl，无重复同路尝试。

源码SHA-256：
- transcribe.py 69ebd664f12dec0424554aa83f0d3634dff76c1dd2f00197bf55ba7e1549bfbf
- source_attribution.py ba527936589f1e011dcd79308527b79c631509434363625ef622ea351742985d
- watch_formal_asr.py 9ce79a986ee63b34281bc0ba31dfc70a704b0044bc5bb59e2e2aaebfa46137fa

下一步：定位用户所指项目的确切名称/路径，再对照运行入口；在未找到前按本地已证实的近远场链路校准，不能宣布该外部估距项目已部署或未安装于整台机器。
