/// Decides whether a completed recorder segment may roll into another segment.
public enum CaptureSegmentDisposition: Equatable, Sendable {
    case continueRecording
    case pauseForInterruption
    case finalizeSession

    public static func resolve(
        stateIsRecording: Bool,
        stopRequested: Bool,
        audioInterrupted: Bool,
        segmentSucceeded: Bool
    ) -> Self {
        guard stateIsRecording, !stopRequested else {
            return .finalizeSession
        }
        if audioInterrupted {
            return .pauseForInterruption
        }
        guard segmentSucceeded else {
            return .finalizeSession
        }
        return .continueRecording
    }
}
