import Combine
import Foundation
import os
#if os(watchOS)
import WatchKit
#endif

private let batteryLog = Logger(subsystem: "com.lifelogger.watch", category: "battery")
private let syncLog = Logger(subsystem: "com.lifelogger.watch", category: "sync")

struct WatchSyncConfiguration: Equatable, Sendable {
    let baseURL: URL
    let deviceID: String
}

enum WatchMacConnectionIndicator: Equatable {
    case unchecked
    case checking
    case reachable
    case unreachable
    case attention
    case notConfigured
}

enum WatchSyncConfigurationStore {
    private static let baseURLKey = "lifeLogger.watch.macBaseURL"
    private static let deviceIDKey = "lifeLogger.watch.deviceID"
    static let candidateBaseURLStrings = [
        "http://lwkdeMacBook-Pro-1020.local:8765",
        "http://192.168.1.100:8765",
        "http://172.20.10.3:8765",
        "http://172.20.10.1:8765",
        "http://172.20.10.2:8765",
        "http://172.20.10.4:8765",
        "http://192.168.1.100:8765",
        "http://192.168.0.100:8765",
        "http://192.168.31.100:8765"
    ]
    static let defaultBaseURLString = "http://lwkdeMacBook-Pro-1020.local:8765"

    static func load() -> WatchSyncConfiguration? {
        let storedURL = UserDefaults.standard.string(forKey: baseURLKey)
        guard let defaultURL = WatchMacEndpoint.validatedBaseURL(defaultBaseURLString) else {
            return nil
        }
        let url = storedURL.flatMap(WatchMacEndpoint.validatedBaseURL) ?? defaultURL
        if storedURL != url.absoluteString {
            UserDefaults.standard.set(url.absoluteString, forKey: baseURLKey)
        }
        return WatchSyncConfiguration(
            baseURL: url,
            deviceID: loadOrCreateDeviceID()
        )
    }

    static func updateWorkingBaseURL(_ url: URL) {
        UserDefaults.standard.set(url.absoluteString, forKey: baseURLKey)
    }

    static func testEndpoint(_ url: URL) async -> Bool {
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        components?.path = "/api/watch-health"
        guard let healthURL = components?.url else { return false }
        var request = URLRequest(url: healthURL)
        request.httpMethod = "GET"
        request.timeoutInterval = 2.5
        // 探活不再把密钥发出去：发一个随机数，Mac 用密钥算 HMAC 回来。答不对的主机不会被选中。
        let challenge = WatchMacChallenge.makeChallenge()
        request.setValue(challenge, forHTTPHeaderField: WatchMacChallenge.challengeHeader)

        let config = URLSessionConfiguration.ephemeral
        config.waitsForConnectivity = false
        config.timeoutIntervalForRequest = 2.5
        config.timeoutIntervalForResource = 3.0
        let session = URLSession(configuration: config)
        defer { session.finishTasksAndInvalidate() }

        do {
            let (data, response) = try await session.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                return false
            }
            if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let ok = json["ok"] as? Bool, ok,
               let enabled = json["watch_upload_enabled"] as? Bool, enabled,
               let proof = json["watch_challenge_response"] as? String,
               proof == WatchMacChallenge.response(challenge: challenge, key: WatchUploadCredential.headerValue()) {
                return true
            }
            return false
        } catch {
            return false
        }
    }

    static func resolveWorkingConfiguration() async -> WatchSyncConfiguration? {
        let current = load()
        var allURLs: [URL] = []
        if let currentURL = current?.baseURL {
            allURLs.append(currentURL)
        }
        for str in candidateBaseURLStrings {
            if let valid = WatchMacEndpoint.validatedBaseURL(str), !allURLs.contains(valid) {
                allURLs.append(valid)
            }
        }

        let workingURL = await withTaskGroup(of: URL?.self, returning: URL?.self) { group in
            for target in allURLs {
                group.addTask {
                    if await testEndpoint(target) {
                        return target
                    }
                    return nil
                }
            }
            while let result = await group.next() {
                if let validURL = result {
                    group.cancelAll()
                    return validURL
                }
            }
            return nil
        }

        if let validURL = workingURL {
            if validURL != current?.baseURL {
                updateWorkingBaseURL(validURL)
                syncLog.notice("resolved working Mac URL: \(validURL.absoluteString, privacy: .public)")
            }
            return WatchSyncConfiguration(baseURL: validURL, deviceID: loadOrCreateDeviceID())
        }

        // 【2026-10-15 新增】上面那批地址全不通时，用 Bonjour 问一次「局域网里谁在跑 Life Logger」。
        // 为什么需要：候选列表是硬编码的（192.168.1.100 / 172.20.10.* / 192.168.1.100 …），
        // 而热点重启、换路由器、换网段都会让 Mac 的 IP 变（实测从 .114 变成 .50），
        // 于是「明明同一个 WiFi 却连不上」——用户 2026-10-15 报的就是这个。
        // 放在**最后**而不是最前：正常情况（上次地址还有效）零额外延迟；
        // 只有真的找不到时才花最多 2 秒广播询问一次。
        if let discovered = await WatchMacDiscovery.discoverMacBaseURL(),
           !allURLs.contains(discovered),
           await testEndpoint(discovered) {
            updateWorkingBaseURL(discovered)
            syncLog.notice("discovered Mac via Bonjour: \(discovered.absoluteString, privacy: .public)")
            return WatchSyncConfiguration(baseURL: discovered, deviceID: loadOrCreateDeviceID())
        }

        return current
    }

    private static func loadOrCreateDeviceID() -> String {
        if let existing = UserDefaults.standard.string(forKey: deviceIDKey) {
            return existing
        }
        let deviceID = "watch-\(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased())"
        UserDefaults.standard.set(deviceID, forKey: deviceIDKey)
        return deviceID
    }

}

