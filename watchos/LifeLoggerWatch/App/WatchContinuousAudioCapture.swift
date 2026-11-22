import AVFAudio
import Foundation

protocol WatchCaptureRecorderDelegate: AnyObject {
    nonisolated func audioRecorderDidFinishRecording(_ recorder: WatchCaptureRecorder, successfully flag: Bool)
    nonisolated func audioRecorderEncodeErrorDidOccur(_ recorder: WatchCaptureRecorder, error: Error?)
}

/// One engine and input tap per recording session; closing a file never releases the microphone.
@MainActor
final class WatchContinuousAudioCapture {
    private let engine = AVAudioEngine()
    private let sink: WatchCaptureSink
    private var installed = false
    private nonisolated(unsafe) var engineConfigObserver: (any NSObjectProtocol)?
    var onFailure: (@MainActor (String) -> Void)?
    var onQueueWarning: (@MainActor (String) -> Void)?
    private nonisolated let rotationHandlerLock = NSLock()
    private nonisolated(unsafe) var rotationHandler: (@Sendable (URL, Int, Error?) async -> Void)?
    nonisolated var onSegmentRotated: (@Sendable (URL, Int, Error?) async -> Void)? {
        get { rotationHandlerLock.withLock { rotationHandler } }
        set { rotationHandlerLock.withLock { rotationHandler = newValue } }
    }

