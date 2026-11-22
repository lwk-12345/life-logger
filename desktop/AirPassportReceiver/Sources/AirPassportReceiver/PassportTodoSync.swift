import Foundation
import Darwin

// ==============================================================================
// 今日待办 → BLE 下发（AirPassport 设备只做「显示终端」）
// ==============================================================================
//
// 一、协议（root 已定稿，固件侧按同一份实现，必须严格一致）
//
//   复用**已有的 capabilities 特征**（PassportBLEBridge.capabilitiesUUID，
//   01000054-5450-42A1-6F4E-2D9102107A8B），按**载荷长度 + 首字节**区分三类语义：
//
//   ┌ 长度 == 4 ────────────────────────────────────────────────────────────┐
//   │ 校时：小端 uint32 Unix 秒（现有功能，真机已验证，本文件不参与、不修改）  │
//   └───────────────────────────────────────────────────────────────────────┘
//   ┌ 长度 >= 5 且首字节 == 0x02 ───────────────────────────────────────────┐
//   │ 待办帧：[0x02][index][total][status][title_len][title utf8...]        │
//   │   index     0 起，逐条递增                                            │
//   │   total     总条数；**0 = 清空**（设备显示「暂无待办」）               │
//   │   status    0=待办 1=进行中 2=已完成                                  │
//   │   title_len 标题 UTF-8 字节数，<= 60，须落在字符边界上；截断则末尾补 …│
//   └───────────────────────────────────────────────────────────────────────┘
//   ┌ 长度 == 5 且首字节 == 0x03 ───────────────────────────────────────────┐
//   │ 连接状态帧（设备第 3 屏「Life Logger 连接状态」）：                      │
//   │            [0x03][state][count][age_lo][age_hi]                       │
//   │   state  1 = 已连接 / 拉取成功；0 = 未连接 / 拉取失败                   │
//   │          （401 鉴权失败、连接被拒、超时等一律落到 0）                    │
//   │   count  本次已知的今日待办**真实条数**：映射后、丢掉 dismissed/已完成  │
//   │          之后的条数。字段 1 字节 → 0–255，>255 **饱和**写 255（不回绕）│
//   │          并打一条日志。⚠️ 它**不是**设备能显示的行数：设备只有 4 个槽位，│
//   │          count 允许 > 4（用户据此知道"还有几条没显示"，2026-10-02 真机） │
//   │   age    距**上次成功同步**的秒数，小端 uint16；>65535 封顶写 65535     │
//   │   协议里**没有** detail 字段：失败原因不塞进帧，只打 app 日志           │
//   └───────────────────────────────────────────────────────────────────────┘
//
//   三类载荷靠长度天然区分：校时固定 4 字节，待办帧最短 5 字节（空标题时正好 5 字节）
//   且首字节 0x02，状态帧固定 5 字节且首字节 0x03 —— 不存在歧义。
//   写入类型统一 `.withResponse`，与校时保持一致。
//
//   设备最多显示 4 条 → Mac 端只发前 4 条，待办帧的 `total` 写**实际发送条数**（≤ 4）。
//   ⚠️ 别把这两件事搞混（2026-10-02 真机暴露的体验缺口）：**状态帧的 count 写真实总数**，
//      早先它被一并钳到 4，结果第 3 屏永远显示「今日待办 4 条」，用户根本看不出还有条目没显示。
//   逐条写、条与条之间 ~60ms，避免写队列拥塞；每条失败只打日志，不做聚合回执、不重试。
//
// 二、为什么只有「展示」，没有「勾选回传」
//
//   录音上传已改由 Apple Watch 负责，Passport 的定位收窄为**只读显示终端**：
//   Mac → 设备 单向下发今日待办即可。设备侧不需要把「用户点了完成」写回 Mac，
//   因此本文件里**没有**任何反向（设备 → Mac）的待办状态通道，也不该有人去加：
//   一旦加了，就必须处理离线冲突、重复回传、鉴权等问题，而收益只是省一次手机/电脑上的点击。
//
//   同理，本通道与音频/PTT/按键分发**完全独立**：拉取失败、超时、服务没开、
//   令牌失效，全部只降级为「发一条清空帧 + 打日志」，绝不抛出、绝不阻塞
//   CoreBluetooth 回调队列（CBCentralManager 用的是主队列）。
//
// 三、数据源
//
//   Life Logger 本地服务 `GET /api/reminders`（root 已拍板；早先的 /api/tasks 是任务状态
//   台账，实测映射后 0 条，故弃用）。真实返回结构（2026-10-02 实测，见 PassportTodoMapper 注释）：
//     {"backend":"local", "reminders":[{...}]}
//
// 四、为什么「Life Logger 通不通」只能由 Mac 转达给设备
//
//   设备只有 BLE 一条链路连 Mac：**没有 WiFi、不知道 Life Logger 的地址、也没有访问令牌**
//   （地址要遍历 Mac 的网卡才知道，令牌躺在 Mac 的 output/local-web/secrets.json 里）。
//   所以设备自己既判断不了 Life Logger 是死是活，也数不出今天有几条待办，更不知道上次
//   同步成功是什么时候 —— 这些信息**只在 Mac 这一侧存在**。
//
//   于是 Mac 在每次拉取结束（成功或失败）时，顺手把结论压成 5 字节状态帧写下去，
//   设备的第 3 屏才有东西可显示。这是「转达」，不是「设备自己探测」：
//   状态帧只在 Mac 拉取完的那一刻生成，设备永远不会主动来问。
//   断连也不补发 —— 重连后 startTodoRefresh 会立刻补一帧（设备期间显示旧状态）。
// ==============================================================================

