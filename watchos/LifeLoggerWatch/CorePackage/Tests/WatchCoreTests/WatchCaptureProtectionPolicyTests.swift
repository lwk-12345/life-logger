import XCTest
@testable import WatchCore

final class WatchCaptureProtectionPolicyTests: XCTestCase {
    func testMeterUpdateOnlyWhenRecordingActiveAndNotDimmed() {
        // Active recording in foreground and not dimmed -> meter updates
        XCTAssertTrue(
            WatchCaptureProtectionPolicy.shouldMeterUpdate(
                isRecording: true,
                isDimmed: false,
                isSceneActive: true
            )
        )

        // Dimmed in low-power mode -> meter updates must be suspended
        XCTAssertFalse(
            WatchCaptureProtectionPolicy.shouldMeterUpdate(
                isRecording: true,
                isDimmed: true,
                isSceneActive: true
            )
        )

        // App in background / inactive -> meter updates must be suspended
        XCTAssertFalse(
            WatchCaptureProtectionPolicy.shouldMeterUpdate(
                isRecording: true,
                isDimmed: false,
                isSceneActive: false
            )
        )

        // Not recording -> meter updates must be suspended
        XCTAssertFalse(
            WatchCaptureProtectionPolicy.shouldMeterUpdate(
                isRecording: false,
                isDimmed: false,
                isSceneActive: true
            )
        )
    }

    func testHoldProgressProgressionAndCompletion() {
        XCTAssertEqual(WatchCaptureProtectionPolicy.progress(forElapsed: -0.1), 0.0)
        XCTAssertEqual(WatchCaptureProtectionPolicy.progress(forElapsed: 0.0), 0.0)
        XCTAssertFalse(WatchCaptureProtectionPolicy.canTriggerStop(progress: 0.0))

        let halfProgress = WatchCaptureProtectionPolicy.progress(forElapsed: 1.0)
        XCTAssertEqual(halfProgress, 0.5, accuracy: 0.001)
        XCTAssertFalse(WatchCaptureProtectionPolicy.canTriggerStop(progress: halfProgress))
        XCTAssertEqual(WatchCaptureProtectionPolicy.remainingSeconds(forElapsed: 1.0), 1.0, accuracy: 0.001)

        let almostProgress = WatchCaptureProtectionPolicy.progress(forElapsed: 1.9)
        XCTAssertEqual(almostProgress, 0.95, accuracy: 0.001)
        XCTAssertFalse(WatchCaptureProtectionPolicy.canTriggerStop(progress: almostProgress))
        XCTAssertEqual(WatchCaptureProtectionPolicy.remainingSeconds(forElapsed: 1.9), 0.1, accuracy: 0.001)

        let completeProgress = WatchCaptureProtectionPolicy.progress(forElapsed: 2.0)
        XCTAssertEqual(completeProgress, 1.0, accuracy: 0.001)
        XCTAssertTrue(WatchCaptureProtectionPolicy.canTriggerStop(progress: completeProgress))
        XCTAssertEqual(WatchCaptureProtectionPolicy.remainingSeconds(forElapsed: 2.0), 0.0, accuracy: 0.001)

        let beyondProgress = WatchCaptureProtectionPolicy.progress(forElapsed: 3.5)
        XCTAssertEqual(beyondProgress, 1.0, accuracy: 0.001)
        XCTAssertTrue(WatchCaptureProtectionPolicy.canTriggerStop(progress: beyondProgress))
    }

    func testTouchToleranceAndDragCancellation() {
        XCTAssertTrue(WatchCaptureProtectionPolicy.isTouchWithinTolerance(dx: 0, dy: 0))
        XCTAssertTrue(WatchCaptureProtectionPolicy.isTouchWithinTolerance(dx: 15, dy: 15)) // hypot ~21.2 <= 30
        XCTAssertTrue(WatchCaptureProtectionPolicy.isTouchWithinTolerance(dx: 30, dy: 0))
        XCTAssertTrue(WatchCaptureProtectionPolicy.isTouchWithinTolerance(dx: 0, dy: 30))

        // Exceeding 30 points cancels touch to avoid accidental drags or wrist bends
        XCTAssertFalse(WatchCaptureProtectionPolicy.isTouchWithinTolerance(dx: 25, dy: 25)) // hypot ~35.3 > 30
        XCTAssertFalse(WatchCaptureProtectionPolicy.isTouchWithinTolerance(dx: 35, dy: 0))
    }

    func testTimingConstantsConformToProductSpec() {
        XCTAssertEqual(WatchCaptureProtectionPolicy.holdToStopDuration, 2.0)
        XCTAssertEqual(WatchCaptureProtectionPolicy.initialAutoDimDelay, 3.0)
        XCTAssertEqual(WatchCaptureProtectionPolicy.wakeAutoDimDelay, 5.0)
        XCTAssertEqual(WatchCaptureProtectionPolicy.meterUpdateIntervalMilliseconds, 150)
    }
}
