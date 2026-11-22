import XCTest
import Foundation
@testable import AirPassportReceiver
import PassportBridgeC

final class AirPassportReceiverTests: XCTestCase {

    // MARK: - Frame Parser Tests

    func testDecodeBeginFrame() throws {
        // v1 BEGIN: version=1, type=1, seq=7 (LE), time=99 (LE)
        let data = Data([1, 1, 7, 0, 99, 0, 0, 0])
        let frame = try PassportFrameParser.decode(data)
        XCTAssertEqual(frame.type, .begin)
        XCTAssertEqual(frame.sequence, 7)
        XCTAssertEqual(frame.monotonicMs, 99)
        XCTAssertEqual(frame.payload.count, 0)
    }

    func testDecodeEndFrame() throws {
        // v1 END: version=1, type=3, seq=8 (LE), time=100 (LE)
        let data = Data([1, 3, 8, 0, 100, 0, 0, 0])
        let frame = try PassportFrameParser.decode(data)
        XCTAssertEqual(frame.type, .end)
        XCTAssertEqual(frame.sequence, 8)
        XCTAssertEqual(frame.monotonicMs, 100)
        XCTAssertEqual(frame.payload.count, 0)
    }

    func testDecodeAudioFrame() throws {
        // v1 AUDIO: version=1, type=2, seq=0x1234, time=0x78563412 + 164 bytes payload
        var data = Data([1, 2, 0x34, 0x12, 0x12, 0x34, 0x56, 0x78])
        var payload = Data(repeating: 0, count: 164)
        payload[0] = 0xA5
        payload[1] = 0x5A
        data.append(payload)

        let frame = try PassportFrameParser.decode(data)
        XCTAssertEqual(frame.type, .audio)
        XCTAssertEqual(frame.sequence, 0x1234)
        XCTAssertEqual(frame.monotonicMs, 0x78563412)
        XCTAssertEqual(frame.payload.count, 164)
        XCTAssertEqual(frame.payload[0], 0xA5)
        XCTAssertEqual(frame.payload[1], 0x5A)
    }

    func testDecodeHighTimestamps() throws {
        // High timestamp: 0x80000000
        let highData = Data([1, 1, 0, 0, 0, 0, 0, 0x80])
        let highFrame = try PassportFrameParser.decode(highData)
        XCTAssertEqual(highFrame.monotonicMs, 0x80000000)

        // Max timestamp: 0xFFFFFFFF
        let maxData = Data([1, 3, 0, 0, 0xFF, 0xFF, 0xFF, 0xFF])
        let maxFrame = try PassportFrameParser.decode(maxData)
        XCTAssertEqual(maxFrame.monotonicMs, 0xFFFFFFFF)
    }

    func testRejectMalformedFrames() {
        // Less than 8 bytes header
        XCTAssertThrowsError(try PassportFrameParser.decode(Data([1, 1, 0]))) { error in
            XCTAssertEqual(error as? PassportFrameError, .malformed)
        }

        // Unsupported version
        XCTAssertThrowsError(try PassportFrameParser.decode(Data([2, 1, 0, 0, 0, 0, 0, 0]))) { error in
            XCTAssertEqual(error as? PassportFrameError, .unsupportedVersion)
        }

        // Unknown type
        XCTAssertThrowsError(try PassportFrameParser.decode(Data([1, 99, 0, 0, 0, 0, 0, 0]))) { error in
            XCTAssertEqual(error as? PassportFrameError, .unknownType)
        }

        // Audio frame with truncated payload (less than 164 bytes)
        var shortAudio = Data([1, 2, 0, 0, 0, 0, 0, 0])
        shortAudio.append(Data(repeating: 0, count: 163))
        XCTAssertThrowsError(try PassportFrameParser.decode(shortAudio)) { error in
            XCTAssertEqual(error as? PassportFrameError, .malformed)
        }
    }

    // MARK: - ADPCM Decoder Tests

    func testDecodeSilentBlock() throws {
        let decoder = PassportAudioDecoder()
        let silentBlock = Data(repeating: 0, count: 164)
        let pcm = try decoder.decodeBlock(silentBlock)
        XCTAssertEqual(pcm.count, 320)
        XCTAssertTrue(pcm.allSatisfy { $0 == 0 })
    }

    func testDecodeKnownQuantizationVector() throws {
        let decoder = PassportAudioDecoder()
        var block = Data(repeating: 0, count: 164)
        // Standard IMA quantization vector matching test_passport_ptt_adpcm.c
        block[4] = 0x70
        block[5] = 0x2F

        let decoded = try decoder.decodeBlock(block)
        XCTAssertEqual(decoded.count, 320)
        XCTAssertEqual(decoded[0], 0)
        XCTAssertEqual(decoded[1], 11)
        XCTAssertEqual(decoded[2], -19)
    }

    func testDecoderRejectsInvalidBlockSize() {
        let decoder = PassportAudioDecoder()
        XCTAssertThrowsError(try decoder.decodeBlock(Data(repeating: 0, count: 163))) { error in
            XCTAssertEqual(error as? PassportAudioDecoderError, .invalidBlockSize)
        }
        XCTAssertThrowsError(try decoder.decodeBlock(Data(repeating: 0, count: 165))) { error in
            XCTAssertEqual(error as? PassportAudioDecoderError, .invalidBlockSize)
        }
    }

    // MARK: - Key Dispatcher Tests

    func testKeyDispatcherTransitions() {
        let dispatcher = PassportKeyDispatcher(virtualKeyCode: 0x69)
        XCTAssertFalse(dispatcher.isKeyDown)

        XCTAssertTrue(dispatcher.handleKeyDown())
        XCTAssertTrue(dispatcher.isKeyDown)

        // Idempotent key down
        XCTAssertFalse(dispatcher.handleKeyDown())
        XCTAssertTrue(dispatcher.isKeyDown)

        XCTAssertTrue(dispatcher.handleKeyUp())
        XCTAssertFalse(dispatcher.isKeyDown)

        // Idempotent key up
        XCTAssertFalse(dispatcher.handleKeyUp())
        XCTAssertFalse(dispatcher.isKeyDown)
    }

    // MARK: - BLE Bridge Coordinator Tests

    final class MockDelegate: PassportBLEBridgeDelegate {
        var beganSequence: UInt16?
        var beganTime: UInt32?
        var audioCount = 0
        var endedSequence: UInt16?
        var endedTime: UInt32?

        func bridgeDidChangeState(_ bridge: PassportBLEBridge, state: PassportBLEBridge.State) {}

        func bridgeSessionDidBegin(_ bridge: PassportBLEBridge, sequence: UInt16, monotonicMs: UInt32) {
            beganSequence = sequence
            beganTime = monotonicMs
        }

        func bridgeAudioDidReceive(_ bridge: PassportBLEBridge, pcmSamples: [Int16], sequence: UInt16, monotonicMs: UInt32) {
            audioCount += 1
        }

        func bridgeSessionDidEnd(_ bridge: PassportBLEBridge, sequence: UInt16, monotonicMs: UInt32) {
            endedSequence = sequence
            endedTime = monotonicMs
        }
    }

