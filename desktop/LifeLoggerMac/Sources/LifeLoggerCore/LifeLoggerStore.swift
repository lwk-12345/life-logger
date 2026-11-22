import Foundation
import Combine

extension LLActivity {
    public var primaryTimestampString: String? {
        for candidate in [occurrenceStart, spokenAt, startAt, reportedAt] {
            if let s = candidate?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty {
                return s
            }
        }
        return nil
    }

    public static func extractBeijingComponents(from raw: String?) -> (hour: Int, minute: Int)? {
        guard let raw = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else {
            return nil
        }

        // Plain HH:mm or HH:mm:ss
        let parts = raw.split(separator: ":")
        if parts.count >= 2, !raw.contains("T"), !raw.contains("-"),
           let h = Int(parts[0]), let m = Int(parts[1].prefix(2)), (0..<24).contains(h), (0..<60).contains(m) {
            return (h, m)
        }

        // Try ISO8601DateFormatter with and without fractional seconds
        let beijingTZ = TimeZone(identifier: "Asia/Shanghai")!
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = beijingTZ

        let isoFractional = ISO8601DateFormatter()
        isoFractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = isoFractional.date(from: raw) {
            let comps = calendar.dateComponents([.hour, .minute], from: date)
            if let h = comps.hour, let m = comps.minute {
                return (h, m)
            }
        }

        let isoStandard = ISO8601DateFormatter()
        isoStandard.formatOptions = [.withInternetDateTime]
        if let date = isoStandard.date(from: raw) {
            let comps = calendar.dateComponents([.hour, .minute], from: date)
            if let h = comps.hour, let m = comps.minute {
                return (h, m)
            }
        }

        let df = DateFormatter()
        df.calendar = calendar
        df.locale = Locale(identifier: "en_US_POSIX")
        df.timeZone = beijingTZ
        for format in [
            "yyyy-MM-dd'T'HH:mm:ss.SSSSSSXXXXX",
            "yyyy-MM-dd'T'HH:mm:ss.SSSXXXXX",
            "yyyy-MM-dd'T'HH:mm:ssXXXXX",
            "yyyy-MM-dd'T'HH:mm:ss.SSSSSS",
            "yyyy-MM-dd'T'HH:mm:ss"
        ] {
            df.dateFormat = format
            if let date = df.date(from: raw) {
                let comps = calendar.dateComponents([.hour, .minute], from: date)
                if let h = comps.hour, let m = comps.minute {
                    return (h, m)
                }
            }
        }

        return nil
    }

    public static func extractBeijingHHMM(from raw: String?) -> String? {
        guard let comps = extractBeijingComponents(from: raw) else { return nil }
        return String(format: "%02d:%02d", comps.hour, comps.minute)
    }

    public var displayTitle: String {
        var cleaned = title.trimmingCharacters(in: .whitespacesAndNewlines)
        for prefix in ["录音中疑似：", "录音中疑似:"] {
            if cleaned.hasPrefix(prefix) {
                cleaned = String(cleaned.dropFirst(prefix.count)).trimmingCharacters(in: .whitespacesAndNewlines)
            }
        }
        return cleaned.isEmpty ? title : cleaned
    }

    public var displayTimeText: String {
        Self.extractBeijingHHMM(from: primaryTimestampString) ?? "--:--"
    }

    public var displayHour: Int {
        Self.extractBeijingComponents(from: primaryTimestampString)?.hour ?? 0
    }

    public var displayAppBadge: String? {
        let resolvedContext = inputContext
            ?? items.compactMap(\.inputContext).first
            ?? items.flatMap { $0.evidence ?? [] }.compactMap(\.inputContext).first
        guard let label = resolvedContext?.appLabel?.trimmingCharacters(in: .whitespacesAndNewlines),
              !label.isEmpty else {
            return nil
        }
        if resolvedContext?.kind == "dictation" {
            return "语音输入到 \(label)"
        }
        return "前台 \(label)"
    }

    public var displaySpeechBadge: String? {
        guard let target = speechTarget?.trimmingCharacters(in: .whitespacesAndNewlines),
              !target.isEmpty else {
            return nil
        }
        let ignoredTargets: Set<String> = ["未知", "unknown", "none", "null", "媒体内容", "本人", "self", "无", "-"]
        if ignoredTargets.contains(target) || ignoredTargets.contains(target.lowercased()) {
            return nil
        }
        return target.hasPrefix("对") ? target : "对\(target)"
    }
}

