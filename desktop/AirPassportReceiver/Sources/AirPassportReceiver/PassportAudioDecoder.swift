import Foundation
import PassportBridgeC

public enum PassportAudioDecoderError: Error, Equatable, Sendable {
    case invalidBlockSize
    case decodeFailed(Int32)
}

public final class PassportAudioDecoder {
    public static let sampleRate: Double = 16_000
    public static let channelCount: Int = 1
    public static let blockSize: Int = Int(PASSPORT_PTT_ADPCM_BLOCK_SIZE)
    public static let samplesPerBlock: Int = Int(PASSPORT_PTT_ADPCM_SAMPLES_PER_BLOCK)

    private var state = passport_ptt_adpcm_state_t()

    public init() {
        reset()
    }

    public func reset() {
        passport_ptt_adpcm_state_init(&state)
    }

    public func decodeBlock(_ blockData: Data) throws -> [Int16] {
        guard blockData.count == Self.blockSize else {
            throw PassportAudioDecoderError.invalidBlockSize
        }

        var pcm = [Int16](repeating: 0, count: Self.samplesPerBlock)
        let result = blockData.withUnsafeBytes { rawBuffer -> passport_ptt_adpcm_result_t in
            guard let blockPtr = rawBuffer.baseAddress?.assumingMemoryBound(to: UInt8.self) else {
                return PASSPORT_PTT_ADPCM_INVALID_ARGUMENT
            }
            return pcm.withUnsafeMutableBufferPointer { pcmBuffer in
                passport_ptt_adpcm_decode_block(
                    &state,
                    blockPtr,
                    rawBuffer.count,
                    pcmBuffer.baseAddress,
                    pcmBuffer.count
                )
            }
        }

        guard result == PASSPORT_PTT_ADPCM_OK else {
            throw PassportAudioDecoderError.decodeFailed(result.rawValue)
        }

        return pcm
    }

    public func decodeBlockToPCMData(_ blockData: Data) throws -> Data {
        let samples = try decodeBlock(blockData)
        return samples.withUnsafeBufferPointer { buffer in
            Data(buffer: buffer)
        }
    }
}
