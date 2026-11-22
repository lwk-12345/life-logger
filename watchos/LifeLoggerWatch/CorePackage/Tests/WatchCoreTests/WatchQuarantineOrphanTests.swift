import Foundation
import XCTest
@testable import WatchCore

/// The orphan check at the end of recovery used to treat *any* non-empty staging directory
/// without a session manifest as unreachable audio and put the recorder into `.failed`. A directory
/// whose only remaining entries are quarantined files holds bytes that are already set aside, already
/// visible and already removable from the 坏文件 screen — failing the recorder over that rubbish strands
/// the app for a reason the user cannot act on with the recorder itself.
final class WatchQuarantineOrphanTests: XCTestCase {
    // MARK: - What still blocks

    func testDirectoryWithOnlyQuarantinedFilesDoesNotBlock() {
        XCTAssertFalse(
            WatchQuarantinePolicy.blocksRecovery(contents: ["segment-000002.m4a.unreadable"])
        )
    }

    func testDirectoryWithSeveralQuarantinedFilesDoesNotBlock() {
        XCTAssertFalse(
            WatchQuarantinePolicy.blocksRecovery(
                contents: [
                    "segment-000002.m4a.unreadable",
                    "segment-000003.m4a.unreadable",
                ]
            )
        )
    }

    /// The real case this check exists for: audio the Watch still owns but can no longer tie to a
    /// session. Losing sight of it is losing hours of recording, so it must keep blocking.
    func testDirectoryWithRecoverableAudioStillBlocks() {
        XCTAssertTrue(
            WatchQuarantinePolicy.blocksRecovery(
                contents: [
                    "segment-000002.m4a.unreadable",
                    "segment-000003.m4a",
                ]
            )
        )
    }

    /// An empty directory is the normal end state of a finished session and never blocks.
    func testEmptyDirectoryDoesNotBlock() {
        XCTAssertFalse(WatchQuarantinePolicy.blocksRecovery(contents: []))
    }

    /// Anything recovery cannot classify is treated as recovery-worthy, as it was before: this change
    /// only whitelists the one extension recovery itself created.
    func testUnrecognisedEntriesStillBlock() {
        XCTAssertTrue(WatchQuarantinePolicy.blocksRecovery(contents: ["manifest.json"]))
    }

    // MARK: - Keeping the two paths in step

    /// The extension the quarantine path writes and the extension this check skips must be the same
    /// string: if one drifts, quarantined audio silently becomes a blocker again.
    func testBlockingCheckUsesTheSameExtensionAsQuarantine() {
        let quarantined = WatchStagedAudioPolicy.quarantineFileName(for: "segment-000001.m4a")

        XCTAssertTrue(WatchStagedAudioPolicy.isQuarantined(quarantined))
        XCTAssertFalse(WatchQuarantinePolicy.blocksRecovery(contents: [quarantined]))
    }
}