@MainActor
public final class LifeLoggerStore: ObservableObject {
    @Published public var selectedDate: String
    @Published public var isLoading: Bool = false
    @Published public var errorMessage: String? = nil
    @Published public var timeline: LLTimelineResponse? = nil
    @Published public var daily: LLDailyResponse? = nil
    @Published public var healthSummary: LLHealthSummary? = nil
    @Published public var versionInfo: LLVersionResponse? = nil
    @Published public var lastUpdatedText: String = "未同步"
    @Published public var lastSuccessHHMM: String? = nil
    @Published public var isMainWindowVisible: Bool = false
    @Published public var isMenuPopoverVisible: Bool = false

    public var hasSucceededOnce: Bool { lastSuccessHHMM != nil }
    public var isAnySurfaceVisible: Bool { isMainWindowVisible || isMenuPopoverVisible }

    public let client: LifeLoggerAPIClient
    private var refreshTask: Task<Void, Never>? = nil

    public var baseURL: URL { client.baseURL }
    public var accessToken: String? { client.accessToken }

    public init(client: LifeLoggerAPIClient = LifeLoggerAPIClient(), initialDate: String? = nil) {
        self.client = client
        self.selectedDate = initialDate ?? LifeLoggerAPIClient.beijingTodayString()
    }

    public func refresh() async {
        let targetDate = selectedDate
        isLoading = true

        async let timelineResult: Result<LLTimelineResponse, Error> = {
            do { return .success(try await client.fetchTimeline(date: targetDate)) }
            catch { return .failure(error) }
        }()
        async let dailyResult: Result<LLDailyResponse, Error> = {
            do { return .success(try await client.fetchDaily(date: targetDate)) }
            catch { return .failure(error) }
        }()
        async let healthResult: Result<LLHealthSummary, Error> = {
            do { return .success(try await client.fetchHealthSummary(date: targetDate)) }
            catch { return .failure(error) }
        }()
        async let versionResult: Result<LLVersionResponse, Error> = {
            do { return .success(try await client.fetchVersion()) }
            catch { return .failure(error) }
        }()

        let (tRes, dRes, hRes, vRes) = await (timelineResult, dailyResult, healthResult, versionResult)

        var dailySucceeded = false
        switch tRes {
        case .success(let value):
            timeline = value
        case .failure:
            break
        }

        switch dRes {
        case .success(let value):
            daily = value
            dailySucceeded = true
        case .failure:
            break
        }

        switch hRes {
        case .success(let value):
            healthSummary = value
        case .failure:
            break
        }

        switch vRes {
        case .success(let value):
            versionInfo = value
        case .failure:
            break
        }

        if dailySucceeded {
            let now = Date()
            let hhmmFormatter = DateFormatter()
            hhmmFormatter.locale = Locale(identifier: "en_US_POSIX")
            hhmmFormatter.timeZone = TimeZone(identifier: "Asia/Shanghai")!
            hhmmFormatter.dateFormat = "HH:mm"
            lastSuccessHHMM = hhmmFormatter.string(from: now)

            let fullFormatter = DateFormatter()
            fullFormatter.locale = Locale(identifier: "en_US_POSIX")
            fullFormatter.timeZone = TimeZone(identifier: "Asia/Shanghai")!
            fullFormatter.dateFormat = "HH:mm:ss"
            lastUpdatedText = fullFormatter.string(from: now)
            errorMessage = nil
        } else {
            if let hhmm = lastSuccessHHMM {
                errorMessage = "服务连不上，显示的是 \(hhmm) 的数据"
            } else {
                errorMessage = "Life Logger 服务没开"
            }
        }
        isLoading = false
    }

    public func selectDate(_ date: String) async {
        let trimmed = date.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        selectedDate = trimmed
        await refresh()
    }

    public func shiftDate(byDays days: Int) async {
        let nextDate = LifeLoggerAPIClient.shiftDateString(selectedDate, byDays: days)
        await selectDate(nextDate)
    }

    public func jumpToToday() async {
        await selectDate(LifeLoggerAPIClient.beijingTodayString())
    }

