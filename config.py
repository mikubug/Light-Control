# -*- coding: utf-8 -*-
"""
config.py —— 全局配置中心（教室灯光控制 v2）

约定：
1. 所有可调参数集中在本文件，其他模块只读取、不修改。
2. OTA 升级不覆盖本文件（见 OTA_FILE_LIST），避免凭据被远程改写。
3. 本文件必须能独立 import，不依赖任何其他业务模块。
"""

import machine

# ===================== 固件标识 =====================
FIRMWARE_VERSION = "2.0.0"
DEVICE_MODEL = "ZZ-ESP32-S3-Nano-EB-V20"

# 读取 MAC 作为设备唯一后缀
_mac_raw = machine.unique_id()
MAC_HEX = "".join(["%02X" % b for b in _mac_raw])

# ===================== WiFi：多 AP 冗余 =====================
# 按顺序尝试；运行期断线会在列表内自动漫游，全部失败才进入退避
WIFI_NETWORKS = [
    {"ssid": "TP-LINK_1937", "password": "wmywbyt!"},
    {"ssid": "TGG-313", "password": "wmywbyt!"},   # 原 AP，改名后保留作备用
    # {"ssid": "手机热点",   "password": "热点密码"},
]

WIFI_CONNECT_TIMEOUT = 15      # 单个 AP 的连接等待上限（秒，非阻塞分摊到主循环）
WIFI_RSSI_FLOOR = -85          # 信号低于此值视为弱网，主动重选 AP（dBm）
WIFI_RECHECK_INTERVAL = 5      # 在线巡检周期（秒）
WIFI_MAX_CONSECUTIVE_FAIL = 3  # 连续失败达此值 → 重建网卡（active(False/True)）
WIFI_REBUILD_COOLDOWN = 30     # 网卡重建后的静默期（秒）
WIFI_SCAN_ROAM_ENABLE = True   # 弱网时是否允许切换到下一个 AP

# ===================== MQTT：多 Broker 冗余 =====================
# 主 Broker 不可用时自动切换到下一个，全部轮完才退避
MQTT_BROKERS = [
    {"host": "172.70.80.7", "port": 1883},
    # {"host": "备用Broker地址", "port": 1883},
]

MQTT_USER = "33014912@qq.com"
MQTT_PWD = "2d2b339778a6d6f5f448aa6b7c3d05b1"
PROJECT_NAME = "ZM_LOT"

# 全局唯一客户端 ID：以 MAC 结尾，避免多设备同名互踢
MQTT_CLIENT_ID = "ESP32S3_LIGHT_%s" % MAC_HEX

# ===================== 主题定义 =====================
TOPIC_PREFIX = "%s/%s" % (MQTT_USER, PROJECT_NAME)

# 四路灯光主题，顺序 L1~L4（与 RELAY_PINS / KEY_PINS 一一对应）
LIGHT_TOPICS = ["%s/TGG313_L%d" % (TOPIC_PREFIX, i + 1) for i in range(4)]

# 统一遗嘱主题：设备异常离线时，平台收到后置全关
WILL_TOPIC = "%s/ALL_LIGHT_WILL" % TOPIC_PREFIX
WILL_MSG = b"0"
WILL_QOS = 1
WILL_RETAIN = True

# 设备在线状态（上线发 1，与遗嘱 0 配套）
ONLINE_TOPIC = "%s/DEVICE_ONLINE" % TOPIC_PREFIX
ONLINE_QOS = 1
ONLINE_RETAIN = True

# 设备遥测：信号强度、内存、运行时长、固件版本
TELEMETRY_TOPIC = "%s/DEVICE_TELEMETRY" % TOPIC_PREFIX
TELEMETRY_INTERVAL = 60

# 全开 / 全关广播指令（冗余通道：单条指令控四路）
CMD_ALL_TOPIC = "%s/TGG313_ALL" % TOPIC_PREFIX

