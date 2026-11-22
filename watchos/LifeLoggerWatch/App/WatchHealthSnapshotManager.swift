import Foundation
import HealthKit
import os

private let healthLog = Logger(subsystem: "com.lifelogger.watch", category: "health")

/// Lightweight, safe biometric sampler for Apple Watch segments.
/// Gracefully falls back to nil if HealthKit authorization is unavailable or unsupported.
@MainActor
final class WatchHealthSnapshotManager: NSObject, ObservableObject {
    static let shared = WatchHealthSnapshotManager()

    private let healthStore = HKHealthStore()
    private var isAuthorized = false
    private var cachedHeartRate: Double?
    private var cachedStepCount: Int?
    /// 【V68】当前 `cachedStepCount` 属于哪一天（本地时区 YYYY-MM-DD）与采样时刻。
    /// 跨日后必须作废，否则 00:38 会把昨天 23:12 的累计当成"今天"发出去。
    private var cachedStepsDate: String?
    private var cachedStepsSampledAt: String?
    private var cachedSleep: WatchSleepSummary?
    private var lastSleepQueryAt = Date.distantPast
    private var lastSampleDate: Date?
    /// 是否至少有一次 HealthKit 查询**已经返回**（无论有没有数据）。
    /// 用来区分 nil 的两种原因：「查询还没回来」vs「查询回来了但没有数据」。
    private var hasRefreshCompleted = false

    private override init() {
        super.init()
        requestAuthorizationIfNeeded()
    }

    func requestAuthorizationIfNeeded() {
        guard HKHealthStore.isHealthDataAvailable() else {
            healthLog.notice("HealthKit is not available on this device")
            return
        }

        let heartRateType = HKQuantityType.quantityType(forIdentifier: .heartRate)
        let stepCountType = HKQuantityType.quantityType(forIdentifier: .stepCount)
        let sleepType = HKCategoryType.categoryType(forIdentifier: .sleepAnalysis)

        var typesToRead: Set<HKObjectType> = []
        if let hr = heartRateType { typesToRead.insert(hr) }
        if let sc = stepCountType { typesToRead.insert(sc) }
        if let sl = sleepType { typesToRead.insert(sl) }

        guard !typesToRead.isEmpty else { return }

        healthStore.requestAuthorization(toShare: nil, read: typesToRead) { [weak self] success, error in
            Task { @MainActor in
                if success {
                    self?.isAuthorized = true
                    self?.lastSleepQueryAt = .distantPast
                    healthLog.notice("HealthKit read authorization granted")
                    self?.refreshSample()
                } else if let error = error {
                    healthLog.warning("HealthKit authorization failed: \(error.localizedDescription, privacy: .public)")
                }
            }
        }
    }

    /// Refresh current heart rate and today's cumulative steps
    func refreshSample() {
        // 不等授权回调：升级后新增睡眠权限时，弹窗处理完之前心率和步数也要继续读。
        guard HKHealthStore.isHealthDataAvailable() else { return }

        // 1. Fetch latest Heart Rate sample
        if let heartRateType = HKQuantityType.quantityType(forIdentifier: .heartRate) {
            let sortDescriptor = NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: false)
            let query = HKSampleQuery(
                sampleType: heartRateType,
                predicate: nil,
                limit: 1,
                sortDescriptors: [sortDescriptor]
            ) { [weak self] _, samples, _ in
                Task { @MainActor in self?.hasRefreshCompleted = true }
                guard let sample = samples?.first as? HKQuantitySample else { return }
                let bpmUnit = HKUnit.count().unitDivided(by: .minute())
                let bpm = sample.quantity.doubleValue(for: bpmUnit)
                Task { @MainActor in
                    // 摘表过夜后的旧心率不能当现在的心率上报。
                    self?.cachedHeartRate = Date().timeIntervalSince(sample.endDate) <= 15 * 60 ? bpm : nil
                    self?.lastSampleDate = sample.endDate
                }
            }
            healthStore.execute(query)
        }

        // 2. Fetch today's step count
        if let stepCountType = HKQuantityType.quantityType(forIdentifier: .stepCount) {
            // 【V68】按**手表本地时区**的自然日求和（原来写死 Asia/Shanghai；
            // 出差/换时区时"今天"应该跟表自己的 0 点走）。
            let timeZone = TimeZone.current
            let now = Date()
            let startOfDay = WatchDailyStepsPolicy.startOfDay(for: now, timeZone: timeZone)
            let predicate = HKQuery.predicateForSamples(withStart: startOfDay, end: now, options: .strictStartDate)

            let query = HKStatisticsQuery(
                quantityType: stepCountType,
                quantitySamplePredicate: predicate,
                options: .cumulativeSum
            ) { [weak self] _, statistics, _ in
                Task { @MainActor in self?.hasRefreshCompleted = true }
                guard let sum = statistics?.sumQuantity() else { return }
                let steps = Int(sum.doubleValue(for: .count()))
                Task { @MainActor in
                    // 查询是异步的：结果回来时可能已经跨日，按**当前**时刻重算日期。
                    let resolvedAt = Date()
                    let resolvedDay = WatchDailyStepsPolicy.dateString(
                        for: resolvedAt, timeZone: TimeZone.current
                    )
                    self?.cachedStepCount = steps
                    self?.cachedStepsDate = resolvedDay
                    self?.cachedStepsSampledAt = WatchDailyStepsPolicy.sampledAtString(for: resolvedAt)
                }
            }
            healthStore.execute(query)
        }

