import Foundation

public enum WatchHealthResponseResult: Equatable, Sendable {
    case ready
    case directAccessDisabled
    case unauthorized
    case serverRejected(statusCode: Int)
    case invalidResponse
}

public enum WatchHealthResponsePolicy {
    public static func classify(statusCode: Int, body: Data) -> WatchHealthResponseResult {
        if statusCode == 401 {
            return .unauthorized
        }
        guard (200..<300).contains(statusCode) else {
            return .serverRejected(statusCode: statusCode)
        }
        guard let payload = try? JSONDecoder().decode(WatchHealthPayload.self, from: body) else {
            return .invalidResponse
        }
        return payload.watchUploadEnabled ? .ready : .directAccessDisabled
    }
}

private struct WatchHealthPayload: Decodable {
    let watchUploadEnabled: Bool

    enum CodingKeys: String, CodingKey {
        case watchUploadEnabled = "watch_upload_enabled"
    }
}
