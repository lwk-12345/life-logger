import CryptoKit
import Foundation
import AVFAudio
#if canImport(AudioToolbox) && !os(watchOS)
import AudioToolbox
#endif

/// Shared recording policy. Telemetry reads each sealed file, not this target.
public enum WatchAudioEncoding {
    // Use the highest bitrate CoreAudio allows for AAC at 16 kHz mono (48 kbps).
    // Evidence: work/watch-bitrate-v68-20261107/report.md.
    public static let bitRate = 48_000

    /// Encoded stream's average bits/second, excluding container overhead.
    /// Missing telemetry never prevents retaining/uploading the audio itself.
    public static func measuredBitRate(at url: URL) -> Int? {
        #if canImport(AudioToolbox) && !os(watchOS)
        var file: AudioFileID?
        guard AudioFileOpenURL(url as CFURL, .readPermission, 0, &file) == noErr,
              let file else { return nil }
        defer { AudioFileClose(file) }
        var bitRate: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        guard AudioFileGetProperty(file, kAudioFilePropertyBitRate, &size, &bitRate) == noErr,
              bitRate > 0 else { return nil }
        return Int(bitRate)
        #else
        guard let file = try? AVAudioFile(forReading: url),
              file.processingFormat.sampleRate > 0,
              file.length > 0 else { return nil }
        return WatchAudioEncoding.bitRate
        #endif
    }
}

public enum FinalizedAudio {
    /// Recorder.currentTime is valid only during recording, not in its finished callback.
    public static func durationMilliseconds(at url: URL) throws -> Int64 {
        let file = try AVAudioFile(forReading: url)
        let milliseconds = Double(file.length) / file.processingFormat.sampleRate * 1000
        guard milliseconds.isFinite, milliseconds >= 1, milliseconds < Double(Int64.max) else {
            throw SegmentLedgerError.invalidSegmentTiming
        }
        return Int64(milliseconds.rounded())
    }
}

public enum SegmentUploadState: String, Codable, Equatable, Sendable {
    case pending
    case uploading
    case failed
    case acknowledged
}

public enum WatchBatteryState: String, Codable, Equatable, Sendable {
    case unknown
    case unplugged
    case charging
    case full
}

public struct WatchBatterySnapshot: Codable, Equatable, Sendable {
    public let levelPercent: Int?
    public let state: WatchBatteryState

    public init(level: Double, state: WatchBatteryState) {
        if level.isFinite, (0...1).contains(level) {
            levelPercent = Int((level * 100).rounded())
        } else {
            levelPercent = nil
        }
        self.state = state
    }

    public static func capture(level: Double, state: WatchBatteryState) -> Self? {
        let snapshot = Self(level: level, state: state)
        return snapshot.levelPercent == nil ? nil : snapshot
    }

    /// WatchKit reports `batteryLevel == -1` until it has published its first reading, so
    /// the first segment after a fresh launch (and any read taken before the system
    /// notification lands) has no usable percentage. `capture` drops such a reading — that
    /// is what left every manifest segment without battery telemetry — so the caller keeps
    /// the last good snapshot and reuses it for the segment that could not be read.
    ///
    /// A carried-forward snapshot is real, previously measured data for the same session,
    /// never an invented percentage.
    public static func resolved(
        level: Double,
        state: WatchBatteryState,
        previous: WatchBatterySnapshot?
    ) -> WatchBatterySnapshot? {
        capture(level: level, state: state) ?? previous
    }

    /// True when this snapshot came from a usable device reading rather than being carried
    /// forward from an earlier one; the log stream uses it to show which segments are live.
    public func isFreshReading(level: Double, state: WatchBatteryState) -> Bool {
        let reading = Self(level: level, state: state)
        return reading.levelPercent != nil && reading == self
    }
}

public struct WatchHealthSnapshot: Codable, Equatable, Sendable {
    public let heartRateBPM: Double?
    /// 【V68】旧字段，保留不删：V67 及以前的手表只发这个（裸的步数，没有日期）。
    /// 新版本同时发 `stepsToday` + `stepsDate`，服务端优先用新字段。
    public let stepCount: Int?
    /// 【V68】当天 0 点（手表本地时区）起的累计步数。
    public let stepsToday: Int?
    /// 【V68】`stepsToday` 所属的自然日 `YYYY-MM-DD`（手表本地时区）。
    public let stepsDate: String?
    /// 【V68】该步数的采样时刻（ISO8601）。
    public let stepsSampledAt: String?
    public let sleep: WatchSleepSummary?

