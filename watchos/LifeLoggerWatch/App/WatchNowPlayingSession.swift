import Foundation
import MediaPlayer

/// Publishes the recording to the system as a **now playing** audio session.
///
/// Why this exists: watchOS decides the *form and placement* of an audio indicator from
/// the role the audio session declares. A bare `.record` session only qualifies for the
/// hardware microphone privacy indicator — a small orange dot that the system owns and
/// that no third-party API can move, resize or recolor. Apple's own players, which sit at
/// the **top center of the watch face** and are tappable to reopen the app, are *playback*
/// sessions that publish Now Playing metadata.
///
/// `MPNowPlayingInfoCenter` and `MPRemoteCommandCenter` are both available on watchOS 5.0+
/// (verified against the WatchOS SDK headers), so a long-running recording can publish the
/// same metadata and ask the system to treat it as an audio app instead of a bare
/// microphone client.
///
/// This is a hypothesis-driven change: it is the only public API surface that can plausibly
/// change *which* system indicator watchOS shows for a background recording.
@MainActor
final class WatchNowPlayingSession {
    private var didRegisterRemoteCommands = false
    private var activeTitle = WatchNowPlayingSession.title
    private var elapsedProvider: (@MainActor () -> Double)?

    private static let title = "Life Logger"

    func begin(title: String = WatchNowPlayingSession.title, elapsed: @escaping @MainActor () -> Double) {
        registerRemoteCommandsIfNeeded()
        activeTitle = title
        elapsedProvider = elapsed
        publish(title: title, seconds: elapsed())
    }

    /// Re-publishes the current metadata once, without starting a timer.
    ///
    /// Call this on foreground returns: watchOS settles the indicator's placement at
    /// discrete moments, so re-asserting the identity makes it re-evaluate instead of
    /// keeping a stale one.
    func refresh() {
        guard let elapsedProvider else { return }
        publish(title: activeTitle, seconds: elapsedProvider())
    }

    func end() {
        elapsedProvider = nil
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        disableRemoteCommands()
    }

    private func publish(title: String, seconds: Double) {
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: title,
            MPMediaItemPropertyArtist: "已录 " + Self.elapsedText(seconds),
            MPNowPlayingInfoPropertyElapsedPlaybackTime: seconds,
            MPNowPlayingInfoPropertyPlaybackRate: 0.0,
            MPNowPlayingInfoPropertyDefaultPlaybackRate: 0.0,
        ]
        info[MPMediaItemPropertyAlbumTitle] = "正在录音"
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    /// Remote commands are deliberately all disabled.
    ///
    /// The app is a recording app, not a media player. Keeping remote playback commands
    /// disabled prevents mediaserverd from treating the app as a media playback source,
    /// which would cause AVAudioSession category .record activation conflicts and Bluetooth
    /// headset routing issues.
    private func registerRemoteCommandsIfNeeded() {
        guard !didRegisterRemoteCommands else { return }
        didRegisterRemoteCommands = true
        let center = MPRemoteCommandCenter.shared()
        center.playCommand.isEnabled = false
        center.pauseCommand.isEnabled = false
        center.togglePlayPauseCommand.isEnabled = false
        center.stopCommand.isEnabled = false
    }

    private func disableRemoteCommands() {
        let center = MPRemoteCommandCenter.shared()
        center.playCommand.isEnabled = false
        center.pauseCommand.isEnabled = false
        center.togglePlayPauseCommand.isEnabled = false
        center.stopCommand.isEnabled = false
    }

    private static func elapsedText(_ seconds: Double) -> String {
        let total = max(0, Int(seconds.rounded(.down)))
        let hours = total / 3_600
        let minutes = (total / 60) % 60
        let secs = total % 60
        return hours > 0
            ? String(format: "%02d:%02d:%02d", hours, minutes, secs)
            : String(format: "%02d:%02d", minutes, secs)
    }
}
