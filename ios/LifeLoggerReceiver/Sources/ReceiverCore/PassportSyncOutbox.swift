import Foundation

/// 待同步条目的类型。`path` 与 Mac 接收器的路由一一对应。
public enum PassportSyncKind: String, Equatable {
    case audio
    case end
    case event
}

/// 一条待发送给 Mac 的请求（已经按接收器契约拼好 path/headers/body）。
public struct PassportSyncItem {
    public let kind: PassportSyncKind
    public let path: String
    public let body: Data
    public let headers: [String: String]
    /// 稳定 id：跨进程重启也一致，`acknowledge` 按它落盘去重。
    public let identifier: String

    public init(kind: PassportSyncKind, path: String, body: Data,
                headers: [String: String], identifier: String) {
        self.kind = kind
        self.path = path
        self.body = body
        self.headers = headers
        self.identifier = identifier
    }
}

/// iPhone 侧「已经收下的东西怎么补发给 Mac」的只读队列。
///
/// 为什么队列是从**已落盘**的接收目录反推出来的：
/// 接受器（`PassportStreamStore`）与发送器（本类）必须共用一份事实——
/// 收下即入队、Mac 确认（ack）才出队。这样进程被杀/断网重连都不会丢 chunk，
/// 也不会重复发送已被 Mac 收下的东西。
///
/// 只有 `acknowledge()` 会写盘（`<root>/.outbox/acks.json`），`pendingBatch()` 完全只读。
public final class PassportSyncOutbox {
    public static let defaultItemLimit = 10
    public static let defaultByteLimit = 1_048_576

    public let root: URL
    private let fileManager = FileManager.default

    public init(root: URL) {
        self.root = root
    }

    // MARK: - 读取

    public func pendingBatch(
        destination: String,
        itemLimit: Int = PassportSyncOutbox.defaultItemLimit,
        byteLimit: Int = PassportSyncOutbox.defaultByteLimit
    ) throws -> [PassportSyncItem] {
        guard itemLimit > 0 else { return [] }
        let acknowledged = loadAcks()[destination] ?? []
        let pending = try allItems().filter { !acknowledged.contains($0.identifier) }

        var batch: [PassportSyncItem] = []
        var bytes = 0
        for item in pending {
            if batch.count >= itemLimit { break }
            let wouldBe = bytes + item.body.count
            if !batch.isEmpty && wouldBe > max(0, byteLimit) { break }
            batch.append(item)
            bytes = wouldBe
        }
        return batch
    }

    /// Mac 确认收到后落盘。重复 ack 同一 item 是幂等的。
    public func acknowledge(_ item: PassportSyncItem, destination: String) throws {
        var acks = loadAcks()
        var seen = acks[destination] ?? []
        if !seen.contains(item.identifier) {
            seen.append(item.identifier)
            acks[destination] = seen
            try saveAcks(acks)
        }
    }

    // MARK: - 扫描

    private func allItems() throws -> [PassportSyncItem] {
        guard fileManager.fileExists(atPath: root.path) else { return [] }
        let devices = (try? fileManager.contentsOfDirectory(
            at: root, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles]
        )) ?? []

        var audio: [PassportSyncItem] = []
        var ends: [PassportSyncItem] = []
        var events: [PassportSyncItem] = []

        for deviceDirectory in devices.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            guard isDirectory(deviceDirectory) else { continue }
            let device = deviceDirectory.lastPathComponent
            events.append(contentsOf: eventItems(deviceDirectory: deviceDirectory, device: device))

