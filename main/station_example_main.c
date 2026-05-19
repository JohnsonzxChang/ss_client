#include <string.h>
#include <stdlib.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/ringbuf.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "driver/ledc.h"
#include "driver/gpio.h"
#include "lwip/err.h"
#include "lwip/sockets.h"
#include "lwip/sys.h"
#include <lwip/netdb.h>
#include "lwip/ip4_addr.h"

static const char *TAG = "ssvep";

/* ---- WiFi 配置 (硬编码) ---- */
#define WIFI_SSID           "eeg"
#define WIFI_PASS           "zhangxu123"
#define WIFI_MAX_RETRY      10

/* ---- WiFi 事件组 ---- */
#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_FAIL_BIT       BIT1

/* ---- UDP 配置 ---- */
#define UDP_PORT            5005
#define UDP_BUF_SIZE        64
#define PC_IP               "255.255.255.255"  /* 广播, PC 无需固定 IP */
#define PC_PORT             5006               /* PC 端监听此端口收招呼 */

/* ---- LEDC 配置 ---- */
#define LEDC_DUTY_RES       LEDC_TIMER_13_BIT   /* 13-bit = 8192 counts */
#define LEDC_DUTY_50PCT     4096                 /* 50% of 8192          */
#define NUM_CHANNELS        8

/* ---- GPIO 引脚 ---- */
static const int CHANNEL_PINS[NUM_CHANNELS] = {19, 23, 18, 21, 27, 13, 14, 4};

/* ---- 默认频率 (Hz) — SSVEP 优化: 全奇数, 2Hz 等间距, Nh≤5 无谐波碰撞 ---- */
static uint32_t channel_freq[NUM_CHANNELS] = {23, 25, 27, 29, 31, 33, 35, 37};

/* ---- LEDC 速度模式: CH0-3 高速, CH4-7 低速 ---- */
static const ledc_mode_t CHANNEL_SPEED[NUM_CHANNELS] = {
    LEDC_HIGH_SPEED_MODE, LEDC_HIGH_SPEED_MODE,
    LEDC_HIGH_SPEED_MODE, LEDC_HIGH_SPEED_MODE,
    LEDC_LOW_SPEED_MODE,  LEDC_LOW_SPEED_MODE,
    LEDC_LOW_SPEED_MODE,  LEDC_LOW_SPEED_MODE
};

/* ---- LEDC 定时器索引: 每通道独占一个定时器 ---- */
static const ledc_timer_t CHANNEL_TIMER[NUM_CHANNELS] = {
    LEDC_TIMER_0, LEDC_TIMER_1, LEDC_TIMER_2, LEDC_TIMER_3,
    LEDC_TIMER_0, LEDC_TIMER_1, LEDC_TIMER_2, LEDC_TIMER_3
};

/* ---- LEDC 通道索引 (每个速度模式内 0-3) ---- */
static const ledc_channel_t CHANNEL_LEDC[NUM_CHANNELS] = {
    LEDC_CHANNEL_0, LEDC_CHANNEL_1, LEDC_CHANNEL_2, LEDC_CHANNEL_3,
    LEDC_CHANNEL_0, LEDC_CHANNEL_1, LEDC_CHANNEL_2, LEDC_CHANNEL_3
};

/* ---- 全局变量 ---- */
static EventGroupHandle_t s_wifi_event_group;
static int s_retry_num = 0;

/* ---- EVOKE 单次刺激: 每通道一个 esp_timer, 用于到时自动关闭 ---- */
static esp_timer_handle_t evoke_off_timers[NUM_CHANNELS];

/* ================================================================
 *  EVOKE 关闭回调: 由 esp_timer 在 duration 到期时触发
 *  arg = (intptr_t)idx, 即通道索引 (0..NUM_CHANNELS-1)
 * ================================================================ */
static void evoke_off_cb(void *arg)
{
    intptr_t idx = (intptr_t)arg;
    if (idx < 0 || idx >= NUM_CHANNELS) return;
    ledc_set_duty(CHANNEL_SPEED[idx], CHANNEL_LEDC[idx], 0);
    ledc_update_duty(CHANNEL_SPEED[idx], CHANNEL_LEDC[idx]);
}