    init(workerQueue: DispatchQueue = DispatchQueue(
        label: "com.lifelogger.watch.continuous-audio",
        qos: .userInitiated
    ), maximumQueuedBuffers: Int = 1024) {
        let sink = WatchCaptureSink(
            workerQueue: workerQueue,
            maximumBuffers: maximumQueuedBuffers
        )
        self.sink = sink
        sink.setFailureHandler { [weak self] message in
            Task { @MainActor [weak self] in
                self?.onFailure?(message)
            }
        }
        sink.setQueueWarningHandler { [weak self] in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.onQueueWarning?(self.sink.takeQueueWarningSummary())
                self.sink.acknowledgeQueueWarning()
            }
        }
        sink.setSegmentCompletedHandler { [weak self] url, sequence, error in
            guard let self else { return }
            let handler = self.rotationHandlerLock.withLock { self.rotationHandler }
            guard let handler else { return }
            Task.detached(priority: .userInitiated) {
                await handler(url, sequence, error)
            }
        }
        engineConfigObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.handleEngineConfigurationChange()
            }
        }
    }

    // V43: silentPlayer REMOVED — it caused watchOS to fire audio route
    // interruptions (`AVAudioSession.interruptionNotification .began`) when
    // no Bluetooth output device is connected, which permanently set
    // `isAudioInterrupted = true` and killed the recording at the 60 s
    // segment boundary.  Pure input-only capture needs no output node.

    fileprivate func start(_ recorder: WatchCaptureRecorder, duration: TimeInterval, autoRotate: Bool = false) throws {
        if !installed {
            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            guard format.sampleRate > 0, format.channelCount > 0 else {
                throw WatchCaptureError.unavailableInput
            }
            input.installTap(onBus: 0, bufferSize: 1024, format: format,
                             block: Self.makeInputTap(sink: sink))
            installed = true
        }
        try sink.begin(recorder, duration: duration, autoRotate: autoRotate)
        do {
            if !engine.isRunning {
                sink.startNewCaptureClock()
                try engine.start()
            }
        } catch {
            // 分段根本没录成，不能走轮转回调去登记空文件。
            sink.end(recorder, notify: false, deliverRotation: false)
            throw error
        }
    }

    // The microphone invokes this on its audio thread. Construct outside MainActor
    // isolation so Swift does not attach a main-executor check to every PCM packet.
    nonisolated static func makeInputTap(sink: WatchCaptureSink) -> @Sendable (AVAudioPCMBuffer, AVAudioTime) -> Void {
        { buffer, _ in sink.receive(buffer) }
    }

    fileprivate func stop(_ recorder: WatchCaptureRecorder) {
        stopInputProducer()
        sink.end(recorder, notify: false)
    }
    fileprivate func snapshot(_ id: UUID) -> WatchCaptureSnapshot {
        sink.snapshot(id)
    }
    var audioEngineIsRunning: Bool { engine.isRunning }
    var isCapturing: Bool { engine.isRunning && installed }
    fileprivate func lastInputPacketAt(for id: UUID) -> Date? {
        sink.lastInputPacketAt(for: id)
    }
    nonisolated fileprivate func currentSegmentURL(for id: UUID) -> URL? {
        sink.currentSegmentURL(for: id)
    }

    func stopCapture() {
        stopInputProducer()
        sink.shutdown()
    }

    func reinstallTapAndRestartEngine() throws {
        if engine.isRunning { engine.stop() }
        if installed {
            engine.inputNode.removeTap(onBus: 0)
            installed = false
        }
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw WatchCaptureError.unavailableInput
        }
        input.installTap(onBus: 0, bufferSize: 1024, format: format,
                         block: Self.makeInputTap(sink: sink))
        installed = true
        sink.startNewCaptureClock()
        try engine.start()
    }

    func restartEngineIfNeeded() throws {
        if !engine.isRunning {
            try reinstallTapAndRestartEngine()
        }
    }

    private func handleEngineConfigurationChange() {
        guard installed else { return }
        WatchCaptureEventLog.shared.record("AVAudioEngineConfigurationChange detected")
        do {
            try reinstallTapAndRestartEngine()
            WatchCaptureEventLog.shared.record("AVAudioEngine successfully restarted on new configuration")
        } catch {
            WatchCaptureEventLog.shared.record("AVAudioEngine restart failed on new configuration: \(error.localizedDescription), retrying in 250ms...")
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 250_000_000)
                guard let self, self.installed else { return }
                do {
                    try self.reinstallTapAndRestartEngine()
                    WatchCaptureEventLog.shared.record("AVAudioEngine delayed restart succeeded")
                } catch {
                    WatchCaptureEventLog.shared.record("AVAudioEngine delayed restart failed: \(error.localizedDescription)")
                    self.onFailure?("音频配置变更，引擎重启失败：\(error.localizedDescription)")
                }
            }
        }
    }

    private func stopInputProducer() {
        if engine.isRunning { engine.stop() }
        if installed {
            engine.inputNode.removeTap(onBus: 0)
            installed = false
        }
    }

    deinit {
        if let engineConfigObserver {
            NotificationCenter.default.removeObserver(engineConfigObserver)
        }
    }
}

@MainActor
final class WatchCaptureRecorder {
    nonisolated let url: URL
    fileprivate let settings: [String: Any]
    nonisolated let id = UUID()
    private let source: WatchContinuousAudioCapture
    weak var delegate: (any WatchCaptureRecorderDelegate)?
    var isMeteringEnabled = false
    private(set) var lastError: Error?

    init(url: URL, settings: [String: Any], source: WatchContinuousAudioCapture) throws {
        self.url = url
        self.settings = settings
        self.source = source
    }

    nonisolated var currentSegmentURL: URL {
        source.currentSegmentURL(for: id) ?? url
    }
    var currentTime: Double { source.snapshot(id).seconds }
    var captureStartedAt: Date? { source.snapshot(id).startedAt }
    var isRecording: Bool { source.snapshot(id).recording }
    var audioEngineIsRunning: Bool { source.audioEngineIsRunning }
    var lastInputPacketAt: Date? { source.lastInputPacketAt(for: id) }
    func restartEngine() throws {
        try source.restartEngineIfNeeded()
    }
    func livenessIssue(at now: Date = Date()) -> WatchCaptureLivenessIssue? {
        let snapshot = source.snapshot(id)
        guard snapshot.recording else { return nil }
        return WatchCaptureLivenessPolicy.issue(
            engineRunning: source.audioEngineIsRunning,
            recordingBeganAt: snapshot.recordingBeganAt,
            lastInputPacketAt: source.lastInputPacketAt(for: id),
            now: now
        )
    }
    func averagePower(forChannel channel: Int) -> Float { source.snapshot(id).power }
    func updateMeters() {}
    func prepareToRecord() -> Bool { true }
    func record(forDuration duration: TimeInterval, autoRotate: Bool = false) -> Bool {
        do { try source.start(self, duration: duration, autoRotate: autoRotate); return true }
        catch { lastError = error; return false }
    }
    func stop() { source.stop(self) }

