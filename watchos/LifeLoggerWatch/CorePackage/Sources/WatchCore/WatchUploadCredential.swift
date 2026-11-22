import Foundation

/// The credential the Watch presents on the Mac endpoints kept open for it.
///
/// Why this exists: those endpoints used to be exempt from the bearer token purely because the
/// request arrived from a private-network address, so anything on the same network could create
/// sessions and push segments with no credential at all. A Watch cannot be asked to type a
/// token, so the value is compiled into the app, sent as a header, and compared by the Mac.
///
/// Scope, stated honestly: this stops unrelated devices on the LAN. It is not a defence against
/// someone who can unpack the app bundle — the alternative (a per-session handshake) costs a
/// re-install per rotation, which is the tradeoff deliberately not taken here.
public enum WatchUploadCredential {
    public static let headerName = "X-LifeLogger-Watch-Key"

    /// Baked into this build; the Mac stores the same value in its `secrets.json`.
    public static let embedded = "lifelogger-watch-upload-key-v1"

    /// Overridable at runtime so a deployment can rotate the key without a source change.
    public static func current(defaults: UserDefaults = .standard) -> String {
        if let stored = defaults.string(forKey: "lifeLogger.watch.uploadKey"),
           !stored.isEmpty {
            return stored
        }
        return embedded
    }

    public static func headerValue(defaults: UserDefaults = .standard) -> String {
        current(defaults: defaults)
    }

    /// Constant-time comparison, for the Mac side's check.
    public static func matches(_ candidate: String?, expected: String = embedded) -> Bool {
        guard let candidate, candidate.utf8.count == expected.utf8.count else { return false }
        return zip(candidate.utf8, expected.utf8).reduce(0) { $0 | ($1.0 ^ $1.1) } == 0
    }
}