    public func startAutoRefresh(intervalSeconds: TimeInterval = 30) {
        stopAutoRefresh()
        let nanos = UInt64(max(intervalSeconds, 5) * 1_000_000_000)
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: nanos)
                if Task.isCancelled { break }
                guard let self else { break }
                if self.isAnySurfaceVisible || !self.hasSucceededOnce {
                    await self.refresh()
                }
            }
        }
    }

    public func stopAutoRefresh() {
        refreshTask?.cancel()
        refreshTask = nil
    }

    nonisolated public static func formatCurrencyAmount(_ amount: Double) -> String {
        let rounded = (amount * 100).rounded() / 100
        if rounded == 0 {
            return "¥0"
        }
        if rounded.truncatingRemainder(dividingBy: 1) == 0 {
            return "¥\(Int(rounded))"
        }
        let oneDecimal = (rounded * 10).rounded() / 10
        if abs(rounded - oneDecimal) < 0.001 {
            return String(format: "¥%.1f", rounded)
        }
        return String(format: "¥%.2f", rounded)
    }

    public var totalSpendText: String {
        guard let total = daily?.expenseLedger?.totalAmount else {
            return "—"
        }
        return Self.formatCurrencyAmount(total)
    }

    public var caloriesValueText: String? {
        guard let ledger = daily?.nutritionLedger else { return nil }
        if ledger.hasRecords == false {
            return nil
        }
        guard let kcal = ledger.totals?.caloriesKcal else {
            return nil
        }
        return "\(Int(kcal.rounded())) kcal"
    }

    public var caloriesText: String {
        caloriesValueText ?? "未知"
    }

    public var macrosText: String {
        let totals = daily?.nutritionLedger?.totals
        if let c = totals?.carbohydratesG, let p = totals?.proteinG, let f = totals?.fatG {
            return "碳水 \(Int(c.rounded()))g · 蛋白 \(Int(p.rounded()))g · 脂肪 \(Int(f.rounded()))g"
        }
        if totals?.carbohydratesG != nil || totals?.proteinG != nil || totals?.fatG != nil {
            let c = Int((totals?.carbohydratesG ?? 0).rounded())
            let p = Int((totals?.proteinG ?? 0).rounded())
            let f = Int((totals?.fatG ?? 0).rounded())
            return "碳水 \(c)g · 蛋白 \(p)g · 脂肪 \(f)g"
        }
        return "未知"
    }

    public var stepsText: String {
        if let steps = healthSummary?.totalSteps, steps > 0 {
            return "\(steps) 步"
        }
        return "—"
    }

    nonisolated public static func formatSleep(sleepMinutes: Int?, deepSleepMinutes: Int?) -> String {
        guard let total = sleepMinutes, total > 0 else {
            return "还没有数据"
        }
        let hours = total / 60
        let mins = total % 60
        let base: String
        if hours > 0 {
            base = "\(hours)小时\(mins)分"
        } else {
            base = "\(mins)分"
        }
        if let deep = deepSleepMinutes, deep > 0 {
            return "\(base)（深睡 \(deep)分）"
        }
        return base
    }

    public var sleepText: String {
        Self.formatSleep(
            sleepMinutes: healthSummary?.sleepMinutes,
            deepSleepMinutes: healthSummary?.deepSleepMinutes
        )
    }

    public var heartRateText: String {
        guard let avg = healthSummary?.avgHeartRate, avg > 0 else {
            return "还没有数据"
        }
        let avgInt = Int(avg.rounded())
        if let minHR = healthSummary?.minHeartRate,
           let maxHR = healthSummary?.maxHeartRate,
           minHR > 0, maxHR > 0 {
            return "\(avgInt) bpm（\(Int(minHR.rounded()))–\(Int(maxHR.rounded()))）"
        }
        return "\(avgInt) bpm"
    }

    public var workoutText: String {
        if let summary = daily?.fitnessLedger?.summary?.trimmingCharacters(in: .whitespacesAndNewlines),
           !summary.isEmpty {
            return summary
        }
        if let count = daily?.fitnessLedger?.totals?.workoutCount ?? daily?.fitnessLedger?.totals?.sessionCount,
           count > 0 {
            return "今日 \(count) 次训练"
        }
        return "今日无训练记录"
    }

    public var smokingText: String? {
        if let count = daily?.smokingLedger?.totalCount ?? daily?.smokingLedger?.recordCount,
           count > 0 {
            return "抽烟 \(count) 次"
        }
        return nil
    }

    public var lastRecordingTimeText: String? {
        let raw = daily?.lastRecordingEndAt
            ?? daily?.lastSegmentReceivedAt
            ?? healthSummary?.lastRecordingEndAt
            ?? healthSummary?.lastSegmentReceivedAt
        guard let raw = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else {
            return nil
        }
        return LLActivity.extractBeijingHHMM(from: raw) ?? raw
    }

    public var menuBarSummaryText: String {
        if !hasSucceededOnce, daily == nil, let errorMessage, !errorMessage.isEmpty {
            return errorMessage
        }
        if let kcalText = caloriesValueText {
            return "\(totalSpendText) · \(kcalText)"
        }
        return totalSpendText
    }

    public var webWaterfallURL: URL {
        var components = URLComponents(url: baseURL.appendingPathComponent("waterfall-v2.html"), resolvingAgainstBaseURL: true)
        var queryItems = [URLQueryItem(name: "date", value: selectedDate)]
        if let token = accessToken, !token.isEmpty {
            queryItems.append(URLQueryItem(name: "token", value: token))
        }
        components?.queryItems = queryItems
        return components?.url ?? URL(string: "http://127.0.0.1:8765/waterfall-v2.html?date=\(selectedDate)")!
    }
}
