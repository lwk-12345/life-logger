    /// Remembers a stopped-session sync request that arrives while capture upload owns
    /// the coordinator or while the capture network gate still blocks ordinary sync.
public struct WatchSyncFollowUpPolicy: Sendable {
    private(set) var isRequested = false
    private(set) var isLiveUploadRequested = false

    public init() {}

    /// Returns true when the caller must defer the full sync.
    public mutating func shouldDeferFullSync(isSyncing: Bool, captureBlocked: Bool) -> Bool {
        guard isSyncing || captureBlocked else {
            isRequested = false
            return false
        }
        isRequested = true
        return true
    }

    /// A still-blocked gate keeps the request pending for the stop-triggered sync.
    public mutating func liveUploadDidFinish(captureBlocked: Bool) -> Bool {
        guard isRequested, !captureBlocked else { return false }
        isRequested = false
        return true
    }

    /// A live upload tap during another live upload is not an error. Keep one follow-up
    /// request so the newly sealed/current pending segments are drained after the active pass.
    public mutating func shouldDeferLiveUpload(isSyncing: Bool) -> Bool {
        guard isSyncing else { return false }
        isLiveUploadRequested = true
        return true
    }

    public mutating func consumeLiveUploadRequest() -> Bool {
        guard isLiveUploadRequested else { return false }
        isLiveUploadRequested = false
        return true
    }
}