import XCTest
@testable import WatchCore

final class WatchSleepSummaryTests: XCTestCase {
    func testHeaderUsesMacFieldNames() throws {
        let summary = WatchSleepSummary(date: "2026-11-05", startTime: "a", endTime: "b",
                                        totalMinutes: 400, deepMinutes: 60, coreMinutes: 280,
                                        remMinutes: 60, awakeMinutes: 10)
        let data = try XCTUnwrap(summary.headerValue()?.data(using: .utf8))
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(json["date"] as? String, "2026-11-05")
        XCTAssertEqual(json["total_minutes"] as? Int, 400)
        XCTAssertEqual(json["deep_minutes"] as? Int, 60)
    }

    func testOldSnapshotWithoutSleepStillDecodes() throws {
        let old = #"{"heartRateBPM":80,"stepCount":10}"#.data(using: .utf8)!
        let snapshot = try JSONDecoder().decode(WatchHealthSnapshot.self, from: old)
        XCTAssertEqual(snapshot.stepCount, 10)
        XCTAssertNil(snapshot.sleep)
    }
}
