import XCTest
@testable import WatchCore

final class WatchCaptureResumePolicyTests: XCTestCase {
    private func decide(exists: Bool, recording: Bool, handled: Bool, engine: Bool) -> WatchCaptureResumePolicy.Decision {
        WatchCaptureResumePolicy.decide(recorderExists: exists, recorderIsRecording: recording,
                                        recorderWasHandled: handled, engineRunning: engine)
    }

    func testEngineRunningWithoutWriterIsNotCapturing() {
        XCTAssertEqual(decide(exists: false, recording: false, handled: false, engine: true), .startNewSegment)
    }

    func testWritingWithEngineIsCapturing() {
        XCTAssertEqual(decide(exists: true, recording: true, handled: false, engine: true), .alreadyCapturing)
    }

    func testWritingWithoutEngineRevivesEngine() {
        XCTAssertEqual(decide(exists: true, recording: true, handled: false, engine: false), .reviveEngine)
    }

    func testFinishedUnhandledRecorderWaits() {
        XCTAssertEqual(decide(exists: true, recording: false, handled: false, engine: false), .waitForSegmentHandling)
    }

    func testFinishedHandledRecorderDoesNotBlockResume() {
        XCTAssertEqual(decide(exists: true, recording: false, handled: true, engine: false), .startNewSegment)
    }

    func testWatchdogHealthyOnlyWhenAllGood() {
        XCTAssertTrue(WatchCaptureResumePolicy.captureIsHealthy(engineRunning: true, recorderIsRecording: true, livenessIssueFound: false))
        XCTAssertFalse(WatchCaptureResumePolicy.captureIsHealthy(engineRunning: true, recorderIsRecording: false, livenessIssueFound: false))
        XCTAssertFalse(WatchCaptureResumePolicy.captureIsHealthy(engineRunning: true, recorderIsRecording: true, livenessIssueFound: true))
    }
}
