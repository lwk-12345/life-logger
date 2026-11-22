import Foundation

/// Rules for keeping a recording alive across the moments where it is most fragile:
/// the segment boundary, and a system audio interruption.
///
/// Why this exists: the old code made exactly one attempt to start the next segment at the
/// 60 s boundary and, if that single attempt failed, closed the whole recording. A system
/// interruption was worse: it stopped the recorder and then waited for an `.ended`
/// notification that a backgrounded Watch app frequently never receives. Both read, from the
/// user's side, as "it always stops after about a minute".
public enum WatchCaptureRecoveryPolicy {
    public enum Cause: Equatable, Sendable {
        /// The recorder did not start at a segment boundary.
        case rotationFailed
        /// The system took the microphone (call, Siri, alarm, ...).
        case interruption
    }

    public enum Step: Equatable, Sendable {
        /// Wait this long, then try to resume again.
        case retry(afterSeconds: Double)
        /// Stop retrying.
        case giveUp
    }

    /// Rotation failures have a bounded recovery window. Interruptions switch to a
    /// slower retry cadence after this window, while the process remains scheduled.
    public static func graceSeconds(for cause: Cause) -> Double {
        switch cause {
        case .rotationFailed: 90
        case .interruption: 120
        }
    }

    public static func nextStep(
        cause: Cause,
        attempt: Int,
        elapsedSeconds: Double
    ) -> Step {
        if cause == .interruption, elapsedSeconds >= graceSeconds(for: cause) {
            return .retry(afterSeconds: 30)
        }
        guard elapsedSeconds < graceSeconds(for: cause) else { return .giveUp }
        let attempt = max(1, attempt)
        // Interruptions are retried more gently: hammering the session while a call holds it
        // only produces more interruptions.
        let base: Double = cause == .interruption ? 2 : 0.5
        let delay = min(8, base * pow(2, Double(attempt - 1)))
        return .retry(afterSeconds: delay)
    }

    /// Seconds between watchdog checks while a session is open.
    public static let watchdogIntervalSeconds: Double = 5

    /// Capture, including interruption recovery, always takes priority over sync.
    public static func shouldSync(
        hasOpenCaptureSession: Bool,
        pendingSegments: Int
    ) -> Bool {
        guard hasOpenCaptureSession else { return true }
        return false
    }
}
