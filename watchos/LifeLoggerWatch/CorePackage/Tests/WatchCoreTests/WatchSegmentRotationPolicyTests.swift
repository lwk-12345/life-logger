import Foundation
import XCTest
@testable import WatchCore

final class WatchSegmentRotationPolicyTests: XCTestCase {
    /// The regression this guards: the rotation duration argument disappeared during a
    /// "reduce main-thread work" refactor, the constants were left unreferenced, and every
    /// recording longer than 61 seconds was rejected by the Mac forever.
    func testRotationFitsInsideWhatTheMacAccepts() {
        XCTAssertTrue(WatchSegmentRotationPolicy.rotationFitsMacLimits)
        XCTAssertEqual(WatchSegmentRotationPolicy.declaredDurationMilliseconds, 60_000)
        XCTAssertLessThanOrEqual(
            WatchSegmentRotationPolicy.declaredDurationMilliseconds,
            WatchSegmentRotationPolicy.maximumAcceptedDurationMilliseconds
        )
    }

    func testTheTwoDurationConstantsCannotDriftApart() {
        XCTAssertEqual(
            WatchSegmentRotationPolicy.segmentDurationMilliseconds,
            WatchSegmentRotationPolicy.declaredDurationMilliseconds
        )
        XCTAssertGreaterThan(WatchSegmentRotationPolicy.segmentDurationSeconds, 0)
    }

    /// A segment must be declared at least as long as one rotation, otherwise the Mac would
    /// reject the file it just received as too short to be one of ours.
    func testDeclaredDurationCoversTheRecordedRotation() {
        XCTAssertGreaterThanOrEqual(
            WatchSegmentRotationPolicy.declaredDurationMilliseconds,
            Int64(WatchSegmentRotationPolicy.segmentDurationSeconds * 1_000) - 1_000
        )
    }
}
