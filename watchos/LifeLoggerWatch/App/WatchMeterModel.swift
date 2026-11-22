import Foundation

/// Owns the live microphone waveform levels shown by the recording dashboard.
///
/// Levels change every 150 ms while recording. Publishing them from
/// `WatchRecordingCoordinator` invalidated every view observing the coordinator,
/// so this re-evaluated the whole screen tree (NavigationStack + ScrollView +
/// three NavigationLinks + home sync panel + 24 waveform capsules) roughly seven
/// times a second on Series 5 hardware.
///
/// The same full-tree invalidation also fired *at the moment the Digital Crown was
/// pressed*, because leaving the app calls `suspendMeterDisplay()`. A main-thread
/// block right there stalls the scene snapshot watchOS hands to SpringBoard for the
/// crown-press zoom animation, which is what leaves the app grid stuck in a
/// half-collapsed state with touches ignored.
///
/// Keeping the levels in this dedicated model means only the waveform strip redraws.
@MainActor
final class WatchMeterModel: ObservableObject {
    static let levelCount = 24

    @Published private(set) var levels: [Double]

    init(levelCount: Int = WatchMeterModel.levelCount) {
        levels = Array(repeating: 0, count: max(1, levelCount))
    }

    /// Shifts one new (already clamped) sample into the strip.
    func push(_ level: Double) {
        let clamped = level.isFinite ? max(0, min(1, level)) : 0
        var next = levels
        next.removeFirst()
        next.append(clamped)
        levels = next
    }

    /// Flattens the strip to silence. No-ops when already flat so that leaving the
    /// app does not queue an extra redraw during the crown-press transition.
    func suspend() {
        guard levels.contains(where: { $0 != 0 }) else { return }
        levels = Array(repeating: 0, count: levels.count)
    }
}
