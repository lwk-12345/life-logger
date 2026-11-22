# AI Passport 屏幕展示与数据同步规范

> 版本：`passport-card-v1`  
> 日期：2026-09-28  
> 适用硬件：AI Passport (ESP32-C3 + 240×280 彩屏 / LVGL 驱动)  
> 对应模块：`src/life_logger/passport_sync.py`、`src/life_logger/local_web.py`

---

## 1. 架构定位与目标

依据 `task_plan.md` 阶段 7 需求：
- **AI Passport** 定位为随身轻量信息展示与状态同步终端；
- **展示内容**：呈现 OpenClaw / Life Logger 整理后的待办事项、每日生活摘要（录音时长、记账金额、关键事件）与硬件连接/电量状态；
- **单一事实源原则**：遵循 `docs/openclaw-todo-unify.md`，OpenClaw 保持为逻辑单一事实源（SoT），AI Passport 不自建独立数据库，只做投影展示与轻量确认回写；
- **传输双通道支持**：支持经由局域网 Wi-Fi HTTP / JSON，或经由 BLE GATT Stream（受控于 MTU 185 限制）分包传输。

---

## 2. 硬件与 LVGL 屏幕约束

- **物理分辨率**：240 (宽) × 280 (高) 像素，彩色显示（RGB565）；
- **交互按键**：
  - **中键短按**：卡片翻页（下一页循环切换）；
  - **中键长按**：卡片操作触发（如将当前待办标记为“已完成 / 进行中”）；
- **屏幕分区建议**：
  - **顶部状态栏（高度 ~30px）**：显示当前页码索引 `(1/4)`、设备微标、电量与连接图标；
  - **主体卡片区（高度 ~210px）**：卡片核心信息区，字体推荐 14–16px，单行最大容纳 12–16 个中文字符；
  - **底部提示区（高度 ~40px）**：显示操作提示（如 `[中键] 翻页 | [长按] 完成`）。

---

## 3. 卡片数据契约 (Card Schema)

所有卡片均遵循统一基类 `PassportCard`，版本标号为 `passport-card-v1`。

### 3.1 待办任务卡片 (`TODO_CARD`)

展示当日待办或 OpenClaw 导入任务，支持按优先级（P0/P1/P2/P3）色彩高亮。

```json
{
  "card_id": "card_todo_oc_task_01",
  "card_type": "TODO_CARD",
  "title": "提交 Life Logger 周报",
  "updated_at": "2026-09-28T20:00:00+08:00",
  "task_key": "oc_task_01",
  "status": "pending",
  "priority": "P1",
  "due_time": "18:00",
  "project": "life-logger",
  "tags": ["work", "reporting"],
  "source_event_id": "evt_101",
  "metadata": {"source_system": "openclaw"}
}
```

- **优先级色标**：
  - `P0`：红色 `#FF4D4F`（紧急/立刻）
  - `P1`：橙色 `#FA8C16`（重要/优先）
  - `P2`：蓝色 `#1890FF`（常规待办）
  - `P3`：灰色 `#8C8C8C`（低优先/顺便）
- **状态流转**：`pending` ➔ `in_progress` ➔ `completed` / `dismissed` / `blocked`。
- **与 OpenClaw 桥接**：提供 `to_openclaw_task()` 与 `from_openclaw_task()` 双向无损映射。

### 3.2 每日生活摘要卡片 (`SUMMARY_CARD`)

整合当日录音时长、记账笔数与支出金额、关键生活/工作事件。

```json
{
  "card_id": "card_summary_2026-09-28",
  "card_type": "SUMMARY_CARD",
  "title": "今日概况 (09-28)",
  "updated_at": "2026-09-28T20:00:00+08:00",
  "date": "2026-09-28",
  "recording_duration_seconds": 7320.0,
  "recording_duration_formatted": "2小时2分",
  "expense_count": 3,
  "expense_total_cny": 128.50,
  "key_events": [
    {"time": "09:30", "title": "团队晨会", "category": "work_communication"},
    {"time": "12:15", "title": "午餐外卖", "category": "food"},
    {"time": "15:00", "title": "架构方案评审", "category": "work_communication"}
  ],
  "stats": {"routine": 2, "expense": 3, "work_communication": 4}
}
```

### 3.3 系统状态卡片 (`STATUS_CARD`)

呈现 AI Passport 硬件运行健康状态与连接信道参数。

```json
{
  "card_id": "card_status_AirPassport",
  "card_type": "STATUS_CARD",
  "title": "系统状态",
  "updated_at": "2026-09-28T20:00:00+08:00",
  "device_name": "AirPassport-C3",
  "battery_level": 85,
  "is_charging": false,
  "connection_status": "ready",
  "sync_time": "20:00:00",
  "ble_encrypted": true,
  "transport": "ble",
  "storage_free_kb": 2048
}
```

---

## 4. 传输协议与分包机制

针对 ESP32 BLE GATT MTU 限制（实测握手 MTU 为 185 字节），设计紧凑型二进制帧协议。

### 4.1 二进制帧结构 (15 字节头部 + 负载)

```text
+-------------------+----------------+------------------+------------------+------------------+-------------------+--------------------+
| Magic (4 Bytes)   | Flags (1 Byte) | Seq (2 Bytes BE) | Total (2 Byte BE)| Len (2 Bytes BE) | CRC32 (4 Bytes BE)| Payload (N Bytes)  |
| 0x41 0x50 0x43 0x31| 0x01 / 0x02    | 0x0000 .. 0xFFFF | 0x0001 .. 0xFFFF | 0x0000 .. 0xFFFF | Checksum          | Max 160 Bytes      |
+-------------------+----------------+------------------+------------------+------------------+-------------------+--------------------+
```