/* ================================================================
 *  为每通道创建一个一次性 off-timer
 * ================================================================ */
static void evoke_timers_init(void)
{
    for (intptr_t i = 0; i < NUM_CHANNELS; i++) {
        const esp_timer_create_args_t args = {
            .callback = &evoke_off_cb,
            .arg      = (void *)i,
            .name     = "evoke_off",
        };
        ESP_ERROR_CHECK(esp_timer_create(&args, &evoke_off_timers[i]));
    }
    ESP_LOGI(TAG, "Evoke off-timers ready (%d channels)", NUM_CHANNELS);
}

/* ================================================================
 *  EDGE LOGGING — Path A: GPIO ANYEDGE ISR -> RingBuffer -> UDP
 *  用途: 把 LEDC 真实驱动的每个 on/off 边沿打戳上报, 让 PC 端获得
 *  per-cycle 时间真值, 而不只是首个 onset.
 * ================================================================ */

/* 单条边沿事件 — 12 字节 */
typedef struct {
    int64_t ts_us;     /* esp_timer_get_time() 在 ISR 中的捕获值 */
    uint8_t ch;        /* 通道索引 0..NUM_CHANNELS-1 */
    uint8_t level;     /* GPIO 电平 0/1 */
    uint8_t _pad[2];
} edge_event_t;

#define EDGE_RINGBUF_SIZE    (8 * 1024)   /* 装得下 ~680 事件 */
#define EDGE_PACK_MAX        50           /* 单 UDP 包最多事件数, 留足 MTU 余量 */
#define EDGE_DRAIN_TIMEOUT_MS 50          /* 首事件等待上限, 决定打包延迟 */

static RingbufHandle_t edge_ringbuf = NULL;
static volatile bool   edge_log_enabled = false;
static uint32_t        edge_pkt_seq = 0;
static volatile uint32_t edge_drop_count = 0;  /* ringbuf 溢出计数 */

/* ----------------------------------------------------------------
 *  IRAM ISR — 任何 GPIO 边沿都进这里, 只做时间戳 + 入队
 *  禁止调用任何非 ISR-safe 的 API (UART log, lwIP, malloc 等)
 * ---------------------------------------------------------------- */
static void IRAM_ATTR edge_isr(void *arg)
{
    intptr_t idx = (intptr_t)arg;
    int64_t t = esp_timer_get_time();
    int level = gpio_get_level(CHANNEL_PINS[idx]);

    edge_event_t ev = {
        .ts_us = t,
        .ch    = (uint8_t)idx,
        .level = (uint8_t)(level & 0x1),
    };

    BaseType_t hp_woken = pdFALSE;
    if (xRingbufferSendFromISR(edge_ringbuf, &ev, sizeof(ev), &hp_woken) != pdTRUE) {
        edge_drop_count++;
    }
    if (hp_woken == pdTRUE) {
        portYIELD_FROM_ISR();
    }
}

/* ----------------------------------------------------------------
 *  启用/停用边沿日志
 *  启用: 给所有通道 GPIO 加 INPUT 缓冲 + ANYEDGE 中断 + ISR handler
 *  停用: 仅移除 ISR handler (保留 INPUT 缓冲, 不影响 LEDC 输出)
 * ---------------------------------------------------------------- */
static esp_err_t edge_log_enable_all(void)
{
    if (edge_log_enabled) return ESP_OK;
    for (int i = 0; i < NUM_CHANNELS; i++) {
        /* LEDC 已把 GPIO 设为 OUTPUT 并通过矩阵驱动; 这里加 INPUT 不破坏 LEDC 路由 */
        gpio_set_direction(CHANNEL_PINS[i], GPIO_MODE_INPUT_OUTPUT);
        gpio_set_intr_type(CHANNEL_PINS[i], GPIO_INTR_ANYEDGE);
        esp_err_t err = gpio_isr_handler_add(CHANNEL_PINS[i],
                                             edge_isr,
                                             (void *)(intptr_t)i);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "gpio_isr_handler_add(CH%d) -> %s", i + 1, esp_err_to_name(err));
            return err;
        }
    }
    edge_drop_count = 0;
    edge_log_enabled = true;
    ESP_LOGI(TAG, "EDGE_LOG enabled (8 channels, ANYEDGE)");
    return ESP_OK;
}