MQTT_KEEPALIVE = 60            # MQTT 协议层心跳（秒）
MQTT_PING_INTERVAL = 20        # 实际发送 PINGREQ 的最小间隔（秒）
MQTT_MSG_TIMEOUT = 5           # 判定"连接疑似假死"的静默时长（秒）
MQTT_MAX_CONSECUTIVE_FAIL = 3  # 连续失败达此值 → 切换到下一个 Broker
MQTT_RESUB_INTERVAL = 300      # 周期性重新订阅（秒），防止平台侧订阅丢失
MQTT_QOS = 1

# ===================== 重连退避策略 =====================
# 延迟 = min(BACKOFF_MIN * FACTOR^streak, BACKOFF_MAX) ± JITTER 抖动
BACKOFF_MIN = 2                # 首次重连等待（秒）
BACKOFF_MAX = 60               # 退避上限（秒）：链路恢复后最坏等这么久就能重连
BACKOFF_FACTOR = 2             # 指数底数
BACKOFF_JITTER = 0.3           # 抖动比例（0.3 = ±30%），避免多设备同时重连打爆 AP
BACKOFF_MAX_EXPONENT = 6       # 指数上限，防止数值溢出

# ===================== 状态自愈 =====================
HEARTBEAT_INTERVAL = 30        # 在线心跳上报周期（秒）
STATE_REPUBLISH_INTERVAL = 300 # 全量灯光状态重发周期（秒），纠正平台侧状态漂移
STATE_FILE = "/light_state.json"
RESTORE_STATE_ON_BOOT = False  # 上电是否恢复上次灯光状态（False=强制全关，教室场景更安全）
RELAY_REFRESH_INTERVAL = 10    # 周期性把缓存状态回写 GPIO（秒），抗干扰漂移

# ===================== 硬件引脚 =====================
RELAY_PINS = [9, 10, 43, 44]   # 继电器输出 [L1, L2, L3, L4]
KEY_PINS = [17, 18, 21, 38]    # 物理按键输入 [KEY1..KEY4]

RELAY_ACTIVE_LOW = True        # 继电器低电平吸合
KEY_PULL = machine.Pin.PULL_DOWN  # 按键下拉，按下为高电平
KEY_ACTIVE_HIGH = True         # 按键按下 = 高电平

KEY_SCAN_INTERVAL = 0.2        # 按键扫描周期（秒）
KEY_DEBOUNCE_MS = 25           # 消抖重采样延时（毫秒）

# ===================== 日志系统 =====================
# 注意：目录名绝对不能叫 "log"。MicroPython 中同名目录会遮蔽 log.py 模块，
# 一旦该目录被创建，下次重启 "from log import ..." 就会变成
# "no module named 'log.log_init'" 并导致崩溃循环。改名务必避开任何模块名。
LOG_ROOT_PATH = "/logs"
LOG_DAYS_KEEP = 7              # 日志保留天数
LOG_FILE_MAX_SIZE = 20 * 1024  # 单文件上限（字节），超出分片
LOG_MAX_FILES = 60             # 日志总文件数上限，超出删最旧（防止写满 flash）
LOG_LEVEL = "INFO"             # DEBUG / INFO / WARN / ERROR
LOG_WRITE_RETRY = 2            # 单次写入失败重试次数
LOG_MIN_YEAR = 2024            # 早于该年份视为时钟不可信（不执行按天清理）
LOG_FLUSH_LEVEL = "ERROR"      # 达到该等级的日志立即刷盘

# ===================== 时间同步 =====================
NTP_TIMEZONE_OFFSET = 8 * 3600 # 东八区
NTP_SYNC_INTERVAL = 3600       # 周期重同步（秒）
NTP_RETRY_INTERVAL = 300       # 同步失败后的重试间隔（秒）
NTP_TIMEOUT = 5                # NTP 请求超时（秒）
NTP_HOST = "ntp.aliyun.com"    # 国内 NTP 服务器，比默认 pool.ntp.org 稳

# ===================== 看门狗与资源管理 =====================
WDT_ENABLE = True
WDT_TIMEOUT_MS = 15000         # 看门狗超时（毫秒）；端口不支持会自动降级
WDT_FEED_INTERVAL = 2          # 喂狗间隔（秒）
WDT_FEED_IN_BLOCKING = True    # 阻塞式网络调用前后是否补喂

