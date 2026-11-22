import SwiftUI
import WatchKit

struct RecorderHomeView: View {
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.isLuminanceReduced) private var isLuminanceReduced
    @AppStorage("isWaveformEcoMode") private var isWaveformEcoMode = false
    @ObservedObject var recorder: WatchRecordingCoordinator
    private var sync: WatchSyncCoordinator { recorder.syncCoordinator }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 12) {
                    headerView

                    if recorder.state == .recording {
                        recordingDashboard
                    } else if recorder.state == .stopping {
                        inactiveSessionDashboard
                    } else {
                        idleDashboard
                    }

                    if !recorder.statusMessage.isEmpty,
                       recorder.state == .failed || recorder.state == .stopping {
                        Text(recorder.statusMessage)
                            .font(.system(size: 11))
                            .foregroundStyle(recorder.state == .failed ? .orange : .secondary)
                            .multilineTextAlignment(.center)
                            .padding(.horizontal, 6)
                    }

                    homeSyncPanel

                    navigationSection

                    footerView
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
            }
            .background(Color.black)
        }
        .task(id: "\(scenePhase)-\(recorder.isRecording)-\(isLuminanceReduced)-\(isWaveformEcoMode)") {
            guard !isWaveformEcoMode, WatchCaptureProtectionPolicy.shouldMeterUpdate(
                isRecording: recorder.isRecording,
                isLuminanceReduced: isLuminanceReduced,
                isSceneActive: scenePhase == .active
            ) else {
                recorder.suspendMeterDisplay()
                return
            }
            while !Task.isCancelled {
                recorder.updateMeter()
                do {
                    try await Task.sleep(for: .milliseconds(WatchCaptureProtectionPolicy.meterUpdateIntervalMilliseconds))
                } catch {
                    return
                }
            }
        }
        .task {
            await recorder.refreshPendingQueue()
            await sync.syncPending()
            await recorder.refreshPendingQueue()
        }
        .onChange(of: recorder.syncRevision) { _, _ in
            Task {
                await recorder.refreshPendingQueue()
                // While a recording is open, a health probe plus an upload once a minute
                // competed with the segment rotation on a Series 5 (and the comment further down
                // already promised not to sync while recording). Audio is durable on the Watch,
                // so batch uploads instead; everything goes out as soon as recording stops.
                if WatchCaptureRecoveryPolicy.shouldSync(
                    hasOpenCaptureSession: recorder.hasActiveRecordingSession,
                    pendingSegments: recorder.pendingSegmentCount
                ) {
                    await sync.syncPending()
                    await recorder.refreshPendingQueue()
                }
            }
        }
        .onChange(of: scenePhase) { _, phase in
            WatchCaptureEventLog.shared.record("scene -> \(String(describing: phase))")
            guard phase == .active else {
                recorder.suspendMeterDisplay()
                return
            }
            WatchLocationSnapshotManager.shared.startMonitoringIfPermitted()
            recorder.reconcileAfterForeground()
            // Defer heavy disk I/O + network sync to let the UI render first.
            // On Series 5 (S5 chip), doing this synchronously blocks MainActor
            // for hundreds of milliseconds, causing the orange "inactive" flash.
            Task {
                try? await Task.sleep(for: .milliseconds(300))
                await recorder.refreshPendingQueue()
                // Skip network sync while recording — it's wasteful and slows the UI.
                guard !recorder.hasActiveRecordingSession else { return }
                await sync.syncPending()
                await recorder.refreshPendingQueue()
            }
        }
    }

    private var headerView: some View {
        HStack(alignment: .center, spacing: 6) {
            Text("Life Logger")
                .font(.system(size: 14, weight: .bold, design: .rounded))
                .foregroundStyle(.white)

            Spacer(minLength: 4)

            Text(macConnectionIndicatorTitle)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)

            ZStack {
                Circle()
                    .fill(macConnectionIndicatorColor.opacity(0.18))
                    .frame(width: 14, height: 14)
                Circle()
                    .fill(macConnectionIndicatorColor)
                    .frame(width: 7, height: 7)
                    .shadow(color: macConnectionIndicatorColor.opacity(0.85), radius: sync.macConnectionIndicator == .checking ? 4 : 2)
            }
            .padding(.trailing, 2)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Life Logger，Mac \(macConnectionIndicatorTitle)")
        .accessibilityHint("显示最近一次 Mac 连接检查结果。录音中支持边录边传。")
    }

    private var macConnectionIndicatorTitle: String {
        switch sync.macConnectionIndicator {
        case .unchecked:
            return "未连接"
        case .checking:
            return "连接中"
        case .reachable:
            return "Mac 在线"
        case .unreachable:
            return "Mac 离线"
        case .attention:
            return "连接异常"
        case .notConfigured:
            return "未设置"
        }
    }

    private var macConnectionIndicatorColor: Color {
        switch sync.macConnectionIndicator {
        case .unchecked, .notConfigured:
            return .gray
        case .checking, .attention:
            return .orange
        case .reachable:
            return Color(red: 0.2, green: 0.85, blue: 0.4)
        case .unreachable:
            return Color(red: 0.95, green: 0.25, blue: 0.25)
        }
    }

    private var idleDashboard: some View {
        VStack(spacing: 12) {
            VStack(spacing: 3) {
                Text("随时记录")
                    .font(.system(.title3, design: .rounded, weight: .bold))
                    .foregroundStyle(.white)
                Text("边录边传到 Mac")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            .padding(.top, 2)

            Button {
                WKInterfaceDevice.current().play(.start)
                recorder.start()
            } label: {
                HStack(spacing: 7) {
                    Image(systemName: "mic.fill")
                        .font(.system(size: 17, weight: .bold))
                    Text(recorder.state == .failed ? "重新开始录音" : "开始录音")
                        .font(.system(.headline, design: .rounded, weight: .bold))
                }
                .frame(maxWidth: .infinity)
                .frame(height: 50)
                .foregroundStyle(.black)
                .background(
                    LinearGradient(
                        colors: [
                            Color(red: 0.35, green: 0.95, blue: 0.5),
                            Color(red: 0.15, green: 0.82, blue: 0.38)
                        ],
                        startPoint: .top,
                        endPoint: .bottom
                    ),
                    in: RoundedRectangle(cornerRadius: 25, style: .continuous)
                )
                .contentShape(RoundedRectangle(cornerRadius: 25, style: .continuous))
                .shadow(color: Color.green.opacity(0.35), radius: 8, y: 2)
            }
            .buttonStyle(PlainScaleButtonStyle())
            .disabled(recorder.isBusy)
        }
        .padding(.vertical, 2)
    }

    private var recordingDashboard: some View {
        VStack(spacing: 8) {
            TimelineView(.periodic(from: .now, by: 1)) { timeline in
                Text(recorder.elapsedText(at: timeline.date))
                    .font(.system(size: 34, weight: .bold, design: .rounded).monospacedDigit())
                    .foregroundStyle(.white)
            }

            TimelineView(.periodic(from: .now, by: 5)) { _ in
                VStack(spacing: 8) {
                    HStack(spacing: 5) {
                        PulsingRecordBeacon(isWriting: recorder.isCaptureWriting, animated: !isLuminanceReduced)
                        Text(captureStatusLine)
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.8)
                    }

                    if let notice = recorder.captureNotice {
                        Text(notice)
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(.orange)
                            .multilineTextAlignment(.center)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 5)
                            .frame(maxWidth: .infinity)
                            .background(Color.orange.opacity(0.15), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                            .accessibilityAddTraits(.isStaticText)
                    }
                }
            }

            WaveformStripView(
                meter: recorder.meter,
                isLuminanceReduced: isLuminanceReduced,
                isEcoMode: isWaveformEcoMode
            )
            .padding(.horizontal, 4)

            if let notice = recorder.lastPinNotice {
                Text(notice)
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(.green)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Color.green.opacity(0.18), in: Capsule())
            }

            HStack(spacing: 6) {
                captureActionButton(
                    title: "捏捏快门",
                    icon: "bolt.fill",
                    color: .yellow
                ) {
                    recorder.triggerDoublePinchShutter()
                }

                captureActionButton(
                    title: recorder.isCapsuleActive ? "胶囊进行中" : "会话胶囊",
                    icon: recorder.isCapsuleActive ? "record.circle.fill" : "circle",
                    color: recorder.isCapsuleActive ? .red : .blue
                ) {
                    recorder.triggerDoubleClenchCapsule()
                }
            }

            HoldToStopButton(
                isBusy: recorder.isBusy,
                onHoldCompleted: {
                    recorder.stop()
                }
            )
            .padding(.top, 4)

            Button {
                WKInterfaceDevice.current().play(.click)
                isWaveformEcoMode.toggle()
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: isWaveformEcoMode ? "leaf.fill" : "waveform")
                        .font(.system(size: 11))
                    Text(isWaveformEcoMode ? "省电模式：开" : "省电模式：关")
                        .font(.system(size: 12, weight: .medium))
                }
                .foregroundStyle(isWaveformEcoMode ? Color.green : Color.secondary)
                .frame(maxWidth: .infinity)
                .frame(height: 34)
                .background(
                    isWaveformEcoMode ? Color.green.opacity(0.15) : Color.white.opacity(0.08),
                    in: Capsule()
                )
                .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityHint("关闭波形动画以省电，录音不受影响")

            Text("轻按表冠回表盘，录音继续")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
    }

    private var captureStatusLine: String {
        guard recorder.isCaptureWriting else { return "录音未写入" }
        if recorder.liveSyncedSegmentCount > 0 {
            return "正在录音 · 已传 \(recorder.liveSyncedSegmentCount) 段"
        }
        return "正在录音 · 已存 \(max(0, recorder.nextSegmentNumber - 1)) 段"
    }

    private func captureActionButton(
        title: String,
        icon: String,
        color: Color,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 3) {
                Image(systemName: icon)
                    .font(.system(size: 11))
                Text(title)
                    .font(.system(size: 12, weight: .bold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
            }
            .frame(maxWidth: .infinity)
            .frame(height: 38)
            .background(color.opacity(0.18), in: Capsule())
            .foregroundStyle(color)
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }

    private var inactiveSessionDashboard: some View {
        VStack(spacing: 8) {
            Text(recorder.state == .stopping ? "正在保存已录音频…" : "录音当前未运行")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(recorder.state == .stopping ? .orange : .secondary)

            TimelineView(.periodic(from: .now, by: 1)) { timeline in
                Text(recorder.elapsedText(at: timeline.date))
                    .font(.system(size: 32, weight: .semibold, design: .rounded).monospacedDigit())
                    .foregroundStyle(.white)
            }

            if recorder.hasActiveRecordingSession {
                HoldToStopButton(
                    isBusy: recorder.isBusy,
                    onHoldCompleted: {
                        recorder.stop()
                    }
                )
                .padding(.top, 4)
            }
        }
    }

    private var homeSyncPanel: some View {
        HomeSyncPanel(recorder: recorder, sync: sync)
    }

    private var navigationSection: some View {
        VStack(spacing: 6) {
            NavigationLink {
                SessionsListView(recorder: recorder)
            } label: {
                NavigationRowCard(
                    icon: "clock.fill",
                    iconColor: .blue,
                    title: "录音记录",
                    badge: recorder.sessions.isEmpty ? nil : "\(recorder.sessions.count) 次"
                )
            }

            NavigationLink {
                UploadQueueView(recorder: recorder, sync: sync)
            } label: {
                NavigationRowCard(
                    icon: "tray.full.fill",
                    iconColor: .indigo,
                    title: "录音分段",
                    badge: recorder.pendingSegmentCount > 0 ? "\(recorder.pendingSegmentCount) 待传" : nil,
                    badgeColor: .orange
                )
            }

            if recorder.quarantineStatusLine != nil {
                NavigationLink {
                    QuarantineView(recorder: recorder)
                } label: {
                    NavigationRowCard(
                        icon: "exclamationmark.triangle.fill",
                        iconColor: .orange,
                        title: "损坏文件清理",
                        badge: "\(recorder.quarantinedFiles.count)",
                        badgeColor: .orange
                    )
                }
            }

            NavigationLink {
                CaptureEventsView()
            } label: {
                NavigationRowCard(
                    icon: "list.bullet.rectangle.fill",
                    iconColor: .purple,
                    title: "录音事件日志"
                )
            }

            NavigationLink {
                MacSyncSettingsView()
            } label: {
                NavigationRowCard(
                    icon: "network",
                    iconColor: .teal,
                    title: "Mac 连接"
                )
            }
        }
        .buttonStyle(PlainScaleButtonStyle())
    }

    private var footerView: some View {
        Text("Life Logger v\(versionCode)")
            .font(.system(size: 11, weight: .medium, design: .monospaced))
            .foregroundStyle(.tertiary)
            .padding(.top, 4)
            .padding(.bottom, 6)
            .accessibilityLabel("版本 \(versionCode)")
    }

    private var versionCode: String {
        let buildVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "42"
        guard let buildNumber = Int(buildVersion) else { return buildVersion }
        return String(format: "%02d", buildNumber)
    }
}

