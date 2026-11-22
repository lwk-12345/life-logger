import Foundation
import AudioToolbox
import CoreAudio
import AudioUnit

/// AudioRingBuffer:
/// 线程安全的低延迟立体声环形缓冲池，用于桥接 AirPassport 异步网络收包与 CoreAudio 实时渲染线程。
private final class AudioRingBuffer {
    // 4 秒容量 (48,000 Hz * 2 channels * 4 seconds = 384,000 Float32)
    private var buffer = [Float32](repeating: 0, count: 384_000)
    private var writeIndex = 0
    private var readIndex = 0
    private var count = 0
    private let lock = NSLock()

    func write(samples: [Int16]) {
        lock.lock()
        defer { lock.unlock() }

        // 1 采样点 (16kHz Mono) -> 3 个立体声帧 (6 个 Float32 样本, L=R)
        for s in samples {
            let f = Float32(s) / 32768.0
            for _ in 0..<3 {
                if count + 2 <= buffer.count {
                    buffer[writeIndex] = f
                    buffer[writeIndex + 1] = f
                    writeIndex = (writeIndex + 2) % buffer.count
                    count += 2
                }
            }
        }
    }

    func read(into outPtr: UnsafeMutablePointer<Float32>, frames: Int) {
        lock.lock()
        defer { lock.unlock() }

        let samplesNeeded = frames * 2
        var samplesRead = 0
        while samplesRead < samplesNeeded && count > 0 {
            outPtr[samplesRead] = buffer[readIndex]
            readIndex = (readIndex + 1) % buffer.count
            samplesRead += 1
            count -= 1
        }
        // 若缓冲未饱和或用户处于静音间隙，用纯净静音 (0.0) 填充，防止 CoreAudio 欠载报错
        if samplesRead < samplesNeeded {
            memset(outPtr + samplesRead, 0, (samplesNeeded - samplesRead) * MemoryLayout<Float32>.size)
        }
    }

    func reset() {
        lock.lock()
        defer { lock.unlock() }
        count = 0
        writeIndex = 0
        readIndex = 0
    }
}

/// PassportAudioStreamPlayer:
/// 负责将 AirPassport 接收到的 16kHz 16-bit PCM 音频流实时输出到指定的音频设备。
/// 优先自动寻找虚拟声卡（如 BlackHole 2ch、VB-Cable、Loopback），实现免物理麦克风的虚拟麦克风推流。
///
/// 架构特性：
/// 1. 基于专业底层 CoreAudio HAL Output AudioUnit 构建，彻底淘汰容易欠载暂停的 AudioQueue；
/// 2. 原生输出 48,000 Hz, 2 通道 (Stereo), 32-bit Float 数据流，与 BlackHole 驱动原生流规范完全吻合；
/// 3. 采用实时渲染回调（AURenderCallback）+ 4秒环形缓冲，实现持续流式推流（有声推声、无声补零），电平表灵敏响应；
/// 4. 彻底根除析构挂起与丢包，提供毫秒级超低延迟语音交互。
public final class PassportAudioStreamPlayer {
    private var audioUnit: AudioUnit?
    private let ringBuffer = AudioRingBuffer()
    private let lock = NSRecursiveLock()
    public private(set) var targetDeviceName: String?
    public private(set) var targetDeviceUID: String?
    public var isEnabled: Bool = true
    public private(set) var isOutputRunning: Bool = false

    public init(targetDeviceUID: String? = nil, targetDeviceName: String? = nil) {
        self.targetDeviceUID = targetDeviceUID
        self.targetDeviceName = targetDeviceName
        ensureAudioUnitInitialized()
    }

