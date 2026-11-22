"""
AirPassport Windows 跨平台接收器 (AirPassport for Windows)
=====================================================
功能：
1. 监听并连接 AirPassport-PTT 硬件卡片 (BLE GATT)
2. 支持【立即绑定 / 接管】与【释放 / 断开】按钮，完美支持多电脑切换
3. 顶键 UP 按下：触发全局 Alt / Option 或 Windows 语音听写 (Win+H)，并接收 16kHz 音频
4. 底键 DOWN 单击：触发 Windows 回车 (Enter / Return: VK_RETURN 0x0D)
5. 自动记忆已绑定设备的 MAC 地址，防止串连他人设备
"""

import asyncio
import os
import sys
import time
import json
import wave
import struct
import threading
from typing import Optional

# 尝试导入 Windows 按键模拟库与 BLE 库
try:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    IS_WINDOWS = True
except Exception:
    IS_WINDOWS = False

try:
    from bleak import BleakScanner, BleakClient
    from bleak.backends.characteristic import BleakGATTCharacteristic
except ImportError:
    print("[错误] 未安装 bleak 库，请先执行: pip install bleak")
    BleakScanner = None
    BleakClient = None

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False

def find_windows_virtual_audio():
    if not HAS_SOUNDDEVICE:
        return None
    try:
        devices = sd.query_devices()
        for idx, dev in enumerate(devices):
            name = dev.get("name", "").lower()
            if dev.get("max_output_channels", 0) > 0:
                if "cable input" in name or "vb-audio" in name or "virtual" in name:
                    return (idx, dev["name"])
    except Exception:
        pass
    return None

# UUID 定义（与 ESP32 固件保持 100% 一致）
SERVICE_UUID = "01000054-5450-42a1-6f4e-2d9100107a8b"
STREAM_UUID = "01000054-5450-42a1-6f4e-2d9101107a8b"
CAPABILITIES_UUID = "01000054-5450-42a1-6f4e-2d9102107a8b"
TARGET_NAME = "AirPassport-PTT"

CONFIG_FILE = "air_passport_config.json"

# Windows 虚拟键码
VK_MENU = 0x12       # Alt 键 (类似 Mac 的 Option 键)
VK_RETURN = 0x0D     # 回车键
KEYEVENTF_KEYUP = 0x0002

def win_key_down(vk_code: int):
    if IS_WINDOWS:
        user32.keybd_event(vk_code, 0, 0, 0)

def win_key_up(vk_code: int):
    if IS_WINDOWS:
        user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)

def win_press_key(vk_code: int):
    win_key_down(vk_code)
    time.sleep(0.02)
    win_key_up(vk_code)

# IMA-ADPCM 解码器
STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767
]

INDEX_TABLE = [
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8
]

class ADPCMDecoder:
    def __init__(self):
        self.predictor = 0
        self.step_index = 0

    def reset(self):
        self.predictor = 0
        self.step_index = 0

    def decode_sample(self, nibble: int) -> int:
        step = STEP_TABLE[self.step_index]
        diff = step >> 3
        if nibble & 1: diff += step >> 2
        if nibble & 2: diff += step >> 1
        if nibble & 4: diff += step
        if nibble & 8: diff = -diff
        self.predictor += diff
        self.predictor = max(-32768, min(32767, self.predictor))
        self.step_index += INDEX_TABLE[nibble & 0x07]
        self.step_index = max(0, min(88, self.step_index))
        return self.predictor

    def decode_block(self, payload: bytes) -> bytes:
        if len(payload) < 4:
            return b""
        self.predictor = struct.unpack("<h", payload[0:2])[0]
        self.step_index = max(0, min(88, payload[2]))
        samples = [self.predictor]
        for b in payload[4:]:
            samples.append(self.decode_sample(b & 0x0F))
            samples.append(self.decode_sample((b >> 4) & 0x0F))
        return struct.pack(f"<{len(samples)}h", *samples)