static esp_err_t edge_log_disable_all(void)
{
    if (!edge_log_enabled) return ESP_OK;
    for (int i = 0; i < NUM_CHANNELS; i++) {
        gpio_isr_handler_remove(CHANNEL_PINS[i]);
        gpio_set_intr_type(CHANNEL_PINS[i], GPIO_INTR_DISABLE);
    }
    edge_log_enabled = false;
    ESP_LOGI(TAG, "EDGE_LOG disabled");
    return ESP_OK;
}

/* ----------------------------------------------------------------
 *  Edge sender 任务 — 从 ringbuf 拉事件, 打包成 ASCII UDP 广播给 PC
 *  报文格式: EDGES,<seq>,<count>,<ch>,<lv>,<us>[,<ch>,<lv>,<us>]...
 *  目的地址: 255.255.255.255:5006 (PC 用同一 socket 监听 HELLO + EDGES)
 * ---------------------------------------------------------------- */
static void edge_sender_task(void *arg)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) {
        ESP_LOGE(TAG, "edge_sender: socket failed errno=%d", errno);
        vTaskDelete(NULL);
        return;
    }
    int bcast = 1;
    setsockopt(sock, SOL_SOCKET, SO_BROADCAST, &bcast, sizeof(bcast));

    struct sockaddr_in dst = {
        .sin_family = AF_INET,
        .sin_port   = htons(PC_PORT),
    };
    inet_aton(PC_IP, &dst.sin_addr);

    char buf[1400];

    while (1) {
        /* 阻塞等首个事件, 超时则空转 — 这样 edge_log 关时本任务几乎零开销 */
        size_t item_size = 0;
        edge_event_t *first = (edge_event_t *)xRingbufferReceive(
            edge_ringbuf, &item_size, pdMS_TO_TICKS(EDGE_DRAIN_TIMEOUT_MS));
        if (first == NULL) continue;

        edge_event_t batch[EDGE_PACK_MAX];
        batch[0] = *first;
        vRingbufferReturnItem(edge_ringbuf, first);
        int count = 1;

        /* 不再等, 把当前积压一次性榨干 (至多 EDGE_PACK_MAX) */
        while (count < EDGE_PACK_MAX) {
            edge_event_t *e = (edge_event_t *)xRingbufferReceive(
                edge_ringbuf, &item_size, 0);
            if (e == NULL) break;
            batch[count++] = *e;
            vRingbufferReturnItem(edge_ringbuf, e);
        }

        int len = snprintf(buf, sizeof(buf), "EDGES,%" PRIu32 ",%d",
                           edge_pkt_seq++, count);
        for (int i = 0; i < count && len < (int)sizeof(buf) - 40; i++) {
            len += snprintf(buf + len, sizeof(buf) - len,
                            ",%u,%u,%" PRId64,
                            (unsigned)(batch[i].ch + 1),  /* 1-indexed for PC */
                            (unsigned)batch[i].level,
                            batch[i].ts_us);
        }

        sendto(sock, buf, len, 0, (struct sockaddr *)&dst, sizeof(dst));
    }
}

/* ================================================================
 *  LEDC 初始化: 8 个定时器 + 8 个通道, 50% 占空比方波
 * ================================================================ */
