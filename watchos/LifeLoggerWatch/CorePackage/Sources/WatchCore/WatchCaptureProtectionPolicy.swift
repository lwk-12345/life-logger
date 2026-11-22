import Foundation

/// Policy rules and thresholds for Watch recording anti-accidental touch protection
/// and low-power dark mode state management.
public enum WatchCaptureProtectionPolicy: Sendable {
    /// Duration required to hold down the stop button before triggering a stop (2.0s).
    public static let holdToStopDuration: TimeInterval = 2.0

    /// Initial delay before auto-dimming into low-power dark mode after recording starts (3.0s).
    public static let initialAutoDimDelay: TimeInterval = 3.0

    /// Inactivity delay before re-entering dark mode after waking up to control panel (5.0s).
    public static let wakeAutoDimDelay: TimeInterval = 5.0

    /// Maximum drag distance from initial touch point before a hold gesture is cancelled (30 pt).
    public static let maxDragCancelDistance: Double = 30.0

    /// High-frequency audio meter update interval in milliseconds (150 ms).
    public static let meterUpdateIntervalMilliseconds: UInt64 = 150

    /// Determines whether the 150ms high-frequency audio meter loop should run.
    /// Suspended whenever luminance is reduced (AOD/wrist down), recording is inactive, or scene is not active.
    public static func shouldMeterUpdate(
        isRecording: Bool,
        isLuminanceReduced: Bool,
        isSceneActive: Bool
    ) -> Bool {
        isRecording && !isLuminanceReduced && isSceneActive
    }

    /// Legacy overload for isDimmed parameter.
    public static func shouldMeterUpdate(
        isRecording: Bool,
        isDimmed: Bool,
        isSceneActive: Bool
    ) -> Bool {
        shouldMeterUpdate(
            isRecording: isRecording,
            isLuminanceReduced: isDimmed,
            isSceneActive: isSceneActive
        )
    }

    /// Computes the normalized hold progress [0.0 ... 1.0] for a given elapsed time.
    public static func progress(forElapsed elapsed: TimeInterval) -> Double {
        guard elapsed > 0 else { return 0.0 }
        return min(1.0, elapsed / holdToStopDuration)
    }

    /// Returns whether the hold gesture has reached full completion threshold.
    public static func canTriggerStop(progress: Double) -> Bool {
        progress >= 1.0
    }

    /// Calculates remaining seconds formatted for countdown display.
    public static func remainingSeconds(forElapsed elapsed: TimeInterval) -> Double {
        max(0.0, holdToStopDuration - elapsed)
    }

    /// Checks if a drag gesture displacement is within the allowable touch tolerance.
    public static func isTouchWithinTolerance(dx: Double, dy: Double) -> Bool {
        hypot(dx, dy) <= maxDragCancelDistance
    }
}
