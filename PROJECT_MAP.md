# 项目地图（一页看懂）

> 这一页就是入口。想知道"某个东西在哪、进度记在哪"，看这里就够，不用记别的。

## 一、这个项目由 4 个模块组成，装在 2 个仓里

| 模块 | 干什么 | 代码在哪 | 谁在用 |
|---|---|---|---|
| **Life Logger 网页端** | 网页工作台、本地服务的接口 | 仓① `web/`、`src/life_logger/`、`scripts/` | 你在浏览器里用 |
| **Apple Watch 应用** | 录音、分段落账、同步 | 仓① `watchos/`、`ios/`、`mobile/` | 手表上 |
| **Mac 接收器** | 收无线麦音频（AirPassport.app）、和 Life Logger 交接 | 仓① `desktop/AirPassportReceiver/` | 你 Mac 菜单栏 |
| **AirPassport 固件** | 设备本身（屏幕、按键、蓝牙） | 仓② `main/`、`components/`、`tests/` | 你手里那台设备 |

## 二、两个仓分别是什么

| | 仓① Life Logger | 仓② AirPassport 固件 |
|---|---|---|
| 本机位置 | `~/Documents/Codex/2026-09-02/referenced-chatgpt-conversation-this-is-an` | `~/.codex/.chatgpt-projects/g-p-6a5c9d774b188191b306d47e0cf644fa/hardware-v0.1/ai-passport-original` |
| 它是什么 | **你自己的应用**（纯本地起步） | **上游 FoloToy 官方固件的克隆**（`github.com/FoloToy/ai-passport`） |
| 云上备份 | 私有仓 `github.com/lwk-12345/life-logger` | 私有仓 `github.com/lwk-12345/ai-passport-firmware`（推送待修） |
| 能提 PR 给官方吗 | 没有上游，不需要 | 能，但**只提通用 bug 修复**（如按键判定），不提个人功能 |

**为什么是两个仓**：固件要能向上游提 PR，必须守上游那套规矩（不许改仓库规范文件、文档中英配对等）；这些规矩不该污染你自己的应用仓。硬合并要重写历史，风险大、收益小，**不做**。

## 三、记录规则（进度记在哪）

- **一个仓 = 一本台账**，就在该仓的 `.planning/` 下（已被 gitignore，不进仓）：
  - 仓①：`.planning/life-logger/`（`task_plan.md` / `findings.md` / `progress.md`）
  - 仓②：`.planning/ai-passport-20261001/`
- 同一本账里用**标记**区分模块：`[网页端]` `[手表]` `[接收器]` `[固件]`。
- **交付前自检**（在该仓根目录跑）：`bash .planning/tools/planning_check.sh --strict`
  → 有改动没记账、或记录里的版本号与产物对不上，就判不合格（固件仓还会直接拒绝烧录）。
- **跨仓改动**：在"真正被改动的那个仓"记账，另一个仓只记一行指针。

## 四、你平时只需要说这几句

| 你想干什么 | 直接说 |
|---|---|
| 看某个模块的进度 | "看台账" / "网页端/手表/接收器/固件 到哪了" |
| 让我干活 | 说模块名 + 要什么效果（例："网页端待办加上标签"） |
| 让我改完就上 | "改完直接上"（我会构建 + 烧录/替换，做完给你证据） |
| 想确认没漏记 | "三件套同步了吗"（我会跑自检并把真实输出贴给你） |

## 五、两条红线（重要）

1. **`.planning/` 被 gitignore** → `git clean -xdf` 会把台账一起删掉；重要节点请自行 tar 备份。
2. **两台设备的事实只能靠实测** → 设备里装的是哪一版、手表上是不是真跑起来了，必须你确认，不许从源码或构建号推断。