    public init(
        heartRateBPM: Double? = nil,
        stepCount: Int? = nil,
        stepsToday: Int? = nil,
        stepsDate: String? = nil,
        stepsSampledAt: String? = nil,
        sleep: WatchSleepSummary? = nil
    ) {
        self.heartRateBPM = heartRateBPM
        self.stepCount = stepCount
        self.stepsToday = stepsToday
        self.stepsDate = stepsDate
        self.stepsSampledAt = stepsSampledAt
        self.sleep = sleep
    }
}

/// 一晚睡眠的汇总。`date` 按醒来那天（UTC+8）归档；字段名与 Mac 端 SleepSessionRecord 对齐。
public struct WatchSleepSummary: Codable, Equatable, Sendable {
    public let date: String
    public let startTime: String
    public let endTime: String
    public let totalMinutes: Int
    public let deepMinutes: Int
    public let coreMinutes: Int
    public let remMinutes: Int
    public let awakeMinutes: Int

    public init(date: String, startTime: String, endTime: String, totalMinutes: Int,
                deepMinutes: Int, coreMinutes: Int, remMinutes: Int, awakeMinutes: Int) {
        self.date = date
        self.startTime = startTime
        self.endTime = endTime
        self.totalMinutes = totalMinutes
        self.deepMinutes = deepMinutes
        self.coreMinutes = coreMinutes
        self.remMinutes = remMinutes
        self.awakeMinutes = awakeMinutes
    }

    enum CodingKeys: String, CodingKey {
        case date
        case startTime = "start_time"
        case endTime = "end_time"
        case totalMinutes = "total_minutes"
        case deepMinutes = "deep_minutes"
        case coreMinutes = "core_minutes"
        case remMinutes = "rem_minutes"
        case awakeMinutes = "awake_minutes"
    }

