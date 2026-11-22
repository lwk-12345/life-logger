import Foundation

/// A small, durable, on-device log of everything that decides whether a recording lives.
///
/// `os_log` is gone the moment the app is suspended or killed, and the Watch is rarely
/// connected to Xcode when a recording dies in someone's pocket. This writes the few events
/// that matter (segment rotations, interruptions, recovery attempts, failures, scene changes)
/// to `Application Support/LifeLogger/capture-events.log`, so the reason a recording stopped
/// can be read afterwards — on the Watch itself (首页 → 录音事件), or from the Mac with
/// `xcrun devicectl device copy from --domain-type appDataContainer ...`.
final class WatchCaptureEventLog: @unchecked Sendable {
    static let shared = WatchCaptureEventLog()

    private let queue = DispatchQueue(label: "com.lifelogger.watch.capture-event-log", qos: .utility)
    private let fileURL: URL
    private let archiveURL: URL
    private let maximumBytes = 192 * 1024
    private let formatter: DateFormatter

    private init() {
        let root = (FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true))
            .appendingPathComponent("LifeLogger", isDirectory: true)
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        fileURL = root.appendingPathComponent("capture-events.log")
        archiveURL = root.appendingPathComponent("capture-events.1.log")
        formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "MM-dd HH:mm:ss.SSS"
    }

    func record(_ message: String) {
        let now = Date()
        queue.async { [self] in
            let line = "\(formatter.string(from: now)) \(message)\n"
            guard let data = line.data(using: .utf8) else { return }
            rotateIfNeeded()
            if let handle = try? FileHandle(forWritingTo: fileURL) {
                defer { try? handle.close() }
                _ = try? handle.seekToEnd()
                try? handle.write(contentsOf: data)
            } else {
                try? data.write(to: fileURL, options: .atomic)
            }
        }
    }

    /// Newest lines first, for the on-Watch viewer.
    func recentLines(limit: Int = 40) -> [String] {
        queue.sync {
            guard let text = try? String(contentsOf: fileURL, encoding: .utf8) else { return [] }
            return Array(text.split(separator: "\n").suffix(limit).reversed()).map(String.init)
        }
    }

    func clear() {
        queue.sync {
            try? FileManager.default.removeItem(at: fileURL)
            try? FileManager.default.removeItem(at: archiveURL)
        }
    }

    private func rotateIfNeeded() {
        guard let size = (try? FileManager.default.attributesOfItem(atPath: fileURL.path))?[.size] as? Int,
              size > maximumBytes else { return }
        try? FileManager.default.removeItem(at: archiveURL)
        try? FileManager.default.moveItem(at: fileURL, to: archiveURL)
    }
}
