import Foundation

// ==============================================================================
// AirPassport 日志辅助：给每一行 stdout 日志加本地时间戳 `[HH:mm:ss] `
// ==============================================================================
//
// 【为什么要加时间戳】
// 正式安装版 AirPassport.app 由 launchd 托管，stdout 全部落在
// `~/Library/Logs/AirPassport/launchd.stdout.log`。改造前这些行没有时间信息，
// 排查时只能看到"某条错误出现了 N 次"（例如 `Failed to connect: Peer removed
// pairing information` 出现了 10 次），却无法判断它们集中在哪个时间段，
// 只能靠"再复现一次看有没有新增"这种笨办法。行首带上秒级时刻后，
// 直接按时间轴回溯事件即可，不需要复现。
//
// 【为什么不用 os_log / Logger】
// 1. 现有排查经验（grep 关键字、less 翻页、launchd 重定向）全部建立在 stdout 文本上，
//    改用 os_log 会让这些姿势全部失效（内容进统一日志系统，不再落这个文件）；
// 2. 出问题时往往要离线看日志文件，`log show` 不如直接看文件方便；
// 3. 不引入任何第三方依赖，保持"print 到 stdout"这唯一一条日志通道不变。
//
// 【性能】
// `localtime_r` + `strftime` 都是 C 层调用，比"每次新建 DateFormatter"快一个数量级；
// 并且只用栈上状态（没有共享可变缓存/锁），多线程并发打印也不会互相踩。
// 这个函数会被高频调用（音频流每秒可能打印多次），所以不做任何堆上的缓存对象。
public enum AirPassportLog {

    /// 本地时间 `HH:mm:ss`（24 小时制，秒级）。
    ///
    /// 例：`AirPassportLog.timeStamp()` → `"09:07:04"`
    public static func timeStamp(_ now: Date = Date()) -> String {
        var seconds = time_t(now.timeIntervalSince1970)
        var parts = tm()
        _ = localtime_r(&seconds, &parts)
        // 16 字节足够容纳 "HH:mm:ss"（8 字符 + NUL）。
        var buffer = [CChar](repeating: 0, count: 16)
        _ = strftime(&buffer, buffer.count, "%H:%M:%S", &parts)
        return String(cString: buffer)
    }

    /// 在行首加时间戳后打印，其余行为与 `print(_:terminator:)` 完全一致。
    ///
    /// - 注意：`BridgeCLI` 的音频进度行以 `\r` 开头（原地刷新同一行），
    ///   时间戳必须插在 `\r` **之后**，否则 `\r` 回车会把刚打印的时间戳擦掉、
    ///   串到上一行去。
    public static func line(_ text: String, terminator: String = "\n") {
        if text.hasPrefix("\r") {
            print("\r[\(timeStamp())] \(text.dropFirst())", terminator: terminator)
        } else {
            print("[\(timeStamp())] \(text)", terminator: terminator)
        }
    }
}

/// 全局便捷入口：用法与 `print` 相同，只是行首多一个 `[HH:mm:ss] ` 时间戳。
///
/// 排查经验挂在日志正文的字符串上，所以调用点只替换 `print(` → `logLine(`，
/// **不改动任何已有文案**，保证 grep 关键字依旧命中。
public func logLine(_ text: String, terminator: String = "\n") {
    AirPassportLog.line(text, terminator: terminator)
}
