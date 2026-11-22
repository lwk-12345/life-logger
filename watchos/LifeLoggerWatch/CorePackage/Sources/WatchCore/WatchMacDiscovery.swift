import Foundation
import Network

/// 通过 Bonjour 自动发现 Mac 端的 Life Logger 服务。
///
/// 背景（2026-10-15）：`WatchSyncConfigurationStore.candidateBaseURLStrings` 里那 9 个地址
/// 是**写死的**（`192.168.1.100` / `172.20.10.*` / `192.168.1.100` …）。手机热点每次重启、
/// 换路由器、换网络环境都会重新分配 IP（实测：Mac 从 `192.168.1.100` 变成 `192.168.1.50`），
/// 于是手表只会去敲那个过期的地址 —— 表现就是「明明同一个 WiFi 却连不上电脑」。
///
/// 本文件让手表**主动询问局域网里谁在提供 Life Logger 服务**，Mac 端用 `dns-sd -R` 广播
/// `_lifelogger._tcp`，手表用 `NWBrowser` 收。IP 怎么变都能找到，不再依赖硬编码。
///
/// 设计要求：
/// 1. **只发现，不改判定标准** —— 发现到的 host 仍交给 `WatchMacEndpoint.validatedBaseURL`
///    过一遍（端口、scheme、私网段、`.local` 那套规则不变），避免引入新的信任边界。
/// 2. **超时即放弃** —— 局域网可能没有 Mac（用户在外面），必须能在 1~2 秒内干净地返回 nil，
///    不能把同步流程拖住。
/// 3. **不持有长命 browser** —— 每次同步前短促地播一次即可，避免常驻 socket 与耗电。
public enum WatchMacDiscovery {
    /// Bonjour 服务类型。必须与 Mac 端 `dns-sd -R` 广播的类型一致，
    /// 且必须写进 Watch 端 Info.plist 的 `NSBonjourServices`（否则系统会拒绝浏览）。
    public static let serviceType = "_lifelogger._tcp"

    /// 单次发现的总超时（秒）。局域网发现通常 <0.5s 就有结果；超过这个值视为没有 Mac。
    public static let defaultTimeout: TimeInterval = 2.0

    /// 在网络里找 Mac。找到第一个通过 `WatchMacEndpoint` 校验的地址就返回；超时返回 nil。
    ///
    /// - Parameter timeout: 最长等待时间，默认 `defaultTimeout`。
    /// - Returns: 形如 `http://<host>:8765` 的 URL；没找到则为 nil。
    public static func discoverMacBaseURL(timeout: TimeInterval = defaultTimeout) async -> URL? {
        await withCheckedContinuation { (continuation: CheckedContinuation<URL?, Never>) in
            // 用一把锁保证「只 resume 一次」：browseResults 回调可能多次触发，
            // 而 CheckedContinuation 被 resume 两次会直接崩溃。
            let state = ResumeOnce(continuation: continuation)

            let parameters = NWParameters.tcp
            // 允许本地网络访问（watchOS 需要用户授权本地网络；未授权时浏览器状态会报错）
            parameters.includePeerToPeer = true

            let browser = NWBrowser(
                for: .bonjour(type: serviceType, domain: nil),
                using: parameters
            )

            let queue = DispatchQueue(label: "life-logger.watch.mac-discovery")

            browser.stateUpdateHandler = { browserState in
                switch browserState {
                case .failed, .cancelled:
                    state.finish(nil)
                default:
                    break
                }
            }

            browser.browseResultsChangedHandler = { results, _ in
                for result in results {
                    guard case let .service(name, type, domain, _) = result.endpoint else { continue }
                    // 只认我们自己那一类服务；同网段可能有别的 Bonjour 服务混进来。
                    guard type == serviceType else { continue }
                    // 解析出主机名与端口（.local 名字由系统 mDNS 解析）
                    let host = "\(name.replacingOccurrences(of: " ", with: "-")).local"
                    if let url = WatchMacEndpoint.validatedBaseURL("http://\(host):8765") {
                        _ = domain
                        state.finish(url)
                        return
                    }
                }
            }

            browser.start(queue: queue)

            queue.asyncAfter(deadline: .now() + timeout) {
                state.finish(nil)
            }

            state.onFinish = {
                browser.cancel()
            }
        }
    }
}

/// 保证一段异步逻辑只完成一次（NWBrowser 的回调 + 超时定时器会竞争）。
private final class ResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false
    private let continuation: CheckedContinuation<URL?, Never>

    /// 完成时执行（用于取消 browser）。
    var onFinish: (() -> Void)?

    init(continuation: CheckedContinuation<URL?, Never>) {
        self.continuation = continuation
    }

    func finish(_ value: URL?) {
        lock.lock()
        if done {
            lock.unlock()
            return
        }
        done = true
        lock.unlock()
        onFinish?()
        continuation.resume(returning: value)
    }
}
