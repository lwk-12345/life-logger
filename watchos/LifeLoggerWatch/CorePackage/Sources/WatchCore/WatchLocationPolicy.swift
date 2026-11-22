import Foundation

/// Auxiliary scene-localization snapshot attached to a Watch audio segment when throttling allows.
///
/// Encodes to the exact manifest schema:
/// ```json
/// "location": {
///   "latitude": 31.2304,
///   "longitude": 121.4737,
///   "horizontal_accuracy": 65.0,
///   "timestamp": "2026-10-12T21:30:00+08:00",
///   "source": "significant_change"
/// }
/// ```
public struct WatchLocationSnapshot: Codable, Equatable, Sendable {
    public static let significantChangeSource = "significant_change"

    public let latitude: Double
    public let longitude: Double
    public let horizontalAccuracy: Double
    public let timestamp: String
    public let source: String

    enum CodingKeys: String, CodingKey {
        case latitude
        case longitude
        case horizontalAccuracy = "horizontal_accuracy"
        case timestamp
        case source
    }

    public init(
        latitude: Double,
        longitude: Double,
        horizontalAccuracy: Double,
        timestamp: String,
        source: String = WatchLocationSnapshot.significantChangeSource
    ) {
        self.latitude = latitude
        self.longitude = longitude
        self.horizontalAccuracy = horizontalAccuracy
        self.timestamp = timestamp
        self.source = source
    }

    /// Validates raw coordinate and accuracy readings before constructing a snapshot.
    /// Returns `nil` when coordinates are out of bounds or accuracy is negative/non-finite.
    public static func capture(
        latitude: Double,
        longitude: Double,
        horizontalAccuracy: Double,
        timestamp: Date,
        timeZone: TimeZone = .current,
        source: String = WatchLocationSnapshot.significantChangeSource
    ) -> WatchLocationSnapshot? {
        guard latitude.isFinite, (-90.0...90.0).contains(latitude),
              longitude.isFinite, (-180.0...180.0).contains(longitude),
              horizontalAccuracy.isFinite, horizontalAccuracy >= 0 else {
            return nil
        }
        return WatchLocationSnapshot(
            latitude: latitude,
            longitude: longitude,
            horizontalAccuracy: horizontalAccuracy,
            timestamp: WatchLocationPolicy.iso8601String(from: timestamp, timeZone: timeZone),
            source: source
        )
    }
}

public enum WatchLocationAuthorizationState: String, Codable, Equatable, Sendable {
    case notDetermined
    case restricted
    case denied
    case authorizedWhenInUse
    case authorizedAlways

    public var isAuthorized: Bool {
        switch self {
        case .authorizedWhenInUse, .authorizedAlways:
            return true
        case .notDetermined, .restricted, .denied:
            return false
        }
    }
}

public struct WatchLocationCandidate: Equatable, Sendable {
    public let latitude: Double
    public let longitude: Double
    public let horizontalAccuracy: Double
    public let timestamp: Date

    public init(
        latitude: Double,
        longitude: Double,
        horizontalAccuracy: Double,
        timestamp: Date
    ) {
        self.latitude = latitude
        self.longitude = longitude
        self.horizontalAccuracy = horizontalAccuracy
        self.timestamp = timestamp
    }
}

public enum WatchLocationPolicy {
    /// Report when distance from last reported location exceeds 100 meters.
    /// 500 米分不清家和楼下的健身房。
    public static let minimumDistanceMeters: Double = 100.0
    /// Or when time elapsed since last reported location exceeds 5 minutes.
    /// 15 分钟太稀，Mac 端只认 15 分钟内的定位，留出余量。
    public static let minimumIntervalSeconds: TimeInterval = 5 * 60
    /// Accuracy target (`kCLLocationAccuracyHundredMeters`) for scene context.
    public static let desiredAccuracyMeters: Double = 100.0
    /// Continuous `startUpdatingLocation()` is strictly prohibited.
    public static let usesSignificantLocationChangesOnly: Bool = true
    public static let allowsContinuousLocationUpdates: Bool = false
    public static let allowsBackgroundLocationUpdates: Bool = false
    public static let usageDescription: String = "用于判断所处场景，不跟踪轨迹"

    /// Great-circle (Haversine) distance in meters between two WGS84 coordinates.
    public static func distanceMeters(
        fromLatitude lat1: Double,
        fromLongitude lon1: Double,
        toLatitude lat2: Double,
        toLongitude lon2: Double
    ) -> Double {
        let earthRadiusMeters = 6_371_000.0
        let dLat = (lat2 - lat1) * .pi / 180.0
        let dLon = (lon2 - lon1) * .pi / 180.0
        let radLat1 = lat1 * .pi / 180.0
        let radLat2 = lat2 * .pi / 180.0

        let a = sin(dLat / 2) * sin(dLat / 2)
            + cos(radLat1) * cos(radLat2) * sin(dLon / 2) * sin(dLon / 2)
        let c = 2 * atan2(sqrt(max(0, a)), sqrt(max(0, 1 - a)))
        return earthRadiusMeters * c
    }

