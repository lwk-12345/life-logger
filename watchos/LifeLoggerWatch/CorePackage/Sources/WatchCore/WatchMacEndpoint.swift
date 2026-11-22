import Foundation

public enum WatchMacEndpoint {
    public static func validatedBaseURL(_ rawValue: String) -> URL? {
        guard let url = URL(string: rawValue),
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == "http",
              components.user == nil,
              components.password == nil,
              components.query == nil,
              components.fragment == nil,
              components.port == 8765,
              let host = components.host,
              isTrustedHost(host),
              components.path.isEmpty || components.path == "/" else {
            return nil
        }

        var normalized = components
        normalized.path = ""
        return normalized.url
    }

    private static func isTrustedHost(_ host: String) -> Bool {
        let lower = host.lowercased()
        if lower == "localhost" || lower.hasSuffix(".local") {
            return true
        }
        return isTrustedPrivateIPv4(host)
    }

    private static func isTrustedPrivateIPv4(_ host: String) -> Bool {
        let octets = host.split(separator: ".").compactMap { Int($0) }
        guard octets.count == 4, octets.allSatisfy({ (0...255).contains($0) }) else {
            return false
        }
        let first = octets[0]
        let second = octets[1]
        return first == 10
            || (first == 172 && (16...31).contains(second))
            || (first == 192 && second == 168)
            || (first == 100 && (64...127).contains(second))
    }
}
