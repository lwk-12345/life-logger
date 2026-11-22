// PassportHabitFetcher.swift
//
// 习惯数据拉取器 —— 与 PassportTodoFetcher 同构，只换数据源路径与映射函数。
//
// 为什么单独一个类而不是泛化 PassportTodoFetcher：
//   待办链路（0x02）是**已经跑通并在真机验证过**的，改它的抽象层次会引入回归风险；
//   复制一份结构相同、职责单一的类，代价是 ~130 行相似代码，收益是零回归。
//   两条链路的差异点只有两处：path（/api/reminders → /api/habits）与 mapper。
//
// 与服务端约定（src/life_logger/local_web.py:5820 GET /api/habits）：
//   返回 snapshot + badge_slots（详见 PassportHabitMapper 的注释）。
//   鉴权同待办：Cookie `life_logger_access=<mobile_access_token>`。

import Foundation

// ==============================================================================
// 端点解析
// ==============================================================================

public enum PassportHabitEndpoint {
    public static let defaultPort = 8765
    /// 习惯的唯一事实源（habits.json）的只读投影端点
    public static let habitsPath = "/api/habits"

    /// 环境变量覆盖（与待办分开，便于单独调试）
    public static let urlOverrideKey = "AIRPASSPORT_HABIT_URL"
    public static let tokenOverrideKey = "AIRPASSPORT_HABIT_TOKEN"

    /// 候选 URL：环境变量 → 127.0.0.1:8765 → 本机各网卡 IPv4。
    ///
    /// 第 3 条是必需的：Life Logger 的 `trusted_network_bind` 会把服务**只**绑到
    /// 受信私网地址（本机当前为 172.20.10.3），此时 127.0.0.1:8765 没人监听
    /// （实测 connection refused）。热点 IP 会变，所以遍历网卡而不是写死。
    public static func candidateURLs(environment: [String: String] = ProcessInfo.processInfo.environment) -> [URL] {
        var urls: [URL] = []
        var seen = Set<String>()

        func append(_ url: URL?) {
            guard let url else { return }
            let key = url.absoluteString
            guard !seen.contains(key) else { return }
            seen.insert(key)
            urls.append(url)
        }

        if let raw = environment[urlOverrideKey]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !raw.isEmpty {
            append(URL(string: raw))
        }
        append(URL(string: "http://127.0.0.1:\(defaultPort)\(habitsPath)"))
        for address in PassportTodoEndpoint.localIPv4Addresses() {
            append(URL(string: "http://\(address):\(defaultPort)\(habitsPath)"))
        }
        return urls
    }

    /// 访问令牌：环境变量优先，其次 secrets.json 的 mobile_access_token。
    /// 复用待办那条已验证的读取路径（同一个 token 服务两端）。
    public static func accessToken(environment: [String: String] = ProcessInfo.processInfo.environment) -> String? {
        if let raw = environment[tokenOverrideKey]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !raw.isEmpty {
            return raw
        }
        return PassportTodoEndpoint.accessToken(environment: environment)
    }
}

public enum PassportHabitFetchError: Error, LocalizedError {
    case noEndpoint
    case unauthorized(Int)
    case httpStatus(Int)

    public var errorDescription: String? {
        switch self {
        case .noEndpoint: return "没有可用的习惯端点（所有候选地址都失败）"
        case .unauthorized(let code): return "习惯端点鉴权失败（HTTP \(code)）"
        case .httpStatus(let code): return "习惯端点返回 HTTP \(code)"
        }
    }
}

// ==============================================================================
// 拉取器
// ==============================================================================

/// 拉一次 `GET /api/habits`，映射成 `[PassportHabitItem]`。
///
/// 与 PassportTodoFetcher 相同的并发约定：同一时刻只允许一个请求在飞；
/// 上一轮没回来就直接跳过（不排队、不堆积）。
public final class PassportHabitFetcher {

    private let session: URLSession
    private let requestTimeout: TimeInterval
    private let totalTimeout: TimeInterval
    private let environment: [String: String]

    private let lock = NSLock()
    /// 上一轮命中的地址，命中后优先复用，避免每 15 秒把所有网卡都试一遍。
    private var resolvedURL: URL?
    /// 同一时刻只允许一个拉取在飞。
    private var isFetching = false

