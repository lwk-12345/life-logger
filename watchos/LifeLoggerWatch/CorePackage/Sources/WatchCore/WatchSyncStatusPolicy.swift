import Foundation

public enum WatchConnectionFailure: Equatable, Sendable {
    case unreachable
    case directAccessDisabled
    case unauthorized
    case serverRejected(statusCode: Int, detail: String?)
    case invalidResponse
}

public enum WatchSyncStatusPolicy {
    public static func classifyTransportError(_ error: Error) -> WatchConnectionFailure {
        if error is URLError || (error as NSError).domain == NSURLErrorDomain {
            return .unreachable
        }
        return .invalidResponse
    }

    public static func captureStatus(
        isRecoveringStagedAudio: Bool,
        hasUnrecoveredStagedAudio: Bool,
        isRecording: Bool,
        hasActiveRecordingSession: Bool
    ) -> String? {
        if isRecoveringStagedAudio {
            return "正在检查手表本地录音…"
        }
        if hasUnrecoveredStagedAudio {
            return "有暂存录音尚未恢复，不能视为已同步。"
        }
        if isRecording {
            return "正在录音，当前这段还在保存。"
        }
        if hasActiveRecordingSession {
            return "录音还没结束；待上传数量只包括已保存在手表的分段。"
        }
        return nil
    }

    public static func queueStatus(pendingSegments: Int) -> String {
        guard pendingSegments > 0 else {
            return "没有待 Mac 接收的录音。"
        }
        return "还有 \(pendingSegments) 段未传到 Mac，音频仍保存在手表。"
    }

    /// The Mac explains itself: every rejection carries a `detail` sentence. Keep it in the
    /// message so the Watch screen shows *why* the sync stopped instead of a generic
    /// "upload failed" that forces the reason to be guessed at.
    public static func failureMessage(for error: WatchSyncError) -> String {
        switch error {
        case .sessionCompletionFailed(let detail):
            return "Mac 已收到录音，但结束状态尚未同步：\(detail)"
        case .acknowledgementMismatch(let detail):
            return "Mac 返回的接收信息与录音不匹配：\(detail)"
        case .sessionRegistrationFailed(let detail):
            return "Mac 会话登记失败：\(detail)"
        case .segmentUploadFailed(let detail):
            return "Mac 没有返回有效的接收信息：\(detail)"
        case .segmentsMissingLocally(let sequences):
            let list = sequences.map(String.init).joined(separator: "、")
            return "有 \(sequences.count) 个片段在手表上已丢失（序号 \(list)），其余片段已上传并结束该会话"
        }
    }

    public static func connectionFailureMessage(for failure: WatchConnectionFailure) -> String {
        switch failure {
        case .unreachable:
            return "Mac 当前不可达，手表本地队列未更改。"
        case .directAccessDisabled:
            return "已连接 Mac，但 Watch 直连功能未开放。"
        case .unauthorized:
            return "已连接 Mac，但访问未授权（HTTP 401）。"
        case .serverRejected(let statusCode, let detail):
            if let detail, !detail.isEmpty {
                return "Mac 拒绝请求（HTTP \(statusCode)）：\(detail)"
            }
            return "已连接 Mac，但服务拒绝请求（HTTP \(statusCode)）。"
        case .invalidResponse:
            return "已连接 Mac，但返回内容无法识别。"
        }
    }
}
