# AI Passport → Life Logger 同网录音通道

本通道复用 AI Passport 固件 PoC 的既有协议，不引入云中转，也不要求修改硬件仓主线。

协议保持兼容：POST /v1/audio/chunk、POST /v1/audio/end，以及
X-LifeLogger-Session / Device / Chunk / Checksum / Sample-Rate / Format /
End / Type / Event / Event-Time-Ms 等头。Checksum 使用 crc32:XXXXXXXX。

## 数据落点

接收器原始数据保存在：

~~~text
output/local-web/passport-stream/<device>/<session>/
├── chunk-00000000.s16le
├── chunk-00000001.s16le
├── manifest.jsonl
├── summary.json
└── recording.wav
~~~

recording.wav 只有 complete=true 时才存在。设备级短按等事件保存在
output/local-web/passport-stream/<device>/events.jsonl。

每个分片先验证 CRC32 再落盘。结束包中的 X-LifeLogger-Chunk 是应有总分片数。
若缺片，summary.json 明确列出 missing_chunks，complete=false，不生成 WAV，
也不创建假完整的 Life Logger job。

完整后按顺序组装 16 kHz / mono / 16-bit WAV，再复用现有 JobStore.add_file()
和 start_job() 进入：

~~~text
WAV → Fun-ASR-Nano → CAM++ / 声源归属 → 事件提取 → 日记
~~~

导入文件状态会带 capture_source=ai-passport-wifi、source_device、
source_session、source_transport=lan-http-pcm 与
source_integrity=complete-crc32-verified。

## 1. 启动 Life Logger Web

正常启动：

~~~bash
python3 scripts/run_local_web.py
~~~

正常的 Life Logger Web 启动路径会自动拉起 AI Passport companion receiver：
TCP 8766 接收音频，UDP 8767 响应设备发现。固件保存 Wi-Fi 后会先确认设备取得
局域网地址，再检查已有 endpoint；不可用时先 UDP 自动发现，再做有界子网回退扫描。
配网页的手动 endpoint 只是高级回退，不是日常必填项。

页面 **导入与处理 → AI PASSPORT · LAN** 会显示：

- receiver 是否正在自动监听；
- TCP / UDP 端口；
- receiving / complete / incomplete / event-only 会话；
- 缺片编号；
- 完整 WAV 的本地回放；
- 自动导入后的 Life Logger job。

## 2. 独立 receiver（仅调试 / 手工回退）

正常使用不需要再开第二个终端。只有调试协议、希望 `--no-auto-start`，或临时不运行
Life Logger Web 时，才在仓库根目录单独启动：

~~~bash
PYTHONPATH=. ~/.local/bin/uv run python scripts/run_passport_stream_receiver.py \
  --host 0.0.0.0 \
  --port 8766
~~~

独立 receiver 下，完整会话同样会自动创建并启动普通 Life Logger job。

只验收接收/导入、不想跑长 ASR 时：

~~~bash
PYTHONPATH=. ~/.local/bin/uv run python scripts/run_passport_stream_receiver.py \
  --host 0.0.0.0 \
  --port 8766 \
  --no-auto-start
~~~

此时完整 WAV 会创建 draft job，但不会启动 ASR。接收器拒绝公网 IP 监听地址；
不要做路由器端口映射。

正式 Web companion 可通过 `local-config.json` 的可选段落调整；默认无需配置：

~~~json
{
  "passport_stream": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 8766,
    "discovery_port": 8767,
    "auto_start": true
  }
}
~~~

`auto_start=true` 表示完整 WAV 会立即进入现有 ASR 主链；改成 `false` 时只创建
Life Logger draft job。不要把 host 配成公网地址，也不要对路由器做端口映射。

## 3. 自动发现与本机局域网 IP

Mac 使用 Wi-Fi 时可先尝试：

~~~bash
ipconfig getifaddr en0
~~~

若为空，到 系统设置 → Wi-Fi → 当前网络 → 详细信息 查看 IPv4。

假设电脑地址是 192.168.1.20，则 Passport 要访问这个地址，不是 127.0.0.1。
正常情况下不需要把这个 IP 手抄进 Passport；UDP discovery 会从响应包的源地址
得到 receiver IP。只有自动发现失败时才需要手动查 IP 并填 endpoint。

## 4. Passport 配网与 receiver 配对

1. 手机连接 AI-Passport-XXXX。
2. SoftAP 密码：aipassport。
3. 打开 http://192.168.4.1/。
4. 填 Passport 要加入的 2.4 GHz Wi-Fi SSID / 密码。
5. 通常将 endpoint 留空：设备会用 UDP 8767 自动发现同一局域网内的 receiver，
   然后通过 TCP 8766 的 `/health` 验证它可用。若自动发现被网络隔离或防火墙拦截，
   可在“高级”中手动填写：

~~~text
http://192.168.1.20:8766/v1/audio/chunk
~~~

把 IP 换成实际运行 receiver 的电脑局域网 IPv4。

Passport 与电脑必须在同一个可互访 LAN；路由器不能开启客户端隔离。
macOS 防火墙需要允许 Python/receiver 的 TCP 8766 和 UDP 8767 入站连接。

不要填 http://127.0.0.1:8766/v1/audio/chunk，因为对 Passport 来说
127.0.0.1 是 ESP32 自己。

## 5. 真机闭环验收

1. 正常启动 Life Logger Web；页面应显示 **自动发现已运行 · TCP 8766 · UDP 8767**。
2. Passport 与电脑加入同一个可互访 LAN。
3. Passport 配网页保存 2.4 GHz Wi-Fi；endpoint 留空，观察设备自动发现 receiver。
4. 若网络阻断 UDP discovery，再手动填 `http://<电脑局域网IP>:8766/v1/audio/chunk`。
5. 在 Passport 上录几秒长录音并结束。
6. 打开 Life Logger 的 **导入与处理 → AI PASSPORT · LAN**。

完整会话应显示 AI Passport、device、session、完整、N/N 片与关联 job。
磁盘同时应有原始 chunk、manifest、summary 和可播放 recording.wav。
任务详情会标明 AI Passport · device · session。
Passport 的独立设备事件（当前固件 contract 包括 `note_start` / `note_end`）也会作为 event-only 会话显示并保留原始事件证据，
但不会伪造成一段录音或擅自转换成七类 LifeEvent。

若缺片，Web 必须显示缺片编号；summary.json 中 complete=false 和
missing_chunks 非空；此时不得出现 recording.wav，也不得创建假完整 job。

若固件没有发送 `/v1/audio/end`，receiver 不会猜测总分片数。音频分片停止超过 30 秒后，
Web 会把该会话从“接收中”标为 **“接收停滞”**：已有 `.s16le` 与 manifest 继续保留，
但不会组 WAV、不会创建 job，也不会伪造 `missing_chunks`。如果同一 session 后续继续收到
分片，状态会自动恢复为“接收中”；只有真实 end marker 才能进入完整/缺片判定。

## 6. 本地协议回归

~~~bash
PYTHONPATH=. ~/.local/bin/uv run pytest tests/test_passport_stream.py -q
~~~

覆盖 CRC32 错误、原始 chunk 保留、两条 HTTP 路径、完整才组 WAV、缺片不导入、
短按 event-only 会话、UDP nonce discovery、HTTP `/health`、combined receiver runtime、
Web companion 自动导入、正常 JobStore 导入、会话状态 API 与本地音频回放。
