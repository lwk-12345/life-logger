import XCTest
@testable import WatchCore

final class SegmentLedgerPruneTests: XCTestCase {
    private func makeLedger() throws -> (SegmentLedger, URL) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return (SegmentLedger(rootURL: root), root)
    }

    func testUnfinishedAndUnreceivedSessionsAreKept() async throws {
        let (ledger, _) = try makeLedger()
        _ = try await ledger.createSession(id: "open", startedAt: Date(timeIntervalSince1970: 0))
        _ = try await ledger.createSession(id: "stopped", startedAt: Date(timeIntervalSince1970: 0))
        try await ledger.finishSession(id: "stopped")
        let removed = try await ledger.pruneFinishedSessions(olderThan: Date().addingTimeInterval(86_400))
        XCTAssertEqual(removed, 0)
        let remaining = try await ledger.listSessions()
        XCTAssertEqual(Set(remaining.map(\.id)), ["open", "stopped"])
    }
}
