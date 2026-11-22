import AVFAudio
import Combine
import Foundation
import WatchKit
import os

/// Segment-rotation timing probes for Series 5. Read them on the Mac with
/// `log stream --device --predicate 'subsystem == "com.lifelogger.watch"'`.
private let captureLog = Logger(subsystem: "com.lifelogger.watch", category: "capture")
private let batteryLog = Logger(subsystem: "com.lifelogger.watch", category: "battery")
private let recoveryLog = Logger(subsystem: "com.lifelogger.watch", category: "recovery")

enum WatchRecorderState: Equatable {
    case ready
    case requestingPermission
    case recording
    case stopping
    case failed
}

@MainActor
final class WatchRecordingCoordinator: NSObject, AVAudioRecorderDelegate, WatchCaptureRecorderDelegate, ObservableObject {
    static let interruptionRefreshTaskIdentifier = "com.lifelogger.watch.capture-recovery"
    @Published private(set) var state: WatchRecorderState = .ready {
        didSet {
            WatchCaptureNetworkGate.shared.setBlocked(
                state == .requestingPermission || state == .recording || state == .stopping
            )
        }
    }
    @Published private(set) var statusMessage = ""
    @Published var isCapsuleActive: Bool = false
    @Published var lastPinNotice: String? = nil
    private var capsuleStartTime: Date? = nil
    private var capsuleStartOffsetMs: Int64 = 0
    private var playedMilestones: Set<WatchGesturePinPolicy.MilestoneReminder> = []
    private var capsuleMilestoneTask: Task<Void, Never>?
    private var currentPendingPinType: String? = nil
    @Published private(set) var pendingSegmentCount = 0
    private(set) var queuedSegments: [AudioSegment] = []
    @Published private(set) var recentSegments: [AudioSegment] = []
    @Published private(set) var startedAt: Date?
    @Published private(set) var nextSegmentNumber = 1
    @Published private(set) var syncRevision = 0
    @Published private(set) var isRecoveringStagedAudio = true
    @Published private(set) var sessions: [RecordingSessionManifest] = []
    /// Waveform levels are published by `WatchMeterModel` rather than by this
    /// coordinator, so the 150 ms meter ticks — and the flatten that runs the moment
    /// the Digital Crown is pressed — only invalidate the waveform strip instead of
    /// the whole screen tree.
    let meter: WatchMeterModel
    /// Publishes the recording to the system Now Playing / audio-app surface.
    let nowPlaying = WatchNowPlayingSession()
    @Published private(set) var lastSavedAt: Date?
    private(set) var savedMilliseconds: Int64 = 0
    @Published private(set) var hasUnrecoveredStagedAudio = false
    /// Staged audio the Watch could not read, kept on disk as `<name>.unreadable`.
    ///
    /// Recovery deliberately keeps the bytes (they are the only copy of that moment), but
    /// nothing ever surfaced or removed them: they occupied Watch storage for good and kept
    /// their `staging/<session>/` directory alive. This is the app's visible, cleanable view
    /// of that rubbish.
    @Published private(set) var quarantinedFiles: [WatchQuarantinedFile] = []
    @Published private(set) var quarantineMessage = ""

    var quarantinedBytes: Int64 {
        WatchQuarantinePolicy.totalBytes(quarantinedFiles)
    }

    /// `nil` while there is nothing to clean, so the home screen stays compact.
    var quarantineStatusLine: String? {
        WatchQuarantinePolicy.statusLine(
            count: quarantinedFiles.count,
            totalBytes: quarantinedBytes
        )
    }

    func updateMeter() {
        guard let currentRecorder, currentRecorder.isRecording else {
            meter.push(0)
            return
        }
        currentRecorder.updateMeters()
        let level = max(0, min(1, (Double(currentRecorder.averagePower(forChannel: 0)) + 60) / 60))
        meter.push(level)
    }

    func consumePendingPins(sessionID: String, offsetMilliseconds: Int64) -> [WatchSessionPin] {
        guard let pinType = currentPendingPinType else { return [] }
        currentPendingPinType = nil
        let pin = WatchSessionPin(
            sessionID: sessionID,
            type: pinType,
            recordedAt: Date(),
            offsetMilliseconds: offsetMilliseconds
        )
        return [pin]
    }

