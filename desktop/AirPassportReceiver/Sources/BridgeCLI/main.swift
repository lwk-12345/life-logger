import Foundation
import CoreGraphics
import AppKit
import AirPassportReceiver

struct BridgeConfig {
    var virtualKeyCode: CGKeyCode = 0x3A // Left Option (kVK_Option = 58)
    var enableKeyInjection: Bool = true
    var dumpWavDirectory: URL? = nil
    var verbose: Bool = false
    var testMode: Bool = false
    var playFeedbackSound: Bool = true
}

func parseCommandLineArguments() -> BridgeConfig {
    var config = BridgeConfig()
    var args = Array(CommandLine.arguments.dropFirst())

    while !args.isEmpty {
        let arg = args.removeFirst()
        switch arg {
        case "--key-code":
            guard !args.isEmpty else {
                print("Error: --key-code requires an argument (e.g. 0x3A or 58)")
                exit(1)
            }
            let valStr = args.removeFirst()
            if valStr.hasPrefix("0x") || valStr.hasPrefix("0X") {
                if let val = UInt16(valStr.dropFirst(2), radix: 16) {
                    config.virtualKeyCode = CGKeyCode(val)
                }
            } else if let val = UInt16(valStr) {
                config.virtualKeyCode = CGKeyCode(val)
            }
        case "--no-key":
            config.enableKeyInjection = false
        case "--dump-wav":
            guard !args.isEmpty else {
                print("Error: --dump-wav requires a directory path")
                exit(1)
            }
            let dirPath = args.removeFirst()
            config.dumpWavDirectory = URL(fileURLWithPath: dirPath)
        case "--no-sound":
            config.playFeedbackSound = false
        case "-t", "--test":
            config.testMode = true
        case "-v", "--verbose":
            config.verbose = true
        case "-h", "--help":
            print("""
            AirPassport PTT macOS Desktop Receiver Bridge & Tester

            Usage: air-passport-bridge [options]

            Options:
              -t, --test           Run in dedicated interactive key testing mode
              --key-code <code>    Virtual key code to dispatch (default: 0x3A for Left Option)
              --no-key             Disable CGEvent keyboard injection (audio only)
              --no-sound           Disable audio feedback sounds (Tink/Pop)
              --dump-wav <dir>     Dump each PTT session's decoded 16kHz audio to WAV files in <dir>
              -v, --verbose        Enable verbose frame logging
              -h, --help           Show this help message
            """)
            exit(0)
        default:
            print("Unknown argument: \(arg). Use --help for usage.")
            exit(1)
        }
    }
    return config
}

func playSoundAsync(_ path: String) {
    DispatchQueue.global(qos: .userInitiated).async {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/afplay")
        process.arguments = [path]
        try? process.run()
    }
}

final class BridgeRunner: PassportBLEBridgeDelegate {
    let config: BridgeConfig
    private var sessionSamples: [Int16] = []
    private var sessionIndex = 0
    private var sessionStartTime: Date?
    var isOptionDownInSystem: Bool = false

    init(config: BridgeConfig) {
        self.config = config
    }

    func bridgeDidChangeState(_ bridge: PassportBLEBridge, state: PassportBLEBridge.State) {
        let timeStr = ISO8601DateFormatter().string(from: Date())
        switch state {
        case .idle:
            logLine("[\(timeStr)] [AirPassport Bridge] ⏸️ 状态: 空闲 (idle)")
        case .scanning:
            logLine("[\(timeStr)] [AirPassport Bridge] 📡 状态: 正在扫描 AirPassport-PTT 广播...")
        case .connecting:
            logLine("[\(timeStr)] [AirPassport Bridge] 🔄 状态: 正在建立蓝牙连接...")
        case .connected:
            logLine("[\(timeStr)] [AirPassport Bridge] 🔗 状态: 蓝牙物理链路已连接，正在发现服务与协商加密...")
        case .ready:
            logLine("""
            \n========================================================================
            🟢 [READY] AirPassport BLE 接收桥就绪！(GATT 加密 & Stream 订阅完成)
            👉 请按压 AirPassport 实体键（顶键 UP / 中键 OK）：
               • 按下瞬间：立即触发 macOS 左 Option 键 (Down) 并启动 16kHz 音频传输
               • 松开瞬间：立即释放 macOS 左 Option 键 (Up) 并自动保存 WAV 音频
            ========================================================================\n
            """)
        case .disconnected:
            logLine("[\(timeStr)] [AirPassport Bridge] ⚠️ 状态: 蓝牙连接断开，正在自动重新扫描...")
        }
        fflush(stdout)
    }

    func bridgeSessionDidBegin(_ bridge: PassportBLEBridge, sequence: UInt16, monotonicMs: UInt32) {
        sessionSamples.removeAll(keepingCapacity: true)
        sessionStartTime = Date()
        let timeStr = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)
        
        let keyDesc = config.virtualKeyCode == 0x3A ? "Left Option (0x3A)" : "0x" + String(config.virtualKeyCode, radix: 16)
        let keyInfo = config.enableKeyInjection ? " -> Key Down (\(keyDesc)) [已注入]" : " (按键注入已禁用)"

        logLine("""
        \n========================================================================
        🚨 [\(timeStr)] 【检测到硬件按键按下 DOWN！】
           • 蓝牙帧: PTT BEGIN (序号: \(sequence), 硬件运行时间: \(monotonicMs) ms)
           • macOS 键值注入: \(keyInfo)
        ========================================================================
        """)
        fflush(stdout)

