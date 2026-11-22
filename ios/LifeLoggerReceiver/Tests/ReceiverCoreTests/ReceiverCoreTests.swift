import Foundation
import XCTest
@testable import ReceiverCore

final class ReceiverCoreTests: XCTestCase {
    func testDiscoveryEchoesNonceAndRejectsMalformedRequests() {
        XCTAssertEqual(
            PassportDiscovery.response(
                to: Data("LL_DISCOVER_V1 a1B2c3D4".utf8),
                receiverPort: 8766
            ),
            Data("LL_RECEIVER_V1 a1B2c3D4 8766".utf8)
        )
        XCTAssertNil(
            PassportDiscovery.response(
                to: Data("LL_DISCOVER_V1 bad".utf8),
                receiverPort: 8766
            )
        )
    }

    func testHealthEndpointMatchesReceiverContract() throws {
        let core = try makeCore()
        let response = core.handle(
            PassportHTTPRequest(method: "GET", path: "/health", headers: [:], body: Data())
        )

        XCTAssertEqual(response.statusCode, 200)
        let json = try jsonObject(response.body)
        XCTAssertEqual(json["ok"] as? Bool, true)
        XCTAssertEqual(json["service"] as? String, "LifeLoggerReceiver")
        XCTAssertEqual(json["version"] as? Int, 1)
        XCTAssertEqual(json["port"] as? Int, 8766)
    }

    func testValidChunkIsSavedBeforeSuccessfulAcknowledgement() throws {
        let root = temporaryDirectory()
        let core = PassportReceiverCore(store: try PassportStreamStore(root: root))
        let pcm = Data([0x01, 0x00, 0x02, 0x00])

        let response = core.handle(chunkRequest(index: 0, body: pcm))

        XCTAssertEqual(response.statusCode, 200)
        XCTAssertEqual(
            try Data(contentsOf: root
                .appendingPathComponent("passport-01/session-1/chunk-00000000.s16le")),
            pcm
        )
        XCTAssertEqual(try jsonObject(response.body)["checksum_ok"] as? Bool, true)
    }

    func testRejectsChecksumMismatch() throws {
        let core = try makeCore()
        var request = chunkRequest(index: 0, body: Data([0x01, 0x00]))
        request.headers["X-LifeLogger-Checksum"] = "crc32:00000000"

        let response = core.handle(request)

        XCTAssertEqual(response.statusCode, 422)
        XCTAssertEqual(try jsonObject(response.body)["error"] as? String, "checksum mismatch")
    }

    func testEndReportsMissingChunksAndDuplicateAndOrderCounts() throws {
        let core = try makeCore()
        let body = Data([0x01, 0x00])
        XCTAssertEqual(core.handle(chunkRequest(index: 1, body: body)).statusCode, 200)
        XCTAssertEqual(core.handle(chunkRequest(index: 0, body: body)).statusCode, 200)
        XCTAssertEqual(core.handle(chunkRequest(index: 0, body: body)).statusCode, 200)

        let response = core.handle(endRequest(expectedChunks: 3))
        let summary = try XCTUnwrap(try jsonObject(response.body)["summary"] as? [String: Any])

        XCTAssertEqual(response.statusCode, 200)
        XCTAssertEqual(summary["missing_chunks"] as? [Int], [2])
        XCTAssertEqual(summary["duplicate_chunks"] as? Int, 1)
        XCTAssertEqual(summary["out_of_order_chunks"] as? Int, 1)
        XCTAssertEqual(summary["complete"] as? Bool, false)
    }

    func testConflictingDuplicateIsRejected() throws {
        let core = try makeCore()
        XCTAssertEqual(core.handle(chunkRequest(index: 0, body: Data([0x01, 0x00]))).statusCode, 200)

        let response = core.handle(chunkRequest(index: 0, body: Data([0x02, 0x00])))

        XCTAssertEqual(response.statusCode, 409)
    }

