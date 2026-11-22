import XCTest
@testable import WatchCore

final class CaptureStopDispositionTests: XCTestCase {
    func testActiveSessionWithoutRecorderMustStillFinalize() {
        XCTAssertEqual(
            CaptureStopDisposition.resolve(
                hasActiveSession: true,
                hasRecorder: false,
                recorderIsRecording: false
            ),
            .finalizeSessionWithoutRecorder
        )
    }

    func testStopWithNoActiveSessionIsIgnored() {
        XCTAssertEqual(
            CaptureStopDisposition.resolve(
                hasActiveSession: false,
                hasRecorder: false,
                recorderIsRecording: false
            ),
            .ignore
        )
    }

    func testActiveRecorderIsStoppedAndAwaited() {
        XCTAssertEqual(
            CaptureStopDisposition.resolve(
                hasActiveSession: true,
                hasRecorder: true,
                recorderIsRecording: true
            ),
            .stopRecorderAndAwaitCompletion
        )
    }

    func testStoppedRecorderIsFinalizedImmediately() {
        XCTAssertEqual(
            CaptureStopDisposition.resolve(
                hasActiveSession: true,
                hasRecorder: true,
                recorderIsRecording: false
            ),
            .finalizeStoppedRecorder
        )
    }
}
