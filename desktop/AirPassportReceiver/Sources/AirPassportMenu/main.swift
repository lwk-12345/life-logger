import AppKit
import Foundation
import CoreGraphics
import AudioToolbox
import AirPassportReceiver

final class MenuBarController: NSObject, NSApplicationDelegate, PassportBLEBridgeDelegate {
    private var statusItem: NSStatusItem!
    private var bridge: PassportBLEBridge!
    private var isConnected: Bool = false
    private var isRecording: Bool = false
    private var playSound: Bool = true
    private var sessionSamples: [Int16] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        setbuf(stdout, nil)
        setbuf(stderr, nil)

        // 设置为后台轻量应用（不显示 Dock 图标，仅常驻顶部菜单栏）
        NSApplication.shared.setActivationPolicy(.accessory)

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        updateStatusItemTitle(status: "🔴")

        bridge = PassportBLEBridge(virtualKeyCode: 0x3A, isKeyInjectionEnabled: true)
        if let savedModeStr = UserDefaults.standard.string(forKey: "airpassport_hotkey_mode"),
           let mode = PassportKeyDispatcher.HotkeyMode(rawValue: savedModeStr) {
            bridge.keyDispatcher.hotkeyMode = mode
        } else {
            bridge.keyDispatcher.hotkeyMode = .doubleTapOption
        }
        bridge.delegate = self

        if !AXIsProcessTrusted() {
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(options)
        }

