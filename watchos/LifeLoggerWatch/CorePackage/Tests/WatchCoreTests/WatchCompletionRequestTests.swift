import Foundation
import XCTest
@testable import WatchCore

final class WatchCompletionRequestTests: XCTestCase {
    func testRecoveredTailCannotSendStopTimeBeforeAcknowledgedAudioEnd() throws {
        let start = Date(timeIntervalSince1970: 1_790_000_000)
        let originalStop = start.addingTimeInterval(60)
        let segments = (0..<2).map { sequence in
            AudioSegment(
                sessionID: "recovered-tail", sequence: sequence,
                startOffsetMilliseconds: Int64(sequence * 60_000), durationMilliseconds: 60_000,
                localURL: URL(fileURLWithPath: "/unused/segment-\(sequence).m4a"),
                byteCount: 1, sha256: "unused", uploadState: .acknowledged
            )
        }
        let manifest = RecordingSessionManifest(
            id: "recovered-tail", startedAt: start, stoppedAt: originalStop, segments: segments
        )
        let body = try XCTUnwrap(WatchCompletionRequest.body(session: manifest))
        let payload = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
        let ended = try XCTUnwrap(payload["ended_at"] as? String)
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = try XCTUnwrap(parser.date(from: ended))
        XCTAssertEqual(date.timeIntervalSince1970, start.addingTimeInterval(120).timeIntervalSince1970, accuracy: 0.001)
        XCTAssertEqual(payload["expected_segments"] as? Int, 2)
        XCTAssertEqual(manifest.stoppedAt, originalStop, "Keep the historical stop metadata unchanged")
    }

    func testCompletionDoesNotFinishAnOpenRecording() {
        let manifest = RecordingSessionManifest(id: "still-recording", startedAt: Date())
        XCTAssertNil(WatchCompletionRequest.body(session: manifest))
    }

    func testPathNamesTheSessionTheMacHasToClose() {
        XCTAssertEqual(
            WatchCompletionRequest.path(deviceID: "watch-abc", sessionID: "session-1"),
            ["api", "watch-sessions", "watch-abc", "session-1", "complete"]
        )
    }

    func testBodyCarriesTheRealSegmentCountAndStopTime() throws {
        let stoppedAt = Date(timeIntervalSince1970: 1_790_000_000)
        let body = try XCTUnwrap(
            WatchCompletionRequest.body(segmentCount: 58, stoppedAt: stoppedAt)
        )
        let payload = try XCTUnwrap(
            JSONSerialization.jsonObject(with: body) as? [String: Any]
        )

        XCTAssertEqual(payload["expected_segments"] as? Int, 58)
        let endedAt = try XCTUnwrap(payload["ended_at"] as? String)
        // ISO-8601 with a zone, which is what the Mac validates before closing a session.
        // The seconds carry a fraction, so round-trip it with a matching formatter: the
        // plain ISO8601DateFormatter cannot read fractional seconds and would report nil.
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let parsed = try XCTUnwrap(parser.date(from: endedAt))
        XCTAssertEqual(parsed.timeIntervalSince1970, stoppedAt.timeIntervalSince1970, accuracy: 0.01)
    }

    func testBodyRefusesASessionWithoutSegments() {
        XCTAssertNil(
            WatchCompletionRequest.body(segmentCount: 0, stoppedAt: Date())
        )
    }

    func testAcceptedOutcomeCarriesTheJobID() {
        let body = Data(#"{"complete":true,"job_id":"job-20261002-002857-c760f1e5"}"#.utf8)

        XCTAssertEqual(
            WatchCompletionRequest.outcome(from: body, statusCode: 201),
            .accepted(jobID: "job-20261002-002857-c760f1e5")
        )
    }

    /// A completion the Mac refuses must keep the Mac's own words: this sentence is the only
    /// thing that makes the failure diagnosable from the Watch.
    func testRejectedOutcomeKeepsTheMacsSentence() {
        let body = Data(#"{"error":"conflict","detail":"Watch session is not accepting new segments"}"#.utf8)

        XCTAssertEqual(
            WatchCompletionRequest.outcome(from: body, statusCode: 409),
            .rejected(detail: "Watch session is not accepting new segments")
        )
    }

    func testTwoHundredWithoutAJobIDIsNotTreatedAsSuccess() {
        let body = Data(#"{"complete":true}"#.utf8)

        guard case .indeterminate(let reason) = WatchCompletionRequest.outcome(
            from: body,
            statusCode: 200
        ) else {
            return XCTFail("A 2xx without a job id must not be recorded as imported")
        }
        XCTAssertFalse(reason.isEmpty)
    }

    /// An error status is a rejection even when the body cannot be parsed; only a *2xx*
    /// without a usable job id is indeterminate (nothing may be marked imported without it).
    func testUnreadableBodyWithAnErrorStatusIsARejection() {
        guard case .rejected(let detail) = WatchCompletionRequest.outcome(
            from: Data("not json".utf8),
            statusCode: 500
        ) else {
            return XCTFail("Expected a rejected outcome")
        }
        XCTAssertTrue(detail.contains("500"))
    }

    func testUnreadableBodyWithASuccessStatusIsIndeterminate() {
        guard case .indeterminate = WatchCompletionRequest.outcome(
            from: Data("not json".utf8),
            statusCode: 200
        ) else {
            return XCTFail("Expected an indeterminate outcome")
        }
    }

    /// The background session hands back a result without our in-memory state, so the session
    /// identity has to be recoverable from the response URL alone.
    func testSessionIdentityIsRecoveredFromTheResponseURL() {
        let url = URL(string: "http://172.20.10.3:8765/api/watch-sessions/watch-abc/session-9/complete")!

        let identity = WatchCompletionRequest.sessionIdentity(fromResponseURL: url, taskDescription: nil)

        XCTAssertEqual(identity?.deviceID, "watch-abc")
        XCTAssertEqual(identity?.sessionID, "session-9")
    }

    func testSessionIdentityFallsBackToTheTaskDescription() {
        let identity = WatchCompletionRequest.sessionIdentity(
            fromResponseURL: nil,
            taskDescription: "api/watch-sessions/watch-def/session-10/complete"
        )

        XCTAssertEqual(identity?.deviceID, "watch-def")
        XCTAssertEqual(identity?.sessionID, "session-10")
    }

    func testSessionIdentityRejectsUnrelatedURLs() {
        XCTAssertNil(
            WatchCompletionRequest.sessionIdentity(
                fromResponseURL: URL(string: "http://172.20.10.3:8765/api/watch-health"),
                taskDescription: nil
            )
        )
    }
}