/// 待办状态字节（协议定义）
public enum PassportTodoStatus: UInt8, Equatable, Sendable {
    /// 0 = 待办
    case pending = 0
    /// 1 = 进行中
    case inProgress = 1
    /// 2 = 已完成
    case completed = 2
}

/// 单条待办（Mac 侧模型，只保留设备要显示的两件事：标题 + 状态）
public struct PassportTodoItem: Equatable, Sendable {
    public let title: String
    public let status: PassportTodoStatus

    public init(title: String, status: PassportTodoStatus) {
        self.title = title
        self.status = status
    }
}

/// 待办帧编解码（纯函数，便于单测）
public enum PassportTodoProtocol {
    /// 待办帧魔数：第一个字节固定 0x02（也是与 4 字节校时帧区分的依据之一）
    public static let frameMagic: UInt8 = 0x02
    /// 待办帧最短长度：0x02 + index + total + status + title_len（空标题）= 5
    public static let minFrameLength = 5
    /// 标题上限（字节，按 UTF-8 字符边界截断）
    public static let maxTitleBytes = 60
    /// 设备主屏第二页最多显示 4 条 → Mac 端只发前 4 条**待办帧**。
    /// ⚠️ 只限制待办帧：状态帧的 count 是真实总数（可 > 4），见 `PassportConnectionStatusProtocol`。
    public static let maxItems = 4
    /// 逐条写入间隔：约 60ms，避免写队列拥塞
    public static let interFrameDelay: TimeInterval = 0.060
    /// 截断标记：U+2026（HORIZONTAL ELLIPSIS），UTF-8 占 3 字节。
    /// 只在**确实发生截断**时补在标题末尾，让用户一眼看出"这条没显示完"，
    /// 而不是把半句话当成完整标题读下去。
    public static let truncationEllipsis = "…"
    /// `truncationEllipsis` 的 UTF-8 字节数（`E2 80 A6` = 3）。
    public static let truncationEllipsisBytes = 3

    /// 编码一条待办帧：`[0x02][index][total][status][title_len][title utf8...]`
    public static func encodeFrame(index: Int,
                                   total: Int,
                                   status: PassportTodoStatus,
                                   title: String) -> Data {
        let titleBytes = Data(truncateTitle(title).utf8)
        var data = Data(capacity: minFrameLength + titleBytes.count)
        data.append(frameMagic)
        data.append(UInt8(clamping: index))
        data.append(UInt8(clamping: total))
        data.append(status.rawValue)
        data.append(UInt8(clamping: titleBytes.count))
        data.append(titleBytes)
        return data
    }

    /// 清空帧：`[0x02][0][0][0][0]`（total = 0 → 设备显示「暂无待办」）。
    /// 长度 5，满足「>= 5 且首字节 0x02」，不会被设备当成 4 字节校时帧。
    public static func clearFrame() -> Data {
        Data([frameMagic, 0x00, 0x00, PassportTodoStatus.pending.rawValue, 0x00])
    }

