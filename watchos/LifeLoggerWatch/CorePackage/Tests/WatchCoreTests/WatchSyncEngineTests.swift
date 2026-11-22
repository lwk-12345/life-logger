import Foundation
import XCTest
@testable import WatchCore

final class WatchSyncEngineTests: XCTestCase {
    /// The recording UI counts the entire outbox, including finished older recordings.
    /// Starting a fresh recording must not make those seven segments unreachable.
    func testCaptureUploadDrainsSevenHistoricalSegmentsBeforeNewRecordingSeals() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        _ = try await ledger.createSession(id: "older-recording")
        for sequence in 0..<7 {
            let source = root.appendingPathComponent("old-\(sequence).m4a")
            try Data("old audio \(sequence)".utf8).write(to: source)
            _ = try await ledger.appendSegment(
                sessionID: "older-recording", sequence: sequence, sourceURL: source,
                startOffsetMilliseconds: Int64(sequence * 60_000), durationMilliseconds: 60_000
            )
        }
        try await ledger.finishSession(id: "older-recording")
        _ = try await ledger.createSession(id: "fresh-recording")
        let transport = FakeWatchTransport()
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        let report = try await engine.syncSealedSegments(sessionID: "fresh-recording")
        let old = try await ledger.loadSession(id: "older-recording")
        let fresh = try await ledger.loadSession(id: "fresh-recording")
        let counts = await transport.callCounts()

        XCTAssertEqual(report.segmentsAcknowledged, 7, "The seven segments shown on the button must actually upload")
        XCTAssertEqual(counts.upload, 7)
        XCTAssertEqual(counts.complete, 0, "Capture transport must defer /complete until recording stops")
        XCTAssertNil(old?.macJobID)
        XCTAssertTrue(old?.segments.allSatisfy { $0.uploadState == .acknowledged } == true)
        XCTAssertNil(fresh?.stoppedAt, "Uploading must leave the active recording open")
        XCTAssertTrue(fresh?.segments.isEmpty == true)

