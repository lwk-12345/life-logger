# AI Passport × 待办闭环 设计文档

> 版本：2026-10-12
> 状态：设计定稿，待实施
> 关系：`docs/ai-passport-screen-sync.md`（2026-09-28 `passport-card-v1`，**部分未落地**）、`docs/openclaw-todo-unify.md`（**设计已落定，尚未开始跨系统大改**）

## 0. 一句话

**一份数据（OpenClaw），三处只读投影（看板 / 徽章 / 微信），两个动作入口（嘴 / 微信）。**

## 1. 数据架构

### 1.1 目标形态

```
【写入口：只有一个】
语音（Watch / 徽章）→ Life Logger 提取 ──┐
人（微信 / 语音改）────────────────────┤
                                       ↓
                               ★ OpenClaw ★
                             【唯一事实源】
                                       │
           ┌───────────────────────────┼───────────────────────────┐
           ↓                           ↓                           ↓
    Life Logger 看板            AI Passport 徽章              微信汇报
     （只读投影）                 （只读投影，4 条）          （只读投影，不限量）
```

### 1.2 现状（实测 2026-10-12）

| 系统 | 现在实际读什么 | 证据 |
|---|---|---|
| Life Logger 看板 | 自己的 `task-states.json` + `reminders.json` | `output/local-web/` |
| AI Passport 徽章 | 自己的 `/api/reminders` | `desktop/AirPassportReceiver/Sources/AirPassportReceiver/PassportTodoSync.swift:526` |
| 微信 | OpenClaw | ✅ 已经对 |

**三条线互不相连 → 你现在三个地方看到的，是三份不同的数据。**

### 1.3 关键结论：**回传通道不需要做**

只要三个出口都只读 OpenClaw，「同步」这个问题自动消失。
现在之所以需要"回传"，是因为 Life Logger 自己存了一份、徽章又读那一份，两边必然对不上。

---

## 2. 三个语音口令

| 口令 | 行为 | 现状 |
|---|---|---|
| **添加待办 XXX** | 新建 todo → 进 OpenClaw | ✅ **已有**（`src/life_logger/extract.py:634` 强标记） |
| **删除待办 XXX** | 标记 dismissed | ❌ 要新增 |
| **待办打卡 XXX** | 标记 completed | ❌ 要新增 |

### 2.1 为什么用明确口令

- **零歧义**：不用猜、不用筛选、不用再确认
- **口令本身就是确认动作** → 免掉网页点确认那一步
- 与现有 `voice_lexicon.py` 的口令体系一致（`MEMO_TRIGGER_TERMS` / `QUERY_TRIGGER_TERMS`）

### 2.2 已有的轮子（不必重写）

| 能力 | 位置 | 状态 |
|---|---|---|
| 改本地 task 状态 | `local_reminders.set_status()` / `local_web.save_task_state()` | ✅ 网页在用 |
| **改 OpenClaw 状态** | `openclaw_bridge.py:261 patch_task()` | ⚠️ **写好了，但全仓库零调用** |

**注意**：口令词当前散在 7 个文件里（`voice_lexicon` / `contextual_extraction` / `extract` / `voice_triggers` / `watch_formal_asr` / `daily_cognitive_session` / `natural_correction`），且互相不一致。**新增口令前必须先统一，否则会变成第 8 处。**

---

## 3. 匹配：「XXX」怎么找到对应那条

### 3.1 核心认知

**没有任何方法能 100% 准。** 所以设计目标不是"保证准"，而是**"不准的时候怎么便宜地纠正"**。

### 3.2 三层策略

| 场景 | 你怎么说 | 系统做什么 |
|---|---|---|
| 手边有徽章 | 「第 2 条打卡」 | **按编号精确执行** |
| 手边没徽章 | 「VPN 那个搞定了」 | **模糊匹配最像的**（标题 + 关键词 + 时间衰减） |
| **任何情况** | — | **执行完往微信报一句**：「已打卡：检查 VPN 设置（对吗？）」 |

**第三层是重点**：它把"匹配必须准"的压力卸掉了 —— 错了在微信说一句就能改回来，而且你本来就每天收三次汇报。

### 3.3 编号的坑（必须避开）

| 做法 | 问题 |
|---|---|
| 用「当前第几条」当编号 | ❌ **会漂**：第 1 条完成后，原第 2 条变第 1，你刚记的号作废 |
| 用稳定身份（创建时分配、完成不重排） | ✅ 但编号会耗尽 |

**定案**：**编号只用于「当次看着徽章说」** —— 看完作废，下次重新看。不看徽章时用名字说。

### 3.4 撞车时怎么办

两条都含「VPN」时：
1. 若其中一条**更近**（创建时间、最近出现在徽章上）→ 优先它
2. 若**同样近** → **执行最匹配的那条，并在微信里列出另一条**，让你决定要不要改

---

## 4. 待办 vs 习惯：必须分开

### 4.1 为什么

徽章只有 **4 槽**（固件 `main/passport_todo.h:30` `PASSPORT_TODO_MAX_ITEMS = 4`）。
习惯类**做完不消失、明天还会出现** → **永久占位** → 把真正急的待办挤出去。

### 4.2 分工

| | 一次性待办 | 习惯打卡 |
|---|---|---|
| 回答什么 | 这事**做完了吗** | 今天**做了没** |
| 时间尺度 | 有截止，做完消失 | 每天重复 |
| 不做会怎样 | **误事** | 少坚持一天 |
| **徽章归宿** | **第 2 屏（4 槽）** | **第 3 屏（新）** |
| 微信汇报 | ✅ | ✅ |

---

## 5. 徽章 UI 改动

