// PassportHabitSync.swift
//
// 习惯打卡帧（0x04）的协议、拉取与映射 —— 与待办帧（0x02）同一条 capabilities 特征，
// 靠「首字节 + 载荷长度」分流，互不干扰。
//
// 固件侧已就绪（另一个仓库 main/passport_todo.h:42-44、passport_ptt_ble.c:454-459、
// identity_home.c:213/655-680 第 3 屏 HABITS 页），本文件补的是 Mac 侧从零缺失的那一段。
//
// 帧格式（与固件一致）：
//   [0x04][index][total][flags][title_len][title utf8…]
//   index      0 起
//   total      本轮习惯总数；0 = 清空
//   flags      bit0 = 今日已打卡（1=已打卡 / 0=未打卡）；其余位保留，解析方必须忽略
//   title_len  ≤ 60 字节（20 汉字），与待办同口径；UTF-8 按字符边界截断
//
// 与待办协议的关键差异：习惯没有「进行中」这种中间态，只有「今天打没打卡」两态，
// 所以状态字节换成了 flags（携带打卡位），而不是 0x02 使用的 status。

import Foundation

// ==============================================================================
// 数据模型
// ==============================================================================

/// 一条要下发到设备的习惯。
public struct PassportHabitItem: Equatable, Sendable {
    /// 展示标题（已按 maxTitleBytes 截断）
    public let title: String
    /// 今日是否已打卡
    public let checked: Bool
    /// 连续天数（本轮固件不显示，随协议预留；服务端已算好）
    public let streak: Int

    public init(title: String, checked: Bool, streak: Int = 0) {
        self.title = title
        self.checked = checked
        self.streak = streak
    }
}

/// 一轮完整下发的写入计划：清空帧 + N 条习惯帧。
/// 注意：习惯帧**没有**类似 0x03 的独立状态帧 —— 打卡位就在每条的 flags 里。
public struct PassportHabitPushPlan: Equatable {
    /// 第 0 条永远是清空帧（total=0），保证设备列表是本轮快照、不留残影。
    public let habitFrames: [Data]

    /// 实际下发的条目数（不含清空帧）
    public var itemCount: Int { max(0, habitFrames.count - 1) }
}

// ==============================================================================
// 协议（纯函数，便于单测）
// ==============================================================================

public enum PassportHabitProtocol {
    /// 习惯帧魔数：第一个字节固定 0x04（固件 side 用它跟校时/0x02/0x03 区分）
    public static let frameMagic: UInt8 = 0x04
    /// 帧头长度：[0x04][index][total][flags][title_len]
    public static let headerSize = 5
    /// 设备只有 4 个槽位（固件 PASSPORT_HABIT_MAX_ITEMS）
    public static let maxItems = 4
    /// 标题上限（字节），与待办同口径：60 字节 = 20 个汉字
    public static let maxTitleBytes = 60
    /// 相邻帧之间的写入间隔（与待办一致，避免写队列拥塞）
    public static let interFrameDelay: TimeInterval = 0.06

    /// 清空帧：`[0x04][0][0][0][0]`（total=0 → 设备显示「暂无习惯」）。
    /// 长度 5，满足固件的 `length >= 5 且 payload[0] == 0x04`，不会被当成别的帧。
    public static func clearFrame() -> Data {
        Data([frameMagic, 0, 0, 0, 0])
    }

    /// 编码一条习惯帧。title_len 写**实际字节数**（= data.count - headerSize），
    /// 越界值在这里收敛，调用方不需要自己保证。
    public static func encodeFrame(index: Int,
                                   total: Int,
                                   checked: Bool,
                                   title: String,
                                   maxTitleBytes: Int = PassportHabitProtocol.maxTitleBytes) -> Data {
        let safeIndex = UInt8(clamping: max(0, index))
        let safeTotal = UInt8(clamping: max(0, total))
        // flags: 只设 bit0（今日已打卡）；其余位保留为 0，供后续扩展。
        let flags: UInt8 = checked ? 0x01 : 0x00
        let titleData = truncatedTitleData(title, maxBytes: maxTitleBytes)
        let titleLen = UInt8(clamping: titleData.count)
        var payload = Data([frameMagic, safeIndex, safeTotal, flags, titleLen])
        payload.append(titleData)
        return payload
    }

    /// 组装一轮下发的写入计划。超过 maxItems 的条目在此丢弃（设备只有 4 槽）。
    public static func pushPlan(items: [PassportHabitItem]) -> PassportHabitPushPlan {
        let limited = Array(items.prefix(maxItems))
        var frames: [Data] = [clearFrame()]
        for (index, item) in limited.enumerated() {
            frames.append(encodeFrame(index: index,
                                      total: limited.count,
                                      checked: item.checked,
                                      title: item.title))
        }
        return PassportHabitPushPlan(habitFrames: frames)
    }

