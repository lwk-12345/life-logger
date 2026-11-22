import Foundation

/// The one request that has to survive the app being suspended: the session *completion*
/// POST.
///
/// Segment uploads are self-healing — a suspended upload simply stays `.pending` in the
/// ledger and is retried on the next sync. Completion is not: if it never lands, the Mac
/// keeps the session in `receiving`, never assembles the audio, and the recording looks
/// "unsyncable" even though every segment arrived. That is exactly what stranded eleven
/// sessions on the real device.
///
/// 【2026-10-15 文档订正】实现上**并不是** background `URLSession`：watchOS 上
/// `URLSessionConfiguration.background` 是特权 API，缺少配套 background-task 管道时，
/// URL loading system 会在启动几秒后于 CFNetwork 内部直接 SIGABRT（真机上每次启动都崩）。
/// 所以 `WatchCompletionQueue` 故意改用**前台** session（见该文件 `attachLocked()` 的说明），
/// 并把「这次没送达」交给下一次前台同步从 ledger 重扫重发
/// （见 `MacWatchSessionTransport.complete`；`stoppedAt != nil && macJobID == nil` 的会话会被重试）。
/// 这一段原本写的是 background session，与实现矛盾，会误导后续维护者。
public enum WatchCompletionRequest {
    /// 仅供查阅/历史对齐：当前实现**未**使用 background session。
    public static let backgroundSessionIdentifier = "com.lifelogger.watch.mac-sync"

    /// Stable path segments, in order, for a completion POST of one recording session.
    public static func path(
        deviceID: String,
        sessionID: String
    ) -> [String] {
        ["api", "watch-sessions", deviceID, sessionID, "complete"]
    }

    /// Body for the completion POST. `expected_segments` counts what we actually have, and
    /// `ended_at` is the moment recording stopped, as the Mac requires ISO-8601 with a zone.
    public static func body(
        segmentCount: Int,
        stoppedAt: Date,
        capsules: [WatchSessionCapsule] = []
    ) -> Data? {
        guard segmentCount > 0 else { return nil }
        var payload: [String: Any] = [
            "expected_segments": segmentCount,
            "ended_at": iso8601(stoppedAt),
        ]
        if !capsules.isEmpty {
            if let data = try? JSONEncoder().encode(capsules),
               let json = try? JSONSerialization.jsonObject(with: data) {
                payload["capsules"] = json
            }
        }
        return try? JSONSerialization.data(withJSONObject: payload)
    }

    /// Use the saved audio timeline as the lower bound for a closed session's completion.
    /// Recovery can append a tail after an older stoppedAt was persisted. Sending that
    /// unchanged timestamp makes the Mac reject every retry, even after every ACK arrived.
    /// Only the request is normalized; the ledger's original timestamp is preserved.
    public static func body(session: RecordingSessionManifest) -> Data? {
        guard let stoppedAt = session.stoppedAt else { return nil }
        let endedAt = max(stoppedAt, SegmentLedger.audioTimelineEnd(of: session) ?? stoppedAt)
        return body(segmentCount: session.segments.count, stoppedAt: endedAt, capsules: session.allCapsules)
    }

    public static func iso8601(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }

    /// What the Mac returned for one completion attempt.
    public enum Outcome: Equatable, Sendable {
        /// The Mac accepted it (or had already accepted it) and named the job.
        case accepted(jobID: String)
        /// The Mac answered, but refused: this is its own sentence and must reach the user.
        case rejected(detail: String)
        /// We never got a usable answer (no response, unreadable body).
        case indeterminate(String)
    }

    public static func outcome(
        from body: Data,
        statusCode: Int
    ) -> Outcome {
        let payload = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
        if let jobID = payload?["job_id"] as? String, !jobID.isEmpty {
            return .accepted(jobID: jobID)
        }
        let detail = (payload?["detail"] as? String)
            ?? (payload?["error"] as? String)
            ?? "Mac returned HTTP \(statusCode) without a reason"
        if (200..<300).contains(statusCode) {
            // A 2xx without a job id cannot be recorded as success: the ledger needs the id.
            return .indeterminate(detail)
        }
        return .rejected(detail: detail)
    }

    /// Which session a finished background transfer belonged to. The identifier is derived
    /// from the request URL, so no side-car bookkeeping file has to survive a crash.
    public static func sessionIdentity(
        fromResponseURL url: URL?,
        taskDescription: String?
    ) -> (deviceID: String, sessionID: String)? {
        if let url, let parsed = parsePath(url.pathComponents) {
            return parsed
        }
        if let taskDescription, let parsed = parsePath(taskDescription.split(separator: "/").map(String.init)) {
            return parsed
        }
        return nil
    }

    private static func parsePath(_ components: [String]) -> (deviceID: String, sessionID: String)? {
        let parts = components.filter { $0 != "/" && !$0.isEmpty }
        guard parts.count == 5,
              parts[0] == "api",
              parts[1] == "watch-sessions",
              parts[4] == "complete" else {
            return nil
        }
        return (deviceID: parts[2], sessionID: parts[3])
    }
}


/// Where a background completion ends up, and the last problem it hit.
///
/// The recorder reads `latestIssue` after a sync so a refusal that arrived while the app was
/// suspended still reaches the screen. Holding it here keeps the background path out of the
/// app's launch sequence: nothing is constructed until a session actually finishes.
public actor WatchBackgroundDelivery {
    private let ledger: SegmentLedger
    private var latestIssue: String?

    public init(ledger: SegmentLedger) {
        self.ledger = ledger
    }

    public func record(sessionID: String, outcome: WatchCompletionRequest.Outcome) async {
        switch outcome {
        case .accepted(let jobID):
            do {
                try await ledger.markSessionImported(id: sessionID, macJobID: jobID)
            } catch {
                latestIssue = "completion accepted but not recorded: \(error.localizedDescription)"
            }
        case .rejected(let detail), .indeterminate(let detail):
            latestIssue = detail
        }
    }

    public func reportIssue(_ message: String) {
        latestIssue = message
    }

    /// Returns the pending issue once, clearing it, so the same message is not shown twice.
    public func takeLatestIssue() -> String? {
        defer { latestIssue = nil }
        return latestIssue
    }
}
