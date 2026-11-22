import Foundation

/// 一个「当日累计步数」读数：**值 + 它属于哪一天**（手表本地时区的自然日）。
///
/// 为什么需要它（2026-11-07 V68）：
/// V67 及以前只上传一个裸的 `stepCount`，服务端只能按**分段采集日**归档，
/// 于是两件事同时出错：
///   1. 延迟上传的旧分段带着当天较早的小值，把已经写好的大值覆盖掉
///      （实测 11-04 真实 7666 被覆盖成 1212）；
///   2. 跨日时把前一天的累计带进新的一天（11-02 23:12 的 8328 出现在 11-03 00:38）。
/// 修法是把「值」和「值所属的自然日」一起上传，服务端按日期归档、同日只增不减。
///
/// 这个类型是**纯数据**（不带 HealthKit），便于在 CorePackage 里做单测。
public struct WatchDailySteps: Codable, Equatable, Sendable {
    /// 当天 0 点（本地时区）到采样时刻的累计步数。
    public let stepsToday: Int
    /// `stepsToday` 所属的自然日，格式 `YYYY-MM-DD`（手表**本地时区**）。
    public let date: String
    /// 采样时刻（ISO8601，带时区偏移）。
    public let sampledAt: String

    public init(stepsToday: Int, date: String, sampledAt: String) {
        self.stepsToday = stepsToday
        self.date = date
        self.sampledAt = sampledAt
    }
}

/// 当日累计步数的**纯策略**：自然日边界、缓存是否过期、新值该不该覆盖旧值。
///
/// 全部按**传进来的时区**算，不写死 Asia/Shanghai —— 用户出差/换时区时，
/// "今天"应该跟手表自己的本地 0 点走。
public enum WatchDailyStepsPolicy {
    /// `date` 所在自然日的 `YYYY-MM-DD`（按 `timeZone`）。
    public static func dateString(for date: Date, timeZone: TimeZone) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let comps = calendar.dateComponents([.year, .month, .day], from: date)
        let year = comps.year ?? 1970
        let month = comps.month ?? 1
        let day = comps.day ?? 1
        return String(format: "%04d-%02d-%02d", year, month, day)
    }

    /// `date` 所在自然日的 0 点（按 `timeZone`）。HealthKit 的统计查询用它做下界。
    public static func startOfDay(for date: Date, timeZone: TimeZone) -> Date {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        return calendar.startOfDay(for: date)
    }

    /// 采样时刻的 ISO8601 字符串（带偏移，例如 `2026-11-04T23:59:00+08:00`）。
    public static func sampledAtString(for date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }

    /// 缓存的读数是否还属于 `now` 所在的那一天。
    ///
    /// **跨日必须作废**：否则 00:38 上传时会把昨天 23:12 的累计（8328）当成"今天"的值发出去。
    public static func isFresh(cachedDate: String?, now: Date, timeZone: TimeZone) -> Bool {
        guard let cachedDate else { return false }
        return cachedDate == dateString(for: now, timeZone: timeZone)
    }

    /// 新读数该不该覆盖旧读数。
    ///
    /// 规则：
    ///   * 日期不同 → 接受（不同自然日互不影响）；
    ///   * 同一天 → **只接受更大的值**（当日累计只增不减）；
    ///   * 手表重启/重装会让计数从 0 重新数 → 变小的一律忽略（服务端也会再挡一次）。
    public static func shouldAccept(_ candidate: WatchDailySteps, previous: WatchDailySteps?) -> Bool {
        guard let previous else { return true }
        if previous.date != candidate.date { return true }
        return candidate.stepsToday >= previous.stepsToday
    }
}