    func testBridgeEndToEndFrameProcessing() {
        let bridge = PassportBLEBridge()
        let mockDelegate = MockDelegate()
        bridge.delegate = mockDelegate

        // Feed BEGIN
        let beginData = Data([1, 1, 1, 0, 10, 0, 0, 0])
        bridge.processIncomingData(beginData)
        XCTAssertEqual(mockDelegate.beganSequence, 1)
        XCTAssertEqual(mockDelegate.beganTime, 10)
        XCTAssertTrue(bridge.keyDispatcher.isKeyDown)

        // Feed AUDIO
        var audioData = Data([1, 2, 2, 0, 30, 0, 0, 0])
        audioData.append(Data(repeating: 0, count: 164))
        bridge.processIncomingData(audioData)
        XCTAssertEqual(mockDelegate.audioCount, 1)
        XCTAssertTrue(bridge.keyDispatcher.isKeyDown)

        // Feed END
        let endData = Data([1, 3, 3, 0, 50, 0, 0, 0])
        bridge.processIncomingData(endData)
        XCTAssertEqual(mockDelegate.endedSequence, 3)
        XCTAssertEqual(mockDelegate.endedTime, 50)
        XCTAssertFalse(bridge.keyDispatcher.isKeyDown)
    }

    // MARK: - WAV Writer Tests

    func testWAVHeaderLayout() {
        let header = PassportWAVWriter.createWAVHeader(pcmDataSize: 640, sampleRate: 16_000, channels: 1, bitsPerSample: 16)
        XCTAssertEqual(header.count, 44)

        // Chunk ID: RIFF
        let riff = String(decoding: header.subdata(in: 0..<4), as: UTF8.self)
        XCTAssertEqual(riff, "RIFF")

        // Chunk Size: 36 + 640 = 676
        let chunkSize = header.subdata(in: 4..<8).withUnsafeBytes { $0.load(as: UInt32.self).littleEndian }
        XCTAssertEqual(chunkSize, 676)

        // Format: WAVE
        let wave = String(decoding: header.subdata(in: 8..<12), as: UTF8.self)
        XCTAssertEqual(wave, "WAVE")

        // Subchunk1ID: fmt
        let fmt = String(decoding: header.subdata(in: 12..<16), as: UTF8.self)
        XCTAssertEqual(fmt, "fmt ")

        // AudioFormat: 1 (PCM)
        let audioFormat = header.subdata(in: 20..<22).withUnsafeBytes { $0.load(as: UInt16.self).littleEndian }
        XCTAssertEqual(audioFormat, 1)

        // Channels: 1
        let channels = header.subdata(in: 22..<24).withUnsafeBytes { $0.load(as: UInt16.self).littleEndian }
        XCTAssertEqual(channels, 1)

        // SampleRate: 16000
        let sampleRate = header.subdata(in: 24..<28).withUnsafeBytes { $0.load(as: UInt32.self).littleEndian }
        XCTAssertEqual(sampleRate, 16_000)

        // ByteRate: 32000
        let byteRate = header.subdata(in: 28..<32).withUnsafeBytes { $0.load(as: UInt32.self).littleEndian }
        XCTAssertEqual(byteRate, 32_000)

        // BitsPerSample: 16
        let bitsPerSample = header.subdata(in: 34..<36).withUnsafeBytes { $0.load(as: UInt16.self).littleEndian }
        XCTAssertEqual(bitsPerSample, 16)

        // Subchunk2ID: data
        let dataTag = String(decoding: header.subdata(in: 36..<40), as: UTF8.self)
        XCTAssertEqual(dataTag, "data")

        // Subchunk2Size: 640
        let subchunk2Size = header.subdata(in: 40..<44).withUnsafeBytes { $0.load(as: UInt32.self).littleEndian }
        XCTAssertEqual(subchunk2Size, 640)
    }

    func testWAVEncodeEmpty() {
        let wav = PassportWAVWriter.encodeWAV(samples: [])
        XCTAssertEqual(wav.count, 44)
    }

    func testWAVEncodeSamples() {
        let samples: [Int16] = [0, 100, -100, 32767, -32768]
        let wav = PassportWAVWriter.encodeWAV(samples: samples)
        XCTAssertEqual(wav.count, 44 + samples.count * 2)

        // Verify PCM payload matches
        let payload = wav.subdata(in: 44..<wav.count)
        payload.withUnsafeBytes { rawBuffer in
            let buffer = rawBuffer.bindMemory(to: Int16.self)
            XCTAssertEqual(buffer.count, samples.count)
            for i in 0..<samples.count {
                XCTAssertEqual(buffer[i], samples[i])
            }
        }
    }

    // MARK: - Audio Stream Player Tests

    func testAudioStreamPlayerLifecycle() {
        let player = PassportAudioStreamPlayer()
        XCTAssertTrue(player.isOutputRunning, "AudioUnit must be running to maintain CoreAudio virtual clock")
        player.startSession()
        let silentSamples = [Int16](repeating: 0, count: 320)
        player.write(samples: silentSamples)
        player.stopSession(immediate: true)
        XCTAssertTrue(player.isOutputRunning, "AudioUnit keeps running to maintain virtual clock")
    }

    func testVirtualAudioDeviceDetection() {
        // Verification that query does not throw or crash
        _ = PassportAudioStreamPlayer.findVirtualAudioDevice()
    }
}

// MARK: - 今日待办 BLE 下发协议测试（2026-10-02）

final class PassportTodoSyncTests: XCTestCase {

    // MARK: 帧编码

    /// 协议：`[0x02][index][total][status][title_len][title utf8...]`
    func testTodoFrameLayout() throws {
        let frame = PassportTodoProtocol.encodeFrame(index: 2,
                                                     total: 4,
                                                     status: .inProgress,
                                                     title: "买牛奶")
        XCTAssertEqual(frame[0], 0x02, "首字节必须是魔数 0x02")
        XCTAssertEqual(frame[1], 2, "index")
        XCTAssertEqual(frame[2], 4, "total")
        XCTAssertEqual(frame[3], 1, "status: 1 = 进行中")
        XCTAssertEqual(frame[4], 9, "title_len = 3 个汉字的 UTF-8 字节数")
        XCTAssertEqual(frame.count, 5 + 9)
        XCTAssertEqual(String(decoding: frame[5...], as: UTF8.self), "买牛奶")
    }

    /// 长度 4 → 校时；长度 >= 5 且首字节 0x02 → 待办帧。清空帧必须落在后者。
    func testClearFrameShape() throws {
        let frame = PassportTodoProtocol.clearFrame()
        XCTAssertEqual(frame.count, 5)
        XCTAssertEqual(frame[0], 0x02)
        XCTAssertEqual(frame[1], 0, "index")
        XCTAssertEqual(frame[2], 0, "total = 0 表示清空")
        XCTAssertEqual(frame[3], 0, "status")
        XCTAssertEqual(frame[4], 0, "title_len")
    }

