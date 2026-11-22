import AppKit
import LifeLoggerCore
import SwiftUI

public struct DashboardWindowView: View {
    @ObservedObject var store: LifeLoggerStore

    public init(store: LifeLoggerStore) {
        self.store = store
    }

    public var body: some View {
        VStack(spacing: 0) {
            topHeaderBar

            Divider()

            if let errorMessage = store.errorMessage, !errorMessage.isEmpty {
                errorBanner(errorMessage)
            }

            if !store.hasSucceededOnce, let errorMessage = store.errorMessage, !errorMessage.isEmpty {
                VStack(spacing: 12) {
                    Image(systemName: "wifi.exclamationmark")
                        .font(.system(size: 36, weight: .semibold))
                        .foregroundStyle(.orange)
                    Text(errorMessage)
                        .font(.system(size: 18, weight: .bold))
                        .foregroundStyle(.primary)
                    Button("重试连接") {
                        Task { await store.refresh() }
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.regular)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color(nsColor: .windowBackgroundColor))
            } else {
                WebWaterfallView(url: store.webWaterfallURL)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }

            Divider()

            statusFooterBar
        }
        .onAppear {
            store.isMainWindowVisible = true
        }
        .onDisappear {
            store.isMainWindowVisible = false
        }
        .task {
            if store.daily == nil && !store.isLoading {
                await store.refresh()
            }
        }
    }

    // MARK: - Top Header Bar

    private var topHeaderBar: some View {
        HStack(spacing: 12) {
            // App Branding
            HStack(spacing: 8) {
                Image(systemName: "waveform.path.ecg.rectangle.fill")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(Color.accentColor)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Life Logger")
                        .font(.system(size: 14, weight: .bold))
                    Text("个人生活与认知时间线")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
            }

            Divider()
                .frame(height: 22)

            // Date Navigation
            HStack(spacing: 6) {
                Button {
                    Task { await store.shiftDate(byDays: -1) }
                } label: {
                    Text("◀ 前一天")
                        .font(.system(size: 12, weight: .medium))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Text(store.selectedDate)
                    .font(.system(size: 13, weight: .bold, design: .monospaced))
                    .monospacedDigit()
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .fill(Color.primary.opacity(0.06))
                    )

                Button {
                    Task { await store.shiftDate(byDays: 1) }
                } label: {
                    Text("后一天 ▶")
                        .font(.system(size: 12, weight: .medium))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Button {
                    Task { await store.jumpToToday() }
                } label: {
                    Text("今天")
                        .font(.system(size: 12, weight: .semibold))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }

            Spacer(minLength: 12)

            // Open in Browser
            Button {
                NSWorkspace.shared.open(store.webWaterfallURL)
            } label: {
                Label("在浏览器打开", systemImage: "safari")
                    .font(.system(size: 12))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)

            // Refresh Button
            Button {
                Task { await store.refresh() }
            } label: {
                HStack(spacing: 4) {
                    if store.isLoading {
                        ProgressView()
                            .controlSize(.mini)
                    } else {
                        Image(systemName: "arrow.clockwise")
                    }
                    Text("刷新")
                        .font(.system(size: 12, weight: .medium))
                }
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.small)
            .disabled(store.isLoading)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    // MARK: - Error Banner

    private func errorBanner(_ message: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            Text(message)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.primary)
                .lineLimit(2)
            Spacer()
            Button("重试") {
                Task { await store.refresh() }
            }
            .buttonStyle(.bordered)
            .controlSize(.mini)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(Color.orange.opacity(0.12))
    }

    // MARK: - Status Footer

    private var statusFooterBar: some View {
        HStack(spacing: 14) {
            HStack(spacing: 5) {
                Circle()
                    .fill(store.errorMessage == nil ? Color.green : Color.orange)
                    .frame(width: 7, height: 7)
                Text("更新状态: \(store.lastUpdatedText)")
                    .font(.system(size: 11, design: .monospaced))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }

            if let through = store.timeline?.coverage?.through, !through.isEmpty {
                Text("覆盖至: \(through)")
                    .font(.system(size: 11, design: .monospaced))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }

            if let sourceCount = store.timeline?.coverage?.sourceCount {
                Text("音源分片: \(sourceCount)")
                    .font(.system(size: 11, design: .monospaced))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Text(store.baseURL.absoluteString)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.tertiary)

            if let commit = store.versionInfo?.gitCommit, !commit.isEmpty {
                Text("版本: \(String(commit.prefix(8)))")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            } else if let version = store.versionInfo?.appVersion, !version.isEmpty {
                Text("v\(version)")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
        .background(Color(nsColor: .controlBackgroundColor))
    }
}
