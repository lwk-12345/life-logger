import Foundation
@testable import CaptureUnderTest

/// Pins the normal-sync/capture-start interleaving without depending on Wi-Fi timing.
actor InterruptedConnection: WatchConnectionTransport {
    private var continuation: CheckedContinuation<Void, any Error>?
    var isHeld: Bool { continuation != nil }

    func checkConnection() async throws {
        try await withCheckedThrowingContinuation { continuation = $0 }
    }

    func interrupt() { continuation?.resume(throwing: CancellationError()); continuation = nil }
    func takeBackgroundIssue() async -> String? { nil }
    func register(session: RecordingSessionManifest) async throws { throw CancellationError() }
    func upload(segment: AudioSegment) async throws -> WatchSegmentAcknowledgement { throw CancellationError() }
    func complete(session: RecordingSessionManifest) async throws -> String { throw CancellationError() }
}
