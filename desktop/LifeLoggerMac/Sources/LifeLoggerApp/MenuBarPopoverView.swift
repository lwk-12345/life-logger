import AppKit
import LifeLoggerCore
import SwiftUI

public struct MenuBarPopoverView: View {
    @ObservedObject var store: LifeLoggerStore
    @Environment(\.openWindow) private var openWindow

    public init(store: LifeLoggerStore) {
        self.store = store
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Header
            HStack {
                HStack(spacing: 6) {
                    Image(systemName: "waveform.path.ecg")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(Color.accentColor)
                    Text("Life Logger")
                        .font(.system(size: 13, weight: .bold))
                }
                Spacer()
                Text(store.selectedDate)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }

            if let errorMessage = store.errorMessage, !errorMessage.isEmpty {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Text(errorMessage)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(.primary)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(Color.orange.opacity(0.16))
                )
            }

            // Metrics Panel: 消费、热量、步数、睡眠、最后一段录音时间（若服务返回）
            VStack(alignment: .leading, spacing: 8) {
                popoverMetricRow(
                    title: "消费",
                    value: store.totalSpendText,
                    icon: "yensign.circle.fill",
                    color: .orange
                )
                popoverMetricRow(
                    title: "热量",
                    value: store.caloriesText,
                    icon: "flame.circle.fill",
                    color: .green
                )
                popoverMetricRow(
                    title: "步数",
                    value: store.stepsText,
                    icon: "figure.walk.circle.fill",
                    color: .pink
                )
                popoverMetricRow(
                    title: "睡眠",
                    value: store.sleepText,
                    icon: "bed.double.circle.fill",
                    color: .indigo
                )
                if let lastRecording = store.lastRecordingTimeText {
                    popoverMetricRow(
                        title: "最后一段录音时间",
                        value: lastRecording,
                        icon: "waveform.circle.fill",
                        color: .teal
                    )
                }
            }
            .padding(10)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(Color(nsColor: .controlBackgroundColor))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(Color.primary.opacity(0.08), lineWidth: 1)
            )

            HStack {
                Text("更新: \(store.lastUpdatedText)")
                    .font(.system(size: 10, design: .monospaced))
                    .monospacedDigit()
                    .foregroundStyle(.tertiary)
                Spacer()
            }

            Divider()

            // Quick Actions
            HStack(spacing: 8) {
                Button {
                    NSApplication.shared.setActivationPolicy(.regular)
                    openWindow(id: "main")
                    NSApplication.shared.activate(ignoringOtherApps: true)
                } label: {
                    Label("打开主窗口", systemImage: "macwindow")
                        .font(.system(size: 11, weight: .medium))
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)

                Button {
                    NSWorkspace.shared.open(store.webWaterfallURL)
                } label: {
                    Label("在浏览器打开", systemImage: "safari")
                        .font(.system(size: 11))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Button {
                    Task { await store.refresh() }
                } label: {
                    Label("立即刷新", systemImage: "arrow.clockwise")
                        .font(.system(size: 11))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(store.isLoading)

                Spacer(minLength: 2)

                Button {
                    NSApplication.shared.terminate(nil)
                } label: {
                    Text("退出")
                        .font(.system(size: 11))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
        }
        .padding(14)
        .frame(width: 400)
        .onAppear {
            store.isMenuPopoverVisible = true
        }
        .onDisappear {
            store.isMenuPopoverVisible = false
        }
        .task {
            if !store.isLoading {
                await store.refresh()
            }
        }
    }

    private func popoverMetricRow(
        title: String,
        value: String,
        icon: String,
        color: Color
    ) -> some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(color)
                .frame(width: 18, alignment: .center)
            Text(title)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.secondary)
            Spacer(minLength: 8)
            Text(value)
                .font(.system(size: 13, weight: .bold, design: .monospaced))
                .monospacedDigit()
                .foregroundStyle(.primary)
                .lineLimit(1)
        }
    }
}
