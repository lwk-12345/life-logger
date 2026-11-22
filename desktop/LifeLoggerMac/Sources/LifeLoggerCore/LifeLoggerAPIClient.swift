import Foundation

public enum LifeLoggerAPIError: Error, LocalizedError, Sendable {
    case invalidURL(String)
    case httpError(statusCode: Int, path: String)
    case missingSummary

    public var errorDescription: String? {
        switch self {
        case .invalidURL(let raw):
            return "无效 URL: \(raw)"
        case .httpError(let statusCode, let path):
            return "请求失败 (\(statusCode)): \(path)"
        case .missingSummary:
            return "健康摘要响应缺少 summary 字段"
        }
    }
}

public final class LifeLoggerAPIClient: @unchecked Sendable {
    public let baseURL: URL
    public let repoRootPath: String
    public var accessToken: String?
    private let session: URLSession

    public static let fallbackRepoRootPath = (NSHomeDirectory() as NSString).appendingPathComponent("life-logger")

    public static func resolveRepoRootPath(
        explicit: String? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> String {
        if let explicit = explicit?.trimmingCharacters(in: .whitespacesAndNewlines), !explicit.isEmpty {
            return explicit
        }
        if let envRoot = environment["LIFE_LOGGER_REPO_ROOT"]?.trimmingCharacters(in: .whitespacesAndNewlines), !envRoot.isEmpty {
            return envRoot
        }
        return fallbackRepoRootPath
    }

    public static func resolveBaseURL(
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> URL {
        if let envURLString = environment["LIFE_LOGGER_BASE_URL"]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !envURLString.isEmpty,
           let envURL = URL(string: envURLString),
           envURL.host == "127.0.0.1" {
            return envURL
        }
        return URL(string: "http://127.0.0.1:8765")!
    }

    public init(
        baseURL: URL = LifeLoggerAPIClient.resolveBaseURL(),
        repoRootPath: String = LifeLoggerAPIClient.resolveRepoRootPath(),
        accessToken: String? = nil,
        session: URLSession = .shared
    ) {
        self.baseURL = baseURL
        let resolvedRepoRoot = Self.resolveRepoRootPath(explicit: repoRootPath)
        self.repoRootPath = resolvedRepoRoot
        self.session = session

        if let explicitToken = accessToken?.trimmingCharacters(in: .whitespacesAndNewlines), !explicitToken.isEmpty {
            self.accessToken = explicitToken
        } else if let envToken = ProcessInfo.processInfo.environment["LIFE_LOGGER_TOKEN"]?.trimmingCharacters(in: .whitespacesAndNewlines), !envToken.isEmpty {
            self.accessToken = envToken
        } else {
            self.accessToken = Self.loadAccessTokenFromSecrets(repoRootPath: resolvedRepoRoot)
        }
    }

    public static func loadAccessTokenFromSecrets(repoRootPath: String) -> String? {
        let secretsPath = (repoRootPath as NSString).appendingPathComponent("output/local-web/secrets.json")
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: secretsPath)),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let token = json["mobile_access_token"] as? String else {
            return nil
        }
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private func performGet(pathAndQuery: String) async throws -> Data {
        guard let url = URL(string: pathAndQuery, relativeTo: baseURL)?.absoluteURL else {
            throw LifeLoggerAPIError.invalidURL(pathAndQuery)
        }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 8
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token = accessToken, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw LifeLoggerAPIError.httpError(statusCode: http.statusCode, path: pathAndQuery)
        }
        return data
    }

    public func fetchTimeline(date: String) async throws -> LLTimelineResponse {
        let encodedDate = date.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? date
        let data = try await performGet(pathAndQuery: "/api/activity-timeline?date=\(encodedDate)")
        return try JSONDecoder().decode(LLTimelineResponse.self, from: data)
    }

    public func fetchDaily(date: String) async throws -> LLDailyResponse {
        let encodedDate = date.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? date
        let data = try await performGet(pathAndQuery: "/api/daily?date=\(encodedDate)")
        return try JSONDecoder().decode(LLDailyResponse.self, from: data)
    }

    public func fetchHealthSummary(date: String) async throws -> LLHealthSummary {
        let encodedDate = date.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? date
        let data = try await performGet(pathAndQuery: "/api/health/summary?date=\(encodedDate)")
        let decoded = try JSONDecoder().decode(LLHealthSummaryResponse.self, from: data)
        if let summary = decoded.summary {
            return LLHealthSummary(
                date: summary.date ?? date,
                avgHeartRate: summary.avgHeartRate,
                minHeartRate: summary.minHeartRate,
                maxHeartRate: summary.maxHeartRate,
                totalSteps: summary.totalSteps,
                sleepMinutes: summary.sleepMinutes,
                deepSleepMinutes: summary.deepSleepMinutes,
                stressSpikesCount: summary.stressSpikesCount,
                lastRecordingEndAt: summary.lastRecordingEndAt ?? decoded.lastRecordingEndAt,
                lastSegmentReceivedAt: summary.lastSegmentReceivedAt ?? decoded.lastSegmentReceivedAt
            )
        }
        if let direct = try? JSONDecoder().decode(LLHealthSummary.self, from: data),
           (direct.sleepMinutes != nil || direct.totalSteps != nil || direct.avgHeartRate != nil) {
            return direct
        }
        throw LifeLoggerAPIError.missingSummary
    }

    public func fetchVersion() async throws -> LLVersionResponse {
        let data = try await performGet(pathAndQuery: "/api/version")
        return try JSONDecoder().decode(LLVersionResponse.self, from: data)
    }

    public static func beijingTodayString(now: Date = Date()) -> String {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: now)
    }

    public static func shiftDateString(_ dateStr: String, byDays days: Int) -> String {
        let formatter = DateFormatter()
        var calendar = Calendar(identifier: .gregorian)
        let tz = TimeZone(identifier: "Asia/Shanghai")!
        calendar.timeZone = tz
        formatter.calendar = calendar
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = tz
        formatter.dateFormat = "yyyy-MM-dd"

        guard let date = formatter.date(from: dateStr),
              let shifted = calendar.date(byAdding: .day, value: days, to: date) else {
            return dateStr
        }
        return formatter.string(from: shifted)
    }
}