public struct WatchSyncProgress: Equatable, Sendable {
    public let completed: Int
    public let total: Int
    public var fraction: Double {
        guard total > 0 else { return 0 }
        return min(1.0, max(0.0, Double(completed) / Double(total)))
    }

    public init(completed: Int, total: Int) {
        self.completed = completed
        self.total = total
    }
}

/// Actor that performs live sealed segment uploading in background without touching MainActor.
actor WatchLiveUploadWorker {
    private let ledger: SegmentLedger
    private let eventLog: WatchCaptureEventLog
    private var captureConfiguration: WatchSyncConfiguration?
    private var captureTransport: (any WatchSessionTransport)?
    private var backoff = WatchUploadBackoff()
    private var backoffSessionID: String?
    private var isUploading = false
    private let transportFactory: @Sendable (WatchSyncConfiguration) -> any WatchSessionTransport

    init(
        ledger: SegmentLedger,
        eventLog: WatchCaptureEventLog = .shared,
        transportFactory: @escaping @Sendable (WatchSyncConfiguration) -> any WatchSessionTransport = {
            MacWatchSessionTransport(configuration: $0, allowsCaptureUploads: true)
        }
    ) {
        self.ledger = ledger
        self.eventLog = eventLog
        self.transportFactory = transportFactory
    }

    func uploadSealedSegments(
        sessionID: String,
        userInitiated: Bool = false,
        onProgress: (@Sendable (Int, Int) -> Void)? = nil
    ) async -> Result<WatchSyncReport, Error> {
        guard !isUploading else {
            return .failure(WatchSyncError.sessionRegistrationFailed("正在上传中，请稍候"))
        }
        guard WatchCaptureNetworkGate.shared.isBlocked else {
            return .failure(CancellationError())
        }
        guard var configuration = WatchSyncConfigurationStore.load() else {
            return .failure(WatchSyncError.sessionRegistrationFailed("Mac 地址未配置"))
        }

        // 退避只限制自动上传；用户点上传总要真的试一次。换了录音会话也重新计算。
        if backoffSessionID != sessionID {
            backoffSessionID = sessionID
            backoff.reset()
        }
        guard userInitiated || backoff.shouldAttempt(at: .now) else {
            return .success(WatchSyncReport())
        }

        isUploading = true
        defer { isUploading = false }

        // 地址可能在录音途中失效（换热点、换路由器）。连续失败后限频重新找一次。
        if backoff.shouldRediscover(at: .now) {
            backoff.recordRediscovery(at: .now)
            if let resolved = await WatchSyncConfigurationStore.resolveWorkingConfiguration(), resolved != configuration {
                eventLog.record("capture upload: Mac address changed")
                configuration = resolved
            }
        }

        let transport: any WatchSessionTransport
        if captureConfiguration == configuration, let captureTransport {
            transport = captureTransport
        } else {
            transport = transportFactory(configuration)
            captureConfiguration = configuration
            captureTransport = transport
        }

        do {
            let engine = WatchSyncEngine(ledger: ledger, transport: transport)
            let report = try await engine.syncSealedSegments(sessionID: sessionID, onProgress: onProgress)
            eventLog.record("sealed outbox upload finished ack=\(report.segmentsAcknowledged) failures=\(report.failures.count)")
            if let failure = report.failures.first {
                backoff.recordFailure(at: .now)
                eventLog.record("live upload deferred for \(failure.sessionID.prefix(8)): \(String(describing: failure.error))")
                return .failure(failure.error)
            }
            backoff.recordSuccess()
            if report.segmentsAcknowledged > 0 {
                eventLog.record("live upload acknowledged \(report.segmentsAcknowledged) sealed segment(s)")
            }
            return .success(report)
        } catch {
            backoff.recordFailure(at: .now)
            eventLog.record("live upload deferred: \(error.localizedDescription)")
            return .failure(error)
        }
    }
}

/// App-level connection checks are separate from the core segment transport contract.
protocol WatchConnectionTransport: WatchSessionTransport {
    func checkConnection() async throws
    func takeBackgroundIssue() async -> String?
}