/// A standard card-styled navigation row for secondary actions.
private let shanghaiTimeZone = TimeZone(identifier: "Asia/Shanghai") ?? .current

/// 手表上的时间统一显示 UTC+8，和网页一致。
private func shanghaiClock(_ date: Date) -> String {
    let formatter = DateFormatter()
    formatter.timeZone = shanghaiTimeZone
    formatter.dateFormat = "HH:mm:ss"
    return formatter.string(from: date)
}

private func shanghaiDateTime(_ date: Date) -> String {
    let formatter = DateFormatter()
    formatter.timeZone = shanghaiTimeZone
    formatter.dateFormat = "MM/dd HH:mm"
    return formatter.string(from: date)
}

private struct NavigationRowCard: View {
    let icon: String
    let iconColor: Color
    let title: String
    var badge: String? = nil
    var badgeColor: Color = .secondary

    var body: some View {
        HStack(spacing: 8) {
            ZStack {
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(iconColor.opacity(0.2))
                    .frame(width: 24, height: 24)
                Image(systemName: icon)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(iconColor)
            }

            Text(title)
                .font(.system(size: 12.5, weight: .medium))
                .foregroundStyle(.primary)
                .lineLimit(1)

            Spacer(minLength: 4)

            if let badge {
                Text(badge)
                    .font(.system(size: 11, weight: .bold, design: .rounded))
                    .foregroundStyle(badgeColor)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(badgeColor.opacity(0.15), in: Capsule())
            }

            Image(systemName: "chevron.right")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity)
        .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

/// Full screen view for browsing recorded sessions.
private struct SessionsListView: View {
    @ObservedObject var recorder: WatchRecordingCoordinator

    var body: some View {
        List {
            if recorder.sessions.isEmpty {
                Text("暂无录音记录")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(recorder.sessions, id: \.id) { session in
                    NavigationLink {
                        List(session.segments) { segment in
                            VStack(alignment: .leading, spacing: 3) {
                                Text(formatSegmentTimeRange(sessionStartedAt: session.startedAt, segment: segment))
                                    .font(.caption.weight(.medium))
                                Text(segmentStatusText(segment.uploadState))
                                    .font(.caption2)
                                    .foregroundStyle(segmentStatusColor(segment.uploadState))
                            }
                        }
                        .navigationTitle("录音记录")
                    } label: {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(shanghaiDateTime(session.startedAt))
                                .font(.caption.weight(.medium))
                            Text("共 \(sessionDurationMinutes(session)) 分钟 · \(session.macJobID != nil ? "Mac 已接收" : session.stoppedAt == nil ? "录音中" : "手表已保存，待上传")")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .navigationTitle("录音记录")
    }
}

/// The on-Watch view of `WatchCaptureEventLog`: the answer to "why did it stop?" without a Mac.
private struct CaptureEventsView: View {
    @State private var lines: [String] = []
    @State private var confirmClear = false

    var body: some View {
        List {
            if lines.isEmpty {
                Text("暂无记录").font(.caption2).foregroundStyle(.secondary)
            }
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                Text(line)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(
                        line.contains("FAILED") || line.contains("gave up") || line.contains("BEGAN")
                            ? Color.orange : Color.secondary
                    )
            }
            if !lines.isEmpty {
                Button("清空记录", role: .destructive) { confirmClear = true }
                    .font(.caption2)
            }
        }
        .navigationTitle("录音事件")
        .onAppear { lines = WatchCaptureEventLog.shared.recentLines() }
        .confirmationDialog("清空录音事件记录？", isPresented: $confirmClear) {
            Button("清空", role: .destructive) {
                WatchCaptureEventLog.shared.clear()
                lines = []
            }
        }
    }
}

/// A pulsing indicator providing real-time visual heartbeat confidence that recording hardware is active.
private struct PulsingRecordBeacon: View {
    var isWriting = true
    var animated = true
    @State private var isPulsing = false

    private var color: Color { isWriting ? .red : .orange }

    var body: some View {
        ZStack {
            Circle()
                .fill(color.opacity(0.35))
                .frame(width: 12, height: 12)
                .scaleEffect(isPulsing && animated ? 1.35 : 0.8)
                .opacity(isPulsing && animated ? 0.3 : 0.85)
            Circle()
                .fill(color)
                .frame(width: 6, height: 6)
        }
        .accessibilityHidden(true)
        .onAppear {
            withAnimation(.easeInOut(duration: 1.0).repeatForever(autoreverses: true)) {
                isPulsing = true
            }
        }
    }
}

/// Isolates the 150 ms waveform redraw from the rest of the screen.
private struct WaveformStripView: View {
    @ObservedObject var meter: WatchMeterModel
    let isLuminanceReduced: Bool
    var isEcoMode: Bool = false

    var body: some View {
        HStack(alignment: .center, spacing: 2.5) {
            if isEcoMode {
                HStack(spacing: 4) {
                    Image(systemName: "leaf.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(Color.green)
                    Text("省电中 · 录音继续")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Color.green.opacity(0.85))
                }
                .frame(maxWidth: .infinity, alignment: .center)
            } else {
                ForEach(Array(meter.levels.enumerated()), id: \.offset) { _, level in
                    Capsule()
                        .fill(
                            isLuminanceReduced
                                ? Color.green.opacity(0.3)
                                : Color(red: 0.2, green: 0.88, blue: 0.45)
                        )
                        .frame(maxWidth: .infinity)
                        .frame(height: max(3.5, 30 * (isLuminanceReduced ? 0.08 : level)))
                }
            }
        }
        .frame(height: 32)
        .accessibilityLabel(isEcoMode ? "麦克风录音省电模式" : "麦克风音量")
    }
}

/// A dedicated button that requires holding down for 2.0s to stop recording,
/// preventing accidental taps while providing tactile and visual ring feedback.
private struct HoldToStopButton: View {
    let isBusy: Bool
    let onHoldCompleted: () -> Void
    var onHoldStateChanged: ((Bool) -> Void)?

    @State private var isPressing = false
    @State private var progress: Double = 0.0
    @State private var holdTask: Task<Void, Never>?
    @State private var hasTriggered = false
    @State private var touchStart: CGPoint?

    private let buttonSize: CGFloat = 66

    var body: some View {
        VStack(spacing: 6) {
            ZStack {
                // Outer subtle track
                Circle()
                    .stroke(Color.red.opacity(0.2), lineWidth: 4.5)
                    .frame(width: buttonSize, height: buttonSize)

                // Filling progress ring
                Circle()
                    .trim(from: 0, to: progress)
                    .stroke(
                        Color.red,
                        style: StrokeStyle(lineWidth: 4.5, lineCap: .round)
                    )
                    .rotationEffect(.degrees(-90))
                    .frame(width: buttonSize, height: buttonSize)

                // Center tactile button surface
                Circle()
                    .fill(isPressing ? Color.red.opacity(0.35) : Color.red.opacity(0.18))
                    .frame(width: buttonSize - 12, height: buttonSize - 12)
                    .scaleEffect(isPressing ? 0.94 : 1.0)

                if isBusy {
                    ProgressView()
                        .tint(.red)
                } else {
                    RoundedRectangle(cornerRadius: 3.5, style: .continuous)
                        .fill(Color.red)
                        .frame(width: 18, height: 18)
                        .scaleEffect(isPressing ? 0.92 : 1.0)
                }
            }
            .animation(.spring(response: 0.25, dampingFraction: 0.75), value: isPressing)

            Text(
                isBusy
                    ? "正在保存…"
                    : isPressing
                        ? "保持按住 \(String(format: "%.1f", WatchCaptureProtectionPolicy.remainingSeconds(forElapsed: progress * WatchCaptureProtectionPolicy.holdToStopDuration)))s"
                        : "长按 2 秒停止"
            )
            .font(.system(size: 11.5, weight: .semibold, design: .rounded))
            .foregroundStyle(isPressing ? .red : .secondary)
            .monospacedDigit()
        }
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel(isBusy ? "正在保存录音" : "停止录音")
        .accessibilityHint("长按 2 秒防误触停止并保存录音")
        .accessibilityAction {
            guard !isBusy else { return }
            WKInterfaceDevice.current().play(.stop)
            onHoldCompleted()
        }
        .gesture(
            DragGesture(minimumDistance: 0)
                .onChanged { value in
                    guard !isBusy, !hasTriggered else { return }
                    if !isPressing {
                        touchStart = value.startLocation
                        startHold()
                    } else {
                        let start = touchStart ?? value.startLocation
                        let dx = Double(value.location.x - start.x)
                        let dy = Double(value.location.y - start.y)
                        if !WatchCaptureProtectionPolicy.isTouchWithinTolerance(dx: dx, dy: dy) {
                            cancelHold()
                        }
                    }
                }
                .onEnded { _ in
                    touchStart = nil
                    if hasTriggered {
                        hasTriggered = false
                    } else {
                        cancelHold()
                    }
                }
        )
    }

    private func startHold() {
        guard !hasTriggered else { return }
        isPressing = true
        progress = 0.0
        onHoldStateChanged?(true)
        WKInterfaceDevice.current().play(.click)

        let startTime = Date()

        holdTask?.cancel()
        holdTask = Task { @MainActor in
            var didPlayHalfway = false
            while !Task.isCancelled {
                let elapsed = Date().timeIntervalSince(startTime)
                let p = WatchCaptureProtectionPolicy.progress(forElapsed: elapsed)
                progress = p

                if !didPlayHalfway && p >= 0.5 {
                    didPlayHalfway = true
                    WKInterfaceDevice.current().play(.click)
                }

                if WatchCaptureProtectionPolicy.canTriggerStop(progress: p) {
                    hasTriggered = true
                    holdTask = nil
                    isPressing = false
                    progress = 1.0
                    onHoldStateChanged?(false)
                    WKInterfaceDevice.current().play(.stop)
                    onHoldCompleted()
                    break
                }

                do {
                    try await Task.sleep(nanoseconds: 30_000_000)
                } catch {
                    return
                }
            }
        }
    }

    private func cancelHold() {
        guard !hasTriggered else { return }
        holdTask?.cancel()
        holdTask = nil
        isPressing = false
        onHoldStateChanged?(false)
        withAnimation(.spring(response: 0.25, dampingFraction: 0.8)) {
            progress = 0.0
        }
    }
}

private struct HomeSyncPanel: View {
    @ObservedObject var recorder: WatchRecordingCoordinator
    @ObservedObject var sync: WatchSyncCoordinator
    @State private var isMessageExpanded = false
    @State private var showingSyncDetails = false

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .center, spacing: 6) {
                HStack(spacing: 5) {
                    Image(systemName: "arrow.triangle.2.circlepath")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(.blue)
                    Text("Mac 同步")
                        .font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                }

                Spacer(minLength: 4)

                Button {
                    showingSyncDetails = true
                } label: {
                    Text("\(recorder.pendingSegmentCount) 待传")
                        .font(.system(size: 11, weight: .bold, design: .rounded))
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                    .foregroundStyle(recorder.pendingSegmentCount == 0 ? Color.green : Color.orange)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2.5)
                    .background(
                        (recorder.pendingSegmentCount == 0 ? Color.green : Color.orange).opacity(0.14),
                        in: Capsule()
                    )
                }
                .buttonStyle(.plain)
            }

            if let captureStatus = WatchSyncStatusPolicy.captureStatus(
                isRecoveringStagedAudio: recorder.isRecoveringStagedAudio,
                hasUnrecoveredStagedAudio: recorder.hasUnrecoveredStagedAudio,
                isRecording: recorder.isRecording,
                hasActiveRecordingSession: recorder.hasActiveRecordingSession
            ) {
                Text(captureStatus)
                    .font(.system(size: 11))
                    .foregroundStyle(recorder.hasUnrecoveredStagedAudio ? .orange : .secondary)
                    .lineLimit(nil)
            }

            if let progress = sync.syncProgress, progress.total > 0 {
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text("上传中")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text("\(progress.completed) / \(progress.total) 段 · \(Int(progress.fraction * 100))%")
                            .font(.system(size: 11, weight: .bold, design: .rounded).monospacedDigit())
                            .foregroundStyle(.blue)
                    }
                    ProgressView(value: progress.fraction)
                        .progressViewStyle(.linear)
                        .tint(Color.blue)
                }
                .padding(.vertical, 1)
            }

            if !sync.statusMessage.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    Text(sync.statusMessage)
                        .font(.system(size: 11))
                        .foregroundStyle(syncStatusColor)
                        .lineLimit(isMessageExpanded ? nil : 3)
                        .fixedSize(horizontal: false, vertical: true)

                    if sync.statusMessage.count > 25 {
                        HStack(spacing: 2) {
                            Text(isMessageExpanded ? "收起" : "轻按展开全文")
                                .font(.system(size: 11, weight: .medium))
                                .foregroundStyle(.blue)
                            Image(systemName: isMessageExpanded ? "chevron.up" : "chevron.down")
                                .font(.system(size: 11, weight: .bold))
                                .foregroundStyle(.blue)
                        }
                        .padding(.top, 1)
                    }
                }
                .contentShape(Rectangle())
                .onTapGesture {
                    withAnimation(.spring(response: 0.25, dampingFraction: 0.8)) {
                        isMessageExpanded.toggle()
                    }
                }
            }

            if let savedAt = recorder.lastSavedAt {
                Text("最近保存 \(shanghaiClock(savedAt))")
                    .font(.system(size: 11))
                    .foregroundStyle(Color.green.opacity(0.85))
            }

            Button(action: syncNow) {
                HStack(spacing: 6) {
                    if showsSyncProgress {
                        ProgressView()
                            .controlSize(.mini)
                            .tint(.white)
                    } else {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.system(size: 13, weight: .semibold))
                    }
                    Text(syncButtonTitle)
                        .font(.system(size: 12, weight: .semibold, design: .rounded))
                        .lineLimit(1)
                        .minimumScaleFactor(0.75)
                }
                .padding(.horizontal, 6)
                .frame(maxWidth: .infinity)
                .frame(height: 40)
                .foregroundStyle(.white)
                .background(
                    recorder.pendingSegmentCount > 0 && !showsSyncProgress
                        ? Color.blue.opacity(0.85)
                        : Color.white.opacity(0.12),
                    in: RoundedRectangle(cornerRadius: 10, style: .continuous)
                )
            }
            .buttonStyle(PlainScaleButtonStyle())
            .disabled(recorder.isRecoveringStagedAudio || (!recorder.isRecording && sync.isSyncing))
            .accessibilityHint("录音收到 Mac 的接收回执后，才会从待上传列表移除")
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .sheet(isPresented: $showingSyncDetails) {
            SyncDetailSheet(recorder: recorder, sync: sync)
        }
    }

    private func syncNow() {
        Task { @MainActor in
            await sync.syncPending(recordingSessionID: recorder.currentSessionID)
            await recorder.refreshPendingQueue()
        }
    }

    private var showsSyncProgress: Bool {
        sync.isSyncing || recorder.isRecoveringStagedAudio
    }

    private var syncButtonTitle: String {
        if recorder.isRecoveringStagedAudio { return "整理录音…" }
        if let progress = sync.syncProgress, progress.total > 0 {
            return "正在上传 (\(progress.completed)/\(progress.total))…"
        }
        if sync.isSyncing {
            return sync.statusMessage.contains("检查 Mac") ? "连接 Mac…" : "正在上传…"
        }
        if recorder.isRecording {
            return recorder.pendingSegmentCount > 0 ? "上传已录分段 (\(recorder.pendingSegmentCount))" : "边录边传正常"
        }
        return recorder.pendingSegmentCount > 0 ? "上传到 Mac" : "立即检查同步"
    }

    private var syncStatusColor: Color {
        let message = sync.statusMessage
        if ["失败", "不可达", "无效", "拒绝", "未完成", "无法上传", "错误", "未能连上", "未连接"].contains(where: message.contains) {
            return .orange
        }
        if sync.isSyncing || message.contains("暂无待 Mac") || message.contains("边录边传中") { return .green }
        return .secondary
    }
}

