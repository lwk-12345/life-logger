import XCTest
@testable import WatchCore

final class WatchSyncFollowUpPolicyTests: XCTestCase {
    func testSyncRequestDuringLiveUploadRunsAfterGateOpens() {
        var policy = WatchSyncFollowUpPolicy()

        XCTAssertTrue(policy.shouldDeferFullSync(isSyncing: true, captureBlocked: false))
        XCTAssertTrue(policy.isRequested)
        XCTAssertTrue(policy.liveUploadDidFinish(captureBlocked: false))
        XCTAssertFalse(policy.isRequested)
        XCTAssertFalse(policy.liveUploadDidFinish(captureBlocked: false))
    }

    func testRequestDuringCaptureRemainsPendingUntilStoppedSyncCanStart() {
        var policy = WatchSyncFollowUpPolicy()

        XCTAssertTrue(policy.shouldDeferFullSync(isSyncing: false, captureBlocked: true))
        XCTAssertFalse(policy.liveUploadDidFinish(captureBlocked: true))
        XCTAssertTrue(policy.isRequested)
        XCTAssertFalse(policy.shouldDeferFullSync(isSyncing: false, captureBlocked: false))
        XCTAssertFalse(policy.isRequested)
    }

    func testLiveUploadTapDuringActiveUploadIsQueuedInsteadOfRejected() {
        var policy = WatchSyncFollowUpPolicy()

        XCTAssertTrue(policy.shouldDeferLiveUpload(isSyncing: true))
        XCTAssertTrue(policy.isLiveUploadRequested)
        XCTAssertTrue(policy.consumeLiveUploadRequest())
        XCTAssertFalse(policy.isLiveUploadRequested)
        XCTAssertFalse(policy.consumeLiveUploadRequest())
    }

    func testLiveUploadStartsImmediatelyWhenNoUploadIsActive() {
        var policy = WatchSyncFollowUpPolicy()

        XCTAssertFalse(policy.shouldDeferLiveUpload(isSyncing: false))
        XCTAssertFalse(policy.isLiveUploadRequested)
    }

    func testRequestWhileLiveUploadAndCaptureGateAreActiveIsRetained() {
        var policy = WatchSyncFollowUpPolicy()

        XCTAssertTrue(policy.shouldDeferFullSync(isSyncing: true, captureBlocked: true))
        XCTAssertFalse(policy.liveUploadDidFinish(captureBlocked: true))
        XCTAssertTrue(policy.isRequested)
        XCTAssertTrue(policy.liveUploadDidFinish(captureBlocked: false))
    }
}