    /// 任何待办帧都不能是 4 字节（否则设备会把它当成校时帧）。
    func testTodoFramesAreNeverMistakenForTimeSync() throws {
        let titles = ["", "a", "买牛奶", String(repeating: "汉", count: 40)]
        for title in titles {
            let frame = PassportTodoProtocol.encodeFrame(index: 0, total: 1, status: .pending, title: title)
            XCTAssertGreaterThanOrEqual(frame.count, PassportTodoProtocol.minFrameLength)
            XCTAssertNotEqual(frame.count, 4)
            XCTAssertEqual(frame[0], 0x02)
        }
    }

    // MARK: UTF-8 边界截断 + 截断标记（不能切碎汉字；截断必须补 …）

    /// ① 短标题不动：没截断 → 原样返回，也不会凭空多出省略号。
    func testShortTitleIsUntouched() throws {
        XCTAssertEqual(PassportTodoProtocol.truncateTitle("早点睡"), "早点睡")
        XCTAssertFalse(PassportTodoProtocol.truncateTitle("早点睡").hasSuffix("…"))
    }

    /// ② 恰好等于上限：整条装得下 → **不算截断**，绝不补省略号。
    func testTitleExactlyAtLimitKeepsNoEllipsis() throws {
        let exact = String(repeating: "汉", count: 20) // 20 × 3 = 60 字节
        XCTAssertEqual(exact.utf8.count, PassportTodoProtocol.maxTitleBytes)
        let result = PassportTodoProtocol.truncateTitle(exact)
        XCTAssertEqual(result, exact)
        XCTAssertFalse(result.hasSuffix("…"), "恰好等于上限不是截断，不该补省略号")
        XCTAssertEqual(result.utf8.count, PassportTodoProtocol.maxTitleBytes)
    }

    /// ③ 超长 → 末尾必须是 `…`，且补完总字节数仍 <= 上限。
    /// 用真机日志里的那条：「你要记得，就是每个方向的都要严格按照那个」（截断后正好 20 字 = 60 字节）。
    func testOversizedTitleGetsEllipsisWithinLimit() throws {
        XCTAssertEqual(PassportTodoProtocol.truncationEllipsis, "\u{2026}")
        XCTAssertEqual(PassportTodoProtocol.truncationEllipsis.utf8.count, 3, "… 是 3 字节")

        let real = "你要记得，就是每个方向的都要严格按照那个方向去检查"
        let result = PassportTodoProtocol.truncateTitle(real)
        XCTAssertTrue(result.hasSuffix("…"), "被截断的标题必须以 … 结尾")
        XCTAssertEqual(result, String(real.prefix(19)) + "…", "退 1 个字给省略号腾位置")
        XCTAssertEqual(result.utf8.count, 60, "19 字 × 3 + … 3 = 60，正好用满上限")
        XCTAssertLessThanOrEqual(result.utf8.count, PassportTodoProtocol.maxTitleBytes)

        // 进帧后：title_len 含省略号、仍 <= 60，且载荷解码回来就是截断结果。
        let frame = PassportTodoProtocol.encodeFrame(index: 2, total: 4, status: .pending, title: real)
        XCTAssertEqual(Int(frame[4]), result.utf8.count)
        XCTAssertLessThanOrEqual(Int(frame[4]), PassportTodoProtocol.maxTitleBytes)
        XCTAssertEqual(frame.count, 5 + Int(frame[4]))
        XCTAssertEqual(String(decoding: frame[5...], as: UTF8.self), result)
    }

    /// ④ 多字节字符边界不被切断：正文必须是原标题的**整字前缀**，不能出现半个汉字/半个 emoji。
    func testTitleTruncationStopsOnCharacterBoundary() throws {
        // 60 字节刚好 20 个汉字（3 字节/字）；第 21 个字必须整字丢弃。
        let title = String(repeating: "汉", count: 21)
        let truncated = PassportTodoProtocol.truncateTitle(title)
        XCTAssertEqual(truncated.count, 20, "19 个汉字 + 1 个省略号")
        XCTAssertEqual(truncated.utf8.count, 60)
        XCTAssertEqual(truncated, String(repeating: "汉", count: 19) + "…")
        // 去掉省略号后的正文，必须是原标题的整字前缀（切在字符边界上）
        let body = String(truncated.dropLast())
        XCTAssertTrue(title.hasPrefix(body), "正文必须整字取自原标题")
        XCTAssertEqual(body.utf8.count, 57, "57 = 19 × 3，字节数是 3 的整数倍 → 没有半个汉字")
        // 截断结果本身必须是合法 UTF-8（没有半个码点）
        XCTAssertFalse(truncated.unicodeScalars.contains { $0.value == 0xFFFD })
    }

    /// emoji 是 4 字节：15 个正好 60 字节（第 16 个放不下）→ 再退 1 个给 `…` → 14 个 + `…` = 59。
    func testTitleTruncationWithEmoji() throws {
        let emoji = String(repeating: "😀", count: 16)
        let truncated = PassportTodoProtocol.truncateTitle(emoji)
        XCTAssertEqual(truncated.count, 15)
        XCTAssertEqual(truncated, String(repeating: "😀", count: 14) + "…")
        XCTAssertEqual(truncated.utf8.count, 59)
        XCTAssertLessThanOrEqual(truncated.utf8.count, PassportTodoProtocol.maxTitleBytes)
    }

    /// 19 个汉字 = 57 字节，再加 1 个 emoji = 61 > 60 → 整字只留 19 个汉字，
    /// 剩下 3 字节刚好给省略号：19 字 + `…` = 60。
    func testTitleTruncationMixedWidths() throws {
        let mixed = String(repeating: "汉", count: 19) + "😀"
        let truncated = PassportTodoProtocol.truncateTitle(mixed)
        XCTAssertEqual(truncated, String(repeating: "汉", count: 19) + "…")
        XCTAssertEqual(truncated.utf8.count, 60)
        XCTAssertTrue(mixed.hasPrefix(String(truncated.dropLast())))
    }

    /// 极端：上限小到连「一个字符 + `…`」都放不下 → 按上限整字截断、**不补**省略号（并打日志）。
    func testTinyLimitFallsBackToPlainBoundaryTruncation() throws {
        // 5 字节放不下「汉 + …」（3 + 3 = 6 > 5）→ 保留整字「汉」，不补省略号
        let five = PassportTodoProtocol.truncateTitle("汉字", maxBytes: 5)
        XCTAssertEqual(five, "汉")
        XCTAssertLessThanOrEqual(five.utf8.count, 5)
        // 2 字节连第一个字都放不下 → 空标题（同样不补省略号）
        XCTAssertEqual(PassportTodoProtocol.truncateTitle("汉字", maxBytes: 2), "")
        XCTAssertEqual(PassportTodoProtocol.truncateTitle("汉字", maxBytes: 0), "")
        // 6 字节刚好放得下「汉 + …」→ 走正常分支，补省略号（原标题 3 个字，确实被截断）
        let six = PassportTodoProtocol.truncateTitle("汉字汉", maxBytes: 6)
        XCTAssertEqual(six, "汉…")
        XCTAssertEqual(six.utf8.count, 6)
        // 同样是 6 字节上限，但标题**恰好**装得下 → 不是截断，不加省略号
        XCTAssertEqual(PassportTodoProtocol.truncateTitle("汉字", maxBytes: 6), "汉字")
    }