@MainActor
final class WatchSyncCoordinator: ObservableObject {
    @Published private(set) var isSyncing = false
    @Published private(set) var statusMessage = "正在检查 Mac 连接…"
    @Published private(set) var macConnectionIndicator: WatchMacConnectionIndicator = .unchecked
    @Published private(set) var syncProgress: WatchSyncProgress?
    private(set) var lastMacJobID: String?

    /// Called when full sync starts (true) or finishes/aborts (false), allowing
    /// coordinator to keep extended runtime session alive during active sync.
    var onSyncLifecycleChanged: ((_ isActive: Bool) -> Void)?

    private let ledger: SegmentLedger

    /// Sessions whose ledger still lists segments that are no longer on disk.
    ///
    /// Such a session can never upload as-is: every sync tries to send a missing file and
    /// fails locally, which is how nine sessions ended up retrying on every attempt. Detecting
    /// them lets the screen say something true instead of "waiting to sync".
    @Published private(set) var sessionsWithMissingSegments: [String] = []

    func refreshMissingSegmentSessions() {
        Task { @MainActor [weak self] in
            guard let self else { return }
            let sessions = (try? await self.ledger.listSessions()) ?? []
            var stuck: [String] = []
            for session in sessions where !session.segments.isEmpty {
                // If the session has already been completed on Mac, or all segments are acknowledged,
                // the local audio files were intentionally removed after successful upload to save space.
                // It is NOT a missing segment failure!
                guard session.macJobID == nil else { continue }
                let pendingSegments = session.segments.filter { $0.uploadState != .acknowledged }
                guard !pendingSegments.isEmpty else { continue }

                let rows = pendingSegments.map { segment -> (sequence: Int, fileExists: Bool) in
                    (segment.sequence, FileManager.default.fileExists(atPath: segment.localURL.path))
                }
                if WatchSegmentRecoveryPolicy.needsUserDecision(
                    WatchSegmentRecoveryPolicy.verdict(segments: rows)
                ) {
                    stuck.append(session.id)
                }
            }
            self.sessionsWithMissingSegments = stuck
            syncLog.notice("sessions with missing local segments: \(stuck.count, privacy: .public)")
        }
    }
    private let liveUploadWorker: WatchLiveUploadWorker
    private let transportFactory: @Sendable (WatchSyncConfiguration) -> any WatchConnectionTransport
    private var activeConfiguration: WatchSyncConfiguration?
    private var activeTransport: (any WatchConnectionTransport)?
    private var captureConfiguration: WatchSyncConfiguration?
    private var captureTransport: MacWatchSessionTransport?
    private var pendingLiveUploadSessionID: String?
    private var syncFollowUpPolicy = WatchSyncFollowUpPolicy()
    private let eventLog: WatchCaptureEventLog

    init(
        ledger: SegmentLedger,
        eventLog: WatchCaptureEventLog = .shared,
        liveUploadWorker: WatchLiveUploadWorker? = nil,
        transportFactory: @escaping @Sendable (WatchSyncConfiguration) -> any WatchConnectionTransport = {
            MacWatchSessionTransport(configuration: $0)
        }
    ) {
        self.ledger = ledger
        self.eventLog = eventLog
        self.transportFactory = transportFactory
        self.liveUploadWorker = liveUploadWorker ?? WatchLiveUploadWorker(ledger: ledger, eventLog: eventLog)
    }

