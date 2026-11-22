import Foundation
import XCTest
@testable import WatchCore

final class WatchStagedAudioPolicyTests: XCTestCase {
    /// An unreadable staged file used to abort recovery for the whole session and leave the
    /// app in a failed state on every launch — the recording queue could never be reached.
    /// It is set aside instead, with the bytes kept on the Watch for a later look.
    func testUnreadableStagedFileIsQuarantinedAndRecoveryContinues() {
        let outcome = WatchStagedAudioPolicy.recoveryOutcome(
            unreadableStagedFile: "segment-000001.m4a",
            underlyingError: "The operation couldn’t be completed."
        )

        guard case .quarantined(let quarantined, let reason) = outcome else {
            return XCTFail("Expected the bad staged file to be quarantined, got \(outcome)")
        }
        XCTAssertEqual(quarantined, "segment-000001.m4a")
        XCTAssertTrue(reason.contains("segment-000001.m4a"))
        XCTAssertTrue(reason.contains("The operation couldn’t be completed."))
    }

    func testQuarantineFileNameKeepsTheOriginalAndIsRecognisable() {
        let name = WatchStagedAudioPolicy.quarantineFileName(for: "segment-000012.m4a")

        XCTAssertTrue(name.hasPrefix("segment-000012.m4a"))
        XCTAssertTrue(WatchStagedAudioPolicy.isQuarantined(name))
        XCTAssertFalse(WatchStagedAudioPolicy.isQuarantined("segment-000012.m4a"))
    }

    func testStoppedSessionWithLeftoverStagedAudioStillBlocksFinalization() {
        XCTAssertEqual(
            WatchStagedAudioPolicy.sessionCompletion(
                isStopped: true,
                hasUnresolvedStagedAudio: true
            ),
            .keepOpenForRecovery
        )
        XCTAssertEqual(
            WatchStagedAudioPolicy.sessionCompletion(
                isStopped: true,
                hasUnresolvedStagedAudio: false
            ),
            .finishManifest
        )
    }

    /// Only a genuine blocker may put the recorder into the failed state; a quarantined file
    /// is a recorded, non-blocking issue and must not make the app look broken each launch.
    func testQuarantinedIssueDoesNotBlockButUnknownProblemDoes() {
        let quarantined = WatchRecoveryIssue.quarantined(
            stagedFile: "segment-000001.m4a",
            detail: "unreadable"
        )
        let blocking = WatchRecoveryIssue.blocking("staged audio has no session manifest")

        XCTAssertFalse(quarantined.blocksRecording)
        XCTAssertTrue(blocking.blocksRecording)
        XCTAssertEqual(
            WatchRecoveryIssue.quarantined(stagedFile: "a.m4a", detail: "d").statusMessage,
            "Could not read staged audio (kept on Watch as a.m4a.unreadable): d"
        )
        XCTAssertEqual(
            WatchRecoveryIssue.blocking("boom").statusMessage,
            "Could not recover all saved audio. boom"
        )
    }
}