    /// 正文自带 `…`（上游 OpenClaw 待办标题常见，实测「我只看它的一个形态…」）
    /// 且确实超长被截断时，不再叠加第二个省略号 → 屏幕上不出现「……」，末尾仍是 `…`。
    func testTruncationDoesNotDoubleUpstreamEllipsis() throws {
        let title = String(repeating: "汉", count: 18) + "…" + "尾巴还把字数撑过上限"
        XCTAssertGreaterThan(title.utf8.count, PassportTodoProtocol.maxTitleBytes)
        let truncated = PassportTodoProtocol.truncateTitle(title)
        XCTAssertTrue(truncated.hasSuffix("…"))
        XCTAssertFalse(truncated.hasSuffix("……"), "不能出现两个连续省略号")
        XCTAssertLessThanOrEqual(truncated.utf8.count, PassportTodoProtocol.maxTitleBytes)
        XCTAssertEqual(truncated, String(repeating: "汉", count: 18) + "…")
    }

    /// 编码必须自己截断：超长标题进帧后 title_len 依然 <= 60，且末尾带省略号。
    func testEncodedFrameRespectsTitleLimit() throws {
        let frame = PassportTodoProtocol.encodeFrame(index: 0,
                                                     total: 1,
                                                     status: .pending,
                                                     title: String(repeating: "汉", count: 50))
        XCTAssertLessThanOrEqual(Int(frame[4]), PassportTodoProtocol.maxTitleBytes)
        XCTAssertEqual(frame.count, 5 + Int(frame[4]))
        let decoded = String(decoding: frame[5...], as: UTF8.self)
        XCTAssertEqual(decoded, String(repeating: "汉", count: 19) + "…")
        XCTAssertTrue(decoded.hasSuffix("…"))
    }

    // MARK: 字段映射（基于真实 /api/reminders 返回）

    /// 实测原文（172.20.10.3:8765/api/reminders，2026-10-02）：共 6 条 = 4 条 pending + 2 条 dismissed。
    /// 标题已脱敏缩写；字段名与结构完全照搬真实返回。
    func testMapperOnRealRemindersResponse() throws {
        let json = """
        {"backend":"local","reminders":[
          {"review_status":"confirmed","task_key":"evt_21926803048b","title":"我只看它的一个形态…","event_id":"evt_21926803048b","job_id":"job-20260921-232252-9007aa8c","artifact_path":"files/fc22/result/2026-09-21/events.json","backend":"local","source_status":"pending","updated_at":"2026-10-02T00:47:39.572886+08:00","created_at":"2026-09-22T20:38:09.066083+08:00","remind_at":null,"status":"unscheduled"},
          {"review_status":"confirmed","task_key":"evt_a9211deb8849","title":"难道说需要我重新打开一下VPN…","event_id":"evt_a9211deb8849","job_id":"job-20260921-232252-9007aa8c","artifact_path":"files/fc22/result/2026-09-21/events.json","backend":"local","source_status":"pending","updated_at":"2026-10-02T00:47:39.572886+08:00","created_at":"2026-09-22T20:38:09.066083+08:00","remind_at":null,"status":"unscheduled"},
          {"review_status":"confirmed","task_key":"evt_b64f9707656f","title":"你先把你做的能够做好…","event_id":"evt_b64f9707656f","job_id":"job-20260921-232252-9007aa8c","artifact_path":"files/fc22/result/2026-09-21/events.json","backend":"local","source_status":"pending","updated_at":"2026-10-02T00:47:39.572886+08:00","created_at":"2026-09-22T20:38:09.066083+08:00","remind_at":null,"status":"unscheduled"},
          {"task_key":"evt_ac4a6dead545","title":"我记得我是一直开着的…","event_id":"evt_ac4a6dead545","job_id":"job-20260928-224109-4fb1da8f","artifact_path":"files/6699/result/2026-09-28/events.json","backend":"local","source_status":"dismissed","updated_at":"2026-10-02T00:47:39.572886+08:00","created_at":"2026-09-29T00:20:24.416097+08:00","remind_at":null,"status":"dismissed"},
          {"task_key":"job-20260928-224109-4fb1da8f:files/6699/result/2026-09-28/events.json:evt_ac4a6dead545","title":"我记得我是一直开着的…","event_id":"evt_ac4a6dead545","job_id":"job-20260928-224109-4fb1da8f","artifact_path":"files/6699/result/2026-09-28/events.json","backend":"local","source_status":"dismissed","updated_at":"2026-10-02T00:47:39.572886+08:00","created_at":"2026-09-29T00:20:37.959296+08:00","remind_at":null,"status":"dismissed"},
          {"review_status":"confirmed","task_key":"evt_5433a4f58470","title":"你要记得，就是每个方向的…","event_id":"evt_5433a4f58470","job_id":"job-20260929-042210-9de30d6d","artifact_path":"files/6e85/result/2026-09-28/events.json","backend":"local","source_status":"pending","updated_at":"2026-10-02T00:47:39.572886+08:00","created_at":"2026-09-29T04:28:42.072888+08:00","remind_at":null,"status":"unscheduled"}
        ]}
        """
        let items = try PassportTodoMapper.items(fromRemindersResponse: Data(json.utf8))
        // 6 条里 2 条 dismissed 被过滤 → 剩 4 条（是"映射结果恰好 4 条"，不是被截到 4 条），
        // 且换源后条数 > 0。
        XCTAssertEqual(items.count, 4)
        XCTAssertEqual(items.map(\.title), [
            "我只看它的一个形态…",
            "难道说需要我重新打开一下VPN…",
            "你先把你做的能够做好…",
            "你要记得，就是每个方向的…"
        ])
        XCTAssertEqual(items.map(\.status), [.pending, .pending, .pending, .pending])
    }