    /// 按 **UTF-8 字符边界** 截断标题，绝不切碎汉字/emoji；**一旦截断就补 `…`**。
    ///
    /// 例：60 字节刚好放得下 20 个汉字（3 字节/字）；第 21 个字会被整字丢弃，
    /// 而不是留下 3 字节里的 0 字节或半个码点（那会让设备端解码出乱码）。
    ///
    /// 为什么要补省略号（2026-10-02 真机反馈）：
    /// 设备侧 `title_len` 上限 60 字节，20 个汉字就是天花板。此前"硬截"的结果是
    /// 屏幕上出现半句话（例如「你要记得，就是每个方向的都要严格按照那个」），
    /// 用户读到的是一个**看起来完整、实际被砍掉后半截**的句子，既不知道还有内容，
    /// 也读不通。补一个 `…` 后，用户至少知道"这条没显示完"。
    ///
    /// 规则（顺序固定）：
    /// 1. 先按整字贪心装满 `maxBytes`（绝不切碎字符）；
    /// 2. **没发生截断 → 原样返回，不补 `…`**（标题恰好等于上限也一样）；
    /// 3. 发生截断 → 末尾补 `…`；放不下就整字回退，直到 `已用字节 + 3 <= maxBytes`；
    /// 4. 极端情况（上限连「一个字符 + `…`」都放不下）→ 按上限整字截断、不补 `…`，
    ///    并打日志说明（正常 maxBytes = 60 时走不到这条分支）。
    ///
    /// 注意：`…` 是**标题内容的一部分**，会被算进 `title_len`，协议字段与字节布局没变。
    public static func truncateTitle(_ title: String, maxBytes: Int = maxTitleBytes) -> String {
        guard maxBytes > 0 else { return "" }

        // 1) 整字贪心装满：这一步保证永远落在字符边界上（不会出现半个汉字）。
        var kept: [Character] = []
        var keptBytes = 0
        var didTruncate = false
        for character in title {
            let size = String(character).utf8.count
            if keptBytes + size > maxBytes {
                didTruncate = true
                break
            }
            kept.append(character)
            keptBytes += size
        }

        // 2) 没截断 → 原样返回：标题恰好等于上限不是"被截断"，不补省略号。
        guard didTruncate else { return String(kept) }

        // 3) 发生了截断 → 末尾必须有省略号；给它腾位置（整字回退，不做半字节切割）。
        let boundaryTrimmed = kept
        while keptBytes + truncationEllipsisBytes > maxBytes, !kept.isEmpty {
            keptBytes -= String(kept.removeLast()).utf8.count
        }

        // 4) 极端：连「一个字符 + 省略号」都放不下 → 按上限整字截断，不补省略号。
        guard !kept.isEmpty else {
            logLine("[AirPassport Todo] 标题上限过小（maxBytes=\(maxBytes) < 单个字符字节数 + 省略号 \(truncationEllipsisBytes) 字节），按上限整字截断、不补省略号")
            return String(boundaryTrimmed)
        }

        // 5) 正文本身已经以 `…` 收尾时不叠加第二个：上游待办标题自带省略号很常见
        //    （实测「我只看它的一个形态…」），叠加会在屏幕上出现「……」，反而更怪。
        //    此时末尾仍是 `…`，截断信号没有丢。
        let body = String(kept)
        return body.hasSuffix(truncationEllipsis) ? body : body + truncationEllipsis
    }

    /// 组装**一轮完整下发**的写入计划：清空帧 + N 条待办帧 + 最后 1 条连接状态帧。
    ///
    /// 为什么状态帧放最后：设备拿到待办帧才会重建列表。若状态帧先到，会出现
    /// 「状态说 3 条、列表还空着」的短暂打架画面；放最后则列表先就位、数字随后对齐。
    /// 顺带用这个纯函数把「写入顺序 + 帧内容」变成可单测的东西（见 PassportTodoSyncTests）。
    ///
    /// 条数上界：待办帧按 `maxItems`（4）截断，`total` 写实际发送条数；
    /// 状态帧的 count 由调用方给（真实总数，见 `PassportConnectionStatus`），这里不参与截断。
    public static func pushPlan(items: [PassportTodoItem],
                                status: PassportConnectionStatus) -> PassportTodoPushPlan {
        let limited = Array(items.prefix(maxItems))
        var todoFrames: [Data] = [clearFrame()]
        for (index, item) in limited.enumerated() {
            todoFrames.append(encodeFrame(index: index,
                                          total: limited.count,
                                          status: item.status,
                                          title: item.title))
        }
        return PassportTodoPushPlan(todoFrames: todoFrames, statusFrame: status.frame)
    }
}

// ==============================================================================
// 连接状态帧（协议第三类载荷，设备第 3 屏数据源）
// ==============================================================================

/// 状态字节：设备第 3 屏只区分「通 / 不通」两态。
public enum PassportConnectionState: UInt8, Equatable, Sendable {
    /// 0 = 未连接 / 拉取失败（401、连接被拒、超时、JSON 解不动……一律归到这里）
    case disconnected = 0
    /// 1 = 已连接 / 拉取成功（拿到了一份可用的今日待办）
    case connected = 1
}

/// 一次「Life Logger 连接状态」快照。
///
/// 注意 count 的语义：它是**本次已知的真实待办条数**（映射后、丢掉 dismissed/已完成之后），
/// **不是**设备能显示的行数 —— 设备只有 4 个槽位，8 条待办时 count = 8。
/// - 拉取成功 → 就是这一轮映射出来的条数（可以 > 4）；
/// - 拉取失败 → 沿用上次成功时的条数（从没成功过则是 0）。
///
/// 之所以失败时保留旧条数而不是清零：设备第 3 屏要回答「现在通不通、有几条在等着」，
/// 网络抖一下不代表那 3 条待办不存在；清零会让屏幕上的数字无缘无故跳成 0。
public struct PassportConnectionStatus: Equatable, Sendable {
    public let state: PassportConnectionState
    public let count: Int
    public let ageSeconds: Int