    func triggerDoublePinchShutter() {
        let device = WKInterfaceDevice.current()
        device.play(.click)
        currentPendingPinType = "double_pinch"
        lastPinNotice = "⚡ 捏捏快门：已打点"
        WatchCaptureEventLog.shared.record("gesture -> double_pinch shutter (pinned to upcoming segment)")
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(3))
            await MainActor.run {
                if self?.lastPinNotice == "⚡ 捏捏快门：已打点" {
                    self?.lastPinNotice = nil
                }
            }
        }
    }

    func triggerDoubleClenchCapsule() {
        let device = WKInterfaceDevice.current()
        if !isCapsuleActive {
            isCapsuleActive = true
            let startDate = Date()
            let startOffset = savedMilliseconds
            capsuleStartTime = startDate
            capsuleStartOffsetMs = startOffset
            playedMilestones.removeAll()
            device.play(.start)
            lastPinNotice = "✊ 会话胶囊：已开启"
            startCapsuleMilestoneMonitor()
            if let sessionID = currentSessionID {
                Task { [weak self] in
                    try? await self?.ledger.startCapsule(sessionID: sessionID, startOffsetMilliseconds: startOffset, startedAt: startDate)
                }
            }
            if !isRecording {
                start()
            }
        } else {
            isCapsuleActive = false
            let endOffsetMs = savedMilliseconds
            let startDate = capsuleStartTime ?? Date()
            let endDate = Date()
            capsuleStartTime = nil
            playedMilestones.removeAll()
            capsuleMilestoneTask?.cancel()
            capsuleMilestoneTask = nil
            device.play(.stop)
            lastPinNotice = "✊ 会话胶囊：已封口打包"
            WatchCaptureEventLog.shared.record("gesture -> double_clench capsule seal [\(capsuleStartOffsetMs)ms - \(endOffsetMs)ms]")
            if let sessionID = currentSessionID {
                Task { [weak self] in
                    try? await self?.ledger.sealCapsule(sessionID: sessionID, endOffsetMilliseconds: endOffsetMs, endedAt: endDate)
                }
            }
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(3))
                await MainActor.run {
                    if self?.lastPinNotice == "✊ 会话胶囊：已封口打包" {
                        self?.lastPinNotice = nil
                    }
                }
            }
        }
    }

    private func startCapsuleMilestoneMonitor() {
        capsuleMilestoneTask?.cancel()
        capsuleMilestoneTask = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    try await Task.sleep(for: .seconds(1))
                } catch {
                    return
                }
                guard let self = self, self.isCapsuleActive, let anchor = self.capsuleStartTime else {
                    return
                }
                let elapsed = Date().timeIntervalSince(anchor)
                let milestone = WatchGesturePinPolicy.milestoneForElapsedSeconds(elapsed)
                if milestone != .none && !self.playedMilestones.contains(milestone) {
                    await MainActor.run {
                        self.playedMilestones.insert(milestone)
                        let device = WKInterfaceDevice.current()
                        switch milestone {
                        case .oneMinuteLightTap:
                            device.play(.click)
                            self.lastPinNotice = "1分钟：胶囊持续中"
                        case .fiveMinuteMilestone:
                            device.play(.notification)
                            self.lastPinNotice = "5分钟：长事件节点"
                        case .fifteenMinuteMilestone:
                            device.play(.notification)
                            self.lastPinNotice = "15分钟：会议进度"
                        case .thirtyMinuteMilestone:
                            device.play(.notification)
                            self.lastPinNotice = "30分钟：会议进度"
                        case .none:
                            break
                        }
                    }
                }
            }
        }
    }

    func suspendMeterDisplay() {
        meter.suspend()
    }

    var isRecording: Bool {
        currentSessionID != nil && state == .recording
    }
    /// 录音界面要如实显示的异常。没有异常返回 nil。
    var captureNotice: String? {
        guard state == .recording else { return nil }
        if isAudioInterrupted { return "麦克风被系统占用，恢复后自动继续" }
        if currentRecorder?.isRecording != true { return "录音中断，正在恢复…" }
        if currentRecorder?.livenessIssue() != nil { return "暂时没收到麦克风声音，正在恢复…" }
        if isStorageLow { return "手表空间不多了，记得连 Mac 上传" }
        return nil
    }

    var isCaptureWriting: Bool {
        state == .recording && !isAudioInterrupted && currentRecorder?.isRecording == true
            && currentRecorder?.livenessIssue() == nil
    }

    var hasActiveRecordingSession: Bool {
        currentSessionID != nil
    }
    var isBusy: Bool {
        isRecoveringStagedAudio
            || state == .requestingPermission
            || state == .stopping
    }
    var liveSyncedSegmentCount: Int {
        guard let currentSessionID,
              let session = sessions.first(where: { $0.id == currentSessionID }) else {
            return 0
        }
        return session.segments.filter { $0.uploadState == .acknowledged }.count
    }

    let ledger: SegmentLedger
    private var syncWatchdogTask: Task<Void, Never>?
    let syncCoordinator: WatchSyncCoordinator
    private let storageRoot: URL
    private(set) var currentSessionID: String?
    private var startingSessionID: String?
    private var isReplayingStaged = false
    private var lastStallRepairAt = Date.distantPast
    private var criticalStorageChecks = 0
    @Published private(set) var isStorageLow = false
    private let continuousCapture = WatchContinuousAudioCapture()
    private var currentRecorder: WatchCaptureRecorder?
    private var captureTimelineAnchor: Date?
    private var contextByFilePath: [String: SegmentContext] = [:]
    private var handledRecorderPaths: Set<String> = []
    private var stopRequested = false
    private var captureFailure: String?
    /// Local session belief; activation is still reasserted at each segment boundary.
    private var isCaptureSessionActive = false
    /// Last battery snapshot that came from a usable WatchKit reading in this capture
    /// session. Segment rotation reuses it while `batteryLevel` is still unavailable.
    private var lastCommittedBatterySnapshot: WatchBatterySnapshot?
    private var stagedRecoveryTask: Task<Void, Never>?
    private var interruptionObserver: NSObjectProtocol?
    private var routeChangeObserver: NSObjectProtocol?
    private var isAudioInterrupted = false
    private var finalizationTask: Task<Void, Never>?
    /// Single in-flight attempt to bring a dead recorder back (rotation failure / interruption).
    private var recoveryTask: Task<Void, Never>?
    /// Notices a recording that has silently stopped (lost delegate callback, suspended rotation).
    private var watchdogTask: Task<Void, Never>?
    private var mediaServicesObserver: NSObjectProtocol?
    private var interruptionRefreshScheduled = false
    private static let interruptionFailureMessage = "录音被系统打断（来电/Siri/其它应用占用麦克风）"
    private let eventLog = WatchCaptureEventLog.shared
    private var runtimeSession: WKExtendedRuntimeSession?

    private func startRuntimeSession() {
        guard runtimeSession == nil || runtimeSession?.state == .invalid else { return }
        let session = WKExtendedRuntimeSession()
        session.delegate = self
        runtimeSession = session
        session.start()
        eventLog.record("physical-therapy runtime session start requested")
    }

    private func stopRuntimeSession() {
        guard let session = runtimeSession else { return }
        if session.state == .running || session.state == .scheduled {
            session.invalidate()
        }
        runtimeSession = nil
        eventLog.record("physical-therapy runtime session invalidated")
    }

    /// Rotation length lives in `WatchSegmentRotationPolicy`, next to the Mac-compatibility
    /// rule it has to satisfy, and is covered by `WatchSegmentRotationPolicyTests` so the
    /// duration argument cannot quietly go missing again.
    private static var segmentDurationSeconds: TimeInterval {
        WatchSegmentRotationPolicy.segmentDurationSeconds
    }
    private static var segmentDurationMilliseconds: Int64 {
        WatchSegmentRotationPolicy.segmentDurationMilliseconds
    }

    /// Identical for every segment. Rebuilding this dictionary on each 60 s rotation
    /// was pure main-thread waste on the record/rotate path.
    private static let segmentRecordingSettings: [String: Any] = [
        AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
        AVSampleRateKey: 16_000,
        AVNumberOfChannelsKey: 1,
        AVEncoderBitRateKey: WatchAudioEncoding.bitRate,
        AVEncoderAudioQualityKey: AVAudioQuality.medium.rawValue,
    ]

    private struct SegmentContext {
        let sequence: Int
        let startOffsetMilliseconds: Int64
        let batterySnapshot: WatchBatterySnapshot?
    }

    override init() {
        let supportRoot = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
        storageRoot = supportRoot.appendingPathComponent("LifeLogger", isDirectory: true)
        ledger = SegmentLedger.liveShared
        meter = WatchMeterModel()
        let coordinator = WatchSyncCoordinator(ledger: ledger, eventLog: eventLog)
        syncCoordinator = coordinator
        super.init()
        coordinator.onSyncLifecycleChanged = { [weak self] isActive in
            guard let self else { return }
            self.syncWatchdogTask?.cancel()
            self.syncWatchdogTask = nil
            if isActive {
                if !self.hasActiveRecordingSession {
                    self.startRuntimeSession()
                }
                // Cap background sync session to 5 minutes max to guard battery
                self.syncWatchdogTask = Task { @MainActor [weak self] in
                    try? await Task.sleep(for: .seconds(300))
                    guard let self else { return }
                    if !self.hasActiveRecordingSession {
                        self.eventLog.record("sync watchdog expired; invalidating runtime session")
                        self.stopRuntimeSession()
                    }
                }
            } else {
                if !self.hasActiveRecordingSession {
                    self.stopRuntimeSession()
                }
            }
        }
        continuousCapture.onSegmentRotated = { [weak self, ledger, eventLog] url, sequence, error in
            eventLog.record("onSegmentRotated: seq=\(sequence), url=\(url.lastPathComponent), err=\(error?.localizedDescription ?? "nil")")
            if let error {
                Task { @MainActor [weak self] in
                    self?.recoverFromSegmentError(fileURL: url, message: error.localizedDescription)
                }
                return
            }

            let segmentEndedAt = Date()
            let sessionID = url.deletingLastPathComponent().lastPathComponent
            guard !sessionID.isEmpty else { return }
            let standardizedPath = url.standardizedFileURL.path

            let durationMs = (try? FinalizedAudio.durationMilliseconds(at: url)) ?? 60_000
            let currentManifest = try? await ledger.loadSession(id: sessionID)
            let previousEnd = currentManifest?.segments.last.map {
                $0.startOffsetMilliseconds + $0.durationMilliseconds
            } ?? 0
            let startOffsetMs = WatchRotatedTimingPolicy.startOffset(
                sequence: sequence,
                previousEndMilliseconds: previousEnd,
                sessionStartedAt: currentManifest?.startedAt,
                segmentEndedAt: segmentEndedAt,
                durationMilliseconds: durationMs
            )

            let (batterySnapshot, healthSnapshot, locationSnapshot, pins) = await MainActor.run {
                // 引擎已经开始写 sequence + 1，中断恢复要从再下一段接着录，不能回到旧序号。
                // 没有下一段时（被 stop 或重置收尾）只能到 sequence + 1，否则会留下序号空洞。
                if let self, self.currentSessionID == sessionID {
                    let next = self.currentRecorder?.isRecording == true ? sequence + 2 : sequence + 1
                    self.nextSegmentNumber = max(self.nextSegmentNumber, next)
                }
                let batt = self?.captureBatterySnapshot(sequence: sequence)
                let health = WatchHealthSnapshotManager.shared.captureSnapshot()
                let loc = WatchLocationSnapshotManager.shared.captureSnapshot()
                let p = self?.consumePendingPins(sessionID: sessionID, offsetMilliseconds: startOffsetMs) ?? []
                return (batt, health, loc, p)
            }

            do {
                _ = try await ledger.appendSegment(
                    sessionID: sessionID,
                    sequence: sequence,
                    sourceURL: url,
                    startOffsetMilliseconds: startOffsetMs,
                    durationMilliseconds: durationMs,
                    batterySnapshot: batterySnapshot,
                    healthSnapshot: healthSnapshot,
                    location: locationSnapshot,
                    pins: pins
                )
                try? FileManager.default.removeItem(at: url)
                await MainActor.run { self?.savedMilliseconds += durationMs }
                eventLog.record("segment \(sequence) saved & indexed to ledger (\(durationMs)ms, batt=\(batterySnapshot?.levelPercent.map(String.init) ?? "none")%, hr=\(healthSnapshot?.heartRateBPM.map { String(format: "%.0f", $0) } ?? "none"))")
            } catch {
                eventLog.record("INDEX FAILED seg \(sequence): \(error.localizedDescription); retained for recovery")
                await self?.replayStagedSegments(
                    sessionID: sessionID,
                    directory: url.deletingLastPathComponent(),
                    upTo: sequence,
                    batterySnapshot: batterySnapshot,
                    healthSnapshot: healthSnapshot,
                    location: locationSnapshot,
                    pins: pins ?? []
                )
            }

            Task { @MainActor [weak self] in
                guard let self else { return }
                self.handledRecorderPaths.insert(standardizedPath)
                self.lastSavedAt = .now
                self.checkStorage()
                self.syncRevision += 1
                await self.refreshPendingQueue()
                // Automatic uploads must use the same coordinator gate as the manual
                // "上传已录分段" button. The old path called the worker directly, so the
                // UI still looked idle while the worker was registering/uploading; a tap
                // then hit the actor's "正在上传中" guard and was shown as a false
                // "Mac 会话登记失败". The coordinator owns the visible in-flight state.
                await self.syncCoordinator.uploadSealedSegmentsDuringCapture(
                    sessionID: sessionID
                )
                await self.refreshPendingQueue()
            }
        }
        continuousCapture.onFailure = { [weak self] message in
            guard let self else { return }
            self.eventLog.record("CONTINUOUS CAPTURE FAILURE: \(message)")
            guard self.currentSessionID != nil, self.state == .recording, !self.stopRequested else {
                self.captureFailure = message
                self.stop()
                return
            }
            // Do not abruptly terminate recording on an engine glitch; seal current segment and recover.
            self.statusMessage = "音频流出现异常，尝试恢复中…"
            if let recorder = self.currentRecorder {
                let fileURL = recorder.currentSegmentURL.standardizedFileURL
                self.handleFinishedSegment(
                    fileURL: fileURL,
                    successfully: FileManager.default.fileExists(atPath: fileURL.path)
                )
            }
            self.scheduleCaptureRecovery(cause: .rotationFailed, reason: "continuous capture failure: \(message)")
        }
        continuousCapture.onQueueWarning = { [weak self] summary in
            self?.eventLog.record("QUEUE WARNING: \(summary)")
        }
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance(),
            queue: .main
        ) { [weak self] notification in
            guard
                let rawType = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            else {
                return
            }
            let rawReason = notification.userInfo?[AVAudioSessionInterruptionReasonKey] as? UInt
            Task { @MainActor [weak self] in
                self?.handleAudioSessionInterruption(rawType: rawType, rawReason: rawReason)
            }
        }
        routeChangeObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.routeChangeNotification,
            object: AVAudioSession.sharedInstance(),
            queue: .main
        ) { [weak self] notification in
            guard
                let rawReason = notification.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            else {
                return
            }
            let reason = AVAudioSession.RouteChangeReason(rawValue: rawReason)
            let previousRoute = notification.userInfo?[AVAudioSessionRouteChangePreviousRouteKey] as? AVAudioSessionRouteDescription
            Task { @MainActor [weak self] in
                self?.handleAudioSessionRouteChange(reason: reason, previousRoute: previousRoute)
            }
        }
        mediaServicesObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.eventLog.record("media services were reset")
                self.isCaptureSessionActive = false
                guard self.currentSessionID != nil, self.state == .recording, !self.stopRequested else { return }
                self.isAudioInterrupted = true
                if let recorder = self.currentRecorder {
                    let fileURL = recorder.currentSegmentURL.standardizedFileURL
                    recorder.stop()
                    self.continuousCapture.stopCapture()
                    self.handleFinishedSegment(fileURL: fileURL, successfully: true)
                }
                self.scheduleCaptureRecovery(cause: .rotationFailed, reason: "mediaServicesReset")
            }
        }
        statusMessage = "正在检查已保存的录音…"
        eventLog.record("app launched (build \(Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "?"))")
        WatchLocationSnapshotManager.shared.startMonitoringIfPermitted()
        Task { @MainActor [weak self] in
            await self?.refreshPendingQueue()
        }
    }

    func start() {
        guard !isRecoveringStagedAudio,
              currentSessionID == nil,
              currentRecorder == nil,
              state == .ready || state == .failed else { return }
        state = .requestingPermission
        statusMessage = "正在申请麦克风权限…"

        Task { @MainActor in
            guard await Self.requestMicrophonePermission() else {
                state = .failed
                statusMessage = "请允许使用麦克风，才能开始录音。"
                return
            }
            WatchLocationSnapshotManager.shared.startMonitoringIfPermitted()

            do {
                if WatchStoragePolicy.level(availableBytes: availableStorageBytes()) == .critical {
                    captureFailure = "手表空间不足，暂时不能录音。连上 Mac 传完已录内容后会释放空间。"
                    statusMessage = captureFailure ?? ""
                    state = .failed
                    return
                }
                try configureCaptureSession()
                try activateCaptureSession()
                prepareCaptureEnvironment()
                startRuntimeSession()
                nowPlaying.begin { [weak self] in self?.elapsedSeconds ?? 0 }
                // A new capture session must not inherit the previous session's reading.
                lastCommittedBatterySnapshot = nil

                let sessionID = UUID().uuidString.replacingOccurrences(of: "-", with: "")
                startingSessionID = sessionID
                defer { startingSessionID = nil }
                _ = try await ledger.createSession(id: sessionID)
                currentSessionID = sessionID
                captureTimelineAnchor = nil
                startedAt = .now
                nextSegmentNumber = 1
                savedMilliseconds = 0
                captureLog.notice("capture session started")
                lastSavedAt = nil
                handledRecorderPaths.removeAll(keepingCapacity: true)
                contextByFilePath.removeAll(keepingCapacity: true)
                stopRequested = false
                isAudioInterrupted = false
                captureFailure = nil
                await refreshPendingQueue()

                try startSegment(sequence: 0)
                state = .recording
                statusMessage = "正在录音 · 已封段将自动边录边传至 Mac"
                eventLog.record("capture session \(sessionID.prefix(8)) started")
                // A normal sync is cancelled when capture takes the network gate.
                // Immediately hand its saved outbox to the capture-safe worker instead
                // of waiting for the first 60-second rotation or a second button tap.
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    await self.syncCoordinator.syncPending(recordingSessionID: sessionID)
                    await self.refreshPendingQueue()
                }
                // 【2026-10-15 修】会话一开始就预热 HealthKit 采样：等到第一个分段轮转
                // （60 秒后）时心率/步数已在缓存里。原来只在授权回调里刷过一次，
                // 而 captureSnapshot() 同步读缓存 → 首段必为 nil（实测 7246 个 segment
                // 里 health_snapshot 出现 0 次）。
                WatchHealthSnapshotManager.shared.primeForRecording()
                startWatchdog()
                WKInterfaceDevice.current().play(.start)
            } catch {
                captureFailure = "录音启动失败：\(error.localizedDescription)"
                eventLog.record("START FAILED: \(error.localizedDescription)")
                if let currentSessionID {
                    state = .stopping
                    statusMessage = "正在整理启动失败前生成的录音…"
                    // 【2026-10-15 修】结束会话时只扫**本会话**的 staging，不再遍历全部历史会话。
                    // 原实现每次结束都先 listSessions() 再逐会话扫 staging、对每个 staged 文件读音频
                    // 测时长（measureDurationMilliseconds），复杂度随历史会话数增长 —— 用户报的
                    // 「长时间录音后长按暂停，卡在"保存文件"很久」正是这个。历史会话的遗留 staging
                    // 交给下次启动的 recoverStagedAudioIfNeeded() 兜底。
                    let recoveryIssue = await recoverStagedAudio(targetSessionID: currentSessionID)
                    let completionDisposition = CaptureSessionCompletionDisposition.resolve(
                        hasUnresolvedStagedAudio: hasStagedAudio(for: currentSessionID)
                    )
                    if completionDisposition == .finishManifest {
                        do {
                            try await ledger.finishSession(id: currentSessionID)
                        } catch {
                            captureFailure = appendFailure(
                                "录音记录无法正常结束：\(error.localizedDescription)",
                                to: captureFailure
                            )
                        }
                    } else {
                        captureFailure = appendFailure(
                            "暂存音频尚未完成索引，旧录音会话保持待恢复。",
                            to: captureFailure
                        )
                    }
                    if hasStagedAudio(for: currentSessionID), let recoveryIssue {
                        captureFailure = appendFailure(
                            "暂存音频问题：\(recoveryIssue.statusMessage)",
                            to: captureFailure
                        )
                    }
                    self.currentSessionID = nil
                    startedAt = nil
                    syncRevision += 1
                    await refreshPendingQueue()
                }
                continuousCapture.stopCapture()
                stopRuntimeSession()
                nowPlaying.end()
                try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
                isCaptureSessionActive = false
                lastCommittedBatterySnapshot = nil
                state = .failed
                statusMessage = captureFailure ?? "录音启动失败，请重试。"
            }
        }
    }

    func stop() {
        let disposition = CaptureStopDisposition.resolve(
            hasActiveSession: currentSessionID != nil,
            hasRecorder: currentRecorder != nil,
            recorderIsRecording: currentRecorder?.isRecording == true
        )
        guard disposition != .ignore else { return }

        stopRequested = true
        state = .stopping
        statusMessage = "正在保存已录音频…"
        recoveryTask?.cancel()
        recoveryTask = nil
        eventLog.record("stop requested (\(String(describing: disposition))) failure=\(captureFailure ?? "none")")

        switch disposition {
        case .ignore:
            return
        case .finalizeSessionWithoutRecorder:
            finalizeSessionWithoutRecorder()
        case .stopRecorderAndAwaitCompletion, .finalizeStoppedRecorder:
            guard let currentRecorder else {
                finalizeSessionWithoutRecorder()
                return
            }
            let fileURL = currentRecorder.currentSegmentURL.standardizedFileURL
            currentRecorder.stop()
            continuousCapture.stopCapture()
            // 这一段已经收尾过时 handleFinishedSegment 会直接返回，界面会卡在“正在保存”。
            if handledRecorderPaths.contains(fileURL.path) {
                self.currentRecorder = nil
                finalizeSessionWithoutRecorder()
                return
            }
            handleFinishedSegment(
                fileURL: fileURL,
                successfully: FileManager.default.fileExists(atPath: fileURL.path)
            )
        }
    }

    private func finalizeSessionWithoutRecorder() {
        guard let sessionID = currentSessionID else { return }
        let previousFinalization = finalizationTask
        finalizationTask = Task { @MainActor in
            await previousFinalization?.value
            guard self.currentSessionID == sessionID else { return }

            // 【2026-10-15 修】结束会话时只扫**本会话**的 staging，不再遍历全部历史会话。
            // 原实现每次结束都先 listSessions() 再逐会话扫 staging、对每个 staged 文件读音频
            // 测时长（measureDurationMilliseconds），复杂度随历史会话数增长 —— 用户报的
            // 「长时间录音后长按暂停，卡在"保存文件"很久」正是这个。历史会话的遗留 staging
            // 交给下次启动的 recoverStagedAudioIfNeeded() 兜底。
            let recoveryIssue = await recoverStagedAudio(targetSessionID: sessionID)
            guard self.currentSessionID == sessionID else { return }
            let completionDisposition = CaptureSessionCompletionDisposition.resolve(
                hasUnresolvedStagedAudio: hasStagedAudio(for: sessionID)
            )
            if completionDisposition == .finishManifest {
                do {
                    try await ledger.finishSession(id: sessionID)
                } catch {
                    captureFailure = appendFailure(
                        "录音记录无法正常结束：\(error.localizedDescription)",
                        to: captureFailure
                    )
                }
            } else {
                captureFailure = appendFailure(
                    "暂存音频尚未完成索引，旧录音会话保持待恢复。",
                    to: captureFailure
                )
            }
            if hasStagedAudio(for: sessionID), let recoveryIssue {
                captureFailure = appendFailure(
                    "有一段暂存音频问题：\(recoveryIssue.statusMessage)",
                    to: captureFailure
                )
            }
            await refreshPendingQueue()
            guard self.currentSessionID == sessionID else { return }

            if let session = sessions.first(where: { $0.id == sessionID }) {
                savedMilliseconds = session.segments.reduce(Int64(0)) {
                    $0 + $1.durationMilliseconds
                }
                if !session.segments.isEmpty {
                    lastSavedAt = .now
                }
            }
            contextByFilePath.removeAll()
            handledRecorderPaths.removeAll()
            currentSessionID = nil
            startedAt = nil
            continuousCapture.stopCapture()
            nowPlaying.end()
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            isCaptureSessionActive = false
            lastCommittedBatterySnapshot = nil
            WKInterfaceDevice.current().play(.stop)

            if pendingSegmentCount > 0 {
                // Keep the active runtime session alive so the post-stop sync can finish
                // draining pending segments to Mac even if the user drops their wrist or turns away.
                eventLog.record("post-stop sync drain started with \(pendingSegmentCount) pending segment(s)")
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    await self.syncCoordinator.syncPending()
                    await self.refreshPendingQueue()
                }
            } else {
                stopRuntimeSession()
            }
            syncRevision += 1

            if let captureFailure {
                state = .failed
                statusMessage = captureFailure
            } else {
                state = .ready
                statusMessage = "录音已保存，已封段已同步或可稍后同步至 Mac。"
            }
        }
    }

    /// Drops the audio session so the system can suspend the app and let the screen sleep.
    ///
    /// Called whenever capture is not actually running (interruption, failed resume). Leaving
    /// the session active while nothing is being recorded is what pinned the Watch awake.
    private func releaseCaptureSession() {
        guard isCaptureSessionActive else { return }
        isCaptureSessionActive = false
        do {
            try AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        } catch {
            captureLog.notice(
                "audio session deactivate returned \(error.localizedDescription, privacy: .public); session considered inactive"
            )
        }
    }

    private func appendFailure(_ message: String, to existing: String?) -> String {
        guard let existing, !existing.isEmpty else { return message }
        return "\(existing)；\(message)"
    }

    /// Single place that decides whether the app may keep holding an active audio session.
    /// An active session while nothing is captured keeps the Watch awake (see
    /// `WatchCaptureSessionPolicy`).
    private func applyCaptureSessionPolicy() {
        let disposition = WatchCaptureSessionPolicy.disposition(
            hasActiveSession: isCaptureSessionActive,
            isRecording: currentRecorder?.isRecording ?? false,
            isInterrupted: isAudioInterrupted,
            stopRequested: stopRequested,
            hasOpenSession: currentSessionID != nil && state == .recording
        )
        switch disposition {
        case .release:
            releaseCaptureSession()
        case .activate, .unchanged:
            break
        }
    }

    // MARK: - Capture session & recovery

    private func configureCaptureSession() throws {
        let audioSession = AVAudioSession.sharedInstance()
        do {
            try audioSession.setCategory(
                .record,
                mode: .default
            )
            captureLog.notice("audio session category = record (pure microphone capture)")
            eventLog.record("audio session configured with .record")
        } catch {
            captureLog.error("record refused: \(error.localizedDescription, privacy: .public)")
            eventLog.record("record category refused (\(error.localizedDescription)); falling back to .playAndRecord")
            var options: AVAudioSession.CategoryOptions = [.mixWithOthers]
            if #available(watchOS 11.0, *) {
                options.insert(.allowBluetooth)
            }
            try audioSession.setCategory(
                .playAndRecord,
                mode: .default,
                options: options
            )
        }
    }

    /// Activates the audio session. Calling it while already active is effectively free, and
    /// it is done before *every* segment: `isCaptureSessionActive` is only our own belief, and
    /// the system can drop the session in the background without telling us. Trusting the flag
    /// is how a rotation could fail at the 60 s mark and end the whole recording.
    private func activateCaptureSession() throws {
        let audioSession = AVAudioSession.sharedInstance()
        do {
            try audioSession.setActive(true)
        } catch {
            eventLog.record("setActive(true) failed: \(error.localizedDescription), resetting...")
            try? audioSession.setActive(false, options: .notifyOthersOnDeactivation)
            do {
                try audioSession.setActive(true)
            } catch {
                eventLog.record("setActive retry failed: \(error.localizedDescription), trying .playAndRecord fallback")
                var options: AVAudioSession.CategoryOptions = [.mixWithOthers]
                if #available(watchOS 11.0, *) {
                    options.insert(.allowBluetooth)
                }
                try? audioSession.setCategory(.playAndRecord, mode: .default, options: options)
                try audioSession.setActive(true)
            }
        }
        isCaptureSessionActive = true
    }

    /// Tries to bring capture back right now. Returns `true` when a recorder is running.
    @discardableResult
    private func resumeCapture(reason: String) -> Bool {
        guard currentSessionID != nil, state == .recording, !stopRequested else { return false }
        let recorderHandled = currentRecorder.map {
            handledRecorderPaths.contains($0.currentSegmentURL.standardizedFileURL.path)
        } ?? false
        switch WatchCaptureResumePolicy.decide(
            recorderExists: currentRecorder != nil,
            recorderIsRecording: currentRecorder?.isRecording == true,
            recorderWasHandled: recorderHandled,
            engineRunning: continuousCapture.audioEngineIsRunning
        ) {
        case .alreadyCapturing:
            isAudioInterrupted = false
            return true
        case .reviveEngine:
            do {
                try continuousCapture.restartEngineIfNeeded()
                isAudioInterrupted = false
                return true
            } catch {
                eventLog.record("resume: engine restart failed (\(reason)): \(error.localizedDescription)")
                return false
            }
        case .waitForSegmentHandling:
            // A finished-but-unhandled recorder belongs to `handleFinishedSegment`, not to us.
            return false
        case .startNewSegment:
            currentRecorder = nil
        }
        do {
            try configureCaptureSession()
            try activateCaptureSession()
            try startSegment(sequence: nextSegmentNumber)
            isAudioInterrupted = false
            if captureFailure == Self.interruptionFailureMessage { captureFailure = nil }
            statusMessage = "录音已恢复，继续录制"
            eventLog.record("resumed (\(reason)) -> segment \(nextSegmentNumber - 1)")
            return true
        } catch {
            eventLog.record("resume failed (\(reason)): \(error.localizedDescription)")
            return false
        }
    }

    /// Keeps trying to bring a dead recorder back, with backoff, instead of closing the
    /// recording on the first failure. One attempt at a time.
    private func scheduleCaptureRecovery(
        cause: WatchCaptureRecoveryPolicy.Cause,
        reason: String
    ) {
        if cause == .interruption {
            scheduleInterruptionBackgroundRefresh()
        }
        guard recoveryTask == nil,
              currentSessionID != nil, state == .recording, !stopRequested else { return }
        eventLog.record("recovery scheduled (\(String(describing: cause))): \(reason)")
        recoveryTask = Task { @MainActor [weak self] in
            await self?.runCaptureRecovery(cause: cause)
            guard let self, !Task.isCancelled else { return }
            self.recoveryTask = nil
        }
    }

    private func runCaptureRecovery(cause: WatchCaptureRecoveryPolicy.Cause) async {
        let startedAt = Date()
        var attempt = 0
        while !Task.isCancelled {
            guard currentSessionID != nil, state == .recording, !stopRequested else { return }
            attempt += 1
            switch WatchCaptureRecoveryPolicy.nextStep(
                cause: cause,
                attempt: attempt,
                elapsedSeconds: Date().timeIntervalSince(startedAt)
            ) {
            case .giveUp:
                giveUpRecovery(cause: cause)
                return
            case .retry(let delay):
                do {
                    try await Task.sleep(for: .seconds(delay))
                } catch {
                    return
                }
                guard !Task.isCancelled else { return }
                if resumeCapture(reason: "recovery attempt \(attempt)") { return }
            }
        }
    }

    private func giveUpRecovery(cause: WatchCaptureRecoveryPolicy.Cause) {
        switch cause {
        case .interruption:
            // The policy never gives up interruptions; a long interruption uses slow retries.
            eventLog.record("interruption recovery remains pending")
        case .rotationFailed:
            captureFailure = "下一段录音无法启动（已重试\(Int(WatchCaptureRecoveryPolicy.graceSeconds(for: .rotationFailed))) 秒），已保存已录部分"
            eventLog.record("recovery gave up (rotation): closing the recording")
            stop()
        }
    }

    private func startWatchdog() {
        watchdogTask?.cancel()
        watchdogTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                do {
                    try await Task.sleep(for: .seconds(WatchCaptureRecoveryPolicy.watchdogIntervalSeconds))
                } catch {
                    return
                }
                guard let self, self.currentSessionID != nil else { return }
                self.watchdogTick()
            }
        }
    }

    /// Catches a recording that stopped without anyone noticing (a lost delegate callback, a
    /// Catches a recording that stopped without anyone noticing (a lost delegate callback, a
    /// rotation that never ran). Cheap: a few property reads every few seconds.
    private func watchdogTick() {
        guard state == .recording, !stopRequested else { return }
        if isAudioInterrupted {
            if recoveryTask == nil {
                eventLog.record("watchdog: audio marked interrupted but no recovery task; probing audio session")
                do {
                    try configureCaptureSession()
                    try activateCaptureSession()
                    isAudioInterrupted = false
                    eventLog.record("watchdog: session reactivated successfully; resuming capture")
                    if !resumeCapture(reason: "watchdog interruption recovery") {
                        scheduleCaptureRecovery(cause: .interruption, reason: "watchdog resume failed")
                    }
                } catch {
                    scheduleCaptureRecovery(cause: .interruption, reason: "watchdog session still unavailable")
                }
            }
            return
        }
        guard recoveryTask == nil else { return }

        if WatchCaptureResumePolicy.captureIsHealthy(
            engineRunning: continuousCapture.audioEngineIsRunning,
            recorderIsRecording: currentRecorder?.isRecording == true,
            livenessIssueFound: currentRecorder?.livenessIssue() != nil
        ) {
            return
        }

        if let recorder = currentRecorder {
            if let issue = recorder.livenessIssue() {
                eventLog.record("watchdog: recorder liveness issue: \(issue.rawValue)")
                switch issue {
                case .engineStopped:
                    do {
                        try continuousCapture.restartEngineIfNeeded()
                        eventLog.record("watchdog: revived stopped audio engine")
                        return
                    } catch {
                        eventLog.record("watchdog: failed to restart audio engine: \(error.localizedDescription)")
                    }
                case .inputStalled:
                    // 引擎看着在跑但没有数据：重装 tap 才可能修好，换文件只会产生空分段。
                    guard Date().timeIntervalSince(lastStallRepairAt) >= 60 else { return }
                    lastStallRepairAt = Date()
                    do {
                        try continuousCapture.reinstallTapAndRestartEngine()
                        eventLog.record("watchdog: reinstalled tap after input stall")
                        return
                    } catch {
                        eventLog.record("watchdog: tap reinstall failed: \(error.localizedDescription)")
                    }
                }
                let fileURL = recorder.currentSegmentURL.standardizedFileURL
                let standardizedPath = fileURL.path
                guard !handledRecorderPaths.contains(standardizedPath) else {
                    resumeAfterHandledRecorder(reason: "watchdog liveness")
                    return
                }
                handleFinishedSegment(
                    fileURL: fileURL,
                    successfully: FileManager.default.fileExists(atPath: fileURL.path)
                )
                scheduleCaptureRecovery(cause: .rotationFailed, reason: "watchdog liveness: \(issue.rawValue)")
                return
            }
            guard !recorder.isRecording else { return }
            eventLog.record("watchdog: recorder stopped without callback; sealing segment")
            let fileURL = recorder.currentSegmentURL.standardizedFileURL
            let standardizedPath = fileURL.path
            guard !handledRecorderPaths.contains(standardizedPath) else {
                resumeAfterHandledRecorder(reason: "stale handled recorder")
                return
            }
            handleFinishedSegment(
                fileURL: fileURL,
                successfully: FileManager.default.fileExists(atPath: fileURL.path)
            )
            return
        }
        eventLog.record("watchdog: session open but no recorder; resuming")
        if !resumeCapture(reason: "watchdog") {
            scheduleCaptureRecovery(cause: .rotationFailed, reason: "watchdog")
        }
    }

    func reconcileAfterForeground() {
        guard currentSessionID != nil, state == .recording else { return }
        suspendMeterDisplay()
        // Re-assert the audio-app identity on every return to the foreground so the system
        // re-evaluates the indicator instead of keeping a placement it settled on earlier.
        nowPlaying.refresh()
        if runtimeSession == nil || runtimeSession?.state == .invalid {
            eventLog.record("foreground: restoring physical-therapy runtime session")
            startRuntimeSession()
        }
        eventLog.record(
            "foreground: recorder=\(currentRecorder?.isRecording == true ? "running" : "idle") interrupted=\(isAudioInterrupted)"
        )
        guard !stopRequested else { return }
        if continuousCapture.audioEngineIsRunning || currentRecorder?.isRecording == true {
            isAudioInterrupted = false
            updateMeter()
            statusMessage = "正在录音 · 边录边传中，已封段自动同步至 Mac。"
            return
        }
        if let recorder = currentRecorder {
            let fileURL = recorder.currentSegmentURL.standardizedFileURL
            let standardizedPath = fileURL.path
            guard !handledRecorderPaths.contains(standardizedPath) else {
                resumeAfterHandledRecorder(reason: "stale handled recorder")
                return
            }
            handleFinishedSegment(
                fileURL: fileURL,
                successfully: FileManager.default.fileExists(atPath: fileURL.path)
            )
            return
        }
        // No recorder: whatever stopped it, the user is looking at the screen now — try at once.
        recoveryTask?.cancel()
        recoveryTask = nil
        if resumeCapture(reason: "foreground") { return }
        scheduleCaptureRecovery(
            cause: isAudioInterrupted ? .interruption : .rotationFailed,
            reason: "foreground"
        )
    }

    private func handleAudioSessionInterruption(rawType: UInt, rawReason: UInt?) {
        guard currentSessionID != nil, state == .recording else { return }
        guard let type = AVAudioSession.InterruptionType(rawValue: rawType) else {
            return
        }
        let reasonText = rawReason.map(String.init) ?? "n/a"

        switch type {
        case .began:
            // V43: watchOS sends spurious .began interruptions when the screen dims,
            // the user presses the Digital Crown, or audio ducking occurs. If the
            // engine is still running and receiving microphone packets, the capture
            // is alive — do NOT set isAudioInterrupted, or the next 60 s segment
            // rotation will refuse to continue and the recording dies silently.
            let engineAlive = continuousCapture.audioEngineIsRunning || currentRecorder?.audioEngineIsRunning == true
            if engineAlive {
                eventLog.record("interruption BEGAN reason=\(reasonText) — engine alive, IGNORING")
            } else {
                do {
                    try continuousCapture.restartEngineIfNeeded()
                    isAudioInterrupted = false
                    eventLog.record("interruption BEGAN reason=\(reasonText) — engine revived immediately")
                } catch {
                    isAudioInterrupted = true
                    suspendMeterDisplay()
                    statusMessage = "系统音频打断 · 等待恢复录音"
                    eventLog.record("interruption BEGAN reason=\(reasonText) — engine dead, scheduling recovery: \(error.localizedDescription)")
                    scheduleCaptureRecovery(cause: .interruption, reason: "interruption began (\(reasonText))")
                }
            }
        case .ended:
            isAudioInterrupted = false
            interruptionRefreshScheduled = false
            eventLog.record("interruption ENDED reason=\(reasonText)")
            guard !stopRequested, hasActiveRecordingSession else {
                applyCaptureSessionPolicy()
                return
            }
            try? activateCaptureSession()
            recoveryTask?.cancel()
            recoveryTask = nil
            if currentRecorder == nil, !resumeCapture(reason: "interruption ended") {
                scheduleCaptureRecovery(cause: .interruption, reason: "interruption ended, resume failed")
            } else {
                statusMessage = "正在录音 · 边录边传中，已封段自动同步至 Mac。"
            }
        @unknown default:
            break
        }
    }

    private func handleAudioSessionRouteChange(
        reason: AVAudioSession.RouteChangeReason?,
        previousRoute: AVAudioSessionRouteDescription?
    ) {
        let reasonStr: String
        switch reason {
        case .unknown: reasonStr = "unknown"
        case .newDeviceAvailable: reasonStr = "newDeviceAvailable"
        case .oldDeviceUnavailable: reasonStr = "oldDeviceUnavailable"
        case .categoryChange: reasonStr = "categoryChange"
        case .override: reasonStr = "override"
        case .wakeFromSleep: reasonStr = "wakeFromSleep"
        case .noSuitableRouteForCategory: reasonStr = "noSuitableRouteForCategory"
        case .routeConfigurationChange: reasonStr = "routeConfigurationChange"
        case .none: reasonStr = "nil"
        @unknown default: reasonStr = "unknownDefault"
        }
        eventLog.record("audio route change: reason=\(reasonStr)")
        guard currentSessionID != nil, state == .recording, !stopRequested else { return }

        do {
            try configureCaptureSession()
            try activateCaptureSession()
            try continuousCapture.restartEngineIfNeeded()
            eventLog.record("audio route change handled: session active, engine running=\(continuousCapture.audioEngineIsRunning)")
            if isAudioInterrupted {
                isAudioInterrupted = false
                statusMessage = "正在录音 · 边录边传中，已封段自动同步至 Mac。"
            }
        } catch {
            eventLog.record("audio route change handle failed: \(error.localizedDescription)")
            if !continuousCapture.audioEngineIsRunning {
                isAudioInterrupted = true
                statusMessage = "音频路由切换 · 正在重新连接麦克风…"
                scheduleCaptureRecovery(cause: .rotationFailed, reason: "route change (\(reasonStr))")
            }
        }
    }

    /// A best-effort second recovery path for a process suspended before `.ended` arrives.
    /// watchOS may defer or throttle this task, so in-process backoff, `.ended`, and foreground
    /// reconciliation remain in place as independent recovery routes.
    private func scheduleInterruptionBackgroundRefresh() {
        guard !interruptionRefreshScheduled,
              currentSessionID != nil,
              !stopRequested else { return }
        interruptionRefreshScheduled = true
        WKApplication.shared().scheduleBackgroundRefresh(
            withPreferredDate: Date(timeIntervalSinceNow: 60),
            userInfo: Self.interruptionRefreshTaskIdentifier as NSSecureCoding & NSObjectProtocol
        ) { [weak self] error in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if let error {
                    self.interruptionRefreshScheduled = false
                    self.eventLog.record(
                        "interruption background refresh scheduling failed: \(error.localizedDescription)"
                    )
                } else {
                    self.eventLog.record("interruption background recovery refresh scheduled")
                }
            }
        }
    }

    func handleInterruptionBackgroundRefresh() async {
        interruptionRefreshScheduled = false
        guard currentSessionID != nil,
              state == .recording,
              !stopRequested else { return }
        eventLog.record("interruption background recovery refresh fired")
        if resumeCapture(reason: "scheduled background refresh") { return }
        scheduleCaptureRecovery(
            cause: .interruption,
            reason: "scheduled background refresh could not resume"
        )
    }

    func elapsedText(at date: Date) -> String {
        let elapsed = Int(elapsedSeconds(at: date))
        let hours = elapsed / 3_600
        let minutes = (elapsed / 60) % 60
        let seconds = elapsed % 60
        return hours > 0
            ? String(format: "%02d:%02d:%02d", hours, minutes, seconds)
            : String(format: "%02d:%02d", minutes, seconds)
    }

    func elapsedSeconds(at date: Date = Date()) -> Double {
        guard let startedAt else {
            return Double(savedMilliseconds) / 1000
        }
        if state == .recording {
            return max(0, date.timeIntervalSince(startedAt))
        } else {
            return max(0, Double(savedMilliseconds) / 1000)
        }
    }

    /// Total seconds of audio captured in the current session.
    var elapsedSeconds: Double {
        elapsedSeconds(at: Date())
    }

    func refreshPendingQueue() async {
        // Staged audio recovery is ONLY for cleaning up past orphaned crashes,
        // it must NEVER run while an active recording session is running!
        if currentSessionID == nil && state != .recording {
            await recoverStagedAudioIfNeeded()
        }
        do {
            let sessions = try await ledger.listSessions()
            self.sessions = sessions
            let allSegments = sessions.flatMap { $0.segments.reversed() }
            // Cap in-memory recentSegments to 50 items to prevent unbounded memory growth on Watch Series 5
            recentSegments = Array(allSegments.prefix(50))
            queuedSegments = allSegments.filter { $0.uploadState != .acknowledged }
            pendingSegmentCount = queuedSegments.count
        } catch {
            statusMessage = "无法读取手表上的录音文件：\(error.localizedDescription)"
        }
        // Quarantined audio is part of "what is still sitting on this Watch", so it is
        // refreshed together with the queue: the home entry can never go stale after a
        // recovery that just set a new file aside.
        refreshQuarantine()
    }

    /// Cheap directory listing of `staging/**/*.unreadable` — safe on the record path.
    func refreshQuarantine() {
        quarantinedFiles = WatchQuarantinePolicy.scan(stagingRoot: stagingRoot)
        recoveryLog.debug(
            "quarantine refreshed: \(self.quarantinedFiles.count, privacy: .public) file(s), \(self.quarantinedBytes, privacy: .public) bytes"
        )
    }

    /// Removes one quarantined file. A failure is reported, not hidden: the file stays
    /// listed, because claiming it was cleaned when it wasn't is worse than showing it.
    func deleteQuarantined(_ file: WatchQuarantinedFile) {
        do {
            let freed = try WatchQuarantinePolicy.delete(file, stagingRoot: stagingRoot)
            quarantineMessage = freed > 0
                ? "已释放 \(WatchQuarantinePolicy.formattedSize(freed))"
                : "已删除（已无占用）"
        } catch {
            quarantineMessage = "删除失败：\(error.localizedDescription)"
        }
        refreshQuarantine()
    }

    func deleteAllQuarantined() {
        let removal = WatchQuarantinePolicy.deleteAll(quarantinedFiles, stagingRoot: stagingRoot)
        quarantineMessage = removal.failedCount > 0
            ? "已清理 \(removal.deletedCount) 个 · \(removal.failedCount) 个删除失败"
            : "已释放 \(WatchQuarantinePolicy.formattedSize(removal.bytesFreed))"
        refreshQuarantine()
    }

    private func recoverStagedAudioIfNeeded() async {
        guard currentSessionID == nil && state != .recording else { return }
        if let stagedRecoveryTask {
            await stagedRecoveryTask.value
            return
        }

        let task = Task { @MainActor [weak self] in
            guard let self, self.currentSessionID == nil, self.state != .recording else { return }
            _ = await self.recoverStagedAudio()
        }
        stagedRecoveryTask = task
        await task.value
        stagedRecoveryTask = nil
    }

    /// 正在开始或正在录的会话。恢复流程可能跑几十秒，每次都要重新判断，不能只在开头算一次。
    private var protectedSessionID: String? {
        if let startingSessionID { return startingSessionID }
        guard state == .recording || state == .requestingPermission || currentRecorder?.isRecording == true else {
            return nil
        }
        return currentSessionID
    }

    private func recoverStagedAudio(targetSessionID: String? = nil) async -> WatchRecoveryIssue? {
        let recoveryStarted = Date()
        eventLog.record("startup recovery began")
        if targetSessionID == nil, protectedSessionID == nil {
            let pruned = (try? await ledger.pruneFinishedSessions(olderThan: Date().addingTimeInterval(-7 * 86_400))) ?? 0
            if pruned > 0 { eventLog.record("pruned \(pruned) finished session manifest(s)") }
        }
        var recoveredCount = 0
        var recoveryIssue: WatchRecoveryIssue?
        let stagingRoot = storageRoot.appendingPathComponent("staging", isDirectory: true)

        do {
            let sessions = try await ledger.listSessions()
            let knownSessionIDs = Set(sessions.map(\.id))

            for session in sessions {
                if let targetSessionID, session.id != targetSessionID {
                    continue
                }
                if session.id == protectedSessionID {
                    // NEVER scan or modify staging files for the currently active recording session!
                    continue
                }
                let sessionStagingDirectory = stagingRoot
                    .appendingPathComponent(session.id, isDirectory: true)
                let hasStagingDirectory = FileManager.default.fileExists(
                    atPath: sessionStagingDirectory.path
                )
                guard session.stoppedAt == nil || hasStagingDirectory else { continue }

                let stagedFiles: [URL]
                if hasStagingDirectory {
                    do {
                        stagedFiles = try FileManager.default.contentsOfDirectory(
                            at: sessionStagingDirectory,
                            includingPropertiesForKeys: nil,
                            options: [.skipsHiddenFiles]
                        )
                        .filter {
                            $0.pathExtension.lowercased() == "m4a"
                                || $0.lastPathComponent.lowercased().hasSuffix(".m4a.unreadable")
                        }
                        .sorted { $0.lastPathComponent < $1.lastPathComponent }
                    } catch {
                        recoveryIssue = .blocking(error.localizedDescription)
                        continue
                    }
                } else {
                    stagedFiles = []
                }

                var canFinalizeSession = true
                var workingSegments = session.segments
                for stagedFile in stagedFiles {
                    guard let fileSequence = Self.sequence(fromStagedFile: stagedFile) else {
                        recoveryIssue = .blocking(
                            "Unrecognized staged audio file: \(stagedFile.lastPathComponent)"
                        )
                        canFinalizeSession = false
                        break
                    }

                    let sequence = WatchRecoverySequencePolicy.target(
                        fileSequence: fileSequence,
                        committed: Set(workingSegments.map(\.sequence))
                    )

                    let fileSize = (try? stagedFile.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
                    if fileSize <= 1024 && stagedFile.pathExtension.lowercased() == "m4a" {
                        try? FileManager.default.removeItem(at: stagedFile)
                        continue
                    }

                    var didReadAudio = false
                    do {
                        let measuredDuration = try await Self.measureDurationMilliseconds(at: stagedFile)
                        didReadAudio = true
                        let timing = WatchSegmentTimingPolicy.recovery(
                            sequence: sequence,
                            measuredDurationMilliseconds: measuredDuration,
                            manifestSegments: workingSegments
                        )
                        let recoveredSegment = try await ledger.recoverStagedSegment(
                            sessionID: session.id,
                            sequence: sequence,
                            sourceURL: stagedFile,
                            startOffsetMilliseconds: timing.startOffsetMilliseconds,
                            durationMilliseconds: timing.durationMilliseconds
                        )
                        workingSegments.removeAll { $0.sequence == sequence }
                        workingSegments.append(recoveredSegment)
                        workingSegments.sort { $0.sequence < $1.sequence }
                        recoveredCount += 1
                    } catch {
                        if didReadAudio {
                            // Ledger/disk errors are not corrupt audio. Preserve the original
                            // file and retry later instead of excluding it from recovery.
                            recoveryIssue = .blocking(error.localizedDescription)
                            canFinalizeSession = false
                            eventLog.record("RECOVERY WRITE FAILED: \(error.localizedDescription)")
                            continue
                        }
                        if stagedFile.pathExtension.lowercased() == "unreadable" {
                            // Already quarantined in a past session; preserve its bytes and do not re-alert.
                            continue
                        }
                        // One interrupted .m4a used to abort this whole session and leave the
                        // recorder failed on every launch. Set the file aside (bytes kept) and
                        // keep recovering the readable ones.
                        let outcome = WatchStagedAudioPolicy.recoveryOutcome(
                            unreadableStagedFile: stagedFile.lastPathComponent,
                            underlyingError: error.localizedDescription
                        )
                        guard case .quarantined(let badFileName, let reason) = outcome else {
                            continue
                        }
                        let quarantineDestination = stagedFile.deletingLastPathComponent()
                            .appendingPathComponent(
                                WatchStagedAudioPolicy.quarantineFileName(for: badFileName)
                            )
                        do {
                            try FileManager.default.moveItem(
                                at: stagedFile,
                                to: quarantineDestination
                            )
                            if recoveryIssue?.blocksRecording != true {
                                recoveryIssue = .quarantined(
                                    stagedFile: badFileName,
                                    detail: error.localizedDescription
                                )
                            }
                            recoveryLog.error(
                                "quarantined staged audio \(badFileName, privacy: .public): \(reason, privacy: .public)"
                            )
                            continue
                        } catch {
                            // Could not even set it aside: this one really is a blocker.
                            recoveryIssue = .blocking(
                                "Staged audio kept on Watch: \(badFileName) · \(error.localizedDescription)"
                            )
                            canFinalizeSession = false
                            break
                        }
                    }
                }

                guard canFinalizeSession else { continue }
                if session.stoppedAt == nil {
                    if session.id == protectedSessionID {
                        continue
                    }
                    do {
                        try await ledger.finishSession(id: session.id)
                    } catch {
                        recoveryIssue = .blocking(error.localizedDescription)
                    }
                }
                if hasStagingDirectory,
                   let remaining = try? FileManager.default.contentsOfDirectory(
                       at: sessionStagingDirectory,
                       includingPropertiesForKeys: nil,
                       options: [.skipsHiddenFiles]
                   ), remaining.isEmpty {
                    try? FileManager.default.removeItem(at: sessionStagingDirectory)
                }
            }

            if let stagedSessionDirectories = try? FileManager.default.contentsOfDirectory(
                at: stagingRoot,
                includingPropertiesForKeys: nil,
                options: [.skipsHiddenFiles]
            ),
               let orphan = stagedSessionDirectories.first(where: { directory in
                   guard directory.lastPathComponent != currentSessionID else { return false }
                   guard !knownSessionIDs.contains(directory.lastPathComponent),
                         let contents = try? FileManager.default.contentsOfDirectory(
                             at: directory,
                             includingPropertiesForKeys: nil,
                             options: [.skipsHiddenFiles]
                         ) else {
                       return false
                   }
                   // Only audio recovery could still index counts as an orphan. A directory left
                   // holding nothing but quarantined files is rubbish the 坏文件 screen lists and
                   // removes; failing the whole recorder over it leaves the app broken for a reason
                   // the user cannot act on from the recorder.
                   return WatchQuarantinePolicy.blocksRecovery(
                       contents: contents.map(\.lastPathComponent)
                   )
               }) {
                recoveryIssue = .blocking(
                    "Staged audio has no session manifest: \(orphan.lastPathComponent)"
                )
            }
        } catch {
            recoveryIssue = .blocking(error.localizedDescription)
        }

        if let recoveryIssue {
            switch recoveryIssue {
            case .quarantined(let stagedFile, _):
                statusMessage = "发现受损录音片段（\(stagedFile)），已安全隔离"
            case .blocking(let reason):
                statusMessage = "部分录音未能完全恢复：\(reason)"
            }
            // A quarantined file is a recorded, non-blocking issue: the app must not report
            // itself broken on every launch because of one unreadable staged file.
            if recoveryIssue.blocksRecording, state != .stopping, protectedSessionID == nil {
                state = .failed
            }
        } else if recoveredCount > 0 {
            statusMessage = "已恢复 \(recoveredCount) 段录音，并保存在手表。"
        } else {
            statusMessage = ""
        }
        hasUnrecoveredStagedAudio = recoveryIssue != nil
        isRecoveringStagedAudio = false
        eventLog.record("startup recovery finished recovered=\(recoveredCount) blocking=\(recoveryIssue?.blocksRecording == true) elapsedMs=\(Int(Date().timeIntervalSince(recoveryStarted) * 1000))")
        return recoveryIssue
    }

    private func hasStagedAudio(for sessionID: String) -> Bool {
        let directory = storageRoot
            .appendingPathComponent("staging", isDirectory: true)
            .appendingPathComponent(sessionID, isDirectory: true)
        guard FileManager.default.fileExists(atPath: directory.path) else { return false }
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ) else {
            return true
        }
        return files.contains { $0.pathExtension.lowercased() == "m4a" }
    }

    private static func sequence(fromStagedFile fileURL: URL) -> Int? {
        let audioURL = fileURL.pathExtension.lowercased() == "unreadable"
            ? fileURL.deletingPathExtension() : fileURL
        let filename = audioURL.deletingPathExtension().lastPathComponent
        let prefix = "segment-"
        guard filename.hasPrefix(prefix) else { return nil }
        return Int(filename.dropFirst(prefix.count))
    }

    nonisolated func audioRecorderDidFinishRecording(
        _ recorder: AVAudioRecorder,
        successfully flag: Bool
    ) {
        let fileURL = recorder.url.standardizedFileURL
        Task { @MainActor [weak self] in
            self?.handleFinishedSegment(
                fileURL: fileURL,
                successfully: flag,
                isAutoRotated: false
            )
        }
    }

    nonisolated func audioRecorderEncodeErrorDidOccur(
        _ recorder: AVAudioRecorder,
        error: Error?
    ) {
        let message = error?.localizedDescription ?? "Audio encoder failed."
        WatchCaptureEventLog.shared.record("ENCODE ERROR: \(message)")
        let fileURL = recorder.url.standardizedFileURL
        Task { @MainActor [weak self] in
            guard let self,
                  self.contextByFilePath[fileURL.path] != nil else {
                return
            }
            self.recoverFromSegmentError(fileURL: fileURL, message: message)
        }
    }

    nonisolated func audioRecorderDidFinishRecording(
        _ recorder: WatchCaptureRecorder,
        successfully flag: Bool
    ) {
        let fileURL = recorder.currentSegmentURL.standardizedFileURL
        Task { @MainActor [weak self] in
            self?.handleFinishedSegment(
                fileURL: fileURL,
                successfully: flag,
                isAutoRotated: false
            )
        }
    }

    nonisolated func audioRecorderEncodeErrorDidOccur(
        _ recorder: WatchCaptureRecorder,
        error: Error?
    ) {
        let message = error?.localizedDescription ?? "Audio encoder failed."
        WatchCaptureEventLog.shared.record("ENCODE ERROR: \(message)")
        let fileURL = recorder.currentSegmentURL.standardizedFileURL
        Task { @MainActor [weak self] in
            guard let self,
                  self.contextByFilePath[fileURL.path] != nil else {
                return
            }
            self.recoverFromSegmentError(fileURL: fileURL, message: message)
        }
    }

    /// 上一段已收尾但 currentRecorder 还留着旧对象时，不能就此不管，否则录音再也不恢复。
    private func resumeAfterHandledRecorder(reason: String) {
        currentRecorder = nil
        if !resumeCapture(reason: reason) {
            scheduleCaptureRecovery(cause: .rotationFailed, reason: reason)
        }
    }

    /// 单次写入或转换错误不结束录音：封存当前段再恢复。只有真的放不下才停止。
    private func recoverFromSegmentError(fileURL: URL, message: String) {
        eventLog.record("segment error: \(message)")
        guard currentSessionID != nil, state == .recording, !stopRequested else {
            captureFailure = message
            statusMessage = message
            stop()
            return
        }
        if WatchStoragePolicy.level(availableBytes: availableStorageBytes()) == .critical {
            captureFailure = "手表空间不足，录音已停止。已录的内容都保存了，连上 Mac 传完后会释放空间。"
            statusMessage = captureFailure ?? message
            stop()
            return
        }
        statusMessage = "录音分段异常，正在恢复…"
        let path = fileURL.standardizedFileURL.path
        if !handledRecorderPaths.contains(path) {
            handleFinishedSegment(
                fileURL: fileURL.standardizedFileURL,
                successfully: FileManager.default.fileExists(atPath: path)
            )
        }
        scheduleCaptureRecovery(cause: .rotationFailed, reason: "segment error: \(message)")
    }

    private func availableStorageBytes() -> Int64? {
        (try? FileManager.default.attributesOfFileSystem(forPath: storageRoot.path))?[.systemFreeSize] as? Int64
    }

    /// 分段封存后看一眼空间：不够时提前提醒，真的放不下才停止。
    private func checkStorage() {
        switch WatchStoragePolicy.level(availableBytes: availableStorageBytes()) {
        case .ok:
            isStorageLow = false
            criticalStorageChecks = 0
        case .low:
            if !isStorageLow { eventLog.record("storage low") }
            isStorageLow = true
            criticalStorageChecks = 0
        case .critical:
            isStorageLow = true
            criticalStorageChecks += 1
            // 系统可能正在回收空间，连续两次都不够才停。
            guard criticalStorageChecks >= 2, state == .recording, !stopRequested else { return }
            eventLog.record("storage critical; stopping capture")
            captureFailure = "手表空间不足，录音已停止。已录的内容都保存了，连上 Mac 传完后会释放空间。"
            statusMessage = captureFailure ?? ""
            stop()
        }
    }

    /// 某段登记失败后，把暂存的分段按顺序补登记，后面的分段才不会一直卡住。
    /// 序号按账本重排：中间确实丢了文件时也能继续，不会让整场卡死。
    private func replayStagedSegments(
        sessionID: String,
        directory: URL,
        upTo limit: Int,
        batterySnapshot: WatchBatterySnapshot? = nil,
        healthSnapshot: WatchHealthSnapshot? = nil,
        location: WatchLocationSnapshot? = nil,
        pins: [WatchSessionPin] = []
    ) async {
        guard !isReplayingStaged else { return }
        isReplayingStaged = true
        defer { isReplayingStaged = false }
        let names = (try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []
        let numbers = names.compactMap { name -> Int? in
            guard name.hasPrefix("segment-"), name.hasSuffix(".m4a") else { return nil }
            return Int(name.dropFirst("segment-".count).dropLast(".m4a".count))
        }.filter { $0 <= limit }.sorted()
        for number in numbers {
            guard let manifest = try? await ledger.loadSession(id: sessionID), manifest.stoppedAt == nil else { return }
            guard number >= manifest.segments.count else { continue }
            let fileURL = directory.appendingPathComponent(String(format: "segment-%06d.m4a", number))
            guard let duration = try? await Self.measureDurationMilliseconds(at: fileURL) else {
                // 读不出的文件（比如 0 帧）不能挡住后面的分段：改名隔离，恢复流程会跳过它。
                let quarantined = fileURL.deletingPathExtension().appendingPathExtension("unreadable")
                try? FileManager.default.moveItem(at: fileURL, to: quarantined)
                eventLog.record("replay set aside unreadable segment \(number)")
                continue
            }
            let previousEnd = manifest.segments.last.map {
                $0.startOffsetMilliseconds + $0.durationMilliseconds
            } ?? 0
            let endedAt = (try? fileURL.resourceValues(forKeys: [.contentModificationDateKey]))?
                .contentModificationDate ?? Date()
            let isLatest = number == limit
            do {
                _ = try await ledger.appendSegment(
                    sessionID: sessionID,
                    sequence: manifest.segments.count,
                    sourceURL: fileURL,
                    startOffsetMilliseconds: WatchRotatedTimingPolicy.startOffset(
                        sequence: manifest.segments.count,
                        previousEndMilliseconds: previousEnd,
                        sessionStartedAt: manifest.startedAt,
                        segmentEndedAt: endedAt,
                        durationMilliseconds: duration
                    ),
                    durationMilliseconds: duration,
                    batterySnapshot: isLatest ? batterySnapshot : nil,
                    healthSnapshot: isLatest ? healthSnapshot : nil,
                    location: isLatest ? location : nil,
                    pins: isLatest ? pins : []
                )
                try? FileManager.default.removeItem(at: fileURL)
                savedMilliseconds += duration
                eventLog.record("replayed staged segment \(number) as \(manifest.segments.count)")
            } catch {
                eventLog.record("replay failed at segment \(number): \(error.localizedDescription)")
                return
            }
        }
    }

    private func captureBatterySnapshot(sequence: Int) -> WatchBatterySnapshot? {
        let device = WKInterfaceDevice.current()
        let rawLevel = Double(device.batteryLevel)
        let batteryState: WatchBatteryState
        switch device.batteryState {
        case .unplugged: batteryState = .unplugged
        case .charging: batteryState = .charging
        case .full: batteryState = .full
        default: batteryState = .unknown
        }
        let batterySnapshot = WatchBatterySnapshot.resolved(
            level: rawLevel,
            state: batteryState,
            previous: lastCommittedBatterySnapshot
        )
        if WatchBatterySnapshot.capture(level: rawLevel, state: batteryState) != nil {
            lastCommittedBatterySnapshot = batterySnapshot
        }
        batteryLog.notice(
            "segment \(sequence, privacy: .public) battery raw=\(rawLevel, privacy: .public) state=\(batteryState.rawValue, privacy: .public) committed=\(batterySnapshot?.levelPercent.map(String.init) ?? "none", privacy: .public) fresh=\(batterySnapshot?.isFreshReading(level: rawLevel, state: batteryState) == true, privacy: .public)"
        )
        return batterySnapshot
    }

    private func startSegment(sequence: Int) throws {
        guard let currentSessionID else { return }
        let rotationStartedAt = Date()
        // Reassert activation at every boundary; the system may have invalidated our flag.
        try activateCaptureSession()
        let batterySnapshot = captureBatterySnapshot(sequence: sequence)
        let stagingDirectory = stagingRoot.appendingPathComponent(currentSessionID, isDirectory: true)
        if !FileManager.default.fileExists(atPath: stagingDirectory.path) {
            try FileManager.default.createDirectory(
                at: stagingDirectory,
                withIntermediateDirectories: true
            )
        }
        let fileURL = stagingDirectory.appendingPathComponent(
            String(format: "segment-%06d.m4a", sequence)
        )
        let recorder = try WatchCaptureRecorder(
            url: fileURL,
            settings: Self.segmentRecordingSettings,
            source: continuousCapture
        )
        recorder.isMeteringEnabled = true
        recorder.delegate = self
        guard recorder.prepareToRecord(),
              recorder.record(forDuration: Self.segmentDurationSeconds, autoRotate: true) else {
            throw WatchRecorderError.couldNotStartSegment
        }
        let elapsedMilliseconds = Int64(
            max(0, Date().timeIntervalSince(startedAt ?? .now)) * 1000
        )
        contextByFilePath[fileURL.standardizedFileURL.path] = SegmentContext(
            sequence: sequence,
            startOffsetMilliseconds: WatchSegmentTimingPolicy.startOffset(
                sequence: sequence,
                elapsedMilliseconds: elapsedMilliseconds
            ),
            batterySnapshot: batterySnapshot
        )
        currentRecorder = recorder
        nextSegmentNumber = sequence + 1
        captureLog.notice(
            "segment \(sequence, privacy: .public) rotation \(Int(Date().timeIntervalSince(rotationStartedAt) * 1000), privacy: .public)ms"
        )
        eventLog.record(
            "segment \(sequence) recording (setup \(Int(Date().timeIntervalSince(rotationStartedAt) * 1000))ms, offset \(elapsedMilliseconds)ms)"
        )
    }

    private func handleFinishedSegment(
        fileURL: URL,
        sequence: Int? = nil,
        successfully: Bool,
        isAutoRotated: Bool = false
    ) {
        guard let sessionID = currentSessionID else { return }
        let standardizedPath = fileURL.standardizedFileURL.path
        let expectedStagingDirectory = storageRoot
            .appendingPathComponent("staging", isDirectory: true)
            .appendingPathComponent(sessionID, isDirectory: true)
            .standardizedFileURL
        guard fileURL.deletingLastPathComponent().standardizedFileURL
            == expectedStagingDirectory else {
            return
        }
        if handledRecorderPaths.contains(standardizedPath) {
            return
        }
        handledRecorderPaths.insert(standardizedPath)

        let resolvedSequence = sequence ?? Self.sequence(fromStagedFile: fileURL) ?? 0
        let context = contextByFilePath.removeValue(forKey: standardizedPath) ?? SegmentContext(
            sequence: resolvedSequence,
            startOffsetMilliseconds: WatchSegmentTimingPolicy.startOffset(
                sequence: resolvedSequence,
                elapsedMilliseconds: savedMilliseconds
            ),
            batterySnapshot: lastCommittedBatterySnapshot
        )
        eventLog.record("segment \(context.sequence) finished ok=\(successfully) autoRotated=\(isAutoRotated)")

        if isAutoRotated && continuousCapture.audioEngineIsRunning {
            isAudioInterrupted = false
        }
        if currentRecorder?.isRecording != true && !continuousCapture.audioEngineIsRunning {
            currentRecorder = nil
        }
        let disposition = CaptureSegmentDisposition.resolve(
            stateIsRecording: state == .recording,
            stopRequested: stopRequested,
            audioInterrupted: isAudioInterrupted,
            segmentSucceeded: successfully
        )

        var continueCapture = false
        var shouldFinalize = false

        switch disposition {
        case .continueRecording:
            continueCapture = true
            shouldFinalize = false
        case .pauseForInterruption:
            continueCapture = false
            shouldFinalize = false
            statusMessage = "系统音频打断 · 等待恢复录音"
            scheduleCaptureRecovery(cause: .interruption, reason: "segment paused for interruption")
        case .finalizeSession:
            continueCapture = false
            shouldFinalize = true
            stopRequested = true
            state = .stopping
            statusMessage = "正在保存已录音频…"
        }

        if !continueCapture && !stopRequested && !isAudioInterrupted && !successfully && captureFailure == nil {
            captureFailure = "录音分段异常结束，正在保存可恢复音频。"
        }
        if continueCapture {
            if !isAutoRotated {
                do {
                    try startSegment(sequence: context.sequence + 1)
                } catch {
                    eventLog.record("rotation to segment \(context.sequence + 1) FAILED: \(error.localizedDescription)")
                    statusMessage = "下一段录音启动失败，正在重试…"
                    scheduleCaptureRecovery(cause: .rotationFailed, reason: "rotation after segment \(context.sequence)")
                }
            } else {
                let nextSeq = context.sequence + 1
                nextSegmentNumber = context.sequence + 2
                let nextURL = expectedStagingDirectory.appendingPathComponent(
                    String(format: "segment-%06d.m4a", nextSeq)
                )
                contextByFilePath[nextURL.standardizedFileURL.path] = SegmentContext(
                    sequence: nextSeq,
                    startOffsetMilliseconds: WatchSegmentTimingPolicy.startOffset(
                        sequence: nextSeq,
                        elapsedMilliseconds: savedMilliseconds + 60000
                    ),
                    batterySnapshot: lastCommittedBatterySnapshot
                )
                eventLog.record("auto-rotated to segment \(nextSeq) seamlessly in background")
            }
        }

        let previousFinalization = finalizationTask
        finalizationTask = Task { @MainActor in
            await previousFinalization?.value
            guard self.currentSessionID == sessionID else { return }
            var finalizeSession = shouldFinalize
            do {
                let indexingStartedAt = Date()
                let durationMilliseconds = try await Self.measureDurationMilliseconds(at: fileURL)
                let existingManifest = try? await ledger.loadSession(id: sessionID)
                let previousEnd = existingManifest?.segments.last.map {
                    $0.startOffsetMilliseconds + $0.durationMilliseconds
                } ?? 0
                let resolvedStartOffset = context.sequence == 0 ? 0 : max(context.startOffsetMilliseconds, previousEnd)
                _ = try await ledger.appendSegment(
                    sessionID: sessionID,
                    sequence: context.sequence,
                    sourceURL: fileURL,
                    startOffsetMilliseconds: resolvedStartOffset,
                    durationMilliseconds: durationMilliseconds,
                    batterySnapshot: context.batterySnapshot,
                    healthSnapshot: WatchHealthSnapshotManager.shared.captureSnapshot(),
                    location: WatchLocationSnapshotManager.shared.captureSnapshot(),
                    pins: consumePendingPins(sessionID: sessionID, offsetMilliseconds: resolvedStartOffset)
                )
                await refreshPendingQueue()
                guard self.currentSessionID == sessionID else {
                    try? FileManager.default.removeItem(at: fileURL)
                    return
                }
                let indexedSession = try? await ledger.loadSession(id: sessionID)
                let unacknowledgedCount = indexedSession?.segments.filter {
                    $0.uploadState != .acknowledged
                }.count ?? 0
                if WatchSegmentUploadPolicy.shouldUpload(
                    isRecording: state == .recording && !stopRequested && !isAudioInterrupted,
                    pendingSegmentCount: unacknowledgedCount
                ) {
                    eventLog.record(
                        "live upload batch triggered with \(unacknowledgedCount) sealed segment(s)"
                    )
                    // Transfer in a separate task. File indexing and the next segment's
                    // rotation must not wait for a slow Mac or Wi-Fi response.
                    Task { @MainActor [weak self] in
                        await self?.syncCoordinator.uploadSealedSegmentsDuringCapture(
                            sessionID: sessionID
                        )
                        await self?.refreshPendingQueue()
                    }
                }
                savedMilliseconds += durationMilliseconds
                lastSavedAt = .now
                syncRevision += 1
                try? FileManager.default.removeItem(at: fileURL)
                captureLog.notice(
                    "segment \(context.sequence, privacy: .public) indexed \(Int(Date().timeIntervalSince(indexingStartedAt) * 1000), privacy: .public)ms"
                )
            } catch {
                guard self.currentSessionID == sessionID else { return }
                let alreadyIndexed = (try? await ledger.loadSession(id: sessionID))?.segments
                    .contains { $0.sequence == context.sequence } ?? false
                if alreadyIndexed {
                    // 停止时轮转回调已经登记过这一段，音频没丢，不算失败。
                    eventLog.record("segment \(context.sequence) already indexed by rotation handler")
                } else if state != .recording {
                    eventLog.record("INDEX FAILED seg \(context.sequence): \(error.localizedDescription); retained in staging for recovery")
                    captureFailure = "无法登记录音分段：\(error.localizedDescription)"
                    statusMessage = captureFailure ?? "Segment could not be saved."
                    finalizeSession = true
                } else {
                    eventLog.record("INDEX FAILED seg \(context.sequence): \(error.localizedDescription); retained in staging for recovery")
                }
            }

            guard finalizeSession else { return }
            // 【2026-10-15 修】结束会话时只扫**本会话**的 staging，不再遍历全部历史会话。
            // 原实现每次结束都先 listSessions() 再逐会话扫 staging、对每个 staged 文件读音频
            // 测时长（measureDurationMilliseconds），复杂度随历史会话数增长 —— 用户报的
            // 「长时间录音后长按暂停，卡在"保存文件"很久」正是这个。历史会话的遗留 staging
            // 交给下次启动的 recoverStagedAudioIfNeeded() 兜底。
            let recoveryIssue = await recoverStagedAudio(targetSessionID: sessionID)
            guard self.currentSessionID == sessionID else { return }
            let completionDisposition = CaptureSessionCompletionDisposition.resolve(
                hasUnresolvedStagedAudio: hasStagedAudio(for: sessionID)
            )
            if completionDisposition == .finishManifest {
                do {
                    try await ledger.finishSession(id: sessionID)
                } catch {
                    guard self.currentSessionID == sessionID else { return }
                    captureFailure = "无法完成录音记录：\(error.localizedDescription)"
                }
            } else {
                captureFailure = appendFailure(
                    "暂存音频尚未完成索引，旧录音会话保持待恢复。",
                    to: captureFailure
                )
            }
            if hasStagedAudio(for: sessionID), let recoveryIssue {
                captureFailure = appendFailure(
                    "暂存音频问题：\(recoveryIssue.statusMessage)",
                    to: captureFailure
                )
            }
            guard self.currentSessionID == sessionID else { return }
            syncRevision += 1
            self.currentSessionID = nil
            startedAt = nil
            isAudioInterrupted = false
            contextByFilePath.removeAll()
            handledRecorderPaths.removeAll()
            continuousCapture.stopCapture()
            stopRuntimeSession()
            nowPlaying.end()
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            isCaptureSessionActive = false
            lastCommittedBatterySnapshot = nil
            WKInterfaceDevice.current().play(.stop)
            if let captureFailure {
                state = .failed
                statusMessage = captureFailure
            } else {
                state = .ready
                statusMessage = "录音已保存，已封段已同步或可稍后同步至 Mac。"
            }
        }
    }

    private var stagingRoot: URL {
        storageRoot.appendingPathComponent("staging", isDirectory: true)
    }

    /// Session-scoped capture setup that used to be redone on every 60 s rotation.
    ///
    /// With the Watch screen off, the rotation path was measured at ~3.6 s of blocked
    /// main thread on Series 5: audio-session activation, re-enabling battery monitoring
    /// (a slow WatchKit call), staging-directory I/O and recorder-buffer preparation.
    /// A block that long landing on a Digital Crown press stalls the scene snapshot
    /// watchOS hands to SpringBoard for the app-grid zoom animation, which is what
    /// leaves the grid frozen with touches ignored.
    private func prepareCaptureEnvironment() {
        let device = WKInterfaceDevice.current()
        if !device.isBatteryMonitoringEnabled {
            device.isBatteryMonitoringEnabled = true
        }
        try? FileManager.default.createDirectory(
            at: stagingRoot,
            withIntermediateDirectories: true
        )
    }

    /// `FinalizedAudio.durationMilliseconds` opens and parses the finished m4a. It used
    /// to run inline on the main actor once a minute — exactly the kind of block that
    /// can stall a crown-press transition.
    private static func measureDurationMilliseconds(at url: URL) async throws -> Int64 {
        try await Task.detached(priority: .utility) {
            try FinalizedAudio.durationMilliseconds(at: url)
        }.value
    }

    private static func requestMicrophonePermission() async -> Bool {
        await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }
}

