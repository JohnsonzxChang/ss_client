#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "driver/ledc.h"
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
 *  UDP 服务器任务 — 接收频率配置命令
 *  协议: "FREQ,<channel 1-8>,<hz 1-100>"
 *  响应: "OK,<ch>,<hz>" 或 "ERR,PARSE"
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
        if (len < 0) {
            ESP_LOGE(TAG, "recvfrom failed: errno %d", errno);
            continue;
        }

        rx_buf[len] = '\0';
        ESP_LOGI(TAG, "UDP recv: \"%s\"", rx_buf);

        int ch = 0, hz = 0;
        if (sscanf(rx_buf, "FREQ,%d,%d", &ch, &hz) == 2) {
            if (ch < 1 || ch > NUM_CHANNELS) {
                const char *resp = "ERR,CHANNEL";
                sendto(sock, resp, strlen(resp), 0,
                       (struct sockaddr *)&client_addr, addr_len);
                ESP_LOGW(TAG, "Invalid channel %d", ch);
            } else if (hz < 0 || hz > 100) {
                const char *resp = "ERR,RANGE";
                sendto(sock, resp, strlen(resp), 0,
                       (struct sockaddr *)&client_addr, addr_len);
                ESP_LOGW(TAG, "Invalid frequency %d Hz", hz);
            } else {
                int idx = ch - 1;
                esp_err_t err = ESP_OK;
                if (hz == 0) {
                    /* 关闭通道: 占空比设 0, GPIO 输出恒低 */
                    err = ledc_set_duty(CHANNEL_SPEED[idx], CHANNEL_LEDC[idx], 0);
                    if (err == ESP_OK)
                        err = ledc_update_duty(CHANNEL_SPEED[idx], CHANNEL_LEDC[idx]);
                } else {
                    /* 恢复/更改频率: 先设频率, 再恢复 50% 占空比 */
                    err = ledc_set_freq(CHANNEL_SPEED[idx],
                                        CHANNEL_TIMER[idx],
                                        (uint32_t)hz);
                    if (err == ESP_OK) {
                        ledc_set_duty(CHANNEL_SPEED[idx], CHANNEL_LEDC[idx], LEDC_DUTY_50PCT);
                        ledc_update_duty(CHANNEL_SPEED[idx], CHANNEL_LEDC[idx]);
                    }
                }
                if (err == ESP_OK) {
                    channel_freq[idx] = (uint32_t)hz;
                    char ack[32];
                    int ack_len = snprintf(ack, sizeof(ack), "OK,%d,%d", ch, hz);
                    sendto(sock, ack, ack_len, 0,
                           (struct sockaddr *)&client_addr, addr_len);
                    ESP_LOGI(TAG, "CH%d → %d Hz", ch, hz);
                } else {
                    const char *resp = "ERR,LEDC";
                    sendto(sock, resp, strlen(resp), 0,
                           (struct sockaddr *)&client_addr, addr_len);
                    ESP_LOGE(TAG, "ledc_set_freq failed: %s", esp_err_to_name(err));
                }
            }
        } else {
            const char *resp = "ERR,PARSE";
            sendto(sock, resp, strlen(resp), 0,
                   (struct sockaddr *)&client_addr, addr_len);
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