    /// Mac 端读取 `X-LifeLogger-Sleep` 请求头用的紧凑 JSON；编码失败返回 nil。
    public func headerValue() -> String? {
        guard let data = try? JSONEncoder().encode(self) else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

public struct WatchSessionPin: Codable, Equatable, Sendable, Identifiable {
    public let pinID: String
    public let sessionID: String
    public let type: String
    public let recordedAt: Date
    public let offsetMilliseconds: Int64

    public var id: String { pinID }

    public init(
        pinID: String = "pin_\(UUID().uuidString)",
        sessionID: String,
        type: String,
        recordedAt: Date = Date(),
        offsetMilliseconds: Int64
    ) {
        self.pinID = pinID
        self.sessionID = sessionID
        self.type = type
        self.recordedAt = recordedAt
        self.offsetMilliseconds = offsetMilliseconds
    }
}

public struct WatchActiveCapsule: Codable, Equatable, Sendable {
    public let startedAt: Date
    public let startOffsetMilliseconds: Int64

    public init(startedAt: Date, startOffsetMilliseconds: Int64) {
        self.startedAt = startedAt
        self.startOffsetMilliseconds = startOffsetMilliseconds
    }
}

public struct WatchSessionCapsule: Codable, Equatable, Sendable {
    public let startOffsetMilliseconds: Int64
    public let endOffsetMilliseconds: Int64
    public let startedAt: Date
    public let endedAt: Date

    public init(
        startOffsetMilliseconds: Int64,
        endOffsetMilliseconds: Int64,
        startedAt: Date,
        endedAt: Date
    ) {
        self.startOffsetMilliseconds = startOffsetMilliseconds
        self.endOffsetMilliseconds = endOffsetMilliseconds
        self.startedAt = startedAt
        self.endedAt = endedAt
    }
}

public struct AudioSegment: Codable, Equatable, Identifiable, Sendable {
    public let sessionID: String
    public let sequence: Int
    public let startOffsetMilliseconds: Int64
    public let durationMilliseconds: Int64
    public let localURL: URL
    public let byteCount: Int
    public let sha256: String
    /// Measured encoded stream bitrate (bits/second); nil in pre-V68 manifests.
    public let audioBitrate: Int?
    public var uploadState: SegmentUploadState
    public let batterySnapshot: WatchBatterySnapshot?
    public let healthSnapshot: WatchHealthSnapshot?
    public let location: WatchLocationSnapshot?
    public let pins: [WatchSessionPin]?

    public var id: String { "\(sessionID)-\(sequence)" }
    public var effectivePins: [WatchSessionPin] { pins ?? [] }
    public var audioBitrateHeaderValue: String? {
        // Old queued segments lack metadata; inspect their own file, not V68's target.
        guard let rate = audioBitrate ?? WatchAudioEncoding.measuredBitRate(at: localURL),
              rate > 0 else { return nil }
        return String(rate)
    }

    public init(
        sessionID: String,
        sequence: Int,
        startOffsetMilliseconds: Int64,
        durationMilliseconds: Int64,
        localURL: URL,
        byteCount: Int,
        sha256: String,
        audioBitrate: Int? = nil,
        uploadState: SegmentUploadState = .pending,
        batterySnapshot: WatchBatterySnapshot? = nil,
        healthSnapshot: WatchHealthSnapshot? = nil,
        location: WatchLocationSnapshot? = nil,
        pins: [WatchSessionPin] = []
    ) {
        self.sessionID = sessionID
        self.sequence = sequence
        self.startOffsetMilliseconds = startOffsetMilliseconds
        self.durationMilliseconds = durationMilliseconds
        self.localURL = localURL
        self.byteCount = byteCount
        self.sha256 = sha256
        self.audioBitrate = audioBitrate
        self.uploadState = uploadState
        self.batterySnapshot = batterySnapshot
        self.healthSnapshot = healthSnapshot
        self.location = location
        self.pins = pins
    }
}

public struct RecordingSessionManifest: Codable, Equatable, Sendable {
    public let id: String
    public let startedAt: Date
    public var stoppedAt: Date?
    public var macJobID: String?
    public var segments: [AudioSegment]
    public var location: WatchLocationSnapshot?
    public var activeCapsule: WatchActiveCapsule?
    public var capsules: [WatchSessionCapsule]?
    public var pins: [WatchSessionPin]?

    public var allCapsules: [WatchSessionCapsule] { capsules ?? [] }
    public var allPins: [WatchSessionPin] { pins ?? [] }

    public init(
        id: String,
        startedAt: Date,
        stoppedAt: Date? = nil,
        macJobID: String? = nil,
        segments: [AudioSegment] = [],
        location: WatchLocationSnapshot? = nil,
        activeCapsule: WatchActiveCapsule? = nil,
        capsules: [WatchSessionCapsule] = [],
        pins: [WatchSessionPin] = []
    ) {
        self.id = id
        self.startedAt = startedAt
        self.stoppedAt = stoppedAt
        self.macJobID = macJobID
        self.segments = segments
        self.location = location
        self.activeCapsule = activeCapsule
        self.capsules = capsules
        self.pins = pins
    }
}

public enum SegmentLedgerError: Error, Equatable {
    case invalidSessionID
    case sessionAlreadyExists
    case sessionAlreadyFinished
    case sessionNotFound
    case segmentNotFound
    case unexpectedSequence(expected: Int, received: Int)
    case invalidSegmentTiming
    case missingSourceFile
    case emptySegment
    case checksumMismatch
    case invalidStateTransition
    case invalidMacJobID
    case conflictingMacJobID
}

/// Durable, local-first segment metadata. Acknowledged audio is removed only
/// after a matching Mac checksum has been persisted to the manifest.
public actor SegmentLedger {
    private let rootURL: URL
    private let fileManager: FileManager
    private var cachedManifests: [String: RecordingSessionManifest] = [:]

    public init(rootURL: URL, fileManager: FileManager = .default) {
        self.rootURL = rootURL
        self.fileManager = fileManager
    }

    /// App 进程内**唯一**的 ledger 实例 —— 任何需要读写同一份 manifest 的组件都必须用它。
    ///
    /// 【2026-10-15 修】此前 App 里存在两个 `SegmentLedger` 实例指向**同一个** `LifeLogger` 目录：
    /// `WatchRecordingCoordinator.init()` 建一个（录音主流程用它记账），
    /// `MacWatchSessionTransport.sharedCompletionQueue()` 又建一个（completion 上传链路用它）。
    /// 两者虽是 actor，但**不同实例之间没有互斥**，各自持有独立的 `cachedManifests`，
    /// 交错落盘时后写的会整体覆盖先写的 —— 表现为 `macJobID` 被抹掉（会话反复 complete）
    /// 或 `acknowledged` 退回 pending（重复上传），即**丢分段记录**。
    /// 统一到同一个实例后，actor 的串行语义才真正覆盖全部读写方。
    ///
    /// 测试请继续用 `init(rootURL:)` 建隔离实例，不要用这个。
    public static let liveShared: SegmentLedger = {
        let supportRoot = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
        return SegmentLedger(
            rootURL: supportRoot.appendingPathComponent("LifeLogger", isDirectory: true)
        )
    }()

    @discardableResult
    public func createSession(
        id: String,
        startedAt: Date = .now
    ) throws -> RecordingSessionManifest {
        guard Self.isSafeSessionID(id) else {
            throw SegmentLedgerError.invalidSessionID
        }
        let directory = sessionDirectory(id: id)
        guard cachedManifests[id] == nil, !fileManager.fileExists(atPath: manifestURL(id: id).path) else {
            throw SegmentLedgerError.sessionAlreadyExists
        }
        try fileManager.createDirectory(
            at: directory.appendingPathComponent("segments", isDirectory: true),
            withIntermediateDirectories: true
        )
        let manifest = RecordingSessionManifest(id: id, startedAt: startedAt)
        cachedManifests[id] = manifest
        try write(manifest)
        return manifest
    }

    public func loadSession(id: String) throws -> RecordingSessionManifest? {
        guard Self.isSafeSessionID(id) else {
            throw SegmentLedgerError.invalidSessionID
        }
        if let cached = cachedManifests[id] {
            return cached
        }
        let url = manifestURL(id: id)
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        let manifest = try decodeManifest(at: url)
        cachedManifests[id] = manifest
        return manifest
    }

    /// 清理已全部传完的旧会话元数据（音频在确认后就删了），账本才不会一直变大。
    /// 只删确认结束、每段都已确认、并且 Mac 已接收的会话。
    @discardableResult
    public func pruneFinishedSessions(olderThan cutoff: Date) throws -> Int {
        var removed = 0
        for manifest in try listSessions() {
            guard let stoppedAt = manifest.stoppedAt, stoppedAt < cutoff, manifest.macJobID != nil,
                  manifest.segments.allSatisfy({ $0.uploadState == .acknowledged }) else { continue }
            let directory = sessionDirectory(id: manifest.id)
            let leftover = (try? fileManager.contentsOfDirectory(
                at: directory.appendingPathComponent("segments", isDirectory: true),
                includingPropertiesForKeys: nil
            )) ?? []
            guard leftover.isEmpty else { continue }
            try fileManager.removeItem(at: directory)
            cachedManifests[manifest.id] = nil
            removed += 1
        }
        return removed
    }

    public func listSessions() throws -> [RecordingSessionManifest] {
        let sessionsDirectory = rootURL.appendingPathComponent("sessions", isDirectory: true)
        guard fileManager.fileExists(atPath: sessionsDirectory.path) else { return [] }
        let directories = try fileManager.contentsOfDirectory(
            at: sessionsDirectory,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        )
        let manifests = try directories.compactMap { directory -> RecordingSessionManifest? in
            let id = directory.lastPathComponent
            if let cached = cachedManifests[id] {
                return cached
            }
            let manifestURL = directory.appendingPathComponent("manifest.json")
            guard fileManager.fileExists(atPath: manifestURL.path) else { return nil }
            guard let decoded = try? decodeManifest(at: manifestURL) else { return nil }
            cachedManifests[id] = decoded
            return decoded
        }
        return manifests.sorted { $0.startedAt > $1.startedAt }
    }

    /// Closes a session at the moment its audio actually ends.
    ///
    /// The default used to be `.now`, which is only right on the stop path, where the ledger
    /// is finalized within a second of the last recorded audio. Recovery finalizes sessions
    /// hours or days later (measured on this project: 8 sessions closed 2.3–4.3 days after
    /// their audio stopped), and `.now` then invents a stop time far past the audio. The Mac
    /// refuses to change `ended_at` once a session is complete, so that wrong value is
    /// permanent and lands in the manifest as fact.
    ///
    /// The audio timeline is also what the Mac itself uses to close a stranded session, and
    /// its `ended_at >= started + latest audio end` check accepts the derived value by
    /// construction. Pass `stoppedAt` explicitly only when the caller knows the real stop
    /// moment and it differs from the end of the indexed audio.
    public func finishSession(id: String, stoppedAt: Date? = nil) throws {
        var manifest = try requireManifest(id: id)
        guard manifest.stoppedAt == nil else { return }
        let stopMoment = stoppedAt ?? Self.audioTimelineEnd(of: manifest) ?? .now
        manifest.stoppedAt = stopMoment
        if let active = manifest.activeCapsule {
            let sessionDurationMs = manifest.segments.reduce(Int64(0)) { $0 + $1.durationMilliseconds }
            let endOffsetMs = max(active.startOffsetMilliseconds + 1000, sessionDurationMs)
            let sealed = WatchSessionCapsule(
                startOffsetMilliseconds: active.startOffsetMilliseconds,
                endOffsetMilliseconds: endOffsetMs,
                startedAt: active.startedAt,
                endedAt: stopMoment
            )
            manifest.capsules = (manifest.capsules ?? []) + [sealed]
            manifest.activeCapsule = nil
        }
        try write(manifest)
    }

    public func recordPin(sessionID: String, pin: WatchSessionPin) throws {
        var manifest = try requireManifest(id: sessionID)
        var all = manifest.pins ?? []
        if !all.contains(where: { $0.pinID == pin.pinID }) {
            all.append(pin)
        }
        manifest.pins = all
        try write(manifest)
    }

    public func startCapsule(sessionID: String, startOffsetMilliseconds: Int64, startedAt: Date = Date()) throws {
        var manifest = try requireManifest(id: sessionID)
        manifest.activeCapsule = WatchActiveCapsule(startedAt: startedAt, startOffsetMilliseconds: startOffsetMilliseconds)
        try write(manifest)
    }

    @discardableResult
    public func sealCapsule(sessionID: String, endOffsetMilliseconds: Int64, endedAt: Date = Date()) throws -> WatchSessionCapsule? {
        var manifest = try requireManifest(id: sessionID)
        guard let active = manifest.activeCapsule else {
            return nil
        }
        let resolvedEndMs = max(active.startOffsetMilliseconds + 1000, endOffsetMilliseconds)
        let sealed = WatchSessionCapsule(
            startOffsetMilliseconds: active.startOffsetMilliseconds,
            endOffsetMilliseconds: resolvedEndMs,
            startedAt: active.startedAt,
            endedAt: endedAt
        )
        var allCapsules = manifest.capsules ?? []
        allCapsules.append(sealed)
        manifest.capsules = allCapsules
        manifest.activeCapsule = nil
        try write(manifest)
        return sealed
    }

    /// Absolute end of the last indexed segment, or `nil` when the session holds no usable
    /// segment to derive from (an empty session, or non-positive timings).
    static func audioTimelineEnd(of manifest: RecordingSessionManifest) -> Date? {
        let latest = manifest.segments
            .map { $0.startOffsetMilliseconds + $0.durationMilliseconds }
            .max() ?? 0
        guard latest > 0 else { return nil }
        return manifest.startedAt.addingTimeInterval(Double(latest) / 1_000)
    }

    public func markSessionImported(id: String, macJobID: String) throws {
        guard macJobID.range(of: "^job-[A-Za-z0-9_-]{1,100}$", options: .regularExpression) != nil else {
            throw SegmentLedgerError.invalidMacJobID
        }
        var manifest = try requireManifest(id: id)
        if let existing = manifest.macJobID {
            guard existing == macJobID else {
                throw SegmentLedgerError.conflictingMacJobID
            }
            return
        }
        manifest.macJobID = macJobID
        try write(manifest)
    }

    @discardableResult
    public func appendSegment(
        sessionID: String,
        sequence: Int,
        sourceURL: URL,
        startOffsetMilliseconds: Int64,
        durationMilliseconds: Int64,
        batterySnapshot: WatchBatterySnapshot? = nil,
        healthSnapshot: WatchHealthSnapshot? = nil,
        location: WatchLocationSnapshot? = nil,
        pins: [WatchSessionPin] = []
    ) throws -> AudioSegment {
        try storeSegment(sessionID: sessionID, sequence: sequence, sourceURL: sourceURL,
                         startOffsetMilliseconds: startOffsetMilliseconds,
                         durationMilliseconds: durationMilliseconds, recovering: false,
                         batterySnapshot: batterySnapshot,
                         healthSnapshot: healthSnapshot,
                         location: location,
                         pins: pins)
    }

    private func storeSegment(
        sessionID: String, sequence: Int, sourceURL: URL,
        startOffsetMilliseconds: Int64, durationMilliseconds: Int64, recovering: Bool,
        batterySnapshot: WatchBatterySnapshot? = nil,
        healthSnapshot: WatchHealthSnapshot? = nil,
        location: WatchLocationSnapshot? = nil,
        pins: [WatchSessionPin] = []
    ) throws -> AudioSegment {
        guard let sourceValues = try? sourceURL.resourceValues(forKeys: [.isRegularFileKey]),
              sourceValues.isRegularFile == true else {
            throw SegmentLedgerError.missingSourceFile
        }
        guard startOffsetMilliseconds >= 0, durationMilliseconds > 0 else {
            throw SegmentLedgerError.invalidSegmentTiming
        }
        var manifest = try requireManifest(id: sessionID)
        guard manifest.stoppedAt == nil || (recovering && manifest.macJobID == nil) else {
            throw SegmentLedgerError.sessionAlreadyFinished
        }
        guard sequence == manifest.segments.count else {
            throw SegmentLedgerError.unexpectedSequence(
                expected: manifest.segments.count,
                received: sequence
            )
        }
        if sequence == 0 {
            guard startOffsetMilliseconds == 0 else {
                throw SegmentLedgerError.invalidSegmentTiming
            }
        } else if let previous = manifest.segments.last {
            guard startOffsetMilliseconds > previous.startOffsetMilliseconds else {
                throw SegmentLedgerError.invalidSegmentTiming
            }
            let previousEnd = previous.startOffsetMilliseconds + previous.durationMilliseconds
            guard startOffsetMilliseconds + 250 >= previousEnd else {
                throw SegmentLedgerError.invalidSegmentTiming
            }
        }

        let data = try Data(contentsOf: sourceURL, options: .mappedIfSafe)
        guard !data.isEmpty else { throw SegmentLedgerError.emptySegment }
        let digest = Self.sha256(data)
        let filename = String(format: "segment-%06d.m4a", sequence)
        let segmentsDirectory = sessionDirectory(id: sessionID)
            .appendingPathComponent("segments", isDirectory: true)
        let finalURL = segmentsDirectory.appendingPathComponent(filename)

        if fileManager.fileExists(atPath: finalURL.path) {
            let existingData = try Data(contentsOf: finalURL, options: .mappedIfSafe)
            guard Self.sha256(existingData) == digest else {
                throw SegmentLedgerError.checksumMismatch
            }
        } else {
            try data.write(to: finalURL, options: [.atomic, .noFileProtection])
        }

        let segment = AudioSegment(
            sessionID: sessionID,
            sequence: sequence,
            startOffsetMilliseconds: startOffsetMilliseconds,
            durationMilliseconds: durationMilliseconds,
            localURL: finalURL,
            byteCount: data.count,
            sha256: digest,
            audioBitrate: WatchAudioEncoding.measuredBitRate(at: finalURL),
            batterySnapshot: batterySnapshot,
            healthSnapshot: healthSnapshot,
            location: location,
            pins: pins
        )
        manifest.segments.append(segment)
        if let location {
            manifest.location = location
        }
        if !pins.isEmpty {
            var all = manifest.pins ?? []
            for p in pins {
                if !all.contains(where: { $0.pinID == p.pinID }) {
                    all.append(p)
                }
            }
            manifest.pins = all
        }
        do {
            // write() 内部负责更新/回滚内存缓存，这里不再重复写
            try write(manifest)
        } catch {
            // 【2026-10-15 修】原实现是 `try? fileManager.removeItem(at: finalURL)` ——
            // 写 manifest 失败（磁盘满 / 权限不足）时把刚写入或本就存在的**音频文件删掉**。
            // 音频是用户不可再生的数据；一条元数据没写成功，不该毁掉录音本身。
            // 现在：保留音频、由 write() 回滚缓存、把错误抛给上层（上层会显示失败并可重试）。
            throw error
        }
        return segment
    }

    /// Recovers a finalized audio file left in the recorder's staging directory
    /// when the app stopped before queueing it or removing the staging copy.
    @discardableResult
    public func recoverStagedSegment(
        sessionID: String,
        sequence: Int,
        sourceURL: URL,
        startOffsetMilliseconds: Int64,
        durationMilliseconds: Int64
    ) throws -> AudioSegment {
        guard startOffsetMilliseconds >= 0, durationMilliseconds > 0 else {
            throw SegmentLedgerError.invalidSegmentTiming
        }
        let standardizedSourceURL = sourceURL.standardizedFileURL
        guard let sourceValues = try? standardizedSourceURL.resourceValues(
            forKeys: [.isRegularFileKey]
        ), sourceValues.isRegularFile == true else {
            throw SegmentLedgerError.missingSourceFile
        }
        let data = try Data(contentsOf: standardizedSourceURL, options: .mappedIfSafe)
        guard !data.isEmpty else { throw SegmentLedgerError.emptySegment }
        let digest = Self.sha256(data)
        let manifest = try requireManifest(id: sessionID)

        if let existing = manifest.segments.first(where: { $0.sequence == sequence }) {
            guard existing.sha256 == digest else {
                throw SegmentLedgerError.checksumMismatch
            }
            guard existing.startOffsetMilliseconds == startOffsetMilliseconds,
                  existing.durationMilliseconds == durationMilliseconds else {
                throw SegmentLedgerError.invalidSegmentTiming
            }
            if existing.localURL.standardizedFileURL != standardizedSourceURL {
                try fileManager.createDirectory(
                    at: existing.localURL.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                try data.write(to: existing.localURL, options: .atomic)
                try fileManager.removeItem(at: standardizedSourceURL)
            }
            return existing
        }

        let recovered = try storeSegment(
            sessionID: sessionID,
            sequence: sequence,
            sourceURL: standardizedSourceURL,
            startOffsetMilliseconds: startOffsetMilliseconds,
            durationMilliseconds: durationMilliseconds,
            recovering: true,
            batterySnapshot: nil
        )
        if recovered.localURL.standardizedFileURL != standardizedSourceURL {
            try fileManager.removeItem(at: standardizedSourceURL)
        }
        return recovered
    }

    public func markUploading(sessionID: String, sequence: Int) throws {
        try updateSegment(sessionID: sessionID, sequence: sequence) { segment in
            guard segment.uploadState == .pending
                || segment.uploadState == .failed
                || segment.uploadState == .uploading else {
                throw SegmentLedgerError.invalidStateTransition
            }
            segment.uploadState = .uploading
        }
    }

    public func markFailed(sessionID: String, sequence: Int) throws {
        try updateSegment(sessionID: sessionID, sequence: sequence) { segment in
            guard segment.uploadState == .uploading || segment.uploadState == .pending else {
                throw SegmentLedgerError.invalidStateTransition
            }
            segment.uploadState = .failed
        }
    }

    public func retry(sessionID: String, sequence: Int) throws {
        try updateSegment(sessionID: sessionID, sequence: sequence) { segment in
            guard segment.uploadState == .failed else {
                throw SegmentLedgerError.invalidStateTransition
            }
            segment.uploadState = .pending
        }
    }

    public func acknowledge(
        sessionID: String,
        sequence: Int,
        sha256: String
    ) throws {
        var manifest = try requireManifest(id: sessionID)
        guard let index = manifest.segments.firstIndex(where: { $0.sequence == sequence }) else {
            throw SegmentLedgerError.segmentNotFound
        }
        var segment = manifest.segments[index]
        guard segment.sha256 == sha256 else { throw SegmentLedgerError.checksumMismatch }
        if segment.uploadState != .acknowledged {
            segment.uploadState = .acknowledged
            manifest.segments[index] = segment
            try write(manifest)
        }

        if fileManager.fileExists(atPath: segment.localURL.path) {
            let remainingData = try Data(contentsOf: segment.localURL, options: .mappedIfSafe)
            guard Self.sha256(remainingData) == segment.sha256 else {
                throw SegmentLedgerError.checksumMismatch
            }
            try fileManager.removeItem(at: segment.localURL)
        }
    }

    private func updateSegment(
        sessionID: String,
        sequence: Int,
        update: (inout AudioSegment) throws -> Void
    ) throws {
        var manifest = try requireManifest(id: sessionID)
        guard let index = manifest.segments.firstIndex(where: { $0.sequence == sequence }) else {
            throw SegmentLedgerError.segmentNotFound
        }
        try update(&manifest.segments[index])
        try write(manifest)
    }

    private func requireManifest(id: String) throws -> RecordingSessionManifest {
        guard Self.isSafeSessionID(id) else { throw SegmentLedgerError.invalidSessionID }
        if let cached = cachedManifests[id] {
            return cached
        }
        guard let manifest = try loadSession(id: id) else {
            throw SegmentLedgerError.sessionNotFound
        }
        return manifest
    }

    private func sessionDirectory(id: String) -> URL {
        rootURL.appendingPathComponent("sessions", isDirectory: true)
            .appendingPathComponent(id, isDirectory: true)
    }

    private func manifestURL(id: String) -> URL {
        sessionDirectory(id: id).appendingPathComponent("manifest.json")
    }

    private func decodeManifest(at url: URL) throws -> RecordingSessionManifest {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        var manifest = try decoder.decode(RecordingSessionManifest.self, from: Data(contentsOf: url))
        guard Self.isSafeSessionID(manifest.id),
              manifest.id == url.deletingLastPathComponent().lastPathComponent else {
            throw SegmentLedgerError.invalidSessionID
        }
        // App installation can change the sandbox UUID. Persisted absolute URLs are
        // metadata, never authority to read or write outside the current container.
        manifest.segments = try manifest.segments.map { segment in
            guard segment.sequence >= 0, segment.sessionID == manifest.id else {
                throw SegmentLedgerError.invalidSessionID
            }
            let localURL = sessionDirectory(id: manifest.id)
                .appendingPathComponent("segments", isDirectory: true)
                .appendingPathComponent(String(format: "segment-%06d.m4a", segment.sequence))
            return AudioSegment(
                sessionID: segment.sessionID, sequence: segment.sequence,
                startOffsetMilliseconds: segment.startOffsetMilliseconds,
                durationMilliseconds: segment.durationMilliseconds, localURL: localURL,
                byteCount: segment.byteCount, sha256: segment.sha256,
                audioBitrate: segment.audioBitrate,
                uploadState: segment.uploadState, batterySnapshot: segment.batterySnapshot,
                healthSnapshot: segment.healthSnapshot, location: segment.location,
                pins: segment.effectivePins
            )
        }
        return manifest
    }

    private func write(_ manifest: RecordingSessionManifest) throws {
        // 【2026-10-15 修】缓存更新改为「成功才生效」。
        // 原实现第一行就写缓存、之后才落盘，落盘失败时内存里认为分段/会话已存在
        // 而磁盘上没有 → 同步侧读到不一致状态（走 localSegmentMissing）。
        let previous = cachedManifests[manifest.id]
        cachedManifests[manifest.id] = manifest
        let directory = sessionDirectory(id: manifest.id)
        do {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
            let encoder = JSONEncoder()
            encoder.dateEncodingStrategy = .iso8601
            encoder.outputFormatting = [.sortedKeys]
            let data = try encoder.encode(manifest)
            try data.write(to: manifestURL(id: manifest.id), options: [.atomic, .noFileProtection])
        } catch {
            if let previous {
                cachedManifests[manifest.id] = previous
            } else {
                cachedManifests.removeValue(forKey: manifest.id)
            }
            throw error
        }
    }

    private static func isSafeSessionID(_ id: String) -> Bool {
        !id.isEmpty && id.count <= 80 && id.range(of: "^[A-Za-z0-9_-]+$", options: .regularExpression) != nil
    }

    private static func sha256(_ data: Data) -> String {
        let digest = SHA256.hash(data: data)
        let hex = digest.map { String(format: "%02x", $0) }.joined()
        return "sha256:\(hex)"
    }
}
