import Foundation

/// A staged recording the Watch could not read, kept on disk as `<name>.unreadable`.
///
/// Recovery deliberately sets these aside instead of deleting them (the bytes are the only
/// copy of that moment), but nothing ever surfaced or removed them: the file and the session
/// directory holding it stayed on the Watch forever, and `staging/<session>/` could never be
/// pruned while one was inside. This type is what the app lists and deletes.
public struct WatchQuarantinedFile: Identifiable, Equatable, Sendable {
    public let sessionID: String
    public let fileName: String
    public let byteCount: Int64
    public let quarantinedAt: Date?

    public init(sessionID: String, fileName: String, byteCount: Int64, quarantinedAt: Date?) {
        self.sessionID = sessionID
        self.fileName = fileName
        self.byteCount = byteCount
        self.quarantinedAt = quarantinedAt
    }

    public var id: String { path }

    /// Staging-root-relative location, e.g. `watch-session-001/003.m4a.unreadable`.
    public var path: String { "\(sessionID)/\(fileName)" }

    /// The name the segment had before it was set aside, e.g. `003.m4a`.
    public var displayName: String {
        let suffix = ".\(WatchStagedAudioPolicy.quarantinedExtension)"
        guard fileName.hasSuffix(suffix) else { return fileName }
        return String(fileName.dropLast(suffix.count))
    }

    /// Segment number, when the name still carries one. Staged files are written as
    /// `segment-%06d.m4a` with the ledger's 0-based segment index (`segment-000003.m4a` → 3,
    /// the app shows it as 第 4 段). A name with no trailing number has no sequence to claim,
    /// so this stays `nil` rather than guessing.
    public var sequence: Int? {
        let stem = (displayName as NSString).deletingPathExtension
        let digits = stem.reversed().prefix { $0.isNumber }.reversed()
        guard !digits.isEmpty else { return nil }
        return Int(String(digits))
    }
}

/// What a bulk removal actually did, measured per file at deletion time.
public struct WatchQuarantineRemoval: Equatable, Sendable {
    public let deletedCount: Int
    public let failedCount: Int
    public let bytesFreed: Int64

    public init(deletedCount: Int, failedCount: Int, bytesFreed: Int64) {
        self.deletedCount = deletedCount
        self.failedCount = failedCount
        self.bytesFreed = bytesFreed
    }
}

/// Listing and removal of quarantined staged audio. Pure filesystem work so the Watch screen
/// can show real sizes and honest results instead of a hopeful "已清理".
public enum WatchQuarantinePolicy {
    public static func scan(
        stagingRoot: URL,
        fileManager: FileManager = .default
    ) -> [WatchQuarantinedFile] {
        guard let sessionDirectories = try? fileManager.contentsOfDirectory(
            at: stagingRoot,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        ) else {
            return []
        }

        var found: [WatchQuarantinedFile] = []
        for sessionDirectory in sessionDirectories {
            let sessionID = sessionDirectory.lastPathComponent
            guard let contents = try? fileManager.contentsOfDirectory(
                at: sessionDirectory,
                includingPropertiesForKeys: [.fileSizeKey, .contentModificationDateKey],
                options: [.skipsHiddenFiles]
            ) else {
                continue
            }
            for url in contents where WatchStagedAudioPolicy.isQuarantined(url.lastPathComponent) {
                let values = try? url.resourceValues(forKeys: [.fileSizeKey, .contentModificationDateKey])
                found.append(
                    WatchQuarantinedFile(
                        sessionID: sessionID,
                        fileName: url.lastPathComponent,
                        byteCount: Int64(values?.fileSize ?? 0),
                        quarantinedAt: values?.contentModificationDate
                    )
                )
            }
        }

        return found.sorted { $0.path < $1.path }
    }

    public static func totalBytes(_ files: [WatchQuarantinedFile]) -> Int64 {
        files.reduce(0) { $0 + $1.byteCount }
    }

    /// Removes one quarantined file and returns the bytes actually freed.
    ///
    /// The size is re-measured at removal time: a listing can be stale (recovery may have
    /// replaced the file), and reporting a stale number would be a lie about what was freed.
    @discardableResult
    public static func delete(
        _ file: WatchQuarantinedFile,
        stagingRoot: URL,
        fileManager: FileManager = .default
    ) throws -> Int64 {
        let sessionDirectory = stagingRoot.appendingPathComponent(file.sessionID, isDirectory: true)
        let url = sessionDirectory.appendingPathComponent(file.fileName)
        let measured = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        try fileManager.removeItem(at: url)
        pruneIfEmpty(sessionDirectory, fileManager: fileManager)
        return Int64(measured)
    }

    public static func deleteAll(
        _ files: [WatchQuarantinedFile],
        stagingRoot: URL,
        fileManager: FileManager = .default
    ) -> WatchQuarantineRemoval {
        var deletedCount = 0
        var failedCount = 0
        var bytesFreed: Int64 = 0

        for file in files {
            do {
                bytesFreed += try delete(file, stagingRoot: stagingRoot, fileManager: fileManager)
                deletedCount += 1
            } catch {
                failedCount += 1
            }
        }

        return WatchQuarantineRemoval(
            deletedCount: deletedCount,
            failedCount: failedCount,
            bytesFreed: bytesFreed
        )
    }

    /// The entry point stays hidden while there is nothing to clean.
    public static func statusLine(count: Int, totalBytes: Int64) -> String? {
        guard count > 0 else { return nil }
        return "\(count) 个无法读取的录音片段 · \(formattedSize(totalBytes))"
    }

    /// Whether a staging directory that has no session manifest should still put the recorder into
    /// `.failed`.
    ///
    /// Recovery's orphan check treated *any* non-empty directory as unreachable audio. A directory
    /// whose only remaining entries are quarantined files is not that: those bytes were already set
    /// aside by recovery itself, are already listed on the 坏文件 screen, and are already removable
    /// there — failing the recorder over them leaves the app broken for a reason the recorder cannot
    /// fix. Anything recovery could still index keeps blocking, exactly as before.
    public static func blocksRecovery(contents: [String]) -> Bool {
        contents.contains { !WatchStagedAudioPolicy.isQuarantined($0) }
    }

    /// Decimal units (1 KB = 1000 B), one fraction digit, trailing `.0` dropped.
    public static func formattedSize(_ bytes: Int64) -> String {
        let units = ["B", "KB", "MB", "GB", "TB"]
        var value = Double(max(0, bytes))
        var unitIndex = 0
        while value >= 1000, unitIndex < units.count - 1 {
            value /= 1000
            unitIndex += 1
        }

        if unitIndex == 0 {
            return "\(Int(value)) B"
        }
        let rounded = (value * 10).rounded() / 10
        if rounded == rounded.rounded() {
            return "\(Int(rounded)) \(units[unitIndex])"
        }
        return String(format: "%.1f %@", rounded, units[unitIndex])
    }

    private static func pruneIfEmpty(_ directory: URL, fileManager: FileManager) {
        guard let remaining = try? fileManager.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ), remaining.isEmpty else {
            return
        }
        try? fileManager.removeItem(at: directory)
    }
}
