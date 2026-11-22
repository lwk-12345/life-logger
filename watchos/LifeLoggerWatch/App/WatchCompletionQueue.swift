import Foundation
import os

private let completionLog = Logger(subsystem: "com.lifelogger.watch", category: "completion")

/// Asynchronous completion requests using a foreground session.
/// Results update the durable ledger; interrupted delivery retries on a later idle sync.
final class WatchCompletionQueue: NSObject, @unchecked Sendable {
    let delivery: WatchBackgroundDelivery
    private let deviceID: String
    private let baseURL: URL
    private var session: URLSession?
    /// `attach()` is reached from the main actor (app launch, background wake) *and* from the
    /// transport actor (when a session ends), and the delegate callbacks arrive on a URLSession
    /// thread. Every read and write of `session` is serialised here: two unsynchronised
    /// check-then-set passes could otherwise build two background sessions for the same
    /// identifier, which Apple leaves undefined.
    private let stateLock = NSLock()

    init(
        delivery: WatchBackgroundDelivery,
        deviceID: String,
        baseURL: URL
    ) {
        self.delivery = delivery
        self.deviceID = deviceID
        self.baseURL = baseURL
        super.init()
    }

    /// Lazily create the request session.
    func attach() {
        stateLock.lock()
        defer { stateLock.unlock() }
        attachLocked()
    }

    /// Caller must already hold `stateLock`.
    private func attachLocked() {
        guard session == nil else { return }
        // Deliberately a *foreground* session. `URLSessionConfiguration.background` is a
        // privileged API on watchOS and the URL loading system aborts the process when it is
        // brought up without the matching background-task plumbing: the app died with
        // SIGABRT inside CFNetwork a few seconds into launch, every launch, once completion
        // requests started going out. The completion POST is tiny and is sent the moment a
        // recording stops, so a normal session delivers it in milliseconds; if the app is
        // suspended mid-flight the session simply stays unfinished locally and the next
        // foreground sync sends it again.
        let configuration = URLSessionConfiguration.default
        configuration.waitsForConnectivity = true
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 600
        // No delegate: every task uses a completion handler, so task-level delegate callbacks
        // are never delivered anyway. Passing `self` here would require conforming to
        // `URLSessionDelegate` for methods that never fire.
        session = URLSession(configuration: configuration)
    }

    /// Returns after enqueue, before the handler records success or refusal in the ledger.
    func enqueue(session manifest: RecordingSessionManifest) throws {
        try WatchCaptureNetworkGate.shared.check()
        let sessionID = manifest.id
        let segmentCount = manifest.segments.count
        // Everything that can throw happens before the lock is taken, and the lock is held only
        // for the session lookup. The previous version took the lock first and left it held on
        // two `throw` paths, so the next call blocked forever and sync hung for good.
        guard let body = WatchCompletionRequest.body(session: manifest) else {
            throw WatchSyncTransportError.sessionNotFinished
        }
        var url = baseURL
        for component in WatchCompletionRequest.path(deviceID: deviceID, sessionID: sessionID) {
            url.appendPathComponent(component)
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(sessionID, forHTTPHeaderField: "X-LifeLogger-Session-Id")
        // This request is built here rather than through MacWatchSessionTransport's
        // `makeRequest`, so it has to carry the Watch credential itself. Missing it here is
        // what made `/complete` answer 401 while `/segments` sailed through.
        request.setValue(
            WatchUploadCredential.headerValue(),
            forHTTPHeaderField: WatchUploadCredential.headerName
        )
        let bodyFile = try Self.stagingFile(for: body)

        let session: URLSession? = stateLock.withLock {
            attachLocked()
            return self.session
        }
        guard let session else {
            try? FileManager.default.removeItem(at: bodyFile)
            throw WatchSyncTransportError.invalidResponse
        }

        // The completion-handler form is deliberate: the response body is handed to us directly,
        // so the outcome is recorded even though this is no longer a background session.
        do {
            try WatchCaptureNetworkGate.shared.start {
                session.uploadTask(with: request, fromFile: bodyFile) { [weak self] data, response, error in
                    // The body file has been consumed by the upload; leaving it behind would grow
                    // `Application Support/LifeLogger/completion/` by one file per completion forever.
                    try? FileManager.default.removeItem(at: bodyFile)
                    guard let self else { return }
                    let statusCode = (response as? HTTPURLResponse)?.statusCode
                    let responseBody = data ?? Data()
                    Task {
                        await self.delivery.record(
                            sessionID: sessionID,
                            outcome: WatchCompletionRequest.outcome(
                                from: responseBody,
                                statusCode: statusCode ?? 0
                            )
                        )
                        if let error {
                            await self.delivery.reportIssue(String(describing: error))
                        }
                    }
                }
            }
        } catch {
            try? FileManager.default.removeItem(at: bodyFile)
            throw error
        }
        completionLog.notice(
            "queued completion for \(sessionID, privacy: .public) (\(segmentCount, privacy: .public) segments)"
        )
    }

    // MARK: - Helpers

    private static func stagingFile(for body: Data) throws -> URL {
        let root = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
        let directory = root
            .appendingPathComponent("LifeLogger", isDirectory: true)
            .appendingPathComponent("completion", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let file = directory.appendingPathComponent("complete-\(UUID().uuidString).json")
        try body.write(to: file, options: .atomic)
        return file
    }
}


/// Where a background completion ends up: the durable ledger, or a surface the user can read.
///
/// The completion queue funnels every outcome through here, so an accepted completion is
/// recorded exactly once, and a refusal keeps the Mac's own wording for the screen.