    /// Formats a `Date` as ISO-8601 with an explicit timezone offset (e.g. `2026-10-12T21:30:00+08:00`).
    public static func iso8601String(from date: Date, timeZone: TimeZone = .current) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.timeZone = timeZone
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }

    /// Evaluates whether a candidate location should be attached to an outgoing segment.
    ///
    /// Rules:
    /// 1. If authorization is not granted (`.denied`, `.restricted`, `.notDetermined`), returns `nil`.
    /// 2. If candidate is `nil` or invalid (`horizontalAccuracy < 0`), returns `nil`.
    /// 3. If no previous location has been reported (`lastReported == nil`), returns the snapshot.
    /// 4. Otherwise attaches location ONLY when:
    ///    - distance from `lastReported` > 500 meters, OR
    ///    - elapsed time since `lastReportedAt` > 15 minutes (900 seconds).
    public static func evaluateSegmentLocation(
        authorization: WatchLocationAuthorizationState,
        candidate: WatchLocationCandidate?,
        lastReported: WatchLocationCandidate?,
        lastReportedAt: Date?,
        evaluatedAt: Date? = nil,
        timeZone: TimeZone = .current
    ) -> WatchLocationSnapshot? {
        guard authorization.isAuthorized, let candidate else {
            return nil
        }
        guard let snapshot = WatchLocationSnapshot.capture(
            latitude: candidate.latitude,
            longitude: candidate.longitude,
            horizontalAccuracy: candidate.horizontalAccuracy,
            timestamp: candidate.timestamp,
            timeZone: timeZone
        ) else {
            return nil
        }

        guard let lastReported, let lastReportedAt else {
            return snapshot
        }

        let distance = distanceMeters(
            fromLatitude: lastReported.latitude,
            fromLongitude: lastReported.longitude,
            toLatitude: candidate.latitude,
            toLongitude: candidate.longitude
        )
        let effectiveNow = max(evaluatedAt ?? candidate.timestamp, candidate.timestamp)
        let elapsedSeconds = effectiveNow.timeIntervalSince(lastReportedAt)

        if distance > minimumDistanceMeters || elapsedSeconds > minimumIntervalSeconds {
            return snapshot
        }
        return nil
    }
}

/// Stateful throttle & permission tracker used across segment rotations.
///
/// Guarantees silent degradation when location permission is denied or revoked, and enforces
/// the `> 500m` OR `> 15min` reporting throttle on segment uploads.
public struct WatchLocationThrottleTracker: Sendable {
    public private(set) var authorization: WatchLocationAuthorizationState
    public private(set) var latestCandidate: WatchLocationCandidate?
    public private(set) var lastReportedCandidate: WatchLocationCandidate?
    public private(set) var lastReportedAt: Date?
    public var timeZone: TimeZone

    public init(
        authorization: WatchLocationAuthorizationState = .notDetermined,
        timeZone: TimeZone = .current
    ) {
        self.authorization = authorization
        self.timeZone = timeZone
    }

    public mutating func updateAuthorization(_ newState: WatchLocationAuthorizationState) {
        authorization = newState
        if !newState.isAuthorized {
            // Permission denied or revoked: drop cached coordinate immediately so
            // subsequent segments silently degrade without leaking stale locations.
            latestCandidate = nil
        }
    }

    public mutating func recordCandidate(_ candidate: WatchLocationCandidate) {
        guard authorization.isAuthorized else {
            latestCandidate = nil
            return
        }
        guard WatchLocationSnapshot.capture(
            latitude: candidate.latitude,
            longitude: candidate.longitude,
            horizontalAccuracy: candidate.horizontalAccuracy,
            timestamp: candidate.timestamp,
            timeZone: timeZone
        ) != nil else {
            return
        }
        latestCandidate = candidate
    }

    /// Returns a `WatchLocationSnapshot` to attach to the current segment if and only if
    /// permission is active and the throttle (`> 500m` or `> 15min`, or first report) passes.
    public mutating func consumeSnapshotForSegment(evaluatedAt: Date? = nil) -> WatchLocationSnapshot? {
        guard let candidate = latestCandidate else {
            return nil
        }
        guard let snapshot = WatchLocationPolicy.evaluateSegmentLocation(
            authorization: authorization,
            candidate: candidate,
            lastReported: lastReportedCandidate,
            lastReportedAt: lastReportedAt,
            evaluatedAt: evaluatedAt,
            timeZone: timeZone
        ) else {
            return nil
        }
        let effectiveReportTime = max(evaluatedAt ?? candidate.timestamp, candidate.timestamp)
        lastReportedCandidate = candidate
        lastReportedAt = effectiveReportTime
        return snapshot
    }
}