static void ledc_init_all(void)
{
    for (int i = 0; i < NUM_CHANNELS; i++) {
        ledc_timer_config_t timer_conf = {
            .speed_mode      = CHANNEL_SPEED[i],
            .duty_resolution = LEDC_DUTY_RES,
            .timer_num       = CHANNEL_TIMER[i],
            .freq_hz         = channel_freq[i],
            .clk_cfg         = LEDC_AUTO_CLK
        };
        ESP_ERROR_CHECK(ledc_timer_config(&timer_conf));

        ledc_channel_config_t ch_conf = {
            .speed_mode = CHANNEL_SPEED[i],
            .channel    = CHANNEL_LEDC[i],
            .timer_sel  = CHANNEL_TIMER[i],
            .intr_type  = LEDC_INTR_DISABLE,
            .gpio_num   = CHANNEL_PINS[i],
            .duty       = LEDC_DUTY_50PCT,
            .hpoint     = 0
        };
        ESP_ERROR_CHECK(ledc_channel_config(&ch_conf));
    }

    ESP_LOGI(TAG, "LEDC initialized: %d channels, %lu-%lu Hz, 50%% duty",
             NUM_CHANNELS, channel_freq[0], channel_freq[NUM_CHANNELS - 1]);
}

/* ================================================================
 *  向 PC 广播上线通知 (含自身 IP 和当前频率)
 * ================================================================ */
static void send_announce(void)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) return;

    int broadcast = 1;
    setsockopt(sock, SOL_SOCKET, SO_BROADCAST, &broadcast, sizeof(broadcast));

    /* 获取自身 IP */
    esp_netif_ip_info_t ip_info;
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_get_ip_info(netif, &ip_info);

    char msg[128];
    int len = snprintf(msg, sizeof(msg),
        "HELLO,SSVEP," IPSTR ",%d,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu",
        IP2STR(&ip_info.ip), UDP_PORT,
        channel_freq[0], channel_freq[1], channel_freq[2], channel_freq[3],
        channel_freq[4], channel_freq[5], channel_freq[6], channel_freq[7]);

    struct sockaddr_in dest = {
        .sin_family = AF_INET,
        .sin_port   = htons(PC_PORT),
    };
    inet_aton(PC_IP, &dest.sin_addr);

    sendto(sock, msg, len, 0, (struct sockaddr *)&dest, sizeof(dest));
    close(sock);

    ESP_LOGI(TAG, "Announce sent to %s:%d -> %s", PC_IP, PC_PORT, msg);
}

/* ================================================================
 *  WiFi 事件处理
 * ================================================================ */
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_CONNECTED) {
        /* 静态 IP 模式: L2 连接即可用, 无需等 DHCP */
        s_retry_num = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        ESP_LOGI(TAG, "WiFi L2 connected (static IP mode)");
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry_num < WIFI_MAX_RETRY) {
            esp_wifi_connect();
            s_retry_num++;
            ESP_LOGW(TAG, "WiFi disconnected, retry %d/%d", s_retry_num, WIFI_MAX_RETRY);
        } else {
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
            ESP_LOGE(TAG, "WiFi connection failed after %d retries", WIFI_MAX_RETRY);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

/* ================================================================
 *  静态 IP 配置 (Windows 11 热点网段 192.168.137.x)
 * ================================================================ */
#define STATIC_IP       "192.168.137.100"
#define STATIC_GW       "192.168.137.1"
#define STATIC_NETMASK  "255.255.255.0"

/* ================================================================
 *  WiFi STA 模式初始化 (硬编码 SSID/密码 + 静态 IP)
 * ================================================================ */
static void wifi_init_sta(void)
{
    s_wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_t *sta_netif = esp_netif_create_default_wifi_sta();

    /* 关闭 DHCP 客户端, 使用静态 IP */
    ESP_ERROR_CHECK(esp_netif_dhcpc_stop(sta_netif));
    esp_netif_ip_info_t ip_info = {0};
    ip4addr_aton(STATIC_IP,      (ip4_addr_t *)&ip_info.ip);
    ip4addr_aton(STATIC_GW,      (ip4_addr_t *)&ip_info.gw);
    ip4addr_aton(STATIC_NETMASK, (ip4_addr_t *)&ip_info.netmask);
    ESP_ERROR_CHECK(esp_netif_set_ip_info(sta_netif, &ip_info));
    ESP_LOGI(TAG, "Static IP: %s  GW: %s", STATIC_IP, STATIC_GW);

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t h_any_id;
    esp_event_handler_instance_t h_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, &h_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &h_got_ip));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASS,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
            .sae_pwe_h2e = WPA3_SAE_PWE_BOTH,
            .sae_h2e_identifier = "",
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "WiFi STA: connecting to %s ...", WIFI_SSID);

    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
        pdFALSE, pdFALSE, pdMS_TO_TICKS(15000));  /* 15 秒超时 */

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "Connected to %s with IP %s", WIFI_SSID, STATIC_IP);
        send_announce();
    } else if (bits & WIFI_FAIL_BIT) {
        ESP_LOGE(TAG, "Failed to connect to %s (LEDC output continues)", WIFI_SSID);
    } else {
        ESP_LOGW(TAG, "WiFi timeout — LEDC continues, will retry in background.");
    }
}