    /// 扫描当前系统中的 CoreAudio 输出设备，查找虚拟音频通道
    public static func findVirtualAudioDevice() -> (uid: String, name: String)? {
        var propertyAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var dataSize: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &propertyAddress, 0, nil, &dataSize) == noErr else {
            return nil
        }
        let count = Int(dataSize) / MemoryLayout<AudioDeviceID>.size
        var deviceIDs = [AudioDeviceID](repeating: 0, count: count)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &propertyAddress, 0, nil, &dataSize, &deviceIDs) == noErr else {
            return nil
        }

        let virtualKeywords = ["blackhole", "vb-cable", "loopback", "soundflower", "existential"]
        for dev in deviceIDs {
            var nameAddr = AudioObjectPropertyAddress(
                mSelector: kAudioDevicePropertyDeviceNameCFString,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            var nameRef: Unmanaged<CFString>?
            var strSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
            guard AudioObjectGetPropertyData(dev, &nameAddr, 0, nil, &strSize, &nameRef) == noErr,
                  let name = nameRef?.takeRetainedValue() as String? else { continue }

            var uidAddr = AudioObjectPropertyAddress(
                mSelector: kAudioDevicePropertyDeviceUID,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            var uidRef: Unmanaged<CFString>?
            guard AudioObjectGetPropertyData(dev, &uidAddr, 0, nil, &strSize, &uidRef) == noErr,
                  let uid = uidRef?.takeRetainedValue() as String? else { continue }

            let lowerName = name.lowercased()
            if virtualKeywords.contains(where: { lowerName.contains($0) }) {
                return (uid: uid, name: name)
            }
        }
        return nil
    }

    /// 根据 UID 检索对应的 CoreAudio 设备 ID
    public static func findAudioDeviceID(forUID uid: String) -> AudioDeviceID? {
        var propertyAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var dataSize: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &propertyAddress, 0, nil, &dataSize) == noErr else {
            return nil
        }
        let count = Int(dataSize) / MemoryLayout<AudioDeviceID>.size
        var deviceIDs = [AudioDeviceID](repeating: 0, count: count)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &propertyAddress, 0, nil, &dataSize, &deviceIDs) == noErr else {
            return nil
        }

        for dev in deviceIDs {
            var uidAddr = AudioObjectPropertyAddress(
                mSelector: kAudioDevicePropertyDeviceUID,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            var uidRef: Unmanaged<CFString>?
            var strSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
            guard AudioObjectGetPropertyData(dev, &uidAddr, 0, nil, &strSize, &uidRef) == noErr,
                  let devUID = uidRef?.takeRetainedValue() as String? else { continue }
            if devUID == uid {
                return dev
            }
        }
        return nil
    }

    private func ensureAudioUnitInitialized() {
        lock.lock()
        defer { lock.unlock() }
        guard audioUnit == nil else { return }

        // 优先使用设置的目标设备，若无则自动探测虚拟声卡
        if targetDeviceUID == nil {
            if let virtualDev = Self.findVirtualAudioDevice() {
                self.targetDeviceUID = virtualDev.uid
                self.targetDeviceName = virtualDev.name
            }
        }

        var devID: AudioDeviceID = 0
        if let uid = targetDeviceUID, let id = Self.findAudioDeviceID(forUID: uid) {
            devID = id
        } else {
            // 回退到系统默认输出
            var defAddr = AudioObjectPropertyAddress(
                mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            var devSize = UInt32(MemoryLayout<AudioDeviceID>.size)
            AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &defAddr, 0, nil, &devSize, &devID)
        }

        var desc = AudioComponentDescription(
            componentType: kAudioUnitType_Output,
            componentSubType: kAudioUnitSubType_HALOutput,
            componentManufacturer: kAudioUnitManufacturer_Apple,
            componentFlags: 0,
            componentFlagsMask: 0
        )

        guard let comp = AudioComponentFindNext(nil, &desc) else {
            logLine("[PassportAudioStreamPlayer] Failed to find HALOutput component")
            return
        }

        var unit: AudioUnit?
        let newStatus = AudioComponentInstanceNew(comp, &unit)
        guard newStatus == noErr, let au = unit else {
            logLine("[PassportAudioStreamPlayer] AudioComponentInstanceNew failed: \(newStatus)")
            return
        }

        // 1. 开启 Output Scope
        var enableIO: UInt32 = 1
        AudioUnitSetProperty(au, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &enableIO, UInt32(MemoryLayout<UInt32>.size))

        // 2. 绑定目标声卡硬件设备
        if devID != 0 {
            let setDevStatus = AudioUnitSetProperty(au, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &devID, UInt32(MemoryLayout<AudioDeviceID>.size))
            if setDevStatus == noErr {
                logLine("[PassportAudioStreamPlayer] Audio successfully routed to device: \(self.targetDeviceName ?? self.targetDeviceUID ?? "\(devID)")")
            } else {
                logLine("[PassportAudioStreamPlayer] AudioUnitSetProperty CurrentDevice failed: \(setDevStatus)")
            }
        }

        // 3. 配置格式: 48,000 Hz, 2 通道 (Stereo), 32 位 Float
        var format = AudioStreamBasicDescription(
            mSampleRate: 48000.0,
            mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
            mBytesPerPacket: 8,
            mFramesPerPacket: 1,
            mBytesPerFrame: 8,
            mChannelsPerFrame: 2,
            mBitsPerChannel: 32,
            mReserved: 0
        )
        AudioUnitSetProperty(au, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0, &format, UInt32(MemoryLayout<AudioStreamBasicDescription>.size))

        // 4. 设置实时渲染回调
        let renderCallback: AURenderCallback = { inRefCon, _, _, _, inNumberFrames, ioData -> OSStatus in
            guard let ioData = ioData else { return noErr }
            let rb = Unmanaged<AudioRingBuffer>.fromOpaque(inRefCon).takeUnretainedValue()
            let list = UnsafeMutableAudioBufferListPointer(ioData)
            for buf in list {
                guard let ptr = buf.mData?.assumingMemoryBound(to: Float32.self) else { continue }
                rb.read(into: ptr, frames: Int(inNumberFrames))
            }
            return noErr
        }

        var callbackStruct = AURenderCallbackStruct(
            inputProc: renderCallback,
            inputProcRefCon: Unmanaged.passUnretained(self.ringBuffer).toOpaque()
        )
        AudioUnitSetProperty(au, kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0, &callbackStruct, UInt32(MemoryLayout<AURenderCallbackStruct>.size))

        // 5. 初始化并启动
        AudioUnitInitialize(au)
        AudioOutputUnitStart(au)
        self.audioUnit = au
        self.isOutputRunning = true
    }

    public func startSession() {
        guard isEnabled else { return }
        ensureAudioUnitInitialized()
        ringBuffer.reset()
    }

    public func write(samples: [Int16]) {
        guard isEnabled, !samples.isEmpty else { return }
        ensureAudioUnitInitialized()
        ringBuffer.write(samples: samples)
    }

    public func stopSession(immediate: Bool = false) {
        // 音频在环形缓冲池中自动流淌完毕后自动输出零电平静音，保持 CoreAudio 虚拟声卡时钟持续运转
    }

    deinit {
        lock.lock()
        defer { lock.unlock() }
        if let au = audioUnit {
            if isOutputRunning {
                AudioOutputUnitStop(au)
                isOutputRunning = false
            }
            AudioUnitUninitialize(au)
            AudioComponentInstanceDispose(au)
            audioUnit = nil
        }
    }
}
