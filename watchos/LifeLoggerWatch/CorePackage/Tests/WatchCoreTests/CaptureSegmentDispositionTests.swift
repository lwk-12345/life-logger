import XCTest
@testable import WatchCore

final class CaptureSegmentDispositionTests: XCTestCase {
    func testSuccessfulSegmentContinuesOnlyDuringHealthyCapture() {
        XCTAssertEqual(
            CaptureSegmentDisposition.resolve(
                stateIsRecording: true,
                stopRequested: false,
                audioInterrupted: false,
                segmentSucceeded: true
            ),
            .continueRecording
        )
    }

    func testFailedSegmentFinalizesBeforeAsynchronousSessionCloseout() {
        XCTAssertEqual(
            CaptureSegmentDisposition.resolve(
                stateIsRecording: true,
                stopRequested: false,
                audioInterrupted: false,
                segmentSucceeded: false
            ),
            .finalizeSession
        )
    }

    func testInterruptedSegmentDoesNotStartAnotherRecorder() {
        XCTAssertEqual(
            CaptureSegmentDisposition.resolve(
                stateIsRecording: true,
                stopRequested: false,
                audioInterrupted: true,
                segmentSucceeded: true
            ),
            .pauseForInterruption
        )
    }

    func testInterruptedFailedSegmentAlsoPausesForInterruption() {
        XCTAssertEqual(
            CaptureSegmentDisposition.resolve(
                stateIsRecording: true,
                stopRequested: false,
                audioInterrupted: true,
                segmentSucceeded: false
            ),
            .pauseForInterruption
        )
    }

    func testExplicitStopFinalizesInsteadOfRollingOver() {
        XCTAssertEqual(
            CaptureSegmentDisposition.resolve(
                stateIsRecording: true,
                stopRequested: true,
                audioInterrupted: false,
                segmentSucceeded: true
            ),
            .finalizeSession
        )
    }
}
