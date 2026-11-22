import XCTest
@testable import WatchCore

final class WatchCaptureRuntimePolicyTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_800_000_000)

    func testContinuousSegmentKeepsPreviousEnd() {
        let end = start.addingTimeInterval(120.3)
        XCTAssertEqual(WatchRotatedTimingPolicy.startOffset(
            sequence: 1, previousEndMilliseconds: 60_000, sessionStartedAt: start,
            segmentEndedAt: end, durationMilliseconds: 60_000), 60_000)
    }

    func testInterruptionGapIsPreserved() {
        // 第一段结束后被打断 10 分钟。
        let end = start.addingTimeInterval(60 + 600 + 60)
        XCTAssertEqual(WatchRotatedTimingPolicy.startOffset(
            sequence: 1, previousEndMilliseconds: 60_000, sessionStartedAt: start,
            segmentEndedAt: end, durationMilliseconds: 60_000), 660_000)
    }

    func testFirstSegmentAndMissingStartStaySafe() {
        XCTAssertEqual(WatchRotatedTimingPolicy.startOffset(
            sequence: 0, previousEndMilliseconds: 0, sessionStartedAt: start,
            segmentEndedAt: start.addingTimeInterval(70), durationMilliseconds: 60_000), 0)
        XCTAssertEqual(WatchRotatedTimingPolicy.startOffset(
            sequence: 2, previousEndMilliseconds: 120_000, sessionStartedAt: nil,
            segmentEndedAt: start, durationMilliseconds: 60_000), 120_000)
    }

    func testBackoffGrowsAndCaps() {
        var backoff = WatchUploadBackoff()
        XCTAssertTrue(backoff.shouldAttempt(at: start))
        backoff.recordFailure(at: start)
        XCTAssertFalse(backoff.shouldAttempt(at: start.addingTimeInterval(59)))
        XCTAssertTrue(backoff.shouldAttempt(at: start.addingTimeInterval(60)))
        for _ in 0..<10 { backoff.recordFailure(at: start) }
        XCTAssertFalse(backoff.shouldAttempt(at: start.addingTimeInterval(599)))
        XCTAssertTrue(backoff.shouldAttempt(at: start.addingTimeInterval(600)))
        backoff.recordSuccess()
        XCTAssertTrue(backoff.shouldAttempt(at: start))
    }

    func testRediscoveryIsRateLimited() {
        var backoff = WatchUploadBackoff()
        backoff.recordFailure(at: start)
        XCTAssertFalse(backoff.shouldRediscover(at: start))
        backoff.recordFailure(at: start)
        XCTAssertTrue(backoff.shouldRediscover(at: start))
        backoff.recordRediscovery(at: start)
        XCTAssertFalse(backoff.shouldRediscover(at: start.addingTimeInterval(299)))
        XCTAssertTrue(backoff.shouldRediscover(at: start.addingTimeInterval(300)))
    }

    func testStorageLevels() {
        XCTAssertEqual(WatchStoragePolicy.level(availableBytes: nil), .ok)
        XCTAssertEqual(WatchStoragePolicy.level(availableBytes: 1_000_000_000), .ok)
        XCTAssertEqual(WatchStoragePolicy.level(availableBytes: 400_000_000), .low)
        XCTAssertEqual(WatchStoragePolicy.level(availableBytes: 10_000_000), .critical)
    }

    func testChallengeResponseMatchesKnownHmac() {
        // python: hmac.new(b"key", b"abc", "sha256").hexdigest()
        XCTAssertEqual(WatchMacChallenge.response(challenge: "abc", key: "key"),
                       "9c196e32dc0175f86f4b1cb89289d6619de6bee699e4c378e68309ed97a1a6ab")
        XCTAssertNotEqual(WatchMacChallenge.response(challenge: "abc", key: "other"),
                          WatchMacChallenge.response(challenge: "abc", key: "key"))
    }

    func testRecoverySequenceRenumbersOnlyWhenThereIsAHole() {
        XCTAssertEqual(WatchRecoverySequencePolicy.target(fileSequence: 2, committed: [0]), 1)
        XCTAssertEqual(WatchRecoverySequencePolicy.target(fileSequence: 1, committed: [0]), 1)
        XCTAssertEqual(WatchRecoverySequencePolicy.target(fileSequence: 0, committed: [0, 1]), 0)
        XCTAssertEqual(WatchRecoverySequencePolicy.target(fileSequence: 5, committed: []), 0)
    }

    func testBackoffResetClearsFailures() {
        var backoff = WatchUploadBackoff()
        backoff.recordFailure(at: start)
        backoff.reset()
        XCTAssertTrue(backoff.shouldAttempt(at: start))
        XCTAssertEqual(backoff.consecutiveFailures, 0)
    }
}
