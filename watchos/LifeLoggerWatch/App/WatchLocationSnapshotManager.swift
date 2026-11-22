import CoreLocation
import Foundation
import os

private let locationLog = Logger(subsystem: "com.lifelogger.watch", category: "location")

@MainActor
final class WatchLocationSnapshotManager: NSObject, ObservableObject {
    static let shared = WatchLocationSnapshotManager()

    private let locationManager = CLLocationManager()
    private var tracker = WatchLocationThrottleTracker()
    private var isMonitoringSignificantChanges = false
    private var lastOneShotRequestAt: Date?
    private var isRequestingOneShotLocation = false

    private override init() {
        super.init()
        locationManager.delegate = self
        // 百米精度才分得清家和附近的地方；不用 Best，也不持续定位。
        locationManager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }

    /// Requests When-In-Use permission on first launch and starts low-power significant-change
    /// monitoring when authorized. If denied or revoked, degrades silently without affecting
    /// audio capture or segment uploads.
    func startMonitoringIfPermitted() {
        locationManager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        let authState = Self.mapAuthorizationStatus(locationManager.authorizationStatus)
        tracker.updateAuthorization(authState)
        WatchCaptureEventLog.shared.record("location init auth=\(authState.rawValue)")

        switch authState {
        case .notDetermined:
            locationManager.requestWhenInUseAuthorization()
        case .authorizedWhenInUse, .authorizedAlways:
            beginSignificantLocationChangeMonitoring()
            ingestSystemCachedLocationIfAvailable()
            requestOneShotLocationIfNeeded(now: .now)
        case .restricted, .denied:
            stopMonitoringSignificantChanges()
        }
    }

    func stopMonitoring() {
        stopMonitoringSignificantChanges()
    }

    /// Returns a throttled `WatchLocationSnapshot` for the current segment upload, or `nil`
    /// when unauthorized, unavailable, or within the 500m / 15min throttle window.
    func captureSnapshot(evaluatedAt: Date = .now) -> WatchLocationSnapshot? {
        let authState = Self.mapAuthorizationStatus(locationManager.authorizationStatus)
        tracker.updateAuthorization(authState)
        guard authState.isAuthorized else {
            if authState == .notDetermined {
                locationManager.requestWhenInUseAuthorization()
            }
            stopMonitoringSignificantChanges()
            return nil
        }

        if !isMonitoringSignificantChanges {
            beginSignificantLocationChangeMonitoring()
        }
        ingestSystemCachedLocationIfAvailable()
        requestOneShotLocationIfNeeded(now: evaluatedAt)

        let snapshot = tracker.consumeSnapshotForSegment(evaluatedAt: evaluatedAt)
        if let snapshot {
            WatchCaptureEventLog.shared.record(
                String(
                    format: "location attached lat=%.4f lon=%.4f acc=%.0fm",
                    snapshot.latitude,
                    snapshot.longitude,
                    snapshot.horizontalAccuracy
                )
            )
        }
        return snapshot
    }

    private func requestOneShotLocationIfNeeded(now: Date) {
        #if os(watchOS)
        guard !isRequestingOneShotLocation else { return }
        let needsInitialFix = tracker.latestCandidate == nil
        let throttleExpired: Bool = {
            guard let lastRequest = lastOneShotRequestAt else { return true }
            return now.timeIntervalSince(lastRequest) > WatchLocationPolicy.minimumIntervalSeconds
        }()
        guard needsInitialFix || throttleExpired else { return }
        isRequestingOneShotLocation = true
        lastOneShotRequestAt = now
        locationManager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        locationManager.requestLocation()
        #endif
    }

