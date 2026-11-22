# Demo 档与 Local 档

Life Logger 保留两个明确用途不同的运行档位。二者共享事件提取、证据指针和输出契约，但 ASR 入口不同。

## Demo 档：作品集 / 零配置验收

目标是让陌生人 clone 仓库后，在**没有本地 ASR model、没有 local-config.json、没有云端账号**的情况下验证最小处理链。

默认数据：

- fixture：`fixtures/demo_transcript.json`
- 内容：人工编写的脱敏中文样例，不是真实私人录音
- 时间：固定 timestamp offsets
- 覆盖：7 段完整口语；其中 5 段具有足够确定性，可结构化为 routine / food / expense / exercise / todo

一条命令：

~~~bash
python3 scripts/verify_demo.py
~~~

验收脚本只使用 Python 标准库和仓库源码，并在执行期间主动阻断 socket 网络连接。它会验证：

1. fixture transcript 走 `DemoTranscriber`，不加载 Fun-ASR / faster-whisper；
2. `transcript.json` 与人可读的 `transcript.md` 都完整保留 7 段原文；
3. 只生成 5 个 evidence-backed 高置信点；明确金额和提醒指令仍能结构化；
4. 两段普通工作闲聊 / 想法仍能在全文里回看，但不会被强行变成事件；
5. daily / events / transcript / SQLite 输出能写出；
6. Demo 不读取本地 model 路径，也不需要 `local-config.json`。

成功结果包含：

~~~text
"ok": true
"mode": "demo"
"event_count": 5
"transcript_count": 7
"transcript_only_count": 2
"network_allowed": false
"local_asr_required": false
~~~

如需保留生成物：

~~~bash
python3 scripts/verify_demo.py --output output/demo
~~~

也可以直接使用 CLI：

~~~bash
PYTHONPATH=src python3 -m life_logger.cli demo --output output/demo
~~~

Demo 只证明**处理链和输出契约能跑通**，不代表真实 ASR 准确率。

这里的产品口径是：**全文转写是真资产，事件只是高置信索引点。** 口语天然可能是半截话、修正句和上下文依赖句；事件契约仍保持 evidence / confidence / category 等稳定字段，但只有足够自足、确定的内容才进入该契约，其余内容继续留在全文而不是被编造成事件。

## Local 档：自用 / 隐私优先

Local 档处理真实录音，继续使用本机 Fun-ASR / CAM++ 等资源。它不会因为模型缺失自动切到 Demo，也不会静默上传音频。

准备：

~~~bash
cp local-config.example.json local-config.json
# 填写本机 worker Python、ASR、VAD、CAM++ 等路径
~~~

运行：

~~~bash
life-logger
~~~

或：

~~~bash
python -m life_logger.cli process /path/to/recording.m4a \
  --output output \
  --config local-config.json
~~~

如果本地资源不存在，Local 档应明确失败并提示缺失项；这是隐私边界，不做自动云端 fallback。

## 为什么不默认接云端 ASR

当前 Demo fixture 已经解决“clone 后能跑”的作品集问题，因此本批没有新增云端 ASR 依赖。若未来加入云端 ASR，只能作为显式 opt-in，并且不能改变 Local 档默认不上传原始音频的约束。