/// A comprehensive modal sheet displaying detailed, unabridged sync and connection diagnostic status.
private struct SyncDetailSheet: View {
    @ObservedObject var recorder: WatchRecordingCoordinator
    @ObservedObject var sync: WatchSyncCoordinator
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 9) {
                HStack {
                    Text("同步状态详情")
                        .font(.system(size: 13, weight: .bold, design: .rounded))
                    Spacer()
                    Button("完成") {
                        dismiss()
                    }
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.blue)
                }
                .padding(.top, 2)

                VStack(alignment: .leading, spacing: 4) {
                    Text("当前状态")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                    Text(sync.statusMessage.isEmpty ? "无状态信息" : sync.statusMessage)
                        .font(.system(size: 11))
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 10, style: .continuous))

                VStack(spacing: 5) {
                    HStack {
                        Text("待传队列")
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text("\(recorder.pendingSegmentCount) 段")
                            .fontWeight(.bold)
                    }
                    if let progress = sync.syncProgress, progress.total > 0 {
                        HStack {
                            Text("传输进度")
                                .foregroundStyle(.secondary)
                            Spacer()
                            Text("\(progress.completed)/\(progress.total) 段 (\(Int(progress.fraction * 100))%)")
                                .fontWeight(.bold)
                                .foregroundStyle(.blue)
                        }
                    }
                    if let savedAt = recorder.lastSavedAt {
                        HStack {
                            Text("最近保存")
                                .foregroundStyle(.secondary)
                            Spacer()
                            Text(shanghaiClock(savedAt))
                        }
                    }
                    if let lastJobID = sync.lastMacJobID {
                        HStack {
                            Text("最近任务")
                                .foregroundStyle(.secondary)
                            Spacer()
                            Text(String(lastJobID.suffix(8)))
                                .font(.system(size: 11, design: .monospaced))
                        }
                    }
                }
                .font(.system(size: 11))
                .padding(8)
                .frame(maxWidth: .infinity)
                .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 10, style: .continuous))

                Button {
                    dismiss()
                    Task { @MainActor in
                        await sync.syncPending()
                        await recorder.refreshPendingQueue()
                    }
                } label: {
                    Text("立即重试同步")
                        .font(.system(size: 11, weight: .semibold))
                        .frame(maxWidth: .infinity)
                }
                .tint(.blue)
            }
            .padding(.horizontal, 4)
            .padding(.vertical, 4)
        }
    }
}

