import Foundation
import Testing
@testable import WatchCore

/// [2026-11-07 V68] 当日累计步数：自然日边界、跨日、时区。
///
/// 对应诊断里两个真实 bug：
///   * 11-04 真实 7666 被延迟上传的小值覆盖成 1212；
///   * 11-02 23:12 的 8328 出现在 11-03 00:38（跨日串了）。
struct WatchDailyStepsPolicyTests {
    private func date(_ iso: String) -> Date {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: iso)!
    }

    private var beijing: TimeZone { TimeZone(identifier: "Asia/Shanghai")! }
    private var utc: TimeZone { TimeZone(identifier: "UTC")! }

    @Test("自然日字符串按传入时区算，不是写死的 Asia/Shanghai")
    func dateStringFollowsTheGivenTimeZone() {
        // 2026-11-04T16:30Z = 北京 11-05 00:30 = UTC 11-04 16:30
        let moment = date("2026-11-04T16:30:00Z")
        #expect(WatchDailyStepsPolicy.dateString(for: moment, timeZone: beijing) == "2026-11-05")
        #expect(WatchDailyStepsPolicy.dateString(for: moment, timeZone: utc) == "2026-11-04")
    }

    @Test("自然日 0 点用于统计查询的下界")
    func startOfDayIsLocalMidnight() {
        let moment = date("2026-11-04T23:59:00Z")           // 北京 11-05 07:59
        let start = WatchDailyStepsPolicy.startOfDay(for: moment, timeZone: beijing)
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = beijing
        let comps = calendar.dateComponents([.year, .month, .day, .hour, .minute], from: start)
        #expect(comps.year == 2026 && comps.month == 11 && comps.day == 5)
        #expect(comps.hour == 0 && comps.minute == 0)
    }

    @Test("跨日后缓存必须作废（否则 00:38 会发昨天的 8328）")
    func cacheExpiresAtMidnight() {
        let before = date("2026-11-02T23:12:00+08:00")
        let after = date("2026-11-03T00:38:00+08:00")
        let cachedDate = WatchDailyStepsPolicy.dateString(for: before, timeZone: beijing)
        #expect(WatchDailyStepsPolicy.isFresh(cachedDate: cachedDate, now: before, timeZone: beijing))
        #expect(!WatchDailyStepsPolicy.isFresh(cachedDate: cachedDate, now: after, timeZone: beijing))
        #expect(!WatchDailyStepsPolicy.isFresh(cachedDate: nil, now: before, timeZone: beijing))
    }

    @Test("同一天只接受更大的值（当日累计只增不减）")
    func sameDayKeepsTheLargerValue() {
        let morning = WatchDailySteps(stepsToday: 1212, date: "2026-11-04",
                                      sampledAt: "2026-11-04T07:30:00+08:00")
        let night = WatchDailySteps(stepsToday: 7666, date: "2026-11-04",
                                    sampledAt: "2026-11-04T23:59:00+08:00")
        #expect(WatchDailyStepsPolicy.shouldAccept(night, previous: morning))
        #expect(!WatchDailyStepsPolicy.shouldAccept(morning, previous: night),
                "延迟上传的小值不能覆盖大值")
        #expect(WatchDailyStepsPolicy.shouldAccept(night, previous: nil))
    }

    @Test("不同自然日互不影响（跨日不串）")
    func differentDaysAreIndependent() {
        let day1 = WatchDailySteps(stepsToday: 8328, date: "2026-11-02",
                                   sampledAt: "2026-11-02T23:12:00+08:00")
        let day2 = WatchDailySteps(stepsToday: 1088, date: "2026-11-03",
                                   sampledAt: "2026-11-03T04:20:00+08:00")
        #expect(WatchDailyStepsPolicy.shouldAccept(day2, previous: day1))
        #expect(WatchDailyStepsPolicy.shouldAccept(day1, previous: day2))
    }

    @Test("采样时刻带时区偏移，服务端能直接解析")
    func sampledAtIsISO8601() {
        let moment = date("2026-11-04T23:59:00+08:00")
        let text = WatchDailyStepsPolicy.sampledAtString(for: moment)
        #expect(text.contains("2026-11-04T15:59:00"))
        #expect(ISO8601DateFormatter().date(from: text) != nil)
    }

    @Test("旧快照（只有 stepCount）仍可编码/解码，字段不丢")
    func legacySnapshotStillRoundTrips() throws {
        let legacy = WatchHealthSnapshot(heartRateBPM: 93, stepCount: 1212)
        let data = try JSONEncoder().encode(legacy)
        let decoded = try JSONDecoder().decode(WatchHealthSnapshot.self, from: data)
        #expect(decoded.stepCount == 1212)
        #expect(decoded.stepsToday == nil && decoded.stepsDate == nil && decoded.stepsSampledAt == nil)

        let modern = WatchHealthSnapshot(stepCount: 7666, stepsToday: 7666,
                                         stepsDate: "2026-11-04",
                                         stepsSampledAt: "2026-11-04T23:59:00+08:00")
        let roundTrip = try JSONDecoder().decode(
            WatchHealthSnapshot.self, from: JSONEncoder().encode(modern)
        )
        #expect(roundTrip.stepsToday == 7666)
        #expect(roundTrip.stepsDate == "2026-11-04")
    }
}
