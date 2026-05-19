"""
SSVEP 信号发生器 GUI 上位机
- tkinter 界面: 8 通道频率配置, EVOKE 单次刺激, EDGE_LOG 边沿日志, 会话 JSON 保存
- 后台线程监听 UDP 5006: HELLO 设备发现广播 + EDGES 边沿事件流
- 所有收到的数据包在 recvfrom 返回后立即用 cv2.getTickCount() 打 PC 端时间戳,
  与包内 ESP esp_timer_get_time() us 时间戳一并保存
- 协议详见项目根目录 CLAUDE.md
"""

import json
import queue
import socket
import threading
import time
from datetime import datetime
from tkinter import (
    Tk, ttk, scrolledtext, filedialog, messagebox,
    StringVar, IntVar, BooleanVar,
)

import cv2

# ---- 协议常量 (与 main/station_example_main.c 对齐) ----
ESP_CMD_PORT  = 5005
LISTEN_PORT   = 5006
NUM_CHANNELS  = 8
GPIO_PINS     = [19, 23, 18, 21, 27, 13, 14, 4]
DEFAULT_FREQS = [23, 25, 27, 29, 31, 33, 35, 37]


class SSVEPApp:
    """主应用. 一个实例对应一个 Tk 主窗口."""

    def __init__(self, root):
        self.root = root
        self.root.title("SSVEP 信号发生器控制台")
        self.root.geometry("780x740")

        # ---- 会话状态 ----
        self.tick_freq = cv2.getTickFrequency()
        self.session_start_tick = cv2.getTickCount()
        self.session_start_dt = datetime.now()

        self.esp_ip = None
        self.time_offset_us = None
        self.sync_rtt_us = None

        # ---- 日志缓冲 ----
        self.commands_log = []     # 命令/响应记录, 每条带 pc_tick_sent / pc_tick_recv
        self.edges_log = []        # 每个边沿事件一条, 带 pc_tick_recv (该包到达时刻)
        self.edge_pkt_count = 0
        self.edge_event_count = 0
        self.edge_drop_count = 0   # ESP 回报的 ringbuf 丢弃数

        # ---- 网络/线程 ----
        self.msg_queue = queue.Queue()
        self.listener_running = True

        self._build_ui()

        self.listener = threading.Thread(target=self._listener_loop, daemon=True)
        self.listener.start()

        # 主线程周期性 drain 队列
        self.root.after(50, self._poll_queue)
        # 关闭窗口时优雅退出
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._log_line("启动. 等待 ESP32 广播 (端口 5006)...")

    # =========================================================
    #                          UI
    # =========================================================
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        # ---- 连接条 ----
        conn = ttk.LabelFrame(self.root, text="连接")
        conn.pack(fill="x", **pad)

        self.var_ip = StringVar(value="未连接")
        self.var_offset = StringVar(value="offset: -- µs")
        self.var_rtt = StringVar(value="RTT: -- µs")

        ttk.Label(conn, text="ESP IP:").grid(row=0, column=0, padx=4, pady=4)
        ttk.Label(conn, textvariable=self.var_ip, font=("TkFixedFont",)).grid(
            row=0, column=1, padx=4)
        ttk.Label(conn, textvariable=self.var_offset).grid(row=0, column=2, padx=12)
        ttk.Label(conn, textvariable=self.var_rtt).grid(row=0, column=3, padx=4)
        ttk.Button(conn, text="重新同步", command=self._on_sync).grid(
            row=0, column=4, padx=8)

        # ---- 通道频率 (2 列 × 4 行) ----
        freq = ttk.LabelFrame(self.root, text="通道频率 (Hz)")
        freq.pack(fill="x", **pad)

        self.freq_vars = []
        for i in range(NUM_CHANNELS):
            row = i % 4
            col = i // 4
            colbase = col * 5

            ttk.Label(freq, text=f"CH{i+1}").grid(
                row=row, column=colbase, padx=2, pady=3, sticky="e")
            ttk.Label(freq, text=f"GPIO{GPIO_PINS[i]:>2}", foreground="gray").grid(
                row=row, column=colbase + 1, padx=2)
            var = IntVar(value=DEFAULT_FREQS[i])
            self.freq_vars.append(var)
            ttk.Spinbox(freq, from_=0, to=100, increment=1, width=5,
                        textvariable=var).grid(row=row, column=colbase + 2, padx=2)
            ttk.Button(freq, text="应用", width=6,
                       command=lambda c=i + 1: self._on_apply_freq(c)).grid(
                row=row, column=colbase + 3, padx=2)

        bulk = ttk.Frame(freq)
        bulk.grid(row=4, column=0, columnspan=10, pady=6)
        ttk.Button(bulk, text="全部应用", command=self._on_apply_all).pack(
            side="left", padx=4)
        ttk.Button(bulk, text="全部关闭", command=self._on_all_off).pack(
            side="left", padx=4)
        ttk.Button(bulk, text="恢复默认频率", command=self._on_restore_defaults).pack(
            side="left", padx=4)

        # ---- EVOKE 单次刺激 ----
        evoke = ttk.LabelFrame(self.root, text="EVOKE 单次刺激")
        evoke.pack(fill="x", **pad)

        ttk.Label(evoke, text="通道:").grid(row=0, column=0, padx=4, pady=4)
        self.var_evoke_ch = IntVar(value=1)
        ttk.Spinbox(evoke, from_=1, to=NUM_CHANNELS, increment=1, width=4,
                    textvariable=self.var_evoke_ch).grid(row=0, column=1, padx=4)
        ttk.Label(evoke, text="时长:").grid(row=0, column=2, padx=4)
        self.var_evoke_ms = IntVar(value=500)
        ttk.Spinbox(evoke, from_=1, to=10000, increment=50, width=8,
                    textvariable=self.var_evoke_ms).grid(row=0, column=3, padx=4)
        ttk.Label(evoke, text="ms").grid(row=0, column=4, padx=2)
        ttk.Button(evoke, text="触发 EVOKE", command=self._on_evoke).grid(
            row=0, column=5, padx=12)

        # ---- 边沿日志 ----
        edge = ttk.LabelFrame(self.root, text="边沿日志 (Path A: GPIO ISR 自监听)")
        edge.pack(fill="x", **pad)

        self.var_edge_enabled = BooleanVar(value=False)
        ttk.Button(edge, text="启用", width=8,
                   command=lambda: self._on_edge_log(True)).grid(row=0, column=0, padx=4, pady=4)
        ttk.Button(edge, text="停用", width=8,
                   command=lambda: self._on_edge_log(False)).grid(row=0, column=1, padx=4)
        self.var_edge_stats = StringVar(
            value="状态: 关闭  已收 0 包 / 0 边沿 / 0 丢失")
        ttk.Label(edge, textvariable=self.var_edge_stats).grid(
            row=0, column=2, padx=12)

        # ---- 保存 ----
        save = ttk.LabelFrame(self.root, text="会话保存")
        save.pack(fill="x", **pad)

        ttk.Button(save, text="保存为 JSON...", command=self._on_save).pack(
            side="left", padx=8, pady=4)
        ttk.Button(save, text="清空缓冲", command=self._on_clear).pack(
            side="left", padx=4)
        self.var_save_stats = StringVar(value="命令: 0  边沿: 0")
        ttk.Label(save, textvariable=self.var_save_stats).pack(side="left", padx=8)

        # ---- 实时日志 ----
        log = ttk.LabelFrame(self.root, text="实时日志")
        log.pack(fill="both", expand=True, **pad)
        self.log_widget = scrolledtext.ScrolledText(
            log, height=12, font=("TkFixedFont", 9))
        self.log_widget.pack(fill="both", expand=True, padx=4, pady=4)
        self.log_widget.tag_config("info", foreground="black")
        self.log_widget.tag_config("ok", foreground="#1f6f1f")
        self.log_widget.tag_config("err", foreground="red")
        self.log_widget.tag_config("edge", foreground="#1f4f9f")

    # =========================================================
    #                       Network I/O
    # =========================================================
    def _listener_loop(self):
        """后台线程: 单 socket 同时收 HELLO 广播和 EDGES 流."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", LISTEN_PORT))
        except OSError as e:
            self.msg_queue.put(("err", f"端口 {LISTEN_PORT} 绑定失败: {e}"))
            return

        sock.settimeout(0.5)  # 让 listener 能感知关闭
        while self.listener_running:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            # 立即打 PC 端时间戳, 之后的 decode 与解析不影响这个值
            pc_tick = cv2.getTickCount()
            msg = data.decode("utf-8", errors="ignore")
            self.msg_queue.put(("recv", pc_tick, addr, msg))
        sock.close()

    def _poll_queue(self):
        """主线程: 周期性把 listener 队列的数据搬到 UI."""
        try:
            while True:
                item = self.msg_queue.get_nowait()
                kind = item[0]
                if kind == "recv":
                    _, pc_tick, addr, msg = item
                    self._handle_recv(pc_tick, addr, msg)
                elif kind == "err":
                    self._log_line(item[1], "err")
        except queue.Empty:
            pass
        if self.listener_running:
            self.root.after(50, self._poll_queue)

    def _handle_recv(self, pc_tick, addr, msg):
        if msg.startswith("HELLO,SSVEP,"):
            self._handle_hello(pc_tick, msg)
        elif msg.startswith("EDGES,"):
            self._handle_edges(pc_tick, msg)
        else:
            self._log_line(f"未识别 UDP: {msg[:80]}")

    def _handle_hello(self, pc_tick, msg):
        parts = msg.split(",")
        try:
            new_ip = parts[2]
            freqs = [int(x) for x in parts[4:12]]
        except (IndexError, ValueError):
            return
        if self.esp_ip != new_ip:
            self.esp_ip = new_ip
            self.var_ip.set(new_ip)
            for i, f in enumerate(freqs):
                self.freq_vars[i].set(f)
            self._log_line(f"ESP 上线: {new_ip}, 当前频率={freqs}", "ok")
        # 同 IP 再广播时静默更新频率
        else:
            for i, f in enumerate(freqs):
                self.freq_vars[i].set(f)

    def _handle_edges(self, pc_tick, msg):
        """报文: EDGES,<seq>,<count>,<ch>,<lv>,<us>,...,<ch>,<lv>,<us>"""
        parts = msg.split(",")
        try:
            seq = int(parts[1])
            count = int(parts[2])
        except (IndexError, ValueError):
            return
        if len(parts) != 3 + count * 3:
            return
        for i in range(count):
            try:
                ch = int(parts[3 + i * 3])
                lv = int(parts[3 + i * 3 + 1])
                us = int(parts[3 + i * 3 + 2])
            except ValueError:
                continue
            self.edges_log.append({
                "pc_tick_recv": int(pc_tick),
                "packet_seq": seq,
                "ch": ch,
                "level": lv,
                "esp_us": us,
            })
        self.edge_pkt_count += 1
        self.edge_event_count += count
        self._refresh_edge_stats()
        # 每 20 个包打印一行, 避免日志洪水
        if self.edge_pkt_count % 20 == 1:
            self._log_line(
                f"EDGES seq={seq} count={count}  累计 {self.edge_event_count} 边沿",
                "edge")

    def _send_cmd(self, cmd_str, log_type, parsed_extra=None, timeout=2.0):
        """同步发命令并等响应; 记录 pc_tick_sent / pc_tick_recv."""
        if self.esp_ip is None:
            self._log_line(f"ESP 未连接, 跳过: {cmd_str}", "err")
            return None

        entry = {
            "type": log_type,
            "request": cmd_str,
            "response": None,
            "pc_tick_sent": int(cv2.getTickCount()),
            "pc_tick_recv": None,
        }
        if parsed_extra:
            entry.update(parsed_extra)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(cmd_str.encode(), (self.esp_ip, ESP_CMD_PORT))
            data, _ = sock.recvfrom(256)
            entry["pc_tick_recv"] = int(cv2.getTickCount())
            resp = data.decode("utf-8", errors="ignore")
            entry["response"] = resp
        except socket.timeout:
            resp = None
            self._log_line(f"超时: {cmd_str}", "err")
        finally:
            sock.close()

        self.commands_log.append(entry)
        self._refresh_save_stats()
        return resp

    # =========================================================
    #                      UI Handlers
    # =========================================================
    def _on_apply_freq(self, ch):
        try:
            hz = int(self.freq_vars[ch - 1].get())
        except (ValueError, IndexError):
            return
        if not (0 <= hz <= 100):
            self._log_line(f"CH{ch}: hz 越界 ({hz})", "err")
            return
        resp = self._send_cmd(f"FREQ,{ch},{hz}", "FREQ",
                              parsed_extra={"ch": ch, "hz": hz})
        if resp and resp.startswith("OK,FREQ,"):
            try:
                esp_us = int(resp.split(",")[4])
                self._log_line(
                    f"FREQ CH{ch} = {hz} Hz  | esp_us={esp_us}", "ok")
            except (IndexError, ValueError):
                self._log_line(f"FREQ CH{ch} = {hz} Hz  | {resp}", "ok")
        elif resp:
            self._log_line(f"FREQ CH{ch}: {resp}", "err")

    def _on_apply_all(self):
        for i in range(NUM_CHANNELS):
            self._on_apply_freq(i + 1)

    def _on_all_off(self):
        for i in range(NUM_CHANNELS):
            self.freq_vars[i].set(0)
            self._on_apply_freq(i + 1)

    def _on_restore_defaults(self):
        for i, f in enumerate(DEFAULT_FREQS):
            self.freq_vars[i].set(f)
        self._log_line("UI 频率值已恢复默认 (未发送), 按需点击 应用 / 全部应用", "info")

    def _on_evoke(self):
        try:
            ch = int(self.var_evoke_ch.get())
            ms = int(self.var_evoke_ms.get())
        except ValueError:
            return
        if not (1 <= ch <= NUM_CHANNELS):
            self._log_line(f"EVOKE: 通道越界 {ch}", "err")
            return
        if not (1 <= ms <= 10000):
            self._log_line(f"EVOKE: 时长越界 {ms}", "err")
            return
        resp = self._send_cmd(f"EVOKE,{ch},{ms}", "EVOKE",
                              parsed_extra={"ch": ch, "ms": ms})
        if resp and resp.startswith("OK,EVOKE,"):
            try:
                esp_us = int(resp.split(",")[4])
                self._log_line(
                    f"EVOKE CH{ch} {ms} ms  | onset esp_us={esp_us}", "ok")
            except (IndexError, ValueError):
                self._log_line(f"EVOKE CH{ch}: {resp}", "ok")
        elif resp:
            self._log_line(f"EVOKE CH{ch}: {resp}", "err")

    def _on_edge_log(self, enable):
        cmd = "EDGE_LOG,ON" if enable else "EDGE_LOG,OFF"
        resp = self._send_cmd(cmd, "EDGE_LOG", parsed_extra={"enable": enable})
        if resp and resp.startswith("OK,EDGE_LOG,"):
            self.var_edge_enabled.set(enable)
            if not enable:
                # OK,EDGE_LOG,OFF,<drop_count>
                try:
                    self.edge_drop_count = int(resp.split(",")[3])
                except (IndexError, ValueError):
                    pass
            self._log_line(f"EDGE_LOG -> {'ON' if enable else 'OFF'}", "ok")
            self._refresh_edge_stats()
        elif resp:
            self._log_line(f"EDGE_LOG: {resp}", "err")

    def _on_sync(self):
        """20 轮 SYNC, 保留最小 RTT 的样本作为 offset 估计."""
        if self.esp_ip is None:
            self._log_line("ESP 未连接, 无法同步", "err")
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        best_rtt = float("inf")
        best_offset = 0
        successes = 0
        try:
            for _ in range(20):
                pc_send_us = time.monotonic_ns() // 1000
                try:
                    sock.sendto(f"SYNC,{pc_send_us}".encode(),
                                (self.esp_ip, ESP_CMD_PORT))
                    data, _ = sock.recvfrom(128)
                    pc_recv_us = time.monotonic_ns() // 1000
                except socket.timeout:
                    continue
                resp = data.decode("utf-8", errors="ignore")
                if not resp.startswith("OK,SYNC,"):
                    continue
                parts = resp.split(",")
                if len(parts) != 5:
                    continue
                try:
                    esp_recv = int(parts[3])
                    esp_send = int(parts[4])
                except ValueError:
                    continue
                rtt = (pc_recv_us - pc_send_us) - (esp_send - esp_recv)
                offset = ((esp_recv - pc_send_us) + (esp_send - pc_recv_us)) // 2
                if rtt < best_rtt:
                    best_rtt = rtt
                    best_offset = offset
                successes += 1
                time.sleep(0.005)
        finally:
            sock.close()

        if successes == 0:
            self._log_line("同步失败 (无响应)", "err")
            return
        self.time_offset_us = best_offset
        self.sync_rtt_us = best_rtt
        self.var_offset.set(f"offset: {best_offset:+d} µs")
        self.var_rtt.set(f"RTT: {best_rtt} µs")
        self._log_line(
            f"SYNC done: offset={best_offset:+d} µs, "
            f"min RTT={best_rtt} µs ({successes}/20 OK)", "ok")

    def _on_clear(self):
        if not messagebox.askyesno("清空缓冲", "确认清空当前命令/边沿日志? (UI 显示不受影响)"):
            return
        self.commands_log.clear()
        self.edges_log.clear()
        self.edge_pkt_count = 0
        self.edge_event_count = 0
        self.edge_drop_count = 0
        self._refresh_edge_stats()
        self._log_line("缓冲已清空", "info")

    def _on_save(self):
        if not self.commands_log and not self.edges_log:
            messagebox.showinfo("保存", "目前没有任何数据可保存")
            return
        default_name = (
            f"ssvep_session_{self.session_start_dt.strftime('%Y%m%d_%H%M%S')}.json"
        )
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile=default_name,
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
        )
        if not path:
            return
        payload = {
            "session": {
                "start_pc_tick": int(self.session_start_tick),
                "tick_frequency": float(self.tick_freq),
                "start_datetime": self.session_start_dt.isoformat(),
                "esp_ip": self.esp_ip,
            },
            "time_sync": {
                "offset_us": self.time_offset_us,
                "rtt_us": self.sync_rtt_us,
                "note": "esp_us ≈ pc_monotonic_us + offset_us (空时表示未执行 SYNC)",
            },
            "commands": self.commands_log,
            "edges": self.edges_log,
            "edge_stats": {
                "packet_count": self.edge_pkt_count,
                "event_count": self.edge_event_count,
                "drop_count_reported": self.edge_drop_count,
            },
        }
        try:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, indent=2, ensure_ascii=False)
            self._log_line(
                f"已保存 {len(self.commands_log)} 命令 + {len(self.edges_log)} 边沿 -> {path}",
                "ok")
        except OSError as e:
            self._log_line(f"保存失败: {e}", "err")

    def _on_close(self):
        self.listener_running = False
        self.root.after(100, self.root.destroy)

    # =========================================================
    #                     UI Refreshers
    # =========================================================
    def _log_line(self, text, tag="info"):
        ts = (cv2.getTickCount() - self.session_start_tick) / self.tick_freq
        line = f"[{ts:8.3f}s] {text}\n"
        self.log_widget.insert("end", line, tag)
        self.log_widget.see("end")
        # 上限 500 行, 防止长会话内存爆
        lines = int(self.log_widget.index("end-1c").split(".")[0])
        if lines > 500:
            self.log_widget.delete("1.0", f"{lines - 400}.0")

    def _refresh_edge_stats(self):
        state = "运行" if self.var_edge_enabled.get() else "关闭"
        self.var_edge_stats.set(
            f"状态: {state}  已收 {self.edge_pkt_count} 包 / "
            f"{self.edge_event_count} 边沿 / {self.edge_drop_count} 丢失"
        )
        self._refresh_save_stats()

    def _refresh_save_stats(self):
        self.var_save_stats.set(
            f"命令: {len(self.commands_log)}  边沿: {len(self.edges_log)}"
        )


def main():
    root = Tk()
    SSVEPApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
