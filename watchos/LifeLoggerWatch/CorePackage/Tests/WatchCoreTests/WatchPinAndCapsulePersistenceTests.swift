import Foundation
import XCTest
@testable import WatchCore

final class WatchPinAndCapsulePersistenceTests: XCTestCase {
    private var tempDirectory: URL!

    override func setUpWithError() throws {
        try super.setUpWithError()
        tempDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("ledger-pin-test-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: tempDirectory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: tempDirectory)
        try super.tearDownWithError()
    }

    private func createDummyAudio(name: String, bytes: Int = 1024) throws -> URL {
        let url = tempDirectory.appendingPathComponent(name)
        let data = Data(repeating: 0x55, count: bytes)
        try data.write(to: url)
        return url
    }

    func testOldManifestWithoutPinsOrCapsulesDecodesCleanly() throws {
        // Raw JSON representing historical V50/V64 manifest without pins/capsules fields
        let oldJSON = """
        {
            "id": "old-session-123",
            "startedAt": 750000000.0,
            "segments": [
                {
                    "sessionID": "old-session-123",
                    "sequence": 0,
                    "startOffsetMilliseconds": 0,
                    "durationMilliseconds": 60000,
                    "localURL": "file:///tmp/dummy.m4a",
                    "byteCount": 1024,
                    "sha256": "sha256:abcd",
                    "uploadState": "pending"
                }
            ]
        }
        """.data(using: .utf8)!

        let manifest = try JSONDecoder().decode(RecordingSessionManifest.self, from: oldJSON)
        XCTAssertEqual(manifest.id, "old-session-123")
        XCTAssertEqual(manifest.allPins.count, 0)
        XCTAssertEqual(manifest.allCapsules.count, 0)
        XCTAssertNil(manifest.activeCapsule)
        XCTAssertEqual(manifest.segments.first?.effectivePins.count, 0)
    }

    func testMultiplePinsInSameSegmentPreservedDurably() async throws {
        let ledger = SegmentLedger(rootURL: tempDirectory)
        let sessionID = "sess-multi-pin"
        _ = try await ledger.createSession(id: sessionID)

        let pin1 = WatchSessionPin(sessionID: sessionID, type: "double_pinch", offsetMilliseconds: 5200)
        let pin2 = WatchSessionPin(sessionID: sessionID, type: "double_pinch", offsetMilliseconds: 14800)
        let pin3 = WatchSessionPin(sessionID: sessionID, type: "double_pinch", offsetMilliseconds: 28100)

        try await ledger.recordPin(sessionID: sessionID, pin: pin1)
        try await ledger.recordPin(sessionID: sessionID, pin: pin2)
        try await ledger.recordPin(sessionID: sessionID, pin: pin3)

        let source = try createDummyAudio(name: "seg0.m4a")
        _ = try await ledger.appendSegment(
            sessionID: sessionID,
            sequence: 0,
            sourceURL: source,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60000,
            pins: [pin1, pin2, pin3]
        )

        // Verify in-memory manifest
        let manifest = try await ledger.loadSession(id: sessionID)
        XCTAssertEqual(manifest?.allPins.count, 3)
        XCTAssertEqual(manifest?.segments.first?.effectivePins.count, 3)
        XCTAssertEqual(manifest?.allPins.map { $0.offsetMilliseconds }, [5200, 14800, 28100])

        // Verify durable reload from disk
        let restoredLedger = SegmentLedger(rootURL: tempDirectory)
        let restoredManifest = try await restoredLedger.loadSession(id: sessionID)
        XCTAssertEqual(restoredManifest?.allPins.count, 3)
        XCTAssertEqual(restoredManifest?.segments.first?.effectivePins.count, 3)
    }

    func testCapsuleStartAndEndInSameSegmentHasAccurateNonZeroRange() async throws {
        let ledger = SegmentLedger(rootURL: tempDirectory)
        let sessionID = "sess-capsule-test"
        _ = try await ledger.createSession(id: sessionID)

        let startDate = Date()
        try await ledger.startCapsule(sessionID: sessionID, startOffsetMilliseconds: 12000, startedAt: startDate)

        // Verify active capsule is persisted
        let activeManifest = try await ledger.loadSession(id: sessionID)
        XCTAssertNotNil(activeManifest?.activeCapsule)
        XCTAssertEqual(activeManifest?.activeCapsule?.startOffsetMilliseconds, 12000)

        // Seal capsule at 28500ms (within same 60s segment)
        let endDate = startDate.addingTimeInterval(16.5)
        let sealed = try await ledger.sealCapsule(sessionID: sessionID, endOffsetMilliseconds: 28500, endedAt: endDate)

        XCTAssertNotNil(sealed)
        XCTAssertEqual(sealed?.startOffsetMilliseconds, 12000)
        XCTAssertEqual(sealed?.endOffsetMilliseconds, 28500)

        let finalManifest = try await ledger.loadSession(id: sessionID)
        XCTAssertNil(finalManifest?.activeCapsule)
        XCTAssertEqual(finalManifest?.allCapsules.count, 1)
        XCTAssertEqual(finalManifest?.allCapsules.first?.startOffsetMilliseconds, 12000)
        XCTAssertEqual(finalManifest?.allCapsules.first?.endOffsetMilliseconds, 28500)
    }

    func testUnendedCapsuleRecoveredAcrossRestartsAndAutoSealedOnFinish() async throws {
        let ledger = SegmentLedger(rootURL: tempDirectory)
        let sessionID = "sess-restart-capsule"
        _ = try await ledger.createSession(id: sessionID)

        try await ledger.startCapsule(sessionID: sessionID, startOffsetMilliseconds: 5000, startedAt: Date())

        // Recreate ledger from disk
        let restoredLedger = SegmentLedger(rootURL: tempDirectory)
        let restoredManifest = try await restoredLedger.loadSession(id: sessionID)
        XCTAssertNotNil(restoredManifest?.activeCapsule, "Unended capsule must survive restart")

        let source = try createDummyAudio(name: "seg0.m4a")
        _ = try await restoredLedger.appendSegment(
            sessionID: sessionID,
            sequence: 0,
            sourceURL: source,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 30000
        )

        // Finish session without explicitly sealing capsule -> must auto-seal safely
        try await restoredLedger.finishSession(id: sessionID)
        let closedManifest = try await restoredLedger.loadSession(id: sessionID)
        XCTAssertNil(closedManifest?.activeCapsule)
        XCTAssertEqual(closedManifest?.allCapsules.count, 1)
        XCTAssertEqual(closedManifest?.allCapsules.first?.startOffsetMilliseconds, 5000)
        XCTAssertGreaterThan(closedManifest?.allCapsules.first?.endOffsetMilliseconds ?? 0, 5000)
    }
}
