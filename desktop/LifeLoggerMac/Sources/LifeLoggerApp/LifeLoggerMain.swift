import AppKit
import LifeLoggerCore
import SwiftUI

@main
struct LifeLoggerMacApp: App {
    @StateObject private var store = LifeLoggerStore()

    var body: some Scene {
        WindowGroup("Life Logger — 个人生活与认知时间线", id: "main") {
            DashboardWindowView(store: store)
                .frame(minWidth: 960, minHeight: 640)
                .onAppear {
                    NSApplication.shared.setActivationPolicy(.regular)
                    NSApplication.shared.activate(ignoringOtherApps: true)
                    store.startAutoRefresh()
                }
        }
        .defaultSize(width: 1240, height: 840)

        MenuBarExtra {
            MenuBarPopoverView(store: store)
        } label: {
            HStack(spacing: 5) {
                Image(systemName: "waveform.path.ecg")
                Text(store.menuBarSummaryText)
                    .font(.system(size: 12, weight: .medium, design: .monospaced))
                    .monospacedDigit()
            }
        }
        .menuBarExtraStyle(.window)
    }
}
