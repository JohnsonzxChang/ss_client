"""
SSVEP 信号发生器 PC 上位机
- 自动发现 ESP32（监听广播招呼包）
- 交互式命令行控制频率
"""

import socket
import threading
import sys

LISTEN_PORT = 5006   # 接收 ESP32 广播招呼
ESP_CMD_PORT = 5005  # 发送命令到 ESP32

esp_ip = None
channel_freq = [0] * 8


def wait_for_esp32():
    """监听 ESP32 的上线广播"""
    global esp_ip, channel_freq
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", LISTEN_PORT))
    print(f"[*] 等待 ESP32 上线 (监听端口 {LISTEN_PORT}) ...")

    while True:
        data, addr = sock.recvfrom(256)
        msg = data.decode()
        # 格式: HELLO,SSVEP,<ip>,<port>,<f1>,<f2>,...,<f8>
        if msg.startswith("HELLO,SSVEP,"):
            parts = msg.split(",")
            esp_ip = parts[2]
            channel_freq = [int(x) for x in parts[4:12]]
            print(f"\n[+] ESP32 上线: {esp_ip}")
            print(f"    当前频率: {channel_freq}")
            print(f"    控制端口: {ESP_CMD_PORT}")
            print()
            show_help()


def send_cmd(cmd_str):
    """发送 UDP 命令到 ESP32 并等待响应"""
    if esp_ip is None:
        print("[!] ESP32 尚未上线，请等待...")
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    try:
        sock.sendto(cmd_str.encode(), (esp_ip, ESP_CMD_PORT))
        data, _ = sock.recvfrom(64)
        return data.decode()
    except socket.timeout:
        print("[!] 超时，ESP32 无响应")
        return None
    finally:
        sock.close()


def set_freq(ch, hz):
    """设置单通道频率"""
    resp = send_cmd(f"FREQ,{ch},{hz}")
    if resp and resp.startswith("OK"):
        channel_freq[ch - 1] = hz
        if hz == 0:
            print(f"[OK] 通道 {ch} -> 关闭")
        else:
            print(f"[OK] 通道 {ch} -> {hz} Hz")
    elif resp:
        print(f"[ERR] {resp}")


def show_status():
    """显示当前状态"""
    if esp_ip is None:
        print("[*] ESP32 尚未连接")
    else:
        print(f"[*] ESP32 IP: {esp_ip}")
    print(f"[*] 通道频率:")
    for i in range(8):
        print(f"    CH{i+1} (GPIO {[19,23,18,21,27,13,14,4][i]}): {channel_freq[i]} Hz")


def show_help():
    print("命令:")
    print("  freq <ch> <hz>   - 设置通道频率 (ch: 1-8, hz: 0-100, 0=关闭)")
    print("  all <f1> ... <f8> - 设置全部 8 通道频率 (0=关闭)")
    print("  status           - 显示当前状态")
    print("  help             - 显示帮助")
    print("  quit             - 退出")
    print()


def main():
    # 后台线程监听 ESP32 广播
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

        if cmd == "quit" or cmd == "exit" or cmd == "q":
            break
        elif cmd == "help" or cmd == "h":
            show_help()
        elif cmd == "status" or cmd == "s":
            show_status()
        elif cmd == "freq" or cmd == "f":
            if len(parts) != 3:
                print("用法: freq <通道 1-8> <频率 Hz>")
                continue
            try:
                ch, hz = int(parts[1]), int(parts[2])
                if ch < 1 or ch > 8:
                    print("通道范围: 1-8")
                elif hz < 0 or hz > 100:
                    print("频率范围: 0-100 Hz (0=关闭)")
                else:
                    set_freq(ch, hz)
            except ValueError:
                print("参数必须是整数")
        elif cmd == "all" or cmd == "a":
            if len(parts) != 9:
                print("用法: all <f1> <f2> <f3> <f4> <f5> <f6> <f7> <f8>")
                continue
            try:
                freqs = [int(x) for x in parts[1:9]]
                if any(f < 0 or f > 100 for f in freqs):
                    print("频率范围: 0-100 Hz (0=关闭)")
                    continue
                for i, hz in enumerate(freqs):
                    set_freq(i + 1, hz)
            except ValueError:
                print("参数必须是整数")
        else:
            print(f"未知命令: {cmd}，输入 help 查看帮助")


if __name__ == "__main__":
    main()
