import Foundation

public struct WatchSleepInterval: Equatable, Sendable {
    public enum Stage: Sendable {
        case deep, core, rem, unspecified, awake
    }

    public let start: Date
    public let end: Date
    public let stage: Stage
    public let isAppleSource: Bool

    public init(start: Date, end: Date, stage: Stage, isAppleSource: Bool) {
        self.start = start
        self.end = end
        self.stage = stage
        self.isAppleSource = isAppleSource
    }
}

/// 从 HealthKit 样本算出“最近一次已结束的主睡眠”。
///
/// 只上报完整的一晚：查询窗口滑动时，半截数据不能覆盖同一天已上报的整晚；小睡不算；
/// 多来源和重叠样本不重复累加。
public enum WatchSleepSummarizer {
    public static let maxGap: TimeInterval = 90 * 60
    public static let settleTime: TimeInterval = 30 * 60
    public static let minimumMainSleep: TimeInterval = 3 * 3600
    public static let settledThrottle: TimeInterval = 3600
    public static let pendingRetryInterval: TimeInterval = 60

    /// 当天主睡眠尚未拿到（`cached == nil`，或缓存仍是前一日的睡眠）时，按 60 秒间隔重试查询；
    /// 拿到当天已结束的主睡眠后，降频到 3600 秒查一次。
    public static func shouldQuerySleep(
        cached: WatchSleepSummary?,
        lastQueryAt: Date,
        now: Date,
        timeZone: TimeZone = TimeZone(identifier: "Asia/Shanghai") ?? .current
    ) -> Bool {
        let dayFormatter = DateFormatter()
        dayFormatter.locale = Locale(identifier: "en_US_POSIX")
        dayFormatter.timeZone = timeZone
        dayFormatter.dateFormat = "yyyy-MM-dd"
        let todayString = dayFormatter.string(from: now)
        let interval = (cached?.date == todayString) ? settledThrottle : pendingRetryInterval
        return now.timeIntervalSince(lastQueryAt) >= interval
    }

    public static func latestMainSleep(
        _ samples: [WatchSleepInterval],
        now: Date,
        timeZone: TimeZone = TimeZone(identifier: "Asia/Shanghai") ?? .current
    ) -> WatchSleepSummary? {
        let usable = samples.filter { $0.end > $0.start }
        let hasAppleAsleep = usable.contains { $0.isAppleSource && $0.stage != .awake }
        let preferred = hasAppleAsleep ? usable.filter(\.isAppleSource) : usable
        let asleep = preferred.filter { $0.stage != .awake }.sorted { $0.start < $1.start }

        var groups: [(start: Date, end: Date)] = []
        for sample in asleep {
            if let last = groups.last, sample.start.timeIntervalSince(last.end) < maxGap {
                groups[groups.count - 1].end = max(last.end, sample.end)
            } else {
                groups.append((sample.start, sample.end))
            }
        }

        for group in groups.reversed() where now.timeIntervalSince(group.end) >= settleTime {
            let inside = preferred.filter { $0.start >= group.start && $0.end <= group.end }
            let minutes = stageMinutes(inside)
            let total = minutes.deep + minutes.core + minutes.rem + minutes.unspecified
            guard total * 60 >= minimumMainSleep else { continue }
            let dayFormatter = DateFormatter()
            dayFormatter.locale = Locale(identifier: "en_US_POSIX")
            dayFormatter.timeZone = timeZone
            dayFormatter.dateFormat = "yyyy-MM-dd"
            let isoFormatter = ISO8601DateFormatter()
            isoFormatter.timeZone = timeZone
            return WatchSleepSummary(
                date: dayFormatter.string(from: group.end),
                startTime: isoFormatter.string(from: group.start),
                endTime: isoFormatter.string(from: group.end),
                totalMinutes: Int(total.rounded()),
                deepMinutes: Int(minutes.deep.rounded()),
                coreMinutes: Int(minutes.core.rounded()),
                remMinutes: Int(minutes.rem.rounded()),
                awakeMinutes: Int(minutes.awake.rounded())
            )
        }
        return nil
    }

    /// 重叠部分只算一次：先到的样本占住这段时间，清醒和睡着重叠时算睡着。
    private static func stageMinutes(
        _ samples: [WatchSleepInterval]
    ) -> (deep: Double, core: Double, rem: Double, unspecified: Double, awake: Double) {
        var result = (deep: 0.0, core: 0.0, rem: 0.0, unspecified: 0.0, awake: 0.0)
        var covered = Date.distantPast
        let ordered = samples.filter { $0.stage != .awake }.sorted { $0.start < $1.start }
        for sample in ordered {
            let start = max(sample.start, covered)
            guard sample.end > start else { continue }
            let minutes = sample.end.timeIntervalSince(start) / 60
            switch sample.stage {
            case .deep: result.deep += minutes
            case .core: result.core += minutes
            case .rem: result.rem += minutes
            case .unspecified: result.unspecified += minutes
            case .awake: break
            }
            covered = sample.end
        }
        let asleepRanges = ordered.map { ($0.start, $0.end) }
        for sample in samples where sample.stage == .awake {
            var uncovered = sample.end.timeIntervalSince(sample.start)
            for (start, end) in asleepRanges {
                let overlap = min(end, sample.end).timeIntervalSince(max(start, sample.start))
                if overlap > 0 { uncovered -= overlap }
            }
            result.awake += max(0, uncovered) / 60
        }
        return result
    }
}