class AirPassportController:
    def __init__(self, on_status_change=None, on_log=None):
        self.on_status_change = on_status_change
        self.on_log = on_log
        self.client: Optional[BleakClient] = None
        self.bound_address: Optional[str] = self.load_bound_address()
        self.is_running = False
        self.is_connected = False
        self.is_held = False
        self.decoder = ADPCMDecoder()
        self.audio_samples = bytearray()
        self.virtual_device = find_windows_virtual_audio()
        self.stream_out = None
        self.enable_virtual_mic = True
        self.loop = None
        self.thread = None

    def log(self, text: str):
        print(f"[AirPassport] {text}")
        if self.on_log:
            self.on_log(text)

    def set_status(self, status: str):
        if self.on_status_change:
            self.on_status_change(status)

    def load_bound_address(self) -> Optional[str]:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("bound_address")
            except Exception:
                pass
        return None

    def save_bound_address(self, address: str):
        self.bound_address = address
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"bound_address": address}, f, indent=2)
        except Exception:
            pass

    def clear_bound_address(self):
        self.bound_address = None
        if os.path.exists(CONFIG_FILE):
            try:
                os.remove(CONFIG_FILE)
            except Exception:
                pass
        self.log("已清除绑定地址记录")

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self.thread.start()

    def _run_event_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main_worker())

    async def _main_worker(self):
        while self.is_running:
            if not self.is_connected:
                self.set_status("🔴 未连接")
            await asyncio.sleep(0.5)

    def request_bind_and_connect(self):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.bind_and_connect_async(), self.loop)

    def request_disconnect(self):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.disconnect_async(), self.loop)

    async def bind_and_connect_async(self):
        if self.client and self.client.is_connected:
            self.log("设备已处于连接状态")
            return

        self.set_status("🟡 正在搜索 AirPassport...")
        self.log("正在搜索附近的 AirPassport-PTT 设备...")
        
        target_device = None
        devices = await BleakScanner.discover(timeout=5.0)
        for d in devices:
            if d.name and (TARGET_NAME in d.name or "AirPassport" in d.name):
                target_device = d
                break

        if not target_device:
            self.log("❌ 未搜索到 AirPassport，请确认硬件已开机且在附近")
            self.set_status("🔴 搜索超时 (未发现设备)")
            return

        self.log(f"发现设备: {target_device.name} [{target_device.address}]")
        self.save_bound_address(target_device.address)
        self.set_status("🟡 正在连接并协商加密...")

        try:
            self.client = BleakClient(target_device.address, disconnected_callback=self._on_disconnected)
            await self.client.connect()
            self.log("BLE 物理链路已连接，正在发现服务...")
            await self.client.start_notify(STREAM_UUID, self._on_stream_notify)
            self.is_connected = True
            self.set_status(f"🟢 已连接就绪 [{target_device.address[-8:]}]")
            self.log("🟢 [READY] AirPassport 已成功连接并绑定到本台 Windows 电脑！")
        except Exception as e:
            self.log(f"连接失败: {e}")
            self.set_status("🔴 连接失败")
            self.is_connected = False

    def _on_disconnected(self, client):
        self.is_connected = False
        if self.is_held:
            self.is_held = False
            win_key_up(VK_MENU)
        self.set_status("🔴 蓝牙连接已断开")
        self.log("⚠️ AirPassport 已断开连接 (可能被另一台电脑接管或关机)")

    async def disconnect_async(self):
        if self.client and self.client.is_connected:
            self.log("正在主动断开连接，释放给其它电脑...")
            await self.client.disconnect()
            self.client = None
        self.is_connected = False
        self.set_status("🔴 已释放 (等待连接)")

    def _on_stream_notify(self, sender: BleakGATTCharacteristic, data: bytearray):
        if len(data) < 8:
            return
        version = data[0]
        frame_type = data[1]
        seq = data[2] | (data[3] << 8)
        monotonic_ms = data[4] | (data[5] << 8) | (data[6] << 16) | (data[7] << 24)
        payload = bytes(data[8:])

        if frame_type == 1:  # BEGIN (顶键按下)
            self.is_held = True
            self.audio_samples.clear()
            self.decoder.reset()
            self.log(f"🚨 【上键按下】启动录音，模拟 Alt 键 Down (seq={seq})")
            win_key_down(VK_MENU)
            if self.enable_virtual_mic and HAS_SOUNDDEVICE and self.virtual_device:
                try:
                    self.stream_out = sd.RawOutputStream(
                        samplerate=16000,
                        channels=1,
                        dtype='int16',
                        device=self.virtual_device[0]
                    )
                    self.stream_out.start()
                except Exception as e:
                    self.log(f"   ⚠️ 启动虚拟声卡输出失败: {e}")
                    self.stream_out = None

        elif frame_type == 2:  # AUDIO (音频推流)
            pcm = self.decoder.decode_block(payload)
            if pcm:
                self.audio_samples.extend(pcm)
                if self.stream_out:
                    try:
                        self.stream_out.write(pcm)
                    except Exception:
                        pass

        elif frame_type == 3:  # END (顶键松开)
            if self.is_held:
                self.is_held = False
                win_key_up(VK_MENU)
            if self.stream_out:
                try:
                    self.stream_out.stop()
                    self.stream_out.close()
                except Exception:
                    pass
                self.stream_out = None
            duration = len(self.audio_samples) / (16000 * 2)
            self.log(f"🟢 【上键松开】停止录音，释放 Alt 键，录制时长: {duration:.2f}s")
            self._save_wav()

        elif frame_type == 4:  # ENTER (底键单击)
            self.log("↵ 【下键单击】模拟触发回车键 (Enter / 发送)！")
            win_press_key(VK_RETURN)

    def _save_wav(self):
        if not self.audio_samples:
            return
        os.makedirs("recordings_windows", exist_ok=True)
        filename = f"recordings_windows/ptt_{int(time.time())}.wav"
        try:
            with wave.open(filename, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(self.audio_samples)
            self.log(f"   • 音频已保存: {filename}")
        except Exception as e:
            self.log(f"   • 保存音频失败: {e}")


def launch_gui():
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title("AirPassport 跨平台接收器 (Windows)")
    root.geometry("520x420")
    root.minsize(460, 360)

    # 状态栏
    status_var = tk.StringVar(value="🔴 未连接")
    lbl_status = tk.Label(root, textvariable=status_var, font=("Microsoft YaHei", 12, "bold"), pady=8)
    lbl_status.pack()

    # 按钮栏
    btn_frame = tk.Frame(root, pady=6)
    btn_frame.pack()

    controller = AirPassportController()

    def on_status_change(text):
        root.after(0, lambda: status_var.set(text))

    def on_log(text):
        def _append():
            txt_log.configure(state='normal')
            txt_log.insert(tk.END, text + "\n")
            txt_log.see(tk.END)
            txt_log.configure(state='disabled')
        root.after(0, _append)

    controller.on_status_change = on_status_change
    controller.on_log = on_log

    btn_bind = tk.Button(btn_frame, text="⚡ 立即绑定 / 接管连接", bg="#0078D7", fg="white",
                         font=("Microsoft YaHei", 10, "bold"), padx=12, pady=6,
                         command=controller.request_bind_and_connect)
    btn_bind.grid(row=0, column=0, padx=8)

    btn_release = tk.Button(btn_frame, text="⏸️ 释放 / 断开当前连接", font=("Microsoft YaHei", 10),
                            padx=10, pady=6, command=controller.request_disconnect)
    btn_release.grid(row=0, column=1, padx=8)

    btn_clear = tk.Button(btn_frame, text="🔄 清除绑定", font=("Microsoft YaHei", 9),
                           padx=6, pady=6, command=controller.clear_bound_address)
    btn_clear.grid(row=0, column=2, padx=8)

    # 虚拟声卡状态栏 (针对无麦克风台式机)
    vmic_frame = tk.Frame(root, pady=3)
    vmic_frame.pack(fill="x", padx=14)
    if controller.virtual_device:
        lbl_vmic = tk.Label(vmic_frame, text=f"🎙️ 虚拟麦克风: {controller.virtual_device[1]} (推流就绪)", font=("Microsoft YaHei", 9), fg="#2E7D32")
    else:
        lbl_vmic = tk.Label(vmic_frame, text="⚠️ 虚拟麦克风: 未检测到 VB-Cable 驱动 (台式机可装: vb-audio.com/Cable)", font=("Microsoft YaHei", 9), fg="#D84315")
    lbl_vmic.pack(side="left")

    # 日志输出框
    lbl_log = tk.Label(root, text="实时通信日志：", font=("Microsoft YaHei", 9), anchor="w")
    lbl_log.pack(fill="x", padx=14, pady=(8, 2))

    txt_log = scrolledtext.ScrolledText(root, wrap=tk.WORD, font=("Consolas", 9), bg="#1E1E1E", fg="#D4D4D4")
    txt_log.pack(fill="both", expand=True, padx=14, pady=(0, 14))
    txt_log.configure(state='disabled')

    controller.start()
    controller.log("AirPassport 控制器就绪，点击上方【立即绑定 / 接管连接】即可连入。")

    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        c = AirPassportController()
        c.start()
        c.request_bind_and_connect()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            c.request_disconnect()
    else:
        launch_gui()
