import XCTest
@testable import WatchCore

final class WatchSegmentUploadPolicyTests: XCTestCase {
    func testLiveBatchUploadsEachSealedSegment() {
        XCTAssertFalse(
            WatchSegmentUploadPolicy.shouldUpload(
                isRecording: true,
                pendingSegmentCount: 0
            )
        )
        XCTAssertTrue(
            WatchSegmentUploadPolicy.shouldUpload(
                isRecording: true,
                pendingSegmentCount: 1
            )
        )
    }

    func testStoppedRecordingUsesTheNormalCompletionPath() {
        XCTAssertFalse(
            WatchSegmentUploadPolicy.shouldUpload(
                isRecording: false,
                pendingSegmentCount: WatchSegmentUploadPolicy.minimumPendingSegments
            )
        )
    }
}
