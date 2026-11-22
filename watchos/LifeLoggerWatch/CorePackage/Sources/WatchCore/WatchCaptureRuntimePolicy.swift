import CryptoKit
import Foundation

/// 录音时间轴：中断造成的缺口要保留在时间轴上，不能把后面的内容往前压。
public enum WatchRotatedTimingPolicy {
    public static let gapThresholdMilliseconds: Int64 = 5_000

    public static func startOffset(
        sequence: Int,
        previousEndMilliseconds: Int64,
        sessionStartedAt: Date?,
        segmentEndedAt: Date,
        durationMilliseconds: Int64
    ) -> Int64 {
        guard sequence > 0 else { return 0 }
        guard let sessionStartedAt else { return previousEndMilliseconds }
        let wallClock = Int64(segmentEndedAt.timeIntervalSince(sessionStartedAt) * 1000) - durationMilliseconds
        return wallClock - previousEndMilliseconds >= gapThresholdMilliseconds ? wallClock : previousEndMilliseconds
    }
}

/// 恢复暂存分段时的序号：账本要求连号，文件序号有空洞时按账本重排，音频才不会永久滞留。
public enum WatchRecoverySequencePolicy {
    public static func target(fileSequence: Int, committed: Set<Int>) -> Int {
        if committed.contains(fileSequence) { return fileSequence }
        return min(fileSequence, committed.count)
    }
}

/// 录音期间上传失败后的退避，以及什么时候重新找 Mac。
public struct WatchUploadBackoff: Equatable, Sendable {
    public static let maximumDelay: TimeInterval = 600
    public static let rediscoverEvery: TimeInterval = 300
    public static let failuresBeforeRediscovery = 2

    public private(set) var consecutiveFailures = 0
    public private(set) var lastFailureAt: Date?
    public private(set) var lastRediscoveryAt: Date?

    public init() {}

    public func shouldAttempt(at now: Date) -> Bool {
        guard consecutiveFailures > 0, let lastFailureAt else { return true }
        let delay = min(Self.maximumDelay, 60 * pow(2, Double(consecutiveFailures - 1)))
        return now.timeIntervalSince(lastFailureAt) >= delay
    }

    public func shouldRediscover(at now: Date) -> Bool {
        guard consecutiveFailures >= Self.failuresBeforeRediscovery else { return false }
        guard let lastRediscoveryAt else { return true }
        return now.timeIntervalSince(lastRediscoveryAt) >= Self.rediscoverEvery
    }

    public mutating func recordFailure(at now: Date) {
        consecutiveFailures += 1
        lastFailureAt = now
    }

    public mutating func reset() {
        self = WatchUploadBackoff()
    }

    public mutating func recordSuccess() {
        consecutiveFailures = 0
        lastFailureAt = nil
    }

    public mutating func recordRediscovery(at now: Date) {
        lastRediscoveryAt = now
    }
}

/// 手表剩余空间：提前提醒，真的放不下时停止，并给账本留余量。
public enum WatchStoragePolicy {
    public enum Level: Equatable, Sendable {
        case ok, low, critical
    }

    public static let lowBytes: Int64 = 500 * 1024 * 1024
    public static let criticalBytes: Int64 = 80 * 1024 * 1024

    public static func level(availableBytes: Int64?) -> Level {
        guard let availableBytes else { return .ok }
        if availableBytes < criticalBytes { return .critical }
        return availableBytes < lowBytes ? .low : .ok
    }
}

/// 探活时让 Mac 证明自己持有密钥，密钥本身不上线路。
public enum WatchMacChallenge {
    public static let challengeHeader = "X-LifeLogger-Watch-Challenge"

    public static func response(challenge: String, key: String) -> String {
        let code = HMAC<SHA256>.authenticationCode(
            for: Data(challenge.utf8),
            using: SymmetricKey(data: Data(key.utf8))
        )
        return code.map { String(format: "%02x", $0) }.joined()
    }

    public static func makeChallenge() -> String {
        UUID().uuidString.replacingOccurrences(of: "-", with: "")
    }
}
