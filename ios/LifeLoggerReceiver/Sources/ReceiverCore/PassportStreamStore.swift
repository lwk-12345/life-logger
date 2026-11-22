import Foundation

/// 存储层错误：只有「会被上层映射成具体 HTTP 状态」的几类。
public enum PassportStoreError: Error, Equatable {
    /// 同一个 chunk 序号重传了**不同内容**（checksum 不同）。
    case conflictingDuplicate
    /// `expectedChunks` 不合理（0 或超过上限）。
    case invalidChunkCount
    /// 设备/会话目录不可用。
    case invalidPath
}

/// 一次写入的结果：`record` 会合并进成功响应的 JSON；`summary` 只在结束时给出。
public struct PassportStoredResult {
    public let record: [String: Any]
    public let summary: [String: Any]?

    public init(record: [String: Any], summary: [String: Any]? = nil) {
        self.record = record
        self.summary = summary
    }
}

/// 落盘状态：每个 (device, session) 一份 `state.json`。
///
/// 为什么要落盘而不是只放内存：`PassportSyncOutbox` 会在**另一个实例**里读同一份目录，
/// 而且进程可能被系统杀掉；状态必须与 chunk 文件同源，才能重建出待同步队列。
struct PassportSessionState: Codable {
    struct ChunkMeta: Codable {
        var checksum: String
        var bytes: Int
        var startMs: String
    }

    var sampleRate: Int = 0
    var format: String = ""
    var chunks: [String: ChunkMeta] = [:]
    var duplicateChunks: Int = 0
    var outOfOrderChunks: Int = 0
    var ended: Bool = false
    var expectedChunks: Int? = nil
    var complete: Bool = false
    var checksum: String = ""
    var updatedAt: String = ""
}

/// iPhone 侧的接收器存储：chunk 落盘 → 结束时校验缺口 → 拼 WAV / 写 summary。
///
/// 目录结构（测试与 `PassportSyncOutbox` 都依赖这套布局）：
/// ```
/// <root>/<device>/events.jsonl                   一行一个事件
/// <root>/<device>/<session>/chunk-00000000.s16le
/// <root>/<device>/<session>/state.json
/// <root>/<device>/<session>/summary.json
/// <root>/<device>/<session>/end.json             结束时写入（outbox 据此产生 .end 项）
/// <root>/<device>/<session>/recording.wav        仅在 chunk 齐全时生成
/// ```
public final class PassportStreamStore {
    /// 单个会话允许的最大 chunk 数（与 `PassportReceiverCore` 的头校验上限一致）。
    public static let maximumChunkCount = 1_000_000

    public let root: URL
    private let fileManager = FileManager.default

    public init(root: URL) throws {
        self.root = root
        do {
            try fileManager.createDirectory(at: root, withIntermediateDirectories: true)
        } catch {
            throw PassportStoreError.invalidPath
        }
    }

    // MARK: - 音频

    public func receiveAudioChunk(
        device: String,
        session: String,
        chunk: Int,
        body: Data,
        checksum: String,
        sampleRate: Int,
        format: String,
        startMilliseconds: String
    ) throws -> PassportStoredResult {
        // 与 `PassportReceiverCore` 的头校验保持同一区间（`0...maximum`，闭区间），
        // 否则 chunk == upperBound 会在协议层被判合法、到存储层变成 400/500。
        guard (0...Self.maximumChunkCount).contains(chunk) else {
            throw PassportStoreError.invalidChunkCount
        }
        let directory = try sessionDirectory(device: device, session: session)
        var state = loadState(directory)
        state.sampleRate = sampleRate
        state.format = format

        let key = String(chunk)
        let existing = state.chunks[key]
        if let existing {
            // 同序号重传：内容一致就只记一次 duplicate；内容不一致必须拒绝（409）。
            guard existing.checksum == checksum else {
                throw PassportStoreError.conflictingDuplicate
            }
            state.duplicateChunks += 1
            writeState(state, directory)
            let summary = summaryPayload(state)
            return PassportStoredResult(
                record: [
                    "checksum_ok": true,
                    "chunk": chunk,
                    "duplicate": true,
                    "stored_chunks": state.chunks.count,
                ],
                summary: summary
            )
        }

        // 「乱序」= 收到的不是当前最小缺口（从 0 开始找第一个没收到过的序号）。
        let expected = nextExpectedIndex(state)
        if chunk != expected {
            state.outOfOrderChunks += 1
        }

        let chunkURL = directory.appendingPathComponent(String(format: "chunk-%08d.s16le", chunk))
        do {
            try body.write(to: chunkURL, options: .atomic)
        } catch {
            throw PassportStoreError.invalidPath
        }
        state.chunks[key] = PassportSessionState.ChunkMeta(
            checksum: checksum,
            bytes: body.count,
            startMs: startMilliseconds
        )
        writeState(state, directory)
        writeSummary(state, directory)
        return PassportStoredResult(
            record: [
                "checksum_ok": true,
                "chunk": chunk,
                "stored_chunks": state.chunks.count,
            ],
            summary: nil
        )
    }