MEM_FREE_FLOOR = 24000         # 可用堆低于此值（字节）→ 主动 gc，连续告警则复位
MEM_CHECK_INTERVAL = 30        # 内存巡检周期（秒）
GC_INTERVAL = 60               # 主动垃圾回收周期（秒）
MEM_LOW_STREAK_RESET = 10      # 连续内存告警达此次数 → 复位

# ===================== 故障升级（降级链） =====================
# 重连 → 换 AP/Broker → 重建网卡 → 重建 MQTT 对象 → 复位 → 安全模式
NET_DOWN_RESET_THRESHOLD = 900 # 网络全断超过此秒数 → 自动复位（15 分钟）
NET_DEGRADED_THRESHOLD = 120   # 断网超过此秒数进入"降级模式"（拉长轮询周期省电）

# ===================== 崩溃保护与安全模式 =====================
SAFE_MODE_FLAG = "/SAFE_MODE"      # 存在该文件则只跑本地控制，不联网
CRASH_WINDOW_SEC = 300             # 上次存活低于此秒数视为"快速崩溃"（不依赖时钟）
CRASH_RESET_LIMIT = 5              # 窗口内重启达此次数 → 进入安全模式
CRASH_STATE_FILE = "/crash_state.json"
SAFE_MODE_RETRY_INTERVAL = 300     # 安全模式下尝试恢复联网的周期（秒）

# ===================== OTA =====================
OTA_SERVER = "http://192.168.1.100/esp32s3/"

# 白名单（服务器需放置同名文件）。两条硬性排除：
#   config.py —— 内含 WiFi/MQTT 凭据，可被远程改写 = 设备被劫持，永久排除
#   main.py   —— 回滚逻辑本身就住在 main.py 里，它一旦损坏就没人能执行回滚，
#                只能拆机刷机；需要更新时手动刷，不走 OTA
OTA_FILE_LIST = ["func.py", "log.py", "ota.py"]

OTA_VERSION_FILE = "version.json"  # 服务器侧版本清单；缺失时自动退化为逐字节内容比对
OTA_TIMEOUT = 8                    # 单次 HTTP 超时（秒）
OTA_MAX_RETRY = 3                  # 单文件下载重试次数
OTA_CHECK_INTERVAL = 600           # 升级检查周期（秒）
OTA_BACKUP_SUFFIX = ".bak"         # 旧文件备份后缀
OTA_TMP_SUFFIX = ".tmp"            # 下载中临时后缀
OTA_PENDING_FLAG = "/OTA_PENDING"  # 升级待确认标记，下次未确认则回滚
OTA_MIN_FILE_SIZE = 64             # 小于此大小视为下载失败（字节）
OTA_ENABLE = True                # 已确认 OTA 服务器可用

# ===================== 日志事件类型 =====================
EVENT_WIFI_CONNECT = "WIFI_CONNECT"
EVENT_WIFI_DISCONNECT = "WIFI_DISCONNECT"
EVENT_WIFI_ROAM = "WIFI_ROAM"
EVENT_MIXIO_CONNECT = "MIXIO_CONNECT"
EVENT_MIXIO_DISCONNECT = "MIXIO_DISCONNECT"
EVENT_MIXIO_FAILOVER = "MIXIO_FAILOVER"
EVENT_KEY_TRIGGER = "KEY_PRESS"
EVENT_CLOUD_CTRL = "CLOUD_CTRL"
EVENT_LIGHT_SWITCH = "LIGHT_CHANGE"
EVENT_STATE_RESTORE = "STATE_RESTORE"
EVENT_OTA_START = "OTA_START"
EVENT_OTA_FINISH = "OTA_FINISH"
EVENT_OTA_ROLLBACK = "OTA_ROLLBACK"
EVENT_SYS_RESET = "SYS_RESET"
EVENT_SYS_BOOT = "SYS_BOOT"
EVENT_WDT = "WATCHDOG"
EVENT_MEM_LOW = "MEM_LOW"
EVENT_SAFE_MODE = "SAFE_MODE"
EVENT_TIME_SYNC = "TIME_SYNC"
