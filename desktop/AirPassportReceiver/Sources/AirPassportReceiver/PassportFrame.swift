import Foundation
import PassportBridgeC

public enum PassportFrameType: UInt8, Sendable {
    case begin = 1
    case audio = 2
    case end = 3
    case enter = 4
}

public enum PassportFrameError: Error, Equatable, Sendable {
    case invalidArgument
    case unsupportedVersion
    case unknownType
    case malformed
}

public struct PassportDecodedFrame: Equatable, Sendable {
    public let type: PassportFrameType
    public let sequence: UInt16
    public let monotonicMs: UInt32
    public let payload: Data

    public init(type: PassportFrameType, sequence: UInt16, monotonicMs: UInt32, payload: Data) {
        self.type = type
        self.sequence = sequence
        self.monotonicMs = monotonicMs
        self.payload = payload
    }
}

public enum PassportFrameParser {
    public static func decode(_ data: Data) throws -> PassportDecodedFrame {
        var decoded = passport_ptt_frame_t()
        let result: passport_ptt_frame_result_t = data.withUnsafeBytes { rawBuffer in
            guard let baseAddress = rawBuffer.baseAddress?.assumingMemoryBound(to: UInt8.self) else {
                return PASSPORT_PTT_FRAME_INVALID_ARGUMENT
            }
            return passport_ptt_frame_decode(baseAddress, rawBuffer.count, &decoded)
        }

        switch result {
        case PASSPORT_PTT_FRAME_OK:
            guard let type = PassportFrameType(rawValue: UInt8(decoded.type.rawValue)) else {
                throw PassportFrameError.unknownType
            }
            let payload: Data
            if let payloadPtr = decoded.payload, decoded.payload_size > 0 {
                payload = Data(bytes: payloadPtr, count: decoded.payload_size)
            } else {
                payload = Data()
            }
            return PassportDecodedFrame(
                type: type,
                sequence: decoded.sequence,
                monotonicMs: decoded.monotonic_ms,
                payload: payload
            )
        case PASSPORT_PTT_FRAME_INVALID_ARGUMENT:
            throw PassportFrameError.invalidArgument
        case PASSPORT_PTT_FRAME_UNSUPPORTED_VERSION:
            throw PassportFrameError.unsupportedVersion
        case PASSPORT_PTT_FRAME_UNKNOWN_TYPE:
            throw PassportFrameError.unknownType
        case PASSPORT_PTT_FRAME_MALFORMED:
            throw PassportFrameError.malformed
        default:
            throw PassportFrameError.malformed
        }
    }
}
