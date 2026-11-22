import Foundation
import XCTest
@testable import WatchCore

final class WatchLocationPolicyTests: XCTestCase {
    private var rootURL: URL!
    private var ledger: SegmentLedger!

    override func setUpWithError() throws {
        rootURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("watch-location-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: rootURL,
            withIntermediateDirectories: true
        )
        ledger = SegmentLedger(rootURL: rootURL)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: rootURL)
    }

    /// 1) 节流逻辑单测：
    /// - 首次上报附带 location
    /// - 距上次 <= 100 米 且 <= 5 分钟 → 省略 location（不写 null）
    /// - 距上次 > 100 米 → 附带 location
    /// - 距上次 <= 100 米 但 > 5 分钟 → 附带 location
    func testLocationThrottlingByDistanceAndElapsedInterval() async throws {
        let tz = try XCTUnwrap(TimeZone(secondsFromGMT: 8 * 3600))
        var tracker = WatchLocationThrottleTracker(
            authorization: .authorizedWhenInUse,
            timeZone: tz
        )
        let t0 = Date(timeIntervalSince1970: 1_760_000_000)

        // Segment 0: Initial valid fix at People's Square (31.2304, 121.4737)
        let initialFix = WatchLocationCandidate(
            latitude: 31.2304,
            longitude: 121.4737,
            horizontalAccuracy: 65.0,
            timestamp: t0
        )
        tracker.recordCandidate(initialFix)
        let seg0Location = tracker.consumeSnapshotForSegment(evaluatedAt: t0)
        let firstSnapshot = try XCTUnwrap(seg0Location)
        XCTAssertEqual(firstSnapshot.latitude, 31.2304, accuracy: 1e-6)
        XCTAssertEqual(firstSnapshot.longitude, 121.4737, accuracy: 1e-6)
        XCTAssertEqual(firstSnapshot.horizontalAccuracy, 65.0, accuracy: 1e-6)
        XCTAssertEqual(firstSnapshot.source, "significant_change")
        XCTAssertTrue(firstSnapshot.timestamp.hasSuffix("+08:00"))

        // Segment 1: 2 minutes later and ~55 meters north
        // Neither >100m nor >5min is met -> must be throttled (nil)
        let t1 = t0.addingTimeInterval(120)
        let nearbyFix = WatchLocationCandidate(
            latitude: 31.2309,
            longitude: 121.4737,
            horizontalAccuracy: 65.0,
            timestamp: t1
        )
        tracker.recordCandidate(nearbyFix)
        let seg1Location = tracker.consumeSnapshotForSegment(evaluatedAt: t1)
        XCTAssertNil(seg1Location, "Should omit location when < 100m and < 5min")

        // Segment 2: 3 minutes after t0, moved ~778 meters north (+0.007 deg lat > 500m)
        // Distance > 100m -> must include location
        let t2 = t0.addingTimeInterval(180)
        let movedFix = WatchLocationCandidate(
            latitude: 31.2374,
            longitude: 121.4737,
            horizontalAccuracy: 50.0,
            timestamp: t2
        )
        tracker.recordCandidate(movedFix)
        let seg2Location = tracker.consumeSnapshotForSegment(evaluatedAt: t2)
        let movedSnapshot = try XCTUnwrap(seg2Location, "Should include location when distance > 100m")
        XCTAssertEqual(movedSnapshot.latitude, 31.2374, accuracy: 1e-6)
        XCTAssertEqual(movedSnapshot.horizontalAccuracy, 50.0, accuracy: 1e-6)
        XCTAssertEqual(movedSnapshot.source, "significant_change")

        // Segment 3: 2 minutes after t2, stationary -> throttled (nil)
        let t3 = t2.addingTimeInterval(120)
        XCTAssertNil(tracker.consumeSnapshotForSegment(evaluatedAt: t3))

        // Segment 4: 5 minutes + 1 second after t2, still stationary
        // Elapsed time > 5 minutes -> must include location
        let t4 = t2.addingTimeInterval(301)
        let timeTriggeredSnapshot = try XCTUnwrap(
            tracker.consumeSnapshotForSegment(evaluatedAt: t4),
            "Should include location when elapsed time > 5 minutes even if stationary"
        )
        XCTAssertEqual(timeTriggeredSnapshot.latitude, 31.2374, accuracy: 1e-6)
        XCTAssertEqual(timeTriggeredSnapshot.source, "significant_change")

        // Verify manifest.json encoding for reported vs throttled segments (no null placeholder)
        _ = try await ledger.createSession(id: "throttle-session", startedAt: t0)
        let src0 = rootURL.appendingPathComponent("s0.m4a")
        let src1 = rootURL.appendingPathComponent("s1.m4a")
        try Data("audio-0".utf8).write(to: src0)
        try Data("audio-1".utf8).write(to: src1)

        _ = try await ledger.appendSegment(
            sessionID: "throttle-session",
            sequence: 0,
            sourceURL: src0,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000,
            location: firstSnapshot
        )
        _ = try await ledger.appendSegment(
            sessionID: "throttle-session",
            sequence: 1,
            sourceURL: src1,
            startOffsetMilliseconds: 60_000,
            durationMilliseconds: 60_000,
            location: seg1Location // nil (throttled)
        )

        let manifestFileURL = rootURL
            .appendingPathComponent("sessions/throttle-session/manifest.json")
        let rawJSON = try Data(contentsOf: manifestFileURL)
        let parsed = try XCTUnwrap(
            JSONSerialization.jsonObject(with: rawJSON) as? [String: Any]
        )
        let segments = try XCTUnwrap(parsed["segments"] as? [[String: Any]])
        XCTAssertEqual(segments.count, 2)

        let seg0LocationJSON = try XCTUnwrap(segments[0]["location"] as? [String: Any])
        XCTAssertEqual(seg0LocationJSON["latitude"] as? Double, 31.2304)
        XCTAssertEqual(seg0LocationJSON["longitude"] as? Double, 121.4737)
        XCTAssertEqual(seg0LocationJSON["horizontal_accuracy"] as? Double, 65.0)
        XCTAssertEqual(seg0LocationJSON["source"] as? String, "significant_change")
        XCTAssertNotNil(seg0LocationJSON["timestamp"] as? String)

        // Segment 1 was throttled -> "location" key must be completely absent (not NSNull)
        XCTAssertFalse(
            segments[1].keys.contains("location"),
            "Throttled segment must omit 'location' key entirely rather than writing null"
        )
    }

    /// 2) 无权限及权限被撤销时静默降级单测：
    /// - 用户拒绝权限（.denied / .restricted / .notDetermined）或录制中途撤销权限时，静默返回 nil
    /// - 绝不影响录音分段写入、manifest.json 持久化和上传确认流程
    /// - manifest.json 中完全省略 location 字段，绝不写入 null 占位
    func testLocationDegradesSilentlyWhenPermissionDeniedOrRevokedWithoutBlockingRecordingOrUpload() async throws {
        var tracker = WatchLocationThrottleTracker(authorization: .denied)
        let now = Date(timeIntervalSince1970: 1_760_010_000)
        let candidate = WatchLocationCandidate(
            latitude: 39.9042,
            longitude: 116.4074,
            horizontalAccuracy: 100.0,
            timestamp: now
        )

        // Denied on first launch -> silently returns nil
        tracker.recordCandidate(candidate)
        XCTAssertNil(tracker.consumeSnapshotForSegment(evaluatedAt: now))

        // Restricted / notDetermined -> silently returns nil
        tracker.updateAuthorization(.restricted)
        tracker.recordCandidate(candidate)
        XCTAssertNil(tracker.consumeSnapshotForSegment(evaluatedAt: now))

        tracker.updateAuthorization(.notDetermined)
        tracker.recordCandidate(candidate)
        XCTAssertNil(tracker.consumeSnapshotForSegment(evaluatedAt: now))

        // Granted -> works, then revoked mid-recording -> clears cached fix and silently returns nil
        tracker.updateAuthorization(.authorizedWhenInUse)
        tracker.recordCandidate(candidate)
        XCTAssertNotNil(tracker.consumeSnapshotForSegment(evaluatedAt: now))

        // Revoke permission mid-session
        tracker.updateAuthorization(.denied)
        let afterRevoke = tracker.consumeSnapshotForSegment(
            evaluatedAt: now.addingTimeInterval(3_600)
        )
        XCTAssertNil(afterRevoke, "Revoked permission must immediately degrade to nil")

        // Verify recording ledger and upload lifecycle work normally when location is nil
        _ = try await ledger.createSession(id: "no-perm-session", startedAt: now)
        let source = rootURL.appendingPathComponent("segment-no-perm.m4a")
        try Data("normal-audio-payload".utf8).write(to: source)

        let savedSegment = try await ledger.appendSegment(
            sessionID: "no-perm-session",
            sequence: 0,
            sourceURL: source,
            startOffsetMilliseconds: 0,
            durationMilliseconds: 60_000,
            batterySnapshot: WatchBatterySnapshot(level: 0.85, state: .unplugged),
            location: afterRevoke
        )
        XCTAssertNil(savedSegment.location)

        let manifestURL = rootURL
            .appendingPathComponent("sessions/no-perm-session/manifest.json")
        let manifestString = try String(contentsOf: manifestURL, encoding: .utf8)
        XCTAssertFalse(
            manifestString.contains("\"location\""),
            "Manifest must completely omit 'location' key when permission is denied"
        )
        XCTAssertFalse(
            manifestString.contains("null"),
            "Manifest must never write null placeholders"
        )

        // Upload state transitions and acknowledgement succeed normally without location
        try await ledger.markUploading(sessionID: "no-perm-session", sequence: 0)
        try await ledger.acknowledge(
            sessionID: "no-perm-session",
            sequence: 0,
            sha256: savedSegment.sha256
        )
        let reloaded = try await ledger.loadSession(id: "no-perm-session")
        XCTAssertEqual(reloaded?.segments.first?.uploadState, .acknowledged)
        XCTAssertNil(reloaded?.segments.first?.location)
    }

    func testLocationPolicyEnforcesPowerSavingConstraints() {
        XCTAssertTrue(WatchLocationPolicy.usesSignificantLocationChangesOnly)
        XCTAssertFalse(WatchLocationPolicy.allowsContinuousLocationUpdates)
        XCTAssertFalse(WatchLocationPolicy.allowsBackgroundLocationUpdates)
        XCTAssertEqual(WatchLocationPolicy.desiredAccuracyMeters, 100.0)
        XCTAssertEqual(WatchLocationPolicy.minimumDistanceMeters, 100.0)
        XCTAssertEqual(WatchLocationPolicy.minimumIntervalSeconds, 300.0)
    }
}