            let sessions = (try? fileManager.contentsOfDirectory(
                at: deviceDirectory, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles]
            )) ?? []
            for sessionDirectory in sessions.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
                guard isDirectory(sessionDirectory) else { continue }
                let session = sessionDirectory.lastPathComponent
                let state = loadState(sessionDirectory)
                let chunks = chunkItems(sessionDirectory: sessionDirectory, device: device,
                                        session: session, state: state)
                audio.append(contentsOf: chunks)
                if let end = endItem(sessionDirectory: sessionDirectory, device: device,
                                     session: session, state: state) {
                    ends.append(end)
                }
            }
        }
        return audio + ends + events
    }

    private func chunkItems(sessionDirectory: URL, device: String, session: String,
                            state: OutboxState) -> [PassportSyncItem] {
        let files = (try? fileManager.contentsOfDirectory(
            at: sessionDirectory, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles]
        )) ?? []
        let chunks = files.compactMap { url -> (Int, URL)? in
            let name = url.lastPathComponent
            guard name.hasPrefix("chunk-"), name.hasSuffix(".s16le") else { return nil }
            let digits = name.dropFirst("chunk-".count).dropLast(".s16le".count)
            guard let index = Int(digits) else { return nil }
            return (index, url)
        }.sorted { $0.0 < $1.0 }

        return chunks.compactMap { index, url in
            guard let body = try? Data(contentsOf: url) else { return nil }
            let meta = state.chunks[String(index)]
            var headers: [String: String] = [
                "X-LifeLogger-Session": session,
                "X-LifeLogger-Device": device,
                "X-LifeLogger-Chunk": String(index),
                "X-LifeLogger-Checksum": CRC32.header(for: body),
                "X-LifeLogger-Sample-Rate": String(state.sampleRate),
                "X-LifeLogger-Format": state.format,
            ]
            if let start = meta?.startMs, !start.isEmpty {
                headers["X-LifeLogger-Chunk-Start-Ms"] = start
            }
            return PassportSyncItem(
                kind: .audio,
                path: "/v1/audio/chunk",
                body: body,
                headers: headers,
                identifier: "\(device)/\(session)/chunk/\(index)"
            )
        }
    }

    private func endItem(sessionDirectory: URL, device: String, session: String,
                         state: OutboxState) -> PassportSyncItem? {
        let url = sessionDirectory.appendingPathComponent("end.json")
        guard let data = try? Data(contentsOf: url),
              let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        let expected = (payload["expected_chunks"] as? Int) ?? state.expectedChunks ?? state.chunks.count
        let sampleRate = (payload["sample_rate"] as? Int) ?? state.sampleRate
        let format = (payload["format"] as? String) ?? state.format
        let empty = Data()
        return PassportSyncItem(
            kind: .end,
            path: "/v1/audio/end",
            body: empty,
            headers: [
                "X-LifeLogger-Session": session,
                "X-LifeLogger-Device": device,
                "X-LifeLogger-Chunk": String(expected),
                "X-LifeLogger-Checksum": CRC32.header(for: empty),
                "X-LifeLogger-Sample-Rate": String(sampleRate),
                "X-LifeLogger-Format": format,
            ],
            identifier: "\(device)/\(session)/end"
        )
    }

    private func eventItems(deviceDirectory: URL, device: String) -> [PassportSyncItem] {
        let url = deviceDirectory.appendingPathComponent("events.jsonl")
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return [] }
        var items: [PassportSyncItem] = []
        var lineNumber = 0
        for line in text.split(separator: "\n", omittingEmptySubsequences: true) {
            lineNumber += 1
            guard let data = line.data(using: .utf8),
                  let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                continue
            }
            let session = (payload["session"] as? String) ?? "unknown"
            let event = (payload["event"] as? String) ?? "event"
            let eventTime = (payload["event_time_ms"] as? String) ?? ""
            let empty = Data()
            items.append(PassportSyncItem(
                kind: .event,
                path: "/v1/audio/chunk",
                body: empty,
                headers: [
                    "X-LifeLogger-Session": session,
                    "X-LifeLogger-Device": device,
                    "X-LifeLogger-Chunk": "0",
                    "X-LifeLogger-Checksum": CRC32.header(for: empty),
                    "X-LifeLogger-Type": "event",
                    "X-LifeLogger-Event": event,
                    "X-LifeLogger-Event-Time-Ms": eventTime,
                ],
                identifier: "\(device)/event/\(lineNumber)"
            ))
        }
        return items
    }

    // MARK: - 状态

    struct OutboxState: Codable {
        struct ChunkMeta: Codable {
            var checksum: String
            var bytes: Int
            var startMs: String
        }

        var sampleRate: Int = 0
        var format: String = ""
        var chunks: [String: ChunkMeta] = [:]
        var expectedChunks: Int? = nil
    }

    private func loadState(_ directory: URL) -> OutboxState {
        let url = directory.appendingPathComponent("state.json")
        guard let data = try? Data(contentsOf: url),
              let state = try? JSONDecoder().decode(OutboxState.self, from: data) else {
            return OutboxState()
        }
        return state
    }

    private func acksURL() -> URL {
        root.appendingPathComponent(".outbox", isDirectory: true)
            .appendingPathComponent("acks.json")
    }

    private func loadAcks() -> [String: [String]] {
        guard let data = try? Data(contentsOf: acksURL()),
              let payload = try? JSONDecoder().decode([String: [String]].self, from: data) else {
            return [:]
        }
        return payload
    }

    private func saveAcks(_ payload: [String: [String]]) throws {
        let directory = root.appendingPathComponent(".outbox", isDirectory: true)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        let data = try JSONEncoder().encode(payload)
        try data.write(to: acksURL(), options: .atomic)
    }

    private func isDirectory(_ url: URL) -> Bool {
        var isDir: ObjCBool = false
        return fileManager.fileExists(atPath: url.path, isDirectory: &isDir) && isDir.boolValue
    }
}