    func testCompleteEndAssemblesMono16BitWav() throws {
        let root = temporaryDirectory()
        let core = PassportReceiverCore(store: try PassportStreamStore(root: root))
        let first = Data([0x01, 0x00, 0x02, 0x00])
        let second = Data([0x03, 0x00, 0x04, 0x00])
        XCTAssertEqual(core.handle(chunkRequest(index: 0, body: first)).statusCode, 200)
        XCTAssertEqual(core.handle(chunkRequest(index: 1, body: second)).statusCode, 200)

        let response = core.handle(endRequest(expectedChunks: 2))
        let wavURL = root.appendingPathComponent("passport-01/session-1/recording.wav")
        let wav = try Data(contentsOf: wavURL)

        XCTAssertEqual(response.statusCode, 200)
        XCTAssertEqual(String(decoding: wav.prefix(4), as: UTF8.self), "RIFF")
        XCTAssertEqual(String(decoding: wav[8..<12], as: UTF8.self), "WAVE")
        XCTAssertEqual(readUInt16(wav, at: 22), 1)
        XCTAssertEqual(readUInt32(wav, at: 24), 16_000)
        XCTAssertEqual(readUInt16(wav, at: 34), 16)
        XCTAssertEqual(wav.suffix(8), first + second)
        let summary = try XCTUnwrap(try jsonObject(response.body)["summary"] as? [String: Any])
        XCTAssertEqual(summary["complete"] as? Bool, true)
    }

    func testEventTimestampIsPersistedOnDevice() throws {
        let root = temporaryDirectory()
        let core = PassportReceiverCore(store: try PassportStreamStore(root: root))
        var headers = baseHeaders(index: 0, body: Data())
        headers["X-LifeLogger-Type"] = "event"
        headers["X-LifeLogger-Event"] = "tap"
        headers["X-LifeLogger-Event-Time-Ms"] = "12345"

        let response = core.handle(
            PassportHTTPRequest(
                method: "POST",
                path: "/v1/audio/chunk",
                headers: headers,
                body: Data()
            )
        )

        XCTAssertEqual(response.statusCode, 200)
        let eventLog = try String(contentsOf: root.appendingPathComponent("passport-01/events.jsonl"), encoding: .utf8)
        XCTAssertTrue(eventLog.contains("\"event_time_ms\":\"12345\""))
        XCTAssertTrue(FileManager.default.fileExists(atPath: root.appendingPathComponent("passport-01/session-1/summary.json").path))
    }

    func testOutboxSendsChunksBeforeEndAndPersistsMacAcknowledgements() throws {
        let root = temporaryDirectory()
        let core = PassportReceiverCore(store: try PassportStreamStore(root: root))
        let audio = Data([0x01, 0x00, 0x02, 0x00])
        XCTAssertEqual(core.handle(chunkRequest(index: 0, body: audio)).statusCode, 200)
        XCTAssertEqual(core.handle(endRequest(expectedChunks: 1)).statusCode, 200)

        let destination = "http://100.64.0.10:8766"
        var outbox = PassportSyncOutbox(root: root)
        let firstBatch = try outbox.pendingBatch(
            destination: destination,
            itemLimit: 1,
            byteLimit: 1_024
        )

        XCTAssertEqual(firstBatch.count, 1)
        XCTAssertEqual(firstBatch[0].kind, .audio)
        XCTAssertEqual(firstBatch[0].path, "/v1/audio/chunk")
        XCTAssertEqual(firstBatch[0].body, audio)
        XCTAssertEqual(firstBatch[0].headers["X-LifeLogger-Checksum"], CRC32.header(for: audio))

        try outbox.acknowledge(firstBatch[0], destination: destination)
        outbox = PassportSyncOutbox(root: root)
        let secondBatch = try outbox.pendingBatch(
            destination: destination,
            itemLimit: 1,
            byteLimit: 1_024
        )

        XCTAssertEqual(secondBatch.count, 1)
        XCTAssertEqual(secondBatch[0].kind, .end)
        XCTAssertEqual(secondBatch[0].path, "/v1/audio/end")
        try outbox.acknowledge(secondBatch[0], destination: destination)
        XCTAssertTrue(try outbox.pendingBatch(destination: destination).isEmpty)
    }

