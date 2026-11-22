/// Chooses a safe closeout path for a Watch capture session.
///
/// A session can remain active after an audio interruption even when its
/// `AVAudioRecorder` has already stopped or been released. Callers must still
/// finalize the session instead of treating the stop request as a no-op.
public enum CaptureStopDisposition: Equatable, Sendable {
    case ignore
    case stopRecorderAndAwaitCompletion
    case finalizeStoppedRecorder
    case finalizeSessionWithoutRecorder

    public static func resolve(
        hasActiveSession: Bool,
        hasRecorder: Bool,
        recorderIsRecording: Bool
    ) -> Self {
        guard hasActiveSession else { return .ignore }
        guard hasRecorder else { return .finalizeSessionWithoutRecorder }
        return recorderIsRecording ? .stopRecorderAndAwaitCompletion : .finalizeStoppedRecorder
    }
}
