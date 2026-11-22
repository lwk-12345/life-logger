import AVFAudio
import Foundation
import XCTest
@testable import WatchCore

final class WatchAudioEncodingTests: XCTestCase {
    private func makeAAC(at url: URL, bitRate: Int) throws {
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 16_000, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 48_000))
        buffer.frameLength = 48_000
        let samples = try XCTUnwrap(buffer.floatChannelData)[0]
        for index in 0..<48_000 {
            samples[index] = Float(0.2 * sin(Double(index) * 2 * .pi * 440 / 16_000))
        }
        let file = try AVAudioFile(forWriting: url, settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 16_000,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: bitRate,
            AVEncoderAudioQualityKey: AVAudioQuality.medium.rawValue,
        ])
        try file.write(from: buffer)
    }

    func testFixedBitRateAndMeasuredAACRemain16kMono() throws {
        XCTAssertEqual(WatchAudioEncoding.bitRate, 48_000)
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let url = root.appendingPathComponent("new.m4a")
        try makeAAC(at: url, bitRate: WatchAudioEncoding.bitRate)
        let file = try AVAudioFile(forReading: url)
        XCTAssertEqual(file.processingFormat.sampleRate, 16_000)
        XCTAssertEqual(file.processingFormat.channelCount, 1)
        let rate = try XCTUnwrap(WatchAudioEncoding.measuredBitRate(at: url))
        XCTAssertGreaterThan(rate, 0)
        print("AAC measured audio_bitrate=\(rate), configured=\(WatchAudioEncoding.bitRate)")
    }

    func testLegacyQueuedAudioReadsOwnBitRateAndDecodesMissingField() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".m4a")
        defer { try? FileManager.default.removeItem(at: url) }
        try makeAAC(at: url, bitRate: WatchAudioEncoding.bitRate)
        let segment = AudioSegment(sessionID: "legacy", sequence: 0, startOffsetMilliseconds: 0,
                                   durationMilliseconds: 3_000, localURL: url, byteCount: 1, sha256: "x")
        let data = try JSONEncoder().encode(segment)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertNil(object["audioBitrate"])
        let decoded = try JSONDecoder().decode(AudioSegment.self, from: data)
        XCTAssertNil(decoded.audioBitrate)
        let header = try XCTUnwrap(decoded.audioBitrateHeaderValue)
        let rate = try XCTUnwrap(Int(header))
        XCTAssertEqual(rate, WatchAudioEncoding.measuredBitRate(at: url))
        XCTAssertNotEqual(rate, 64_000, "old pending audio must not be relabeled as V68")
        print("Legacy AAC measured audio_bitrate=\(rate)")
    }

    func testMeasuredBitRatePersistsAcrossLedgerReload() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let url = root.appendingPathComponent("source.m4a")
        try makeAAC(at: url, bitRate: WatchAudioEncoding.bitRate)
        let ledger = SegmentLedger(rootURL: root)
        _ = try await ledger.createSession(id: "new")
        let segment = try await ledger.appendSegment(sessionID: "new", sequence: 0, sourceURL: url,
                                                     startOffsetMilliseconds: 0, durationMilliseconds: 3_000)
        let rate = try XCTUnwrap(segment.audioBitrate)
        XCTAssertEqual(segment.audioBitrateHeaderValue, String(rate))
        let restored = try await SegmentLedger(rootURL: root).loadSession(id: "new")
        XCTAssertEqual(restored?.segments.first?.audioBitrate, rate)
        XCTAssertEqual(restored?.segments.first?.audioBitrateHeaderValue, String(rate))
    }

    func testUnreadableTelemetryDoesNotInvent64kOrPreventRetention() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let url = root.appendingPathComponent("unknown.m4a")
        try Data("test-audio-placeholder".utf8).write(to: url)
        XCTAssertNil(WatchAudioEncoding.measuredBitRate(at: url))
        let ledger = SegmentLedger(rootURL: root)
        _ = try await ledger.createSession(id: "unknown")
        let segment = try await ledger.appendSegment(sessionID: "unknown", sequence: 0, sourceURL: url,
                                                     startOffsetMilliseconds: 0, durationMilliseconds: 1_000)
        XCTAssertNil(segment.audioBitrate)
        XCTAssertNil(segment.audioBitrateHeaderValue)
        XCTAssertTrue(FileManager.default.fileExists(atPath: segment.localURL.path))
    }
}