/* ================================================================
 *  UDP 服务器任务 — 接收命令
 *  协议 (统一文本格式, 见 CLAUDE.md):
 *    FREQ,<ch 1..8>,<hz 0..100>             -> OK,FREQ,<ch>,<hz>,<onset_us>
 *    EVOKE,<ch 1..8>,<duration_ms 1..10000> -> OK,EVOKE,<ch>,<ms>,<onset_us>
 *    SYNC,<pc_send_us>                      -> OK,SYNC,<pc_send_us>,<esp_recv_us>,<esp_send_us>
 *  错误:
 *    ERR,PARSE | ERR,CHANNEL | ERR,RANGE | ERR,LEDC
 *  所有 us 时间戳均为 esp_timer_get_time() 自启动以来的 int64 微秒计数,
 *  PC 端通过 SYNC 计算 ESP↔PC 时间偏移以消除无线传输抖动。
 * ================================================================ */
static void udp_server_task(void *pvParameters)
{
    char rx_buf[UDP_BUF_SIZE];
    struct sockaddr_in server_addr = {
        .sin_addr.s_addr = htonl(INADDR_ANY),
        .sin_family      = AF_INET,
        .sin_port        = htons(UDP_PORT),
    };

    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) {
        ESP_LOGE(TAG, "UDP socket creation failed: errno %d", errno);
        vTaskDelete(NULL);
        return;
    }

    if (bind(sock, (struct sockaddr *)&server_addr, sizeof(server_addr)) < 0) {
        ESP_LOGE(TAG, "UDP bind failed: errno %d", errno);
        close(sock);
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "UDP server listening on port %d", UDP_PORT);

    while (1) {
        struct sockaddr_in client_addr;
        socklen_t addr_len = sizeof(client_addr);

        int len = recvfrom(sock, rx_buf, UDP_BUF_SIZE - 1, 0,
                           (struct sockaddr *)&client_addr, &addr_len);
        /* 关键: 紧接 recvfrom 解阻塞后捕获时间戳, 用于 SYNC 的 t_recv */
        int64_t t_recv = esp_timer_get_time();
        if (len < 0) {
            ESP_LOGE(TAG, "recvfrom failed: errno %d", errno);
            continue;
        }

        rx_buf[len] = '\0';
        ESP_LOGI(TAG, "UDP recv: \"%s\"", rx_buf);

        char ack[96];
        int ch = 0, val = 0;
        unsigned long long pc_us = 0;

        if (sscanf(rx_buf, "FREQ,%d,%d", &ch, &val) == 2) {
            /* --- FREQ: 设置/关闭通道连续 PWM --- */
            if (ch < 1 || ch > NUM_CHANNELS) {
                const char *resp = "ERR,CHANNEL";
                sendto(sock, resp, strlen(resp), 0, (struct sockaddr *)&client_addr, addr_len);
                ESP_LOGW(TAG, "FREQ: invalid channel %d", ch);
            } else if (val < 0 || val > 100) {
                const char *resp = "ERR,RANGE";
                sendto(sock, resp, strlen(resp), 0, (struct sockaddr *)&client_addr, addr_len);
                ESP_LOGW(TAG, "FREQ: invalid hz %d (0..100)", val);
            } else {
                int idx = ch - 1;
                esp_err_t err = ESP_OK;
                /* 任何 FREQ 设置都先取消未到期的 EVOKE off-timer, 避免被自动关闭 */
                esp_timer_stop(evoke_off_timers[idx]);
                if (val == 0) {
                    err = ledc_set_duty(CHANNEL_SPEED[idx], CHANNEL_LEDC[idx], 0);
                    if (err == ESP_OK)
                        err = ledc_update_duty(CHANNEL_SPEED[idx], CHANNEL_LEDC[idx]);
                } else {
                    err = ledc_set_freq(CHANNEL_SPEED[idx], CHANNEL_TIMER[idx], (uint32_t)val);
                    if (err == ESP_OK) {
                        ledc_set_duty(CHANNEL_SPEED[idx], CHANNEL_LEDC[idx], LEDC_DUTY_50PCT);
                        ledc_update_duty(CHANNEL_SPEED[idx], CHANNEL_LEDC[idx]);
                    }
                }
                int64_t t_onset = esp_timer_get_time();
                if (err == ESP_OK) {
                    channel_freq[idx] = (uint32_t)val;
                    int ack_len = snprintf(ack, sizeof(ack),
                        "OK,FREQ,%d,%d,%" PRId64, ch, val, t_onset);
                    sendto(sock, ack, ack_len, 0, (struct sockaddr *)&client_addr, addr_len);
                    ESP_LOGI(TAG, "FREQ CH%d -> %d Hz @ %" PRId64 " us", ch, val, t_onset);
                } else {
                    const char *resp = "ERR,LEDC";
                    sendto(sock, resp, strlen(resp), 0, (struct sockaddr *)&client_addr, addr_len);
                    ESP_LOGE(TAG, "FREQ ledc failed: %s", esp_err_to_name(err));
                }
            }
        } else if (sscanf(rx_buf, "EVOKE,%d,%d", &ch, &val) == 2) {
            /* --- EVOKE: 单次刺激, 当前频率持续 val 毫秒后自动关闭 --- */
            if (ch < 1 || ch > NUM_CHANNELS) {
                const char *resp = "ERR,CHANNEL";
                sendto(sock, resp, strlen(resp), 0, (struct sockaddr *)&client_addr, addr_len);
                ESP_LOGW(TAG, "EVOKE: invalid channel %d", ch);
            } else if (val < 1 || val > 10000) {
                const char *resp = "ERR,RANGE";
                sendto(sock, resp, strlen(resp), 0, (struct sockaddr *)&client_addr, addr_len);
                ESP_LOGW(TAG, "EVOKE: invalid duration %d ms (1..10000)", val);
            } else {
                int idx = ch - 1;
                /* 若有未到期 off-timer, 先停, 再以新 duration 重启 */
                esp_timer_stop(evoke_off_timers[idx]);
                esp_err_t err = ledc_set_duty(CHANNEL_SPEED[idx], CHANNEL_LEDC[idx], LEDC_DUTY_50PCT);
                if (err == ESP_OK)
                    err = ledc_update_duty(CHANNEL_SPEED[idx], CHANNEL_LEDC[idx]);
                int64_t t_onset = esp_timer_get_time();
                if (err == ESP_OK) {
                    esp_timer_start_once(evoke_off_timers[idx], (uint64_t)val * 1000ULL);
                    int ack_len = snprintf(ack, sizeof(ack),
                        "OK,EVOKE,%d,%d,%" PRId64, ch, val, t_onset);
                    sendto(sock, ack, ack_len, 0, (struct sockaddr *)&client_addr, addr_len);
                    ESP_LOGI(TAG, "EVOKE CH%d %d ms @ %" PRId64 " us", ch, val, t_onset);
                } else {
                    const char *resp = "ERR,LEDC";
                    sendto(sock, resp, strlen(resp), 0, (struct sockaddr *)&client_addr, addr_len);
                    ESP_LOGE(TAG, "EVOKE ledc failed: %s", esp_err_to_name(err));
                }
            }
        } else if (sscanf(rx_buf, "SYNC,%llu", &pc_us) == 1) {
            /* --- SYNC: PC 时间对齐, 回送 t_recv 与 t_send --- */
            int64_t t_send = esp_timer_get_time();
            int ack_len = snprintf(ack, sizeof(ack),
                "OK,SYNC,%llu,%" PRId64 ",%" PRId64, pc_us, t_recv, t_send);
            sendto(sock, ack, ack_len, 0, (struct sockaddr *)&client_addr, addr_len);
            ESP_LOGD(TAG, "SYNC pc=%llu recv=%" PRId64 " send=%" PRId64, pc_us, t_recv, t_send);
        } else if (strcmp(rx_buf, "EDGE_LOG,ON") == 0) {
            /* --- EDGE_LOG,ON: 启用每边沿日志, 后续 EDGES 包广播到 PC:5006 --- */
            esp_err_t err = edge_log_enable_all();
            int ack_len = snprintf(ack, sizeof(ack),
                err == ESP_OK ? "OK,EDGE_LOG,ON" : "ERR,EDGE_LOG");
            sendto(sock, ack, ack_len, 0, (struct sockaddr *)&client_addr, addr_len);
        } else if (strcmp(rx_buf, "EDGE_LOG,OFF") == 0) {
            /* --- EDGE_LOG,OFF: 停用日志 --- */
            edge_log_disable_all();
            int ack_len = snprintf(ack, sizeof(ack),
                "OK,EDGE_LOG,OFF,%" PRIu32, edge_drop_count);  /* 同时回报丢弃计数 */
            sendto(sock, ack, ack_len, 0, (struct sockaddr *)&client_addr, addr_len);
        } else {
            const char *resp = "ERR,PARSE";
            sendto(sock, resp, strlen(resp), 0, (struct sockaddr *)&client_addr, addr_len);
        }
    }

    close(sock);
    vTaskDelete(NULL);
}

