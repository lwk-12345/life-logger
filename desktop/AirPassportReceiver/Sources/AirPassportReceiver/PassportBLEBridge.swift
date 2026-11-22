import Foundation
import CoreBluetooth
import CoreGraphics

public protocol PassportBLEBridgeDelegate: AnyObject {
    func bridgeDidChangeState(_ bridge: PassportBLEBridge, state: PassportBLEBridge.State)
    func bridgeSessionDidBegin(_ bridge: PassportBLEBridge, sequence: UInt16, monotonicMs: UInt32)
    func bridgeAudioDidReceive(_ bridge: PassportBLEBridge, pcmSamples: [Int16], sequence: UInt16, monotonicMs: UInt32)
    func bridgeSessionDidEnd(_ bridge: PassportBLEBridge, sequence: UInt16, monotonicMs: UInt32)
    func bridgeDidTriggerEnter(_ bridge: PassportBLEBridge, monotonicMs: UInt32)
}

public extension PassportBLEBridgeDelegate {
    func bridgeDidTriggerEnter(_ bridge: PassportBLEBridge, monotonicMs: UInt32) {}
}

public final class PassportBLEBridge: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    public enum State: Equatable, Sendable {
        case idle
        case scanning
        case connecting
        case connected
        case ready
        case disconnected
    }

    public static let serviceUUID = CBUUID(string: "01000054-5450-42A1-6F4E-2D9100107A8B")
    public static let streamUUID = CBUUID(string: "01000054-5450-42A1-6F4E-2D9101107A8B")
    public static let capabilitiesUUID = CBUUID(string: "01000054-5450-42A1-6F4E-2D9102107A8B")
    public static let targetDeviceName = "AirPassport-PTT"

    public private(set) var state: State = .idle {
        didSet {
            delegate?.bridgeDidChangeState(self, state: state)
        }
    }

    public weak var delegate: PassportBLEBridgeDelegate?
    public let keyDispatcher: PassportKeyDispatcher
    public let audioDecoder: PassportAudioDecoder
    public let audioStreamPlayer: PassportAudioStreamPlayer

    private var centralManager: CBCentralManager!
    private var peripheral: CBPeripheral?
    private var streamCharacteristic: CBCharacteristic?
    /// [2026-10-01 BLE 校时] capabilities 特征（写入校时用）+ 失败重试计数。
    private var capabilitiesCharacteristic: CBCharacteristic?
    private var timeSyncRetries = 0

    // MARK: - [2026-10-02 今日待办] 显示终端通道
    //
    // 协议：复用同一个 capabilities 特征，按**载荷长度 + 首字节**区分三类语义
    // （完整说明见 PassportTodoSync.swift 顶部）：
    //   - 长度 == 4                      → 校时（小端 uint32 Unix 秒，现有功能，语义未改动）
    //   - 长度 >= 5 且首字节 0x02        → 待办帧 [0x02][index][total][status][title_len][title utf8...]
    //   - 长度 == 5 且首字节 0x03        → 连接状态帧 [0x03][state][count][age_lo][age_hi]
    // 写入类型统一 `.withResponse`，与校时一致。
    //
    // 这里只做"拉取 + 写特征"，**不会**因此发起扫描或重连，所以不会干扰音频/PTT 链路。
    /// 待办轮询间隔（秒）。2026-10-02 由 60 收紧到 15：老大改完 Life Logger 后希望
    /// 尽快在 AI Passport 上看到，而每轮只写 5-6 帧（相比 PTT 音频流可忽略），
    /// 代价只是多一点 BLE 写入；若将来要更即时，再上"设备翻到待办页时主动请求刷新"。
    private static let todoRefreshInterval: TimeInterval = 15
    private let todoFetcher = PassportTodoFetcher()
    /// [2026-10-13] 习惯拉取器 —— 与待办同构、共用同一轮 15 秒定时器，但走 `/api/habits`。
    /// 设备第 3 屏（HABITS 页）的数据就来自这里；帧是 0x04，与待办 0x02 在同一条特征上分流。
    private let habitFetcher = PassportHabitFetcher()
    /// 上一轮成功拉到的习惯。拉取失败时沿用这一份（理由同 lastGoodItems：失败不该清屏）。
    private var lastGoodHabits: [PassportHabitItem] = []
    private var todoRefreshTimer: DispatchSourceTimer?

    // MARK: - [2026-10-02 连接状态帧] 设备第 3 屏「Life Logger 连接状态」的数据源
    //
    // 设备够不着 Life Logger（无 WiFi、无地址、无令牌），状态只能由 Mac 转达。
    // 下面三个字段就是 Mac 侧"记得"的全部事实，全部只在主队列读写
    // （todoFetcher 的 completion 一律先 DispatchQueue.main.async 再碰它们）：
    /// 上次**成功**拉取的时刻；nil = 本进程内从没成功过
    private var lastTodoSuccessAt: Date?
    /// 上次已知的今日待办条数（成功时更新；失败帧沿用它，避免屏幕数字无故跳 0）
    private var lastKnownTodoCount = 0
    /// [2026-10-13 修复] 上一轮**成功**拉到的待办列表。
    /// 拉取失败时改发这一份，而不是空数组 —— 否则 sendTodoFrames 会先写一条
    /// `total=0` 的清空帧，把设备 4 个槽位抹白约 15 秒（实测 25 分钟出现 4 次，≈4%），
    /// 与「宁可显示旧数据，也不能让徽章因上游抖动而整片空白」的设计意图相反。
    /// 只在主队列读写（与上面两个字段同一约定）。
    private var lastGoodItems: [PassportTodoItem] = []
    /// 上次已知的连接状态（重连后立刻补发的那一帧沿用它，见 startTodoRefresh）
    private var lastKnownConnectionState: PassportConnectionState = .disconnected

    /// capabilities 特征上「已发出、等回执」的写入类型队列。
    ///
    /// 为什么需要：CoreBluetooth 对同一特征的写入按序回执，而 `didWriteValueFor` 是三类写入
    /// 共用的回调。若不区分，待办帧/状态帧写失败会被误当成校时写失败，进而用 **4 字节校时帧**去
    /// "重试"一条待办 —— 那既丢了待办，又在错误通道上写了时间。有了它，校时重试语义保持原样，
    /// 待办帧与状态帧失败只打日志（符合协议要求：不重试）。
    private enum CapabilityWriteKind {
        case timeSync
        case todoFrame
        /// [2026-10-02] 连接状态帧：与待办帧一样只打日志、**绝不**落进校时重试分支
        case connectionStatus
        /// [2026-10-13] 习惯帧（0x04）：与待办帧同策略 —— 只打日志、不重试。
        /// 下一轮 15 秒定时器会自然重拉，Mac 侧补写只会打乱 FIFO 回执与写入的对应关系。
        case habitFrame
    }
    private var capabilityWriteKinds: [CapabilityWriteKind] = []

    public init(keyDispatcher: PassportKeyDispatcher = PassportKeyDispatcher(),
                audioDecoder: PassportAudioDecoder = PassportAudioDecoder(),
                audioStreamPlayer: PassportAudioStreamPlayer = PassportAudioStreamPlayer()) {
        self.keyDispatcher = keyDispatcher
        self.audioDecoder = audioDecoder
        self.audioStreamPlayer = audioStreamPlayer
        super.init()
        self.centralManager = CBCentralManager(delegate: self, queue: .main)
    }

    public convenience init(virtualKeyCode: CGKeyCode, isKeyInjectionEnabled: Bool = true) {
        self.init(
            keyDispatcher: PassportKeyDispatcher(virtualKeyCode: virtualKeyCode, isEnabled: isKeyInjectionEnabled),
            audioDecoder: PassportAudioDecoder(),
            audioStreamPlayer: PassportAudioStreamPlayer()
        )
    }

    deinit {
        // [2026-10-02] 待办刷新定时器不能活过 bridge 本身：app 退出/对象释放后必须彻底失效。
        stopTodoRefreshTimer()
    }

    public func startScanning(serviceUUIDs: [CBUUID]? = nil) {
        guard centralManager.state == .poweredOn else { return }

        // Check if device is already connected by macOS bluetooth daemon
        let connected = centralManager.retrieveConnectedPeripherals(withServices: [Self.serviceUUID])
        if let first = connected.first {
            logLine("[AirPassport Bridge] Found already-connected target device: \"\(first.name ?? "Unknown")\" [\(first.identifier.uuidString)]")
            self.peripheral = first
            self.peripheral?.delegate = self
            state = .connecting
            centralManager.connect(first, options: nil)
            return
        }

        state = .scanning
        logLine("[AirPassport Bridge] Central scanning for AirPassport-PTT (serviceUUIDs: \(serviceUUIDs?.map(\.uuidString) ?? ["all (name-filtered)"]))...")
        centralManager.scanForPeripherals(
            withServices: serviceUUIDs,
            options: [CBCentralManagerScanOptionAllowDuplicatesKey: false]
        )
    }

    public func stop() {
        centralManager.stopScan()
        if let peripheral {
            centralManager.cancelPeripheralConnection(peripheral)
        }
        keyDispatcher.reset()
        audioDecoder.reset()
        audioStreamPlayer.stopSession()
        // [2026-10-02] 停止连接时同步停掉待办刷新（app 退出走的就是这条路径）。
        stopTodoRefreshTimer()
        state = .idle
    }

    // MARK: - Process Raw Packet

    public func processIncomingData(_ data: Data) {
        do {
            let frame = try PassportFrameParser.decode(data)
            logLine("[AirPassport Bridge] Incoming frame: \(frame.type) (seq=\(frame.sequence), ms=\(frame.monotonicMs))")
            switch frame.type {
            case .begin:
                audioDecoder.reset()
                audioStreamPlayer.startSession()
                keyDispatcher.handleKeyDown()
                delegate?.bridgeSessionDidBegin(self, sequence: frame.sequence, monotonicMs: frame.monotonicMs)
            case .audio:
                let pcm = try audioDecoder.decodeBlock(frame.payload)
                audioStreamPlayer.write(samples: pcm)
                delegate?.bridgeAudioDidReceive(self, pcmSamples: pcm, sequence: frame.sequence, monotonicMs: frame.monotonicMs)
            case .end:
                keyDispatcher.handleKeyUp()
                audioStreamPlayer.stopSession()
                delegate?.bridgeSessionDidEnd(self, sequence: frame.sequence, monotonicMs: frame.monotonicMs)
            case .enter:
                keyDispatcher.injectEnter()
                delegate?.bridgeDidTriggerEnter(self, monotonicMs: frame.monotonicMs)
            }
        } catch {
            logLine("[AirPassport Bridge] Malformed frame (\(data.count) bytes): \(error.localizedDescription)")
        }
    }

    // MARK: - CBCentralManagerDelegate

    public func centralManagerDidUpdateState(_ central: CBCentralManager) {
        let stateStr: String
        switch central.state {
        case .poweredOn: stateStr = "Powered On"
        case .poweredOff: stateStr = "Powered Off"
        case .unauthorized: stateStr = "Unauthorized"
        case .unsupported: stateStr = "Unsupported"
        case .resetting: stateStr = "Resetting"
        case .unknown: stateStr = "Unknown"
        @unknown default: stateStr = "State(\(central.state.rawValue))"
        }
        logLine("[AirPassport Bridge] Bluetooth Central State: \(stateStr)")

        if central.state == .poweredOn {
            if state == .scanning || state == .idle {
                startScanning()
            }
        } else {
            keyDispatcher.reset()
            audioDecoder.reset()
            state = .idle
        }
    }

    public func centralManager(_ central: CBCentralManager,
                               didDiscover peripheral: CBPeripheral,
                               advertisementData: [String: Any],
                               rssi RSSI: NSNumber) {
        let advertisedName = advertisementData[CBAdvertisementDataLocalNameKey] as? String
        let name = peripheral.name ?? advertisedName
        let advertisedUUIDs = (advertisementData[CBAdvertisementDataServiceUUIDsKey] as? [CBUUID]) ?? []

        let matchesName = name == Self.targetDeviceName || name?.contains("AirPassport") == true
        let matchesUUID = advertisedUUIDs.contains(Self.serviceUUID)

        guard matchesName || matchesUUID else {
            return
        }

        logLine("[AirPassport Bridge] Discovered target device: \"\(name ?? "Unknown")\" [\(peripheral.identifier.uuidString)], RSSI: \(RSSI)")
        self.peripheral = peripheral
        self.peripheral?.delegate = self
        central.stopScan()
        state = .connecting
        central.connect(peripheral, options: nil)
    }

    public func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        logLine("[AirPassport Bridge] Connected to \(peripheral.name ?? "AirPassport"). Discovering services...")
        state = .connected
        peripheral.discoverServices([Self.serviceUUID])
    }

    public func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        logLine("[AirPassport Bridge] Failed to connect: \(error?.localizedDescription ?? "unknown error")")
        state = .disconnected
        keyDispatcher.reset()
        startScanning()
    }

    public func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        logLine("[AirPassport Bridge] Disconnected from \(peripheral.name ?? "AirPassport"): \(error?.localizedDescription ?? "clean disconnect")")
        state = .disconnected
        self.peripheral = nil
        self.streamCharacteristic = nil
        // [2026-10-02] 断开后待办刷新必须失效（重连时会在 ready 分支重新拉取一次），
        // 未回执的写入也随之作废，清空类型队列避免把旧回执算到下一次写入头上。
        stopTodoRefreshTimer()
        capabilityWriteKinds.removeAll()
        keyDispatcher.reset()
        audioDecoder.reset()
        audioStreamPlayer.stopSession(immediate: true)
        startScanning()
    }

    // MARK: - CBPeripheralDelegate

    public func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        if let error {
            logLine("[AirPassport Bridge] Discover services error: \(error.localizedDescription)")
            return
        }
        guard let services = peripheral.services else { return }
        for service in services where service.uuid == Self.serviceUUID {
            logLine("[AirPassport Bridge] Discovered PTT Service: \(service.uuid)")
            peripheral.discoverCharacteristics([Self.streamUUID, Self.capabilitiesUUID], for: service)
        }
    }

    public func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        if let error {
            logLine("[AirPassport Bridge] Discover characteristics error: \(error.localizedDescription)")
            return
        }
        guard let characteristics = service.characteristics else { return }
        for characteristic in characteristics where characteristic.uuid == Self.streamUUID {
            self.streamCharacteristic = characteristic
            logLine("[AirPassport Bridge] Discovered Stream Characteristic, enabling Notify...")
            peripheral.setNotifyValue(true, for: characteristic)
        }
        for characteristic in characteristics where characteristic.uuid == Self.capabilitiesUUID {
            self.capabilitiesCharacteristic = characteristic
            logLine("[AirPassport Bridge] Reading Capabilities Characteristic: \(characteristic.uuid)")
            // [2026-10-01 诊断] 打印 CoreBluetooth 眼里这个特征的可写性：
            // write=false 说明本机 GATT 缓存仍是旧的（设备端固件已加 WRITE 权限）。
            logLine("[AirPassport Bridge] Capabilities properties: \(characteristic.properties) (write=\(characteristic.properties.contains(.write)))")
            peripheral.readValue(for: characteristic)
            // [2026-10-01 修正] 校时写入**必须等链路加密完成之后**：设备端 capabilities 的 WRITE
            // 权限带 _ENC（要求加密链路），而特征发现通常发生在 ENC_CHANGE 之前 —— 真机日志：
            // "BLE time sync write failed … Writing is not permitted"。
            // 所以这里只记下特征，真正的写入放到 didUpdateNotificationStateFor（Bridge ready）里。
        }
    }

    public func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            logLine("[AirPassport Bridge] Stream Notify update error: \(error.localizedDescription)")
            self.state = .disconnected
        } else if characteristic.isNotifying {
            logLine("[AirPassport Bridge] Stream Notify active: true. Bridge is ready!")
            self.state = .ready
            // Verify final capabilities status (confirm subscribed bit)
            if let capChar = peripheral.services?.flatMap({ $0.characteristics ?? [] }).first(where: { $0.uuid == Self.capabilitiesUUID }) {
                peripheral.readValue(for: capChar)
                // [2026-10-01 修正] 此刻链路已加密且订阅完成（设备端 ENC_CHANGE 早于此），
                // 才是写校时的正确时机；发现特征时立刻写会被设备端按 WRITE 权限拒绝。
                self.timeSyncRetries = 0
                syncTimeToDevice(peripheral, characteristic: capChar)
                // [2026-10-02 今日待办] 校时之后紧接着拉一次今日待办并下发，随后每 15 秒刷新一次
                // （今天待办会变）；每次重连也会重新走到这里，等价于"重连后重新拉取"。
                startTodoRefresh(for: peripheral, characteristic: capChar)
            }
        } else {
            logLine("[AirPassport Bridge] Stream Notify is NOT active")
        }
    }

    public func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            logLine("[AirPassport Bridge] Value update error for \(characteristic.uuid): \(error.localizedDescription)")
            return
        }
        if characteristic.uuid == Self.capabilitiesUUID, let val = characteristic.value {
            let hex = val.map { String(format: "%02X", $0) }.joined(separator: " ")
            logLine("[AirPassport Bridge] Capabilities raw value: [\(hex)]")
            if val.count >= 7 {
                let statusByte = val[6]
                let isEncrypted = (statusByte & 2) != 0
                let isSubscribed = (statusByte & 4) != 0
                let isReady = (statusByte & 8) != 0
                logLine("[AirPassport Bridge] Capabilities status: 0x\(String(format: "%02X", statusByte)) (connected: \(statusByte & 1 != 0), encrypted: \(isEncrypted), subscribed: \(isSubscribed), ready: \(isReady))")
                if let streamChar = self.streamCharacteristic, !streamChar.isNotifying {
                    logLine("[AirPassport Bridge] Subscribing to Stream Characteristic...")
                    peripheral.setNotifyValue(true, for: streamChar)
                }
            }
        }
        guard characteristic.uuid == Self.streamUUID, let value = characteristic.value else { return }
        processIncomingData(value)
    }

    public func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        // [2026-10-02] capabilities 特征现在承载三类写入（校时 4 字节 / 待办帧 >=5 字节 /
        // 连接状态帧 5 字节首字节 0x03），靠写入顺序 FIFO 判定这次回执属于哪一类；
        // 队列为空（理论上不该发生）按老行为当校时处理。
        let kind: CapabilityWriteKind = capabilityWriteKinds.isEmpty ? .timeSync : capabilityWriteKinds.removeFirst()
        if let error {
            switch kind {
            case .todoFrame:
                // 待办帧失败只打日志：**不重试**，也**不能**落进下面的校时重试分支
                // （否则会拿 4 字节校时帧去重写一条待办）。
                logLine("[AirPassport Bridge] Todo frame write failed for \(characteristic.uuid): \(error.localizedDescription)")
            case .connectionStatus:
                // [2026-10-02] 状态帧同理：只打日志、不重试，尤其不能被当成校时失败
                // 去补写 4 字节时间帧 —— 那会让设备第 3 屏的状态永远等不到更新。
                logLine("[AirPassport Bridge] Connection status frame write failed for \(characteristic.uuid): \(error.localizedDescription)")
            case .habitFrame:
                logLine("[AirPassport Bridge] Habit frame write failed for \(characteristic.uuid): \(error.localizedDescription)")
            case .timeSync:
                // 校时写失败只记录日志并重试：本通道与音频/PTT 链路完全独立，绝不能因此中断收音。
                logLine("[AirPassport Bridge] BLE time sync write failed for \(characteristic.uuid): \(error.localizedDescription)")
                // [2026-10-01] 链路刚就绪时偶发拒绝，最多补写 3 次、每次隔 2 秒。
                if self.timeSyncRetries < 3 {
                    self.timeSyncRetries += 1
                    let attempt = self.timeSyncRetries
                    logLine("[AirPassport Bridge] BLE time sync retry \(attempt)/3 in 2s")
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self, weak peripheral] in
                        guard let self, let peripheral else { return }
                        self.syncTimeToDevice(peripheral, characteristic: characteristic)
                    }
                }
            }
        } else {
            switch kind {
            case .todoFrame:
                logLine("[AirPassport Bridge] Todo frame write acknowledged by device (\(characteristic.uuid))")
            case .connectionStatus:
                logLine("[AirPassport Bridge] Connection status frame write acknowledged by device (\(characteristic.uuid))")
            case .habitFrame:
                logLine("[AirPassport Bridge] Habit frame write acknowledged by device (\(characteristic.uuid))")
            case .timeSync:
                self.timeSyncRetries = 0
                logLine("[AirPassport Bridge] BLE time sync write acknowledged by device (\(characteristic.uuid))")
            }
        }
    }

    // MARK: - BLE 校时写回（时间同步）

    /// 把 Mac 当前时间写入设备的 capabilities 特征（BLE 校时通道）。
    ///
    /// 用途：设备已改为只走 BLE、不启动 WiFi，原先依赖 SNTP 的校时通道失效，
    /// 主界面右上角时间因此一直显示 `--:--`；改由 Mac 端在每次连接成功后写回当前时间。
    ///
    /// 载荷格式：固定 4 字节，小端序（little-endian）uint32 = Unix 秒，
    /// 即 `UInt32(Date().timeIntervalSince1970)`；写入类型固定为 `.withResponse`。
    ///
    /// 说明：值域守卫（约 2024-01-01 ~ 2100 之间才接受）在设备端、由固件侧实现，
    /// Mac 端不重复校验；这里只保证写入的是当前时刻的正整数秒，不会写负数/异常值。
    private func syncTimeToDevice(_ peripheral: CBPeripheral, characteristic: CBCharacteristic) {
        let unixSeconds = UInt32(Date().timeIntervalSince1970)
        // 显式按小端序拆成 4 字节，避免依赖主机字节序。
        let payload = Data([
            UInt8(unixSeconds & 0xFF),
            UInt8((unixSeconds >> 8) & 0xFF),
            UInt8((unixSeconds >> 16) & 0xFF),
            UInt8((unixSeconds >> 24) & 0xFF)
        ])
        let hex = payload.map { String(format: "%02X", $0) }.joined(separator: " ")
        logLine("[AirPassport Bridge] BLE time sync: writing \(unixSeconds) (\(payload.count) bytes LE: [\(hex)]) to capabilities characteristic")
        // 记下这次写入的类型，供 didWriteValueFor 区分回执（见 CapabilityWriteKind 说明）。
        capabilityWriteKinds.append(.timeSync)
        peripheral.writeValue(payload, for: characteristic, type: .withResponse)
    }

    // MARK: - BLE 今日待办下发（设备只做「显示终端」，无勾选回传）
    //         + 连接状态帧（设备第 3 屏，Mac 转达 Life Logger 通不通）

    /// 链路就绪后启动待办刷新：**立刻补发一帧连接状态** + 立刻拉一次待办，之后每 15 秒一轮。
    ///
    /// 定时器跑在主队列（与 `CBCentralManager(queue: .main)` 同一条队列），
    /// 因此不存在跨队列访问 CoreBluetooth 的问题；它**只做 URLSession 拉取 + 写特征**，
    /// 不调用 startScanning/connect，所以不会造成 BLE 频繁重连。
    ///
    /// 为什么这里要"先补一帧旧状态"：重连瞬间还没有新一轮拉取结果，设备第 3 屏若一直空着，
    /// 用户会以为状态页坏了。先把它上次知道的事实写下去（state/count 沿用上次结果，
    /// age 按上次成功时刻**重新算**，所以时间照样在往前走），几秒内真结果回来再覆盖。
    /// 断连时则**不补发**：设备保留旧显示，等重连后由这里刷新（协议约定）。
    private func startTodoRefresh(for peripheral: CBPeripheral, characteristic: CBCharacteristic) {
        stopTodoRefreshTimer()

        // 1) 立刻补一帧"上次已知状态"（本进程从没成功过 → state=0、count=0、age 封顶）
        let cached = PassportConnectionStatus(state: lastKnownConnectionState,
                                              count: lastKnownTodoCount,
                                              ageSeconds: ageSecondsSinceLastSuccess())
        logLine("[AirPassport Bridge] Connection status push (on ready): state=\(cached.state.rawValue) count=\(cached.count) age=\(cached.ageSeconds)s")
        writeCapabilityFrame(cached.frame,
                             kind: .connectionStatus,
                             to: peripheral,
                             characteristic: characteristic,
                             delay: 0)

        // 2) 紧接着拉第一轮待办；结果回来时会在 refreshTodos 里再补一帧真状态
        refreshTodos(for: peripheral, characteristic: characteristic)
        // 习惯与待办共用这一轮定时器（同一条特征、不同首字节，设备侧分流）
        refreshHabits(for: peripheral, characteristic: characteristic)

        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + Self.todoRefreshInterval,
                       repeating: Self.todoRefreshInterval,
                       leeway: .seconds(5))
        timer.setEventHandler { [weak self, weak peripheral] in
            guard let self, let peripheral else { return }
            self.refreshTodos(for: peripheral, characteristic: characteristic)
            self.refreshHabits(for: peripheral, characteristic: characteristic)
        }
        todoRefreshTimer = timer
        timer.resume()
    }

    /// 让待办刷新定时器失效。可重复调用；断开连接、stop()、deinit 都会走这里。
    private func stopTodoRefreshTimer() {
        guard let timer = todoRefreshTimer else { return }
        timer.setEventHandler {}
        timer.cancel()
        todoRefreshTimer = nil
    }

    /// 距上次成功同步的秒数（封顶 65535）。
    ///
    /// 从没成功过时返回**封顶值**：协议没有"从未同步"的取值，65535 是唯一能表达
    /// "很久很久以前/不知道"的合法值（≈18.2 小时）。**这一点需要固件侧对齐**：
    /// 若设备想显示「从未同步」，请以 age == 65535 作为哨兵判断，别显示成"18 小时前"。
    /// 算法本体在 `PassportConnectionStatusProtocol.ageSeconds(since:now:)`（有单测覆盖）。
    private func ageSecondsSinceLastSuccess(now: Date = Date()) -> Int {
        PassportConnectionStatusProtocol.ageSeconds(since: lastTodoSuccessAt, now: now)
    }

    /// 拉取 Life Logger 今日待办并下发。
    ///
    /// 降级策略（需求要求，且必须保证音频/PTT 链路零影响）：
    /// 服务没开 / 超时 / 401 / JSON 解不动 → 一律下发 **一条 total=0 的清空帧**，
    /// 设备显示「暂无待办」，Mac 端只打日志，绝不抛出、绝不重试轰炸。
    ///
    /// [2026-10-02] 无论成功还是失败，本轮**结束时都再补一帧连接状态**
    /// （成功 state=1/count=本轮**真实**条数/age=0；失败 state=0/count=上次已知条数/age=距上次成功秒数）。
    /// count 是真实总数（可 > 设备槽位数 4），只有待办帧才截到 4 条。
    private func refreshTodos(for peripheral: CBPeripheral, characteristic: CBCharacteristic) {
        todoFetcher.fetch { [weak self, weak peripheral] result in
            // URLSession 的回调在后台队列：这里只打日志，状态与写入统一切回主队列
            // （CoreBluetooth 的回调队列），避免与 15 秒定时器那一侧抢着改状态。
            switch result {
            case .success(let fetched):
                logLine("[AirPassport Bridge] Todo fetch ok: \(fetched.count) 条未完成待办（设备只有 \(PassportTodoProtocol.maxItems) 个槽位，最多下发 \(PassportTodoProtocol.maxItems) 条；状态帧 count 报真实总数）")
            case .failure(let error):
                // 失败原因**不进协议帧**（协议没有 detail 字段），只留在这里的日志里，
                // 方便排查 401 / 连接被拒 / 超时到底是哪一种。
                logLine("[AirPassport Bridge] Todo fetch failed (\(error.localizedDescription)); sending clear frame + state=0 status frame")
            }
            DispatchQueue.main.async {
                guard let self, let peripheral else { return }
                // 连接已经换了/断了就别再往旧外设写。
                guard self.state == .ready, self.peripheral === peripheral else { return }

                let items: [PassportTodoItem]
                let status: PassportConnectionStatus
                switch result {
                case .success(let fetched):
                    // 待办帧只发前 4 条（设备只有 4 个槽位，由 pushPlan 兜底）；
                    // 状态帧 count 报**真实总数**，这样第 3 屏能显示「今日待办 8 条」，
                    // 用户知道列表外还有条目没显示（2026-10-02 真机暴露的体验缺口）。
                    items = fetched
                    self.lastGoodItems = fetched
                    self.lastKnownTodoCount = fetched.count
                    self.lastTodoSuccessAt = Date()
                    self.lastKnownConnectionState = .connected
                    status = PassportConnectionStatus(state: .connected,
                                                      count: fetched.count,
                                                      ageSeconds: 0)
                case .failure:
                    // [2026-10-13 修复] 这里原来是 `items = []`，会先写一条 total=0 的清空帧
                    // 把设备 4 个槽位抹白（实测 25 分钟 4 次，≈4%）。改成沿用上一轮成功的
                    // 列表：屏幕上的待办保持不动，只在状态帧上如实报告 state=disconnected，
                    // 让用户知道「服务连不上」而不是「待办没了」。
                    // 真拿到空列表时走 .success 分支，正常清空 —— 语义仍然准确。
                    items = self.lastGoodItems
                    self.lastKnownConnectionState = .disconnected
                    status = PassportConnectionStatus(state: .disconnected,
                                                      count: self.lastKnownTodoCount,
                                                      ageSeconds: self.ageSecondsSinceLastSuccess())
                }

                logLine("[AirPassport Bridge] Connection status push: state=\(status.state.rawValue) count=\(status.count) age=\(status.ageSeconds)s")
                self.sendTodoFrames(items, status: status, to: peripheral, characteristic: characteristic)
            }
        }
    }

    /// 逐帧写一轮下发：清空帧 + N 条待办帧 + 最后 1 条连接状态帧（顺序由 pushPlan 定，见其注释）。
    ///
    /// 待办帧格式（详见 PassportTodoSync.swift 顶部协议注释）：
    /// `[0x02][index][total][status][title_len][title utf8...]`
    ///
    /// 先写一条 `total = 0` 的清空帧，再逐条写实际条目 —— 这样设备侧列表**完全由本轮快照决定**，
    /// 不会残留上一轮的旧条目（例如从 4 条变成 2 条时，第 3/4 条必须消失）。
    /// 条与条之间间隔 ~60ms，避免写队列拥塞；每条失败只由 didWriteValueFor 打日志，不做回执聚合。
    private func sendTodoFrames(_ items: [PassportTodoItem],
                                status: PassportConnectionStatus,
                                to peripheral: CBPeripheral,
                                characteristic: CBCharacteristic) {
        // 设备最多显示 4 条：pushPlan 里再兜一次底，待办帧的 total 写实际发送条数。
        // 状态帧（plan.statusFrame）不受此限：它的 count 是真实总数，由调用方传入。
        let plan = PassportTodoProtocol.pushPlan(items: items, status: status)
        // 每帧各自记账：待办/清空帧是 .todoFrame，最后那条状态帧是 .connectionStatus。
        var writes: [(Data, CapabilityWriteKind)] = plan.todoFrames.map { ($0, .todoFrame) }
        writes.append((plan.statusFrame, .connectionStatus))

        logLine("[AirPassport Bridge] Todo push: \(plan.todoFrames.count - 1) 条 + 1 条清空帧 + 1 条连接状态帧，共 \(writes.count) 次写入，间隔 \(Int(PassportTodoProtocol.interFrameDelay * 1000))ms")

        for (offset, write) in writes.enumerated() {
            let delay = PassportTodoProtocol.interFrameDelay * Double(offset)
            let (payload, kind) = write
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self, weak peripheral] in
                guard let self, let peripheral else { return }
                guard self.state == .ready, self.peripheral === peripheral else { return }
                self.capabilityWriteKinds.append(kind)
                peripheral.writeValue(payload, for: characteristic, type: .withResponse)
            }
        }
    }

    /// 拉一轮习惯并下发 0x04 帧（设备第 3 屏 HABITS 页的数据源）。
    ///
    /// 与 refreshTodos 的关键差异：**失败时不清空**。
    /// 网络抖一下不代表「习惯没了」，屏幕上的打卡状态该稳住；
    /// 只有真拉到空列表（success + 空数组）才发清空帧 —— 语义仍然准确。
    private func refreshHabits(for peripheral: CBPeripheral, characteristic: CBCharacteristic) {
        logLine("[AirPassport Bridge] Habit refresh invoked")   // [临时探针]
        habitFetcher.fetch { [weak self, weak peripheral] result in
            switch result {
            case .success(let n): logLine("[AirPassport Bridge] Habit fetch returned ok: \(n.count)")
            case .failure(let e): logLine("[AirPassport Bridge] Habit fetch returned failure: \(e.localizedDescription)")
            }
            if case .failure(let error) = result {
                logLine("[AirPassport Bridge] Habit fetch failed (\(error.localizedDescription)); 保留上一轮习惯，不清屏")
            }
            DispatchQueue.main.async {
                guard let self, let peripheral else { return }
                guard self.state == .ready, self.peripheral === peripheral else { return }

                let items: [PassportHabitItem]
                switch result {
                case .success(let fetched):
                    items = fetched
                    self.lastGoodHabits = fetched
                    logLine("[AirPassport Bridge] Habit fetch ok: \(fetched.count) 条（设备只有 \(PassportHabitProtocol.maxItems) 个槽位）")
                case .failure:
                    items = self.lastGoodHabits
                }
                self.sendHabitFrames(items, to: peripheral, characteristic: characteristic)
            }
        }
    }

    /// 写 0x04 习惯帧：先清空、再逐条（格式见 PassportHabitSync.swift 顶部协议注释）。
    /// 逐帧用 writeCapabilityFrame 记账成 .habitFrame，失败只打日志、不重试。
    private func sendHabitFrames(_ items: [PassportHabitItem],
                                 to peripheral: CBPeripheral,
                                 characteristic: CBCharacteristic) {
        let plan = PassportHabitProtocol.pushPlan(items: items)
        logLine("[AirPassport Bridge] Habit push: \(plan.itemCount) 条 + 1 条清空帧，共 \(plan.habitFrames.count) 次写入，间隔 \(Int(PassportHabitProtocol.interFrameDelay * 1000))ms")
        for (offset, frame) in plan.habitFrames.enumerated() {
            writeCapabilityFrame(frame,
                                 kind: .habitFrame,
                                 to: peripheral,
                                 characteristic: characteristic,
                                 delay: PassportHabitProtocol.interFrameDelay * Double(offset))
        }
    }

    /// 写一帧到 capabilities 特征（带延迟），并记下它的写入类型供 didWriteValueFor 区分回执。
    ///
    /// 连接状态帧在"链路刚就绪"和"每轮拉取结束"两处都要写，抽出来避免两处各自记账写错 kind。
    private func writeCapabilityFrame(_ payload: Data,
                                      kind: CapabilityWriteKind,
                                      to peripheral: CBPeripheral,
                                      characteristic: CBCharacteristic,
                                      delay: TimeInterval) {
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self, weak peripheral] in
            guard let self, let peripheral else { return }
            guard self.state == .ready, self.peripheral === peripheral else { return }
            self.capabilityWriteKinds.append(kind)
            peripheral.writeValue(payload, for: characteristic, type: .withResponse)
        }
    }
}
