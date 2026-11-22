import Foundation
import XCTest
@testable import CaptureUnderTest

/// Drive the same coordinator method used by the Watch button, with a durable real ledger.
@MainActor
final class WatchUploadCoordinatorTests: XCTestCase {
    func testRecordingUploadButtonSendsSevenOldSegmentsBeforeFirstRotation() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        try await seedBacklog(ledger, root: root)
        let transport = HeldTransport()
        let worker = WatchLiveUploadWorker(ledger: ledger, transportFactory: { _ in transport })
        let sync = WatchSyncCoordinator(ledger: ledger, liveUploadWorker: worker)
        WatchCaptureNetworkGate.shared.setBlocked(true)
        defer { WatchCaptureNetworkGate.shared.setBlocked(false) }

        // Exact user scenario: old queue = 7, fresh recording has not sealed anything.
        await sync.syncPending(recordingSessionID: "fresh")

        let uploaded = await transport.uploadCount
        let completed = await transport.completionCount
        let fresh = try await ledger.loadSession(id: "fresh")
        XCTAssertEqual(uploaded, 7)
        XCTAssertEqual(completed, 0)
        XCTAssertNil(fresh?.stoppedAt)
        XCTAssertFalse(sync.isSyncing)
        XCTAssertEqual(sync.macConnectionIndicator, .reachable)
        XCTAssertTrue(sync.statusMessage.contains("7"))
    }

    func testQueueScreenWithoutSessionArgumentAlsoUploadsDuringRecording() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        try await seedBacklog(ledger, root: root)
        let transport = HeldTransport()
        let worker = WatchLiveUploadWorker(ledger: ledger, transportFactory: { _ in transport })
        let sync = WatchSyncCoordinator(ledger: ledger, liveUploadWorker: worker)
        WatchCaptureNetworkGate.shared.setBlocked(true)
        defer { WatchCaptureNetworkGate.shared.setBlocked(false) }

        await sync.syncPending()

        let count = await transport.uploadCount
        XCTAssertEqual(count, 7, "Queue/detail screens call syncPending without a recording ID")
    }

    func testTapDuringAutomaticUploadDrainsNewlySealedSegmentInFollowUp() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        try await seedBacklog(ledger, root: root)
        let transport = HeldTransport(holdFirstRegistration: true)
        let worker = WatchLiveUploadWorker(ledger: ledger, transportFactory: { _ in transport })
        let sync = WatchSyncCoordinator(ledger: ledger, liveUploadWorker: worker)
        WatchCaptureNetworkGate.shared.setBlocked(true)
        defer { WatchCaptureNetworkGate.shared.setBlocked(false) }
        let automatic = Task { await sync.uploadSealedSegmentsDuringCapture(sessionID: "fresh") }
        for _ in 0..<1000 {
            if await transport.isHeld { break }
            try await Task.sleep(for: .milliseconds(1))
        }
        let held = await transport.isHeld
        XCTAssertTrue(held)
        XCTAssertTrue(sync.isSyncing)
        // This segment was sealed after the first pass took its manifest snapshot.
        let source = root.appendingPathComponent("fresh.m4a")
        try Data("fresh audio".utf8).write(to: source)
        _ = try await ledger.appendSegment(
            sessionID: "fresh", sequence: 0, sourceURL: source,
            startOffsetMilliseconds: 0, durationMilliseconds: 60_000
        )
        await sync.syncPending(recordingSessionID: "fresh")
        XCTAssertTrue(sync.statusMessage.contains("已排队"))
        await transport.release()
        await automatic.value
        for _ in 0..<1000 {
            if await transport.uploadCount == 8, !sync.isSyncing { break }
            try await Task.sleep(for: .milliseconds(1))
        }
        let count = await transport.uploadCount
        let completed = await transport.completionCount
        let fresh = try await ledger.loadSession(id: "fresh")
        XCTAssertEqual(count, 8)
        XCTAssertEqual(completed, 0)
        XCTAssertNil(fresh?.stoppedAt)
        XCTAssertFalse(sync.statusMessage.contains("登记失败"))
    }

    func testStartingRecordingHandsCancelledOrdinarySyncToLiveOutbox() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = SegmentLedger(rootURL: root)
        try await seedBacklog(ledger, root: root)
        let liveTransport = HeldTransport()
        let ordinaryTransport = InterruptedConnection()
        let worker = WatchLiveUploadWorker(ledger: ledger, transportFactory: { _ in liveTransport })
        let sync = WatchSyncCoordinator(
            ledger: ledger, liveUploadWorker: worker, transportFactory: { _ in ordinaryTransport }
        )
        WatchCaptureNetworkGate.shared.setBlocked(false)
        defer { WatchCaptureNetworkGate.shared.setBlocked(false) }
        let ordinary = Task { await sync.syncPending() }
        for _ in 0..<1000 {
            if await ordinaryTransport.isHeld { break }
            try await Task.sleep(for: .milliseconds(1))
        }
        let held = await ordinaryTransport.isHeld
        XCTAssertTrue(held)
        // Mirrors capture start: cancel ordinary networking, then request a capture upload.
        WatchCaptureNetworkGate.shared.setBlocked(true)
        await sync.syncPending(recordingSessionID: "fresh")
        XCTAssertTrue(sync.statusMessage.contains("已排队"))
        await ordinaryTransport.interrupt()
        await ordinary.value
        for _ in 0..<1000 {
            if await liveTransport.uploadCount == 7, !sync.isSyncing { break }
            try await Task.sleep(for: .milliseconds(1))
        }
        let uploaded = await liveTransport.uploadCount
        let fresh = try await ledger.loadSession(id: "fresh")
        XCTAssertEqual(uploaded, 7, "Queued live request must run when ORDINARY sync exits")
        XCTAssertFalse(sync.isSyncing)
        XCTAssertNil(fresh?.stoppedAt)
        XCTAssertEqual(sync.macConnectionIndicator, .reachable)
    }

    private func seedBacklog(_ ledger: SegmentLedger, root: URL) async throws {
        _ = try await ledger.createSession(id: "old")
        for sequence in 0..<7 {
            let source = root.appendingPathComponent("old-\(sequence).m4a")
            try Data("old audio \(sequence)".utf8).write(to: source)
            _ = try await ledger.appendSegment(
                sessionID: "old", sequence: sequence, sourceURL: source,
                startOffsetMilliseconds: Int64(sequence * 60_000), durationMilliseconds: 60_000
            )
        }
        try await ledger.finishSession(id: "old")
        _ = try await ledger.createSession(id: "fresh")
    }
}

private actor HeldTransport: WatchSessionTransport {
    var uploadCount = 0
    var completionCount = 0
    var isHeld: Bool { continuation != nil }
    private var holdFirstRegistration: Bool
    private var continuation: CheckedContinuation<Void, Never>?

    init(holdFirstRegistration: Bool = false) { self.holdFirstRegistration = holdFirstRegistration }

    func register(session: RecordingSessionManifest) async throws {
        if holdFirstRegistration {
            holdFirstRegistration = false
            await withCheckedContinuation { continuation = $0 }
        }
    }

    func release() { continuation?.resume(); continuation = nil }

    func upload(segment: AudioSegment) async throws -> WatchSegmentAcknowledgement {
        uploadCount += 1
        return WatchSegmentAcknowledgement(sessionID: segment.sessionID, sequence: segment.sequence, sha256: segment.sha256)
    }

    func complete(session: RecordingSessionManifest) async throws -> String {
        completionCount += 1
        return "unexpected-completion"
    }
}