    public init(requestTimeout: TimeInterval = 3.0,
                totalTimeout: TimeInterval = 3.0,
                environment: [String: String] = ProcessInfo.processInfo.environment) {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = requestTimeout
        configuration.timeoutIntervalForResource = requestTimeout
        configuration.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        configuration.waitsForConnectivity = false
        self.session = URLSession(configuration: configuration)
        self.requestTimeout = requestTimeout
        self.totalTimeout = totalTimeout
        self.environment = environment
    }

    deinit {
        session.invalidateAndCancel()
    }

    /// 拉取一次。失败时回调 `.failure`。
    ///
    /// 注意调用方的语义（与待办不同）：习惯拉取失败时**不要**清空设备列表，
    /// 沿用上一轮成功的 `[PassportHabitItem]` —— 见 PassportBLEBridge.refreshHabits。
    public func fetch(completion: @escaping (Result<[PassportHabitItem], Error>) -> Void) {
        lock.lock()
        if isFetching {
            lock.unlock()
            logLine("[AirPassport Bridge] Habit fetch skipped: previous request still in flight")
            return
        }
        isFetching = true
        let cached = resolvedURL
        lock.unlock()

        let candidates = cached.map { [$0] } ?? PassportHabitEndpoint.candidateURLs(environment: environment)
        let token = PassportHabitEndpoint.accessToken(environment: environment)
        let deadline = Date().addingTimeInterval(totalTimeout)
        attempt(candidates: candidates,
                index: 0,
                token: token,
                deadline: deadline,
                lastError: nil,
                completion: completion)
    }

    private func cacheResolvedURL(_ url: URL?) {
        lock.lock()
        resolvedURL = url
        lock.unlock()
    }

    private func attempt(candidates: [URL],
                         index: Int,
                         token: String?,
                         deadline: Date,
                         lastError: Error?,
                         completion: @escaping (Result<[PassportHabitItem], Error>) -> Void) {
        let remaining = deadline.timeIntervalSinceNow
        guard index < candidates.count, remaining > 0 else {
            finish(.failure(lastError ?? PassportHabitFetchError.noEndpoint), completion: completion)
            return
        }

        let url = candidates[index]
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = max(0.2, min(requestTimeout, remaining))
        if let token {
            request.setValue("life_logger_access=\(token)", forHTTPHeaderField: "Cookie")
        }
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        // 强持有 self：请求在飞的时候 fetcher 必须活着（否则 completion 永不回调）。
        let task = session.dataTask(with: request) { data, response, error in
            if let error {
                logLine("[AirPassport Bridge] Habit fetch \(url.host ?? "?"): \(error.localizedDescription)")
                self.cacheResolvedURL(nil)
                self.attempt(candidates: candidates,
                             index: index + 1,
                             token: token,
                             deadline: deadline,
                             lastError: error,
                             completion: completion)
                return
            }

            let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
            if statusCode == 401 || statusCode == 403 {
                self.cacheResolvedURL(nil)
                self.finish(.failure(PassportHabitFetchError.unauthorized(statusCode)), completion: completion)
                return
            }
            guard statusCode == 200, let data else {
                logLine("[AirPassport Bridge] Habit fetch \(url.host ?? "?"): HTTP \(statusCode)")
                self.cacheResolvedURL(nil)
                self.attempt(candidates: candidates,
                             index: index + 1,
                             token: token,
                             deadline: deadline,
                             lastError: PassportHabitFetchError.httpStatus(statusCode),
                             completion: completion)
                return
            }

            do {
                let items = try PassportHabitMapper.items(fromHabitsResponse: data)
                self.cacheResolvedURL(url)
                self.finish(.success(items), completion: completion)
            } catch {
                self.cacheResolvedURL(nil)
                self.finish(.failure(error), completion: completion)
            }
        }
        task.resume()
    }

    private func finish(_ result: Result<[PassportHabitItem], Error>,
                        completion: @escaping (Result<[PassportHabitItem], Error>) -> Void) {
        lock.lock()
        isFetching = false
        lock.unlock()
        completion(result)
    }
}
