"""
SSVEP 频率响应扫描工具
- 逐频率施加刺激 (默认 8→80 Hz)，每个频率持续 T 秒
- 频率切换间用 0 刺激做组间休息 T0 秒
- 用 cv2.getTickCount 精确记录刺激时间戳
- 输出 JSON 供后续 EEG 分析
- 配置项见 scan_config.yaml
"""

import socket
import time
import json
import sys
from pathlib import Path
from datetime import datetime

import cv2
import yaml

# ---- 加载配置 ----
CONFIG_PATH = Path(__file__).parent / "scan_config.yaml"


def load_config():
    if not CONFIG_PATH.exists():
        print(f"[!] 配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def send_cmd(ip, port, cmd_str):
    """发送 UDP 命令到 ESP32 并等待响应"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    try:
        sock.sendto(cmd_str.encode(), (ip, port))
        data, _ = sock.recvfrom(64)
        return data.decode()
    except socket.timeout:
        return None
    finally:
        sock.close()


def set_channels_freq(ip, port, channels, hz):
    """设置多个通道到同一频率，全部成功返回 True"""
    for ch in channels:
        resp = send_cmd(ip, port, f"FREQ,{ch},{hz}")
        if resp is None or not resp.startswith("OK"):
            print(f"  [!] CH{ch} 设置 {hz} Hz 失败: {resp}")
            return False
    return True


def main():
    cfg = load_config()

    esp_ip = cfg["esp"]["ip"]
    esp_port = cfg["esp"]["port"]
    channels = cfg["scan"]["channels"]
    freq_start = cfg["scan"]["freq_start"]
    freq_stop = cfg["scan"]["freq_stop"]
    freq_step = cfg["scan"]["freq_step"]
    stim_duration = cfg["scan"]["stim_duration"]
    rest_duration = cfg["scan"]["rest_duration"]
    output = cfg.get("output")

    freqs = list(range(freq_start, freq_stop + 1, freq_step))
    if not freqs:
        print("[!] 频率列表为空，请检查 scan_config.yaml 中的频率设置")
        return

    if not output:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = f"ssvep_scan_{ts}.json"

    total_time = len(freqs) * stim_duration + (len(freqs) - 1) * rest_duration
    print("=" * 55)
    print("  SSVEP 频率响应扫描")
    print("=" * 55)
    print(f"  通道: {channels}")
    print(f"  频率: {freqs[0]}-{freqs[-1]} Hz, 步进 {freq_step} Hz, 共 {len(freqs)} 个")
    print(f"  刺激: {stim_duration}s / 休息: {rest_duration}s")
    print(f"  预估总时间: {total_time:.0f}s ({total_time/60:.1f} min)")
    print(f"  输出: {output}")
    print(f"  ESP32: {esp_ip}:{esp_port}")
    print()

    # 连通性测试
    print("[*] 测试 ESP32 连接...")
    resp = send_cmd(esp_ip, esp_port, "FREQ,1,0")
    if resp is None:
        print("[!] ESP32 无响应，请检查网络连接")
        return
    print(f"[+] ESP32 连接正常 (响应: {resp})")
    print()

    input("按 Enter 开始扫描...")
    print()

    tick_freq = cv2.getTickFrequency()
    scan_start_tick = cv2.getTickCount()
    trials = []

    for i, f in enumerate(freqs):
        # 设置刺激频率
        ok = set_channels_freq(esp_ip, esp_port, channels, f)
        stim_start_tick = cv2.getTickCount()

        if not ok:
            print(f"  [!] 跳过 {f} Hz (配置失败)")
            continue

        # 刺激持续
        time.sleep(stim_duration)
        stim_end_tick = cv2.getTickCount()

        # 关闭刺激
        set_channels_freq(esp_ip, esp_port, channels, 0)
        rest_start_tick = cv2.getTickCount()

        trials.append({
            "index": i,
            "freq_hz": f,
            "stim_start_tick": int(stim_start_tick),
            "stim_end_tick": int(stim_end_tick),
            "rest_start_tick": int(rest_start_tick),
        })

        elapsed = (cv2.getTickCount() - scan_start_tick) / tick_freq
        print(f"  [{i+1}/{len(freqs)}] {f} Hz done  ({elapsed:.1f}s elapsed)")

        # 组间休息（最后一个不休息）
        if i < len(freqs) - 1:
            time.sleep(rest_duration)

    scan_end_tick = cv2.getTickCount()
    total_elapsed = (scan_end_tick - scan_start_tick) / tick_freq

    print()
    print(f"[+] 扫描完成! 总耗时 {total_elapsed:.1f}s, {len(trials)} 个有效试次")

    # 保存 JSON
    result = {
        "config": {
            "channels": channels,
            "freq_start": freq_start,
            "freq_stop": freq_stop,
            "freq_step": freq_step,
            "stim_duration_s": stim_duration,
            "rest_duration_s": rest_duration,
            "esp_ip": esp_ip,
            "tick_frequency": tick_freq,
        },
        "scan_start_tick": int(scan_start_tick),
        "scan_end_tick": int(scan_end_tick),
        "trials": trials,
    }

    with open(output, "w", encoding="utf-8") as fp:
        json.dump(result, fp, indent=2, ensure_ascii=False)

    print(f"[+] 结果已保存: {output}")


if __name__ == "__main__":
    main()