    func syncPending(recordingSessionID: String? = nil) async {
        if WatchCaptureNetworkGate.shared.isBlocked {
            let sessions = try? await ledger.listSessions()
            guard let sessionID = recordingSessionID ?? sessions?.first(where: { $0.stoppedAt == nil })?.id else {
                statusMessage = "正在准备录音，已保存分段将在录音开始后继续上传"
                return
            }
            await uploadSealedSegmentsDuringCapture(sessionID: sessionID, userInitiated: true)
            return
        }
        guard !syncFollowUpPolicy.shouldDeferFullSync(
            isSyncing: isSyncing,
            captureBlocked: WatchCaptureNetworkGate.shared.isBlocked
        ) else {
            return
        }
        guard let configuration = WatchSyncConfigurationStore.load() else {
            macConnectionIndicator = .notConfigured
            statusMessage = "Mac 地址无效，录音仍保存在手表。"
            return
        }

        isSyncing = true
        onSyncLifecycleChanged?(true)
        syncProgress = nil
        macConnectionIndicator = .checking
        statusMessage = "正在检查 Mac 连接…"
        defer {
            isSyncing = false
            syncProgress = nil
            onSyncLifecycleChanged?(false)
            schedulePendingSyncIfReady()
        }

        var currentConfig = configuration
        var transport: any WatchConnectionTransport
        if activeConfiguration == currentConfig, let activeTransport {
            transport = activeTransport
        } else {
            transport = transportFactory(currentConfig)
            activeConfiguration = currentConfig
            activeTransport = transport
        }

        do {
            try await transport.checkConnection()
        } catch {
            // Capture entry intentionally cancels ordinary requests. Do not perform
            // discovery or report a Mac outage for that local hand-off; defer replays
            // any queued capture request after this pass releases isSyncing.
            if WatchCaptureNetworkGate.shared.isBlocked {
                eventLog.record("ordinary sync interrupted by capture; handing off sealed outbox")
                return
            }
            if let resolved = await WatchSyncConfigurationStore.resolveWorkingConfiguration(), resolved != currentConfig {
                currentConfig = resolved
                transport = transportFactory(resolved)
                activeConfiguration = resolved
                activeTransport = transport
                do {
                    try await transport.checkConnection()
                } catch let retryError {
                    macConnectionIndicator = connectionIndicator(for: retryError)
                    statusMessage = connectionFailureMessage(for: retryError)
                    return
                }
            } else {
                macConnectionIndicator = connectionIndicator(for: error)
                statusMessage = connectionFailureMessage(for: error)
                return
            }
        }

        macConnectionIndicator = .reachable
        refreshMissingSegmentSessions()
        statusMessage = "已找到 Mac，正在同步手表录音…"
        do {
            let engine = WatchSyncEngine(ledger: ledger, transport: transport)
            let report = try await engine.syncPending { [weak self] completed, total in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    self.syncProgress = WatchSyncProgress(completed: completed, total: total)
                    let pct = total > 0 ? Int((Double(completed) / Double(total) * 100).rounded()) : 0
                    self.statusMessage = "正在上传：\(completed)/\(total) 段 (\(pct)%)"
                }
            }
            let sessions = try await ledger.listSessions()
            let pending = sessions
                .flatMap(\.segments)
                .filter { $0.uploadState != .acknowledged }
                .count
            if report.sessionsCompleted > 0 {
                lastMacJobID = sessions.first(where: { $0.macJobID != nil })?.macJobID
            }
            if let firstFailure = report.failures.first {
                // The drain keeps going past a failed session, so the queue is never held
                // hostage; the reason still has to reach the screen, with the session named
                // when more than one recording is affected.
                let prefix = report.failures.count > 1
                    ? "\(report.failures.count) 个录音会话未同步"
                    : "有录音会话未同步"
                statusMessage = "\(prefix)："
                    + WatchSyncStatusPolicy.failureMessage(for: firstFailure.error)
                syncLog.error(
                    "sync finished with \(report.failures.count, privacy: .public) failed session(s); first=\(firstFailure.sessionID, privacy: .public) \(String(describing: firstFailure.error), privacy: .public)"
                )
            } else if !sessionsWithMissingSegments.isEmpty {
                // These cannot be uploaded as they are; "waiting to sync" would be a lie and
                // hides the only action that helps (clean up or abandon them).
                statusMessage = "\(sessionsWithMissingSegments.count) 个录音会话的片段已在手表上丢失，无法上传；可在上传队列查看"
            } else if let backgroundIssue = await transport.takeBackgroundIssue() {
                // A completion finished in the background and was refused; that reason is the
                // only thing that explains why this session keeps coming back.
                statusMessage = "后台同步未完成：\(backgroundIssue)"
            } else {
                statusMessage = WatchSyncStatusPolicy.queueStatus(pendingSegments: pending)
            }
        } catch let error as WatchSyncError {
            // A drain only throws for a ledger-level problem; keep the Mac's own words on
            // screen, since a generic "sync failed" hides the reason.
            statusMessage = WatchSyncStatusPolicy.failureMessage(for: error)
            syncLog.error("sync failed: \(String(describing: error), privacy: .public)")
        } catch {
            statusMessage = "同步未完成（\(String(describing: error))）；音频仍保存在手表，等待 Mac 接收。"
            syncLog.error("sync failed: \(String(describing: error), privacy: .public)")
        }
    }

    /// Sends only sealed segments while a session is recording.
    func uploadSealedSegmentsDuringCapture(sessionID: String, userInitiated: Bool = false) async {
        guard WatchCaptureNetworkGate.shared.isBlocked else { return }
        eventLog.record("capture upload requested session=\(sessionID.prefix(8)) busy=\(isSyncing)")
        if syncFollowUpPolicy.shouldDeferLiveUpload(isSyncing: isSyncing) {
            // A tap while the automatic rotation upload is active is a valid request, not
            // a registration failure. Remember it and drain the same session once the
            // current request finishes; the Watch keeps recording throughout.
            pendingLiveUploadSessionID = sessionID
            eventLog.record("capture upload queued behind existing sync session=\(sessionID.prefix(8))")
            statusMessage = "当前分段正在上传，已排队，完成后继续…"
            return
        }
        guard !isSyncing else { return }

        isSyncing = true
        syncProgress = nil
        statusMessage = "正在上传已录分段…"
        defer {
            isSyncing = false
            syncProgress = nil
            schedulePendingSyncIfReady()
        }

        let result = await liveUploadWorker.uploadSealedSegments(sessionID: sessionID, userInitiated: userInitiated) { [weak self] completed, total in
            Task { @MainActor [weak self] in
                self?.syncProgress = WatchSyncProgress(completed: completed, total: total)
            }
        }

        switch result {
        case .success(let report):
            if report.segmentsAcknowledged > 0 {
                macConnectionIndicator = .reachable
                statusMessage = "录音继续 · 本轮已上传 \(report.segmentsAcknowledged) 段"
            } else {
                statusMessage = "录音继续 · 已保存分段暂无待上传，当前音频仍在录制"
            }
        case .failure(let error):
            macConnectionIndicator = .unreachable
            statusMessage = "未能连上 Mac（\(error.localizedDescription)）；已在手表保存，可点击重试"
        }
    }

    func handleLiveUploadBackgroundReport(result: Result<WatchSyncReport, Error>, pendingCount: Int) {
        switch result {
        case .success(let report):
            if report.segmentsAcknowledged > 0 {
                macConnectionIndicator = .reachable
                statusMessage = "边录边传中 · 已同步 \(report.segmentsAcknowledged) 段"
            }
        case .failure:
            macConnectionIndicator = .unreachable
            if pendingCount > 0 {
                statusMessage = "未连接到 Mac；已在手表保存 \(pendingCount) 段，可点击重试"
            }
        }
    }

    /// Replay requests after either ordinary sync or live sync releases the coordinator.
    /// Starting recording cancels ordinary requests, so a live tap during that hand-off
    /// must not wait for a live pass that has never started.
    private func schedulePendingSyncIfReady() {
        if WatchCaptureNetworkGate.shared.isBlocked {
            if syncFollowUpPolicy.consumeLiveUploadRequest(), let sessionID = pendingLiveUploadSessionID {
                pendingLiveUploadSessionID = nil
                Task { @MainActor [weak self] in
                    await self?.uploadSealedSegmentsDuringCapture(sessionID: sessionID)
                }
            }
            return
        }
        _ = syncFollowUpPolicy.consumeLiveUploadRequest()
        pendingLiveUploadSessionID = nil
        guard syncFollowUpPolicy.liveUploadDidFinish(
            captureBlocked: WatchCaptureNetworkGate.shared.isBlocked
        ) else { return }
        Task { @MainActor [weak self] in
            await self?.syncPending()
        }
    }

    private func connectionIndicator(for error: Error) -> WatchMacConnectionIndicator {
        if error is URLError || (error as NSError).domain == NSURLErrorDomain {
            return .unreachable
        }
        return .attention
    }

    private func connectionFailureMessage(for error: Error) -> String {
        guard let transportError = error as? WatchSyncTransportError else {
            let failure = WatchSyncStatusPolicy.classifyTransportError(error)
            return WatchSyncStatusPolicy.connectionFailureMessage(for: failure)
        }

        let failure: WatchConnectionFailure
        switch transportError {
        case .watchDirectAccessDisabled:
            failure = .directAccessDisabled
        case .httpStatus(401, _):
            failure = .unauthorized
        case .httpStatus(let statusCode, let detail):
            failure = .serverRejected(statusCode: statusCode, detail: detail)
        case .invalidResponse, .invalidAcknowledgement:
            failure = .invalidResponse
        case .localSegmentMissing, .sessionNotFinished:
            failure = .unreachable
        }
        return WatchSyncStatusPolicy.connectionFailureMessage(for: failure)
    }
}

