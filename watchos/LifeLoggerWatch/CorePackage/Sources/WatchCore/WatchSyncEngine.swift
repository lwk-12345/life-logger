import Foundation

public struct WatchSegmentAcknowledgement: Equatable, Sendable {
    public let sessionID: String
    public let sequence: Int
    public let sha256: String

    public init(sessionID: String, sequence: Int, sha256: String) {
        self.sessionID = sessionID
        self.sequence = sequence
        self.sha256 = sha256
    }
}

/// Each failure carries the underlying reason verbatim. Mac rejects an upload with a
/// human-readable `detail` (for example "segment timing overlaps previous segment"); that
/// sentence is the only way to tell *why* a sync stopped, so it travels with the error
/// instead of being flattened into a generic message on the Watch.
public enum WatchSyncError: Error, Equatable {
    case acknowledgementMismatch(String)
    /// Segments the ledger still lists but whose files are gone from the Watch. The session is
    /// finished with whatever survived rather than retried indefinitely.
    case segmentsMissingLocally([Int])
    case sessionRegistrationFailed(String)
    case segmentUploadFailed(String)
    case sessionCompletionFailed(String)
}

/// `LocalizedError` so any plain `catch { error.localizedDescription }` (the status line,
/// os_log, the Xcode console) already shows the Mac's own rejection reason.
extension WatchSyncError: LocalizedError {
    public var errorDescription: String? {
        WatchSyncStatusPolicy.failureMessage(for: self)
    }
}

public struct WatchSyncReport: Equatable, Sendable {
    public var sessionsRegistered = 0
    public var segmentsAcknowledged = 0
    public var sessionsCompleted = 0
    /// Sessions whose completion has been handed to the background session and whose answer
    /// has not arrived yet. Tracked separately so it is never mistaken for a failure.
    public var sessionsAwaitingCompletion = 0
    /// Per-session failures from this drain pass. A session that keeps failing (a segment
    /// the Mac rejects for good, for example) is reported here and skipped; it must never
    /// stop the recordings queued behind it, which is how hours of audio used to sit
    /// unsynced behind one stuck session.
    public var failures: [WatchSyncFailure] = []

    public init() {}
}

public struct WatchSyncFailure: Equatable, Sendable {
    public let sessionID: String
    public let error: WatchSyncError

    public init(sessionID: String, error: WatchSyncError) {
        self.sessionID = sessionID
        self.error = error
    }
}

public protocol WatchSessionTransport: Sendable {
    func register(session: RecordingSessionManifest) async throws
    func upload(segment: AudioSegment) async throws -> WatchSegmentAcknowledgement
    func complete(session: RecordingSessionManifest) async throws -> String
    func reportOutboxStatus(_ status: WatchOutboxStatus) async
}

public extension WatchSessionTransport {
    /// Existing transports may ignore Watch queue telemetry.
    func reportOutboxStatus(_ status: WatchOutboxStatus) async {}
}

public struct WatchOutboxStatus: Equatable, Sendable {
    public let pendingSegmentCount: Int
    public let pendingSegmentBytes: Int64
    public let openSessionCount: Int
    public let pendingCompletionCount: Int
    public let syncState: String

    public init(
        pendingSegmentCount: Int,
        pendingSegmentBytes: Int64,
        openSessionCount: Int,
        pendingCompletionCount: Int,
        syncState: String
    ) {
        self.pendingSegmentCount = pendingSegmentCount
        self.pendingSegmentBytes = pendingSegmentBytes
        self.openSessionCount = openSessionCount
        self.pendingCompletionCount = pendingCompletionCount
        self.syncState = syncState
    }
}

