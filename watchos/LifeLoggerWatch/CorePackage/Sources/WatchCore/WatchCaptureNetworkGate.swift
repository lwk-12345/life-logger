import Foundation

/// The same lock guards capture state and network task creation/resume.
final class WatchCaptureNetworkGate: @unchecked Sendable {
    static let shared = WatchCaptureNetworkGate()
    private let lock = NSLock()
    private var blocked = false
    /// 【2026-10-15 改】每个在途任务连同它的「是否允许在录音期运行」一起记录。
    /// 原来只存 `[URLSessionTask]`，导致关闸时无法区分两者（见 setBlocked 的说明）。
    private var tasks: [(task: URLSessionTask, allowDuringCapture: Bool)] = []
    var isBlocked: Bool { lock.withLock { blocked } }

    func setBlocked(_ value: Bool) {
        lock.withLock {
            let newlyBlocked = !blocked && value
            blocked = value
            tasks.removeAll { $0.task.state == .completed }
            if newlyBlocked {
                // 【2026-10-15 修】原来这里是无差别 `tasks.forEach { $0.cancel() }`，
                // 把 `allowDuringCapture: true` 的**边录边传**任务也一起杀了。
                // 后果：用户一按录音（state → .recording）所有在途上传被取消、新上传被
                // check() 拒绝 —— 「边录音边上传」从来没有真正生效过，积压的几百个分段
                // 永远排不完。现在只取消**不允许**在录音期运行的任务，边录边传的保留。
                tasks.removeAll { entry in
                    if entry.allowDuringCapture { return false }
                    entry.task.cancel()
                    return true
                }
            }
        }
    }

    func check(allowDuringCapture: Bool = false) throws {
        guard allowDuringCapture || !isBlocked else { throw CancellationError() }
    }

    func start(
        allowDuringCapture: Bool = false,
        _ create: () -> URLSessionTask
    ) throws {
        try lock.withLock {
            guard allowDuringCapture || !blocked else { throw CancellationError() }
            tasks.removeAll { $0.task.state == .completed }
            let task = create()
            tasks.append((task: task, allowDuringCapture: allowDuringCapture))
            task.resume()
        }
    }

    func perform(
        session: URLSession,
        request: URLRequest,
        fileURL: URL? = nil,
        allowDuringCapture: Bool = false
    ) async throws -> (Data, URLResponse) {
        try await withCheckedThrowingContinuation { continuation in
            do {
                try start(allowDuringCapture: allowDuringCapture) {
                    let completion: @Sendable (Data?, URLResponse?, Error?) -> Void = { data, response, error in
                        if let error {
                            continuation.resume(throwing: error)
                        } else if let response {
                            continuation.resume(returning: (data ?? Data(), response))
                        } else {
                            continuation.resume(throwing: URLError(.badServerResponse))
                        }
                    }
                    if let fileURL {
                        return session.uploadTask(with: request, fromFile: fileURL, completionHandler: completion)
                    }
                    return session.dataTask(with: request, completionHandler: completion)
                }
            } catch {
                continuation.resume(throwing: error)
            }
        }
    }
}
