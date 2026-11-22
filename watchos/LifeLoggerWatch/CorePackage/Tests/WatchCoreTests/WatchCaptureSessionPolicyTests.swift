import Foundation
import XCTest
@testable import WatchCore

final class WatchCaptureSessionPolicyTests: XCTestCase {
    /// The bug this pins: after a system audio interruption the recorder was stopped but the
    /// audio session stayed active, so the app was never suspended and the screen never slept.
    func testInterruptedCaptureMustNotHoldAnActiveSession() {
        XCTAssertEqual(
            WatchCaptureSessionPolicy.disposition(
                hasActiveSession: true,
                isRecording: false,
                isInterrupted: true,
                stopRequested: false,
                hasOpenSession: true
            ),
            .release
        )
    }

    func testActiveCaptureKeepsTheSession() {
        XCTAssertEqual(
            WatchCaptureSessionPolicy.disposition(
                hasActiveSession: true,
                isRecording: true,
                isInterrupted: false,
                stopRequested: false,
                hasOpenSession: true
            ),
            .unchanged
        )
        XCTAssertEqual(
            WatchCaptureSessionPolicy.disposition(
                hasActiveSession: false,
                isRecording: true,
                isInterrupted: false,
                stopRequested: false,
                hasOpenSession: true
            ),
            .activate
        )
    }

    /// A rotation gap (not recording, not interrupted) still needs the session: the next
    /// segment starts immediately.
    func testRotationGapKeepsTheSessionAvailable() {
        XCTAssertEqual(
            WatchCaptureSessionPolicy.disposition(
                hasActiveSession: true,
                isRecording: false,
                isInterrupted: false,
                stopRequested: false,
                hasOpenSession: true
            ),
            .unchanged
        )
    }

    func testStoppedOrClosedSessionAlwaysReleases() {
        XCTAssertEqual(
            WatchCaptureSessionPolicy.disposition(
                hasActiveSession: true,
                isRecording: false,
                isInterrupted: false,
                stopRequested: true,
                hasOpenSession: true
            ),
            .release
        )
        XCTAssertEqual(
            WatchCaptureSessionPolicy.disposition(
                hasActiveSession: true,
                isRecording: true,
                isInterrupted: false,
                stopRequested: false,
                hasOpenSession: false
            ),
            .release
        )
        // Nothing held and nothing to do: no pointless setActive call on every foreground.
        XCTAssertEqual(
            WatchCaptureSessionPolicy.disposition(
                hasActiveSession: false,
                isRecording: false,
                isInterrupted: false,
                stopRequested: false,
                hasOpenSession: false
            ),
            .unchanged
        )
    }
}