/* ================================================================
 *  主入口
 * ================================================================ */
void app_main(void)
{
    /* 1. NVS — WiFi 需要 */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* 2. LEDC — 立即开始 PWM 输出, 零 CPU 开销 */
    ledc_init_all();

    /* 2.1 EVOKE 单次刺激 off-timer (8 个一次性定时器) */
    evoke_timers_init();

    /* 2.2 边沿日志: ringbuf + GPIO ISR 服务 + 发送任务 (默认禁用, EDGE_LOG,ON 启用) */
    edge_ringbuf = xRingbufferCreate(EDGE_RINGBUF_SIZE, RINGBUF_TYPE_NOSPLIT);
    if (edge_ringbuf == NULL) {
        ESP_LOGE(TAG, "RingBuffer create failed — EDGE_LOG will be disabled");
    } else {
        ESP_ERROR_CHECK(gpio_install_isr_service(ESP_INTR_FLAG_IRAM));
        xTaskCreatePinnedToCore(edge_sender_task, "edge_tx", 3072, NULL, 3, NULL, 0);
        ESP_LOGI(TAG, "Edge logging infrastructure ready (use EDGE_LOG,ON to start)");
    }

    /* 3. WiFi — 可能阻塞数秒, LEDC 已在运行 */
    wifi_init_sta();

    /* 4. UDP 服务器 — Core 0, 低优先级, 不影响任何硬件输出 */
    xTaskCreatePinnedToCore(udp_server_task, "udp_srv", 4096, NULL, 2, NULL, 0);

    ESP_LOGI(TAG, "System running: %d-ch LEDC PWM + UDP control on port %d", NUM_CHANNELS, UDP_PORT);

    /* 心跳日志 + 周期性广播 (DHCP 可能延迟完成) */
    for (;;) {
        ESP_LOGI(TAG, "freq: %lu %lu %lu %lu %lu %lu %lu %lu Hz",
                 channel_freq[0], channel_freq[1], channel_freq[2], channel_freq[3],
                 channel_freq[4], channel_freq[5], channel_freq[6], channel_freq[7]);

        /* 如果已获取 IP，每轮都重发 announce 让 PC 发现 */
        esp_netif_ip_info_t ip_info;
        esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        if (netif && esp_netif_get_ip_info(netif, &ip_info) == ESP_OK
            && ip_info.ip.addr != 0) {
            send_announce();
        }

        vTaskDelay(pdMS_TO_TICKS(10000));
    }
}
