# ========== WIFI 配置 ==========
WIFI_SSID = "TGG-313"
WIFI_PASSWORD = "wmywbyt!"
WIFI_RECONNECT_INTERVAL = 5  # 断网重连检测间隔 秒

# ========== MixIO MQTT 平台参数 ==========
MQTT_BROKER = "172.70.80.7"  # 米思齐MixIO标准MQTT地址
MQTT_PORT = 1883
#MQTT_CLIENT_ID = "ESP32S3_TGG313_LIGHT"#终端ID
import machine
# 读取设备MAC地址作为唯一后缀
mac_raw = machine.unique_id()
mac_hex = "".join(f"{b:02X}" for b in mac_raw)
# 全局唯一客户端ID，避免多设备/重启互踢
MQTT_CLIENT_ID = f"ESP32S3_LIGHT_{mac_hex}"
MQTT_USER = "33014912@qq.com"#用户名
MQTT_PWD = "2d2b339778a6d6f5f448aa6b7c3d05b1"#密码
PROJECT_NAME = "ZM_LOT"

# ===================== 日志系统配置 =====================
LOG_ROOT_PATH = "/log"
LOG_DAYS_KEEP = 7               # 保留7天日志
LOG_FILE_MAX_SIZE = 1024 * 20   # 单日志文件最大20KB，超出分片
LOG_LEVEL = "INFO"              # 日志等级 INFO/WARN/ERROR
# 日志事件类型标识
EVENT_WIFI_CONNECT = "WIFI_CONNECT"
EVENT_WIFI_DISCONNECT = "WIFI_DISCONNECT"
EVENT_MIXIO_CONNECT = "MIXIO_CONNECT"
EVENT_MIXIO_DISCONNECT = "MIXIO_DISCONNECT"
EVENT_KEY_TRIGGER = "KEY_PRESS"
EVENT_CLOUD_CTRL = "CLOUD_CTRL"
EVENT_LIGHT_SWITCH = "LIGHT_CHANGE"
EVENT_OTA_START = "OTA_START"
EVENT_OTA_FINISH = "OTA_FINISH"
EVENT_SYS_RESET = "SYS_RESET"

# 四路灯光主题列表，顺序 L1~L4
LIGHT_TOPICS = [
    f"{MQTT_USER}/{PROJECT_NAME}/TGG313_L1",
    f"{MQTT_USER}/{PROJECT_NAME}/TGG313_L2",
    f"{MQTT_USER}/{PROJECT_NAME}/TGG313_L3",
    f"{MQTT_USER}/{PROJECT_NAME}/TGG313_L4"
]

# 统一遗嘱主题（单条遗嘱，解决umqtt多遗嘱报错）
WILL_ALL_TOPIC = f"{MQTT_USER}/{PROJECT_NAME}/ALL_LIGHT_WILL"
# 遗嘱消息：离线时所有灯置0关灯
WILL_MSG = b"0"
WILL_QOS = 1
WILL_RETAIN = False
# MQTT心跳保活延长至60秒
MQTT_KEEPALIVE = 60

# ========== 硬件引脚配置 ==========
# 继电器输出引脚 [L1, L2, L3, L4]
RELAY_PINS = [9, 10, 43, 44]
# 物理按键输入引脚 [KEY1, KEY2, KEY3, KEY4]
KEY_PINS = [17, 18, 21, 38]

# ========== 业务运行参数 ==========
KEY_SCAN_INTERVAL = 0.2    # 按键扫描周期
MQTT_CHECK_INTERVAL = 1    # MQTT保活检查周期
QUERY_PLATFORM_STATE_ON_CONNECT = True  # 连上MQTT后同步云端状态

# 遗嘱统一主题（替代四路分别设置遗嘱）
WILL_ALL_TOPIC = f"{PROJECT_NAME}/ALL_LIGHT_WILL"
# NTP时区偏移 东八区+8小时
NTP_TIMEZONE_OFFSET = 8 * 3600
# MQTT心跳保活30秒
MQTT_KEEPALIVE = 30