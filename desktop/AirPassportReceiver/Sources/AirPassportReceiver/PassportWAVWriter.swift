import Foundation

/// Utility to generate standard RIFF/WAVE containers for 16 kHz 16-bit linear PCM audio.
public enum PassportWAVWriter {
    public static let defaultSampleRate: Int = 16_000
    public static let defaultChannels: Int = 1
    public static let defaultBitsPerSample: Int = 16

    /// Creates a 44-byte standard RIFF header for linear PCM.
    public static func createWAVHeader(
        pcmDataSize: Int,
        sampleRate: Int = defaultSampleRate,
        channels: Int = defaultChannels,
        bitsPerSample: Int = defaultBitsPerSample
    ) -> Data {
        var header = Data()
        header.reserveCapacity(44)

        let byteRate = UInt32(sampleRate * channels * (bitsPerSample / 8))
        let blockAlign = UInt16(channels * (bitsPerSample / 8))
        let chunkSize = UInt32(36 + pcmDataSize)
        let subchunk2Size = UInt32(pcmDataSize)

        // RIFF chunk descriptor
        header.append(contentsOf: "RIFF".utf8)
        withUnsafeBytes(of: chunkSize.littleEndian) { header.append(contentsOf: $0) }
        header.append(contentsOf: "WAVE".utf8)

        // "fmt " subchunk
        header.append(contentsOf: "fmt ".utf8)
        withUnsafeBytes(of: UInt32(16).littleEndian) { header.append(contentsOf: $0) }
        withUnsafeBytes(of: UInt16(1).littleEndian) { header.append(contentsOf: $0) } // PCM = 1
        withUnsafeBytes(of: UInt16(channels).littleEndian) { header.append(contentsOf: $0) }
        withUnsafeBytes(of: UInt32(sampleRate).littleEndian) { header.append(contentsOf: $0) }
        withUnsafeBytes(of: byteRate.littleEndian) { header.append(contentsOf: $0) }
        withUnsafeBytes(of: blockAlign.littleEndian) { header.append(contentsOf: $0) }
        withUnsafeBytes(of: UInt16(bitsPerSample).littleEndian) { header.append(contentsOf: $0) }

        // "data" subchunk
        header.append(contentsOf: "data".utf8)
        withUnsafeBytes(of: subchunk2Size.littleEndian) { header.append(contentsOf: $0) }

        return header
    }

    /// Encodes Int16 samples into a full WAV container.
    public static func encodeWAV(
        samples: [Int16],
        sampleRate: Int = defaultSampleRate
    ) -> Data {
        let pcmData = samples.withUnsafeBufferPointer { Data(buffer: $0) }
        var wav = createWAVHeader(pcmDataSize: pcmData.count, sampleRate: sampleRate)
        wav.append(pcmData)
        return wav
    }
}