    public func finishAudio(
        device: String,
        session: String,
        expectedChunks: Int,
        sampleRate: Int,
        format: String,
        checksum: String
    ) throws -> PassportStoredResult {
        guard (1...Self.maximumChunkCount).contains(expectedChunks) else {
            throw PassportStoreError.invalidChunkCount
        }
        let directory = try sessionDirectory(device: device, session: session)
        var state = loadState(directory)
        if sampleRate > 0 { state.sampleRate = sampleRate }
        if !format.isEmpty { state.format = format }
        state.ended = true
        state.expectedChunks = expectedChunks
        state.checksum = checksum

        let missing = missingChunks(state, expectedChunks: expectedChunks)
        state.complete = missing.isEmpty
        let summary = summaryPayload(state, expectedChunks: expectedChunks, missing: missing)

        if state.complete {
            try assembleWAV(state: state, expectedChunks: expectedChunks, directory: directory)
        }
        writeState(state, directory)
        writeSummary(state, directory, summary: summary)
        writeEndMarker(state, directory, expectedChunks: expectedChunks, checksum: checksum,
                       sampleRate: state.sampleRate, format: state.format)
        return PassportStoredResult(
            record: [
                "checksum_ok": true,
                "complete": state.complete,
                "received_chunks": state.chunks.count,
            ],
            summary: summary
        )
    }

    // MARK: - 事件

