# Life Logger

[English](README.md) | [简体中文](README_CN.md)

**一套本地优先、注重隐私的个人全天候生活记录系统：Apple Watch 持续分段录音 + Mac 本地语音转写与声纹识别 + 上下文感知的活动抽取（v3）+ 多端可视化看板（Web 瀑布流 v2、macOS 原生桌面端、Apple Watch、iOS 接收器）。**

Life Logger 能把全天候的语音流整理成可追溯、不胡猜的每日账本——**消费账本、饮食与三大营养素账本、健身训练记录、睡眠与健康概览、以及清爽的「我的生活」时间轴**，同时自动把对电脑/AI 说的话和背景音视频隔离开，让你一眼只看清真实的生活。

---

## 核心特性

### 1. Apple Watch 手表端（`watchos/LifeLoggerWatch`）
- **全天分段录音与断网续传**：16 kHz 单声道 AAC（`48 kbps`）后台持续分段录音，本地落盘缓存，连上局域网 / Wi-Fi 后自动分片上传至 Mac。
- **HealthKit 健康联动**：随音频分片自动附带心率、步数与睡眠时长快照。
- **个人开发者签名自动续期**：[`scripts/build_signed_watch_app.sh`](scripts/build_signed_watch_app.sh) 支持通过 Xcode `-allowProvisioningUpdates` 自动向 Apple 续期 7 天个人证书，并强制校验 `com.apple.developer.healthkit` 权限。

### 2. Mac 本地处理引擎（`src/life_logger/`）
- **本地 ASR + 声纹归属**：可插拔语音转写管线（MOSS / FunASR / Whisper / MLX），结合 CAM++ 声纹识别区分「本人 / 他人 / 媒体外放」，并内置多句循环复读与静音幻觉熔断保护。
- **上下文活动抽取 v3（`activity_prompt_v3.py`）**：
  - 按滑动对话窗口理解整段上下文，替代脆弱的单句抽取；
  - 严格区分 **个人真实生活**、**对电脑/终端/AI 编程工具说的话**（自动归入 `编程·工作`）以及 **外放视频/直播/播客**（折叠进 `媒体内容`），同时确保 **对微信说的话** 始终保留在「我的生活」中；
  - 支持现实口语的自我纠偏（如半途放弃原计划改买别的、或事后同日补报某笔商品的价格）。
- **单一事实来源四本账（`day_facts.py`）**：
  - **消费账本**：区分已确认支付与仅提及/未确认报价；绝不编造金额，也不静默合并两笔同额消费；
  - **饮食与营养账本**：支持口述食物重量、生米重量折算以及口念包装营养成分表（`kJ/kcal`、`NRV%` 反推克数）；支持在页面直接修改克数和每 100g 成分表并持久化写入自定义食物库；**没说份量时绝不瞎估热量**（标为待定并进入复盘）；
  - **健身与健康账本**：结构化提取动作、组数、重量（回顾优先、现场补全），联动手表睡眠/心率/步数；
  - **需要我判断队列（`/api/judgment-queue`）**：不确定的份量、待确认金额、疑似重复记账统一收口等待一键确认，不静默污染账本。

### 3. 多端展示与交互
- **Web 瀑布流 v2（`web/waterfall-v2.html`）**：
  - 顶部三栏概览（**消费 / 饮食 / 运动·健康**）+ 按小时对齐的垂直时间轴（左竖条代表有录音，右竖条代表所属场景色）；
  - **三大营养素与热量可视化进度条**：以你的**体重**和**日常静日消耗（kcal）**为基准，直观展示蛋白、碳水、脂肪与总热量的达标/超标状态，支持一键切换 **纯减脂 / 塑形维持 / 净增肌** 三档目标模式并随时手动微调；
  - **极简无噪音的「我的生活」视图**：默认只展示真实生活事件，点顶栏 `编程·工作`、`媒体内容` 或 `来源待确认` 可随时回看全部内容，不丢数据。
- **原生 macOS 桌面与菜单栏应用（`desktop/LifeLoggerMac`）**：原生 SwiftUI 桌面端与菜单栏小组件。
- **iOS / 移动端与硬件接收器（`ios/LifeLoggerReceiver`、`mobile/life_logger_receiver`、`desktop/AirPassportReceiver`）**：移动端同步与外接 BLE 录音硬件接收端。

---

## 系统架构

```
┌───────────────────────────┐     局域网 / Wi-Fi 上传      ┌────────────────────────────────────┐
│  Apple Watch 端 (watchOS) │ ──────────────────────────► │    Mac 本地 Web + Worker 服务       │
│  • 16kHz 48kbps AAC 分段  │   (音频 + HealthKit + 位置)  │   • watch_stream / watch_upload    │
│  • 本地缓存与断点续传      │                             │   • 本地 ASR + CAM++ 声纹识别       │
└───────────────────────────┘                             │   • 活动抽取 v3（LLM 上下文理解）    │
                                                          │   • 单一事实来源四本账              │
┌───────────────────────────┐     HTTP / JSON API         └─────────────────┬──────────────────┘
│  Web 瀑布流 v2 界面        │ ◄─────────────────────────────────────────────┤
│  macOS 桌面端 LifeLoggerMac│                                               │
│  iOS / 移动端接收器        │ ◄─────────────────────────────────────────────┘
└───────────────────────────┘
```

