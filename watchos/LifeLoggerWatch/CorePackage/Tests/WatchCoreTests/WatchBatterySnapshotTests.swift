import Foundation
import XCTest
@testable import WatchCore

final class WatchBatterySnapshotTests: XCTestCase {
    func testBatteryFractionIsStoredAsRoundedPercentageAndState() {
        let snapshot = WatchBatterySnapshot(level: 0.756, state: .unplugged)

        XCTAssertEqual(snapshot.levelPercent, 76)
        XCTAssertEqual(snapshot.state, .unplugged)
    }

    func testUnknownOrInvalidBatteryLevelDoesNotInventPercentage() {
        XCTAssertNil(WatchBatterySnapshot(level: -1, state: .unknown).levelPercent)
        XCTAssertNil(WatchBatterySnapshot(level: .nan, state: .unknown).levelPercent)
        XCTAssertNil(WatchBatterySnapshot(level: 1.01, state: .charging).levelPercent)
        XCTAssertEqual(WatchBatterySnapshot(level: 0, state: .unplugged).levelPercent, 0)
        XCTAssertEqual(WatchBatterySnapshot(level: 1, state: .full).levelPercent, 100)
    }

    func testCaptureKeepsValidLevelWhenBatteryStateIsUnknown() {
        let snapshot = WatchBatterySnapshot.capture(level: 0.42, state: .unknown)

        XCTAssertEqual(snapshot?.levelPercent, 42)
        XCTAssertEqual(snapshot?.state, .unknown)
    }

    func testCaptureOmitsUnavailableLevel() {
        XCTAssertNil(WatchBatterySnapshot.capture(level: -1, state: .charging))
    }

    /// WatchKit answers `batteryLevel` with -1 until it has published its first reading,
    /// which is exactly what the first segment of a fresh launch sees. Dropping the whole
    /// snapshot there used to leave every segment of a session without battery telemetry.
    func testResolutionCarriesForwardPreviousSnapshotWhenReadingIsUnavailable() {
        let previous = WatchBatterySnapshot(level: 0.64, state: .unplugged)

        let resolved = WatchBatterySnapshot.resolved(
            level: -1,
            state: .unknown,
            previous: previous
        )

        XCTAssertEqual(resolved, previous)
        XCTAssertFalse(resolved?.isFreshReading(level: -1, state: .unknown) ?? true)
    }

    func testResolutionPrefersFreshReadingOverCarriedForwardSnapshot() {
        let previous = WatchBatterySnapshot(level: 0.64, state: .unplugged)

        let resolved = WatchBatterySnapshot.resolved(
            level: 0.58,
            state: .charging,
            previous: previous
        )

        XCTAssertEqual(resolved?.levelPercent, 58)
        XCTAssertEqual(resolved?.state, .charging)
        XCTAssertTrue(resolved?.isFreshReading(level: 0.58, state: .charging) ?? false)
    }

    func testResolutionStillReportsNothingWithoutPreviousOrValidReading() {
        XCTAssertNil(WatchBatterySnapshot.resolved(level: -1, state: .unknown, previous: nil))
        XCTAssertNil(WatchBatterySnapshot.resolved(level: .nan, state: .unknown, previous: nil))
        // A bare state reading with no percentage is not a snapshot we can plot.
        XCTAssertNil(WatchBatterySnapshot.resolved(level: -1, state: .charging, previous: nil))
    }

    func testBatterySnapshotPersistsWithSegmentAndLegacySegmentStillDecodes() throws {
        let snapshot = WatchBatterySnapshot(level: 0.42, state: .unplugged)
        let segment = AudioSegment(
            sessionID: "session-a",
            sequence: 0,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 59_900,
            localURL: URL(fileURLWithPath: "/tmp/segment-000000.m4a"),
            byteCount: 123,
            sha256: "sha256:test",
            batterySnapshot: snapshot
        )
        let encoded = try JSONEncoder().encode(segment)
        let roundTripped = try JSONDecoder().decode(AudioSegment.self, from: encoded)
        XCTAssertEqual(roundTripped.batterySnapshot, snapshot)

        var legacyObject = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        legacyObject.removeValue(forKey: "batterySnapshot")
        let legacyData = try JSONSerialization.data(withJSONObject: legacyObject)
        let legacySegment = try JSONDecoder().decode(AudioSegment.self, from: legacyData)
        XCTAssertNil(legacySegment.batterySnapshot)
    }

    func testLedgerReloadPreservesDistinctBatterySnapshotsPerSegment() async throws {
        let rootURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("battery-ledger-\(UUID().uuidString)")
        try FileManager.default.createDirectory(
            at: rootURL,
            withIntermediateDirectories: true
        )
        defer { try? FileManager.default.removeItem(at: rootURL) }

        let ledger = SegmentLedger(rootURL: rootURL)
        _ = try await ledger.createSession(
            id: "battery-session",
            startedAt: Date(timeIntervalSince1970: 1_000)
        )
        for sequence in 0..<2 {
            let source = rootURL.appendingPathComponent("source-\(sequence).m4a")
            try Data("segment-\(sequence)".utf8).write(to: source)
            let snapshot = WatchBatterySnapshot(
                level: sequence == 0 ? 0.76 : 0.71,
                state: .unplugged
            )
            _ = try await ledger.appendSegment(
                sessionID: "battery-session",
                sequence: sequence,
                sourceURL: source,
                startOffsetMilliseconds: Int64(sequence) * 60_000,
                durationMilliseconds: 60_000,
                batterySnapshot: snapshot
            )
        }

        let relaunchedLedger = SegmentLedger(rootURL: rootURL)
        let restoredManifest = try await relaunchedLedger.loadSession(
            id: "battery-session"
        )
        let restored = try XCTUnwrap(restoredManifest)
        XCTAssertEqual(restored.segments.map(\.batterySnapshot), [
            WatchBatterySnapshot(level: 0.76, state: .unplugged),
            WatchBatterySnapshot(level: 0.71, state: .unplugged),
        ])
    }
}