        buildMenu()
        bridge.startScanning()
    }

    private func updateStatusItemTitle(status: String) {
        DispatchQueue.main.async {
            if let button = self.statusItem.button {
                button.title = "🎙️ \(status)"
            }
        }
    }

    private func buildMenu() {
        let menu = NSMenu()

        // 状态行
        let statusTitle = isConnected ? "🟢 状态: 已连接 (AirPassport-PTT)" : "🔴 状态: 未连接 (空闲)"
        let statusItemMenu = NSMenuItem(title: statusTitle, action: nil, keyEquivalent: "")
        statusItemMenu.isEnabled = false
        menu.addItem(statusItemMenu)

        menu.addItem(NSMenuItem.separator())

        // 绑定 / 接管按钮
        let bindItem = NSMenuItem(title: "⚡ 立即绑定 / 接管连接", action: #selector(handleBindTakeover), keyEquivalent: "b")
        bindItem.target = self
        menu.addItem(bindItem)

        // 释放 / 断开按钮
        let releaseItem = NSMenuItem(title: "⏸️ 释放 / 断开当前连接 (让出给其它电脑)", action: #selector(handleRelease), keyEquivalent: "r")
        releaseItem.target = self
        menu.addItem(releaseItem)

        menu.addItem(NSMenuItem.separator())

        // 提示音开关
        let soundItem = NSMenuItem(title: "🔊 按键音效反馈", action: #selector(toggleSound), keyEquivalent: "")
        soundItem.state = playSound ? .on : .off
        soundItem.target = self
        menu.addItem(soundItem)

        menu.addItem(NSMenuItem.separator())

        // 虚拟麦克风通道与实时推流
        if let virtualDev = PassportAudioStreamPlayer.findVirtualAudioDevice() {
            let micTitle = "🎙️ 虚拟麦克风: \(virtualDev.name) (已激活)"
            let micItem = NSMenuItem(title: micTitle, action: nil, keyEquivalent: "")
            micItem.isEnabled = false
            menu.addItem(micItem)
        } else {
            let micTitle = "⚠️ 虚拟麦克风: 未检测到 BlackHole (点此复制安装命令)"
            let micItem = NSMenuItem(title: micTitle, action: #selector(handleCopyVirtualMicInstallCommand), keyEquivalent: "")
            micItem.target = self
            menu.addItem(micItem)
        }

        let streamToggleItem = NSMenuItem(title: "📡 实时音频麦克风推流", action: #selector(toggleAudioStream), keyEquivalent: "")
        streamToggleItem.state = bridge.audioStreamPlayer.isEnabled ? .on : .off
        streamToggleItem.target = self
        menu.addItem(streamToggleItem)

        // 语音唤起快捷键模式
        let modeMenu = NSMenu()
        for mode in PassportKeyDispatcher.HotkeyMode.allCases {
            let modeItem = NSMenuItem(title: mode.rawValue, action: #selector(handleSelectHotkeyMode(_:)), keyEquivalent: "")
            modeItem.target = self
            modeItem.representedObject = mode
            modeItem.state = (bridge.keyDispatcher.hotkeyMode == mode) ? .on : .off
            modeMenu.addItem(modeItem)
        }
        let modeParentItem = NSMenuItem(title: "⌨️ 输入法热键: \(bridge.keyDispatcher.hotkeyMode.rawValue)", action: nil, keyEquivalent: "")
        modeParentItem.submenu = modeMenu
        menu.addItem(modeParentItem)

        menu.addItem(NSMenuItem.separator())

        // 辅助功能权限状态
        let isTrusted = AXIsProcessTrusted()
        if isTrusted {
            let authItem = NSMenuItem(title: "🛡️ 键盘注入权限: 已授权 ✅", action: nil, keyEquivalent: "")
            authItem.isEnabled = false
            menu.addItem(authItem)
        } else {
            let authItem = NSMenuItem(title: "⚠️ 键盘注入权限: 未生效 (点此去系统设置授权/重开)", action: #selector(handleRequestAccessibility), keyEquivalent: "")
            authItem.target = self
            menu.addItem(authItem)
        }

        menu.addItem(NSMenuItem.separator())

        // 退出
        let quitItem = NSMenuItem(title: "退出 AirPassport", action: #selector(handleQuit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu
    }

    @objc private func handleRequestAccessibility() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
        let alert = NSAlert()
        alert.messageText = "辅助功能 (Accessibility) 权限说明"
        alert.informativeText = "AirPassport 需要辅助功能权限以模拟按下按键（Option）唤醒输入法。\n\n【操作指南】\n1. 请在弹出的【系统设置】→【隐私与安全性】→【辅助功能】中；\n2. 若列表中已有 AirPassport，请【先关闭开关，再重新打开】（每次应用更新重新打包后 macOS 强制要求重新激活一次）；\n3. 若列表中没有，请点击底部的【+】号，添加 /Applications/AirPassport.app 并打开开关。"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "我知道了")
        alert.runModal()
    }

    @objc private func handleBindTakeover() {
        logLine("[Menu] 用户点击: 立即绑定 / 接管连接")
        bridge.stop()
        bridge.startScanning()
        updateStatusItemTitle(status: "🟡")
    }

    @objc private func handleRelease() {
        logLine("[Menu] 用户点击: 释放连接")
        bridge.stop()
        isConnected = false
        updateStatusItemTitle(status: "🔴")
        buildMenu()
    }

    @objc private func toggleSound() {
        playSound.toggle()
        buildMenu()
    }

    @objc private func toggleAudioStream() {
        bridge.audioStreamPlayer.isEnabled.toggle()
        buildMenu()
    }

    @objc private func handleSelectHotkeyMode(_ sender: NSMenuItem) {
        if let mode = sender.representedObject as? PassportKeyDispatcher.HotkeyMode {
            bridge.keyDispatcher.hotkeyMode = mode
            UserDefaults.standard.set(mode.rawValue, forKey: "airpassport_hotkey_mode")
            buildMenu()
        }
    }

    @objc private func handleCopyVirtualMicInstallCommand() {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString("brew install --cask blackhole-2ch", forType: .string)

        let alert = NSAlert()
        alert.messageText = "虚拟麦克风通道 (BlackHole 2ch)"
        alert.informativeText = "安装命令已复制到剪贴板：\nbrew install --cask blackhole-2ch\n\n【适用场景】\n工作室台式机/Mac mini 无内置麦克风时，安装 BlackHole 并将系统【声音输入】选为【BlackHole 2ch】，即可在任何应用（微信、系统听写打字等）直接使用 AirPassport 硬件无线麦！"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "好的")
        alert.runModal()
    }

    @objc private func handleQuit() {
        bridge.stop()
        NSApplication.shared.terminate(nil)
    }

    private func playSoundAsync(_ path: String) {
        guard playSound else { return }
        DispatchQueue.main.async {
            if let sound = NSSound(contentsOfFile: path, byReference: true) {
                let success = sound.play()
                logLine("[Menu Sound] Playing \(path): \(success)")
            } else {
                logLine("[Menu Sound] Failed to load sound: \(path)")
            }
        }
    }

    // MARK: - PassportBLEBridgeDelegate

    func bridgeDidChangeState(_ bridge: PassportBLEBridge, state: PassportBLEBridge.State) {
        logLine("[Menu] bridgeDidChangeState: \(state)")
        switch state {
        case .ready:
            isConnected = true
            updateStatusItemTitle(status: "🟢")
        case .connecting, .connected:
            updateStatusItemTitle(status: "🟡")
        default:
            isConnected = false
            updateStatusItemTitle(status: "🔴")
        }
        buildMenu()
    }

    func bridgeSessionDidBegin(_ bridge: PassportBLEBridge, sequence: UInt16, monotonicMs: UInt32) {
        logLine("[Menu] bridgeSessionDidBegin (seq: \(sequence), ms: \(monotonicMs))")
        isRecording = true
        sessionSamples.removeAll(keepingCapacity: true)
        updateStatusItemTitle(status: "🔴 录音中")
        playSoundAsync("/System/Library/Sounds/Tink.aiff")
    }

    func bridgeAudioDidReceive(_ bridge: PassportBLEBridge, pcmSamples: [Int16], sequence: UInt16, monotonicMs: UInt32) {
        sessionSamples.append(contentsOf: pcmSamples)
    }

    func bridgeSessionDidEnd(_ bridge: PassportBLEBridge, sequence: UInt16, monotonicMs: UInt32) {
        logLine("[Menu] bridgeSessionDidEnd (seq: \(sequence), ms: \(monotonicMs), totalSamples: \(sessionSamples.count))")
        isRecording = false
        updateStatusItemTitle(status: "🟢")
        playSoundAsync("/System/Library/Sounds/Pop.aiff")

        // 异步保存 WAV 录音
        if !sessionSamples.isEmpty {
            let wavData = PassportWAVWriter.encodeWAV(samples: sessionSamples)
            let dumpDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("AirPassportRecordings")
            try? FileManager.default.createDirectory(at: dumpDir, withIntermediateDirectories: true)
            let filename = "ptt_\(Int(Date().timeIntervalSince1970)).wav"
            try? wavData.write(to: dumpDir.appendingPathComponent(filename))
        }
    }

    func bridgeDidTriggerEnter(_ bridge: PassportBLEBridge, monotonicMs: UInt32) {
        logLine("[Menu] bridgeDidTriggerEnter (ms: \(monotonicMs))")
        playSoundAsync("/System/Library/Sounds/Morse.aiff")
    }
}

// 主程序入口
let app = NSApplication.shared
let delegate = MenuBarController()
app.delegate = delegate
app.run()
