import Foundation

public enum WatchGesturePinType: String, Equatable, Sendable, Codable {
    case doublePinch = "double_pinch"
    case doubleClench = "double_clench"
}

public struct WatchGesturePinPolicy: Sendable {
    public static let doublePinchCommandWindowSeconds: TimeInterval = 60.0
    public static let doubleClenchMilestone1MinuteSeconds: TimeInterval = 60.0
    public static let doubleClenchMilestone5MinuteSeconds: TimeInterval = 300.0
    public static let doubleClenchMilestone15MinuteSeconds: TimeInterval = 900.0
    public static let doubleClenchMilestone30MinuteSeconds: TimeInterval = 1800.0

    public static func isWithinPinchWindow(pinTimestamp: Date, evaluationTime: Date = Date()) -> Bool {
        let elapsed = evaluationTime.timeIntervalSince(pinTimestamp)
        return elapsed >= 0 && elapsed <= doublePinchCommandWindowSeconds
    }

    public enum MilestoneReminder: Equatable, Sendable {
        case none
        case oneMinuteLightTap
        case fiveMinuteMilestone
        case fifteenMinuteMilestone
        case thirtyMinuteMilestone
    }

    public static func milestoneForElapsedSeconds(_ elapsed: TimeInterval) -> MilestoneReminder {
        if elapsed >= doubleClenchMilestone30MinuteSeconds - 1 && elapsed <= doubleClenchMilestone30MinuteSeconds + 2 {
            return .thirtyMinuteMilestone
        }
        if elapsed >= doubleClenchMilestone15MinuteSeconds - 1 && elapsed <= doubleClenchMilestone15MinuteSeconds + 2 {
            return .fifteenMinuteMilestone
        }
        if elapsed >= doubleClenchMilestone5MinuteSeconds - 1 && elapsed <= doubleClenchMilestone5MinuteSeconds + 2 {
            return .fiveMinuteMilestone
        }
        if elapsed >= doubleClenchMilestone1MinuteSeconds - 1 && elapsed <= doubleClenchMilestone1MinuteSeconds + 2 {
            return .oneMinuteLightTap
        }
        return .none
    }
}