    private func beginSignificantLocationChangeMonitoring() {
        guard !isMonitoringSignificantChanges else { return }
        locationManager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        #if !os(watchOS)
        locationManager.startMonitoringSignificantLocationChanges()
        #else
        let selector = NSSelectorFromString("startMonitoringSignificantLocationChanges")
        // 【2026-10-15 修】原实现把 `isMonitoringSignificantChanges = true` 和
        // 「monitoring active」日志放在 responds(to:) 判断**之外** —— selector 不存在时
        // 也照样宣称已开启，于是"位置监控开着却一个点都没有"在日志里完全看不出来
        // （实测 111 个 manifest 里只有 3 个带 location）。
        guard locationManager.responds(to: selector) else {
            locationLog.error(
                "significant location change monitoring unavailable on this watchOS build; location will rely on one-shot fixes"
            )
            return
        }
        _ = locationManager.perform(selector)
        #endif
        isMonitoringSignificantChanges = true
        locationLog.notice("significant location change monitoring active (accuracy=100m)")
    }

    private func stopMonitoringSignificantChanges() {
        guard isMonitoringSignificantChanges else { return }
        #if !os(watchOS)
        locationManager.stopMonitoringSignificantLocationChanges()
        #else
        let selector = NSSelectorFromString("stopMonitoringSignificantLocationChanges")
        if locationManager.responds(to: selector) {
            _ = locationManager.perform(selector)
        }
        #endif
        isMonitoringSignificantChanges = false
    }

    private func ingestSystemCachedLocationIfAvailable() {
        guard let location = locationManager.location else { return }
        ingestLocation(location)
    }

    private func ingestLocation(_ location: CLLocation) {
        guard location.horizontalAccuracy >= 0 else { return }
        let candidate = WatchLocationCandidate(
            latitude: location.coordinate.latitude,
            longitude: location.coordinate.longitude,
            horizontalAccuracy: location.horizontalAccuracy,
            timestamp: location.timestamp
        )
        tracker.recordCandidate(candidate)
    }

    private static func mapAuthorizationStatus(
        _ status: CLAuthorizationStatus
    ) -> WatchLocationAuthorizationState {
        switch status {
        case .notDetermined:
            return .notDetermined
        case .restricted:
            return .restricted
        case .denied:
            return .denied
        case .authorizedWhenInUse:
            return .authorizedWhenInUse
        case .authorizedAlways:
            return .authorizedAlways
        @unknown default:
            return .denied
        }
    }
}

extension WatchLocationSnapshotManager: CLLocationManagerDelegate {
    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let rawStatus = manager.authorizationStatus
        Task { @MainActor [weak self] in
            guard let self else { return }
            let state = Self.mapAuthorizationStatus(rawStatus)
            self.tracker.updateAuthorization(state)
            WatchCaptureEventLog.shared.record("location auth changed -> \(state.rawValue)")
            if state.isAuthorized {
                self.beginSignificantLocationChangeMonitoring()
                self.ingestSystemCachedLocationIfAvailable()
                self.requestOneShotLocationIfNeeded(now: .now)
            } else {
                // Silent degradation when permission is denied or revoked.
                self.stopMonitoringSignificantChanges()
                locationLog.debug("location authorization unavailable (\(state.rawValue, privacy: .public)); degrading silently")
            }
        }
    }

    nonisolated func locationManager(
        _ manager: CLLocationManager,
        didUpdateLocations locations: [CLLocation]
    ) {
        guard let latest = locations.last else { return }
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.isRequestingOneShotLocation = false
            self.ingestLocation(latest)
            WatchCaptureEventLog.shared.record(
                String(
                    format: "location fix received lat=%.4f lon=%.4f acc=%.0fm",
                    latest.coordinate.latitude,
                    latest.coordinate.longitude,
                    latest.horizontalAccuracy
                )
            )
        }
    }

    nonisolated func locationManager(
        _ manager: CLLocationManager,
        didFailWithError error: any Error
    ) {
        Task { @MainActor [weak self] in
            self?.isRequestingOneShotLocation = false
            WatchCaptureEventLog.shared.record("location non-fatal error: \(error.localizedDescription)")
        }
        locationLog.debug("location manager non-fatal error: \(error.localizedDescription, privacy: .public)")
    }
}
