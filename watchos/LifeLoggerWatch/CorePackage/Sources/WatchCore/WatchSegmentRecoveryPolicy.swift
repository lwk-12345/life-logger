import Foundation

/// What to do about a session whose local segment files are gone.
///
/// The Watch keeps its own ledger, and a segment can disappear from storage while the ledger
/// still lists it as pending — an interrupted recording, a file moved to quarantine, the
/// system reclaiming space. `WatchSyncEngine` then tries to upload a file that is not there,
/// fails with a local error, and retries on every sync forever. On the real device that showed
/// up as nine sessions that would never finish and a register call on every attempt.
///
/// Nothing may silently drop captured audio, so a missing file becomes an explicit, reported
/// state and the decision to finish a partial session stays with the user.
public enum WatchSegmentRecoveryPolicy {
    public enum Verdict: Equatable, Sendable {
        /// Every segment listed in the ledger is present on disk.
        case intact
        /// Some segments are gone; the rest can still be uploaded and the session finished.
        case partial(missing: [Int], presentCount: Int)
        /// Nothing usable is left locally.
        case empty
    }

    public static func verdict(
        segments: [(sequence: Int, fileExists: Bool)]
    ) -> Verdict {
        guard !segments.isEmpty else { return .empty }
        let missing = segments.filter { !$0.fileExists }.map(\.sequence).sorted()
        if missing.isEmpty { return .intact }
        let present = segments.count - missing.count
        if present == 0 { return .empty }
        return .partial(missing: missing, presentCount: present)
    }

    /// Splits a session's segments into what can still be uploaded and what is gone.
    ///
    /// The gone ones are reported, never silently dropped, and they must not stop the rest:
    /// a recording whose audio survived is worth sending even if one file did not.
    public static func partition(
        segments: [(sequence: Int, fileExists: Bool)]
    ) -> (uploadable: [Int], missing: [Int]) {
        var uploadable: [Int] = []
        var missing: [Int] = []
        for segment in segments.sorted(by: { $0.sequence < $1.sequence }) {
            if segment.fileExists {
                uploadable.append(segment.sequence)
            } else {
                missing.append(segment.sequence)
            }
        }
        return (uploadable, missing)
    }

    /// True when this session can never be uploaded as-is, so retrying it every sync only
    /// burns battery. The session needs a decision, not another attempt.
    public static func needsUserDecision(_ verdict: Verdict) -> Bool {
        switch verdict {
        case .intact: return false
        case .partial, .empty: return true
        }
    }

    public static func summary(_ verdict: Verdict, sessionCount: Int) -> String? {
        switch verdict {
        case .intact:
            return nil
        case .partial(let missing, let present):
            return "\(sessionCount) 个会话有问题：其中 \(missing.count) 个片段在手表上已丢失，剩余 \(present) 个可上传"
        case .empty:
            return "\(sessionCount) 个会话的录音片段在手表上已全部丢失，无法上传"
        }
    }
}