    /// [2026-10-02 语义变更] 映射层新增严格模式：只下发 review_status=confirmed 或
    /// source_type=manual 的条目。本用例原本测「8 条 pending 不被截到 4」，
    /// 现在改为测「**8 条已确认**的 pending 不被截到 4」—— 被验证的性质（count 是过滤后
    /// 的真实条数、不受帧层 4 条上限影响）完全保留。未确认条目改由
    /// `testMapperDropsAllUnconfirmedStrictMode` 覆盖。
    ///
    /// 实测原文（172.20.10.3:8765/api/reminders，2026-10-02 当天晚些时候复测）：
    /// 共 **10** 条 = 8 条 `source_status=pending` + 2 条 `dismissed`（两条 dismissed 是同一件事
    /// 的重复条目，task_key 一个是 `evt_…`、一个是 `job-…:…:evt_…`）。
    /// 这正是暴露「count 被钳到 4」的现场：真实待办 8 条，设备只能显示 4 条。
    /// 标题、状态、顺序全部照搬真实返回；`task_key`/时间戳等与映射无关的字段做了精简。
    func testMapperOnLiveTenReminderResponseKeepsAllEightPending() throws {
        let json = """
        {"backend":"local","reminders":[
          {"review_status":"confirmed","task_key":"evt_1","title":"我只看它的一个形态，谁更便携，然后谁可以满足这个录音一整天的一个需要","source_status":"pending","status":"unscheduled"},
          {"review_status":"confirmed","task_key":"evt_2","title":"难道说需要我重新打开一下VPN","source_status":"pending","status":"unscheduled"},
          {"review_status":"confirmed","task_key":"evt_3","title":"你先把你做的能够做好，然后需要我的时候咱们再配合","source_status":"pending","status":"unscheduled"},
          {"task_key":"evt_4","title":"我记得我是一直开着的，不知道为啥容易退出","source_status":"dismissed","status":"dismissed"},
          {"task_key":"job-1:files/x/events.json:evt_4","title":"我记得我是一直开着的，不知道为啥容易退出","source_status":"dismissed","status":"dismissed"},
          {"review_status":"confirmed","task_key":"evt_5","title":"你要记得，就是每个方向的都要严格按照那个","source_status":"pending","status":"unscheduled"},
          {"review_status":"confirmed","task_key":"evt_6","title":"我记得那个Windows上面可以不需要去做签名跟授权","source_status":"pending","status":"unscheduled"},
          {"review_status":"confirmed","task_key":"evt_7","title":"我记得那个小米的遥控器就是","source_status":"pending","status":"unscheduled"},
          {"review_status":"confirmed","task_key":"evt_8","title":"我记得你之前说过，你之前说过他的一个算了，他的休眠时间改为五分钟吧","source_status":"pending","status":"unscheduled"},
          {"review_status":"confirmed","task_key":"evt_9","title":"呃，不需要去介入专门的一个软件，就映射快映射按按键就可以了","source_status":"pending","status":"unscheduled"}
        ]}
        """
        let items = try PassportTodoMapper.items(fromRemindersResponse: Data(json.utf8))
        XCTAssertEqual(items.count, 8, "10 条里 2 条 dismissed 被丢弃 → 8 条已确认待办（不再截到 4）")
        XCTAssertEqual(items.map(\.status), Array(repeating: .pending, count: 8), "8 条全是 pending")
        XCTAssertFalse(items.contains { $0.status == .completed })
    }

    /// source_status 决定协议状态字节；status（提醒自身状态）只参与过滤。
    ///
    /// [2026-10-02 语义变更] 映射层**不再**截到 4 条：7 条里 2 条被过滤 → 返回 5 条。
    /// 截断只发生在帧层（pushPlan 只发前 4 条待办帧），否则状态帧的 count 天生 ≤4。
    func testMapperStatusesAndOrder() throws {
        let json = """
        {"backend":"local","reminders":[
          {"review_status":"confirmed","task_key":"a","source_status":"pending","status":"unscheduled","title":"第一条"},
          {"review_status":"confirmed","task_key":"b","source_status":"in_progress","status":"scheduled","title":"第二条"},
          {"review_status":"confirmed","task_key":"c","source_status":"blocked","status":"sent","title":"第三条"},
          {"task_key":"d","source_status":"completed","status":"completed","title":"已完成不上屏"},
          {"task_key":"e","source_status":"dismissed","status":"dismissed","title":"已忽略不上屏"},
          {"review_status":"confirmed","task_key":"f","source_status":"pending","status":"unscheduled","title":"第四条"},
          {"review_status":"confirmed","task_key":"g","source_status":"pending","status":"unscheduled","title":"第五条（映射层保留，帧层才截到 4 条）"}
        ]}
        """
        let items = try PassportTodoMapper.items(fromRemindersResponse: Data(json.utf8))
        XCTAssertEqual(items.count, 5, "映射层保留全部未完成条目（2 条 completed/dismissed 被丢弃）")
        XCTAssertEqual(items.map(\.title), ["第一条", "第二条", "第三条", "第四条", "第五条（映射层保留，帧层才截到 4 条）"])
        // blocked → 0 待办；scheduled/sent 不影响状态字节（提醒排没排与待办做没做无关）
        XCTAssertEqual(items.map(\.status), [.pending, .inProgress, .pending, .pending, .pending])
        XCTAssertFalse(items.contains { $0.status == .completed })

        // 帧层仍然只发 4 条，且 total 写实际发送条数
        let plan = PassportTodoProtocol.pushPlan(items: items,
                                                 status: PassportConnectionStatus(state: .connected,
                                                                                  count: items.count,
                                                                                  ageSeconds: 0))
        XCTAssertEqual(plan.todoFrames.count, 1 + PassportTodoProtocol.maxItems, "1 条清空帧 + 4 条待办帧")
        XCTAssertEqual(plan.todoFrames[1][2], 4, "total = 实际发送条数 4")
    }

    /// 防御性规则：只要任一侧是 dismissed/completed 就不上屏。
    func testMapperFiltersWhenEitherSideIsClosed() throws {
        let json = """
        {"backend":"local","reminders":[
          {"task_key":"a","source_status":"pending","status":"dismissed","title":"提醒侧忽略"},
          {"task_key":"b","source_status":"pending","status":"completed","title":"提醒侧已完成"},
          {"review_status":"confirmed","task_key":"c","source_status":"pending","status":"unscheduled","title":"保留"}
        ]}
        """
        let items = try PassportTodoMapper.items(fromRemindersResponse: Data(json.utf8))
        XCTAssertEqual(items.map(\.title), ["保留"])
    }

    /// source_status 缺失时退回提醒侧 status：unscheduled/scheduled/sent 都算「待办」。
    func testMapperFallsBackToReminderStatusWhenSourceMissing() throws {
        let json = """
        {"backend":"local","reminders":[
          {"review_status":"confirmed","task_key":"a","status":"unscheduled","title":"未排提醒"},
          {"review_status":"confirmed","task_key":"b","status":"scheduled","title":"已排提醒"},
          {"review_status":"confirmed","task_key":"c","status":"sent","title":"已发提醒"}
        ]}
        """
        let items = try PassportTodoMapper.items(fromRemindersResponse: Data(json.utf8))
        XCTAssertEqual(items.map(\.status), [.pending, .pending, .pending])
    }

    func testMapperSkipsEmptyTitlesAndMissingFields() throws {
        let json = """
        {"backend":"local","reminders":[{"task_key":"a","source_status":"pending","title":"   "},{"task_key":"b","source_status":"pending"}]}
        """
        let items = try PassportTodoMapper.items(fromRemindersResponse: Data(json.utf8))
        XCTAssertTrue(items.isEmpty)
    }

    // MARK: 严格模式：只下发「人确认过的」待办（2026-10-02）

    /// ① `review_status == "confirmed"` → 保留并下发。
    func testMapperKeepsConfirmedItem() throws {
        let json = """
        {"backend":"local","reminders":[
          {"task_key":"evt_ok","title":"已确认的待办","source_status":"pending","status":"unscheduled","review_status":"confirmed","source_type":"audio"}
        ]}
        """
        let items = try PassportTodoMapper.items(fromRemindersResponse: Data(json.utf8))
        XCTAssertEqual(items.map(\.title), ["已确认的待办"])
    }