    fileprivate func finished(_ error: Error?) {
        if let error {
            lastError = error
            delegate?.audioRecorderEncodeErrorDidOccur(self, error: error)
        } else {
            delegate?.audioRecorderDidFinishRecording(self, successfully: true)
        }
    }
}

private enum WatchCaptureError: Error, LocalizedError {
    case unavailableInput, queueOverloaded, conversionFailed, writerAlreadyActive
    var errorDescription: String? {
        switch self {
        case .unavailableInput: "Microphone input format is unavailable."
        case .queueOverloaded: "Audio capture queue exceeded its bounded capacity; recording stopped to preserve existing audio."
        case .conversionFailed: "Microphone audio conversion failed."
        case .writerAlreadyActive: "An audio segment writer is already active."
        }
    }
}

struct WatchCaptureSnapshot: Sendable {
    var startedAt: Date?
    var recordingBeganAt: Date?
    var seconds: Double = 0
    var recording = false
    var power: Float = -160
}

enum WatchCaptureLivenessIssue: String, Equatable {
    case engineStopped = "audio-engine-stopped"
    case inputStalled = "microphone-input-stalled"
}

enum WatchCaptureLivenessPolicy {
    static let inputStaleSeconds: TimeInterval = 90

    static func issue(
        engineRunning: Bool,
        recordingBeganAt: Date?,
        lastInputPacketAt: Date?,
        now: Date
    ) -> WatchCaptureLivenessIssue? {
        guard engineRunning else { return .engineStopped }
        guard let recordingBeganAt else { return .inputStalled }
        guard now.timeIntervalSince(recordingBeganAt) >= inputStaleSeconds else { return nil }
        guard let lastInputPacketAt,
              lastInputPacketAt >= recordingBeganAt,
              now.timeIntervalSince(lastInputPacketAt) <= inputStaleSeconds else {
            return .inputStalled
        }
        return nil
    }
}

/// AVFoundation buffers and writers are confined to this serial queue after the tap copies them.
final class WatchCaptureSink: @unchecked Sendable {
    private let queue: DispatchQueue
    private let admissionLock = NSLock()
    private let snapshotLock = NSLock()
    private let failureHandlerLock = NSLock()
    private let warningHandlerLock = NSLock()
    private let segmentHandlerLock = NSLock()
    private var publishedSnapshots: [UUID: WatchCaptureSnapshot] = [:]
    private var failureHandler: (@Sendable (String) -> Void)?
    private var queueWarningHandler: (@Sendable () -> Void)?
    private var segmentCompletionHandler: (@Sendable (URL, Int, Error?) -> Void)?
    private var latestInputPacketAt: Date?
    private var epoch = 0
    private let maximumBuffers: Int
    private var drainScheduled = false
    private let maximumDrainBatch = 16
    private var pending: [WatchCapturePacket] = []
    private var droppedBufferCount = 0
    private var droppedDuration = 0.0
    private var queueWarningPending = false
    private var acceptingBuffers = true
    private var captureGeneration = 0
    private var processedGeneration: Int?
    private var captureAnchor: Date?
    private var continuousFrames: AVAudioFramePosition = 0
    private var snapshots: [UUID: WatchCaptureSnapshot] = [:]
    private var writer: AVAudioFile?
    private var recorder: WatchCaptureRecorder?
    private var activeID: UUID?
    private var lastFinishedID: UUID?
    private var lastFinishedURL: URL?
    private var target: AVAudioFormat?
    private var converter: AVAudioConverter?
    private var frames: AVAudioFramePosition = 0
    private var limit: AVAudioFramePosition = 0
    private var failed = false