private enum WatchRecorderError: LocalizedError {
    case couldNotStartSegment
    case invalidStagedAudio

    var errorDescription: String? {
        switch self {
        case .couldNotStartSegment:
            "The Watch audio recorder could not start the next segment."
        case .invalidStagedAudio:
            "The staged Watch audio contains no readable frames."
        }
    }
}

// MARK: - WKExtendedRuntimeSessionDelegate

extension WatchRecordingCoordinator: WKExtendedRuntimeSessionDelegate {
    nonisolated func extendedRuntimeSessionDidStart(_ extendedRuntimeSession: WKExtendedRuntimeSession) {
        let expireDesc = extendedRuntimeSession.expirationDate.map {
            String(format: "%.0fs", $0.timeIntervalSinceNow)
        } ?? "unknown"
        WatchCaptureEventLog.shared.record("physical-therapy session started (expires in \(expireDesc))")
    }

    nonisolated func extendedRuntimeSessionWillExpire(_ extendedRuntimeSession: WKExtendedRuntimeSession) {
        WatchCaptureEventLog.shared.record("physical-therapy session will expire; refreshing")
        Task { @MainActor [weak self] in
            guard let self, self.isRecording, !self.stopRequested else { return }
            self.stopRuntimeSession()
            self.startRuntimeSession()
        }
    }

    nonisolated func extendedRuntimeSession(
        _ extendedRuntimeSession: WKExtendedRuntimeSession,
        didInvalidateWith reason: WKExtendedRuntimeSessionInvalidationReason,
        error: (any Error)?
    ) {
        let errorDesc = error?.localizedDescription ?? "none"
        let reasonName: String
        switch reason {
        case .none: reasonName = "none"
        case .sessionInProgress: reasonName = "sessionInProgress"
        case .expired: reasonName = "expired"
        case .resignedFrontmost: reasonName = "resignedFrontmost"
        case .suppressedBySystem: reasonName = "suppressedBySystem"
        case .error: reasonName = "error"
        @unknown default: reasonName = "unknown(\(reason.rawValue))"
        }
        WatchCaptureEventLog.shared.record("physical-therapy session invalidated (\(reasonName)) err=\(errorDesc)")
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.runtimeSession = nil
            let isForeground = WKApplication.shared().applicationState == .active
            if self.isRecording && !self.stopRequested && reason == .expired && isForeground {
                self.eventLog.record("restarting physical-therapy session after expiration in foreground")
                self.startRuntimeSession()
            }
        }
    }
}