        // Once capture stops, ordinary sync closes the old session without resending audio.
        _ = try await engine.syncPending()
        let finalCounts = await transport.callCounts()
        let finalized = try await ledger.loadSession(id: "older-recording")
        XCTAssertEqual(finalCounts.upload, 7)
        XCTAssertEqual(finalCounts.complete, 1)
        XCTAssertEqual(finalized?.macJobID, "job-watch-001")
    }

    func testActiveSessionCanUploadSealedSegmentsWithoutCompletingSession() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        let sessionID = "watch-live-sealed"
        _ = try await ledger.createSession(id: sessionID)
        var localURLs: [URL] = []
        for sequence in 0..<3 {
            let source = root.appendingPathComponent("live-\(sequence).m4a")
            try Data("live segment \(sequence)".utf8).write(to: source)
            let segment = try await ledger.appendSegment(
                sessionID: sessionID,
                sequence: sequence,
                sourceURL: source,
                startOffsetMilliseconds: Int64(sequence * 60_000),
                durationMilliseconds: 60_000
            )
            localURLs.append(segment.localURL)
        }
        let transport = FakeWatchTransport()
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        let report = try await engine.syncSealedSegments(sessionID: sessionID)
        let session = try await ledger.loadSession(id: sessionID)
        let counts = await transport.callCounts()

        XCTAssertEqual(report.segmentsAcknowledged, 3)
        XCTAssertEqual(report.sessionsCompleted, 0)
        XCTAssertEqual(counts.register, 1)
        XCTAssertEqual(counts.upload, 3)
        XCTAssertEqual(counts.complete, 0)
        XCTAssertNil(session?.stoppedAt)
        XCTAssertTrue(session?.segments.allSatisfy { $0.uploadState == .acknowledged } == true)
        XCTAssertTrue(localURLs.allSatisfy { !FileManager.default.fileExists(atPath: $0.path) })
    }

    func testActiveSessionRetainsSegmentWhenMacAckDoesNotMatch() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        _ = try await ledger.createSession(id: "watch-live-bad-ack")
        let source = root.appendingPathComponent("live.m4a")
        try Data("live segment".utf8).write(to: source)
        let segment = try await ledger.appendSegment(
            sessionID: "watch-live-bad-ack",
            sequence: 0,
            sourceURL: source,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        let transport = FakeWatchTransport(returnWrongChecksum: true)
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        let report = try await engine.syncSealedSegments(sessionID: segment.sessionID)
        let session = try await ledger.loadSession(id: segment.sessionID)
        let counts = await transport.callCounts()

        XCTAssertEqual(report.sessionsCompleted, 0)
        XCTAssertEqual(report.failures.count, 1)
        XCTAssertEqual(counts.complete, 0)
        XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))
        XCTAssertNotEqual(session?.segments.first?.uploadState, .acknowledged)
    }

    func testMatchingMacAckDeletesSegmentAndStoresTheJobID() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: ledger, root: root)
        let transport = FakeWatchTransport()
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        let report = try await engine.syncPending()
        let session = try await ledger.loadSession(id: segment.sessionID)

        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(session?.macJobID, "job-watch-001")
        XCTAssertEqual(session?.segments.first?.uploadState, .acknowledged)
        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
        let counts = await transport.callCounts()
        XCTAssertEqual(counts.register, 1)
        XCTAssertEqual(counts.upload, 1)
        XCTAssertEqual(counts.complete, 1)
    }

    func testCompletedSessionIsIdempotentOnLaterSync() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        _ = try await makeFinishedSession(ledger: ledger, root: root)
        let transport = FakeWatchTransport()
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        let firstReport = try await engine.syncPending()
        let countsAfterFirstSync = await transport.callCounts()
        let secondReport = try await engine.syncPending()
        let countsAfterSecondSync = await transport.callCounts()

        XCTAssertEqual(firstReport.segmentsAcknowledged, 1)
        XCTAssertEqual(firstReport.sessionsCompleted, 1)
        XCTAssertEqual(secondReport.segmentsAcknowledged, 0)
        XCTAssertEqual(secondReport.sessionsCompleted, 0)
        XCTAssertEqual(countsAfterSecondSync.register, countsAfterFirstSync.register)
        XCTAssertEqual(countsAfterSecondSync.upload, countsAfterFirstSync.upload)
        XCTAssertEqual(countsAfterSecondSync.complete, countsAfterFirstSync.complete)
    }

    func testCompletedSessionRemainsNoOpAfterLedgerRecreation() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let firstLedger = SegmentLedger(rootURL: root)
        _ = try await makeFinishedSession(ledger: firstLedger, root: root)
        let firstTransport = FakeWatchTransport()
        let firstEngine = WatchSyncEngine(ledger: firstLedger, transport: firstTransport)

        let firstReport = try await firstEngine.syncPending()
        XCTAssertEqual(firstReport.segmentsAcknowledged, 1)
        XCTAssertEqual(firstReport.sessionsCompleted, 1)

        let restoredLedger = SegmentLedger(rootURL: root)
        let restoredTransport = FakeWatchTransport()
        let restoredEngine = WatchSyncEngine(
            ledger: restoredLedger,
            transport: restoredTransport
        )
        let restoredReport = try await restoredEngine.syncPending()
        let restoredCounts = await restoredTransport.callCounts()

        XCTAssertEqual(restoredReport.segmentsAcknowledged, 0)
        XCTAssertEqual(restoredReport.sessionsCompleted, 0)
        XCTAssertEqual(restoredCounts.register, 0)
        XCTAssertEqual(restoredCounts.upload, 0)
        XCTAssertEqual(restoredCounts.complete, 0)
    }

    func testCompletedAndPendingSessionsOnlySyncPendingAfterRestart() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let firstLedger = SegmentLedger(rootURL: root)
        _ = try await makeFinishedSession(ledger: firstLedger, root: root)
        let firstTransport = FakeWatchTransport()
        let firstEngine = WatchSyncEngine(ledger: firstLedger, transport: firstTransport)
        _ = try await firstEngine.syncPending()

        _ = try await firstLedger.createSession(
            id: "watch-session-002",
            startedAt: Date(timeIntervalSince1970: 2_000)
        )
        let source = root.appendingPathComponent("source-002.m4a")
        try Data("second session audio".utf8).write(to: source)
        _ = try await firstLedger.appendSegment(
            sessionID: "watch-session-002",
            sequence: 0,
            sourceURL: source,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        try await firstLedger.finishSession(
            id: "watch-session-002",
            stoppedAt: Date(timeIntervalSince1970: 2_060)
        )

        let restoredLedger = SegmentLedger(rootURL: root)
        let restoredTransport = FakeWatchTransport()
        let restoredEngine = WatchSyncEngine(
            ledger: restoredLedger,
            transport: restoredTransport
        )
        let report = try await restoredEngine.syncPending()
        let counts = await restoredTransport.callCounts()
        let completed = try await restoredLedger.loadSession(id: "watch-session-002")

        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(counts.register, 1)
        XCTAssertEqual(counts.upload, 1)
        XCTAssertEqual(counts.complete, 1)
        XCTAssertEqual(completed?.segments.first?.uploadState, .acknowledged)
    }

    func testCompletedAndFailedSessionsOnlyRetryFailedAfterRestart() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let firstLedger = SegmentLedger(rootURL: root)
        _ = try await makeFinishedSession(ledger: firstLedger, root: root)
        let firstTransport = FakeWatchTransport()
        let firstEngine = WatchSyncEngine(ledger: firstLedger, transport: firstTransport)
        _ = try await firstEngine.syncPending()

        _ = try await firstLedger.createSession(
            id: "watch-session-failed-002",
            startedAt: Date(timeIntervalSince1970: 2_500)
        )
        let source = root.appendingPathComponent("source-failed-002.m4a")
        try Data("failed second session audio".utf8).write(to: source)
        let segment = try await firstLedger.appendSegment(
            sessionID: "watch-session-failed-002",
            sequence: 0,
            sourceURL: source,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        try await firstLedger.finishSession(
            id: "watch-session-failed-002",
            stoppedAt: Date(timeIntervalSince1970: 2_560)
        )
        try await firstLedger.markFailed(
            sessionID: segment.sessionID,
            sequence: segment.sequence
        )

        let restoredLedger = SegmentLedger(rootURL: root)
        let restoredTransport = FakeWatchTransport()
        let restoredEngine = WatchSyncEngine(
            ledger: restoredLedger,
            transport: restoredTransport
        )
        let report = try await restoredEngine.syncPending()
        let counts = await restoredTransport.callCounts()
        let recovered = try await restoredLedger.loadSession(id: "watch-session-failed-002")

        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(counts.register, 1)
        XCTAssertEqual(counts.upload, 1)
        XCTAssertEqual(counts.complete, 1)
        XCTAssertEqual(recovered?.segments.first?.uploadState, .acknowledged)
    }

    func testMultiplePendingSessionsKeepNewestFirstOrderAfterRestart() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)

        for (id, startedAt) in [
            ("watch-session-older", Date(timeIntervalSince1970: 4_000)),
            ("watch-session-newer", Date(timeIntervalSince1970: 5_000)),
        ] {
            _ = try await ledger.createSession(id: id, startedAt: startedAt)
            let source = root.appendingPathComponent("\(id).m4a")
            try Data(id.utf8).write(to: source)
            _ = try await ledger.appendSegment(
                sessionID: id,
                sequence: 0,
                sourceURL: source,
                startOffsetMilliseconds: 0,
                durationMilliseconds: 60_000
            )
            try await ledger.finishSession(
                id: id,
                stoppedAt: startedAt.addingTimeInterval(60)
            )
        }

        let restoredLedger = SegmentLedger(rootURL: root)
        let transport = FakeWatchTransport()
        let engine = WatchSyncEngine(ledger: restoredLedger, transport: transport)
        let report = try await engine.syncPending()
        let registeredOrder = await transport.registeredSessionOrder()

        XCTAssertEqual(report.sessionsRegistered, 2)
        XCTAssertEqual(report.segmentsAcknowledged, 2)
        XCTAssertEqual(report.sessionsCompleted, 2)
        XCTAssertEqual(registeredOrder, ["watch-session-newer", "watch-session-older"])
    }

    func testNewerSessionFailureDoesNotSkipAheadToOlderSession() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)

        for (id, startedAt) in [
            ("watch-session-older-failfast", Date(timeIntervalSince1970: 6_000)),
            ("watch-session-newer-failfast", Date(timeIntervalSince1970: 7_000)),
        ] {
            _ = try await ledger.createSession(id: id, startedAt: startedAt)
            let source = root.appendingPathComponent("\(id).m4a")
            try Data(id.utf8).write(to: source)
            _ = try await ledger.appendSegment(
                sessionID: id,
                sequence: 0,
                sourceURL: source,
                startOffsetMilliseconds: 0,
                durationMilliseconds: 60_000
            )
            try await ledger.finishSession(
                id: id,
                stoppedAt: startedAt.addingTimeInterval(60)
            )
        }

        // One session keeps failing for good (a permanently rejected segment behaves
        // exactly like this). The queue must stay ordered, but a dead session must not
        // hold every later recording hostage — that is how hours of audio sat unprocessed.
        let transport = FakeWatchTransport(failUploadAttempts: 99)
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)
        let report = try await engine.syncPending()

        XCTAssertEqual(report.failures.count, 2)
        XCTAssertEqual(report.sessionsCompleted, 0)
        XCTAssertTrue(report.failures.allSatisfy { $0.sessionID.hasSuffix("-failfast") })

        // Order is preserved: the newest session is attempted first, and the older one
        // is still attempted in the same pass instead of being skipped forever.
        let orderAfterFailure = await transport.registeredSessionOrder()
        XCTAssertEqual(
            orderAfterFailure,
            ["watch-session-newer-failfast", "watch-session-older-failfast"]
        )

        // With the transport healthy again the whole backlog drains in one pass.
        let healthyTransport = FakeWatchTransport()
        let healthyEngine = WatchSyncEngine(ledger: ledger, transport: healthyTransport)
        let recovered = try await healthyEngine.syncPending()

        XCTAssertTrue(recovered.failures.isEmpty)
        XCTAssertEqual(recovered.segmentsAcknowledged, 2)
        XCTAssertEqual(recovered.sessionsCompleted, 2)
    }

    func testNewerSessionCompletionFailureDoesNotSkipAheadToOlderSession() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)

        for (id, startedAt) in [
            ("watch-session-older-complete-fail", Date(timeIntervalSince1970: 8_000)),
            ("watch-session-newer-complete-fail", Date(timeIntervalSince1970: 9_000)),
        ] {
            _ = try await ledger.createSession(id: id, startedAt: startedAt)
            let source = root.appendingPathComponent("\(id).m4a")
            try Data(id.utf8).write(to: source)
            _ = try await ledger.appendSegment(
                sessionID: id,
                sequence: 0,
                sourceURL: source,
                startOffsetMilliseconds: 0,
                durationMilliseconds: 60_000
            )
            try await ledger.finishSession(
                id: id,
                stoppedAt: startedAt.addingTimeInterval(60)
            )
        }

        let transport = FakeWatchTransport(failCompleteAttempts: 1)
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)
        let failure = try await syncFailure(engine, expecting: .sessionCompletionFailed,
            message: "Expected newest session completion to fail", alternative: "Expected sessionCompletionFailed")
        XCTAssertFalse(failure.isEmpty)

        // The newest session failed to complete, but the older one behind it still got its
        // turn in the same pass: its segments are all acknowledged and it completed.
        let orderAfterFailure = await transport.registeredSessionOrder()
        XCTAssertEqual(
            orderAfterFailure,
            ["watch-session-newer-complete-fail", "watch-session-older-complete-fail"]
        )

        let report = try await engine.syncPending()
        let recoveredOrder = await transport.registeredSessionOrder()

        XCTAssertEqual(report.segmentsAcknowledged, 0)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(
            recoveredOrder,
            [
                "watch-session-newer-complete-fail",
                "watch-session-older-complete-fail",
                "watch-session-newer-complete-fail",
            ]
        )
    }

    func testNewerCompletionFailureSurvivesRestartBeforeOlderSession() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let firstLedger = SegmentLedger(rootURL: root)

        for (id, startedAt) in [
            ("watch-session-older-complete-restart", Date(timeIntervalSince1970: 10_000)),
            ("watch-session-newer-complete-restart", Date(timeIntervalSince1970: 11_000)),
        ] {
            _ = try await firstLedger.createSession(id: id, startedAt: startedAt)
            let source = root.appendingPathComponent("\(id).m4a")
            try Data(id.utf8).write(to: source)
            _ = try await firstLedger.appendSegment(
                sessionID: id,
                sequence: 0,
                sourceURL: source,
                startOffsetMilliseconds: 0,
                durationMilliseconds: 60_000
            )
            try await firstLedger.finishSession(
                id: id,
                stoppedAt: startedAt.addingTimeInterval(60)
            )
        }

        let firstTransport = FakeWatchTransport(failCompleteAttempts: 1)
        let firstEngine = WatchSyncEngine(ledger: firstLedger, transport: firstTransport)
        let failure = try await syncFailure(firstEngine, expecting: .sessionCompletionFailed,
            message: "Expected newest session completion to fail before restart", alternative: "Expected sessionCompletionFailed")
        XCTAssertFalse(failure.isEmpty)

        let persistedNewer = try await firstLedger.loadSession(
            id: "watch-session-newer-complete-restart"
        )
        let persistedOlder = try await firstLedger.loadSession(
            id: "watch-session-older-complete-restart"
        )
        // The older session no longer waits behind the failed one: it was drained — its
        // segment acknowledged and the session completed — in the same pass.
        XCTAssertEqual(persistedNewer?.segments.first?.uploadState, .acknowledged)
        XCTAssertEqual(persistedOlder?.segments.first?.uploadState, .acknowledged)
        XCTAssertNil(persistedNewer?.macJobID)
        XCTAssertNotNil(persistedOlder?.macJobID)

        let restoredLedger = SegmentLedger(rootURL: root)
        let restoredTransport = FakeWatchTransport()
        let restoredEngine = WatchSyncEngine(
            ledger: restoredLedger,
            transport: restoredTransport
        )
        let report = try await restoredEngine.syncPending()
        let restoredCounts = await restoredTransport.callCounts()
        let restoredOrder = await restoredTransport.registeredSessionOrder()

        XCTAssertEqual(report.segmentsAcknowledged, 0)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(restoredCounts.upload, 0)
        XCTAssertEqual(
            restoredOrder,
            ["watch-session-newer-complete-restart"]
        )
    }

    /// A completion handed to the background session is not a failure: it must not be
    /// reported to the user, must not be retried in the same pass, and must leave the session
    /// unfinished locally until the Mac's answer arrives through the delegate.
    func testQueuedCompletionIsNotTreatedAsFailure() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: ledger, root: root)
        let transport = FakeWatchTransport(queuesCompletion: true)
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        let report = try await engine.syncPending()

        XCTAssertTrue(report.failures.isEmpty)
        XCTAssertEqual(report.sessionsAwaitingCompletion, 1)
        XCTAssertEqual(report.sessionsCompleted, 0)
        let state = try await ledger.loadSession(id: segment.sessionID)
        XCTAssertNil(state?.macJobID)
        XCTAssertEqual(state?.segments.first?.uploadState, .acknowledged)
    }

    /// The user's rule: audio that survived must be uploaded. A segment whose file is really
    /// gone is skipped and *reported*, and the session still closes on the Mac with the rest —
    /// instead of retrying a file that will never come back.
    func testSessionWithOneLostSegmentStillUploadsTheRestAndCompletes() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        _ = try await ledger.createSession(
            id: "watch-session-lost-segment",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        var segments: [AudioSegment] = []
        for sequence in 0..<3 {
            let source = root.appendingPathComponent("part-\(sequence).m4a")
            try Data("audio-\(sequence)".utf8).write(to: source)
            segments.append(
                try await ledger.appendSegment(
                    sessionID: "watch-session-lost-segment",
                    sequence: sequence,
                    sourceURL: source,
                    startOffsetMilliseconds: Int64(sequence) * 60_000,
                    durationMilliseconds: 60_000
                )
            )
        }
        try await ledger.finishSession(
            id: "watch-session-lost-segment",
            stoppedAt: Date(timeIntervalSince1970: 1_180)
        )

        // One recording is destroyed after the fact; the other two are perfectly fine.
        let lost = segments[1]
        try FileManager.default.removeItem(at: lost.localURL)

        let transport = FakeWatchTransport()
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        // First pass: the missing file fails locally, the other two are acknowledged.
        let first = try await engine.syncPending()
        let uploadedFirst = await transport.uploadCount()
        XCTAssertEqual(uploadedFirst, 2, "the two surviving segments must still be uploaded")
        XCTAssertTrue(
            first.failures.contains { failure in
                if case .segmentUploadFailed = failure.error { return true }
                return false
            }
        )

        // Second pass: report the missing sequence rather than falsely completing a partial session.
        let second = try await engine.syncPending()
        XCTAssertEqual(second.sessionsCompleted, 0)
        XCTAssertTrue(
            second.failures.contains { failure in
                if case .segmentsMissingLocally(let sequences) = failure.error {
                    return sequences == [1]
                }
                return false
            }
        )
        let stored = try await ledger.loadSession(id: "watch-session-lost-segment")
        XCTAssertNil(stored?.macJobID)
    }

    func testTransportFailureKeepsAudioAndCanRetryLater() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: ledger, root: root)
        let transport = FakeWatchTransport(failFirstUpload: true)
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        // A failed network attempt must not remove the local segment.
        let failure = try await syncFailure(
            engine,
            expecting: .segmentUploadFailed,
            message: "Expected the first upload to fail",
            alternative: "Expected segmentUploadFailed"
        )
        XCTAssertFalse(failure.isEmpty)

        let afterFailure = try await ledger.loadSession(id: segment.sessionID)
        XCTAssertEqual(afterFailure?.segments.first?.uploadState, .failed)
        XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))
        XCTAssertNil(afterFailure?.macJobID)

        let report = try await engine.syncPending()
        let afterRetry = try await ledger.loadSession(id: segment.sessionID)
        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(afterRetry?.segments.first?.uploadState, .acknowledged)
        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
    }

    func testFailedUploadSurvivesLedgerRecreationAndRetries() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let firstLedger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: firstLedger, root: root)
        let firstEngine = WatchSyncEngine(
            ledger: firstLedger,
            transport: FakeWatchTransport(failFirstUpload: true)
        )

        let failure = try await syncFailure(firstEngine, expecting: .segmentUploadFailed,
            message: "Expected upload failure before app restart", alternative: "Expected segmentUploadFailed")
        XCTAssertFalse(failure.isEmpty)

        let persisted = try await firstLedger.loadSession(id: segment.sessionID)
        XCTAssertEqual(persisted?.segments.first?.uploadState, .failed)
        XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))

        let restoredLedger = SegmentLedger(rootURL: root)
        let restoredTransport = FakeWatchTransport()
        let restoredEngine = WatchSyncEngine(
            ledger: restoredLedger,
            transport: restoredTransport
        )
        let report = try await restoredEngine.syncPending()
        let counts = await restoredTransport.callCounts()
        let completed = try await restoredLedger.loadSession(id: segment.sessionID)

        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(counts.upload, 1)
        XCTAssertEqual(completed?.segments.first?.uploadState, .acknowledged)
        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
    }

    func testFirstFailedThenSecondPendingKeepsSequenceOrderAfterRestart() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let firstLedger = SegmentLedger(rootURL: root)
        _ = try await firstLedger.createSession(
            id: "watch-session-failed-ordered",
            startedAt: Date(timeIntervalSince1970: 3_000)
        )
        let firstSource = root.appendingPathComponent("failed-ordered-0.m4a")
        let secondSource = root.appendingPathComponent("failed-ordered-1.m4a")
        try Data("failed ordered zero".utf8).write(to: firstSource)
        try Data("failed ordered one".utf8).write(to: secondSource)
        _ = try await firstLedger.appendSegment(
            sessionID: "watch-session-failed-ordered",
            sequence: 0,
            sourceURL: firstSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        _ = try await firstLedger.appendSegment(
            sessionID: "watch-session-failed-ordered",
            sequence: 1,
            sourceURL: secondSource,
            startOffsetMilliseconds: 60_000,
            durationMilliseconds: 60_000
        )
        try await firstLedger.finishSession(
            id: "watch-session-failed-ordered",
            stoppedAt: Date(timeIntervalSince1970: 3_120)
        )

        let failingTransport = FakeWatchTransport(failFirstUpload: true)
        let firstEngine = WatchSyncEngine(ledger: firstLedger, transport: failingTransport)
        let failure = try await syncFailure(firstEngine, expecting: .segmentUploadFailed,
            message: "Expected first segment upload failure before restart", alternative: "Expected segmentUploadFailed")
        XCTAssertFalse(failure.isEmpty)

        let persisted = try await firstLedger.loadSession(id: "watch-session-failed-ordered")
        XCTAssertEqual(persisted?.segments.map(\.uploadState), [.failed, .pending])

        let restoredLedger = SegmentLedger(rootURL: root)
        let restoredTransport = FakeWatchTransport()
        let restoredEngine = WatchSyncEngine(
            ledger: restoredLedger,
            transport: restoredTransport
        )
        let report = try await restoredEngine.syncPending()
        let restoredOrder = await restoredTransport.uploadedSequenceOrder()

        XCTAssertEqual(report.segmentsAcknowledged, 2)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(restoredOrder, [0, 1])
    }

    func testRepeatedWeakNetworkFailuresKeepAudioUntilSuccessfulAck() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: ledger, root: root)
        let transport = FakeWatchTransport(failUploadAttempts: 3)
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        for _ in 0..<3 {
            let failure = try await syncFailure(engine, expecting: .segmentUploadFailed,
                message: "Expected weak-network upload failure", alternative: "Expected segmentUploadFailed")
            XCTAssertFalse(failure.isEmpty)
            let pending = try await ledger.loadSession(id: segment.sessionID)
            XCTAssertEqual(pending?.segments.first?.uploadState, .failed)
            XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))
            XCTAssertNil(pending?.macJobID)
        }

        let report = try await engine.syncPending()
        let completed = try await ledger.loadSession(id: segment.sessionID)
        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(completed?.segments.first?.uploadState, .acknowledged)
        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
    }

    func testRegisterFailureLeavesPendingAudioUntouched() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: ledger, root: root)
        let transport = FakeWatchTransport(failRegisterAttempts: 1)
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        let failure = try await syncFailure(engine, expecting: .sessionRegistrationFailed,
            message: "Expected registration to fail while offline", alternative: "Expected sessionRegistrationFailed")
        XCTAssertFalse(failure.isEmpty)

        let pending = try await ledger.loadSession(id: segment.sessionID)
        XCTAssertEqual(pending?.segments.first?.uploadState, .pending)
        XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))

        let report = try await engine.syncPending()
        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(report.sessionsCompleted, 1)
    }

    func testRepeatedRegisterFailuresNeverMutatePendingAudio() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: ledger, root: root)
        let transport = FakeWatchTransport(failRegisterAttempts: 3)
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        for _ in 0..<3 {
            let failure = try await syncFailure(engine, expecting: .sessionRegistrationFailed,
                message: "Expected registration to fail while network is unstable", alternative: "Expected sessionRegistrationFailed")
            XCTAssertFalse(failure.isEmpty)
            let pending = try await ledger.loadSession(id: segment.sessionID)
            XCTAssertEqual(pending?.segments.first?.uploadState, .pending)
            XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))
            XCTAssertNil(pending?.macJobID)
        }

        let beforeRecovery = await transport.callCounts()
        XCTAssertEqual(beforeRecovery.register, 3)
        XCTAssertEqual(beforeRecovery.upload, 0)

        let report = try await engine.syncPending()
        let recovered = try await ledger.loadSession(id: segment.sessionID)
        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(recovered?.segments.first?.uploadState, .acknowledged)
        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
    }

    func testRegisterFailureSurvivesLedgerRecreationWithoutUploading() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let firstLedger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: firstLedger, root: root)
        let firstTransport = FakeWatchTransport(failRegisterAttempts: 1)
        let firstEngine = WatchSyncEngine(ledger: firstLedger, transport: firstTransport)

        let failure = try await syncFailure(firstEngine, expecting: .sessionRegistrationFailed,
            message: "Expected register failure before app restart", alternative: "Expected sessionRegistrationFailed")
        XCTAssertFalse(failure.isEmpty)

        let persisted = try await firstLedger.loadSession(id: segment.sessionID)
        XCTAssertEqual(persisted?.segments.first?.uploadState, .pending)
        XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))
        let firstCounts = await firstTransport.callCounts()
        XCTAssertEqual(firstCounts.upload, 0)

        let restoredLedger = SegmentLedger(rootURL: root)
        let restoredTransport = FakeWatchTransport()
        let restoredEngine = WatchSyncEngine(
            ledger: restoredLedger,
            transport: restoredTransport
        )
        let report = try await restoredEngine.syncPending()
        let restoredCounts = await restoredTransport.callCounts()

        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(restoredCounts.register, 1)
        XCTAssertEqual(restoredCounts.upload, 1)
        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
    }

    func testTwoPendingSegmentsKeepUploadOrderAfterRegisterFailureAndRestart() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let firstLedger = SegmentLedger(rootURL: root)
        _ = try await firstLedger.createSession(
            id: "watch-session-ordered",
            startedAt: Date(timeIntervalSince1970: 2_000)
        )
        let firstSource = root.appendingPathComponent("ordered-0.m4a")
        let secondSource = root.appendingPathComponent("ordered-1.m4a")
        try Data("ordered zero".utf8).write(to: firstSource)
        try Data("ordered one".utf8).write(to: secondSource)
        _ = try await firstLedger.appendSegment(
            sessionID: "watch-session-ordered",
            sequence: 0,
            sourceURL: firstSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        _ = try await firstLedger.appendSegment(
            sessionID: "watch-session-ordered",
            sequence: 1,
            sourceURL: secondSource,
            startOffsetMilliseconds: 60_000,
            durationMilliseconds: 60_000
        )
        try await firstLedger.finishSession(
            id: "watch-session-ordered",
            stoppedAt: Date(timeIntervalSince1970: 2_120)
        )

        let firstTransport = FakeWatchTransport(failRegisterAttempts: 1)
        let firstEngine = WatchSyncEngine(ledger: firstLedger, transport: firstTransport)
        let failure = try await syncFailure(firstEngine, expecting: .sessionRegistrationFailed,
            message: "Expected register failure before restart", alternative: "Expected sessionRegistrationFailed")
        XCTAssertFalse(failure.isEmpty)

        let beforeRestart = try await firstLedger.loadSession(id: "watch-session-ordered")
        let beforeRestartOrder = await firstTransport.uploadedSequenceOrder()
        XCTAssertEqual(beforeRestart?.segments.map(\.uploadState), [.pending, .pending])
        XCTAssertEqual(beforeRestartOrder, [])

        let restoredLedger = SegmentLedger(rootURL: root)
        let restoredTransport = FakeWatchTransport()
        let restoredEngine = WatchSyncEngine(
            ledger: restoredLedger,
            transport: restoredTransport
        )
        let report = try await restoredEngine.syncPending()
        let completed = try await restoredLedger.loadSession(id: "watch-session-ordered")
        let restoredOrder = await restoredTransport.uploadedSequenceOrder()

        XCTAssertEqual(report.segmentsAcknowledged, 2)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(restoredOrder, [0, 1])
        XCTAssertEqual(completed?.segments.map(\.uploadState), [.acknowledged, .acknowledged])
    }

    func testCompletionFailureRetriesWithoutReuploadingAcknowledgedAudio() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: ledger, root: root)
        let transport = FakeWatchTransport(failCompleteAttempts: 1)
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        let failure = try await syncFailure(engine, expecting: .sessionCompletionFailed,
            message: "Expected completion request to fail", alternative: "Expected sessionCompletionFailed")
        XCTAssertFalse(failure.isEmpty)

        let afterFailure = try await ledger.loadSession(id: segment.sessionID)
        XCTAssertEqual(afterFailure?.segments.first?.uploadState, .acknowledged)
        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
        XCTAssertNil(afterFailure?.macJobID)
        let firstCounts = await transport.callCounts()
        XCTAssertEqual(firstCounts.upload, 1)

        let report = try await engine.syncPending()
        let finalCounts = await transport.callCounts()
        XCTAssertEqual(report.segmentsAcknowledged, 0)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(finalCounts.upload, 1)
        XCTAssertEqual(finalCounts.complete, 2)
    }

    func testRepeatedCompletionFailuresNeverReuploadAcknowledgedAudio() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: ledger, root: root)
        let transport = FakeWatchTransport(failCompleteAttempts: 3)
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        for _ in 0..<3 {
            let failure = try await syncFailure(engine, expecting: .sessionCompletionFailed,
                message: "Expected completion retry to fail while network is unstable", alternative: "Expected sessionCompletionFailed")
            XCTAssertFalse(failure.isEmpty)

            let state = try await ledger.loadSession(id: segment.sessionID)
            XCTAssertEqual(state?.segments.first?.uploadState, .acknowledged)
            XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
            XCTAssertNil(state?.macJobID)
        }

        let retryCounts = await transport.callCounts()
        XCTAssertEqual(retryCounts.upload, 1)
        XCTAssertEqual(retryCounts.complete, 3)

        let report = try await engine.syncPending()
        let finalCounts = await transport.callCounts()
        XCTAssertEqual(report.segmentsAcknowledged, 0)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(finalCounts.upload, 1)
        XCTAssertEqual(finalCounts.complete, 4)
    }

    func testCompletionFailureSurvivesLedgerRecreationWithoutReupload() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let firstLedger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: firstLedger, root: root)
        let firstTransport = FakeWatchTransport(failCompleteAttempts: 1)
        let firstEngine = WatchSyncEngine(ledger: firstLedger, transport: firstTransport)

        let failure = try await syncFailure(firstEngine, expecting: .sessionCompletionFailed,
            message: "Expected completion to fail before app restart", alternative: "Expected sessionCompletionFailed")
        XCTAssertFalse(failure.isEmpty)

        let persisted = try await firstLedger.loadSession(id: segment.sessionID)
        XCTAssertEqual(persisted?.segments.first?.uploadState, .acknowledged)
        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
        XCTAssertNil(persisted?.macJobID)

        let restoredLedger = SegmentLedger(rootURL: root)
        let restoredTransport = FakeWatchTransport()
        let restoredEngine = WatchSyncEngine(
            ledger: restoredLedger,
            transport: restoredTransport
        )
        let report = try await restoredEngine.syncPending()
        let restoredCounts = await restoredTransport.callCounts()
        let completed = try await restoredLedger.loadSession(id: segment.sessionID)

        XCTAssertEqual(report.segmentsAcknowledged, 0)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(restoredCounts.upload, 0)
        XCTAssertEqual(restoredCounts.complete, 1)
        XCTAssertEqual(completed?.macJobID, "job-watch-001")
    }

    func testPartialMultiSegmentWeakNetworkRetrySkipsAlreadyAcknowledgedAudio() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        _ = try await ledger.createSession(
            id: "watch-session-001",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let firstSource = root.appendingPathComponent("first.m4a")
        let secondSource = root.appendingPathComponent("second.m4a")
        try Data("first audio".utf8).write(to: firstSource)
        try Data("second audio".utf8).write(to: secondSource)
        let first = try await ledger.appendSegment(
            sessionID: "watch-session-001",
            sequence: 0,
            sourceURL: firstSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        let second = try await ledger.appendSegment(
            sessionID: "watch-session-001",
            sequence: 1,
            sourceURL: secondSource,
            startOffsetMilliseconds: 60_000,
            durationMilliseconds: 60_000
        )
        try await ledger.finishSession(
            id: "watch-session-001",
            stoppedAt: Date(timeIntervalSince1970: 1_120)
        )
        try await ledger.acknowledge(
            sessionID: first.sessionID,
            sequence: first.sequence,
            sha256: first.sha256
        )

        let transport = FakeWatchTransport(failFirstUpload: true)
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        let failure = try await syncFailure(engine, expecting: .segmentUploadFailed,
            message: "Expected the remaining segment upload to fail once", alternative: "Expected segmentUploadFailed")
        XCTAssertFalse(failure.isEmpty)

        let afterFailure = try await ledger.loadSession(id: "watch-session-001")
        XCTAssertEqual(afterFailure?.segments[0].uploadState, .acknowledged)
        XCTAssertEqual(afterFailure?.segments[1].uploadState, .failed)
        XCTAssertFalse(FileManager.default.fileExists(atPath: first.localURL.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: second.localURL.path))
        let firstCounts = await transport.callCounts()
        XCTAssertEqual(firstCounts.upload, 1)

        let report = try await engine.syncPending()
        let afterRetry = try await ledger.loadSession(id: "watch-session-001")
        let finalCounts = await transport.callCounts()
        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(afterRetry?.segments.map(\.uploadState), [.acknowledged, .acknowledged])
        XCTAssertEqual(finalCounts.upload, 2)
        XCTAssertFalse(FileManager.default.fileExists(atPath: second.localURL.path))
    }

    func testPartialMultiSegmentFailureSurvivesLedgerRecreationWithoutReuploadingAckedSegment() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let firstLedger = SegmentLedger(rootURL: root)
        _ = try await firstLedger.createSession(
            id: "watch-session-001",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let firstSource = root.appendingPathComponent("first-restart.m4a")
        let secondSource = root.appendingPathComponent("second-restart.m4a")
        try Data("first restart audio".utf8).write(to: firstSource)
        try Data("second restart audio".utf8).write(to: secondSource)
        let first = try await firstLedger.appendSegment(
            sessionID: "watch-session-001",
            sequence: 0,
            sourceURL: firstSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        let second = try await firstLedger.appendSegment(
            sessionID: "watch-session-001",
            sequence: 1,
            sourceURL: secondSource,
            startOffsetMilliseconds: 60_000,
            durationMilliseconds: 60_000
        )
        try await firstLedger.finishSession(
            id: "watch-session-001",
            stoppedAt: Date(timeIntervalSince1970: 1_120)
        )
        try await firstLedger.acknowledge(
            sessionID: first.sessionID,
            sequence: first.sequence,
            sha256: first.sha256
        )

        let failingTransport = FakeWatchTransport(failFirstUpload: true)
        let firstEngine = WatchSyncEngine(ledger: firstLedger, transport: failingTransport)
        let failure = try await syncFailure(firstEngine, expecting: .segmentUploadFailed,
            message: "Expected remaining segment upload to fail before restart", alternative: "Expected segmentUploadFailed")
        XCTAssertFalse(failure.isEmpty)

        let persisted = try await firstLedger.loadSession(id: "watch-session-001")
        XCTAssertEqual(persisted?.segments[0].uploadState, .acknowledged)
        XCTAssertEqual(persisted?.segments[1].uploadState, .failed)
        XCTAssertFalse(FileManager.default.fileExists(atPath: first.localURL.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: second.localURL.path))

        let restoredLedger = SegmentLedger(rootURL: root)
        let restoredTransport = FakeWatchTransport()
        let restoredEngine = WatchSyncEngine(
            ledger: restoredLedger,
            transport: restoredTransport
        )
        let report = try await restoredEngine.syncPending()
        let counts = await restoredTransport.callCounts()
        let completed = try await restoredLedger.loadSession(id: "watch-session-001")
        let restoredOrder = await restoredTransport.uploadedSequenceOrder()

        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(counts.upload, 1)
        XCTAssertEqual(restoredOrder, [1])
        XCTAssertEqual(completed?.segments.map(\.uploadState), [.acknowledged, .acknowledged])
        XCTAssertFalse(FileManager.default.fileExists(atPath: second.localURL.path))
    }

    func testInterruptedUploadingSegmentRetriesAfterLedgerRecreation() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let originalLedger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: originalLedger, root: root)
        try await originalLedger.markUploading(
            sessionID: segment.sessionID,
            sequence: segment.sequence
        )

        let relaunchedLedger = SegmentLedger(rootURL: root)
        let transport = FakeWatchTransport()
        let engine = WatchSyncEngine(ledger: relaunchedLedger, transport: transport)

        let report = try await engine.syncPending()
        let recovered = try await relaunchedLedger.loadSession(id: segment.sessionID)

        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(recovered?.segments.first?.uploadState, .acknowledged)
        XCTAssertEqual(recovered?.macJobID, "job-watch-001")
        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
        let counts = await transport.callCounts()
        XCTAssertEqual(counts.upload, 1)
    }

    func testInterruptedFirstUploadWithSecondPendingKeepsOrderAfterRestart() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let firstLedger = SegmentLedger(rootURL: root)
        _ = try await firstLedger.createSession(
            id: "watch-session-uploading-ordered",
            startedAt: Date(timeIntervalSince1970: 4_000)
        )
        let firstSource = root.appendingPathComponent("uploading-ordered-0.m4a")
        let secondSource = root.appendingPathComponent("uploading-ordered-1.m4a")
        try Data("uploading ordered zero".utf8).write(to: firstSource)
        try Data("uploading ordered one".utf8).write(to: secondSource)
        let first = try await firstLedger.appendSegment(
            sessionID: "watch-session-uploading-ordered",
            sequence: 0,
            sourceURL: firstSource,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        _ = try await firstLedger.appendSegment(
            sessionID: "watch-session-uploading-ordered",
            sequence: 1,
            sourceURL: secondSource,
            startOffsetMilliseconds: 60_000,
            durationMilliseconds: 60_000
        )
        try await firstLedger.finishSession(
            id: "watch-session-uploading-ordered",
            stoppedAt: Date(timeIntervalSince1970: 4_120)
        )
        try await firstLedger.markUploading(
            sessionID: first.sessionID,
            sequence: first.sequence
        )

        let restoredLedger = SegmentLedger(rootURL: root)
        let restoredTransport = FakeWatchTransport()
        let restoredEngine = WatchSyncEngine(
            ledger: restoredLedger,
            transport: restoredTransport
        )
        let report = try await restoredEngine.syncPending()
        let restoredOrder = await restoredTransport.uploadedSequenceOrder()
        let completed = try await restoredLedger.loadSession(id: "watch-session-uploading-ordered")

        XCTAssertEqual(report.segmentsAcknowledged, 2)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(restoredOrder, [0, 1])
        XCTAssertEqual(completed?.segments.map(\.uploadState), [.acknowledged, .acknowledged])
    }

    func testWrongMacAckNeverDeletesLocalAudio() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: ledger, root: root)
        let transport = FakeWatchTransport(returnWrongChecksum: true)
        let engine = WatchSyncEngine(ledger: ledger, transport: transport)

        let failure = try await syncFailure(engine, expecting: .acknowledgementMismatch,
            message: "Expected a mismatched ACK to be rejected", alternative: "Expected acknowledgementMismatch")
        XCTAssertFalse(failure.isEmpty)

        let session = try await ledger.loadSession(id: segment.sessionID)
        XCTAssertEqual(session?.segments.first?.uploadState, .failed)
        XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))
        XCTAssertNil(session?.macJobID)
    }

    func testWrongAckCanRecoverOnNextSyncWithValidTransport() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: ledger, root: root)
        let badEngine = WatchSyncEngine(
            ledger: ledger,
            transport: FakeWatchTransport(returnWrongChecksum: true)
        )

        let failure = try await syncFailure(badEngine, expecting: .acknowledgementMismatch,
            message: "Expected a mismatched ACK to be rejected", alternative: "Expected acknowledgementMismatch")
        XCTAssertFalse(failure.isEmpty)
        XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))

        let goodEngine = WatchSyncEngine(
            ledger: ledger,
            transport: FakeWatchTransport()
        )
        let report = try await goodEngine.syncPending()
        let recovered = try await ledger.loadSession(id: segment.sessionID)

        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(recovered?.segments.first?.uploadState, .acknowledged)
        XCTAssertEqual(recovered?.macJobID, "job-watch-001")
        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
    }

    func testWrongAckFailureSurvivesLedgerRecreationAndRetriesSafely() async throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let firstLedger = SegmentLedger(rootURL: root)
        let segment = try await makeFinishedSession(ledger: firstLedger, root: root)
        let badEngine = WatchSyncEngine(
            ledger: firstLedger,
            transport: FakeWatchTransport(returnWrongChecksum: true)
        )

        let failure = try await syncFailure(badEngine, expecting: .acknowledgementMismatch,
            message: "Expected mismatched ACK before restart", alternative: "Expected acknowledgementMismatch")
        XCTAssertFalse(failure.isEmpty)

        let persisted = try await firstLedger.loadSession(id: segment.sessionID)
        XCTAssertEqual(persisted?.segments.first?.uploadState, .failed)
        XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))

        let restoredLedger = SegmentLedger(rootURL: root)
        let restoredTransport = FakeWatchTransport()
        let restoredEngine = WatchSyncEngine(
            ledger: restoredLedger,
            transport: restoredTransport
        )
        let report = try await restoredEngine.syncPending()
        let restoredCounts = await restoredTransport.callCounts()
        let completed = try await restoredLedger.loadSession(id: segment.sessionID)

        XCTAssertEqual(report.segmentsAcknowledged, 1)
        XCTAssertEqual(report.sessionsCompleted, 1)
        XCTAssertEqual(restoredCounts.upload, 1)
        XCTAssertEqual(completed?.segments.first?.uploadState, .acknowledged)
        XCTAssertEqual(completed?.macJobID, "job-watch-001")
        XCTAssertFalse(FileManager.default.fileExists(atPath: segment.localURL.path))
    }

    private func makeFinishedSession(
        ledger: SegmentLedger,
        root: URL
    ) async throws -> AudioSegment {
        _ = try await ledger.createSession(
            id: "watch-session-001",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        let source = root.appendingPathComponent("source.m4a")
        try Data("recorded audio".utf8).write(to: source)
        let segment = try await ledger.appendSegment(
            sessionID: "watch-session-001",
            sequence: 0,
            sourceURL: source,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000
        )
        try await ledger.finishSession(
            id: "watch-session-001",
            stoppedAt: Date(timeIntervalSince1970: 1_060)
        )
        return segment
    }

    private func temporaryRoot() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("watch-sync-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    /// Drains the queue and asserts the session failed with a specific `WatchSyncError` case,
    /// returning the Mac-supplied detail. A drain never throws for a failed session any more —
    /// it records the failure and keeps going — so these tests read the report instead of
    /// catching an escaping error.
    @discardableResult
    private func syncFailure(
        _ engine: WatchSyncEngine,
        expecting expectedCase: WatchSyncErrorCase,
        message: String,
        alternative: String
    ) async throws -> String {
        let report = try await engine.syncPending()
        guard let failure = report.failures.first else {
            XCTFail(message)
            return ""
        }
        guard expectedCase.matches(failure.error) else {
            XCTFail("\(alternative), got \(failure.error)")
            return ""
        }
        return expectedCase.detail(of: failure.error) ?? ""
    }
}

private enum FakeWatchTransportError: Error {
    case offline
    case localFileMissing
}

/// The four failure kinds, without inventing a detail string just to name a case in a test.
private enum WatchSyncErrorCase {
    case acknowledgementMismatch
    case sessionRegistrationFailed
    case segmentUploadFailed
    case sessionCompletionFailed

    func matches(_ error: WatchSyncError) -> Bool {
        switch (self, error) {
        case (.acknowledgementMismatch, .acknowledgementMismatch),
             (.sessionRegistrationFailed, .sessionRegistrationFailed),
             (.segmentUploadFailed, .segmentUploadFailed),
             (.sessionCompletionFailed, .sessionCompletionFailed):
            return true
        default:
            return false
        }
    }

    func detail(of error: WatchSyncError) -> String? {
        switch error {
        case .acknowledgementMismatch(let detail),
             .sessionRegistrationFailed(let detail),
             .segmentUploadFailed(let detail),
             .sessionCompletionFailed(let detail):
            return detail
        case .segmentsMissingLocally(let sequences):
            return sequences.map(String.init).joined(separator: ",")
        }
    }
}

private actor FakeWatchTransport: WatchSessionTransport {
    private var remainingRegisterFailures: Int
    private var remainingUploadFailures: Int
    private var remainingCompleteFailures: Int
    private var queuesCompletionInsteadOfCompleting: Bool
    private let returnWrongChecksum: Bool
    private var registerCalls = 0
    private var uploadCalls = 0
    private var completeCalls = 0
    private var uploadedSequences: [Int] = []
    private var registeredSessionIDs: [String] = []

    init(
        failFirstUpload: Bool = false,
        failRegisterAttempts: Int = 0,
        failUploadAttempts: Int = 0,
        failCompleteAttempts: Int = 0,
        returnWrongChecksum: Bool = false,
        queuesCompletion: Bool = false
    ) {
        self.queuesCompletionInsteadOfCompleting = queuesCompletion
        self.remainingRegisterFailures = max(0, failRegisterAttempts)
        self.remainingUploadFailures = max(failUploadAttempts, failFirstUpload ? 1 : 0)
        self.remainingCompleteFailures = max(0, failCompleteAttempts)
        self.returnWrongChecksum = returnWrongChecksum
    }
    func register(session: RecordingSessionManifest) async throws {
        registerCalls += 1
        registeredSessionIDs.append(session.id)
        if remainingRegisterFailures > 0 {
            remainingRegisterFailures -= 1
            throw FakeWatchTransportError.offline
        }
    }

    func upload(segment: AudioSegment) async throws -> WatchSegmentAcknowledgement {
        uploadCalls += 1
        uploadedSequences.append(segment.sequence)
        // Mirrors the real transport: a segment whose file is gone cannot be uploaded, and
        // saying otherwise would let a test "pass" against behaviour the device never has.
        if !FileManager.default.fileExists(atPath: segment.localURL.path) {
            throw FakeWatchTransportError.localFileMissing
        }
        if remainingUploadFailures > 0 {
            remainingUploadFailures -= 1
            throw FakeWatchTransportError.offline
        }
        return WatchSegmentAcknowledgement(
            sessionID: segment.sessionID,
            sequence: segment.sequence,
            sha256: returnWrongChecksum ? "sha256:wrong" : segment.sha256
        )
    }

    func complete(session: RecordingSessionManifest) async throws -> String {
        completeCalls += 1
        if queuesCompletionInsteadOfCompleting {
            throw WatchSyncTransportOutcome.completionQueued
        }
        if remainingCompleteFailures > 0 {
            remainingCompleteFailures -= 1
            throw FakeWatchTransportError.offline
        }
        return "job-watch-001"
    }

    func uploadCount() -> Int { uploadCalls }

    func callCounts() -> (register: Int, upload: Int, complete: Int) {
        (registerCalls, uploadCalls, completeCalls)
    }

    func uploadedSequenceOrder() -> [Int] {
        uploadedSequences
    }

    func registeredSessionOrder() -> [String] {
        registeredSessionIDs
    }
}
