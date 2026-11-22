import Foundation

/// 判断录音是否真的在写文件。
///
/// 只看音频引擎不够：媒体服务重置后，路由变化通知可能把引擎拉起来，但没有分段在写，
/// 界面一直显示“正在录音”，实际没有音频。
public enum WatchCaptureResumePolicy {
    public enum Decision: Equatable, Sendable {
        case alreadyCapturing
        case reviveEngine
        case startNewSegment
        /// 上一段已结束但还没登记，由分段收尾流程处理，这里不抢。
        case waitForSegmentHandling
    }

    public static func decide(
        recorderExists: Bool,
        recorderIsRecording: Bool,
        recorderWasHandled: Bool,
        engineRunning: Bool
    ) -> Decision {
        if recorderIsRecording {
            return engineRunning ? .alreadyCapturing : .reviveEngine
        }
        if recorderExists && !recorderWasHandled {
            return .waitForSegmentHandling
        }
        return .startNewSegment
    }

    /// 看门狗只有在“引擎在跑、分段在写、输入正常”时才可以放心返回。
    public static func captureIsHealthy(
        engineRunning: Bool,
        recorderIsRecording: Bool,
        livenessIssueFound: Bool
    ) -> Bool {
        engineRunning && recorderIsRecording && !livenessIssueFound
    }
}