    /// ② `source_type == "manual"` → 保留（人手工添加的条目，未必带 review_status）。
    func testMapperKeepsManualItem() throws {
        let json = """
        {"backend":"local","reminders":[
          {"task_key":"manual_1","title":"我手工加的待办","source_status":"pending","status":"unscheduled","source_type":"manual"}
        ]}
        """
        let items = try PassportTodoMapper.items(fromRemindersResponse: Data(json.utf8))
        XCTAssertEqual(items.map(\.title), ["我手工加的待办"])
    }

    /// ③ 未确认（`review_status` 是 `unreviewed`）→ 丢弃。
    /// 这是本次改动的核心：设备不再显示管线自动抽取的语音碎片候选。
    func testMapperDropsUnconfirmedCandidate() throws {
        let json = """
        {"backend":"local","reminders":[
          {"task_key":"evt_auto","title":"我记得那个小米的遥控器就是","source_status":"pending","status":"unscheduled","review_status":"unreviewed","source_type":"audio"},
          {"task_key":"evt_ok","title":"已确认的待办","source_status":"pending","status":"unscheduled","review_status":"confirmed","source_type":"audio"}
        ]}
        """
        let items = try PassportTodoMapper.items(fromRemindersResponse: Data(json.utf8))
        XCTAssertEqual(items.map(\.title), ["已确认的待办"], "未确认的候选必须被丢弃")
    }

    /// ④ 两个键**完全缺失**（服务端未升级）→ 严格模式，全部丢弃。
    /// 契约：宁可设备显示空列表，也不显示未确认的候选。
    func testMapperDropsAllUnconfirmedStrictMode() throws {
        let json = """
        {"backend":"local","reminders":[
          {"task_key":"a","title":"第一条","source_status":"pending","status":"unscheduled"},
          {"task_key":"b","title":"第二条","source_status":"pending","status":"unscheduled"}
        ]}
        """
        let items = try PassportTodoMapper.items(fromRemindersResponse: Data(json.utf8))
        XCTAssertTrue(items.isEmpty, "缺 review_status/source_type 时按未确认处理，全部丢弃")
    }

    /// ⑤ 状态帧的 `count` 是**过滤后**的真实条数，不是原始条数。
    func testStatusCountReflectsFilteredItems() throws {
        let json = """
        {"backend":"local","reminders":[
          {"task_key":"c1","title":"已确认一","source_status":"pending","status":"unscheduled","review_status":"confirmed"},
          {"task_key":"c2","title":"已确认二","source_status":"pending","status":"unscheduled","review_status":"confirmed"},
          {"task_key":"c3","title":"已确认三","source_status":"pending","status":"unscheduled","review_status":"confirmed"},
          {"task_key":"u1","title":"未确认一","source_status":"pending","status":"unscheduled","review_status":"unreviewed"},
          {"task_key":"u2","title":"未确认二","source_status":"pending","status":"unscheduled","review_status":"unreviewed"},
          {"task_key":"u3","title":"未确认三","source_status":"pending","status":"unscheduled","review_status":"unreviewed"},
          {"task_key":"u4","title":"未确认四","source_status":"pending","status":"unscheduled","review_status":"unreviewed"},
          {"task_key":"u5","title":"未确认五","source_status":"pending","status":"unscheduled","review_status":"unreviewed"}
        ]}
        """
        let items = try PassportTodoMapper.items(fromRemindersResponse: Data(json.utf8))
        XCTAssertEqual(items.count, 3, "8 条里只有 3 条已确认")

        let plan = PassportTodoProtocol.pushPlan(
            items: items,
            status: PassportConnectionStatus(state: .connected, count: items.count, ageSeconds: 0))
        // 帧层只发前 maxItems(4) 条待办帧 —— 3 条已确认全部发得下
        XCTAssertEqual(plan.todoFrames.count, 1 + 3, "1 条清空帧 + 3 条待办帧")
        // 状态帧：[0x03][state][count][age_lo][age_hi] —— count 必须是 3（过滤后），不是 8
        let statusFrame = plan.statusFrame
        XCTAssertEqual(statusFrame[0], 0x03)
        XCTAssertEqual(statusFrame[2], 3, "count = 过滤后的真实条数，不是原始 8 条")
    }

    func testMapperHandlesMissingRemindersKey() throws {
        let items = try PassportTodoMapper.items(fromRemindersResponse: Data("{}".utf8))
        XCTAssertTrue(items.isEmpty)
    }

    func testProtocolStatusMapping() throws {
        XCTAssertEqual(PassportTodoMapper.protocolStatus(from: "pending"), .pending)
        XCTAssertEqual(PassportTodoMapper.protocolStatus(from: "in_progress"), .inProgress)
        XCTAssertEqual(PassportTodoMapper.protocolStatus(from: "completed"), .completed)
        XCTAssertEqual(PassportTodoMapper.protocolStatus(from: "blocked"), .pending)
        XCTAssertNil(PassportTodoMapper.protocolStatus(from: "dismissed"))
        XCTAssertNil(PassportTodoMapper.protocolStatus(from: "DISMISSED"))
        // 提醒侧状态：都不是「待办做完了」，一律按待办
        XCTAssertEqual(PassportTodoMapper.protocolStatus(from: "unscheduled"), .pending)
        XCTAssertEqual(PassportTodoMapper.protocolStatus(from: "scheduled"), .pending)
        XCTAssertEqual(PassportTodoMapper.protocolStatus(from: "sent"), .pending)
        // 未知/缺失一律按"待办"，宁多显示一条也不静默丢
        XCTAssertEqual(PassportTodoMapper.protocolStatus(from: nil), .pending)
        XCTAssertEqual(PassportTodoMapper.protocolStatus(from: "什么鬼状态"), .pending)
    }

    // MARK: 地址与令牌解析

    func testCandidateURLsHonourOverrideAndPreferLoopback() throws {
        let urls = PassportTodoEndpoint.candidateURLs(environment: [
            PassportTodoEndpoint.urlOverrideKey: "http://192.168.1.9:8765/api/reminders"
        ])
        XCTAssertEqual(urls.first?.absoluteString, "http://192.168.1.9:8765/api/reminders")
        // 默认端点必须是 /api/reminders（root 拍板换源）
        XCTAssertTrue(urls.contains { $0.absoluteString == "http://127.0.0.1:8765/api/reminders" })
        XCTAssertFalse(urls.contains { $0.absoluteString.contains("/api/tasks") })
    }

    func testAccessTokenPrefersEnvironmentOverride() throws {
        let token = PassportTodoEndpoint.accessToken(environment: [
            PassportTodoEndpoint.tokenOverrideKey: "unit-test-token"
        ])
        XCTAssertEqual(token, "unit-test-token")
    }

    func testLocalIPv4AddressesArePlausible() throws {
        // 只验证不崩溃 + 元素形如 IPv4；具体地址随网络环境变化，不做断言。
        for address in PassportTodoEndpoint.localIPv4Addresses() {
            XCTAssertEqual(address.split(separator: ".").count, 4, "应只返回 IPv4: \(address)")
            XCTAssertFalse(address.hasPrefix("127."), "应排除回环地址")
        }
    }
}

// MARK: - 连接状态帧协议测试（2026-10-02，设备第 3 屏「Life Logger 连接状态」）

final class PassportConnectionStatusTests: XCTestCase {