    public init(state: PassportConnectionState, count: Int, ageSeconds: Int) {
        self.state = state
        self.count = count
        self.ageSeconds = ageSeconds
    }

    /// 编码为 5 字节状态帧 `[0x03][state][count][age_lo][age_hi]`（越界值在此处收敛）。
    public var frame: Data {
        PassportConnectionStatusProtocol.encodeFrame(state: state,
                                                     count: count,
                                                     ageSeconds: ageSeconds)
    }
}

/// 连接状态帧编解码（纯函数，便于单测）。
public enum PassportConnectionStatusProtocol {
    /// 状态帧魔数：第一个字节固定 0x03（也是与 4 字节校时帧、0x02 待办帧区分的依据）
    public static let frameMagic: UInt8 = 0x03
    /// 状态帧固定长度：0x03 + state + count + age_lo + age_hi = 5
    public static let frameLength = 5
    /// 条数字段（1 字节）的上限：255。超过就**饱和**写 255（绝不回绕），见 `saturateCount`。
    ///
    /// ⚠️ 这**不是**设备能显示的条数：设备只有 `PassportTodoProtocol.maxItems`（4）个槽位，
    /// 而 count 是「今日一共有几条待办」的真实总数，允许大于 4（2026-10-02 真机反馈：
    /// 早先误把它钳到 4，第 3 屏永远显示「4 条」，用户看不出还有条目没显示）。
    public static let maxCount = 255
    /// age 上限：uint16，超过就封顶（协议要求 >65535 写 65535）
    public static let maxAgeSeconds = 65535

    /// 编码一帧：`[0x03][state][count][age_lo][age_hi]`，age 小端 uint16。
    public static func encodeFrame(state: PassportConnectionState,
                                   count: Int,
                                   ageSeconds: Int) -> Data {
        let saturatedCount = saturateCount(count)
        let clampedAge = clampAge(ageSeconds)
        return Data([
            frameMagic,
            state.rawValue,
            UInt8(saturatedCount),
            UInt8(clampedAge & 0xFF),
            UInt8((clampedAge >> 8) & 0xFF)
        ])
    }

    /// 条数**饱和**到 0…255：>255 写 255 并打日志；负数按 0（帧里没有负数语义）。
    ///
    /// 为什么是饱和而不是回绕：字段只有 1 字节，`UInt8(300)` 会直接崩，而 `count & 0xFF`
    /// 会把 300 条悄悄写成 44 条 —— 一个"看起来合理但完全错误"的数字比一个封顶的数字更糟。
    /// 为什么不再钳到 4：设备只有 4 个槽位，但用户需要知道"今天还有几条没显示"。
    public static func saturateCount(_ count: Int) -> Int {
        let nonNegative = max(0, count)
        guard nonNegative > maxCount else { return nonNegative }
        logLine("[AirPassport Todo] 今日待办条数 \(nonNegative) 超过状态帧 count 字段上限 \(maxCount)（1 字节），按饱和写 \(maxCount)")
        return maxCount
    }

    /// age 收敛到 0…65535：>65535 封顶 65535，负数按 0（帧里没有负数语义）。
    public static func clampAge(_ ageSeconds: Int) -> Int {
        min(max(0, ageSeconds), maxAgeSeconds)
    }

    /// 距上次成功同步的秒数（封顶）——"多久前同步过"的唯一算法，桥接侧与单测共用。
    ///
    /// `lastSuccess == nil`（本进程内从没成功过）返回**封顶值 65535**：协议没有
    /// "从未同步"这一取值，65535（≈18.2 小时）是唯一合法的"很久以前/不知道"表达。
    /// ⚠️ 需固件侧对齐：设备若想显示「从未同步」，建议以 `age == 65535` 作哨兵判断。
    /// 时钟回拨/未来时间戳一律按 0 处理（不写负数）。
    public static func ageSeconds(since lastSuccess: Date?, now: Date = Date()) -> Int {
        guard let lastSuccess else { return maxAgeSeconds }
        return clampAge(Int(now.timeIntervalSince(lastSuccess)))
    }
}

/// 一轮下发的写入计划：待办帧与状态帧分开，因为两者在回执 FIFO 里是不同的写入类型
/// （见 PassportBLEBridge.CapabilityWriteKind：状态帧失败**不能**被当成校时失败去重试）。
public struct PassportTodoPushPlan: Equatable, Sendable {
    /// 1 条清空帧 + N 条待办帧，全部按 `.todoFrame` 记账
    public let todoFrames: [Data]
    /// 最后单独写的那一条状态帧，按 `.connectionStatus` 记账
    public let statusFrame: Data

    public init(todoFrames: [Data], statusFrame: Data) {
        self.todoFrames = todoFrames
        self.statusFrame = statusFrame
    }

