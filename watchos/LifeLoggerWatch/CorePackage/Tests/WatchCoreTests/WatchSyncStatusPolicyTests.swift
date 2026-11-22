import Foundation
import XCTest
@testable import WatchCore

final class WatchSyncStatusPolicyTests: XCTestCase {
    func testActiveFirstSegmentIsShownAsRecordingNotAsAlreadySynced() {
        let status = WatchSyncStatusPolicy.captureStatus(
            isRecoveringStagedAudio: false,
            hasUnrecoveredStagedAudio: false,
            isRecording: true,
            hasActiveRecordingSession: true
        )

        XCTAssertEqual(status, "正在录音，当前这段还在保存。")
    }

    func testFailedStagingRecoveryIsNotPresentedAsAnEmptySyncedQueue() {
        let status = WatchSyncStatusPolicy.captureStatus(
            isRecoveringStagedAudio: false,
            hasUnrecoveredStagedAudio: true,
            isRecording: false,
            hasActiveRecordingSession: false
        )

        XCTAssertEqual(status, "有暂存录音尚未恢复，不能视为已同步。")
    }

    func testEmptySavedQueueDoesNotClaimEveryRecordingWasAcknowledged() {
        XCTAssertEqual(
            WatchSyncStatusPolicy.queueStatus(pendingSegments: 0),
            "没有待 Mac 接收的录音。"
        )
    }

    /// The whole point of the associated values: the Mac's own sentence has to survive all
    /// the way to the Watch screen. Each case is pinned to a real server-side rejection text.
    func testCompletionFailureExplainsThatAcknowledgedAudioIsOnMacAndKeepsServerDetail() {
        let message = WatchSyncStatusPolicy.failureMessage(
            for: .sessionCompletionFailed("Watch session is not accepting new segments")
        )

        XCTAssertTrue(message.hasPrefix("Mac 已收到录音，但结束状态尚未同步："))
        XCTAssertTrue(message.contains("Watch session is not accepting new segments"))
    }

    func testRegistrationFailureDoesNotAssumeLocalAudioWasDeletedAndKeepsServerDetail() {
        let message = WatchSyncStatusPolicy.failureMessage(
            for: .sessionRegistrationFailed("expected_segments out of range")
        )

        XCTAssertTrue(message.hasPrefix("Mac 会话登记失败："))
        XCTAssertTrue(message.contains("expected_segments out of range"))
    }

    func testSegmentUploadFailureConfirmsTheSegmentStaysOnWatchAndKeepsServerDetail() {
        let message = WatchSyncStatusPolicy.failureMessage(
            for: .segmentUploadFailed("segment timing overlaps previous segment")
        )

        XCTAssertTrue(message.hasPrefix("Mac 没有返回有效的接收信息："))
        XCTAssertTrue(message.contains("segment timing overlaps previous segment"))
    }

    func testAcknowledgementMismatchKeepsServerDetail() {
        let message = WatchSyncStatusPolicy.failureMessage(
            for: .acknowledgementMismatch("ack seq 12 != local seq 11")
        )

        XCTAssertTrue(message.hasPrefix("Mac 返回的接收信息与录音不匹配："))
        XCTAssertTrue(message.contains("ack seq 12 != local seq 11"))
    }

    func testServerRejectionMessageIncludesTheHTTPServerDetail() {
        let message = WatchSyncStatusPolicy.connectionFailureMessage(
            for: .serverRejected(statusCode: 409, detail: "segment checksum mismatch")
        )

        XCTAssertTrue(message.contains("HTTP 409"))
        XCTAssertTrue(message.contains("segment checksum mismatch"))
    }

    func testServerRejectionWithoutDetailKeepsTheStatusCodeOnlyForm() {
        XCTAssertEqual(
            WatchSyncStatusPolicy.connectionFailureMessage(
                for: .serverRejected(statusCode: 500, detail: nil)
            ),
            "已连接 Mac，但服务拒绝请求（HTTP 500）。"
        )
    }

    func testConnectionFailuresDistinguishNetworkFromReachableMacRejections() {
        XCTAssertEqual(
            WatchSyncStatusPolicy.connectionFailureMessage(for: .unreachable),
            "Mac 当前不可达，手表本地队列未更改。"
        )
        XCTAssertEqual(
            WatchSyncStatusPolicy.connectionFailureMessage(for: .directAccessDisabled),
            "已连接 Mac，但 Watch 直连功能未开放。"
        )
        XCTAssertEqual(
            WatchSyncStatusPolicy.connectionFailureMessage(for: .unauthorized),
            "已连接 Mac，但访问未授权（HTTP 401）。"
        )
    }

    func testURLSessionTimeoutIsClassifiedAsNetworkUnreachable() {
        XCTAssertEqual(
            WatchSyncStatusPolicy.classifyTransportError(URLError(.timedOut)),
            .unreachable
        )
    }
}
