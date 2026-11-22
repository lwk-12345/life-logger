import Foundation
import XCTest
@testable import WatchCore

final class WatchSegmentRecoveryPolicyTests: XCTestCase {
    func testIntactSessionNeedsNoDecision() {
        let verdict = WatchSegmentRecoveryPolicy.verdict(segments: [
            (0, true), (1, true), (2, true),
        ])

        XCTAssertEqual(verdict, .intact)
        XCTAssertFalse(WatchSegmentRecoveryPolicy.needsUserDecision(verdict))
        XCTAssertNil(WatchSegmentRecoveryPolicy.summary(verdict, sessionCount: 1))
    }

    /// The stuck-session case: some files are gone, so retrying forever is pointless but the
    /// remaining audio is still worth uploading.
    func testSessionWithSomeMissingFilesIsPartialAndNeedsADecision() {
        let verdict = WatchSegmentRecoveryPolicy.verdict(segments: [
            (0, true), (1, false), (2, true), (3, false),
        ])

        XCTAssertEqual(verdict, .partial(missing: [1, 3], presentCount: 2))
        XCTAssertTrue(WatchSegmentRecoveryPolicy.needsUserDecision(verdict))
        let summary = try? XCTUnwrap(WatchSegmentRecoveryPolicy.summary(verdict, sessionCount: 1))
        XCTAssertTrue(summary?.contains("2 个片段") ?? false)
        XCTAssertTrue(summary?.contains("2 个可上传") ?? false)
    }

    func testSessionWithNothingLeftIsEmpty() {
        let verdict = WatchSegmentRecoveryPolicy.verdict(segments: [(0, false), (1, false)])

        XCTAssertEqual(verdict, .empty)
        XCTAssertTrue(WatchSegmentRecoveryPolicy.needsUserDecision(verdict))
        XCTAssertNotNil(WatchSegmentRecoveryPolicy.summary(verdict, sessionCount: 9))
    }

    func testSessionWithNoSegmentsAtAllIsEmpty() {
        XCTAssertEqual(WatchSegmentRecoveryPolicy.verdict(segments: []), .empty)
    }
}

extension WatchSegmentRecoveryPolicyTests {
    /// The user's decision, encoded: audio that survived must be uploaded, and only the lost
    /// files are reported — never a reason to abandon the whole recording.
    func testPartitionSeparatesUploadableFromLost() {
        let partition = WatchSegmentRecoveryPolicy.partition(segments: [
            (sequence: 2, fileExists: true),
            (sequence: 0, fileExists: false),
            (sequence: 1, fileExists: true),
            (sequence: 3, fileExists: false),
        ])

        XCTAssertEqual(partition.uploadable, [1, 2])
        XCTAssertEqual(partition.missing, [0, 3])
    }

    func testPartitionWithEverythingPresentUploadsAll() {
        let partition = WatchSegmentRecoveryPolicy.partition(segments: [
            (0, true), (1, true),
        ])

        XCTAssertEqual(partition.uploadable, [0, 1])
        XCTAssertTrue(partition.missing.isEmpty)
    }

    func testPartitionWithNothingPresentStillReportsTheLoss() {
        let partition = WatchSegmentRecoveryPolicy.partition(segments: [
            (0, false), (1, false),
        ])

        XCTAssertTrue(partition.uploadable.isEmpty)
        XCTAssertEqual(partition.missing, [0, 1])
    }
}