    // MARK: 帧编码：`[0x03][state][count][age_lo][age_hi]`

    /// 例 1（成功）：state=1、count=本次条数、age=0。
    func testConnectedFrameLayout() throws {
        let frame = PassportConnectionStatusProtocol.encodeFrame(state: .connected,
                                                                 count: 3,
                                                                 ageSeconds: 0)
        XCTAssertEqual(frame.count, PassportConnectionStatusProtocol.frameLength)
        XCTAssertEqual(frame.count, 5)
        XCTAssertEqual(frame[0], 0x03, "首字节必须是魔数 0x03")
        XCTAssertEqual(frame[1], 0x01, "state: 1 = 已连接 / 拉取成功")
        XCTAssertEqual(frame[2], 3, "count = 本次映射出的条数")
        XCTAssertEqual(frame[3], 0x00, "age_lo")
        XCTAssertEqual(frame[4], 0x00, "age_hi")
    }

    /// 例 2（失败）：state=0、count=上次已知条数、age=距上次成功的秒数（小端）。
    func testDisconnectedFrameCarriesLastKnownCountAndAge() throws {
        let frame = PassportConnectionStatusProtocol.encodeFrame(state: .disconnected,
                                                                 count: 2,
                                                                 ageSeconds: 90)
        XCTAssertEqual(frame.count, 5)
        XCTAssertEqual(frame[0], 0x03)
        XCTAssertEqual(frame[1], 0x00, "state: 0 = 未连接 / 拉取失败")
        XCTAssertEqual(frame[2], 2, "count 沿用上次已知条数，失败不清零")
        XCTAssertEqual(frame[3], 90, "age_lo = 90 & 0xFF")
        XCTAssertEqual(frame[4], 0, "age_hi")
    }

    /// 例 3（age 封顶）：>65535 一律写 65535（0xFFFF），边界值 65535 原样保留。
    func testAgeIsCappedAtUInt16Max() throws {
        XCTAssertEqual(PassportConnectionStatusProtocol.clampAge(65_535), 65_535)
        XCTAssertEqual(PassportConnectionStatusProtocol.clampAge(65_536), 65_535)
        XCTAssertEqual(PassportConnectionStatusProtocol.clampAge(86_400 * 30), 65_535)
        XCTAssertEqual(PassportConnectionStatusProtocol.clampAge(-1), 0, "负数按 0")

        let capped = PassportConnectionStatusProtocol.encodeFrame(state: .disconnected,
                                                                  count: 0,
                                                                  ageSeconds: 999_999)
        XCTAssertEqual(capped[3], 0xFF, "封顶后 age_lo = 0xFF")
        XCTAssertEqual(capped[4], 0xFF, "封顶后 age_hi = 0xFF")
    }

    /// age 是小端 uint16：300 = 0x012C → lo=0x2C、hi=0x01；65534 → FE FF。
    func testAgeIsLittleEndianUInt16() throws {
        let frame = PassportConnectionStatusProtocol.encodeFrame(state: .connected,
                                                                 count: 0,
                                                                 ageSeconds: 300)
        XCTAssertEqual(frame[3], 0x2C, "age_lo")
        XCTAssertEqual(frame[4], 0x01, "age_hi")

        let nearMax = PassportConnectionStatusProtocol.encodeFrame(state: .connected,
                                                                   count: 0,
                                                                   ageSeconds: 65_534)
        XCTAssertEqual(nearMax[3], 0xFE)
        XCTAssertEqual(nearMax[4], 0xFF)
    }

    /// count 是**真实待办条数**，不再钳到设备槽位数 4：
    /// 8 条 pending → 帧里 count=8（这正是 2026-10-02 真机暴露的体验缺口：
    /// 真实 8 条、屏幕只显示 4 条，用户却看到「今日待办 4 条」）。
    func testCountReportsRealTotalOfEightPending() throws {
        let items = try PassportTodoMapper.items(fromRemindersResponse: Self.remindersJSON(pending: 8, dismissed: 2))
        XCTAssertEqual(items.count, 8, "8 条 pending 全部保留（2 条 dismissed 被丢弃）")

        let status = PassportConnectionStatus(state: .connected, count: items.count, ageSeconds: 0)
        XCTAssertEqual(status.frame[2], 8, "count = 真实条数 8（>4 也照实报）")

        // 待办帧本身仍只发最多 4 条：设备 UI 就是 4 个槽位（协议与 UI 都不变）。
        let plan = PassportTodoProtocol.pushPlan(items: items, status: status)
        XCTAssertEqual(plan.todoFrames.count, 1 + PassportTodoProtocol.maxItems, "1 条清空帧 + 4 条待办帧")
        XCTAssertEqual(plan.todoFrames[1][2], UInt8(PassportTodoProtocol.maxItems), "待办帧 total = 实际发送条数 4")
        XCTAssertEqual(plan.statusFrame[2], 8, "同一次下发：待办帧 4 条、状态帧 count 8")
    }

    /// 4 条 → count=4：与旧行为完全一致（这条边界没变）。
    func testCountFourMatchesLegacyBehaviour() throws {
        let items = try PassportTodoMapper.items(fromRemindersResponse: Self.remindersJSON(pending: 4))
        XCTAssertEqual(items.count, 4)
        let frame = PassportConnectionStatusProtocol.encodeFrame(state: .connected,
                                                                 count: items.count,
                                                                 ageSeconds: 0)
        XCTAssertEqual(frame[2], 4)
    }

    /// count 字段只有 1 字节：>255 **饱和**写 255，绝不回绕。
    /// 300 条若回绕（300 & 0xFF）会变成 44 —— 一个看着合理但完全错的数字。
    func testCountSaturatesAt255WithoutWraparound() throws {
        XCTAssertEqual(PassportConnectionStatusProtocol.maxCount, 255, "1 字节字段上限")
        XCTAssertEqual(PassportConnectionStatusProtocol.saturateCount(0), 0)
        XCTAssertEqual(PassportConnectionStatusProtocol.saturateCount(255), 255, "边界值原样保留")
        XCTAssertEqual(PassportConnectionStatusProtocol.saturateCount(256), 255)
        XCTAssertEqual(PassportConnectionStatusProtocol.saturateCount(300), 255, "饱和，不是 44")
        XCTAssertEqual(PassportConnectionStatusProtocol.saturateCount(10_000), 255)
        XCTAssertEqual(PassportConnectionStatusProtocol.saturateCount(-3), 0, "负数按 0")

        // 端到端：300 条真实待办 → 帧里写 255（并打一条日志）
        let items = try PassportTodoMapper.items(fromRemindersResponse: Self.remindersJSON(pending: 300, dismissed: 2))
        XCTAssertEqual(items.count, 300)
        let frame = PassportConnectionStatusProtocol.encodeFrame(state: .connected,
                                                                 count: items.count,
                                                                 ageSeconds: 0)
        XCTAssertEqual(frame.count, 5, "帧长度不变（协议字节布局没动）")
        XCTAssertEqual(frame[2], 255, "饱和写 255")
        XCTAssertNotEqual(frame[2], UInt8(300 & 0xFF), "绝不能回绕成 44")
    }

