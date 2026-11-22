import XCTest
@testable import WatchCore

final class WatchCaptureRecoveryPolicyTests: XCTestCase {
    func testRotationFailureRetriesWithBoundedBackoff() {
        var delays: [Double] = []
        for attempt in 1...6 {
            guard case .retry(let delay) = WatchCaptureRecoveryPolicy.nextStep(
                cause: .rotationFailed, attempt: attempt, elapsedSeconds: 0
            ) else { return XCTFail("expected retry") }
            delays.append(delay)
        }
        XCTAssertEqual(delays, [0.5, 1, 2, 4, 8, 8])
    }

    func testInterruptionBacksOffMoreGently() {
        XCTAssertEqual(
            WatchCaptureRecoveryPolicy.nextStep(cause: .interruption, attempt: 1, elapsedSeconds: 0),
            .retry(afterSeconds: 2)
        )
        XCTAssertEqual(
            WatchCaptureRecoveryPolicy.nextStep(cause: .interruption, attempt: 9, elapsedSeconds: 0),
            .retry(afterSeconds: 8)
        )
    }

    func testRecoveryGivesUpOnlyAfterTheGraceWindow() {
        XCTAssertEqual(
            WatchCaptureRecoveryPolicy.nextStep(cause: .rotationFailed, attempt: 3, elapsedSeconds: 89),
            .retry(afterSeconds: 2)
        )
        XCTAssertEqual(
            WatchCaptureRecoveryPolicy.nextStep(cause: .rotationFailed, attempt: 3, elapsedSeconds: 90),
            .giveUp
        )
        XCTAssertEqual(
            WatchCaptureRecoveryPolicy.nextStep(cause: .interruption, attempt: 3, elapsedSeconds: 120),
            .retry(afterSeconds: 30)
        )
    }

    func testLongInterruptionStillRetriesWithoutAnEndedNotification() {
        XCTAssertEqual(
            WatchCaptureRecoveryPolicy.nextStep(cause: .interruption, attempt: 100, elapsedSeconds: 3600),
            .retry(afterSeconds: 30)
        )
    }

    func testAttemptBelowOneIsTreatedAsFirst() {
        XCTAssertEqual(
            WatchCaptureRecoveryPolicy.nextStep(cause: .rotationFailed, attempt: 0, elapsedSeconds: 0),
            .retry(afterSeconds: 0.5)
        )
    }

    func testSyncIsDeferredForEveryOpenCaptureSession() {
        XCTAssertTrue(WatchCaptureRecoveryPolicy.shouldSync(hasOpenCaptureSession: false, pendingSegments: 0))
        XCTAssertFalse(WatchCaptureRecoveryPolicy.shouldSync(hasOpenCaptureSession: true, pendingSegments: 4))
        XCTAssertFalse(WatchCaptureRecoveryPolicy.shouldSync(hasOpenCaptureSession: true, pendingSegments: 5))
        XCTAssertFalse(WatchCaptureRecoveryPolicy.shouldSync(hasOpenCaptureSession: true, pendingSegments: 1000))
    }
}