actor MacWatchSessionTransport: WatchConnectionTransport {
    private let configuration: WatchSyncConfiguration
    private let session: URLSession
    /// Only the sealed-segment capture path opts into network activity while recording.
    private let allowsCaptureUploads: Bool
    /// Completion uses a small foreground request queue and retries from the durable ledger.
    private var completionQueue: WatchCompletionQueue?
    /// Keep status snapshots ordered while leaving their delivery off the sync path.
    private var statusReportTask: Task<Void, Never>?
    /// Shared with the queue so a background refusal can be read back by the recorder screen.
    private var backgroundDelivery: WatchBackgroundDelivery?

    /// The last problem a background completion hit, if any (one-shot).
    func takeBackgroundIssue() async -> String? {
        await backgroundDelivery?.takeLatestIssue()
    }

    init(configuration: WatchSyncConfiguration, allowsCaptureUploads: Bool = false) {
        self.configuration = configuration
        self.allowsCaptureUploads = allowsCaptureUploads
        // The MVP needs the Mac's response body immediately so it can validate
        // the per-segment ACK before deleting the local copy. A true watchOS
        // background transfer needs a delegate plus WKURLSessionRefreshBackgroundTask
        // handling; until that lifecycle is implemented, keep this request-bound
        // transport honest and rely on the durable ledger for retry after resume.
        let sessionConfiguration = URLSessionConfiguration.default
        if allowsCaptureUploads {
            sessionConfiguration.waitsForConnectivity = false
            // 265 KB audio segment over Bluetooth proxy takes 6-9s under normal radio conditions,
            // and up to 15-20s under congestion. 45s avoids truncation while fitting the 60s rotation window.
            sessionConfiguration.timeoutIntervalForRequest = 45
            sessionConfiguration.timeoutIntervalForResource = 90
        } else {
            sessionConfiguration.waitsForConnectivity = true
            sessionConfiguration.timeoutIntervalForRequest = 45
            sessionConfiguration.timeoutIntervalForResource = 180
        }
        session = URLSession(configuration: sessionConfiguration)
    }

    func checkConnection() async throws {
        guard !allowsCaptureUploads else {
            throw CancellationError()
        }
        let request = try makeRequest(
            path: ["api", "watch-health"],
            method: "GET",
            contentType: "application/json"
        )
        let probeConfiguration = URLSessionConfiguration.ephemeral
        probeConfiguration.waitsForConnectivity = false
        probeConfiguration.timeoutIntervalForRequest = 10
        probeConfiguration.timeoutIntervalForResource = 15
        let probeSession = URLSession(configuration: probeConfiguration)
        defer { probeSession.finishTasksAndInvalidate() }

        let (data, response) = try await WatchCaptureNetworkGate.shared.perform(session: probeSession, request: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw WatchSyncTransportError.invalidResponse
        }
        switch WatchHealthResponsePolicy.classify(
            statusCode: httpResponse.statusCode,
            body: data
        ) {
        case .ready:
            return
        case .directAccessDisabled:
            throw WatchSyncTransportError.watchDirectAccessDisabled
        case .unauthorized:
            throw WatchSyncTransportError.httpStatus(401, "Watch health check is unauthorized")
        case .serverRejected(let statusCode):
            throw WatchSyncTransportError.httpStatus(statusCode, "Mac rejected the health check")
        case .invalidResponse:
            throw WatchSyncTransportError.invalidResponse
        }
    }

    func reportOutboxStatus(_ status: WatchOutboxStatus) async {
        let body: [String: Any] = [
            "device_id": configuration.deviceID,
            "reported_at": Self.iso8601(Date()),
            "pending_segment_count": status.pendingSegmentCount,
            "pending_segment_bytes": status.pendingSegmentBytes,
            "open_session_count": status.openSessionCount,
            "pending_completion_count": status.pendingCompletionCount,
            "sync_state": status.syncState,
        ]

        do {
            let data = try JSONSerialization.data(withJSONObject: body)
            var request = try makeRequest(
                path: ["api", "watch-status"],
                method: "POST",
                contentType: "application/json"
            )
            request.httpBody = data
            let session = self.session
            let allowsCaptureUploads = self.allowsCaptureUploads
            let previousReport = statusReportTask
            // Queue the telemetry request without awaiting its network response, so an
            // unavailable status endpoint cannot delay segment transfer or capture.
            statusReportTask = Task {
                await previousReport?.value
                do {
                    let (_, response) = try await WatchCaptureNetworkGate.shared.perform(
                        session: session,
                        request: request,
                        allowDuringCapture: allowsCaptureUploads
                    )
                    guard let httpResponse = response as? HTTPURLResponse,
                          (200..<300).contains(httpResponse.statusCode) else {
                        let code = (response as? HTTPURLResponse)?.statusCode ?? -1
                        syncLog.error(
                            "Watch queue status report rejected: HTTP \(code, privacy: .public)"
                        )
                        return
                    }
                } catch {
                    syncLog.error(
                        "Watch queue status report failed: \(String(describing: error), privacy: .public)"
                    )
                }
            }
        } catch {
            syncLog.error(
                "Watch queue status report could not be prepared: \(String(describing: error), privacy: .public)"
            )
        }
    }

    func register(session manifest: RecordingSessionManifest) async throws {
        let body: [String: Any] = [
            "device_id": configuration.deviceID,
            "session_id": manifest.id,
            "started_at": Self.iso8601(manifest.startedAt),
            "sample_rate": 16_000,
            "channels": 1,
            "codec": "m4a-aac",
        ]
        let dataBody = try JSONSerialization.data(withJSONObject: body)
        let request = try makeRequest(
            path: ["api", "watch-sessions"],
            method: "POST",
            contentType: "application/json"
        )
        let bodyFile = try bodyDataFile(data: dataBody)
        defer { try? FileManager.default.removeItem(at: bodyFile) }
        let (data, response) = try await WatchCaptureNetworkGate.shared.perform(
            session: session,
            request: request,
            fileURL: bodyFile,
            allowDuringCapture: allowsCaptureUploads
        )
        _ = try validate(response: response, body: data)
    }

    func upload(segment: AudioSegment) async throws -> WatchSegmentAcknowledgement {
        guard FileManager.default.fileExists(atPath: segment.localURL.path) else {
            throw WatchSyncTransportError.localSegmentMissing
        }
        var durationMs = segment.durationMilliseconds
        // 【2026-10-15 修】原实现把超长分段一律压成 60_000ms，但 Mac 端契约是
        // `0 < duration_ms <= 61_000`（src/life_logger/watch_stream.py:465）——
        // 手表端比 Mac 端更严、白改数字：ledger 存的是实测时长，上报的却是 60_000，
        // Mac 侧按上报值拼时间轴 → 系统性偏移。现在只在真的超出 Mac 契约时才钳制。
        if durationMs > 61_000 {
            syncLog.error(
                "segment seq \(segment.sequence, privacy: .public) duration \(durationMs, privacy: .public)ms exceeds Mac contract (61000ms); clamped"
            )
            durationMs = 61_000
        } else if durationMs <= 0 {
            syncLog.error(
                "segment seq \(segment.sequence, privacy: .public) has non-positive duration \(durationMs, privacy: .public)ms; Mac would reject it"
            )
        }
        var headers = [
            "X-LifeLogger-Checksum": segment.sha256,
            "X-LifeLogger-Start-Offset-Ms": String(max(0, segment.startOffsetMilliseconds)),
            "X-LifeLogger-Duration-Ms": String(durationMs),
        ]
        if let bitRate = segment.audioBitrateHeaderValue {
            headers["X-LifeLogger-Audio-Bitrate"] = bitRate
        }
        if let pins = segment.pins, !pins.isEmpty {
            if let pinsData = try? JSONEncoder().encode(pins),
               let pinsJSON = String(data: pinsData, encoding: .utf8) {
                headers["X-LifeLogger-Pins"] = pinsJSON
            }
            if let firstPin = pins.first {
                headers["X-LifeLogger-Pin-Type"] = firstPin.type
            }
        }
        if let snapshot = segment.batterySnapshot {
            if let levelPercent = snapshot.levelPercent {
                headers["X-LifeLogger-Battery-Level-Percent"] = String(levelPercent)
            }
            if snapshot.state != .unknown {
                headers["X-LifeLogger-Battery-State"] = snapshot.state.rawValue
            }
            batteryLog.notice(
                "upload seq \(segment.sequence, privacy: .public) battery level=\(snapshot.levelPercent.map(String.init) ?? "none", privacy: .public) state=\(snapshot.state.rawValue, privacy: .public)"
            )
        } else {
            // No snapshot at all means the Mac will persist this segment without battery
            // telemetry. Log it so the gap is visible on-device instead of silent.
            batteryLog.error("upload seq \(segment.sequence, privacy: .public) battery snapshot missing")
        }
        if let health = segment.healthSnapshot {
            if let hr = health.heartRateBPM {
                headers["X-LifeLogger-Heart-Rate"] = String(format: "%.1f", hr)
            }
            if let steps = health.stepCount {
                headers["X-LifeLogger-Step-Count"] = String(steps)
            }
            // 【V68】当日累计步数 + 它所属的自然日（手表本地时区）+ 采样时刻。
            // 旧头 `X-LifeLogger-Step-Count` 仍然发，老服务端照旧能读；新服务端优先用这三个。
            if let stepsToday = health.stepsToday {
                headers["X-LifeLogger-Steps-Today"] = String(stepsToday)
            }
            if let stepsDate = health.stepsDate {
                headers["X-LifeLogger-Steps-Date"] = stepsDate
            }
            if let sampledAt = health.stepsSampledAt {
                headers["X-LifeLogger-Steps-Sampled-At"] = sampledAt
            }
            if let sleepHeader = health.sleep?.headerValue() {
                headers["X-LifeLogger-Sleep"] = sleepHeader
            }
        }
        if let location = segment.location {
            headers["X-LifeLogger-Location-Latitude"] = String(location.latitude)
            headers["X-LifeLogger-Location-Longitude"] = String(location.longitude)
            headers["X-LifeLogger-Location-Accuracy"] = String(location.horizontalAccuracy)
            headers["X-LifeLogger-Location-Timestamp"] = location.timestamp
            headers["X-LifeLogger-Location-Source"] = location.source
        }
        let request = try makeRequest(
            path: [
                "api",
                "watch-sessions",
                configuration.deviceID,
                segment.sessionID,
                "segments",
                String(segment.sequence),
            ],
            method: "PUT",
            contentType: "audio/mp4",
            headers: headers
        )
        let (data, response) = try await WatchCaptureNetworkGate.shared.perform(
            session: session,
            request: request,
            fileURL: segment.localURL,
            allowDuringCapture: allowsCaptureUploads
        )
        let payload = try validate(response: response, body: data)
        if let ackDict = payload["ack"] as? [String: Any],
           let haptic = ackDict["haptic"] as? String {
            #if os(watchOS)
            DispatchQueue.main.async {
                switch haptic {
                case "success":
                    WKInterfaceDevice.current().play(.success)
                case "directionUp":
                    WKInterfaceDevice.current().play(.directionUp)
                case "notification":
                    WKInterfaceDevice.current().play(.notification)
                case "retry":
                    WKInterfaceDevice.current().play(.retry)
                case "click":
                    WKInterfaceDevice.current().play(.click)
                default:
                    break
                }
            }
            #endif
        }
        guard let ack = payload["ack"] as? [String: Any],
              let sessionID = ack["session_id"] as? String,
              let sequence = ack["sequence"] as? Int,
              let sha256 = ack["sha256"] as? String else {
            throw WatchSyncTransportError.invalidAcknowledgement
        }
        return WatchSegmentAcknowledgement(
            sessionID: sessionID,
            sequence: sequence,
            sha256: sha256
        )
    }

    func complete(session manifest: RecordingSessionManifest) async throws -> String {
        guard !allowsCaptureUploads else {
            throw WatchSyncTransportError.sessionNotFinished
        }
        guard manifest.stoppedAt != nil else {
            throw WatchSyncTransportError.sessionNotFinished
        }
        try WatchCaptureNetworkGate.shared.check()
        // A foreground completion queue records the result asynchronously. If watchOS
        // suspends the process, the next idle foreground sync retries from the ledger.
        let queue = try sharedCompletionQueue()
        try queue.enqueue(session: manifest)
        throw WatchSyncTransportOutcome.completionQueued
    }

    /// Built on first use, never at launch. Constructing a background `URLSession` while the
    /// app is still starting up is exactly the kind of work that does not belong on the launch
    /// path on a Watch, so the queue is created the first time a session actually finishes.
    private func sharedCompletionQueue() throws -> WatchCompletionQueue {
        if let completionQueue { return completionQueue }
        // 【2026-10-15 修】改用 App 进程内共享的 ledger 实例（见 `SegmentLedger.liveShared`）。
        // 此前这里自建了一个指向**同一目录**的新实例：与 `WatchRecordingCoordinator` 的实例
        // 各自持有 `cachedManifests`、彼此无互斥，交错落盘时后写覆盖先写 → 丢分段记录。
        let delivery = WatchBackgroundDelivery(ledger: SegmentLedger.liveShared)
        backgroundDelivery = delivery
        let queue = WatchCompletionQueue(
            delivery: delivery,
            deviceID: configuration.deviceID,
            baseURL: configuration.baseURL
        )
        completionQueue = queue
        return queue
    }

    /// Best-effort safety net: the foreground answer is authoritative, this only ensures the
    /// Mac eventually learns the session ended even if we are suspended mid-request.
    private func makeRequest(
        path: [String],
        method: String,
        contentType: String,
        headers: [String: String] = [:]
    ) throws -> URLRequest {
        try WatchCaptureNetworkGate.shared.check(
            allowDuringCapture: allowsCaptureUploads
        )
        var url = configuration.baseURL
        for component in path {
            url.appendPathComponent(component)
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        // Every request to the Mac carries the Watch credential. The endpoints the Mac keeps
        // open for the Watch (segment upload, session completion, registration) require it, so
        // they no longer rely on "the caller came from a private address" alone.
        request.setValue(
            WatchUploadCredential.headerValue(),
            forHTTPHeaderField: WatchUploadCredential.headerName
        )
        for (name, value) in headers {
            request.setValue(value, forHTTPHeaderField: name)
        }
        return request
    }

    private func validate(response: URLResponse, body: Data) throws -> [String: Any] {
        guard let httpResponse = response as? HTTPURLResponse else {
            throw WatchSyncTransportError.invalidResponse
        }
        guard (200..<300).contains(httpResponse.statusCode) else {
            let errorObject = try? JSONSerialization.jsonObject(with: body) as? [String: Any]
            let detail = (errorObject?["detail"] as? String)
                ?? (errorObject?["error"] as? String)
                ?? "Mac rejected the upload"
            throw WatchSyncTransportError.httpStatus(httpResponse.statusCode, detail)
        }
        guard let payload = try JSONSerialization.jsonObject(with: body) as? [String: Any] else {
            throw WatchSyncTransportError.invalidResponse
        }
        return payload
    }

    private func bodyDataFile(data: Data) throws -> URL {
        let supportRoot = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
        let directory = supportRoot
            .appendingPathComponent("LifeLogger", isDirectory: true)
            .appendingPathComponent("requests", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let file = directory.appendingPathComponent("request-\(UUID().uuidString).json")
        try data.write(to: file, options: .atomic)
        return file
    }

    private static func iso8601(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }

}

enum WatchSyncTransportError: LocalizedError {
    case localSegmentMissing
    case sessionNotFinished
    case invalidAcknowledgement
    case invalidResponse
    case watchDirectAccessDisabled
    case httpStatus(Int, String)

    var errorDescription: String? {
        switch self {
        case .localSegmentMissing:
            "A pending audio segment is missing from Watch storage."
        case .sessionNotFinished:
            "Stop the recording before finalizing the Mac session."
        case .invalidAcknowledgement:
            "Mac returned an invalid segment acknowledgement."
        case .invalidResponse:
            "Mac returned an unreadable response."
        case .watchDirectAccessDisabled:
            "Mac is reachable, but Watch direct upload is not enabled on this trusted network."
        case .httpStatus(let status, let detail):
            "Mac upload failed (HTTP \(status)): \(detail)"
        }
    }
}
