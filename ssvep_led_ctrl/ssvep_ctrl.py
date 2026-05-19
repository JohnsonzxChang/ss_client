"""
SSVEP 信号发生器 PC 上位机
- 自动发现 ESP32 (监听 HELLO 广播)
- 首次连接自动 PC↔ESP 时间对齐 (SYNC 协议, NTP 风格多轮取最小 RTT)
- 交互式命令: freq / evoke / all / sync / status / help / quit
- 所有 OK 响应均携带 ESP us 时间戳, PC 端可换算回本地 monotonic_us
- 协议详见项目根目录 CLAUDE.md
"""

import socket
import threading
import time

LISTEN_PORT = 5006   # 接收 ESP32 HELLO 广播
ESP_CMD_PORT = 5005  # 发送命令到 ESP32

# ---- 运行时状态 ----
esp_ip = None
channel_freq = [0] * 8
time_offset_us = 0       # ESP_us ≈ PC_us + time_offset_us
sync_rtt_us = None       # 最小 RTT, 用于评估同步质量
sync_done = threading.Event()


def now_pc_us():
    """PC monotonic 时钟, 微秒"""
    return time.monotonic_ns() // 1000


def esp_to_pc_us(esp_us):
    """ESP 时间戳 -> PC monotonic 时间戳 (同 now_pc_us 量纲)"""
    return esp_us - time_offset_us


# ---- 自动发现 + 首次时间对齐 ----
def wait_for_esp32():
    """监听 ESP32 上线广播; 每次新设备上线自动跑一次 SYNC"""
    global esp_ip, channel_freq
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", LISTEN_PORT))
    print(f"[*] 等待 ESP32 上线 (监听端口 {LISTEN_PORT}) ...")

    while True:
        data, _addr = sock.recvfrom(256)
        msg = data.decode(errors="ignore")
        if not msg.startswith("HELLO,SSVEP,"):
            continue
        parts = msg.split(",")
        try:
            new_ip = parts[2]
            new_freqs = [int(x) for x in parts[4:12]]
        except (IndexError, ValueError):
            continue

        if esp_ip != new_ip:
            esp_ip = new_ip
            channel_freq = new_freqs
            print(f"\n[+] ESP32 上线: {esp_ip}")
            print(f"    当前频率: {channel_freq}")
            print(f"    控制端口: {ESP_CMD_PORT}")
            result = do_sync(rounds=20)
            if result is not None:
                offset_us, rtt_us = result
                print(f"[+] 时间对齐完成: offset={offset_us} us, 最小 RTT={rtt_us} us")
                sync_done.set()
            else:
                print("[!] 时间对齐失败 (ESP 无 SYNC 响应)")
            show_help()
        else:
            channel_freq = new_freqs  # 同一台设备, 刷新频率


# ---- 命令收发 ----
def send_cmd(cmd_str, timeout=2.0):
    """发送一个 UDP 命令并等待单次响应"""
    if esp_ip is None:
        print("[!] ESP32 尚未上线, 请等待...")
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(cmd_str.encode(), (esp_ip, ESP_CMD_PORT))
        data, _ = sock.recvfrom(128)
        return data.decode(errors="ignore")
    except socket.timeout:
        print("[!] 超时, ESP32 无响应")
        return None
    finally:
        sock.close()


# ---- 时间对齐 (NTP 风格) ----
def do_sync(rounds=20, timeout=1.0):
    """
    多轮 SYNC, 保留最小 RTT 的样本作为 offset 估计.
    offset = ((esp_recv_us - pc_send_us) + (esp_send_us - pc_recv_us)) / 2
    rtt    = (pc_recv_us - pc_send_us) - (esp_send_us - esp_recv_us)
    返回 (offset_us, min_rtt_us) 或 None.
    """
    global time_offset_us, sync_rtt_us
    if esp_ip is None:
        return None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    best_rtt = float("inf")
    best_offset = 0
    successes = 0

    try:
        for _ in range(rounds):
            pc_send_us = now_pc_us()
            try:
                sock.sendto(f"SYNC,{pc_send_us}".encode(), (esp_ip, ESP_CMD_PORT))
                data, _ = sock.recvfrom(128)
                pc_recv_us = now_pc_us()
            except socket.timeout:
                continue
            resp = data.decode(errors="ignore")
            if not resp.startswith("OK,SYNC,"):
                continue
            parts = resp.split(",")
            if len(parts) != 5:
                continue
            try:
                esp_recv_us = int(parts[3])
                esp_send_us = int(parts[4])
            except ValueError:
                continue
            rtt = (pc_recv_us - pc_send_us) - (esp_send_us - esp_recv_us)
            offset = ((esp_recv_us - pc_send_us) + (esp_send_us - pc_recv_us)) // 2
            if rtt < best_rtt:
                best_rtt = rtt
                best_offset = offset
            successes += 1
            time.sleep(0.005)
    finally:
        sock.close()

    if successes == 0:
        return None
    time_offset_us = best_offset
    sync_rtt_us = best_rtt
    return best_offset, best_rtt


