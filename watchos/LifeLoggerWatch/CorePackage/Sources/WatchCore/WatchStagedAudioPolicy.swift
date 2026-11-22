/// Recovery of staged Watch audio: which staged files can be trusted, and what a
/// problem found during recovery means for the recorder as a whole.
///
/// A staged `.m4a` that was interrupted mid-write is unreadable (`AVAudioFile(forReading:)`
/// throws a CoreAudio error). That single file used to abort recovery for its whole session,
/// leave the session open, and set the app to `.failed` on every launch — so the upload queue
/// was never reached and hours of audio appeared "unsyncable". Such a file is quarantined by
/// renaming instead: the bytes stay on the Watch (nothing is deleted), recovery continues with
/// the readable files, and the app no longer reports itself as broken.
public enum WatchStagedAudioPolicy {
    public static let quarantinedExtension = "unreadable"

    public static func quarantineFileName(for stagedFileName: String) -> String {
        if isQuarantined(stagedFileName) {
            return stagedFileName
        }
        return "\(stagedFileName).\(quarantinedExtension)"
    }

    public static func isQuarantined(_ fileName: String) -> Bool {
        fileName.hasSuffix(".\(quarantinedExtension)")
    }

    public static func recoveryOutcome(
        unreadableStagedFile: String,
        underlyingError: String
    ) -> WatchRecoveryOutcome {
        .quarantined(
            stagedFile: unreadableStagedFile,
            reason: "Staged audio kept on Watch: \(unreadableStagedFile) · \(underlyingError)"
        )
    }

    public static func sessionCompletion(
        isStopped: Bool,
        hasUnresolvedStagedAudio: Bool
    ) -> CaptureSessionCompletionDisposition {
        CaptureSessionCompletionDisposition.resolve(
            hasUnresolvedStagedAudio: hasUnresolvedStagedAudio
        )
    }
}

public enum WatchRecoveryOutcome: Equatable, Sendable {
    case quarantined(stagedFile: String, reason: String)
    case blocking(String)
}

/// A problem found while recovering staged audio, and whether it should stop recording.
public enum WatchRecoveryIssue: Equatable, Sendable {
    /// One unreadable staged file, set aside as `<name>.unreadable`; the rest recovered.
    case quarantined(stagedFile: String, detail: String)
    /// Something we cannot work around (a staged file with no session, an unreadable ledger).
    case blocking(String)

    public var blocksRecording: Bool {
        if case .blocking = self { return true }
        return false
    }

    /// Older builds surfaced localised system strings here, so the wording stays English and
    /// explicit: the file is named, and it is stated that nothing was deleted.
    public var statusMessage: String {
        switch self {
        case .quarantined(let stagedFile, let detail):
            let quarantinedName = WatchStagedAudioPolicy.quarantineFileName(for: stagedFile)
            return "Could not read staged audio (kept on Watch as \(quarantinedName)): \(detail)"
        case .blocking(let reason):
            return "Could not recover all saved audio. \(reason)"
        }
    }
}