    func testOutboxKeepsEventNameAndTimestampForMacReceiver() throws {
        let root = temporaryDirectory()
        let core = PassportReceiverCore(store: try PassportStreamStore(root: root))
        let audio = Data([0x01, 0x00])
        XCTAssertEqual(core.handle(chunkRequest(index: 0, body: audio)).statusCode, 200)

        var eventHeaders = baseHeaders(index: 0, body: Data())
        eventHeaders["X-LifeLogger-Type"] = "event"
        eventHeaders["X-LifeLogger-Event"] = "tap"
        eventHeaders["X-LifeLogger-Event-Time-Ms"] = "7654"
        XCTAssertEqual(
            core.handle(PassportHTTPRequest(
                method: "POST",
                path: "/v1/audio/chunk",
                headers: eventHeaders,
                body: Data()
            )).statusCode,
            200
        )

        let items = try PassportSyncOutbox(root: root).pendingBatch(
            destination: "http://mac.local:8766",
            itemLimit: 10,
            byteLimit: 1_024
        )
        let event = try XCTUnwrap(items.first(where: { $0.kind == .event }))

        XCTAssertEqual(event.path, "/v1/audio/chunk")
        XCTAssertEqual(event.headers["X-LifeLogger-Type"], "event")
        XCTAssertEqual(event.headers["X-LifeLogger-Event"], "tap")
        XCTAssertEqual(event.headers["X-LifeLogger-Event-Time-Ms"], "7654")
        XCTAssertEqual(event.body, Data())
    }

    func testOutboxBatchHonorsRequestAndByteLimitsAndLeavesUnsentItemsPending() throws {
        let root = temporaryDirectory()
        let core = PassportReceiverCore(store: try PassportStreamStore(root: root))
        let audio = Data(repeating: 0x01, count: 16)
        XCTAssertEqual(core.handle(chunkRequest(index: 0, body: audio)).statusCode, 200)
        XCTAssertEqual(core.handle(chunkRequest(index: 1, body: audio)).statusCode, 200)

        let outbox = PassportSyncOutbox(root: root)
        let batch = try outbox.pendingBatch(
            destination: "http://mac.local:8766",
            itemLimit: 2,
            byteLimit: 16
        )

        XCTAssertEqual(batch.map(\.kind), [.audio])
        XCTAssertEqual(batch.reduce(0) { $0 + $1.body.count }, 16)
        XCTAssertEqual(try outbox.pendingBatch(destination: "http://mac.local:8766").count, 2)
    }

    private func makeCore() throws -> PassportReceiverCore {
        PassportReceiverCore(store: try PassportStreamStore(root: temporaryDirectory()))
    }

    private func temporaryDirectory() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("receiver-tests-\(UUID().uuidString)", isDirectory: true)
    }

    private func chunkRequest(index: Int, body: Data) -> PassportHTTPRequest {
        PassportHTTPRequest(
            method: "POST",
            path: "/v1/audio/chunk",
            headers: baseHeaders(index: index, body: body),
            body: body
        )
    }

    private func endRequest(expectedChunks: Int) -> PassportHTTPRequest {
        PassportHTTPRequest(
            method: "POST",
            path: "/v1/audio/end",
            headers: baseHeaders(index: expectedChunks, body: Data()),
            body: Data()
        )
    }

    private func baseHeaders(index: Int, body: Data) -> [String: String] {
        [
            "X-LifeLogger-Session": "session-1",
            "X-LifeLogger-Device": "passport-01",
            "X-LifeLogger-Chunk": String(index),
            "X-LifeLogger-Checksum": CRC32.header(for: body),
            "X-LifeLogger-Sample-Rate": "16000",
            "X-LifeLogger-Format": "s16le"
        ]
    }

    private func jsonObject(_ data: Data) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func readUInt16(_ data: Data, at offset: Int) -> UInt16 {
        UInt16(data[offset]) | (UInt16(data[offset + 1]) << 8)
    }

    private func readUInt32(_ data: Data, at offset: Int) -> UInt32 {
        UInt32(data[offset])
            | (UInt32(data[offset + 1]) << 8)
            | (UInt32(data[offset + 2]) << 16)
            | (UInt32(data[offset + 3]) << 24)
    }
}