所有个人录音、转写文本、API 密钥与每日账本均仅保存在你自己的 Mac 本地（`output/` 与 `local-config.json`，已全部加入 `.gitignore`）。

---

## 目录结构

```text
life-logger/
├── src/life_logger/             # Python 核心后端、ASR、v3 活动抽取、四本账与 HTTP 服务
├── web/                         # Web 界面（瀑布流 v2 时间轴、顶部概览卡、内联补录与复盘）
├── watchos/LifeLoggerWatch/     # Apple Watch SwiftUI 录音与 HealthKit 同步应用
├── desktop/LifeLoggerMac/       # 原生 macOS SwiftUI 桌面端与菜单栏小组件
├── desktop/AirPassportReceiver/ # 外接 BLE 录音硬件 Mac 接收器
├── ios/LifeLoggerReceiver/      # iOS Swift 接收器核心包
├── mobile/life_logger_receiver/ # Flutter 移动端接收伴侣
├── config/                      # v3 活动提示词与用户背景示例配置
├── scripts/                     # 编译构建、手表签名、校验与运维脚本
├── deploy/                      # macOS launchd 后台服务模板
├── docs/                        # 架构设计文档、ADR 与运维手册
└── tests/                       # Python (pytest) 与前端 (node --test) 测试套件
```

---

## 5 分钟快速开始（macOS）

### 环境要求
- macOS（推荐使用 Apple Silicon 运行本地 ASR）
- Python 3.11+（`uv` 或 `venv`）
- Node.js 20+（用于运行前端测试）
- Xcode 15+ 与 `xcodegen`（仅在需要编译 Apple Watch / macOS 原生应用时安装）

### 1. 克隆仓库并安装依赖

```bash
git clone https://github.com/lwk-12345/life-logger.git
cd life-logger

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. 初始化本地配置

```bash
cp local-config.example.json local-config.json
cp config/user-profile.example.md config/user-profile.md
```

编辑 `local-config.json`，填写你的本地 ASR / VAD 模型路径以及所选大模型接口（兼容 OpenAI / DeepSeek / Ollama 本地模型）。

### 3. 启动本地 Web 服务

```bash
python3 -m life_logger.local_web --host 127.0.0.1 --port 8765
```

在浏览器打开 **http://127.0.0.1:8765** 即可使用。

> 想先不接硬件直接预览页面效果？请查看 [`docs/demo-mode.md`](docs/demo-mode.md)。

### 4. 编译并安装 Apple Watch 端（可选）

1. 将 `watchos/LifeLoggerWatch/project.yml` 中的 `DEVELOPMENT_TEAM` 改为你自己的 Apple 开发者 Team ID，或设置环境变量 `APPLE_TEAM_ID`；
2. 执行签名编译（自动附加 HealthKit 权限并续期 7 天描述文件）：

```bash
export APPLE_TEAM_ID="你的10位AppleTeamID"
bash scripts/build_signed_watch_app.sh
```

3. 刷入已配对的 Apple Watch（保持手表亮屏并与 Mac 在同一 Wi-Fi，或用数据线连接 iPhone）：

```bash
xcrun devicectl list devices
xcrun devicectl device install app --device 你的手表设备ID \
  watchos/LifeLoggerWatch/build/SignedDerivedData/Build/Products/Debug-watchos/LifeLoggerWatch.app
```

### 5. 编译原生 macOS 桌面端（可选）

```bash
cd desktop/LifeLoggerMac
swift build
swift test
```

---

## 运行测试

```bash
# 前端语义与交互测试
node --test tests/waterfall-v2-semantics.test.mjs

# 后端 Python 核心测试
.venv/bin/python -m pytest -q tests/test_food_nutrition.py tests/test_expense_policy.py

# WatchCore Swift 包测试
cd watchos/LifeLoggerWatch/CorePackage && swift test
```

---

## 隐私与本地优先原则

- **数据完全留在本地**：所有音频、转写原文、声纹模板与账本数据仅存于本机 `output/`，不强制上传第三方云盘；
- **严格的 `.gitignore` 防护**：`local-config.json`、`config/user-profile.md`、`output/`、`*secrets*.json`、`*.mobileprovision`、`golden/` 默认全部忽略；
- **宁可留白，绝不胡猜**：没说份量的食物标为「待定」、不确定的价格进「需要我判断」，绝不用假数字或 `0` 掩盖缺失数据。

---

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
