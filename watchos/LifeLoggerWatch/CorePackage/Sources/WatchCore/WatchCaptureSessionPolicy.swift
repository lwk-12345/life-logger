import Foundation

/// Whether the app should be holding an *active* audio session.
///
/// This is the difference between "recording" and "pinned awake". An active audio session keeps
/// a watchOS app alive, so holding one while nothing is being captured means the Watch will not
/// sleep and the recorder screen stays lit indefinitely — which is exactly what a user saw on
/// the real device after a system audio interruption.
///
/// The rule: an active session is justified only while a recorder is actually capturing.
public enum WatchCaptureSessionPolicy {
    public enum Disposition: Equatable, Sendable {
        /// Keep the session as it is.
        case unchanged
        /// Activate it, then (re)start a segment.
        case activate
        /// Deactivate it, because capture is not running.
        case release
    }

    public static func disposition(
        hasActiveSession: Bool,
        isRecording: Bool,
        isInterrupted: Bool,
        stopRequested: Bool,
        hasOpenSession: Bool
    ) -> Disposition {
        guard hasOpenSession, !stopRequested else {
            return hasActiveSession ? .release : .unchanged
        }
        if isRecording {
            // Capture is running: the active session is earning its keep.
            return hasActiveSession ? .unchanged : .activate
        }
        if isInterrupted {
            // Nothing is being captured, so nothing justifies keeping the app awake. The
            // caller may try to resume; if it cannot, the session must stay released.
            return hasActiveSession ? .release : .unchanged
        }
        // Not recording, not interrupted: a rotation is in flight and the next segment is
        // about to start, so the session is still needed.
        return hasActiveSession ? .unchanged : .activate
    }
}
