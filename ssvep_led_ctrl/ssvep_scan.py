"""
SSVEP 频率响应扫描工具
- 逐频率施加刺激 (默认 8→80 Hz), 每个频率持续 T 秒
- 频率切换间用 0 刺激做组间休息 T0 秒
- 启动时进行 PC↔ESP 时间对齐 (SYNC); 每个 trial 同时记录:
    * cv2.getTickCount() — PC 调度时间戳 (含 UDP 往返抖动)
    * esp_onset_us       — ESP 端 LEDC 实际写入时刻, 经 offset 校正后即真值
- 输出 JSON 供后续 EEG 分析
- 配置项见 scan_config.yaml
- 协议详见项目根目录 CLAUDE.md
"""

import socket
import time
import json
import sys
from pathlib import Path
from datetime import datetime

import cv2
import yaml

CONFIG_PATH = Path(__file__).parent / "scan_config.yaml"


def load_config():
    if not CONFIG_PATH.exists():
        print(f"[!] 配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def now_pc_us():
    return time.monotonic_ns() // 1000


def send_cmd(ip, port, cmd_str, timeout=2.0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(cmd_str.encode(), (ip, port))
        data, _ = sock.recvfrom(128)
        return data.decode(errors="ignore")
    except socket.timeout:
        return None
    finally:
        sock.close()


def do_sync(ip, port, rounds=20, timeout=1.0):
    """
    PC↔ESP 时间对齐. 多轮 SYNC 取最小 RTT 的样本.
    返回 (offset_us, rtt_us); ESP_us ≈ PC_us + offset_us.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    best_rtt = float("inf")
    best_offset = 0
    successes = 0
    try:
        for _ in range(rounds):
            pc_send_us = now_pc_us()
            try:
                sock.sendto(f"SYNC,{pc_send_us}".encode(), (ip, port))
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
    return best_offset, best_rtt


def parse_freq_onset(resp):
    """
    解析 OK,FREQ,<ch>,<hz>,<onset_us>; 老固件无最后字段时返回 None.
    """
    if not resp or not resp.startswith("OK,FREQ,"):
        return None
    parts = resp.split(",")
    if len(parts) < 5:
        return None
    try:
        return int(parts[4])
    except ValueError:
        return None


def set_channels_freq(ip, port, channels, hz):
    """
    设置多个通道到同一频率, 返回每通道的 esp_onset_us 列表 (None=失败/老固件).
    全部成功则 ok=True.
    """
    onsets = []
    ok = True
    for ch in channels:
        resp = send_cmd(ip, port, f"FREQ,{ch},{hz}")
        if resp is None or not resp.startswith("OK"):
            print(f"  [!] CH{ch} 设置 {hz} Hz 失败: {resp}")
            ok = False
            onsets.append(None)
        else:
            onsets.append(parse_freq_onset(resp))
    return ok, onsets


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
        print("[!] 频率列表为空, 请检查 scan_config.yaml")
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

    # 1) 连通性测试
    print("[*] 测试 ESP32 连接...")
    resp = send_cmd(esp_ip, esp_port, "FREQ,1,0")
    if resp is None:
        print("[!] ESP32 无响应, 请检查网络连接")
        return
    print(f"[+] ESP32 连接正常 (响应: {resp})")

    # 2) PC↔ESP 时间对齐
    print("[*] PC<->ESP 时间对齐 (SYNC) ...")
    sync_result = do_sync(esp_ip, esp_port, rounds=20)
    if sync_result is None:
        print("[!] 时间对齐失败, 后续 trial 不记录 esp_onset_us")
        time_offset_us = None
        sync_rtt_us = None
    else:
        time_offset_us, sync_rtt_us = sync_result
        print(f"[+] offset={time_offset_us} us, 最小 RTT={sync_rtt_us} us")
    print()

    input("按 Enter 开始扫描...")
    print()

    tick_freq = cv2.getTickFrequency()
    scan_start_tick = cv2.getTickCount()
    scan_start_pc_us = now_pc_us()
    trials = []

    for i, f in enumerate(freqs):
        ok, on_onsets = set_channels_freq(esp_ip, esp_port, channels, f)
        stim_start_tick = cv2.getTickCount()
        stim_start_pc_us = now_pc_us()

        if not ok:
            print(f"  [!] 跳过 {f} Hz (配置失败)")
            continue

        time.sleep(stim_duration)
        stim_end_tick = cv2.getTickCount()

        _ok_off, off_onsets = set_channels_freq(esp_ip, esp_port, channels, 0)
        rest_start_tick = cv2.getTickCount()
        rest_start_pc_us = now_pc_us()

        trials.append({
            "index": i,
            "freq_hz": f,
            "channels": channels,
            # PC 侧时间戳 (含 UDP 往返抖动)
            "stim_start_tick": int(stim_start_tick),
            "stim_end_tick": int(stim_end_tick),
            "rest_start_tick": int(rest_start_tick),
            "stim_start_pc_us": int(stim_start_pc_us),
            "rest_start_pc_us": int(rest_start_pc_us),
            # ESP 侧时间戳 — 每通道 LEDC 写入的真实时刻 (us, esp_timer_get_time)
            "esp_onset_us_per_ch": on_onsets,
            "esp_offset_us_per_ch": off_onsets,
        })

        elapsed = (cv2.getTickCount() - scan_start_tick) / tick_freq
        print(f"  [{i+1}/{len(freqs)}] {f} Hz done  ({elapsed:.1f}s elapsed)")

        if i < len(freqs) - 1:
            time.sleep(rest_duration)

    scan_end_tick = cv2.getTickCount()
    total_elapsed = (scan_end_tick - scan_start_tick) / tick_freq

    print()
    print(f"[+] 扫描完成! 总耗时 {total_elapsed:.1f}s, {len(trials)} 个有效 trial")

    result = {
        "config": {
            "channels": channels,
            "freq_start": freq_start,
            "freq_stop": freq_stop,
            "freq_step": freq_step,
            "stim_duration_s": stim_duration,
            "rest_duration_s": rest_duration,
            "esp_ip": esp_ip,
            "esp_port": esp_port,
            "tick_frequency": tick_freq,
        },
        "time_sync": {
            # ESP_us ≈ PC_monotonic_us + time_offset_us
            "time_offset_us": time_offset_us,
            "sync_rtt_us": sync_rtt_us,
        },
        "scan_start_tick": int(scan_start_tick),
        "scan_end_tick": int(scan_end_tick),
        "scan_start_pc_us": int(scan_start_pc_us),
        "trials": trials,
    }

    with open(output, "w", encoding="utf-8") as fp:
        json.dump(result, fp, indent=2, ensure_ascii=False)

    print(f"[+] 结果已保存: {output}")


if __name__ == "__main__":
    main()
