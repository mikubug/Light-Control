import time
import machine
from light_func import *
from ota_upgrade import check_and_upgrade
from config import *
from log_manager import log_init, write_log

def main_loop():
    # 初始化日志系统（创建文件夹、清理7天前日志）
    log_init()
    write_log("INFO", EVENT_SYS_RESET, "ESP32-S3 教室灯光控制系统开机启动")
    # 1. 硬件初始化
    io_init()
    print("ESP32-S3 教室灯光控制系统启动完成")
    wifi_last_check = 0
    mqtt_last_check = 0
    key_last_scan = 0
    ota_check_cycle = 300  # 5分钟检查一次OTA更新
    mqtt_retry_cooldown = 0  # MQTT重连冷却计时变量

    while True:
        now_ms = time.ticks_ms()
        # ========== 1. WiFi保活检测 ==========
        if time.ticks_diff(now_ms, wifi_last_check) > WIFI_RECONNECT_INTERVAL * 1000:
            wifi_last_check = now_ms
            if not wifi_is_online():
                write_log("WARN", EVENT_WIFI_DISCONNECT, "WiFi离线，执行重连")
                wifi_connect()

        # ========== 2. MQTT平台保活 ==========
        if wifi_is_online() and time.ticks_diff(now_ms, mqtt_last_check) > MQTT_CHECK_INTERVAL * 1000:
            mqtt_last_check = now_ms
            # 重连冷却：3秒内不重复发起连接
            if time.ticks_diff(now_ms, mqtt_retry_cooldown) < 3000:
                continue
            if not mqtt_is_connected():
                write_log("WARN", EVENT_MIXIO_DISCONNECT, "MixIO MQTT离线，重新连接平台")
                mqtt_connect()
                mqtt_retry_cooldown = now_ms

        # 循环接收平台下发灯光指令
        mqtt_check_msg()

        # ========== 3. 物理按键扫描 ==========
        if time.ticks_diff(now_ms, key_last_scan) > KEY_SCAN_INTERVAL * 1000:
            key_last_scan = now_ms
            scan_key()

        # ========== 4. 定时OTA升级检测（可选） ==========
        if (now_ms // 1000) % ota_check_cycle == 30 and wifi_is_online():
            # check_and_upgrade()
            pass

        time.sleep(0.01)
if __name__ == "__main__":
    try:
        main_loop()
    except Exception as err:
        err_str = str(err)
        write_log("ERROR", EVENT_SYS_RESET, f"主程序异常触发重启:{err_str}")
        print("主程序异常重启:", err)
        time.sleep(3)
        machine.reset()