    /// 实际写入顺序（待办在前、状态在最后）
    public var allFrames: [Data] { todoFrames + [statusFrame] }
}

/// `/api/reminders` → `[PassportTodoItem]` 的字段映射
public enum PassportTodoMapper {

    /// 解析 Life Logger `GET /api/reminders` 的真实返回，映射为待下发列表。
    ///
    /// 数据源变更（2026-10-02，root 拍板）：先前用的 `/api/tasks` 是**任务状态台账**，
    /// 实测只返回 1 条 `dismissed` → 映射结果 0 条 → 设备永远显示「暂无待办」。
    /// Web 端首页那份「未完成待办」清单实际来自 `/api/reminders`，实测 6 条里 4 条 `pending`，
    /// 因此改用本数据源。协议与 BLE 侧完全不受影响。
    ///
    /// 真实结构（2026-10-02 在本机 `172.20.10.3:8765` 实测，curl 原文节选，标题已截断）：
    /// ```json
    /// {
    ///   "backend": "local",
    ///   "reminders": [
    ///     {
    ///       "task_key": "evt_21926803048b",
    ///       "title": "我只看它的一个形态，谁更便携…",
    ///       "event_id": "evt_21926803048b",
    ///       "job_id": "job-20260921-232252-9007aa8c",
    ///       "artifact_path": "files/fc22.../result/2026-09-21/events.json",
    ///       "backend": "local",
    ///       "source_status": "pending",
    ///       "updated_at": "2026-10-02T00:47:39.572886+08:00",
    ///       "created_at": "2026-09-22T20:38:09.066083+08:00",
    ///       "remind_at": null,
    ///       "status": "unscheduled"
    ///     },
    ///     { "…": "…", "source_status": "dismissed", "status": "dismissed", "…": "…" }
    ///   ]
    /// }
    /// ```
    ///
    /// **两个状态字段的区别（映射依据，来自 `local_reminders.py::sync_tasks`）：**
    /// - `source_status` = **上游待办本身的状态**，取值域与 `/api/tasks` 的 `status` 完全一致：
    ///   `pending` / `in_progress` / `completed` / `dismissed` / `blocked`。
    ///   → **协议状态字节取它**，因为设备要显示的是「待办做没做」，不是「提醒排没排」。
    /// - `status` = **提醒自己的状态**：`unscheduled` / `scheduled` / `sent` /
    ///   `completed` / `dismissed`。当上游已完成/已忽略时它会被同步成同名值。
    ///   → 只用来做过滤，不当状态字节用（`scheduled`/`sent` 表示"提醒已排/已发"，
    ///     跟待办做没做完无关，仍应显示为待办）。
    ///
    /// 字段映射：
    /// - 标题  → `title`（空标题的条目直接跳过）
    /// - 状态  → `source_status`，见 `protocolStatus(from:)`
    /// - 顺序  → 沿用服务端顺序（`local_reminders.py::list` 已排好：未完成的在前、
    ///           已排提醒时间的在前、再按 `updated_at`）
    /// - 条数  → **全部**映射后的条目（这里**不再**截到 4 条）：截断是「帧层」的职责 ——
    ///           `pushPlan` 只发前 `maxItems`（4）条待办帧，而状态帧的 count 需要真实总数。
    ///           早先在这里 `break`，导致 count 天生 ≤ 4，第 3 屏永远显示「4 条」（2026-10-02 真机）。
    ///
    /// 过滤（只取「未完成/待办」）：
    /// - `source_status == dismissed` 或 `status == dismissed` → 跳过（用户已忽略）
    /// - 任一侧为 `completed` → 跳过（已完成不上屏，维持"只取未完成"的既有判断）
    ///
    /// 因此当前**不会**下发 status = 2（已完成）的条目。若日后想在设备上顺带看到
    /// 「今天已完成 N 条」，去掉下面两处 `.completed` 判断即可，协议本身已支持 status=2。
    public static func items(fromRemindersResponse data: Data) throws -> [PassportTodoItem] {
        let response = try JSONDecoder().decode(RemindersResponse.self, from: data)
        let records = response.reminders ?? []

        // 严格模式：设备只显示「人确认过的」待办。
        //
        // `/api/reminders` 里绝大多数条目是管线自动抽取的语音碎片（例如半句话
        // 「我记得那个小米的遥控器就是」），把它们推给徽章等于让用户读垃圾。
        // 判据只用两个字段，取真值、不猜：
        //   * `review_status == "confirmed"` —— 人在网页上复核过
        //   * `source_type == "manual"`      —— 人手工添加的条目
        //
        // 缺字段的语义（服务端未升级时会发生）：按**未确认**处理，即全部丢弃并
        // 明确告警。宁可设备显示「暂无待办」，也不显示未确认的候选。
        let serverSupportsReview = records.contains {
            $0.review_status != nil || $0.source_type != nil
        }
        if !serverSupportsReview && !records.isEmpty {
            NSLog("[PassportTodo] 服务端未提供 review_status/source_type，"
                  + "按严格模式处理：丢弃全部 %d 条候选（设备将显示空列表）",
                  records.count)
        }

        var items: [PassportTodoItem] = []
        var skippedUnconfirmed = 0
        for record in records {
            let reviewStatus = record.review_status?
                .trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            let sourceType = record.source_type?
                .trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            let isConfirmed = reviewStatus == "confirmed" || sourceType == "manual"
            guard isConfirmed else {
                skippedUnconfirmed += 1
                continue
            }

            let title = (record.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !title.isEmpty else { continue }

            // 映射依据见上方注释：状态字节取 source_status（上游待办状态），
            // 缺失时退回提醒自身的 status（此时 unscheduled/scheduled/sent 都会落到 0 待办）。
            let sourceStatus = protocolStatus(from: record.source_status)
            let reminderStatus = protocolStatus(from: record.status)
            // 任一侧是「已忽略」→ 不是待办，跳过。
            guard let status = sourceStatus, let reminderStatus else {
                skippedUnconfirmed += 1
                continue
            }
            // 任一侧是「已完成」→ 不上屏（只取未完成）。
            guard status != .completed, reminderStatus != .completed else { continue }

            items.append(PassportTodoItem(title: title, status: status))
            // 这里**不**截断：截断只发生在帧层（pushPlan 只发前 maxItems 条），
            // 否则状态帧的 count 会被动跟着变 ≤4，用户看不出还有多少条没显示。
        }
        if skippedUnconfirmed > 0 {
            NSLog("[PassportTodo] 跳过 %d 条未确认的候选（严格模式：只下发已确认/手工待办），"
                  + "实际下发 %d 条", skippedUnconfirmed, items.count)
        }
        return items
    }

    /// 服务端状态字符串 → 协议状态字节。
    ///
    /// 上游待办取值域（`local_web.py` VALID_TODO_STATUSES）：
    /// `pending` / `in_progress` / `completed` / `dismissed` / `blocked`。
    /// - `pending`            → 0 待办
    /// - `in_progress`        → 1 进行中
    /// - `completed`          → 2 已完成
    /// - `dismissed`          → nil（用户已忽略，不是待办）
    /// - `blocked`            → 0 待办（协议里没有「阻塞」态；按「待办」下发，避免丢条目）
    ///
    /// 提醒侧取值域（`local_reminders.py`）：`unscheduled` / `scheduled` / `sent`，
    /// 这三个都表示「提醒还没做完，但待办本身仍未完成」→ 一律 0 待办。
    ///
    /// 未知 / 缺失状态一律按 0（待办）处理：宁可多显示一条，也不静默丢一条。
    public static func protocolStatus(from raw: String?) -> PassportTodoStatus? {
        switch (raw ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "in_progress", "in-progress", "doing", "active":
            return .inProgress
        case "completed", "complete", "done":
            return .completed
        case "dismissed", "ignored", "dropped", "archived":
            return nil
        case "pending", "open", "todo", "blocked", "unscheduled", "scheduled", "sent", "":
            return .pending
        default:
            return .pending
        }
    }

    private struct RemindersResponse: Decodable {
        let reminders: [ReminderRecord]?
    }

    private struct ReminderRecord: Decodable {
        let title: String?
        /// 上游待办状态：pending / in_progress / completed / dismissed / blocked
        let source_status: String?
        /// 提醒自身状态：unscheduled / scheduled / sent / completed / dismissed
        let status: String?
        /// 复核状态：`confirmed` = 人在网页上确认过；其余（含 unreviewed）视为候选。
        /// 可选类型：服务端未升级时该键缺失，映射层据此进入严格模式。
        let review_status: String?
        /// 来源类型：`manual` = 人手工添加；`audio` = 管线自动抽取。
        let source_type: String?
    }
}

public enum PassportTodoFetchError: LocalizedError {
    case noEndpoint
    case httpStatus(Int)
    case unauthorized(Int)
    case timedOut