### 5.1 页面重排

```
现在（3 屏）                改成（3 屏）
 1  ID BADGE                1  ID BADGE
 2  LIFE LOGGER 待办        2  待办（4 槽）
 3  LINK 连接状态           3  习惯打卡
                            + 状态栏加「LL 连接绿点」
```

### 5.2 连接状态 → 右上角绿点

**依据**：现在有**两个独立**的连接状态，混在一条信号条里表达不了。

| 概念 | 现在的表示 | 数据来源 |
|---|---|---|
| **BLE 连接**（设备 ↔ Mac 蓝牙） | 现有信号条（3 格） | `main/status_bar.c:3` `status_bar_signal_bars(bool connected, int rssi)` |
| **Life Logger 服务连接** | 第 3 屏那句「已连接 Life Logger」 | `main/identity_home.c:611` `status.state == PASSPORT_STATUS_CONNECTED`（来自 Mac 的 `0x03` 状态帧） |

**所以「蓝牙连着、但 Life Logger 服务挂了」这种情况，用户现在必须翻到第 3 屏才知道。**

**改法**（状态栏布局）：

```
┌──────────────────────────────┐
│ 10:24       ●  ▂▄▆    🔋85%  │
│             ↑   ↑            │
│        LL 连接   BLE 信号     │
└──────────────────────────────┘
```

| 元素 | 表示 | 颜色 |
|---|---|---|
| **绿点**（新增） | Life Logger 服务连接 | 绿=通 / 红=断 / 灰=未知 |
| 信号条（3 格） | BLE 通不通 + 信号强度 | 现有逻辑不动 |

**位置**：信号条画在 `x = 176 + i*5`（`main/identity_home.c:200`），屏宽 240 → **左边整块可用**。

### 5.3 第 3 屏：习惯打卡

- 复用第 2 屏的 4 槽卡片布局（`main/passport_todo.c` 的 slot 机制）
- 每格显示：**习惯名 + 今日是否打卡 + 连续天数**
- 数据来源：**OpenClaw**（见 §1）
- 中文文案注意字形（默认 Montserrat 无中文字形）

---

## 6. 改动清单

### 6.1 Mac 端（Life Logger 仓库）

| # | 改什么 | 位置 |
|---|---|---|
| 1 | 统一口令表 + 新增「删除待办 / 待办打卡」 | `src/life_logger/voice_lexicon.py` + `extract.py:634` |
| 2 | 接上 `patch_task` | `openclaw_bridge.py:261`（已有函数，加调用） |
| 3 | 徽章数据源换成 OpenClaw | `desktop/AirPassportReceiver/Sources/AirPassportReceiver/PassportTodoSync.swift:526` |
| 4 | 新增投影接口（OpenClaw todo → 徽章卡片格式） | `src/life_logger/local_web.py` |
| 5 | 补幂等键（voice_query 通道现在没有 → 重复污染） | `src/life_logger/contextual_extraction.py:801` |
| 6 | 执行后往微信报一句 | `src/life_logger/openclaw_bridge.py` |

### 6.2 固件端（AirPassport 仓库：`~/.codex/.chatgpt-projects/g-p-6a5c9d774b188191b306d47e0cf644fa/hardware-v0.1/ai-passport-original`）

| # | 改什么 | 位置 |
|---|---|---|
| 1 | 第 3 屏 LINK → 习惯打卡 | `main/identity_home.c:211`、`make_link_page`、`refresh_link_page:605` |
| 2 | 状态栏加 LL 连接绿点 | `main/identity_home.c:200` 附近 + `main/status_bar.c` |
| 3 | 新增习惯帧协议（复用 `0x02` 待办帧格式或新增 `0x04`） | `main/passport_todo.c` + `main/passport_ptt_ble.c` |
| 4 | 编译 + 烧录 | `./tools/validate.sh --firmware` |

**现有帧协议**（Mac 端 `PassportTodoSync.swift:16-37`）：
- 4 字节 = 校时
- `[0x02][index][total][status][title_len][title]` = 待办帧
- `[0x03][state][count][age_lo][age_hi]` = 状态帧
- **习惯帧建议用 `0x04`**，格式同待办帧 + 打卡标志位

---

## 7. 实施顺序

| 阶段 | 内容 | 依赖 | 量 |
|---|---|---|---|
| **P1** | 三个口令 + `patch_task` 接通 | Mac 端，独立 | 小 |
| **P2** | 徽章数据源换 OpenClaw | 依赖 P1 的投影接口 | 中 |
| **P3** | 固件 UI（绿点 + 习惯屏 + 习惯帧） | Mac 端协议先定 | 中 |
| **P4** | 幂等键 + 微信兜底汇报 | 独立 | 小 |

**P1 + P2 做完，核心诉求就满足了**：徽章显示你真正在管的待办，嘴能归档。

---

## 8. 待确认（设计未定案的部分）

1. **OpenClaw 有没有「习惯 / 周期任务」概念？** 有则复用，无则需新设计存储
2. **编号在徽章上怎么显示**（4 槽 × 20 字已经很紧，加编号要挤掉字）
3. **微信兜底汇报**：搭现有的每天三次，还是每次执行都单独报一句
4. **固件仓有 4 个未提交改动**（`main/main.c`、`sdkconfig.defaults`、2 个测试）—— 交接前需先处理

---

## 附：参考

- `passport_sync.py`（卡片投影）、`openclaw_bridge.py`（桥接）、`passport_todo.c`（固件槽位）
- 2026-10-12 实测数据：OpenClaw 302 任务（238 条来自 Life Logger）；徽章 15 秒一轮推送；今天实际推送 0 条（严格模式过滤后为空）
