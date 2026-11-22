import Foundation
import AVFAudio
import XCTest
@testable import WatchCore

final class SegmentLedgerTests: XCTestCase {
    func testFinalizedAudioDurationUsesSavedFrames() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("duration-\(UUID()).wav")
        defer { try? FileManager.default.removeItem(at: url) }
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 32000))
        buffer.frameLength = 32000
        buffer.floatChannelData![0].initialize(repeating: 0, count: 32000)
        do {
            let file = try AVAudioFile(forWriting: url, settings: format.settings)
            try file.write(from: buffer)
        }
        XCTAssertEqual(try FinalizedAudio.durationMilliseconds(at: url), 2000)
    }

    private var rootURL: URL!
    private var ledger: SegmentLedger!

    override func setUpWithError() throws {
        rootURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("watch-core-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: rootURL,
            withIntermediateDirectories: true
        )
        ledger = SegmentLedger(rootURL: rootURL)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: rootURL)
    }

    func testSandboxMoveRebasesPathsAndRecoversExistingStagedAudio() async throws {
        _ = try await ledger.createSession(id: "sandbox-move")
        let source = rootURL.appendingPathComponent("source.m4a")
        try Data("audio-bytes".utf8).write(to: source)
        _ = try await ledger.appendSegment(sessionID: "sandbox-move", sequence: 0,
            sourceURL: source, startOffsetMilliseconds: 0, durationMilliseconds: 1000)
        let oldRoot = rootURL!
        let movedRoot = oldRoot.deletingLastPathComponent().appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: movedRoot) }
        try FileManager.default.moveItem(at: oldRoot, to: movedRoot)
        let movedLedger = SegmentLedger(rootURL: movedRoot)
        let loaded = try await movedLedger.loadSession(id: "sandbox-move")
        let expected = movedRoot.appendingPathComponent("sessions/sandbox-move/segments/segment-000000.m4a")
        XCTAssertEqual(loaded?.segments.first?.localURL.standardizedFileURL, expected.standardizedFileURL)
        let staging = movedRoot.appendingPathComponent("staged.m4a")
        try Data("audio-bytes".utf8).write(to: staging)
        let recovered = try await movedLedger.recoverStagedSegment(sessionID: "sandbox-move", sequence: 0,
            sourceURL: staging, startOffsetMilliseconds: 0, durationMilliseconds: 1000)
        XCTAssertEqual(recovered.localURL.standardizedFileURL, expected.standardizedFileURL)
        XCTAssertFalse(FileManager.default.fileExists(atPath: oldRoot.path), "must never recreate the old sandbox")
        XCTAssertFalse(FileManager.default.fileExists(atPath: staging.path))
    }

    func testNewSessionIsPersistedWithNoSegments() async throws {
        let startedAt = Date(timeIntervalSince1970: 1_000)
        let created = try await ledger.createSession(id: "session-a", startedAt: startedAt)
        let loaded = try await ledger.loadSession(id: "session-a")

        XCTAssertEqual(created.id, "session-a")
        XCTAssertEqual(created.startedAt, startedAt)
        XCTAssertTrue(created.segments.isEmpty)
        XCTAssertEqual(loaded, created)
    }

    func testSessionListRestoresPendingQueueAfterLedgerRecreation() async throws {
        _ = try await ledger.createSession(
            id: "older-session",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        _ = try await ledger.createSession(
            id: "newer-session",
            startedAt: Date(timeIntervalSince1970: 2_000)
        )
        let source = try makeSourceFile(name: "pending.m4a", contents: "audio-pending")
        _ = try await ledger.appendSegment(
            sessionID: "older-session",
            sequence: 0,
            sourceURL: source,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )

        let relaunchedLedger = SegmentLedger(rootURL: rootURL)
        let sessions = try await relaunchedLedger.listSessions()
        let pendingCount = sessions
            .flatMap(\.segments)
            .filter { $0.uploadState != .acknowledged }
            .count

        XCTAssertEqual(sessions.map(\.id), ["newer-session", "older-session"])
        XCTAssertEqual(pendingCount, 1)
    }

    func testFinishedSessionIsPersistedAndRejectsLaterSegments() async throws {
        let startedAt = Date(timeIntervalSince1970: 1_000)
        _ = try await ledger.createSession(id: "session-a", startedAt: startedAt)
        let stoppedAt = Date(timeIntervalSince1970: 1_120)

        try await ledger.finishSession(id: "session-a", stoppedAt: stoppedAt)

        let loaded = try await ledger.loadSession(id: "session-a")
        XCTAssertEqual(loaded?.stoppedAt, stoppedAt)

        let source = try makeSourceFile(name: "late.m4a", contents: "late-audio")
        do {
            _ = try await ledger.appendSegment(
                sessionID: "session-a",
                sequence: 0,
                sourceURL: source,
                startOffsetMilliseconds: 0,
                durationMilliseconds: 60_000
            )
            XCTFail("Expected a closed recording session to reject new segments")
        } catch let error as SegmentLedgerError {
            XCTAssertEqual(error, .sessionAlreadyFinished)
        }
    }

    func testRecoveryCanRescueAudioAfterPrematureSessionFinish() async throws {
        _ = try await ledger.createSession(id: "rescue")
        try await ledger.finishSession(id: "rescue")
        let source = try makeSourceFile(name: "rescue.m4a", contents: "saved-audio")
        let segment = try await ledger.recoverStagedSegment(
            sessionID: "rescue", sequence: 0, sourceURL: source,
            startOffsetMilliseconds: 0, durationMilliseconds: 60000
        )
        XCTAssertEqual(segment.uploadState, .pending)
        let manifest = try await ledger.loadSession(id: "rescue")
        XCTAssertNotNil(manifest?.stoppedAt)
        XCTAssertEqual(manifest?.segments.count, 1)
    }

    func testSegmentsAreStoredInSequenceAndStartPending() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let firstSource = try makeSourceFile(name: "first.m4a", contents: "audio-one")
        let secondSource = try makeSourceFile(name: "second.m4a", contents: "audio-two")

        let first = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: firstSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        let second = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 1,
            sourceURL: secondSource,
            startOffsetMilliseconds: 60_000,
            durationMilliseconds: 60_000
        )
        let loaded = try await ledger.loadSession(id: "session-a")

        XCTAssertEqual(first.uploadState, .pending)
        XCTAssertEqual(second.uploadState, .pending)
        XCTAssertEqual(first.sequence, 0)
        XCTAssertEqual(second.sequence, 1)
        XCTAssertEqual(loaded?.segments.map(\.sequence), [0, 1])
        XCTAssertTrue(FileManager.default.fileExists(atPath: first.localURL.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: second.localURL.path))
    }

    func testOutOfOrderSegmentIsRejectedWithoutChangingManifest() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let source = try makeSourceFile(name: "second.m4a", contents: "audio-two")

        do {
            _ = try await ledger.appendSegment(
                sessionID: "session-a",
                sequence: 1,
                sourceURL: source,
                startOffsetMilliseconds: 60_000,
                durationMilliseconds: 60_000
            )
            XCTFail("Expected a non-contiguous sequence to be rejected")
        } catch let error as SegmentLedgerError {
            XCTAssertEqual(error, .unexpectedSequence(expected: 0, received: 1))
        }

        let loaded = try await ledger.loadSession(id: "session-a")
        XCTAssertTrue(loaded?.segments.isEmpty == true)
    }

    func testGapIsRejectedButLaterContiguousSegmentStillAppends() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let firstSource = try makeSourceFile(name: "first.m4a", contents: "audio-one")
        let skippedSource = try makeSourceFile(name: "third.m4a", contents: "audio-three")
        let secondSource = try makeSourceFile(name: "second.m4a", contents: "audio-two")

        _ = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: firstSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )

        do {
            _ = try await ledger.appendSegment(
                sessionID: "session-a",
                sequence: 2,
                sourceURL: skippedSource,
                startOffsetMilliseconds: 120_000,
                durationMilliseconds: 60_000
            )
            XCTFail("Expected a gap in the segment sequence to be rejected")
        } catch let error as SegmentLedgerError {
            XCTAssertEqual(error, .unexpectedSequence(expected: 1, received: 2))
        }

        _ = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 1,
            sourceURL: secondSource,
            startOffsetMilliseconds: 60_000,
            durationMilliseconds: 60_000
        )
        let loaded = try await ledger.loadSession(id: "session-a")

        XCTAssertEqual(loaded?.segments.map(\.sequence), [0, 1])
        XCTAssertEqual(loaded?.segments.map(\.startOffsetMilliseconds), [0, 60_000])
    }

    func testFirstSegmentMustStartAtZero() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let source = try makeSourceFile(name: "first.m4a", contents: "audio-one")

        do {
            _ = try await ledger.appendSegment(
                sessionID: "session-a",
                sequence: 0,
                sourceURL: source,
                startOffsetMilliseconds: 1,
                durationMilliseconds: 60_000
            )
            XCTFail("Expected a shifted segment start to be rejected")
        } catch let error as SegmentLedgerError {
            XCTAssertEqual(error, .invalidSegmentTiming)
        }

        let loaded = try await ledger.loadSession(id: "session-a")
        XCTAssertTrue(loaded?.segments.isEmpty == true)
    }

    func testAdjacentSegmentsAllowSmallEncoderPaddingButRejectLargeOverlap() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let firstSource = try makeSourceFile(name: "first.m4a", contents: "audio-one")
        let secondSource = try makeSourceFile(name: "second.m4a", contents: "audio-two")
        _ = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: firstSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_064
        )
        _ = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 1,
            sourceURL: secondSource,
            startOffsetMilliseconds: 60_000,
            durationMilliseconds: 60_000
        )

        let thirdSource = try makeSourceFile(name: "third.m4a", contents: "audio-three")
        do {
            _ = try await ledger.appendSegment(
                sessionID: "session-a",
                sequence: 2,
                sourceURL: thirdSource,
                startOffsetMilliseconds: 60_500,
                durationMilliseconds: 60_000
            )
            XCTFail("Expected a large segment overlap to be rejected")
        } catch let error as SegmentLedgerError {
            XCTAssertEqual(error, .invalidSegmentTiming)
        }
    }

    func testAdjacentSegmentsAllowRealGapWithoutFixedChunkLength() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let firstSource = try makeSourceFile(name: "first-gap.m4a", contents: "audio-one")
        let secondSource = try makeSourceFile(name: "second-gap.m4a", contents: "audio-two")
        _ = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: firstSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 1_000
        )
        _ = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 1,
            sourceURL: secondSource,
            startOffsetMilliseconds: 5_000,
            durationMilliseconds: 1_000
        )

        let loaded = try await ledger.loadSession(id: "session-a")
        XCTAssertEqual(loaded?.segments.map(\.startOffsetMilliseconds), [0, 5_000])
    }

    func testRecoveringStagedSegmentAddsItToDurableQueueAndRemovesStagingCopy() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let stagingDirectory = rootURL
            .appendingPathComponent("staging", isDirectory: true)
            .appendingPathComponent("session-a", isDirectory: true)
        try FileManager.default.createDirectory(
            at: stagingDirectory,
            withIntermediateDirectories: true
        )
        let stagedFile = stagingDirectory.appendingPathComponent("segment-000000.m4a")
        try Data("recovered-audio".utf8).write(to: stagedFile)

        let recovered = try await ledger.recoverStagedSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: stagedFile,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 12_000
        )
        let loaded = try await ledger.loadSession(id: "session-a")

        XCTAssertEqual(loaded?.segments, [recovered])
        XCTAssertTrue(FileManager.default.fileExists(atPath: recovered.localURL.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: stagedFile.path))
    }

    func testRecoveringSameStagedSegmentCleansUpAfterManifestWriteCrash() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let committedSource = try makeSourceFile(name: "committed.m4a", contents: "audio-one")
        let committed = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: committedSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        let stagingDirectory = rootURL
            .appendingPathComponent("staging", isDirectory: true)
            .appendingPathComponent("session-a", isDirectory: true)
        try FileManager.default.createDirectory(
            at: stagingDirectory,
            withIntermediateDirectories: true
        )
        let stagedFile = stagingDirectory.appendingPathComponent("segment-000000.m4a")
        try Data("audio-one".utf8).write(to: stagedFile)

        let recovered = try await ledger.recoverStagedSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: stagedFile,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        let loaded = try await ledger.loadSession(id: "session-a")

        XCTAssertEqual(recovered, committed)
        XCTAssertEqual(loaded?.segments, [committed])
        XCTAssertFalse(FileManager.default.fileExists(atPath: stagedFile.path))
    }

    func testRecoveringSameSegmentRejectsConflictingTimingMetadata() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let committedSource = try makeSourceFile(name: "committed.m4a", contents: "audio-one")
        _ = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: committedSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        let stagingDirectory = rootURL
            .appendingPathComponent("staging", isDirectory: true)
            .appendingPathComponent("session-a", isDirectory: true)
        try FileManager.default.createDirectory(
            at: stagingDirectory,
            withIntermediateDirectories: true
        )
        let stagedFile = stagingDirectory.appendingPathComponent("segment-000000.m4a")
        try Data("audio-one".utf8).write(to: stagedFile)

        do {
            _ = try await ledger.recoverStagedSegment(
                sessionID: "session-a",
                sequence: 0,
                sourceURL: stagedFile,
                startOffsetMilliseconds: 1_000,
                durationMilliseconds: 59_000
            )
            XCTFail("Expected conflicting recovery timing to be rejected")
        } catch let error as SegmentLedgerError {
            XCTAssertEqual(error, .invalidSegmentTiming)
        }

        let loaded = try await ledger.loadSession(id: "session-a")
        XCTAssertEqual(loaded?.segments.first?.startOffsetMilliseconds, 0)
        XCTAssertEqual(loaded?.segments.first?.durationMilliseconds, 60_000)
        XCTAssertTrue(FileManager.default.fileExists(atPath: stagedFile.path))
    }

    func testRecoveryAdoptsFinalFileLeftBeforeManifestCommit() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let segmentsDirectory = rootURL
            .appendingPathComponent("sessions", isDirectory: true)
            .appendingPathComponent("session-a", isDirectory: true)
            .appendingPathComponent("segments", isDirectory: true)
        let finalURL = segmentsDirectory.appendingPathComponent("segment-000000.m4a")
        try Data("recovered-audio".utf8).write(to: finalURL)
        let stagingDirectory = rootURL
            .appendingPathComponent("staging", isDirectory: true)
            .appendingPathComponent("session-a", isDirectory: true)
        try FileManager.default.createDirectory(
            at: stagingDirectory,
            withIntermediateDirectories: true
        )
        let stagedFile = stagingDirectory.appendingPathComponent("segment-000000.m4a")
        try Data("recovered-audio".utf8).write(to: stagedFile)

        let recovered = try await ledger.recoverStagedSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: stagedFile,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 12_000
        )
        let loaded = try await ledger.loadSession(id: "session-a")

        XCTAssertEqual(loaded?.segments, [recovered])
        XCTAssertEqual(try Data(contentsOf: finalURL), Data("recovered-audio".utf8))
        XCTAssertFalse(FileManager.default.fileExists(atPath: stagedFile.path))
    }

    func testStagedDuplicateRestoresMissingLedgerAudioFile() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let committedSource = try makeSourceFile(name: "committed.m4a", contents: "audio-one")
        let committed = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: committedSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        try FileManager.default.removeItem(at: committed.localURL)
        let stagingDirectory = rootURL
            .appendingPathComponent("staging", isDirectory: true)
            .appendingPathComponent("session-a", isDirectory: true)
        try FileManager.default.createDirectory(
            at: stagingDirectory,
            withIntermediateDirectories: true
        )
        let stagedFile = stagingDirectory.appendingPathComponent("segment-000000.m4a")
        try Data("audio-one".utf8).write(to: stagedFile)

        let recovered = try await ledger.recoverStagedSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: stagedFile,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )

        XCTAssertEqual(recovered, committed)
        XCTAssertEqual(try Data(contentsOf: committed.localURL), Data("audio-one".utf8))
        XCTAssertFalse(FileManager.default.fileExists(atPath: stagedFile.path))
    }

    func testMismatchedStagedDuplicateIsPreservedForManualRecovery() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let committedSource = try makeSourceFile(name: "committed.m4a", contents: "audio-one")
        _ = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: committedSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        let stagingDirectory = rootURL
            .appendingPathComponent("staging", isDirectory: true)
            .appendingPathComponent("session-a", isDirectory: true)
        try FileManager.default.createDirectory(
            at: stagingDirectory,
            withIntermediateDirectories: true
        )
        let stagedFile = stagingDirectory.appendingPathComponent("segment-000000.m4a")
        try Data("different-audio".utf8).write(to: stagedFile)

        do {
            _ = try await ledger.recoverStagedSegment(
                sessionID: "session-a",
                sequence: 0,
                sourceURL: stagedFile,
                startOffsetMilliseconds: 0,
                durationMilliseconds: 60_000
            )
            XCTFail("Expected conflicting staged audio to be retained")
        } catch let error as SegmentLedgerError {
            XCTAssertEqual(error, .checksumMismatch)
        }

        XCTAssertTrue(FileManager.default.fileExists(atPath: stagedFile.path))
    }

    func testMismatchedAckKeepsSegmentAndAudioFile() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let source = try makeSourceFile(name: "first.m4a", contents: "audio-one")
        let segment = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: source,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )

        do {
            try await ledger.acknowledge(
                sessionID: "session-a",
                sequence: 0,
                sha256: "sha256:wrong"
            )
            XCTFail("Expected a mismatched ACK to be rejected")
        } catch let error as SegmentLedgerError {
            XCTAssertEqual(error, .checksumMismatch)
        }

        let loaded = try await ledger.loadSession(id: "session-a")
        XCTAssertEqual(loaded?.segments.first?.uploadState, .pending)
        XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))
    }

    func testMatchingMacAckIsPersistedBeforeLocalAudioIsRemoved() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let source = try makeSourceFile(name: "first.m4a", contents: "audio-one")
        let segment = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: source,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )

        try await ledger.acknowledge(
            sessionID: "session-a",
            sequence: 0,
            sha256: segment.sha256
        )

        let loaded = try await ledger.loadSession(id: "session-a")
        XCTAssertEqual(loaded?.segments.first?.uploadState, .acknowledged)
        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
    }

    func testDuplicateAckCleansUpAudioLeftAfterInterruptedDeletion() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let source = try makeSourceFile(name: "first.m4a", contents: "audio-one")
        let segment = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: source,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        try await ledger.acknowledge(
            sessionID: "session-a",
            sequence: 0,
            sha256: segment.sha256
        )

        try Data("audio-one".utf8).write(to: segment.localURL)
        try await ledger.acknowledge(
            sessionID: "session-a",
            sequence: 0,
            sha256: segment.sha256
        )

        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
    }

    func testFailedSegmentCanReturnToPendingForRetry() async throws {
        _ = try await ledger.createSession(
            id: "session-a",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let source = try makeSourceFile(name: "first.m4a", contents: "audio-one")
        let segment = try await ledger.appendSegment(
            sessionID: "session-a",
            sequence: 0,
            sourceURL: source,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )

        try await ledger.markFailed(sessionID: "session-a", sequence: 0)
        try await ledger.retry(sessionID: "session-a", sequence: 0)

        let loaded = try await ledger.loadSession(id: "session-a")
        XCTAssertEqual(loaded?.segments.first?.uploadState, .pending)
        XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))
    }

    func testFinishingRecoveredSessionUsesTheAudioTimelineNotTheRecoveryMoment() async throws {
        let startedAt = Date(timeIntervalSince1970: 1_000)
        _ = try await ledger.createSession(id: "session-late", startedAt: startedAt)
        for (sequence, offset) in [(0, 0), (1, 90_000)] {
            let source = try makeSourceFile(
                name: "late-\(sequence).m4a",
                contents: "audio-late-\(sequence)"
            )
            _ = try await ledger.appendSegment(
                sessionID: "session-late",
                sequence: sequence,
                sourceURL: source,
                startOffsetMilliseconds: Int64(offset),
                durationMilliseconds: sequence == 0 ? 90_000 : 30_000
            )
        }

        // Finalized long after the audio ended (the recovery path), with no explicit stop.
        try await ledger.finishSession(id: "session-late")

        let loaded = try await ledger.loadSession(id: "session-late")
        XCTAssertEqual(
            loaded?.stoppedAt,
            startedAt.addingTimeInterval(120),
            "A recovered session must be closed at the end of its audio, not at the recovery moment"
        )
    }

    func testFinishingSessionWithoutAudioStillClosesItNow() async throws {
        _ = try await ledger.createSession(
            id: "session-empty",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )

        try await ledger.finishSession(id: "session-empty")

        let loaded = try await ledger.loadSession(id: "session-empty")
        let stoppedAt = try XCTUnwrap(loaded?.stoppedAt)
        XCTAssertEqual(
            stoppedAt.timeIntervalSinceNow,
            0,
            accuracy: 5,
            "With no audio to derive from, the close time stays the current moment"
        )
    }

    private func makeSourceFile(name: String, contents: String) throws -> URL {
        let url = rootURL.appendingPathComponent(name)
        try Data(contents.utf8).write(to: url)
        return url
    }
}