    public var errorDescription: String? {
        switch self {
        case .noEndpoint:
            return "没有可用的 Life Logger 地址"
        case .httpStatus(let code):
            return "Life Logger 返回 HTTP \(code)"
        case .unauthorized(let code):
            return "Life Logger 拒绝访问（HTTP \(code)，缺少或过期访问令牌）"
        case .timedOut:
            return "拉取今日待办超时"
        }
    }
}

/// Life Logger 本地服务地址 / 访问令牌的解析
public enum PassportTodoEndpoint {
    public static let defaultPort = 8765
    /// [2026-10-02 root 拍板] 数据源从 `/api/tasks` 换成 `/api/reminders`：
    /// 前者是任务状态台账，实测只回 1 条 dismissed（映射后 0 条）；后者才是 Web 首页
    /// 那份「未完成待办」的来源，实测 6 条里 4 条 pending。
    public static let remindersPath = "/api/reminders"

    /// 环境变量：直接指定完整 URL（最高优先级）
    public static let urlOverrideKey = "AIRPASSPORT_TODO_URL"
    /// 环境变量：直接指定访问令牌
    public static let tokenOverrideKey = "AIRPASSPORT_TODO_TOKEN"
    /// 环境变量：直接指定 secrets.json 路径
    public static let secretsOverrideKey = "LIFE_LOGGER_SECRETS"

