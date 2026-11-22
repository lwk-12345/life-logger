import XCTest
@testable import WatchCore

final class WatchSegmentTimingPolicyTests: XCTestCase {
    func testFirstSegmentStartsAtZeroDespiteSetupDelay() {
        XCTAssertEqual(
            WatchSegmentTimingPolicy.startOffset(sequence: 0, elapsedMilliseconds: 4_500),
            0
        )
    }

    func testLaterSegmentKeepsMeasuredElapsedOffset() {
        XCTAssertEqual(
            WatchSegmentTimingPolicy.startOffset(sequence: 1, elapsedMilliseconds: 60_500),
            60_500
        )
    }

    func testRecoveryReusesCommittedTimingForStagedDuplicate() {
        let committed = AudioSegment(
            sessionID: "session-a",
            sequence: 1,
            startOffsetMilliseconds: 60_500,
            durationMilliseconds: 59_900,
            localURL: URL(fileURLWithPath: "/tmp/segment-000001.m4a"),
            byteCount: 1,
            sha256: "sha256:existing"
        )

        let recovered = WatchSegmentTimingPolicy.recovery(
            sequence: 1,
            measuredDurationMilliseconds: 60_100,
            manifestSegments: [committed]
        )

        XCTAssertEqual(recovered.startOffsetMilliseconds, 60_500)
        XCTAssertEqual(recovered.durationMilliseconds, 59_900)
    }

    func testRecoveryUsesSequenceFallbackOnlyWhenNoManifestSegmentExists() {
        let recovered = WatchSegmentTimingPolicy.recovery(
            sequence: 2,
            measuredDurationMilliseconds: 59_900,
            manifestSegments: []
        )

        XCTAssertEqual(recovered.startOffsetMilliseconds, 120_000)
        XCTAssertEqual(recovered.durationMilliseconds, 59_900)
    }

    func testRecoveryFallbackNeverOverlapsPreviousCommittedSegment() {
        let previous = AudioSegment(
            sessionID: "session-a",
            sequence: 1,
            startOffsetMilliseconds: 60_500,
            durationMilliseconds: 59_900,
            localURL: URL(fileURLWithPath: "/tmp/segment-000001.m4a"),
            byteCount: 1,
            sha256: "sha256:previous"
        )

        let recovered = WatchSegmentTimingPolicy.recovery(
            sequence: 2,
            measuredDurationMilliseconds: 59_900,
            manifestSegments: [previous]
        )

        XCTAssertEqual(recovered.startOffsetMilliseconds, 120_400)
    }

    func testSessionWithUnresolvedStagedAudioMustStayOpen() {
        XCTAssertEqual(
            CaptureSessionCompletionDisposition.resolve(hasUnresolvedStagedAudio: true),
            .keepOpenForRecovery
        )
    }

    func testSessionWithoutUnresolvedStagedAudioCanFinish() {
        XCTAssertEqual(
            CaptureSessionCompletionDisposition.resolve(hasUnresolvedStagedAudio: false),
            .finishManifest
        )
    }
}