    private var autoRotate: Bool = false
    private var stagingDirectory: URL?
    private var currentSequence: Int = 0
    private var writerSettings: [String: Any]?
    private var currentFileURL: URL?
    private var segmentDuration: TimeInterval = 60

    init(
        workerQueue: DispatchQueue = DispatchQueue(
            label: "com.lifelogger.watch.continuous-audio",
            qos: .userInitiated
        ),
        maximumBuffers: Int = 1024
    ) {
        queue = workerQueue
        self.maximumBuffers = max(1, maximumBuffers)
    }

    func setFailureHandler(_ handler: @escaping @Sendable (String) -> Void) {
        failureHandlerLock.withLock { failureHandler = handler }
    }

    func setQueueWarningHandler(_ handler: @escaping @Sendable () -> Void) {
        warningHandlerLock.lock()
        queueWarningHandler = handler
        warningHandlerLock.unlock()
    }

    func takeQueueWarningSummary() -> String {
        admissionLock.lock(); defer { admissionLock.unlock() }
        let count = droppedBufferCount
        let duration = droppedDuration
        droppedBufferCount = 0
        droppedDuration = 0
        return "录音仍在继续；缓冲拥塞丢弃了\(count)个旧音频包，约\(String(format: "%.2f", duration))秒音频"
    }

    func acknowledgeQueueWarning() {
        admissionLock.lock()
        let hasMore = droppedBufferCount > 0
        if !hasMore { queueWarningPending = false }
        admissionLock.unlock()
        if hasMore {
            let handler = warningHandlerLock.withLock { queueWarningHandler }
            handler?()
        }
    }

    func setSegmentCompletedHandler(_ handler: @escaping @Sendable (URL, Int, Error?) -> Void) {
        segmentHandlerLock.withLock { segmentCompletionHandler = handler }
    }

    func currentSegmentURL(for id: UUID) -> URL? {
        queue.sync {
            if activeID != id { return lastFinishedID == id ? lastFinishedURL : nil }
            return currentFileURL ?? recorder?.url
        }
    }

    @MainActor
    func begin(_ recorder: WatchCaptureRecorder, duration: TimeInterval, autoRotate: Bool = false) throws {
        let url = recorder.url
        let settings = recorder.settings
        let id = recorder.id
        try queue.sync {
            admissionLock.lock(); acceptingBuffers = true; admissionLock.unlock()
            if writer != nil {
                drainPendingSynchronously()
                finish(error: nil, notify: false)
            }
            failed = false
            lastFinishedID = nil
            lastFinishedURL = nil
            let file = try AVAudioFile(forWriting: url, settings: settings, commonFormat: .pcmFormatFloat32, interleaved: false)
            writer = file
            target = file.processingFormat
            self.recorder = recorder
            activeID = id
            frames = 0
            limit = AVAudioFramePosition(duration * file.processingFormat.sampleRate)
            self.autoRotate = autoRotate
            self.segmentDuration = duration
            self.stagingDirectory = url.deletingLastPathComponent()
            self.writerSettings = settings
            self.currentFileURL = url
            self.currentSequence = Self.extractSequence(from: url) ?? 0
            let initialSnapshot = WatchCaptureSnapshot(recordingBeganAt: Date(), recording: true)
            snapshots[id] = initialSnapshot
            publishSnapshot(initialSnapshot, for: id)
            admissionLock.lock()
            let waiting = pending
            pending.removeAll(keepingCapacity: true)
            admissionLock.unlock()
            for buffer in waiting { consume(buffer) }
        }
    }

