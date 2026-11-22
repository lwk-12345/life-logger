import Foundation

public struct WatchSegmentTiming: Equatable, Sendable {
    public let startOffsetMilliseconds: Int64
    public let durationMilliseconds: Int64

    public init(startOffsetMilliseconds: Int64, durationMilliseconds: Int64) {
        self.startOffsetMilliseconds = startOffsetMilliseconds
        self.durationMilliseconds = durationMilliseconds
    }
}

public enum WatchSegmentTimingPolicy {
    public static func startOffset(sequence: Int, elapsedMilliseconds: Int64) -> Int64 {
        guard sequence > 0 else { return 0 }
        return max(0, elapsedMilliseconds)
    }

    public static func recovery(
        sequence: Int,
        measuredDurationMilliseconds: Int64,
        manifestSegments: [AudioSegment]
    ) -> WatchSegmentTiming {
        if let committedSegment = manifestSegments.first(where: { $0.sequence == sequence }) {
            return WatchSegmentTiming(
                startOffsetMilliseconds: committedSegment.startOffsetMilliseconds,
                durationMilliseconds: committedSegment.durationMilliseconds
            )
        }
        let previousEnd = manifestSegments.first(where: { $0.sequence == sequence - 1 })
            .map { $0.startOffsetMilliseconds + $0.durationMilliseconds } ?? 0
        return WatchSegmentTiming(
            startOffsetMilliseconds: max(Int64(max(0, sequence)) * 60_000, previousEnd),
            durationMilliseconds: measuredDurationMilliseconds
        )
    }
}

public enum CaptureSessionCompletionDisposition: Equatable, Sendable {
    case finishManifest
    case keepOpenForRecovery

    public static func resolve(hasUnresolvedStagedAudio: Bool) -> Self {
        hasUnresolvedStagedAudio ? .keepOpenForRecovery : .finishManifest
    }
}

/// The rotation contract between this Watch app and the Mac receiver.
///
/// The Mac refuses any segment whose declared duration is 0 or over 61 s
/// (`watch_stream.py`: "segment timing out of range"), and the rotation is driven by
/// `AVAudioRecorder.record(forDuration:)` using `segmentDurationSeconds` below. When that
/// duration argument went missing, recordings stopped rotating entirely: one two-hour file
/// was rejected forever, nothing uploaded, and a mid-recording kill lost the whole session.
/// These rules exist so that cannot regress silently again.
public enum WatchSegmentRotationPolicy {
    public static let segmentDurationSeconds: TimeInterval = 60
    public static let segmentDurationMilliseconds: Int64 = 60_000
    /// The Mac's hard limit, with the encoder-padding allowance it already grants.
    public static let maximumAcceptedDurationMilliseconds: Int64 = 61_000

    public static var declaredDurationMilliseconds: Int64 {
        Int64((segmentDurationSeconds * 1_000).rounded())
    }

    /// True when a segment recorded for exactly one rotation fits inside what the Mac accepts.
    public static var rotationFitsMacLimits: Bool {
        declaredDurationMilliseconds > 0
            && declaredDurationMilliseconds <= maximumAcceptedDurationMilliseconds
    }
}

/// Defines when a live recording has accumulated enough sealed segments to send a batch.
public enum WatchSegmentUploadPolicy {
    /// Live upload begins as soon as the first sealed segment is ready (~60s).
    public static let minimumPendingSegments = 1

    public static func shouldUpload(
        isRecording: Bool,
        pendingSegmentCount: Int
    ) -> Bool {
        isRecording && pendingSegmentCount >= minimumPendingSegments
    }
}