# ---- 命令实现 ----
def set_freq(ch, hz):
    """FREQ,ch,hz -> OK,FREQ,<ch>,<hz>,<onset_us>"""
    resp = send_cmd(f"FREQ,{ch},{hz}")
    if not resp:
        return
    if resp.startswith("OK,FREQ,"):
        parts = resp.split(",")
        try:
            onset_esp = int(parts[4])
            channel_freq[ch - 1] = hz
            label = "关闭" if hz == 0 else f"{hz} Hz"
            print(f"[OK] CH{ch} -> {label}  | onset_esp={onset_esp} us  pc≈{esp_to_pc_us(onset_esp)} us")
            return
        except (IndexError, ValueError):
            pass
    if resp.startswith("OK"):
        # 兼容老固件 (无 onset 时间戳)
        channel_freq[ch - 1] = hz
        print(f"[OK] CH{ch} -> {hz} Hz  (老固件响应, 缺时间戳)")
    else:
        print(f"[ERR] {resp}")


def evoke(ch, ms):
    """EVOKE,ch,ms -> OK,EVOKE,<ch>,<ms>,<onset_us>; ms 到时 ESP 自动关闭通道"""
    resp = send_cmd(f"EVOKE,{ch},{ms}")
    if not resp:
        return
    if resp.startswith("OK,EVOKE,"):
        parts = resp.split(",")
        try:
            onset_esp = int(parts[4])
            offset_esp = onset_esp + ms * 1000  # ESP 在 onset+ms 自动关闭
            print(f"[OK] EVOKE CH{ch} {ms} ms")
            print(f"     onset_esp={onset_esp} us  pc≈{esp_to_pc_us(onset_esp)} us")
            print(f"     offset_esp≈{offset_esp} us  pc≈{esp_to_pc_us(offset_esp)} us")
            return
        except (IndexError, ValueError):
            pass
    print(f"[ERR] {resp}")


def show_status():
    if esp_ip is None:
        print("[*] ESP32 尚未连接")
    else:
        print(f"[*] ESP32 IP: {esp_ip}")
    print(f"[*] 时间对齐: offset={time_offset_us} us, 最小 RTT={sync_rtt_us} us")
    print(f"[*] 通道频率 (GPIO):")
    pins = [19, 23, 18, 21, 27, 13, 14, 4]
    for i in range(8):
        print(f"    CH{i+1} (GPIO {pins[i]:>2}): {channel_freq[i]} Hz")


def manual_sync():
    print("[*] 重新进行 PC<->ESP 时间对齐...")
    result = do_sync(rounds=20)
    if result is None:
        print("[!] 同步失败")
    else:
        offset_us, rtt_us = result
        print(f"[+] offset={offset_us} us, 最小 RTT={rtt_us} us")


def show_help():
    print("命令:")
    print("  freq <ch> <hz>     - 设置通道连续 PWM (ch:1-8, hz:0-100, 0=关闭)")
    print("  evoke <ch> <ms>    - 单次刺激 (ch:1-8, ms:1-10000, 使用当前频率)")
    print("  all <f1> ... <f8>  - 同时设置 8 通道频率")
    print("  sync               - 重新进行 PC<->ESP 时间对齐")
    print("  status             - 显示连接 / 偏移 / 当前频率")
    print("  help               - 显示帮助")
    print("  quit               - 退出")
    print()


def main():
    t = threading.Thread(target=wait_for_esp32, daemon=True)
    t.start()

    print("=" * 50)
    print("  SSVEP 信号发生器控制台")
    print("=" * 50)
    print()

    while True:
        try:
            line = input("ssvep> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            break
        elif cmd in ("help", "h"):
            show_help()
        elif cmd in ("status", "s"):
            show_status()
        elif cmd == "sync":
            manual_sync()
        elif cmd in ("freq", "f"):
            if len(parts) != 3:
                print("用法: freq <通道 1-8> <频率 0-100>")
                continue
            try:
                ch, hz = int(parts[1]), int(parts[2])
            except ValueError:
                print("参数必须是整数")
                continue
            if ch < 1 or ch > 8:
                print("通道范围: 1-8")
            elif hz < 0 or hz > 100:
                print("频率范围: 0-100 Hz (0=关闭)")
            else:
                set_freq(ch, hz)
        elif cmd in ("evoke", "e"):
            if len(parts) != 3:
                print("用法: evoke <通道 1-8> <duration 1-10000 ms>")
                continue
            try:
                ch, ms = int(parts[1]), int(parts[2])
            except ValueError:
                print("参数必须是整数")
                continue
            if ch < 1 or ch > 8:
                print("通道范围: 1-8")
            elif ms < 1 or ms > 10000:
                print("duration 范围: 1-10000 ms")
            else:
                evoke(ch, ms)
        elif cmd in ("all", "a"):
            if len(parts) != 9:
                print("用法: all <f1> <f2> ... <f8>")
                continue
            try:
                freqs = [int(x) for x in parts[1:9]]
            except ValueError:
                print("参数必须是整数")
                continue
            if any(f < 0 or f > 100 for f in freqs):
                print("频率范围: 0-100 Hz")
                continue
            for i, hz in enumerate(freqs):
                set_freq(i + 1, hz)
        else:
            print(f"未知命令: {cmd}, 输入 help 查看帮助")


if __name__ == "__main__":
    main()