    private static func extractSequence(from url: URL) -> Int? {
        let filename = url.deletingPathExtension().lastPathComponent
        let prefix = "segment-"
        guard filename.hasPrefix(prefix) else { return nil }
        return Int(filename.dropFirst(prefix.count))
    }

    func startNewCaptureClock() {
        admissionLock.lock(); captureGeneration += 1; admissionLock.unlock()
    }

    func receive(_ buffer: AVAudioPCMBuffer, capturedAt: Date? = nil) {
        let packetStart = capturedAt ?? Date().addingTimeInterval(-Double(buffer.frameLength) / buffer.format.sampleRate)
        guard let copy = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength) else {
            queue.async { self.fail(WatchCaptureError.conversionFailed) }
            return
        }
        copy.frameLength = buffer.frameLength
        let source = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
        let destination = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
        for index in source.indices {
            if let from = source[index].mData, let to = destination[index].mData {
                memcpy(to, from, Int(source[index].mDataByteSize))
            }
        }
        admissionLock.lock()
        guard acceptingBuffers else { admissionLock.unlock(); return }
        let packet = WatchCapturePacket(buffer: copy, startedAt: packetStart, generation: captureGeneration)
        var shouldReportDrops = false
        if pending.count >= maximumBuffers {
            let dropped = pending.removeFirst()
            droppedBufferCount += 1
            droppedDuration += Double(dropped.buffer.frameLength) / dropped.buffer.format.sampleRate
            if !queueWarningPending {
                queueWarningPending = true
                shouldReportDrops = true
            }
        }
        pending.append(packet)
        snapshotLock.withLock { latestInputPacketAt = Date() }
        let shouldSchedule = !drainScheduled
        if shouldSchedule { drainScheduled = true }
        admissionLock.unlock()
        if shouldReportDrops {
            let handler = warningHandlerLock.withLock { queueWarningHandler }
            handler?()
        }
        if shouldSchedule { queue.async { self.drainPending() } }
    }

    private func drainPending() {
        for _ in 0..<maximumDrainBatch {
            admissionLock.lock()
            let packet = pending.isEmpty ? nil : pending.removeFirst()
            if packet == nil { drainScheduled = false }
            admissionLock.unlock()
            guard let packet else { return }
            guard writer != nil, activeID != nil else {
                admissionLock.lock()
                pending.insert(packet, at: 0)
                drainScheduled = false
                admissionLock.unlock()
                return
            }
            consume(packet)
        }
        admissionLock.lock()
        let hasPending = !pending.isEmpty
        if !hasPending { drainScheduled = false }
        admissionLock.unlock()
        if hasPending { queue.async { self.drainPending() } }
    }

    /// Called only on `queue` after the producer cutoff. The admission capacity bounds
    /// this loop, and synchronous end callers are already executing ahead of re-enqueued drains.
    private func drainPendingSynchronously() {
        while true {
            admissionLock.lock()
            let packet = pending.isEmpty ? nil : pending.removeFirst()
            admissionLock.unlock()
            guard let packet else { return }
            guard writer != nil, activeID != nil else {
                admissionLock.lock(); pending.insert(packet, at: 0); admissionLock.unlock()
                return
            }
            consume(packet)
        }
    }

    private func enqueuePending(_ packet: WatchCapturePacket) {
        admissionLock.lock()
        var shouldReportDrops = false
        if pending.count >= maximumBuffers {
            let dropped = pending.removeFirst()
            droppedBufferCount += 1
            droppedDuration += Double(dropped.buffer.frameLength) / dropped.buffer.format.sampleRate
            if !queueWarningPending {
                queueWarningPending = true
                shouldReportDrops = true
            }
        }
        pending.append(packet)
        let shouldSchedule = !drainScheduled
        if shouldSchedule { drainScheduled = true }
        admissionLock.unlock()
        if shouldReportDrops {
            let handler = warningHandlerLock.withLock { queueWarningHandler }
            handler?()
        }
        if shouldSchedule { queue.async { self.drainPending() } }
    }

    private func consume(_ packet: WatchCapturePacket) {
        let buffer = packet.buffer
        guard !failed else { return }
        guard writer != nil, let target, let id = activeID else {
            enqueuePending(packet)
            return
        }
        if processedGeneration != packet.generation {
            processedGeneration = packet.generation
            captureAnchor = packet.startedAt
            continuousFrames = 0
            converter = nil
        }
        if converter == nil || converter?.inputFormat != buffer.format || converter?.outputFormat != target {
            converter = AVAudioConverter(from: buffer.format, to: target)
        }
        guard let converter,
              let converted = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: AVAudioFrameCount(ceil(Double(buffer.frameLength) * target.sampleRate / buffer.format.sampleRate)) + 32) else {
            fail(WatchCaptureError.conversionFailed); return
        }
        let input = WatchConversionInput(buffer)
        var conversionError: NSError?
        let result = converter.convert(to: converted, error: &conversionError) { _, status in
            guard let next = input.take() else { status.pointee = .noDataNow; return nil }
            status.pointee = .haveData
            return next
        }
        guard result != .error else { fail(conversionError ?? (WatchCaptureError.conversionFailed as NSError)); return }
        do {
            if converted.frameLength > 0, snapshots[id]?.startedAt == nil {
                snapshots[id]?.startedAt = captureAnchor?.addingTimeInterval(Double(continuousFrames) / target.sampleRate)
            }
            try self.writer?.write(from: converted)
            continuousFrames += AVAudioFramePosition(converted.frameLength)
            frames += AVAudioFramePosition(converted.frameLength)
            var power: Float = -160
            if let samples = converted.floatChannelData?[0], converted.frameLength > 0 {
                var sum: Float = 0
                for i in 0..<Int(converted.frameLength) { sum += samples[i] * samples[i] }
                power = 10 * log10(max(sum / Float(converted.frameLength), 1e-16))
            }
            let snapshot = WatchCaptureSnapshot(
                startedAt: snapshots[id]?.startedAt,
                recordingBeganAt: snapshots[id]?.recordingBeganAt,
                seconds: Double(frames) / target.sampleRate,
                recording: true,
                power: power
            )
            snapshots[id] = snapshot
            publishSnapshot(snapshot, for: id)
            // Whole input packets stay together; a segment may exceed its target by one tap packet.
            if frames >= limit {
                if autoRotate,
                   let stagingDir = self.stagingDirectory,
                   let fileSettings = self.writerSettings {
                    let finishedURL = self.currentFileURL ?? recorder?.url
                    let completedSequence = self.currentSequence
                    self.writer = nil // Finalize previous AAC file before rotating
                    
                    let handler = self.segmentHandlerLock.withLock { self.segmentCompletionHandler }
                    if let finishedURL {
                        handler?(finishedURL, completedSequence, nil)
                    }
                    
                    let nextSequence = completedSequence + 1
                    let nextURL = stagingDir.appendingPathComponent(
                        String(format: "segment-%06d.m4a", nextSequence)
                    )
                    
                    do {
                        let nextFile = try AVAudioFile(
                            forWriting: nextURL,
                            settings: fileSettings,
                            commonFormat: .pcmFormatFloat32,
                            interleaved: false
                        )
                        self.writer = nextFile
                        self.currentFileURL = nextURL
                        self.currentSequence = nextSequence
                        self.target = nextFile.processingFormat
                        self.frames = 0
                        self.limit = AVAudioFramePosition(self.segmentDuration * nextFile.processingFormat.sampleRate)
                        if let id = self.activeID {
                            self.snapshots[id]?.seconds = 0
                        }
                    } catch {
                        self.fail(error)
                    }
                } else {
                    finish(error: nil, notify: true)
                }
            }
        } catch { fail(error) }
    }

    @MainActor
    func end(_ recorder: WatchCaptureRecorder, notify: Bool, deliverRotation: Bool = true) {
        let id = recorder.id
        admissionLock.lock(); acceptingBuffers = false; admissionLock.unlock()
        queue.sync {
            if activeID == id {
                drainPendingSynchronously()
                let finishedURL = self.currentFileURL ?? recorder.url
                let completedSequence = self.currentSequence
                let wasAutoRotating = self.autoRotate
                self.autoRotate = false
                finish(error: nil, notify: notify)
                if wasAutoRotating && deliverRotation {
                    let handler = self.segmentHandlerLock.withLock { self.segmentCompletionHandler }
                    handler?(finishedURL, completedSequence, nil)
                }
            }
        }
    }
    /// A meter read must never wait for conversion or AAC writing on the capture queue.
    func snapshot(_ id: UUID) -> WatchCaptureSnapshot {
        snapshotLock.withLock { publishedSnapshots[id] ?? WatchCaptureSnapshot() }
    }

    func lastInputPacketAt(for id: UUID) -> Date? {
        snapshotLock.withLock {
            guard publishedSnapshots[id]?.recording == true else { return nil }
            return latestInputPacketAt
        }
    }

    func flushForTesting() {
        queue.sync {}
    }
    func shutdown() {
        admissionLock.lock(); epoch += 1; acceptingBuffers = false; admissionLock.unlock()
        queue.sync {
            drainPendingSynchronously()
            let finishedURL = currentFileURL
            let finishedSequence = currentSequence
            let shouldReportFinalSegment = autoRotate && activeID != nil
            autoRotate = false
            finish(error: nil, notify: false)
            if shouldReportFinalSegment, let finishedURL {
                let handler = segmentHandlerLock.withLock { segmentCompletionHandler }
                handler?(finishedURL, finishedSequence, nil)
            }
            admissionLock.lock(); pending.removeAll(); drainScheduled = false; admissionLock.unlock()
            currentFileURL = nil
            stagingDirectory = nil
            writerSettings = nil
            converter = nil
            captureAnchor = nil
            processedGeneration = nil
            continuousFrames = 0
            failed = false
        }
    }
    private func fail(_ error: Error) {
        failed = true
        if activeID != nil {
            finish(error: error, notify: true)
        } else {
            let message = error.localizedDescription
            let handler = failureHandlerLock.withLock { failureHandler }
            handler?(message)
        }
    }
    private func finish(error: Error?, notify: Bool) {
        guard let id = activeID else { return }
        let completed = recorder
        let finishedURL = currentFileURL ?? completed?.url
        let finishedSequence = currentSequence
        lastFinishedID = id
        lastFinishedURL = finishedURL
        writer = nil // Finalize the AAC container before notifying the coordinator.
        recorder = nil
        activeID = nil
        snapshots[id]?.recording = false
        if let snapshot = snapshots[id] { publishSnapshot(snapshot, for: id) }
        if notify, let completed {
            if let error, let finishedURL {
                let handler = segmentHandlerLock.withLock { segmentCompletionHandler }
                handler?(finishedURL, finishedSequence, error)
            }
            Task { @MainActor in completed.finished(error) }
        }
    }

    private func publishSnapshot(_ snapshot: WatchCaptureSnapshot, for id: UUID) {
        snapshotLock.withLock { publishedSnapshots[id] = snapshot }
    }
}

private struct WatchCapturePacket: @unchecked Sendable {
    let buffer: AVAudioPCMBuffer
    let startedAt: Date
    let generation: Int
}

/// Converter invokes its input provider synchronously; the lock also enforces one delivery.
private final class WatchConversionInput: @unchecked Sendable {
    private let lock = NSLock()
    private var buffer: AVAudioPCMBuffer?
    init(_ buffer: AVAudioPCMBuffer) { self.buffer = buffer }
    func take() -> AVAudioPCMBuffer? {
        lock.lock(); defer { lock.unlock() }
        let result = buffer
        buffer = nil
        return result
    }
}