    /// 造一份 `/api/reminders` 风格的返回：N 条 pending + M 条 dismissed（默认 0）。
    /// 生成一份 `/api/reminders` 形状的响应。
    ///
    /// [2026-10-02 语义变更] 映射层进入严格模式：只有 `review_status=confirmed`
    /// 或 `source_type=manual` 的条目才会下发。因此 `pending` 默认带
    /// `review_status=confirmed` —— 本辅助函数服务的是「count 语义 / 帧布局」这类测试，
    /// 它们要在**已确认**的前提下验证条数，而不是验证过滤本身（过滤由
    /// `testMapperDropsUnconfirmedCandidate` 等专门覆盖）。
    private static func remindersJSON(pending: Int,
                                      dismissed: Int = 0,
                                      pendingConfirmed: Bool = true) -> Data {
        var records: [String] = []
        let reviewField = pendingConfirmed ? "\"review_status\":\"confirmed\"," : ""
        for index in 0..<pending {
            records.append("{\"task_key\":\"p\(index)\",\(reviewField)\"source_status\":\"pending\",\"status\":\"unscheduled\",\"title\":\"待办\(index)\"}")
        }
        for index in 0..<dismissed {
            records.append("{\"task_key\":\"d\(index)\",\"source_status\":\"dismissed\",\"status\":\"dismissed\",\"title\":\"已忽略\(index)\"}")
        }
        return Data("{\"backend\":\"local\",\"reminders\":[\(records.joined(separator: ","))]}".utf8)
    }

    /// 状态帧必须既不是 4 字节（校时），首字节也不是 0x02（待办帧）——三类载荷不能互相误判。
    func testStatusFrameIsDistinctFromTimeSyncAndTodoFrames() throws {
        for state in [PassportConnectionState.connected, .disconnected] {
            for count in [0, 1, 4, 8, 255] {
                let frame = PassportConnectionStatusProtocol.encodeFrame(state: state,
                                                                         count: count,
                                                                         ageSeconds: 0)
                XCTAssertEqual(frame.count, 5)
                XCTAssertNotEqual(frame.count, 4, "不能是校时帧长度")
                XCTAssertEqual(frame[0], 0x03)
                XCTAssertNotEqual(frame[0], PassportTodoProtocol.frameMagic, "不能与待办帧魔数 0x02 撞车")
            }
        }
    }

    // MARK: "多久前同步过"的算法

    func testAgeSecondsSinceLastSuccess() throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        // 从没成功过 → 封顶（协议没有"从未同步"取值，65535 作哨兵）
        XCTAssertEqual(PassportConnectionStatusProtocol.ageSeconds(since: nil, now: now), 65_535)
        XCTAssertEqual(PassportConnectionStatusProtocol.ageSeconds(since: now, now: now), 0)
        XCTAssertEqual(PassportConnectionStatusProtocol.ageSeconds(since: now.addingTimeInterval(-90), now: now), 90)
        XCTAssertEqual(PassportConnectionStatusProtocol.ageSeconds(since: now.addingTimeInterval(-70_000), now: now), 65_535)
        // 时钟回拨（"上次成功"在未来）按 0，不写负数
        XCTAssertEqual(PassportConnectionStatusProtocol.ageSeconds(since: now.addingTimeInterval(120), now: now), 0)
    }

    // MARK: 一轮下发的写入顺序（清空帧 → 待办帧 → 状态帧）

    /// 顺序：清空帧在最前、状态帧在最后；待办帧夹在中间，index/total 正确。
    func testPushPlanOrdersClearThenItemsThenStatus() throws {
        let items = [PassportTodoItem(title: "买牛奶", status: .pending),
                     PassportTodoItem(title: "写周报", status: .inProgress)]
        let status = PassportConnectionStatus(state: .connected, count: 2, ageSeconds: 0)
        let plan = PassportTodoProtocol.pushPlan(items: items, status: status)

        XCTAssertEqual(plan.todoFrames.count, 3, "1 条清空帧 + 2 条待办帧")
        XCTAssertEqual(plan.allFrames.count, 4, "再加最后 1 条状态帧")
        XCTAssertEqual(plan.allFrames.last, plan.statusFrame, "状态帧必须最后写")

        // 首帧是清空帧
        XCTAssertEqual(plan.todoFrames[0], PassportTodoProtocol.clearFrame())
        XCTAssertEqual(plan.todoFrames[0][2], 0, "清空帧 total = 0")
        // 待办帧 index/total
        XCTAssertEqual(plan.todoFrames[1][1], 0, "index")
        XCTAssertEqual(plan.todoFrames[1][2], 2, "total")
        XCTAssertEqual(plan.todoFrames[2][1], 1, "index")
        XCTAssertEqual(plan.todoFrames[2][3], 1, "status = 进行中")
        // 状态帧
        XCTAssertEqual(plan.statusFrame, status.frame)
        XCTAssertEqual(plan.statusFrame[0], 0x03)
    }

    /// 状态帧的内容与这一轮的成败一致：成功 age=0，失败 age 走上次成功时刻。
    func testPushPlanStatusReflectsOutcome() throws {
        let items = [PassportTodoItem(title: "买牛奶", status: .pending)]

        let ok = PassportTodoProtocol.pushPlan(items: items,
                                               status: PassportConnectionStatus(state: .connected,
                                                                                count: items.count,
                                                                                ageSeconds: 0))
        XCTAssertEqual(ok.statusFrame[1], 1, "成功 → state=1")
        XCTAssertEqual(ok.statusFrame[2], 1, "count = 本轮条数")
        XCTAssertEqual(ok.statusFrame[3], 0, "成功 → age=0")

        let failed = PassportTodoProtocol.pushPlan(items: [],
                                                   status: PassportConnectionStatus(state: .disconnected,
                                                                                    count: 3,
                                                                                    ageSeconds: 120))
        XCTAssertEqual(failed.todoFrames, [PassportTodoProtocol.clearFrame()], "失败仍发清空帧")
        XCTAssertEqual(failed.statusFrame[1], 0, "失败 → state=0")
        XCTAssertEqual(failed.statusFrame[2], 3, "count = 上次已知条数")
        XCTAssertEqual(failed.statusFrame[3], 120, "age = 距上次成功 120s")
    }

    /// 超过 4 条的输入由 pushPlan 兜底截断**待办帧**，`total` 写实际发送条数；
    /// 但状态帧的 count 报真实总数 6（不再是 4）。
    func testPushPlanCapsItemFramesButStatusCountStaysReal() throws {
        let items = (0..<6).map { PassportTodoItem(title: "第\($0)条", status: .pending) }
        let plan = PassportTodoProtocol.pushPlan(items: items,
                                                 status: PassportConnectionStatus(state: .connected,
                                                                                  count: 6,
                                                                                  ageSeconds: 0))
        XCTAssertEqual(plan.todoFrames.count, 5, "1 条清空帧 + 最多 4 条待办帧")
        XCTAssertEqual(plan.todoFrames[1][2], 4, "total 写实际发送条数 4")
        XCTAssertEqual(plan.statusFrame[2], 6, "状态帧 count = 真实总数 6（不再随待办帧钳到 4）")
    }
}



