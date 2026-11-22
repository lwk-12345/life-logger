import Foundation
import XCTest
@testable import WatchCore

final class WatchCaptureNetworkGateTests: XCTestCase {
    func testBlockedCaptureDoesNotEvenCreateARequest() throws {
        let gate = WatchCaptureNetworkGate()
        gate.setBlocked(true)
        var created = false
        XCTAssertThrowsError(try gate.start { created = true; return CountingTask() })
        XCTAssertFalse(created)
    }

    func testCaptureCancelsInFlightTaskAndAllowsRequestsAfterStop() throws {
        let gate = WatchCaptureNetworkGate()
        let first = CountingTask()
        try gate.start { first }
        XCTAssertEqual(first.resumes, 1)
        gate.setBlocked(true)
        XCTAssertEqual(first.cancellations, 1)
        gate.setBlocked(false)
        let next = CountingTask()
        try gate.start { next }
        XCTAssertEqual(next.resumes, 1)
    }

    func testExplicitSealedUploadCanRunDuringCaptureWithoutStoppingAtRotation() throws {
        let gate = WatchCaptureNetworkGate()
        gate.setBlocked(true)
        let liveUpload = CountingTask()

        try gate.start(allowDuringCapture: true) { liveUpload }
        XCTAssertEqual(liveUpload.resumes, 1)
        gate.setBlocked(true) // recording -> stopping keeps the capture gate closed
        XCTAssertEqual(liveUpload.cancellations, 0)

        var ordinaryRequestCreated = false
        XCTAssertThrowsError(
            try gate.start {
                ordinaryRequestCreated = true
                return CountingTask()
            }
        )
        XCTAssertFalse(ordinaryRequestCreated)
    }

    func testCaptureEntryStillCancelsAnAlreadyRunningSync() throws {
        let gate = WatchCaptureNetworkGate()
        let normalSync = CountingTask()
        try gate.start { normalSync }

        gate.setBlocked(true)

        XCTAssertEqual(normalSync.cancellations, 1)
    }

    func testRequestResponseIsDeliveredThroughTheGate() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [GateTestProtocol.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let (data, response) = try await WatchCaptureNetworkGate().perform(
            session: session, request: URLRequest(url: URL(string: "https://gate-test.invalid/health")!)
        )
        XCTAssertEqual(String(decoding: data, as: UTF8.self), "ready")
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
    }

    func testBlockedAsyncRequestReturnsCancellation() async {
        let gate = WatchCaptureNetworkGate()
        gate.setBlocked(true)
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        do {
            _ = try await gate.perform(
                session: session, request: URLRequest(url: URL(string: "https://gate-test.invalid/health")!)
            )
            XCTFail("blocked request succeeded")
        } catch { XCTAssertTrue(error is CancellationError) }
    }

    func testConcurrentStartsCannotBypassBlockedCapture() {
        let gate = WatchCaptureNetworkGate()
        gate.setBlocked(true)
        DispatchQueue.concurrentPerform(iterations: 100) { _ in
            do {
                try gate.start { XCTFail("created request during capture"); return CountingTask() }
                XCTFail("started during capture")
            } catch { XCTAssertTrue(error is CancellationError) }
        }
    }
}

private final class CountingTask: URLSessionTask, @unchecked Sendable {
    private let countLock = NSLock()
    private var resumeCount = 0
    private var cancelCount = 0
    var resumes: Int { countLock.withLock { resumeCount } }
    var cancellations: Int { countLock.withLock { cancelCount } }
    override func resume() { countLock.withLock { resumeCount += 1 } }
    override func cancel() { countLock.withLock { cancelCount += 1 } }
}

private final class GateTestProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data("ready".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