    /// 注意返回类型是**字典**而不是 `PassportStoredResult`：
    /// `PassportProtocol` 的事件分支直接 `payload.merge(record)`（事件没有 summary）。
    public func receiveEvent(
        device: String,
        session: String,
        event: String,
        eventTimeMilliseconds: String,
        checksum: String
    ) throws -> [String: Any] {
        let deviceDirectory = try deviceDirectory(device)
        let directory = try sessionDirectory(device: device, session: session)
        let state = loadState(directory)

        let payload: [String: Any] = [
            "device": device,
            "session": session,
            "event": event,
            "event_time_ms": eventTimeMilliseconds,
            "checksum": checksum,
            "recorded_at": Self.timestamp(),
        ]
        let line = (try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])) ?? Data("{}".utf8)
        let eventURL = deviceDirectory.appendingPathComponent("events.jsonl")
        if !fileManager.fileExists(atPath: eventURL.path) {
            fileManager.createFile(atPath: eventURL.path, contents: nil)
        }
        if let handle = try? FileHandle(forWritingTo: eventURL) {
            defer { try? handle.close() }
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: line + Data("\n".utf8))
        }

        writeState(state, directory)
        // 测试要求：收到事件后也要有 session 级 summary（否则前端看不到这个会话存在过）。
        writeSummary(state, directory)

        return ["checksum_ok": true, "event": event, "event_time_ms": eventTimeMilliseconds]
    }

    // MARK: - 目录与状态

    private func deviceDirectory(_ device: String) throws -> URL {
        let name = safeIdentifier(device, fallback: "device")
        let directory = root.appendingPathComponent(name, isDirectory: true)
        do {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        } catch {
            throw PassportStoreError.invalidPath
        }
        return directory
    }

    private func sessionDirectory(device: String, session: String) throws -> URL {
        let parent = try deviceDirectory(device)
        let name = safeIdentifier(session, fallback: "unknown")
        let directory = parent.appendingPathComponent(name, isDirectory: true)
        do {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        } catch {
            throw PassportStoreError.invalidPath
        }
        return directory
    }

    private func stateURL(_ directory: URL) -> URL {
        directory.appendingPathComponent("state.json")
    }

    private func loadState(_ directory: URL) -> PassportSessionState {
        let url = stateURL(directory)
        guard let data = try? Data(contentsOf: url),
              let state = try? JSONDecoder().decode(PassportSessionState.self, from: data) else {
            return PassportSessionState()
        }
        return state
    }

    private func writeState(_ state: PassportSessionState, _ directory: URL) {
        var snapshot = state
        snapshot.updatedAt = Self.timestamp()
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        try? data.write(to: stateURL(directory), options: .atomic)
    }

    private func writeSummary(_ state: PassportSessionState, _ directory: URL,
                              summary: [String: Any]? = nil) {
        let payload = summary ?? summaryPayload(state)
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) else {
            return
        }
        try? data.write(to: directory.appendingPathComponent("summary.json"), options: .atomic)
    }

    private func writeEndMarker(_ state: PassportSessionState, _ directory: URL,
                                expectedChunks: Int, checksum: String,
                                sampleRate: Int, format: String) {
        let payload: [String: Any] = [
            "expected_chunks": expectedChunks,
            "checksum": checksum,
            "sample_rate": sampleRate,
            "format": format,
            "complete": state.complete,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) else {
            return
        }
        try? data.write(to: directory.appendingPathComponent("end.json"), options: .atomic)
    }

    // MARK: - 计算

    private func nextExpectedIndex(_ state: PassportSessionState) -> Int {
        var index = 0
        while state.chunks[String(index)] != nil { index += 1 }
        return index
    }

    private func missingChunks(_ state: PassportSessionState, expectedChunks: Int) -> [Int] {
        (0..<expectedChunks).filter { state.chunks[String($0)] == nil }
    }

    private func summaryPayload(_ state: PassportSessionState,
                                expectedChunks: Int? = nil,
                                missing: [Int]? = nil) -> [String: Any] {
        let expected = expectedChunks ?? state.expectedChunks ?? state.chunks.count + (missing?.count ?? 0)
        let gaps = missing ?? missingChunks(state, expectedChunks: expected)
        return [
            "device_session_chunks": state.chunks.count,
            "expected_chunks": expected,
            "missing_chunks": gaps,
            "duplicate_chunks": state.duplicateChunks,
            "out_of_order_chunks": state.outOfOrderChunks,
            "complete": gaps.isEmpty && state.ended,
            "sample_rate": state.sampleRate,
            "format": state.format,
        ]
    }

    private func assembleWAV(state: PassportSessionState, expectedChunks: Int, directory: URL) throws {
        var pcm = Data()
        for index in 0..<expectedChunks {
            let url = directory.appendingPathComponent(String(format: "chunk-%08d.s16le", index))
            guard let data = try? Data(contentsOf: url) else {
                throw PassportStoreError.invalidChunkCount
            }
            pcm.append(data)
        }
        let sampleRate = state.sampleRate > 0 ? state.sampleRate : PassportStreamConstants.sampleRate
        let wav = Self.wavContainer(pcm: pcm, sampleRate: sampleRate)
        try wav.write(to: directory.appendingPathComponent("recording.wav"), options: .atomic)
    }

    /// 16-bit 单声道 PCM 的 44 字节 WAV 头（测试逐字节校验偏移 22/24/34）。
    static func wavContainer(pcm: Data, sampleRate: Int) -> Data {
        let channels = 1
        let bitsPerSample = 16
        let blockAlign = channels * bitsPerSample / 8
        let byteRate = sampleRate * blockAlign
        var header = Data()
        header.append(contentsOf: Array("RIFF".utf8))
        header.append(Self.uint32(UInt32(36 + pcm.count)))
        header.append(contentsOf: Array("WAVE".utf8))
        header.append(contentsOf: Array("fmt ".utf8))
        header.append(Self.uint32(16))
        header.append(Self.uint16(1))
        header.append(Self.uint16(UInt16(channels)))
        header.append(Self.uint32(UInt32(sampleRate)))
        header.append(Self.uint32(UInt32(byteRate)))
        header.append(Self.uint16(UInt16(blockAlign)))
        header.append(Self.uint16(UInt16(bitsPerSample)))
        header.append(contentsOf: Array("data".utf8))
        header.append(Self.uint32(UInt32(pcm.count)))
        var out = header
        out.append(pcm)
        return out
    }

    static func uint16(_ value: UInt16) -> Data {
        Data([UInt8(value & 0xff), UInt8((value >> 8) & 0xff)])
    }

    static func uint32(_ value: UInt32) -> Data {
        Data([
            UInt8(value & 0xff),
            UInt8((value >> 8) & 0xff),
            UInt8((value >> 16) & 0xff),
            UInt8((value >> 24) & 0xff),
        ])
    }

    static func timestamp() -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: Date())
    }
}
