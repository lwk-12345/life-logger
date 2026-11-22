import Foundation
import XCTest
@testable import WatchCore

final class WatchGesturePinPolicyTests: XCTestCase {
    func testWithinPinchWindowEvaluation() {
        let now = Date()
        let pinTime = now.addingTimeInterval(-20)
        XCTAssertTrue(WatchGesturePinPolicy.isWithinPinchWindow(pinTimestamp: pinTime, evaluationTime: now))

        let expiredPin = now.addingTimeInterval(-75)
        XCTAssertFalse(WatchGesturePinPolicy.isWithinPinchWindow(pinTimestamp: expiredPin, evaluationTime: now))
    }

    func testMilestoneRemindersConformToOptionOne() {
        XCTAssertEqual(WatchGesturePinPolicy.milestoneForElapsedSeconds(60), .oneMinuteLightTap)
        XCTAssertEqual(WatchGesturePinPolicy.milestoneForElapsedSeconds(300), .fiveMinuteMilestone)
        XCTAssertEqual(WatchGesturePinPolicy.milestoneForElapsedSeconds(900), .fifteenMinuteMilestone)
        XCTAssertEqual(WatchGesturePinPolicy.milestoneForElapsedSeconds(1800), .thirtyMinuteMilestone)
        XCTAssertEqual(WatchGesturePinPolicy.milestoneForElapsedSeconds(120), .none)
    }
}