    /// 候选 URL，按顺序尝试：
    /// 1. 环境变量覆盖
    /// 2. `http://127.0.0.1:8765/api/reminders`（默认地址）
    /// 3. 本机各网卡 IPv4 的 `:8765`
    ///
    /// 第 3 条是必需的：Life Logger 的 `preferences.json` 里
    /// `trusted_network_bind` 会把服务**只**绑到受信私网地址（本机当前为 172.20.10.3），
    /// 这种情况下 127.0.0.1:8765 根本没人监听（实测 connection refused）。
    /// 热点 IP 会变，所以遍历网卡而不是写死 IP。
    public static func candidateURLs(environment: [String: String] = ProcessInfo.processInfo.environment) -> [URL] {
        var urls: [URL] = []
        var seen = Set<String>()

        func append(_ url: URL?) {
            guard let url else { return }
            let key = url.absoluteString
            guard !seen.contains(key) else { return }
            seen.insert(key)
            urls.append(url)
        }

        if let raw = environment[urlOverrideKey]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !raw.isEmpty {
            append(URL(string: raw))
        }
        append(URL(string: "http://127.0.0.1:\(defaultPort)\(remindersPath)"))
        for address in localIPv4Addresses() {
            append(URL(string: "http://\(address):\(defaultPort)\(remindersPath)"))
        }
        return urls
    }

    /// 读取 Life Logger 的本地访问令牌。
    ///
    /// 为什么需要：`/api/reminders` 走 `_require_api_auth`，没有令牌固定返回
    /// `401 {"error": "unauthorized"}`，即使服务就绑在 127.0.0.1 也一样。
    /// 令牌由服务写在 `<仓库>/output/local-web/secrets.json` 的 `mobile_access_token` 字段，
    /// Web 端就是把它塞进 `?token=` 或 `life_logger_access` Cookie 用的 —— 这里用 Cookie，
    /// 与浏览器同一条鉴权路径，不新增服务端接口。
    public static func accessToken(environment: [String: String] = ProcessInfo.processInfo.environment) -> String? {
        if let raw = environment[tokenOverrideKey]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !raw.isEmpty {
            return raw
        }

        var candidates: [String] = []
        if let raw = environment[secretsOverrideKey]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !raw.isEmpty {
            candidates.append(raw)
        }
        // 本机 Life Logger 服务的实际密钥位置（可用 LIFE_LOGGER_SECRETS 覆盖）。
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        candidates.append("\(home)/Documents/Codex/2026-09-02/referenced-chatgpt-conversation-this-is-an/output/local-web/secrets.json")

        for path in candidates {
            guard let data = FileManager.default.contents(atPath: path),
                  let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let token = payload["mobile_access_token"] as? String,
                  !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                continue
            }
            return token.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return nil
    }

    /// 本机非回环网卡的 IPv4 地址（getifaddrs，纯本地查询，不做 DNS，不会阻塞）。
    public static func localIPv4Addresses() -> [String] {
        var results: [String] = []
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return [] }
        defer { freeifaddrs(head) }

        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let interface = cursor {
            defer { cursor = interface.pointee.ifa_next }
            guard let socketAddress = interface.pointee.ifa_addr,
                  socketAddress.pointee.sa_family == UInt8(AF_INET) else { continue }
            let flags = Int32(interface.pointee.ifa_flags)
            guard (flags & IFF_UP) != 0, (flags & IFF_LOOPBACK) == 0 else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            let status = getnameinfo(socketAddress,
                                     socklen_t(socketAddress.pointee.sa_len),
                                     &host,
                                     socklen_t(host.count),
                                     nil,
                                     0,
                                     NI_NUMERICHOST)
            guard status == 0 else { continue }
            let address = String(cString: host)
            guard !address.isEmpty, !results.contains(address) else { continue }
            results.append(address)
        }
        return results
    }
}

/// 今日待办拉取器：GET Life Logger `/api/reminders`，解析为待下发列表。
///
/// 线程模型：URLSession 的 completion 在后台队列回调，**不占用** CoreBluetooth 的
/// 主队列回调；调用方（PassportBLEBridge）负责把结果切回主队列再写特征。
public final class PassportTodoFetcher {

