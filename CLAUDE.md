# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 本仓库为 **SSVEP 视觉刺激发生系统** 的端到端实现, 由两个相互配合的部分组成:
> 1. **ESP32 固件** (`main/`) — 8 通道 LEDC 硬件 PWM 输出, WiFi STA + UDP 服务器
> 2. **PC Python 上位机** (`ssvep_led_ctrl/`) — 自动发现 ESP, 时间对齐, 频率控制, 单次刺激, trial 时间戳记录
>
> 两端共用一套 UDP 文本协议 (见 [第三部分](#第三部分udp-协议两端共用)), 通过 SYNC 子协议实现 PC↔ESP us 级时间对齐, 用 ESP `esp_timer` 捕获事件真实执行时间戳, 消除无线传输抖动。

---

## 构建与开发命令

ESP-IDF v5.5.0, 目标芯片 ESP32, 串口 COM3:

```bash
idf.py build                    # 编译
idf.py -p COM3 flash            # 烧录
idf.py -p COM3 monitor          # 串口监视 (Ctrl-] 退出)
idf.py -p COM3 flash monitor    # 一步完成: 编译 + 烧录 + 监视
idf.py menuconfig               # SDK 配置菜单 (开放 SSVEP_UDP_PORT)
idf.py set-target esp32         # 选择芯片
idf.py fullclean                # 清空 build/
```

PC 端 Python 工具 (依赖 `opencv-python` 提供 `cv2.getTickCount`, `pyyaml` 仅 scan 需要; tkinter 在 Python stdlib):

```cmd
cd ssvep_led_ctrl
python ssvep_gui.py             # tkinter GUI: 多通道 + EVOKE + 边沿日志 + JSON 保存
python ssvep_ctrl.py            # 交互式 REPL (无 GUI, 适合脚本/远程)
python ssvep_scan.py            # 自动频率扫描 (读 scan_config.yaml)
run.cmd                         # Windows 启动器, 用 conda env VIZ 跑 ssvep_ctrl.py
```

---

## 第一部分: ESP32 固件

### 1.1 架构总览

固件位于单一翻译单元 `main/station_example_main.c` (~430 行)。**没有 timer ISR, 没有合成流水线**, 也没有跨核忙等待 — LEDC PWM 由硬件外设自主工作, CPU 几乎只参与 WiFi/UDP 与单次刺激的关闭定时。

四个功能模块:

| 模块 | 函数 | 位置 | 说明 |
|------|------|------|------|
| LEDC PWM 输出 | `ledc_init_all` | main/station_example_main.c:107 | 8 路硬件 PWM, 启动后自主运行 |
| EVOKE 关闭定时器 | `evoke_timers_init`, `evoke_off_cb` | main/station_example_main.c:80-102 | 8 个 esp_timer, 单次刺激到期自动关闭 |
| WiFi STA + 静态 IP | `wifi_init_sta` | main/station_example_main.c:209 | SSID/密码硬编码, DHCP 关闭 |
| UDP 服务器 | `udp_server_task` | main/station_example_main.c:277 | Core 0 / priority 2, 处理 FREQ / EVOKE / SYNC |
| 发现广播 | `send_announce` | main/station_example_main.c:138 | 启动时 + 心跳每 10 秒一次 |

### 1.2 启动流程

```
app_main()  [main/station_example_main.c:411]
  ├─ nvs_flash_init()                       # WiFi 前置依赖
  ├─ ledc_init_all()                        # 8 路 PWM 立即起跑
  ├─ evoke_timers_init()                    # 每通道 1 个一次性 off-timer
  ├─ wifi_init_sta()                        # 静态 IP, 最多阻塞 15 秒
  ├─ xTaskCreatePinnedToCore(
  │     udp_server_task, "udp_srv",
  │     stack=4096, prio=2, core=0)         # UDP 命令监听
  └─ 心跳循环 (每 10 秒):
        ESP_LOGI(freqs); send_announce()
```

### 1.3 LEDC 通道布局

| 通道 | GPIO | LEDC Speed Mode | LEDC Timer | LEDC Channel | 默认频率 (Hz) |
|------|------|-----------------|------------|--------------|---------------|
| 1    | 19   | HIGH            | TIMER_0    | CHANNEL_0    | 23 |
| 2    | 23   | HIGH            | TIMER_1    | CHANNEL_1    | 25 |
| 3    | 18   | HIGH            | TIMER_2    | CHANNEL_2    | 27 |
| 4    | 21   | HIGH            | TIMER_3    | CHANNEL_3    | 29 |
| 5    | 27   | LOW             | TIMER_0    | CHANNEL_0    | 31 |
| 6    | 13   | LOW             | TIMER_1    | CHANNEL_1    | 33 |
| 7    | 14   | LOW             | TIMER_2    | CHANNEL_2    | 35 |
| 8    | 4    | LOW             | TIMER_3    | CHANNEL_3    | 37 |

- 定义阵列: `CHANNEL_PINS[]`, `channel_freq[]`, `CHANNEL_SPEED[]`, `CHANNEL_TIMER[]`, `CHANNEL_LEDC[]` (main/station_example_main.c:44-67)。CH1-4 用 `LEDC_HIGH_SPEED_MODE`, CH5-8 用 `LEDC_LOW_SPEED_MODE`, 同一速度模式内每通道独占一个 timer。
- 分辨率 13-bit (`LEDC_TIMER_13_BIT` = 8192), 占空比固定 50% (`LEDC_DUTY_50PCT` = 4096) → 输出方波。
- `hz=0` 把 duty 置 0 (通道关闭, 输出恒低); 任何正值都会调用 `ledc_set_freq()` 然后恢复 50% duty。
- **默认频率选择**: 全奇数, 2 Hz 间距, 在前 5 阶谐波内无两两碰撞 (main/station_example_main.c:46 注释)。SSVEP 分析常需跨谐波累计能量, 此设计可避免通道间窜扰。

### 1.4 EVOKE 单次刺激机制

单次刺激由 PC 端发出 `EVOKE,<ch>,<duration_ms>` 命令, ESP 内部流程:

1. `esp_timer_stop(evoke_off_timers[idx])` — 取消该通道未到期的旧 off-timer (允许重触发)
2. `ledc_set_duty(50%) + ledc_update_duty()` — 通道开始按当前频率输出
3. `t_onset = esp_timer_get_time()` — 紧接 LEDC 寄存器写入后捕获 us 时间戳
4. `esp_timer_start_once(off_timer, duration_ms * 1000)` — 安排到期自动调用 `evoke_off_cb` 关闭通道
5. 回送 `OK,EVOKE,<ch>,<ms>,<t_onset>` — 客户端据此计算修正后的 onset 时间

关闭回调 `evoke_off_cb` 在 esp_timer 服务任务上下文 (非 ISR) 执行, 可安全调用 LEDC API。

### 1.5 EDGE_LOG 边沿日志 (Path A: GPIO ISR 自监听)

为解决 LEDC "首边沿 onset 抖动达 1 个 PWM 周期"的问题, 提供可选的边沿日志通道: PC 通过 `EDGE_LOG,ON` 启用后, ESP 把每个 GPIO 上升/下降沿的精确时间戳实时上报, 从而获得每个 on/off 帧的真值。**默认关闭, 启用时 CPU 占用 ~0.5%**。

实现要点 (main/station_example_main.c:113-249):

| 组件 | 函数/对象 | 说明 |
|------|-----------|------|
| 事件结构 | `edge_event_t` | `{int64 ts_us; uint8 ch; uint8 level; pad}` — 12 字节 |
| 环形缓冲 | `edge_ringbuf` (FreeRTOS RingBuffer, 8 KB) | ISR 写, sender 读, 容量 ~680 事件 |
| ISR | `edge_isr` (IRAM_ATTR) | 任何边沿触发: 读 `esp_timer_get_time` + `gpio_get_level`, 入队 |
| 发送任务 | `edge_sender_task` (Core 0, prio 3) | 阻塞拉 ringbuf, 每包最多 50 事件, 广播到 `255.255.255.255:5006` |
| 启停 | `edge_log_enable_all` / `edge_log_disable_all` | 动态 add/remove ISR handler; LEDC 输出不受影响 |

关键技巧:
- LEDC 输出引脚通过 `gpio_set_direction(pin, GPIO_MODE_INPUT_OUTPUT)` 同时启用输入缓冲, **不破坏 LEDC 矩阵路由** — 同一引脚既被 LEDC 驱动又被 GPIO 中断采样
- ISR 中**绝不**调用 LEDC/lwIP/UART 等非 IRAM-safe API; 只做时间戳捕获 + RingBuffer 入队
- ringbuf 溢出由 `edge_drop_count` 累计, `EDGE_LOG,OFF` 响应中回报, PC 可据此判断数据完整性

边沿速率与开销 (8 通道, ANYEDGE):

| 通道频率 | 边沿率 | UDP 包率 (50/pkt) | CPU 占用 |
|---------|-------|------------------|---------|
| 30 Hz   | 480 / s   | ~10 pkt/s | < 0.3% |
| 60 Hz   | 960 / s   | ~20 pkt/s | < 0.5% |
| 80 Hz   | 1280 / s  | ~26 pkt/s | < 0.7% |
| 120 Hz  | 1920 / s  | ~38 pkt/s | < 1.0% |

### 1.6 关键文件

- `main/station_example_main.c` — 全部固件 (LEDC, EVOKE timers, edge ISR/sender, WiFi, UDP, announce, 心跳)
- `main/Kconfig.projbuild` — 暴露 `SSVEP_UDP_PORT` (默认 5005) 给 `idf.py menuconfig`
- `main/CMakeLists.txt` — 单源组件注册
- `main/gattc_demo.c.backup` — **不参与构建**, ESP-IDF 原始 GATT client 例程备份, 忽略
- `CMakeLists.txt` (项目根) — 工程名仍是 `project(gatt_client_demo)`, 来自原始例程 fork, 命名误导但无功能影响, 切勿据其推断架构
- `README.md` — **过时**, 内容是 ESP-IDF GATT Client 例程, 与当前固件无关, 忽略
- `sdkconfig` — `idf.py menuconfig` 自动生成, 勿手改; `sdkconfig.old` 是历史快照
- `memory/` — 跨会话固化的 user / feedback / reference 记忆 (与 `~/.claude/projects/<proj>/memory/` 同步, 此处版本是 git 跟踪的真值快照); 入口 `memory/MEMORY.md` (一行一条索引)

---

## 第二部分: PC 端 Python 控制 (`ssvep_led_ctrl/`)

PC 端共三个脚本, 共享同一 UDP 协议; 按使用场景挑一个:

### 2.1 工具一览

| 文件 | 用途 | 输出 |
|------|------|------|
| **`ssvep_gui.py`** | **tkinter GUI: 多通道频率控件 + EVOKE 控件 + 边沿日志开关 + 实时日志 + 会话 JSON 保存** | **JSON 会话 (含 cv2 tick + ESP us)** |
| `ssvep_ctrl.py` | 交互式 REPL: 自动发现 ESP, 自动时间对齐, 手动配置频率, 单次刺激 | 终端打印 ESP 与 PC 双时间戳 |
| `ssvep_scan.py` | 自动频率扫描 (默认 8→80 Hz, 步进 2 Hz), 每个频率持续 5s | JSON, 每 trial 同时含 cv2 tick 与 esp_onset_us |
| `scan_config.yaml` | scan 配置 (ESP IP/端口, 频率范围, 持续时间, 输出文件名) | — |
| `run.cmd` | Windows 启动器, 用 conda env `VIZ` (Python 3.12) 跑 ssvep_ctrl.py | — |

依赖: `socket` / `threading` / `queue` / `tkinter` (stdlib); `cv2` (opencv-python) 用于 `getTickCount` 时基; `pyyaml` 仅 ssvep_scan.py 需要。

### 2.2 GUI (ssvep_gui.py) 使用要点

启动: `python ssvep_gui.py` (推荐用 conda env VIZ, 已装齐依赖)。

布局 (从上到下):

```
┌─ 连接 ───────────────────────────────────────────────────┐
│  ESP IP: <auto-discovered>   offset: -- µs   RTT: -- µs  [重新同步]│
├─ 通道频率 (Hz) ─────────────────────────────────────────┤
│  CH1 GPIO19 [23] [应用]   CH5 GPIO27 [31] [应用]         │
│  CH2 GPIO23 [25] [应用]   CH6 GPIO13 [33] [应用]         │
│  CH3 GPIO18 [27] [应用]   CH7 GPIO14 [35] [应用]         │
│  CH4 GPIO21 [29] [应用]   CH8 GPIO 4 [37] [应用]         │
│  [全部应用] [全部关闭] [恢复默认频率]                    │
├─ EVOKE 单次刺激 ────────────────────────────────────────┤
│  通道:[1]  时长:[500] ms  [触发 EVOKE]                  │
├─ 边沿日志 ─────────────────────────────────────────────┤
│  [启用] [停用]  状态: ...  已收 N 包 / N 边沿 / N 丢失   │
├─ 会话保存 ─────────────────────────────────────────────┤
│  [保存为 JSON...] [清空缓冲]  命令: N  边沿: N           │
├─ 实时日志 ─────────────────────────────────────────────┤
│  [滚动 Text 区, 颜色: black/info, green/ok, red/err, blue/edge] │
└────────────────────────────────────────────────────────┘
```

线程模型:
- **主线程**: tkinter UI, 处理所有 UI 事件、命令发送 (短阻塞 UDP)、JSON 保存
- **listener 线程** (daemon): 在 5006 端口 `recvfrom`, 每包到达后立即 `cv2.getTickCount()` 打戳, 入 `queue.Queue`
- **drain**: 主线程用 `root.after(50, ...)` 周期取队列, 分发到 HELLO / EDGES 处理器

时间戳记录策略 (PC 端统一为 `cv2.getTickCount()`):

| 事件类型 | PC 侧时间戳 | ESP 侧时间戳 (若有) |
|---------|-----------|------------------|
| 命令发送 | `pc_tick_sent` | — |
| 响应到达 | `pc_tick_recv` | 响应内嵌的 `esp_onset_us` (FREQ/EVOKE) 或 `esp_recv/send_us` (SYNC) |
| 边沿事件 | `pc_tick_recv` (包到达时刻, 所有事件共用一个) | 每事件独立的 `esp_us` |
| HELLO 广播 | (打戳但默认不入命令日志) | — |

### 2.3 会话 JSON 输出格式 (GUI 保存)

```json
{
  "session": {
    "start_pc_tick": 12345678901,
    "tick_frequency": 1.0e7,
    "start_datetime": "2026-05-19T16:00:00.000000",
    "esp_ip": "192.168.137.100"
  },
  "time_sync": {
    "offset_us": null,
    "rtt_us": null,
    "note": "esp_us ≈ pc_monotonic_us + offset_us (空表示未执行 SYNC)"
  },
  "commands": [
    {
      "type": "FREQ", "request": "FREQ,1,30",
      "response": "OK,FREQ,1,30,12345678",
      "pc_tick_sent": ..., "pc_tick_recv": ...,
      "ch": 1, "hz": 30
    },
    {
      "type": "EVOKE", "request": "EVOKE,1,500",
      "response": "OK,EVOKE,1,500,12399999",
      "pc_tick_sent": ..., "pc_tick_recv": ...,
      "ch": 1, "ms": 500
    }
  ],
  "edges": [
    {"pc_tick_recv": ..., "packet_seq": 42, "ch": 1, "level": 1, "esp_us": ...},
    {"pc_tick_recv": ..., "packet_seq": 42, "ch": 1, "level": 0, "esp_us": ...}
  ],
  "edge_stats": {
    "packet_count": 1234, "event_count": 56789, "drop_count_reported": 0
  }
}
```

> 离线分析: 把 `esp_us` 当 ESP 自身时间帧的精确事件标尺; 把 `pc_tick_recv` (除以 `tick_frequency`) 当 PC 主时钟下该事件被观测到的时刻。一个事件的真实物理发生时刻 ≈ ESP us, 它对应的 PC 主时钟时刻 ≈ pc_tick_recv − 单向网络延迟 (该延迟约 RTT/2, 见 SYNC 章节)。

### 2.4 ssvep_ctrl.py — REPL 控制台

`ssvep_ctrl.py` 启动时:

1. 后台线程在 5006 端口监听 `HELLO,SSVEP,<ip>,...` 广播
2. 收到 HELLO → 解析 IP/频率, **自动触发 `do_sync(rounds=20)`**
3. `do_sync()` 连续发 20 次 `SYNC,<pc_send_us>`, 保留最小 RTT 的样本作为 offset 估计 (NTP 风格)
4. 设置全局 `time_offset_us`, 后续所有响应中的 ESP 时间戳可通过 `esp_to_pc_us()` 换算回 PC monotonic 时间

REPL 增量命令:
- `freq <ch> <hz>` — 设连续 PWM, 显示 onset 时间戳
- `evoke <ch> <ms>` — 单次刺激, 显示 onset/offset 时间戳 (PC 等效时间)
- `sync` — 手动重做时间对齐
- `status` — 当前连接, offset, RTT, 各通道频率

### 2.5 ssvep_scan.py — 自动频率扫描

每个 trial 同时记录:

| 字段 | 时基 | 含义 |
|------|------|------|
| `stim_start_tick` / `stim_end_tick` | PC `cv2.getTickCount()` | PC 调度时间, **含 UDP 往返抖动** |
| `stim_start_pc_us` / `rest_start_pc_us` | PC `time.monotonic_ns() // 1000` | 同上 |
| `esp_onset_us_per_ch` | **ESP `esp_timer_get_time()`** | LEDC 寄存器写入瞬间, 不含网络抖动 |
| `esp_offset_us_per_ch` | 同上 | 关闭刺激时的 ESP 时间戳 |

JSON 顶层亦保存 `time_sync.time_offset_us` 与 `time_sync.sync_rtt_us`, 便于离线 EEG 分析时将 ESP 时间戳换算到 PC monotonic 轴。

> 推荐做法: EEG 采集端用 PC 主时钟做 trigger 对齐, 用 `esp_onset_us - time_offset_us` 作为 trial 真实 onset 的 PC 等效时间; 它的精度由 SYNC 的最小 RTT 决定 (清洁 WiFi 下通常 ≤500 µs)。

---

## 第三部分: UDP 协议 (两端共用)

### 3.1 端口与方向

| 端口 | 监听方 | 用途 |
|------|--------|------|
| 5005 (ESP) | ESP 服务器 | 接收 FREQ / EVOKE / SYNC; 单播回响应 |
| 5006 (PC)  | PC 监听   | 接收 ESP 启动 / 心跳广播 `HELLO,SSVEP,...` |

ESP 端口 5005 可在 `idf.py menuconfig` → "SSVEP Signal Generator Configuration" → `SSVEP_UDP_PORT` 修改; PC 端 5006 在两个 .py 脚本中作为常量 `LISTEN_PORT`。

### 3.2 报文格式总表

所有报文均为 ASCII 文本, 逗号分隔字段; ESP 时间戳全部是 `esp_timer_get_time()` 返回的 **us 级 int64**, 自 ESP 上电起单调递增。

| 方向 | 请求 / 广播 | 响应 |
|------|-------------|------|
| ESP → PC 广播 (5006) | `HELLO,SSVEP,<ip>,5005,<f1>,<f2>,...,<f8>` | (无响应) |
| ESP → PC 广播 (5006) | `EDGES,<seq>,<count>[,<ch>,<lv>,<esp_us>]×count` | (无响应, 流式) |
| PC → ESP (5005) | `SYNC,<pc_send_us>` | `OK,SYNC,<pc_send_us>,<esp_recv_us>,<esp_send_us>` |
| PC → ESP (5005) | `FREQ,<ch 1..8>,<hz 0..100>` | `OK,FREQ,<ch>,<hz>,<esp_onset_us>` |
| PC → ESP (5005) | `EVOKE,<ch 1..8>,<duration_ms 1..10000>` | `OK,EVOKE,<ch>,<duration_ms>,<esp_onset_us>` |
| PC → ESP (5005) | `EDGE_LOG,ON` | `OK,EDGE_LOG,ON` |
| PC → ESP (5005) | `EDGE_LOG,OFF` | `OK,EDGE_LOG,OFF,<drop_count>` |

错误响应 (单串, 无附加字段):

| 错误码 | 含义 |
|--------|------|
| `ERR,PARSE`     | 报文无法匹配上述任一格式 |
| `ERR,CHANNEL`   | 通道号越界 (须 1..8) |
| `ERR,RANGE`     | hz 越界 (FREQ 须 0..100) 或 duration 越界 (EVOKE 须 1..10000 ms) |
| `ERR,LEDC`      | 底层 LEDC API 返回非 ESP_OK |
| `ERR,EDGE_LOG`  | EDGE_LOG 启停失败 (例如 RingBuffer 未初始化) |

### 3.3 时间对齐 (SYNC) 详解

PC 端 (Cristian 算法, 见 [ssvep_ctrl.py:do_sync](ssvep_led_ctrl/ssvep_ctrl.py)):

```python
pc_send_us  = time.monotonic_ns() // 1000   # 发送前
sendto("SYNC,{pc_send_us}")
data, _ = recvfrom()
pc_recv_us  = time.monotonic_ns() // 1000   # 接收后
# 解析 OK,SYNC,<pc_send_us>,<esp_recv_us>,<esp_send_us>
rtt    = (pc_recv_us - pc_send_us) - (esp_send_us - esp_recv_us)
offset = ((esp_recv_us - pc_send_us) + (esp_send_us - pc_recv_us)) // 2
# 多轮采样, 保留 RTT 最小的 offset
```

换算: `esp_us ≈ pc_us + offset` (即 `pc_us ≈ esp_us - offset`)

ESP 端 (`udp_server_task` SYNC 分支, main/station_example_main.c:391):

```c
// recvfrom 解阻塞后立即捕获
int64_t t_recv = esp_timer_get_time();
// ... 解析 SYNC,<pc_send_us> ...
int64_t t_send = esp_timer_get_time();          // sendto 之前
snprintf(ack, ..., "OK,SYNC,%llu,%lld,%lld", pc_us, t_recv, t_send);
sendto(...);
```

**关键时序**: ESP 端的 `t_recv` 在 `recvfrom()` 返回的下一条指令捕获, 排除 udp_server_task 后续 sscanf 等处理的耗时; `t_send` 在 sendto 之前捕获, 把 ESP 内部处理时长 `(t_send - t_recv)` 从总 RTT 中扣掉, 得到纯网络往返时间。

### 3.4 时间戳精度

| 项 | 量级 | 说明 |
|----|------|------|
| `esp_timer_get_time()` 精度 | < 1 µs | 50 MHz 系统计数器 |
| Cristian 算法误差上限 | RTT/2 | 在路径对称假设下 |
| 清洁 2.4 GHz WiFi RTT | 1-5 ms | 取 20 轮最小后 offset 误差通常 ≤500 µs |
| LEDC 实际 onset 抖动 | 0 至 1 个 PWM 周期 | 因 duty 寄存器只在周期边界生效; 23 Hz 时最坏 43 ms |

> 对于需要亚毫秒 onset 精度的实验, 应使用更高 SSVEP 频率 (例如 30+ Hz, 周期 ≤33 ms), 或在 EVOKE 前用 `FREQ,<ch>,<hz>` 把通道先关再开, 把首个 PWM 周期对齐到 EVOKE 命令时刻附近。`esp_onset_us` 始终反映 LEDC 寄存器写入瞬间, 不含网络抖动 — 这是设计目标。

### 3.5 EDGES 边沿事件流细节

启用方式: PC 发 `EDGE_LOG,ON`, 直到发 `EDGE_LOG,OFF` 为止。

**包格式** (ASCII, 单 UDP 包, 广播到 `255.255.255.255:5006`):

```
EDGES,<seq>,<count>,<ch>,<lv>,<esp_us>[,<ch>,<lv>,<esp_us>]×(count-1)
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `seq` | uint32 | 单调递增的包序号 (从启用时刻 ESP 端 0 起), 用于丢包检测 |
| `count` | int | 本包包含的边沿事件数, 1..50 |
| `ch` | uint, 1..8 | 通道号 (1-indexed, 与 FREQ/EVOKE 一致) |
| `lv` | 0 / 1 | GPIO 在边沿瞬间的电平 (上升=1, 下降=0) |
| `esp_us` | int64 | `esp_timer_get_time()` 在 ISR 中捕获的微秒值 |

**示例**:
```
EDGES,42,3,1,1,123456789,1,0,123462789,2,1,123470000
              ^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^^^^^^^^^
              CH1 rise @ 123,456,789us  CH1 fall @ 123,462,789us  CH2 rise @ 123,470,000us
```

**打包策略 (ESP 端 `edge_sender_task`)**:
- 阻塞等首个事件最多 50 ms (`EDGE_DRAIN_TIMEOUT_MS`)
- 拿到首事件后非阻塞地榨干 ringbuf 当前积压, 至多 50 个 (`EDGE_PACK_MAX`)
- 一次 `sendto` 发出, 立即回去等下一波
- ⇒ 平均延迟 25 ms (一个 drain 周期的一半), 最坏 50 ms

**丢失检测 (PC 端)**:
1. **包级**: `seq` 不连续 → 整批边沿丢失 (WiFi 重传失败 / UDP 丢弃)
2. **事件级**: `EDGE_LOG,OFF` 响应中的 `drop_count` 字段 — ESP RingBuffer 满时未入队的 ISR 调用次数; 通常应为 0
3. **packet_seq + ch + esp_us 序列单调性**: 离线分析按 `(ch, esp_us)` 排序检查间隔 ≈ 半周期, 异常间隔即丢失

**MTU 余量**:
- 最大 ASCII 长度: `"EDGES,4294967295,50" + 50 × ",8,1,9223372036854775807"` ≈ 1170 字节
- 远小于 1500 字节以太网 MTU, 无 IP 分片

---

## 网络配置

- **WiFi STA**: SSID `"eeg"`, 密码硬编码在 main/station_example_main.c:24-25 (单设备实验台, 故意不抽参数化)
- 静态 IP `192.168.137.100/24`, 网关 `192.168.137.1`, 子网掩码 `255.255.255.0` — 匹配 Windows 11 移动热点默认网段
- DHCP 客户端在 `wifi_init_sta` 内显式停止 (main/station_example_main.c:218)

如需更换网络, 修改 main/station_example_main.c 的以下宏:
- `WIFI_SSID` / `WIFI_PASS` (第 24-25 行)
- `STATIC_IP` / `STATIC_GW` / `STATIC_NETMASK` (第 202-204 行)

---

## 环境

- ESP-IDF v5.5.0, 位于 `C:\Users\thlab\esp\v5.5\esp-idf`
- 目标芯片: ESP32 (Xtensa 双核, 240 MHz)
- 烧录: COM3, UART, DIO 模式, 40 MHz, 2 MB Flash
- 固件源码注释为中文
- PC Python: 系统 Python 3.8 不满足 ESP-IDF 要求 (需 ≥3.9); 可用环境 `C:\Users\thlab\.conda\envs\VIZ\python.exe` (3.12, 已装 cv2/yaml)
- 工程名 `gatt_client_demo` 是从 ESP-IDF GATT Client 例程 fork 后保留的旧名, 与当前 SSVEP 实现无关