        // 3. 过去 36 小时里最近一次已结束的主睡眠
        let sleepNow = Date()
        if let sleepType = HKCategoryType.categoryType(forIdentifier: .sleepAnalysis),
           WatchSleepSummarizer.shouldQuerySleep(
               cached: cachedSleep, lastQueryAt: lastSleepQueryAt, now: sleepNow, timeZone: TimeZone.current
           ) {
            lastSleepQueryAt = sleepNow
            let predicate = HKQuery.predicateForSamples(
                withStart: sleepNow.addingTimeInterval(-36 * 3600), end: sleepNow, options: []
            )
            let query = HKSampleQuery(
                sampleType: sleepType, predicate: predicate, limit: HKObjectQueryNoLimit, sortDescriptors: nil
            ) { [weak self] _, samples, error in
                // 查询出错（比如手表锁定）时保留已有结果。
                if let error {
                    healthLog.warning("HealthKit sleep query failed: \(error.localizedDescription, privacy: .public)")
                    return
                }
                let summary = WatchSleepSummarizer.latestMainSleep(
                    Self.sleepIntervals(samples as? [HKCategorySample] ?? []), now: sleepNow
                )
                Task { @MainActor in
                    self?.hasRefreshCompleted = true
                    if let summary { self?.cachedSleep = summary }
                }
            }
            healthStore.execute(query)
        }
    }

    /// 把 HealthKit 样本转成纯数据，汇总逻辑在 WatchCore 里测试。
    nonisolated private static func sleepIntervals(_ samples: [HKCategorySample]) -> [WatchSleepInterval] {
        samples.compactMap { sample in
            guard let value = HKCategoryValueSleepAnalysis(rawValue: sample.value) else { return nil }
            let stage: WatchSleepInterval.Stage
            switch value {
            case .asleepDeep: stage = .deep
            case .asleepCore: stage = .core
            case .asleepREM: stage = .rem
            case .asleepUnspecified: stage = .unspecified
            case .awake: stage = .awake
            default: return nil
            }
            return WatchSleepInterval(
                start: sample.startDate, end: sample.endDate, stage: stage,
                isAppleSource: sample.sourceRevision.source.bundleIdentifier.hasPrefix("com.apple")
            )
        }
    }

    /// 录音会话开始时**预热** HealthKit。
    ///
    /// 【2026-10-15 修】原来只在授权回调里刷过一次，而 `captureSnapshot()` 是同步读缓存，
    /// HealthKit 查询又是异步回调 —— 于是录音**第一个分段必然拿不到心率/步数**；
    /// 实测 111 个 manifest / 7246 个 segment 中 `health_snapshot` 出现 **0 次**。
    /// 现在会话一开始就发起采样，等到第一个分段轮转（60 秒后）缓存里早已有值。
    func primeForRecording() {
        guard HKHealthStore.isHealthDataAvailable() else {
            healthLog.notice("primeForRecording skipped: HealthKit unavailable")
            return
        }
        lastSleepQueryAt = .distantPast
        if !isAuthorized {
            requestAuthorizationIfNeeded()
        }
        refreshSample()
    }

    /// Produce a safe snapshot of current health state
    func captureSnapshot() -> WatchHealthSnapshot? {
        // 【V68】跨日先作废步数缓存：否则新的一天第一个分段会带昨天的累计。
        let timeZone = TimeZone.current
        if cachedStepCount != nil,
           !WatchDailyStepsPolicy.isFresh(cachedDate: cachedStepsDate, now: Date(), timeZone: timeZone) {
            cachedStepCount = nil
            cachedStepsDate = nil
            cachedStepsSampledAt = nil
        }
        refreshSample()
        guard cachedHeartRate != nil || cachedStepCount != nil || cachedSleep != nil else {
            // 【2026-10-15 修】不再静默返回 nil —— 把缺失原因写进日志，
            // 否则「健康数据一直收不到」在设备上看不到任何线索。
            if !HKHealthStore.isHealthDataAvailable() {
                healthLog.error("health snapshot missing: HealthKit not available on this device")
            } else if !isAuthorized {
                healthLog.error("health snapshot missing: read authorization not granted (user may have declined)")
            } else if !hasRefreshCompleted {
                healthLog.error("health snapshot missing: first HealthKit query has not returned yet")
            } else {
                healthLog.error("health snapshot missing: query returned no samples (no heart-rate/step/sleep data for this period)")
            }
            return nil
        }
        // 【V68】`stepCount` 照旧发（兼容旧服务端），同时带上"属于哪一天"的新字段。
        // 只有当日累计确实属于今天时才带新字段 —— 宁可少发，也不发错日期的数。
        let todayString = WatchDailyStepsPolicy.dateString(for: Date(), timeZone: timeZone)
        let stepsFresh = WatchDailyStepsPolicy.isFresh(
            cachedDate: cachedStepsDate, now: Date(), timeZone: timeZone
        )
        return WatchHealthSnapshot(
            heartRateBPM: cachedHeartRate,
            stepCount: cachedStepCount,
            stepsToday: stepsFresh ? cachedStepCount : nil,
            stepsDate: stepsFresh ? todayString : nil,
            stepsSampledAt: stepsFresh ? cachedStepsSampledAt : nil,
            sleep: cachedSleep
        )
    }
}