    private let session: URLSession
    private let environment: [String: String]
    /// 单次请求超时（需求：<= 3 秒）
    private let requestTimeout: TimeInterval
    /// 整个拉取流程（含多候选地址重试）的总预算，同样 <= 3 秒
    private let totalTimeout: TimeInterval

    /// 保护下面两个字段：`fetch()` 从主队列进来，URLSession 的 completion 在后台队列回来。
    private let lock = NSLock()
    /// 上一轮命中的地址，命中后优先复用，避免每 60 秒把所有网卡都试一遍。
    private var resolvedURL: URL?
    /// 同一时刻只允许一个拉取在飞；上一轮没回来就直接跳过（不排队、不堆积）。
    private var isFetching = false

    public init(requestTimeout: TimeInterval = 3.0,
                totalTimeout: TimeInterval = 3.0,
                environment: [String: String] = ProcessInfo.processInfo.environment) {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = requestTimeout
        configuration.timeoutIntervalForResource = requestTimeout
        configuration.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        configuration.waitsForConnectivity = false
        self.session = URLSession(configuration: configuration)
        self.requestTimeout = requestTimeout
        self.totalTimeout = totalTimeout
        self.environment = environment
    }

    deinit {
        session.invalidateAndCancel()
    }

    /// 拉取一次。失败时回调 `.failure`，调用方据此下发清空帧。
    public func fetch(completion: @escaping (Result<[PassportTodoItem], Error>) -> Void) {
        lock.lock()
        if isFetching {
            lock.unlock()
            logLine("[AirPassport Bridge] Todo fetch skipped: previous request still in flight")
            return
        }
        isFetching = true
        let cached = resolvedURL
        lock.unlock()

        let candidates = cached.map { [$0] } ?? PassportTodoEndpoint.candidateURLs(environment: environment)
        let token = PassportTodoEndpoint.accessToken(environment: environment)
        let deadline = Date().addingTimeInterval(totalTimeout)
        attempt(candidates: candidates,
                index: 0,
                token: token,
                deadline: deadline,
                lastError: nil,
                completion: completion)
    }

    private func cacheResolvedURL(_ url: URL?) {
        lock.lock()
        resolvedURL = url
        lock.unlock()
    }

    private func attempt(candidates: [URL],
                         index: Int,
                         token: String?,
                         deadline: Date,
                         lastError: Error?,
                         completion: @escaping (Result<[PassportTodoItem], Error>) -> Void) {
        let remaining = deadline.timeIntervalSinceNow
        guard index < candidates.count, remaining > 0 else {
            let error = lastError ?? PassportTodoFetchError.noEndpoint
            finish(.failure(error), completion: completion)
            return
        }

        let url = candidates[index]
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = max(0.2, min(requestTimeout, remaining))
        if let token {
            // 与 Web 端同一条鉴权路径：life_logger_access Cookie。
            request.setValue("life_logger_access=\(token)", forHTTPHeaderField: "Cookie")
        }
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        // 这里**强**持有 self：请求在飞的时候 fetcher 必须活着。
        // 若用 [weak self]，调用方一旦没有强引用 fetcher（例如 `PassportTodoFetcher().fetch { ... }`），
        // 对象会在 fetch 返回后立刻释放，completion 永远不会回调 —— 表现为"静默什么都不发生"。
        let task = session.dataTask(with: request) { data, response, error in
            if let error {
                logLine("[AirPassport Bridge] Todo fetch \(url.host ?? "?"): \(error.localizedDescription)")
                self.cacheResolvedURL(nil)
                self.attempt(candidates: candidates,
                             index: index + 1,
                             token: token,
                             deadline: deadline,
                             lastError: error,
                             completion: completion)
                return
            }

            let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
            if statusCode == 401 || statusCode == 403 {
                // 鉴权问题换地址也没用，直接失败（下一轮会重试）。
                self.cacheResolvedURL(nil)
                self.finish(.failure(PassportTodoFetchError.unauthorized(statusCode)), completion: completion)
                return
            }
            guard statusCode == 200, let data else {
                logLine("[AirPassport Bridge] Todo fetch \(url.host ?? "?"): HTTP \(statusCode)")
                self.cacheResolvedURL(nil)
                self.attempt(candidates: candidates,
                             index: index + 1,
                             token: token,
                             deadline: deadline,
                             lastError: PassportTodoFetchError.httpStatus(statusCode),
                             completion: completion)
                return
            }

            do {
                let items = try PassportTodoMapper.items(fromRemindersResponse: data)
                self.cacheResolvedURL(url)
                self.finish(.success(items), completion: completion)
            } catch {
                self.cacheResolvedURL(nil)
                self.finish(.failure(error), completion: completion)
            }
        }
        task.resume()
    }

    private func finish(_ result: Result<[PassportTodoItem], Error>,
                        completion: @escaping (Result<[PassportTodoItem], Error>) -> Void) {
        lock.lock()
        isFetching = false
        lock.unlock()
        completion(result)
    }
}
