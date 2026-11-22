import Foundation

public enum PassportStreamConstants {
    public static let httpPort = 8766
    public static let discoveryPort = 8767
    public static let sampleRate = 16_000
    public static let captureSource = "ai-passport-wifi"
}

public struct PassportHTTPRequest {
    public let method: String
    public let path: String
    public var headers: [String: String]
    public let body: Data

    public init(method: String, path: String, headers: [String: String], body: Data) {
        self.method = method.uppercased()
        self.path = path
        self.headers = headers
        self.body = body
    }

    func header(_ name: String) -> String? {
        headers.first { $0.key.caseInsensitiveCompare(name) == .orderedSame }?.value
    }
}

public struct PassportHTTPResponse {
    public let statusCode: Int
    public let body: Data

    public init(statusCode: Int, body: Data) {
        self.statusCode = statusCode
        self.body = body
    }
}

public enum CRC32 {
    private static let table: [UInt32] = (0..<256).map { value in
        var entry = UInt32(value)
        for _ in 0..<8 {
            entry = (entry & 1) == 1 ? (entry >> 1) ^ 0xedb8_8320 : entry >> 1
        }
        return entry
    }

    public static func checksum(_ data: Data) -> UInt32 {
        var value: UInt32 = 0xffff_ffff
        for byte in data {
            let slot = Int((value ^ UInt32(byte)) & 0xff)
            value = (value >> 8) ^ table[slot]
        }
        return value ^ 0xffff_ffff
    }

    public static func header(for data: Data) -> String {
        String(format: "crc32:%08x", checksum(data))
    }
}

public enum PassportDiscovery {
    public static func response(to request: Data, receiverPort: Int) -> Data? {
        guard (1...65_535).contains(receiverPort),
              let text = String(data: request, encoding: .ascii) else {
            return nil
        }
        let fields = text.split(whereSeparator: { $0.isWhitespace })
        guard fields.count == 2,
              fields[0] == "LL_DISCOVER_V1",
              fields[1].count == 8,
              fields[1].allSatisfy({ $0.isHexDigit }) else {
            return nil
        }
        return Data("LL_RECEIVER_V1 \(fields[1]) \(receiverPort)".utf8)
    }
}

public final class PassportReceiverCore {
    private let store: PassportStreamStore

    public init(store: PassportStreamStore) {
        self.store = store
    }

    public func handle(_ request: PassportHTTPRequest) -> PassportHTTPResponse {
        if request.method == "GET" {
            guard request.path == "/health" else {
                return response(status: 404, payload: ["error": "not found"])
            }
            return response(status: 200, payload: [
                "ok": true,
                "service": "LifeLoggerReceiver",
                "version": 1,
                "port": PassportStreamConstants.httpPort
            ])
        }

        guard request.method == "POST",
              ["/v1/audio/chunk", "/v1/audio/end"].contains(request.path) else {
            return response(status: 404, payload: ["error": "not found"])
        }

        let session = safeIdentifier(request.header("X-LifeLogger-Session") ?? "", fallback: "unknown")
        let device = safeIdentifier(request.header("X-LifeLogger-Device") ?? "", fallback: "device")
        guard let chunk = Int(request.header("X-LifeLogger-Chunk") ?? "0") else {
            return response(status: 400, payload: ["error": "invalid stream headers"])
        }
        guard (0...1_000_000).contains(chunk) else {
            return response(status: 400, payload: ["error": "invalid stream counters"])
        }

        let checksum = (request.header("X-LifeLogger-Checksum") ?? "").lowercased()
        guard checksum == CRC32.header(for: request.body) else {
            return response(status: 422, payload: ["error": "checksum mismatch"])
        }

        let streamType = (request.header("X-LifeLogger-Type") ?? "audio").lowercased()
        if streamType == "event" {
            guard request.body.isEmpty else {
                return response(status: 400, payload: ["error": "event body must be empty"])
            }
            let eventName = safeIdentifier(request.header("X-LifeLogger-Event") ?? "", fallback: "event")
            do {
                let record = try store.receiveEvent(
                    device: device,
                    session: session,
                    event: eventName,
                    eventTimeMilliseconds: request.header("X-LifeLogger-Event-Time-Ms") ?? "",
                    checksum: checksum
                )
                var payload: [String: Any] = ["ok": true]
                payload.merge(record) { _, newer in newer }
                return response(status: 200, payload: payload)
            } catch {
                return response(status: 500, payload: ["error": "storage failure"])
            }
        }

        guard streamType == "audio" else {
            return response(status: 400, payload: ["error": "unsupported stream type"])
        }
        guard let sampleRate = Int(request.header("X-LifeLogger-Sample-Rate") ?? "0") else {
            return response(status: 400, payload: ["error": "invalid sample rate"])
        }
        let streamFormat = request.header("X-LifeLogger-Format") ?? ""
        let endMarker = request.path == "/v1/audio/end"
            || request.header("X-LifeLogger-End") == "1"

        if endMarker && !request.body.isEmpty {
            return response(status: 400, payload: ["error": "end marker must have an empty body"])
        }
        if !endMarker && (request.body.isEmpty || request.body.count % 2 != 0) {
            return response(status: 400, payload: ["error": "invalid PCM chunk size"])
        }
        guard streamFormat == "s16le", sampleRate > 0 else {
            return response(status: 400, payload: ["error": "unsupported audio format"])
        }

        do {
            let result: PassportStoredResult
            if endMarker {
                result = try store.finishAudio(
                    device: device,
                    session: session,
                    expectedChunks: chunk,
                    sampleRate: sampleRate,
                    format: streamFormat,
                    checksum: checksum
                )
            } else {
                result = try store.receiveAudioChunk(
                    device: device,
                    session: session,
                    chunk: chunk,
                    body: request.body,
                    checksum: checksum,
                    sampleRate: sampleRate,
                    format: streamFormat,
                    startMilliseconds: request.header("X-LifeLogger-Chunk-Start-Ms") ?? ""
                )
            }
            var payload: [String: Any] = ["ok": true]
            payload.merge(result.record) { _, newer in newer }
            if let summary = result.summary {
                payload["summary"] = summary
            }
            return response(status: 200, payload: payload)
        } catch PassportStoreError.conflictingDuplicate {
            return response(status: 409, payload: ["error": "duplicate chunk has different checksum"])
        } catch PassportStoreError.invalidChunkCount {
            return response(status: 400, payload: ["error": "expected chunk count out of range"])
        } catch {
            return response(status: 500, payload: ["error": "storage failure"])
        }
    }

    private func response(status: Int, payload: [String: Any]) -> PassportHTTPResponse {
        let data = (try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])) ?? Data("{}".utf8)
        return PassportHTTPResponse(statusCode: status, body: data)
    }
}

func safeIdentifier(_ value: String, fallback: String) -> String {
    let expression = try? NSRegularExpression(pattern: "[^A-Za-z0-9_.-]+")
    let range = NSRange(value.startIndex..<value.endIndex, in: value)
    var cleaned = expression?.stringByReplacingMatches(
        in: value,
        range: range,
        withTemplate: "_"
    ) ?? value
    while cleaned.first == "." || cleaned.first == "_" { cleaned.removeFirst() }
    while cleaned.last == "." || cleaned.last == "_" { cleaned.removeLast() }
    cleaned = String(cleaned.prefix(80))
    return cleaned.isEmpty ? fallback : cleaned
}