        if config.playFeedbackSound {
            playSoundAsync("/System/Library/Sounds/Tink.aiff")
        }
    }

    func bridgeAudioDidReceive(_ bridge: PassportBLEBridge, pcmSamples: [Int16], sequence: UInt16, monotonicMs: UInt32) {
        sessionSamples.append(contentsOf: pcmSamples)
        let totalFrames = sessionSamples.count / 160
        let duration = Double(sessionSamples.count) / 16_000.0
        if config.verbose || config.testMode {
            logLine("\r🎙️ [音频传输中] 序号: \(sequence) | 累计帧数: \(totalFrames) | 样本数: \(sessionSamples.count) | 录制时长: \(String(format: "%.2f", duration))s", terminator: "")
            fflush(stdout)
        }
    }

    func bridgeSessionDidEnd(_ bridge: PassportBLEBridge, sequence: UInt16, monotonicMs: UInt32) {
        let timeStr = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)
        let durationSec = Double(sessionSamples.count) / 16_000.0
        let keyDesc = config.virtualKeyCode == 0x3A ? "Left Option" : "0x" + String(config.virtualKeyCode, radix: 16)
        let keyInfo = config.enableKeyInjection ? " -> Key Up (\(keyDesc)) [已释放]" : ""
        let totalFrames = sessionSamples.count / 160

        logLine("""
        \n========================================================================
        🟢 [\(timeStr)] 【检测到硬件按键松开 UP！】
           • 蓝牙帧: PTT END (序号: \(sequence), 硬件运行时间: \(monotonicMs) ms)
           • macOS 键值注入: \(keyInfo)
           • 会话统计: 持续 \(String(format: "%.2f", durationSec)) 秒 | 累计接收 \(totalFrames) 帧音频 (\(sessionSamples.count) 样本)
        """)

        if let dumpDir = config.dumpWavDirectory {
            sessionIndex += 1
            let filename = String(format: "ptt_session_%04d_%d.wav", sessionIndex, sequence)
            let fileURL = dumpDir.appendingPathComponent(filename)
            let wavData = PassportWAVWriter.encodeWAV(samples: sessionSamples)
            do {
                try FileManager.default.createDirectory(at: dumpDir, withIntermediateDirectories: true)
                try wavData.write(to: fileURL)
                logLine("   • WAV 保存路径: \(fileURL.path)")
            } catch {
                logLine("   • ⚠️ 保存 WAV 失败: \(error)")
            }
        }
        logLine("========================================================================\n")
        fflush(stdout)

        if config.playFeedbackSound {
            playSoundAsync("/System/Library/Sounds/Pop.aiff")
        }
    }

    func bridgeDidTriggerEnter(_ bridge: PassportBLEBridge, monotonicMs: UInt32) {
        let timeStr = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)
        logLine("""
        \n========================================================================
        ↵ [\(timeStr)] 【检测到硬件按键触发: Enter (回车 / 发送)！】
           • 硬件运行时间: \(monotonicMs) ms
           • macOS 键值注入: -> Key Press (kVK_Return: 0x24) [已发送]
        ========================================================================\n
        """)
        fflush(stdout)

        if config.playFeedbackSound {
            playSoundAsync("/System/Library/Sounds/Morse.aiff")
        }
    }
}

let config = parseCommandLineArguments()
let runner = BridgeRunner(config: config)
let bridge = PassportBLEBridge(virtualKeyCode: config.virtualKeyCode, isKeyInjectionEnabled: config.enableKeyInjection)
bridge.delegate = runner

logLine("""
========================================================================
🚀 [AirPassport 按键专用检测工具 / Hardware Key Inspector]
========================================================================
""")
let trusted = AXIsProcessTrusted()
logLine("• macOS 辅助功能权限 (Accessibility): \(trusted ? "✅ 已授权 (AXIsProcessTrusted: true)" : "⚠️ 未授权 (可能无法注入全局按键，请前往 系统设置->隐私与安全性->辅助功能 授予权限)")")
let keyDesc = config.virtualKeyCode == 0x3A ? "0x3A (Left Option)" : "0x" + String(config.virtualKeyCode, radix: 16)
logLine("• 目标按键映射: \(config.enableKeyInjection ? keyDesc : "已禁用")")
if let dumpDir = config.dumpWavDirectory {
    logLine("• WAV 音频落盘目录: \(dumpDir.path)")
}
logLine("• 提示音反馈: \(config.playFeedbackSound ? "开启 (Tink/Pop)" : "关闭")")
logLine("• 监听蓝牙服务: \(PassportBLEBridge.serviceUUID)")
if let virtualDev = PassportAudioStreamPlayer.findVirtualAudioDevice() {
    logLine("• 虚拟麦克风通道: ✅ 已就绪 [\(virtualDev.name)] (音频将实时推流至系统麦克风)")
} else {
    logLine("• 虚拟麦克风通道: ⚠️ 未检测到 BlackHole 驱动 (无声卡电脑可执行: brew install --cask blackhole-2ch)")
}
logLine("========================================================================\n")
fflush(stdout)

// Global macOS WindowServer modifier flag monitor to confirm injected keys
NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { event in
    let isOption = event.modifierFlags.contains(.option)
    if isOption != runner.isOptionDownInSystem {
        runner.isOptionDownInSystem = isOption
        logLine("   💻 [macOS 系统全局确认] Option 修饰键状态变更: \(isOption ? "【按下】" : "【抬起】")")
        fflush(stdout)
    }
}

bridge.startScanning()
RunLoop.main.run()