private struct UploadQueueView: View {
    @ObservedObject var recorder: WatchRecordingCoordinator
    @ObservedObject var sync: WatchSyncCoordinator

    private func sessionDateTitle(_ date: Date) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = shanghaiTimeZone
        let formatter = DateFormatter()
        formatter.timeZone = shanghaiTimeZone
        if calendar.isDateInToday(date) {
            formatter.dateFormat = "今天 HH:mm"
        } else if calendar.isDateInYesterday(date) {
            formatter.dateFormat = "昨天 HH:mm"
        } else {
            formatter.dateFormat = "MM/dd HH:mm"
        }
        return formatter.string(from: date)
    }

    private func sessionHeader(_ session: RecordingSessionManifest) -> String {
        let title = sessionDateTitle(session.startedAt)
        let totalMinutes = sessionDurationMinutes(session)
        if session.stoppedAt == nil {
            return "\(title)（录音中 · \(totalMinutes)分钟）"
        }
        return "\(title)（共 \(totalMinutes) 分钟）"
    }

    var body: some View {
        List {
            let visibleSessions = recorder.sessions.filter { !$0.segments.isEmpty }
            if visibleSessions.isEmpty {
                Section("手表已保存") {
                    Text("还没有录音分段")
                        .foregroundStyle(.secondary)
                }
            } else {
                ForEach(visibleSessions.prefix(6), id: \.id) { session in
                    Section(sessionHeader(session)) {
                        ForEach(session.segments.reversed().prefix(10)) { segment in
                            VStack(alignment: .leading, spacing: 3) {
                                Text(formatSegmentTimeRange(sessionStartedAt: session.startedAt, segment: segment))
                                    .font(.caption.weight(.semibold))
                                Text(segmentStatusText(segment.uploadState))
                                    .font(.caption2)
                                    .foregroundStyle(segmentStatusColor(segment.uploadState))
                            }
                        }
                        if session.segments.count > 10 {
                            Text("其余 \(session.segments.count - 10) 条录音…")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }

            Section("发送到 Mac") {
                Text(sync.statusMessage)
                    .font(.caption2)
                    .foregroundStyle(syncStatusColor)
                Button {
                    Task {
                        await recorder.refreshPendingQueue()
                        await sync.syncPending()
                        await recorder.refreshPendingQueue()
                    }
                } label: {
                    HStack(spacing: 5) {
                        if sync.isSyncing {
                            ProgressView().controlSize(.mini)
                            Text(sync.statusMessage.contains("检查 Mac") ? "连接 Mac…" : "正在上传…")
                        } else {
                            Label("上传到 Mac", systemImage: "arrow.up.circle.fill")
                        }
                    }
                }
                .tint(.green)
                .disabled(sync.isSyncing && !recorder.isRecording)
            }

            Section {
                NavigationLink("Mac 连接") {
                    MacSyncSettingsView()
                }
            }
        }
        .navigationTitle("录音分段")
        .task {
            await recorder.refreshPendingQueue()
            await sync.syncPending()
            await recorder.refreshPendingQueue()
        }
    }

    private var syncStatusColor: Color {
        let message = sync.statusMessage
        if ["失败", "不可达", "无效", "拒绝", "未完成", "无法上传", "错误"].contains(where: message.contains) {
            return .orange
        }
        return sync.isSyncing ? .green : .secondary
    }
}

private func formatSegmentTimeRange(sessionStartedAt: Date, segment: AudioSegment) -> String {
    let start = sessionStartedAt.addingTimeInterval(Double(segment.startOffsetMilliseconds) / 1000.0)
    let end = start.addingTimeInterval(Double(segment.durationMilliseconds) / 1000.0)
    let formatter = DateFormatter()
    formatter.timeZone = shanghaiTimeZone
    formatter.dateFormat = "HH:mm:ss"
    return "\(formatter.string(from: start)) - \(formatter.string(from: end))"
}

private func sessionDurationMinutes(_ session: RecordingSessionManifest) -> Int {
    let totalMs = session.segments.reduce(Int64(0)) { $0 + $1.durationMilliseconds }
    return max(1, Int((Double(totalMs) / 60000.0).rounded()))
}

private func segmentStatusText(_ state: SegmentUploadState) -> String {
    switch state {
    case .pending: "待上传 · 手表已保存"
    case .uploading: "正在传到 Mac"
    case .failed: "上传失败 · 音频仍在手表"
    case .acknowledged: "Mac 已接收"
    }
}

private func segmentStatusColor(_ state: SegmentUploadState) -> Color {
    switch state {
    case .pending: .orange
    case .uploading: .blue
    case .failed: .red
    case .acknowledged: .green
    }
}

/// "坏文件"：看得到、清得掉。
private struct QuarantineView: View {
    @ObservedObject var recorder: WatchRecordingCoordinator
    @State private var isConfirmingDeleteAll = false

    var body: some View {
        List {
            if recorder.quarantinedFiles.isEmpty {
                Section {
                    Text("没有无法读取的录音片段。")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            } else {
                Section {
                    Text("这些片段损坏、读不出音频，Mac 上也没有副本。")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Text("共 \(recorder.quarantinedFiles.count) 个 · \(WatchQuarantinePolicy.formattedSize(recorder.quarantinedBytes))")
                        .font(.footnote.weight(.semibold).monospacedDigit())
                        .foregroundStyle(.orange)
                    if !recorder.quarantineMessage.isEmpty {
                        Text(recorder.quarantineMessage)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }

                Section("片段") {
                    ForEach(recorder.quarantinedFiles) { file in
                        HStack(alignment: .top, spacing: 6) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(title(for: file))
                                    .font(.caption.weight(.semibold))
                                Text(detail(for: file))
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer(minLength: 0)
                            Button(role: .destructive) {
                                recorder.deleteQuarantined(file)
                            } label: {
                                Image(systemName: "trash")
                                    .font(.caption)
                            }
                            .buttonStyle(.borderless)
                            .accessibilityLabel("删除 \(title(for: file))")
                        }
                    }
                }

                Section {
                    Button(role: .destructive) {
                        isConfirmingDeleteAll = true
                    } label: {
                        Label(
                            "全部清理 · \(WatchQuarantinePolicy.formattedSize(recorder.quarantinedBytes))",
                            systemImage: "trash"
                        )
                        .font(.caption2)
                    }
                    .confirmationDialog(
                        "删除全部 \(recorder.quarantinedFiles.count) 个？",
                        isPresented: $isConfirmingDeleteAll,
                        titleVisibility: .visible
                    ) {
                        Button("删除 · 不可恢复", role: .destructive) {
                            recorder.deleteAllQuarantined()
                        }
                        Button("取消", role: .cancel) {}
                    } message: {
                        Text("Mac 上没有这些片段，删掉就找不回来了。")
                    }
                }
            }

            if !recorder.quarantineMessage.isEmpty {
                Text(recorder.quarantineMessage)
                    .font(.caption2)
                    .foregroundStyle(recorder.quarantineMessage.contains("失败") ? .orange : .green)
            }
        }
        .navigationTitle("坏文件")
        .task {
            recorder.refreshQuarantine()
        }
    }

    private func title(for file: WatchQuarantinedFile) -> String {
        return file.displayName
    }

    private func detail(for file: WatchQuarantinedFile) -> String {
        let size = WatchQuarantinePolicy.formattedSize(file.byteCount)
        guard let quarantinedAt = file.quarantinedAt else { return size }
        return "\(size) · \(shanghaiDateTime(quarantinedAt))"
    }
}

/// Tactile button press feedback following Apple design principles.
private struct PlainScaleButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.96 : 1.0)
            .animation(.spring(response: 0.25, dampingFraction: 0.7), value: configuration.isPressed)
    }
}

#if DEBUG
private struct RecorderHomeView_Previews: PreviewProvider {
    static var previews: some View {
        RecorderHomeView(recorder: WatchRecordingCoordinator())
    }
}
#endif
