import Foundation
import CoreGraphics

public final class PassportKeyDispatcher {
    public enum HotkeyMode: String, CaseIterable, Codable {
        case holdOption = "长按 Option (豆包按住说话，松手结束)"
        case doubleTapOption = "双击 Option (系统/免按模式)"
        case doubleTapControl = "双击 Control"
        case holdSpace = "长按空格"
    }

    public var hotkeyMode: HotkeyMode
    public let virtualKeyCode: CGKeyCode
    public let isEnabled: Bool
    public private(set) var isKeyDown: Bool = false

    public init(virtualKeyCode: CGKeyCode = 0x3A, isEnabled: Bool = true, hotkeyMode: HotkeyMode = .holdOption) {
        self.virtualKeyCode = virtualKeyCode
        self.isEnabled = isEnabled
        self.hotkeyMode = hotkeyMode
    }

    @discardableResult
    public func handleKeyDown() -> Bool {
        guard !isKeyDown else { return false }
        isKeyDown = true
        guard isEnabled else { return true }

        let source = CGEventSource(stateID: .hidSystemState)

        switch hotkeyMode {
        case .doubleTapOption:
            let flags: CGEventFlags = [.maskAlternate, CGEventFlags(rawValue: 0x20)]
            for _ in 0..<2 {
                if let evDown = CGEvent(source: source) {
                    evDown.type = .flagsChanged
                    evDown.setIntegerValueField(.keyboardEventKeycode, value: 0x3A)
                    evDown.flags = flags
                    evDown.post(tap: .cghidEventTap)
                }
                usleep(40000)
                if let evUp = CGEvent(source: source) {
                    evUp.type = .flagsChanged
                    evUp.setIntegerValueField(.keyboardEventKeycode, value: 0x3A)
                    evUp.flags = []
                    evUp.post(tap: .cghidEventTap)
                }
                usleep(60000)
            }
            return true

        case .holdOption:
            let flags: CGEventFlags = [.maskAlternate, CGEventFlags(rawValue: 0x20)]
            if let evDown = CGEvent(source: source) {
                evDown.type = .flagsChanged
                evDown.setIntegerValueField(.keyboardEventKeycode, value: 0x3A)
                evDown.flags = flags
                evDown.post(tap: .cghidEventTap)
                return true
            }
            return false

        case .doubleTapControl:
            let flags: CGEventFlags = [.maskControl, CGEventFlags(rawValue: 0x01)]
            for _ in 0..<2 {
                if let evDown = CGEvent(source: source) {
                    evDown.type = .flagsChanged
                    evDown.setIntegerValueField(.keyboardEventKeycode, value: 0x3B)
                    evDown.flags = flags
                    evDown.post(tap: .cghidEventTap)
                }
                usleep(40000)
                if let evUp = CGEvent(source: source) {
                    evUp.type = .flagsChanged
                    evUp.setIntegerValueField(.keyboardEventKeycode, value: 0x3B)
                    evUp.flags = []
                    evUp.post(tap: .cghidEventTap)
                }
                usleep(60000)
            }
            return true

        case .holdSpace:
            if let evDown = CGEvent(keyboardEventSource: source, virtualKey: 0x31, keyDown: true) {
                evDown.post(tap: .cghidEventTap)
                return true
            }
            return false
        }
    }

    @discardableResult
    public func handleKeyUp() -> Bool {
        guard isKeyDown else { return false }
        isKeyDown = false
        guard isEnabled else { return true }

        let source = CGEventSource(stateID: .hidSystemState)

        switch hotkeyMode {
        case .doubleTapOption, .doubleTapControl:
            return true

        case .holdOption:
            if let evUp = CGEvent(source: source) {
                evUp.type = .flagsChanged
                evUp.setIntegerValueField(.keyboardEventKeycode, value: 0x3A)
                evUp.flags = []
                evUp.post(tap: .cghidEventTap)
                return true
            }
            return false

        case .holdSpace:
            if let evUp = CGEvent(keyboardEventSource: source, virtualKey: 0x31, keyDown: false) {
                evUp.post(tap: .cghidEventTap)
                return true
            }
            return false
        }
    }

    public func reset() {
        if isKeyDown {
            handleKeyUp()
        }
    }

    @discardableResult
    public func injectEnter() -> Bool {
        guard isEnabled else { return true }
        let returnKeyCode: CGKeyCode = 0x24 // macOS kVK_Return (Enter)
        let source = CGEventSource(stateID: .hidSystemState)
        if let eventDown = CGEvent(keyboardEventSource: source, virtualKey: returnKeyCode, keyDown: true) {
            eventDown.post(tap: .cghidEventTap)
        }
        usleep(30000) // 30ms hold time
        if let eventUp = CGEvent(keyboardEventSource: source, virtualKey: returnKeyCode, keyDown: false) {
            eventUp.post(tap: .cghidEventTap)
            return true
        }
        return false
    }
}
