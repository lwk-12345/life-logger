import AVFAudio
import Foundation
import XCTest
@testable import CaptureUnderTest

@MainActor
final class CaptureTests: XCTestCase {
    func testMicrophoneTapRunsOffMainActor() async throws {
        let sink = WatchCaptureSink()
        let tap = WatchContinuousAudioCapture.makeInputTap(sink: sink)
        try await Task.detached {
            XCTAssertFalse(Thread.isMainThread)
            let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1))
            let packet = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 160))
            packet.frameLength = 160
            for i in 0..<160 { packet.floatChannelData![0][i] = 0 }
            tap(packet, AVAudioTime(sampleTime: 0, atRate: 16000))
        }.value
        sink.shutdown()
    }

    func testMeterSnapshotDoesNotWaitForAudioWriterQueue() throws {
        let workerQueue = DispatchQueue(label: "capture-test.blocked-worker")
        let entered = DispatchSemaphore(value: 0)
        let release = DispatchSemaphore(value: 0)
        workerQueue.async {
            entered.signal()
            release.wait()
        }
        XCTAssertEqual(entered.wait(timeout: .now() + 1), .success)
        let sink = WatchCaptureSink(workerQueue: workerQueue)
        let readFinished = DispatchSemaphore(value: 0)

        DispatchQueue.global().async {
            _ = sink.snapshot(UUID())
            readFinished.signal()
        }
        let snapshotRead = readFinished.wait(timeout: .now() + .milliseconds(100))
        release.signal()

        XCTAssertEqual(snapshotRead, .success)
        sink.shutdown()
    }

    func testCaptureLivenessDetectsStoppedEngineEvenWithRecentPacket() {
        let startedAt = Date(timeIntervalSince1970: 1_000)
        let issue = WatchCaptureLivenessPolicy.issue(
            engineRunning: false,
            recordingBeganAt: startedAt,
            lastInputPacketAt: startedAt.addingTimeInterval(1),
            now: startedAt.addingTimeInterval(2)
        )

        XCTAssertEqual(issue, .engineStopped)
    }

    func testCaptureLivenessAllowsFirstPacketGracePeriod() {
        let startedAt = Date(timeIntervalSince1970: 1_000)

        XCTAssertNil(WatchCaptureLivenessPolicy.issue(
            engineRunning: true,
            recordingBeganAt: startedAt,
            lastInputPacketAt: nil,
            now: startedAt.addingTimeInterval(WatchCaptureLivenessPolicy.inputStaleSeconds - 0.1)
        ))
    }

    func testCaptureLivenessDetectsMissingOrStaleInputAfterGracePeriod() {
        let startedAt = Date(timeIntervalSince1970: 1_000)
        let now = startedAt.addingTimeInterval(WatchCaptureLivenessPolicy.inputStaleSeconds + 0.1)

        XCTAssertEqual(WatchCaptureLivenessPolicy.issue(
            engineRunning: true,
            recordingBeganAt: startedAt,
            lastInputPacketAt: nil,
            now: now
        ), .inputStalled)
        XCTAssertEqual(WatchCaptureLivenessPolicy.issue(
            engineRunning: true,
            recordingBeganAt: startedAt,
            lastInputPacketAt: startedAt,
            now: now
        ), .inputStalled)
    }

    func testRecentTapPacketKeepsCaptureHealthy() {
        let startedAt = Date(timeIntervalSince1970: 1_000)
        let now = startedAt.addingTimeInterval(20)

        XCTAssertNil(WatchCaptureLivenessPolicy.issue(
            engineRunning: true,
            recordingBeganAt: startedAt,
            lastInputPacketAt: now.addingTimeInterval(-2),
            now: now
        ))
    }

    func testInputHeartbeatAdvancesWhileWriterQueueIsBlocked() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let workerQueue = DispatchQueue(label: "capture-test.heartbeat-blocked-worker")
        let source = WatchContinuousAudioCapture()
        let recorder = try WatchCaptureRecorder(
            url: root.appendingPathComponent("heartbeat.wav"),
            settings: pcmSettings,
            source: source
        )
        let sink = WatchCaptureSink(workerQueue: workerQueue, maximumBuffers: 8)
        try sink.begin(recorder, duration: 60)
        let entered = DispatchSemaphore(value: 0)
        let release = DispatchSemaphore(value: 0)
        defer { release.signal() }
        workerQueue.async { entered.signal(); release.wait() }
        XCTAssertEqual(entered.wait(timeout: .now() + 1), .success)

        let acceptedAt = Date()
        sink.receive(try buffer(value: 0.25))
        let lastInputPacketAt = try XCTUnwrap(sink.lastInputPacketAt(for: recorder.id))

        XCTAssertGreaterThanOrEqual(lastInputPacketAt, acceptedAt)
        XCTAssertLessThan(Date().timeIntervalSince(lastInputPacketAt), 1)
        XCTAssertTrue(sink.snapshot(recorder.id).recording)

        release.signal()
        sink.flushForTesting()
        sink.end(recorder, notify: false)
        sink.shutdown()
    }

    func testQueueOverflowDropsOldestQueuedAudioAndRecordingContinues() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let source = WatchContinuousAudioCapture()
        let url = root.appendingPathComponent("after-overflow.wav")
        let recorder = try WatchCaptureRecorder(url: url, settings: pcmSettings, source: source)
        let baseline = try WatchCaptureRecorder(url: root.appendingPathComponent("baseline.wav"), settings: pcmSettings, source: source)
        let baselineSink = WatchCaptureSink()
        try baselineSink.begin(baseline, duration: 60)
        baselineSink.receive(try buffer(value: 0.8))
        baselineSink.receive(try buffer(value: 0.9))
        baselineSink.flushForTesting()
        baselineSink.end(baseline, notify: false)
        let baselineFile = try AVAudioFile(forReading: baseline.url)
        let baselineBuffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: baselineFile.processingFormat,
            frameCapacity: AVAudioFrameCount(baselineFile.length)))
        try baselineFile.read(into: baselineBuffer)

        let workerQueue = DispatchQueue(label: "capture-test.overflow-worker")
        let sink = WatchCaptureSink(workerQueue: workerQueue, maximumBuffers: 2)
        try sink.begin(recorder, duration: 60)
        let entered = DispatchSemaphore(value: 0)
        let release = DispatchSemaphore(value: 0)
        workerQueue.async {
            entered.signal()
            release.wait()
        }
        XCTAssertEqual(entered.wait(timeout: .now() + 1), .success)
        final class WarningCounter: @unchecked Sendable {
            private let lock = NSLock()
            private(set) var count = 0
            func increment() { lock.withLock { count += 1 } }
        }
        let warningCounter = WarningCounter()
        sink.setQueueWarningHandler { warningCounter.increment() }
        for value: Float in [0.1, 0.2, 0.3, 0.4, 0.8, 0.9] {
            sink.receive(try buffer(value: value))
        }
        XCTAssertEqual(warningCounter.count, 1, "overflows should coalesce into one pending warning dispatch")
        let warning = sink.takeQueueWarningSummary()
        XCTAssertTrue(warning.contains("4个旧音频包"))
        XCTAssertTrue(warning.contains("0.40秒"))
        sink.acknowledgeQueueWarning()
        release.signal()
        sink.flushForTesting()
        XCTAssertTrue(sink.snapshot(recorder.id).recording)
        sink.shutdown()
        let file = try AVAudioFile(forReading: url)
        XCTAssertGreaterThan(file.length, 0)
        let decoded = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length)))
        try file.read(into: decoded)
        let samples = try XCTUnwrap(decoded.floatChannelData?[0])
        XCTAssertEqual(decoded.frameLength, baselineBuffer.frameLength)
        let baselineSamples = try XCTUnwrap(baselineBuffer.floatChannelData?[0])
        for index in 0..<Int(decoded.frameLength) {
            XCTAssertEqual(samples[index], baselineSamples[index], accuracy: 0.00001,
                           "only the two newest packets should remain (sample \(index))")
        }
    }

    func testCurrentSegmentQueryAndStopYieldDuringSustainedInput() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let workerQueue = DispatchQueue(label: "capture-test.sustained-worker")
        let source = WatchContinuousAudioCapture()
        let recorder = try WatchCaptureRecorder(url: root.appendingPathComponent("active.wav"),
            settings: pcmSettings, source: source)
        let sink = WatchCaptureSink(workerQueue: workerQueue, maximumBuffers: 256)
        try sink.begin(recorder, duration: 60)
        let blockerEntered = DispatchSemaphore(value: 0)
        let releaseBlocker = DispatchSemaphore(value: 0)
        workerQueue.async { blockerEntered.signal(); releaseBlocker.wait() }
        XCTAssertEqual(blockerEntered.wait(timeout: .now() + 1), .success)
        final class BufferBox: @unchecked Sendable {
            let value: AVAudioPCMBuffer
            init(_ value: AVAudioPCMBuffer) { self.value = value }
        }
        let packet = BufferBox(try buffer(value: 0.25))
        for _ in 0..<256 { sink.receive(packet.value) }

        let stopProducer = DispatchSemaphore(value: 0)
        let producerStarted = DispatchSemaphore(value: 0)
        let producerDone = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            producerStarted.signal()
            while stopProducer.wait(timeout: .now()) == .timedOut { sink.receive(packet.value) }
            producerDone.signal()
        }
        XCTAssertEqual(producerStarted.wait(timeout: .now() + 1), .success)
        DispatchQueue.global().asyncAfter(deadline: .now() + 2) { stopProducer.signal() }
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.05) { releaseBlocker.signal() }

        let queryStarted = Date()
        XCTAssertEqual(sink.currentSegmentURL(for: recorder.id)?.standardizedFileURL,
                       recorder.url.standardizedFileURL)
        let queryElapsed = Date().timeIntervalSince(queryStarted)
        let stopStarted = Date()
        sink.end(recorder, notify: false)
        let stopElapsed = Date().timeIntervalSince(stopStarted)
        stopProducer.signal()
        XCTAssertEqual(producerDone.wait(timeout: .now() + 1), .success)
        sink.shutdown()
        XCTAssertLessThan(queryElapsed, 1, "URL lookup must get queue time while input continues")
        XCTAssertLessThan(stopElapsed, 1, "stop must get queue time while input continues")
    }

    func testImmediateStopDrainsEveryAcceptedPacketBeyondOneBatch() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let source = WatchContinuousAudioCapture()
        let baselineURL = root.appendingPathComponent("baseline.wav")
        let baseline = try WatchCaptureRecorder(url: baselineURL, settings: pcmSettings, source: source)
        let baselineSink = WatchCaptureSink(maximumBuffers: 64)
        try baselineSink.begin(baseline, duration: 60)
        for _ in 0..<32 { baselineSink.receive(try buffer(value: 0.25)) }
        baselineSink.flushForTesting()
        baselineSink.end(baseline, notify: false)
        let baselineFile = try AVAudioFile(forReading: baselineURL)
        let baselineBuffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: baselineFile.processingFormat,
            frameCapacity: AVAudioFrameCount(baselineFile.length)))
        try baselineFile.read(into: baselineBuffer)

        let workerQueue = DispatchQueue(label: "capture-test.immediate-stop-worker")
        let sink = WatchCaptureSink(workerQueue: workerQueue, maximumBuffers: 64)
        let outputURL = root.appendingPathComponent("stopped.wav")
        let recorder = try WatchCaptureRecorder(url: outputURL, settings: pcmSettings, source: source)
        try sink.begin(recorder, duration: 60)
        let entered = DispatchSemaphore(value: 0)
        let release = DispatchSemaphore(value: 0)
        workerQueue.async { entered.signal(); release.wait() }
        XCTAssertEqual(entered.wait(timeout: .now() + 1), .success)
        for _ in 0..<32 { sink.receive(try buffer(value: 0.25)) }
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.05) { release.signal() }
        sink.end(recorder, notify: false)
        XCTAssertFalse(sink.snapshot(recorder.id).recording)
        let outputFile = try AVAudioFile(forReading: outputURL)
        let outputBuffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: outputFile.processingFormat,
            frameCapacity: AVAudioFrameCount(outputFile.length)))
        try outputFile.read(into: outputBuffer)
        XCTAssertEqual(outputBuffer.frameLength, baselineBuffer.frameLength)
        let expected = try XCTUnwrap(baselineBuffer.floatChannelData?[0])
        let actual = try XCTUnwrap(outputBuffer.floatChannelData?[0])
        for index in 0..<Int(outputBuffer.frameLength) {
            XCTAssertEqual(actual[index], expected[index], accuracy: 0.00001,
                           "stop must drain accepted packet sample \(index)")
        }
        sink.shutdown()
    }

    func testShutdownDrainsBacklogAcrossMultipleAutoRotatedSegments() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let workerQueue = DispatchQueue(label: "capture-test.shutdown-rotation-worker")
        let source = WatchContinuousAudioCapture()
        let firstURL = root.appendingPathComponent("segment-000000.m4a")
        let recorder = try WatchCaptureRecorder(url: firstURL, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 16000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: WatchAudioEncoding.bitRate], source: source)
        let sink = WatchCaptureSink(workerQueue: workerQueue, maximumBuffers: 64)
        final class SequenceBox: @unchecked Sendable {
            private let lock = NSLock()
            private var stored: [Int] = []
            var values: [Int] { lock.withLock { stored } }
            func append(_ value: Int) { lock.withLock { stored.append(value) } }
        }
        let sequences = SequenceBox()
        sink.setSegmentCompletedHandler { _, sequence, error in
            XCTAssertNil(error)
            sequences.append(sequence)
        }
        try sink.begin(recorder, duration: 0.15, autoRotate: true)
        let entered = DispatchSemaphore(value: 0)
        let release = DispatchSemaphore(value: 0)
        workerQueue.async { entered.signal(); release.wait() }
        XCTAssertEqual(entered.wait(timeout: .now() + 1), .success)
        for _ in 0..<39 { sink.receive(try buffer(value: 0.35)) }
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.05) { release.signal() }
        sink.shutdown()

        XCTAssertEqual(sequences.values, Array(0..<20), "shutdown must preserve every rotation and final partial sequence")
        for sequence in 0..<20 {
            let url = root.appendingPathComponent(String(format: "segment-%06d.m4a", sequence))
            XCTAssertTrue(FileManager.default.fileExists(atPath: url.path), "missing segment \(sequence)")
            XCTAssertGreaterThan(try AVAudioFile(forReading: url).length, 0, "empty segment \(sequence)")
        }
    }

    func testReceiveRacingSegmentEndAndNextBeginUsesLockedPendingQueue() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        final class BufferBox: @unchecked Sendable {
            let value: AVAudioPCMBuffer
            init(_ value: AVAudioPCMBuffer) { self.value = value }
        }
        final class Counter: @unchecked Sendable {
            private let lock = NSLock()
            private var stored = 0
            var value: Int { lock.withLock { stored } }
            func increment() { lock.withLock { stored += 1 } }
        }
        let source = WatchContinuousAudioCapture()
        let sink = WatchCaptureSink(maximumBuffers: 64)
        let first = try WatchCaptureRecorder(url: root.appendingPathComponent("first.wav"),
            settings: pcmSettings, source: source)
        try sink.begin(first, duration: 60)
        let packet = BufferBox(try buffer(value: 0.4))
        let stopProducer = DispatchSemaphore(value: 0)
        let count = Counter()
        let producerDone = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            while stopProducer.wait(timeout: .now()) == .timedOut {
                sink.receive(packet.value)
                count.increment()
            }
            producerDone.signal()
        }
        while count.value < 32 { Thread.sleep(forTimeInterval: 0.001) }
        sink.end(first, notify: false)
        let second = try WatchCaptureRecorder(url: root.appendingPathComponent("second.wav"),
            settings: pcmSettings, source: source)
        try sink.begin(second, duration: 60)
        while count.value < 64 { Thread.sleep(forTimeInterval: 0.001) }
        stopProducer.signal()
        XCTAssertEqual(producerDone.wait(timeout: .now() + 1), .success)
        sink.flushForTesting()
        sink.end(second, notify: false)
        XCTAssertGreaterThan(try AVAudioFile(forReading: first.url).length, 0)
        XCTAssertGreaterThan(try AVAudioFile(forReading: second.url).length, 0)
        sink.shutdown()
    }

    func testBoundaryBuffersArePreservedAndMicSourceIsReused() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let source = WatchContinuousAudioCapture()
        let sink = WatchCaptureSink()
        let first = try WatchCaptureRecorder(url: root.appendingPathComponent("first.wav"), settings: pcmSettings, source: source)
        try sink.begin(first, duration: 0.15)
        sink.receive(try buffer(value: 0.1))
        sink.receive(try buffer(value: 0.1))
        sink.flushForTesting()
        XCTAssertFalse(sink.snapshot(first.id).recording)
        sink.receive(try buffer(value: 0.5)) // Arrives between writers; it must not disappear.
        sink.flushForTesting()
        let second = try WatchCaptureRecorder(url: root.appendingPathComponent("second.wav"), settings: pcmSettings, source: source)
        try sink.begin(second, duration: 1)
        sink.receive(try buffer(value: 0.5))
        sink.flushForTesting()
        sink.end(second, notify: false)
        let firstFile = try AVAudioFile(forReading: first.url)
        let secondFile = try AVAudioFile(forReading: second.url)
        XCTAssertEqual(firstFile.processingFormat.sampleRate, 16000)
        XCTAssertEqual(secondFile.processingFormat.channelCount, 1)
        // Compare with one continuous file so converter priming is accounted for,
        // while an extra loss at the boundary still fails the test.
        let baseline = try WatchCaptureRecorder(url: root.appendingPathComponent("baseline.wav"),
            settings: pcmSettings, source: source)
        let baselineSink = WatchCaptureSink()
        try baselineSink.begin(baseline, duration: 60)
        for value: Float in [0.1, 0.1, 0.5, 0.5] { baselineSink.receive(try buffer(value: value)) }
        baselineSink.flushForTesting()
        baselineSink.end(baseline, notify: false)
        let baselineFile = try AVAudioFile(forReading: baseline.url)
        XCTAssertEqual(firstFile.length + secondFile.length, baselineFile.length,
                       "segmentation must not lose audio beyond converter startup priming")
        XCTAssertGreaterThan(secondFile.length, 3000, "boundary audio packet must be retained")
        sink.shutdown()
    }

    func testDelayedHandoffUsesBufferedAudioTimeNotWriterCreationTime() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let source = WatchContinuousAudioCapture()
        let sink = WatchCaptureSink()
        let epoch = Date(timeIntervalSince1970: 1000)
        let first = try WatchCaptureRecorder(url: root.appendingPathComponent("a.wav"), settings: pcmSettings, source: source)
        try sink.begin(first, duration: 0.15)
        sink.receive(try buffer(value: 0.1), capturedAt: epoch)
        sink.receive(try buffer(value: 0.1), capturedAt: epoch.addingTimeInterval(0.1))
        sink.flushForTesting()
        let old = sink.snapshot(first.id)
        sink.receive(try buffer(value: 0.5), capturedAt: epoch.addingTimeInterval(0.2))
        sink.flushForTesting()
        let second = try WatchCaptureRecorder(url: root.appendingPathComponent("b.wav"), settings: pcmSettings, source: source)
        try sink.begin(second, duration: 60)
        let next = sink.snapshot(second.id)
        XCTAssertEqual(try XCTUnwrap(next.startedAt).timeIntervalSince(try XCTUnwrap(old.startedAt)), old.seconds, accuracy: 0.001)
        XCTAssertLessThan(try XCTUnwrap(next.startedAt).timeIntervalSince(epoch), 0.3)
        sink.end(second, notify: false)
    }

    func testAACContainerIsReadableAfterSynchronousStop() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".m4a")
        defer { try? FileManager.default.removeItem(at: url) }
        let source = WatchContinuousAudioCapture()
        let recorder = try WatchCaptureRecorder(url: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 16000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: WatchAudioEncoding.bitRate], source: source)
        let sink = WatchCaptureSink()
        try sink.begin(recorder, duration: 60)
        for _ in 0..<10 { sink.receive(try buffer(value: 0.2)) }
        sink.flushForTesting()
        sink.end(recorder, notify: false)
        let file = try AVAudioFile(forReading: url)
        XCTAssertEqual(Double(file.length) / file.processingFormat.sampleRate, 1, accuracy: 0.07)
        XCTAssertFalse(sink.snapshot(recorder.id).recording)
        sink.shutdown()
    }

    func testAACSegmentRotation() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let aacSettings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: WatchAudioEncoding.bitRate
        ]
        let source = WatchContinuousAudioCapture()
        let sink = WatchCaptureSink()

        let firstURL = root.appendingPathComponent("segment-000000.m4a")
        let firstRecorder = try WatchCaptureRecorder(url: firstURL, settings: aacSettings, source: source)
        try sink.begin(firstRecorder, duration: 60)
        sink.receive(try buffer(value: 0.1))
        sink.receive(try buffer(value: 0.1))
        sink.flushForTesting()

        let secondURL = root.appendingPathComponent("segment-000001.m4a")
        let secondRecorder = try WatchCaptureRecorder(url: secondURL, settings: aacSettings, source: source)
        try sink.begin(secondRecorder, duration: 60)
        sink.receive(try buffer(value: 0.2))
        sink.receive(try buffer(value: 0.2))
        sink.flushForTesting()
        sink.end(secondRecorder, notify: false)

        let firstFile = try AVAudioFile(forReading: firstURL)
        let secondFile = try AVAudioFile(forReading: secondURL)
        XCTAssertGreaterThan(firstFile.length, 1000)
        XCTAssertGreaterThan(secondFile.length, 1000)
        sink.shutdown()
    }

    func testAutonomousSegmentRotationOnWorkerQueue() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let aacSettings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: WatchAudioEncoding.bitRate
        ]
        let source = WatchContinuousAudioCapture()
        let sink = WatchCaptureSink()

        final class CompletedBox: @unchecked Sendable {
            private let lock = NSLock()
            private(set) var items: [(URL, Int)] = []
            func append(_ item: (URL, Int)) {
                lock.withLock { items.append(item) }
            }
            var count: Int { lock.withLock { items.count } }
            subscript(index: Int) -> (URL, Int) {
                lock.withLock { items[index] }
            }
        }
        let box = CompletedBox()
        let completedSemaphore = DispatchSemaphore(value: 0)
        sink.setSegmentCompletedHandler { url, sequence, error in
            box.append((url, sequence))
            completedSemaphore.signal()
        }

        let firstURL = root.appendingPathComponent("segment-000000.m4a")
        let recorder = try WatchCaptureRecorder(url: firstURL, settings: aacSettings, source: source)
        // Duration 0.15s: the first packet stays in segment 0, and the second rotates.
        try sink.begin(recorder, duration: 0.15, autoRotate: true)

        sink.receive(try buffer(value: 0.1))
        sink.flushForTesting()
        sink.receive(try buffer(value: 0.2))

        XCTAssertEqual(completedSemaphore.wait(timeout: .now() + 2), .success)
        XCTAssertEqual(box.count, 1)
        XCTAssertEqual(box[0].0.lastPathComponent, "segment-000000.m4a")
        XCTAssertEqual(box[0].1, 0)

        // The first file must be finalized and readable on disk!
        let firstFile = try AVAudioFile(forReading: firstURL)
        XCTAssertGreaterThan(firstFile.length, 1000)

        // Continue feeding audio; it should now write to segment-000001.m4a seamlessly!
        sink.receive(try buffer(value: 0.3))
        sink.flushForTesting()

        let secondURL = root.appendingPathComponent("segment-000001.m4a")
        XCTAssertTrue(FileManager.default.fileExists(atPath: secondURL.path))
        XCTAssertNil(recorder.lastError, "unexpected capture failure: \(String(describing: recorder.lastError))")
        XCTAssertEqual(sink.currentSegmentURL(for: recorder.id)?.standardizedFileURL, secondURL.standardizedFileURL)

        sink.end(recorder, notify: false)
        XCTAssertEqual(sink.currentSegmentURL(for: recorder.id)?.standardizedFileURL, secondURL.standardizedFileURL,
                       "finishing must preserve the actual active segment identity")
        let secondFile = try AVAudioFile(forReading: secondURL)
        XCTAssertGreaterThan(secondFile.length, 1000)
        sink.shutdown()
    }

    func testRotationFailureReportsLastRealActiveSegmentExactly() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let firstURL = root.appendingPathComponent("segment-000000.m4a")
        try FileManager.default.createDirectory(at: root.appendingPathComponent("segment-000002.m4a"), withIntermediateDirectories: true)
        let source = WatchContinuousAudioCapture()
        let sink = WatchCaptureSink()
        let recorder = try WatchCaptureRecorder(url: firstURL, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 16000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: WatchAudioEncoding.bitRate], source: source)
        let reported = DispatchSemaphore(value: 0)
        final class ResultBox: @unchecked Sendable {
            private let lock = NSLock()
            private(set) var value: (URL, Int, Error?)?
            func set(_ value: (URL, Int, Error?)) { lock.withLock { self.value = value } }
        }
        let result = ResultBox()
        sink.setSegmentCompletedHandler { url, sequence, error in
            if error != nil { result.set((url, sequence, error)); reported.signal() }
        }
        try sink.begin(recorder, duration: 0.1, autoRotate: true)
        sink.receive(try buffer(value: 0.2)); sink.flushForTesting()
        sink.receive(try buffer(value: 0.3)); sink.flushForTesting()
        sink.receive(try buffer(value: 0.4))
        XCTAssertEqual(reported.wait(timeout: .now() + 2), .success)
        sink.flushForTesting()
        let failure = try XCTUnwrap(result.value)
        let secondURL = root.appendingPathComponent("segment-000001.m4a")
        XCTAssertEqual(failure.0.standardizedFileURL, secondURL.standardizedFileURL)
        XCTAssertEqual(failure.1, 1)
        XCTAssertNotNil(failure.2)
        XCTAssertEqual(sink.currentSegmentURL(for: recorder.id)?.standardizedFileURL, secondURL.standardizedFileURL)
        XCTAssertTrue((try AVAudioFile(forReading: secondURL)).length > 0)
        sink.shutdown()
    }

    func testShutdownDoesNotReplayPreviousSessionBuffers() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let sink = WatchCaptureSink()
        sink.receive(try buffer(value: 0.8))
        sink.shutdown()
        let recorder = try WatchCaptureRecorder(url: root.appendingPathComponent("new.wav"), settings: pcmSettings,
            source: WatchContinuousAudioCapture())
        try sink.begin(recorder, duration: 60)
        XCTAssertEqual(sink.snapshot(recorder.id).seconds, 0)
        sink.end(recorder, notify: false)
    }

    private var pcmSettings: [String: Any] { [AVFormatIDKey: kAudioFormatLinearPCM,
        AVSampleRateKey: 16000, AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 32,
        AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false] }
    private func buffer(value: Float) throws -> AVAudioPCMBuffer {
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4800))
        buffer.frameLength = 4800
        buffer.floatChannelData![0].initialize(repeating: value, count: 4800)
        return buffer
    }
}