- **Magic**：固定为 ASCII `APC1`（AirPassport Card v1）；
- **Flags**：
  - `0x01`（`FLAG_IS_LAST_CHUNK`）：当前分片为该传输会话的末尾片；
  - `0x02`（`FLAG_IS_COMPRESSED`）：负载经过 zlib/deflate 压缩；
- **CRC32**：对 `Header[0..11] + Payload` 全量计算，防乱序损坏或空中误码；
- **分片大小**：默认 `max_chunk_size = 160`，加上 15 字节头部共 175 字节，严格位于 BLE MTU 185 安全阈值之内。

### 4.2 重组与完整性校验 (`PacketAssembler`)

- 支持乱序接收分片、重复分片自动去重；
- 提供 `missing_chunks()` 报告丢包序号；
- 全片到达后自动进行 CRC32 与 zlib 解压还原为完整 `PassportCardDeck`。

---

## 5. 翻页导航与离线缓存

### 5.1 翻页器 (`PassportPager`)
- 内部维护 `current_page` 指针与总页数；
- `next_page()`：模拟中键短按，到末页自动循环（或配置不循环）；
- `action_click()`：模拟中键长按，若当前为 `TodoCard`，自动就地切换 `pending` ⇄ `completed`，并更新卡组的 `version_hash`；若为其他卡片，返回刷新指令；
- `render_screen()`：直接输出包含页眉、主体 LVGL 字段、页脚提示的结构体。

### 5.2 离线分页缓存 (`OfflineCardCache`)
- 采用 `.tmp` 文件写入后原子性 `replace` 替换策略，杜绝掉电导致缓存损坏；
- 支持 `is_stale(max_age_seconds)` 判定缓存新鲜度；
- 卡组自带基于 SHA-256 的 `version_hash`，供客户端做差异拉取（若 Hash 未变，ESP32 屏幕无需刷新闪烁）。

---

## 6. Web API 接口

Life Logger 桌面端开放以下两个接口：

### 6.1 `GET /api/passport/cards`
- **入参（Query 参数）**：
  - `date`（可选）：指定日期 `YYYY-MM-DD`（默认今日）；
  - `format`（可选）：`json`（默认全量卡组）、`lvgl`（LVGL 预排版页组）、`packets`（十六进制分包流）；
- **响应示例**：返回完整 `PassportCardDeck` 或分包数据。

### 6.2 `POST /api/passport/cards/action`
- **入参**：
  ```json
  {
    "action": "toggle_todo",
    "task_key": "task_custom_1",
    "status": "completed"
  }
  ```
- **处理**：直接回写至 `JobStore.save_task_state()`，并返回最新状态。

---

## 7. 单元测试验证报告

全量单元测试位于 `tests/test_passport_sync.py`，覆盖以下 21 个专项：

1. `test_base_card_validation`: 基类卡片校验及异常阻断；
2. `test_todo_card_validation_and_methods`: 待办卡片各状态/优先级校验及与 OpenClaw 契约互转；
3. `test_summary_card_validation_and_lvgl`: 每日摘要卡片时长格式化、支出汇总与 LVGL 映射；
4. `test_status_card_validation_and_lvgl`: 硬件状态卡片电量/连接/加密标志校验；
5. `test_polymorphic_card_serialization`: 多态卡片反序列化测试；
6. `test_card_deck_operations_and_version_hash`: 卡组管理与 SHA-256 差异哈希一致性；
7. `test_extract_passport_cards_normal`: 正常数据库提取（时长、记账、关键事件、待办自动排序）；
8. `test_extract_passport_cards_with_openclaw_tasks`: OpenClaw 任务高优先级混入；
9. `test_extract_passport_cards_empty_boundary`: 空数据库/空结果零状态安全边界；
10. `test_extract_passport_cards_corrupted_files`: 损坏 JSON 容错与优雅降级；
11. `test_pack_and_unpack_packet_roundtrip`: 二进制分包打包与解包完整往返；
12. `test_unpack_packet_corruptions`: 截断、错误 Magic、CRC32 错误侦测拦截；
13. `test_chunk_and_assemble_deck_stream`: MTU 185 边界压缩分包与整组重组还原；
14. `test_packet_assembler_out_of_order_and_duplicates`: 乱序分片到达与重复包抵抗测试；
15. `test_deck_to_packets_hex`: 十六进制数据流导出验证；
16. `test_passport_pager_navigation`: 中键翻页循环、后退、跳转及状态查询；
17. `test_passport_pager_action_click_toggles_todo`: 中键长按切换待办状态与哈希刷新；
18. `test_passport_pager_empty_deck`: 空卡组翻页器保护机制；
19. `test_offline_card_cache_lifecycle`: 离线磁盘原子缓存、加载、防损坏与过期判断；
20. `test_local_web_api_passport_cards`: 完整 HTTP Web API 集成（JSON、LVGL、Packets 及回写 Action）；
21. `test_utility_formatters`: 时长与货币紧凑格式化工具测试。

测试结果：**21 passed, 100% 通过**，全量关联回归测试（包含 `passport_stream`、`daily_brief`、`local_web`）总计 **100 passed**。
