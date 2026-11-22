import SwiftUI

@main
struct LifeLoggerWatchApp: App {
    @StateObject private var recorder = WatchRecordingCoordinator()

    var body: some Scene {
        WindowGroup {
            RecorderHomeView(recorder: recorder)
                .onOpenURL { url in
                    guard url.scheme == "lifelogger" else { return }
                    let target = ((url.host ?? "") + url.path).lowercased()
                    if target.contains("pinch") || target.contains("shutter") {
                        recorder.triggerDoublePinchShutter()
                    } else if target.contains("clench") || target.contains("capsule") {
                        recorder.triggerDoubleClenchCapsule()
                    }
                }
        }
        .backgroundTask(
            .appRefresh(WatchRecordingCoordinator.interruptionRefreshTaskIdentifier)
        ) {
            await recorder.handleInterruptionBackgroundRefresh()
        }
    }
}