    /// 按 UTF-8 **字符边界**截断到不超过 maxBytes，超长则补「…」。
    /// 与待办帧的截断策略保持一致：宁可少显示一个字，也不写出半个 UTF-8 序列
    /// （半个字符在设备侧是乱码方块）。
    public static func truncatedTitleData(_ title: String, maxBytes: Int) -> Data {
        let ellipsis = "…"
        let ellipsisBytes = ellipsis.utf8.count
        guard maxBytes > 0 else { return Data() }

        let raw = Data(title.utf8)
        if raw.count <= maxBytes { return raw }

        // 上限连一个字符加省略号都放不下：整字截断，不补省略号
        // （补了会超上限，反而让 title_len 与实际字节数不符）。
        let budget = maxBytes - ellipsisBytes
        if budget <= 0 {
            return boundaryTruncate(raw, maxBytes: maxBytes)
        }
        // 先给省略号留位置，再按字符边界填标题。
        let head = boundaryTruncate(raw, maxBytes: budget)
        // 一个字都没放下（budget 比单个字符还小）→ 补省略号只会得到一串「…」，
        // 毫无信息量。此时退回「整字截断」语义：宁可显示一个完整字符，也不显示纯省略号。
        if head.isEmpty {
            return boundaryTruncate(raw, maxBytes: maxBytes)
        }
        // 标题在预算内本来就完整 → 不补省略号
        if head.count == raw.count { return head }
        var out = head
        out.append(Data(ellipsis.utf8))
        return out
    }

    /// 按 UTF-8 字符边界截断（不补省略号），保证不切断多字节序列。
    private static func boundaryTruncate(_ data: Data, maxBytes: Int) -> Data {
        guard data.count > maxBytes else { return data }
        // 从 maxBytes 往回退，直到落在一个字符的起始字节上。
        // UTF-8 起始字节的高位不是 10xxxxxx；/10/ 为 continuation byte。
        var end = maxBytes
        while end > 0 {
            let byte = data[data.startIndex + end]
            if (byte & 0xC0) != 0x80 { break }  // 找到字符起始 → 此处正好是边界
            end -= 1
        }
        return data.prefix(end)
    }
}

// ==============================================================================
// 服务端返回 → items 映射（纯函数，便于单测）
// ==============================================================================

public enum PassportHabitMapper {
    /// 解析 `GET /api/habits` 的真实返回。
    ///
    /// 期望结构（src/life_logger/local_habits.py 的 snapshot + local_web.py:5832 注入的 badge_slots）：
    /// ```json
    /// {
    ///   "ok": true,
    ///   "schema_version": "local-habits-v1",
    ///   "date": "2026-10-13",
    ///   "total": 2,
    ///   "checked_count": 1,
    ///   "habits": [ { "habit_key": "...", "title": "冥想", "checked_today": true, "streak": 3, "status": "active" } ],
    ///   "badge_slots": [ { "index": 1, "total": 1, "flags": 1, "title": "...", "checked_today": true, "streak": 1 } ]
    /// }
    /// ```
    ///
    /// 优先用 `badge_slots`（服务端已经按徽章口径筛好、排好、带上 flags 位）；
    /// 没有该字段时退回 `habits`（按 active + checked_today 自己算）。
    public static func items(fromHabitsResponse data: Data) throws -> [PassportHabitItem] {
        let root = try JSONSerialization.jsonObject(with: data)

        if let dict = root as? [String: Any],
           let slots = dict["badge_slots"] as? [[String: Any]], !slots.isEmpty {
            return slots.compactMap { slot in
                let title = (slot["title"] as? String) ?? ""
                guard !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
                let checked = (slot["flags"] as? Int).map { ($0 & 0x01) == 1 }
                    ?? (slot["checked_today"] as? Bool) ?? false
                let streak = (slot["streak"] as? Int) ?? 0
                return PassportHabitItem(title: title, checked: checked, streak: streak)
            }
        }

        // 兜底：从 habits 数组自己算
        if let dict = root as? [String: Any],
           let habits = dict["habits"] as? [[String: Any]] {
            return habits.compactMap { h in
                guard ((h["status"] as? String) ?? "active") == "active" else { return nil }
                let title = (h["title"] as? String) ?? ""
                guard !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
                let checked = (h["checked_today"] as? Bool) ?? false
                let streak = (h["streak"] as? Int) ?? 0
                return PassportHabitItem(title: title, checked: checked, streak: streak)
            }
        }

        return []
    }
}
