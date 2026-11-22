import XCTest
@testable import WatchCore

final class WatchSleepSummarizerTests: XCTestCase {
    private let base = Date(timeIntervalSince1970: 1_800_000_000)

    private func interval(_ startHour: Double, _ endHour: Double, _ stage: WatchSleepInterval.Stage = .core,
                          apple: Bool = true) -> WatchSleepInterval {
        WatchSleepInterval(start: base.addingTimeInterval(startHour * 3600),
                           end: base.addingTimeInterval(endHour * 3600), stage: stage, isAppleSource: apple)
    }

    private func at(_ hour: Double) -> Date { base.addingTimeInterval(hour * 3600) }

    func testFullNightSummed() throws {
        let out = try XCTUnwrap(WatchSleepSummarizer.latestMainSleep(
            [interval(0, 2), interval(2, 3, .deep), interval(3, 4, .rem), interval(4, 4.5, .awake), interval(4.5, 6)],
            now: at(7)))
        XCTAssertEqual(out.totalMinutes, 330)
        XCTAssertEqual(out.deepMinutes, 60)
        XCTAssertEqual(out.remMinutes, 60)
        XCTAssertEqual(out.awakeMinutes, 30)
    }

    func testStillSleepingIsNotReported() {
        XCTAssertNil(WatchSleepSummarizer.latestMainSleep([interval(0, 6)], now: at(6.1)))
    }

    func testNapAfterNightDoesNotReplaceNight() throws {
        let out = try XCTUnwrap(WatchSleepSummarizer.latestMainSleep(
            [interval(0, 7), interval(13, 13.5)], now: at(15)))
        XCTAssertEqual(out.totalMinutes, 420)
    }

    func testNapMergedOnlyWhenCloseAndShortSleepIgnored() {
        XCTAssertNil(WatchSleepSummarizer.latestMainSleep([interval(13, 14)], now: at(15)))
    }

    func testSlidingWindowRemnantDoesNotBecomeSummary() {
        // 窗口滑过后只剩后半夜 37 分钟，不能上报成当晚睡眠。
        XCTAssertNil(WatchSleepSummarizer.latestMainSleep([interval(0, 37.0 / 60)], now: at(3)))
    }

    func testOtherSourcesIgnoredWhenAppleExists() throws {
        let out = try XCTUnwrap(WatchSleepSummarizer.latestMainSleep(
            [interval(0, 6), interval(0, 6, .core, apple: false)], now: at(7)))
        XCTAssertEqual(out.totalMinutes, 360)
    }

    func testOverlappingSamplesCountOnce() throws {
        let out = try XCTUnwrap(WatchSleepSummarizer.latestMainSleep(
            [interval(0, 4), interval(3, 6)], now: at(7)))
        XCTAssertEqual(out.totalMinutes, 360)
    }

    func testThirdPartyUsedWhenNoApple() throws {
        let out = try XCTUnwrap(WatchSleepSummarizer.latestMainSleep(
            [interval(0, 5, .unspecified, apple: false)], now: at(6)))
        XCTAssertEqual(out.totalMinutes, 300)
    }

    func testThirdPartyUsedWhenAppleOnlyHasAwakeSamples() throws {
        let out = try XCTUnwrap(WatchSleepSummarizer.latestMainSleep(
            [interval(0, 0.5, .awake, apple: true), interval(0.5, 6, .unspecified, apple: false)],
            now: at(7)
        ))
        XCTAssertEqual(out.totalMinutes, 330)
    }

    func testShouldQuerySleepRetriesEveryMinuteUntilTodaySleepIsResolved() {
        let tz = TimeZone(identifier: "Asia/Shanghai")!
        let now = at(10)
        let dayFormatter = DateFormatter()
        dayFormatter.locale = Locale(identifier: "en_US_POSIX")
        dayFormatter.timeZone = tz
        dayFormatter.dateFormat = "yyyy-MM-dd"
        let today = dayFormatter.string(from: now)

        // 1. 尚无缓存（如刚弹窗未授权或刚起床未过 settleTime）：60 秒后就允许重试，不锁死 1 小时
        XCTAssertFalse(WatchSleepSummarizer.shouldQuerySleep(
            cached: nil, lastQueryAt: now.addingTimeInterval(-30), now: now, timeZone: tz
        ))
        XCTAssertTrue(WatchSleepSummarizer.shouldQuerySleep(
            cached: nil, lastQueryAt: now.addingTimeInterval(-60), now: now, timeZone: tz
        ))

        // 2. 缓存仍是前一日的睡眠（当天睡眠尚未过 30 分钟 settleTime）：同样 60 秒重试
        let yesterdaySummary = WatchSleepSummary(
            date: "1999-01-01", startTime: "a", endTime: "b",
            totalMinutes: 420, deepMinutes: 60, coreMinutes: 300, remMinutes: 60, awakeMinutes: 10
        )
        XCTAssertTrue(WatchSleepSummarizer.shouldQuerySleep(
            cached: yesterdaySummary, lastQueryAt: now.addingTimeInterval(-60), now: now, timeZone: tz
        ))

        // 3. 当天主睡眠已拿到：降频到 3600 秒
        let todaySummary = WatchSleepSummary(
            date: today, startTime: "a", endTime: "b",
            totalMinutes: 420, deepMinutes: 60, coreMinutes: 300, remMinutes: 60, awakeMinutes: 10
        )
        XCTAssertFalse(WatchSleepSummarizer.shouldQuerySleep(
            cached: todaySummary, lastQueryAt: now.addingTimeInterval(-120), now: now, timeZone: tz
        ))
        XCTAssertTrue(WatchSleepSummarizer.shouldQuerySleep(
            cached: todaySummary, lastQueryAt: now.addingTimeInterval(-3600), now: now, timeZone: tz
        ))
    }
}