/// Drains the durable Watch outbox without ever treating a network send as an ACK.
public actor WatchSyncEngine {
    private let ledger: SegmentLedger
    private let transport: any WatchSessionTransport

    public init(ledger: SegmentLedger, transport: any WatchSessionTransport) {
        self.ledger = ledger
        self.transport = transport
    }

    /// Drains every session that has work left, newest first.
    ///
    /// Failures are collected per session instead of escaping the loop: one session the Mac
    /// keeps refusing (a segment it will never accept, a session it already closed) is
    /// exactly the case that used to strand every later recording behind it. The caller
    /// still learns what went wrong, through `report.failures`.
    public func syncPending(
        onProgress: (@Sendable (_ completed: Int, _ total: Int) -> Void)? = nil
    ) async throws -> WatchSyncReport {
        let sessions = try await ledger.listSessions()
        var report = WatchSyncReport()
        await reportOutboxStatus(sessions: sessions, syncState: "syncing")

        let totalPending = sessions.flatMap(\.segments).filter {
            $0.uploadState != .acknowledged
        }.count

        for session in sessions where !session.segments.isEmpty {
            let hasPendingSegments = session.segments.contains {
                $0.uploadState != .acknowledged
            }
            let needsCompletion = session.stoppedAt != nil && session.macJobID == nil
            guard hasPendingSegments || needsCompletion else { continue }

            do {
                try await drain(
                    session: session,
                    report: &report,
                    totalPending: totalPending,
                    onProgress: onProgress
                )
            } catch let error as WatchSyncError {
                report.failures.append(
                    WatchSyncFailure(sessionID: session.id, error: error)
                )
            } catch {
                report.failures.append(
                    WatchSyncFailure(
                        sessionID: session.id,
                        error: .segmentUploadFailed(String(describing: error))
                    )
                )
            }
        }

        await reportOutboxStatus(syncState: "idle")
        return report
    }

    /// Upload the sealed outbox while capture continues, including older stopped sessions.
    /// The UI's pending count covers this entire queue, not only the newest recording.
    /// Never send /complete on the capture transport; normal sync closes stopped sessions later.
    public func syncSealedSegments(
        sessionID: String,
        onProgress: (@Sendable (_ completed: Int, _ total: Int) -> Void)? = nil
    ) async throws -> WatchSyncReport {
        let sessions = try await ledger.listSessions()
        var report = WatchSyncReport()
        await reportOutboxStatus(sessions: sessions, syncState: "syncing")
        let totalPending = sessions.flatMap(\.segments).filter {
            $0.uploadState != .acknowledged
        }.count
        // Prioritize the live recording, then continue through historical pending audio.
        let ordered = sessions.filter { $0.id == sessionID } + sessions.filter { $0.id != sessionID }
        for session in ordered where session.segments.contains(where: { $0.uploadState != .acknowledged }) {
            do {
                try await drain(
                    session: session, report: &report, totalPending: totalPending,
                    onProgress: onProgress, allowsCompletion: false
                )
            } catch let error as WatchSyncError {
                report.failures.append(WatchSyncFailure(sessionID: session.id, error: error))
            } catch {
                report.failures.append(WatchSyncFailure(
                    sessionID: session.id, error: .segmentUploadFailed(String(describing: error))
                ))
            }
        }
        await reportOutboxStatus(syncState: "idle")
        return report
    }

    private func reportOutboxStatus(syncState: String) async {
        guard let sessions = try? await ledger.listSessions() else { return }
        await reportOutboxStatus(sessions: sessions, syncState: syncState)
    }

    private func reportOutboxStatus(
        sessions: [RecordingSessionManifest],
        syncState: String
    ) async {
        let pending = sessions.flatMap(\.segments).filter {
            $0.uploadState != .acknowledged
        }
        let byteTotal = pending.reduce(Int64(0)) { total, segment in
            let bytes = Int64(max(0, segment.byteCount))
            let (sum, overflow) = total.addingReportingOverflow(bytes)
            return overflow ? Int64.max : sum
        }
        await transport.reportOutboxStatus(
            WatchOutboxStatus(
                pendingSegmentCount: pending.count,
                pendingSegmentBytes: byteTotal,
                openSessionCount: sessions.filter { $0.stoppedAt == nil }.count,
                pendingCompletionCount: sessions.filter {
                    $0.stoppedAt != nil && $0.macJobID == nil
                }.count,
                syncState: syncState
            )
        )
    }

    private func drain(
        session: RecordingSessionManifest,
        report: inout WatchSyncReport,
        totalPending: Int = 0,
        onProgress: (@Sendable (_ completed: Int, _ total: Int) -> Void)? = nil,
        allowsCompletion: Bool = true
    ) async throws {
        do {
            try await transport.register(session: session)
        } catch {
            throw WatchSyncError.sessionRegistrationFailed(String(describing: error))
        }
        report.sessionsRegistered += 1
        var unrecoverableSequences: [Int] = []

        for segment in session.segments.sorted(by: { $0.sequence < $1.sequence }) {
            guard segment.uploadState != .acknowledged else { continue }
            // A segment we previously gave up on is only skipped when its file is really gone.
            // If it is still there, the earlier failure was transient (network, suspension) and
            // it deserves another attempt — otherwise a flaky sync would silently shorten a
            // recording that is fully intact on disk.
            if segment.uploadState == .failed,
               !FileManager.default.fileExists(atPath: segment.localURL.path) {
                unrecoverableSequences.append(segment.sequence)
                continue
            }
            try await ledger.markUploading(
                sessionID: segment.sessionID,
                sequence: segment.sequence
            )
            let acknowledgement: WatchSegmentAcknowledgement
            do {
                acknowledgement = try await transport.upload(segment: segment)
            } catch {
                try? await ledger.markFailed(
                    sessionID: segment.sessionID,
                    sequence: segment.sequence
                )
                // A segment the Mac will not take must not strand the rest of the recording.
                // The session is reported and retried later; the surviving segments of *other*
                // sessions still go out in this same pass.
                report.failures.append(
                    WatchSyncFailure(
                        sessionID: session.id,
                        error: .segmentUploadFailed(String(describing: error))
                    )
                )
                return
            }
            guard acknowledgement.sessionID == segment.sessionID,
                  acknowledgement.sequence == segment.sequence,
                  acknowledgement.sha256 == segment.sha256 else {
                try? await ledger.markFailed(
                    sessionID: segment.sessionID,
                    sequence: segment.sequence
                )
                throw WatchSyncError.acknowledgementMismatch(
                    "local session \(segment.sessionID) seq \(segment.sequence) sha \(segment.sha256) != ack session \(acknowledgement.sessionID) seq \(acknowledgement.sequence) sha \(acknowledgement.sha256)"
                )
            }
            try await ledger.acknowledge(
                sessionID: segment.sessionID,
                sequence: segment.sequence,
                sha256: acknowledgement.sha256
            )
            report.segmentsAcknowledged += 1
            if let onProgress {
                onProgress(report.segmentsAcknowledged, max(report.segmentsAcknowledged, totalPending))
            }
            if report.segmentsAcknowledged.isMultiple(of: 10) {
                await reportOutboxStatus(syncState: "syncing")
            }
        }

        guard allowsCompletion, session.stoppedAt != nil,
              let refreshed = try await ledger.loadSession(id: session.id),
              refreshed.macJobID == nil else {
            return
        }
        // The Mac requires every sequence before completion; missing local audio stays visible.
        let stillPending = refreshed.segments.filter { $0.uploadState == .pending }
        let unresolved = refreshed.segments.filter {
            $0.uploadState != .acknowledged && $0.uploadState != .pending
        }
        guard stillPending.isEmpty, !refreshed.segments.isEmpty else { return }
        // A session whose every segment is gone *and* that the Mac holds nothing for is a husk:
        // completing it would only earn a 409 ("nothing to close") on every sync. Report it and
        // leave it alone — the user decides whether to clean it up.
        let acknowledgedCount = refreshed.segments.filter { $0.uploadState == .acknowledged }.count
        if acknowledgedCount == 0, unresolved.count == refreshed.segments.count {
            report.failures.append(
                WatchSyncFailure(
                    sessionID: session.id,
                    error: .segmentsMissingLocally(unresolved.map(\.sequence).sorted())
                )
            )
            return
        }
        let lost = Set(unresolved.map(\.sequence)).union(unrecoverableSequences)
        if !lost.isEmpty {
            report.failures.append(
                WatchSyncFailure(
                    sessionID: session.id,
                    error: .segmentsMissingLocally(lost.sorted())
                )
            )
            return
        }

        let jobID: String
        do {
            jobID = try await transport.complete(session: refreshed)
        } catch WatchSyncTransportOutcome.completionQueued {
            // The completion is in the system's hands now. It is *not* a failure and must not
            // be retried in this pass or reported to the user: the background delegate writes
            // `macJobID` when the Mac answers, and until then this session simply stays
            // unfinished locally, which is the truthful state.
            report.sessionsAwaitingCompletion += 1
            return
        } catch {
            throw WatchSyncError.sessionCompletionFailed(String(describing: error))
        }
        try await ledger.markSessionImported(id: session.id, macJobID: jobID)
        report.sessionsCompleted += 1
    }
}

/// Transport-level outcomes that are not failures. The app target's transport throws this
/// when it hands a completion to the background session; the engine treats it as "in flight".
public enum WatchSyncTransportOutcome: Error, Equatable {
    case completionQueued
}
