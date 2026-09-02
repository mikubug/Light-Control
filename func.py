import machine
import network
import time
from umqtt.simple import MQTTClient
from config import *
from log_manager import write_log, sync_ntp_time

# 全局缓存：四路灯光本地状态 [L1,L2,L3,L4] 0关 1开
light_status = [0, 0, 0, 0]
# IO对象缓存
relay_objs = []
key_objs = []
mqtt_client = None
wlan = None

# ===================== IO初始化 =====================
def io_init():
    global relay_objs, key_objs
    relay_objs.clear()
    key_objs.clear()
    # 初始化继电器输出，默认高电平=关灯（低电平开灯）
    for pin_num in RELAY_PINS:
        pin = machine.Pin(pin_num, machine.Pin.OUT, value=1)
        relay_objs.append(pin)
    # 初始化按键，下拉输入，按下为高电平
    for pin_num in KEY_PINS:
        pin = machine.Pin(pin_num, machine.Pin.IN, machine.Pin.PULL_DOWN)
        key_objs.append(pin)

# ===================== 灯光硬件控制 =====================
def set_light(light_idx: int, state: int):
    """
    控制单路灯光硬件
    :param light_idx: 0~3 对应L1~L4
    :param state: 0关 1开（上层逻辑不变）
    """
    global light_status
    if 0 <= light_idx < 4:
        light_status[light_idx] = state
        # 低电平开灯：state=1 → 输出0；state=0 → 输出1
        hw_out = 0 if state == 1 else 1
        relay_objs[light_idx].value(hw_out)

# ===================== WIFI连接管理 =====================
def wifi_connect():
    print("wifi")
    global wlan
    if wlan is None:
        print("wlan is none")
        wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():        
        print(f"WiFi连接成功 IP:{wlan.ifconfig()[0]}")
        return True
    write_log("INFO", EVENT_WIFI_CONNECT, f"开始连接WiFi {WIFI_SSID}")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    timeout = 0
    while not wlan.isconnected() and timeout < 20:
        time.sleep(0.5)
        timeout += 1
    if wlan.isconnected():
        print("wlan.isconnected")
        ip = wlan.ifconfig()[0]
        write_log("INFO", EVENT_WIFI_CONNECT, f"WiFi连接成功 IP:{ip}")
        sync_ntp_time() # WiFi连上同步时间
        return True
    else:
        write_log("ERROR", EVENT_WIFI_DISCONNECT, "WiFi连接超时失败")
        return False
    
def wifi_is_online():
    if wlan is None:
        return False
    return wlan.isconnected()
# ===================== MQTT回调：平台下发指令处理 =====================
def mqtt_callback(topic, msg):
    global light_status
    topic_str = topic.decode()
    msg_str = msg.decode()
    write_log("INFO", EVENT_CLOUD_CTRL, f"云端下发指令 主题:{topic_str} 指令:{msg_str}")
    cmd = int(msg_str) if msg_str.isdigit() else 0
    # 匹配对应灯光通道
    for idx, t in enumerate(LIGHT_TOPICS):
        if t.encode() == topic:
            set_light(idx, cmd)
            write_log("INFO", EVENT_LIGHT_SWITCH, f"L{idx+1} 云端控制切换至 {cmd}")
            break

# ===================== MQTT客户端创建（带遗嘱消息） =====================
def create_mqtt_client():
    global mqtt_client
    try:
        client = MQTTClient(
            client_id=MQTT_CLIENT_ID,
            server=MQTT_BROKER,
            port=MQTT_PORT,
            user=MQTT_USER,
            password=MQTT_PWD,
            keepalive=MQTT_KEEPALIVE  # 手动开启心跳保活
        )
        # 只设置1条统一遗嘱，不再循环四路主题
        client.set_last_will(WILL_ALL_TOPIC, WILL_MSG, WILL_QOS, WILL_RETAIN)
        client.set_callback(mqtt_callback)
        mqtt_client = client
        return True
    except Exception as e:
        write_log("ERROR", EVENT_MIXIO_DISCONNECT, f"创建MQTT客户端失败:{str(e)}")
        mqtt_client = None
        return False

# ===================== 连接MixIO MQTT平台 =====================
def mqtt_connect():
    global mqtt_client
    if mqtt_client is None:
        if not create_mqtt_client():
            return False
    try:
        # 1. TCP握手连接Broker
        mqtt_client.connect()
        write_log("INFO", EVENT_MIXIO_CONNECT, "MixIO MQTT TCP握手完成")
    except OSError as e:
        err_msg = f"TCP握手失败 错误码:{e.args[0]}"
        write_log("ERROR", EVENT_MIXIO_DISCONNECT, err_msg)
        mqtt_client = None
        return False

    # 2. 订阅主题单独捕获异常
    try:
        time.sleep_ms(80)
    # 循环订阅四路灯光组件主题
        for topic in LIGHT_TOPICS:
            print(topic)
            mqtt_client.subscribe(topic)#, qos=1)
            #write_log("INFO", EVENT_MIXIO_CONNECT, f"已订阅主题: {topic}")
            time.sleep_ms(30)  # 每条订阅间隔，防止平台限流断开
    except Exception as e:
        err_msg = f"订阅主题失败:{str(e)}"
        write_log("ERROR", EVENT_MIXIO_DISCONNECT, err_msg)
        try:
            mqtt_client.disconnect()
        except:
            pass
        mqtt_client = None
        return False

    # 3. 同步云端状态
    try:
        if QUERY_PLATFORM_STATE_ON_CONNECT:
            query_all_platform_state()
    except Exception as e:
        write_log("WARN", EVENT_MIXIO_CONNECT, f"同步云端状态异常:{str(e)}")

    write_log("INFO", EVENT_MIXIO_CONNECT, "MixIO平台完全就绪，可收发指令")
    return True

def mqtt_is_connected():
    if mqtt_client is None:
        return False
    try:
        mqtt_client.ping()
        return True
    except:
        return False

def mqtt_check_msg():
    """轮询MQTT消息，阻塞极短时间"""
    if mqtt_is_connected():
        try:
            mqtt_client.check_msg()
        except:
            pass

# ===================== 主动上报本地灯光状态到MixIO =====================
def publish_light_state(light_idx: int):
    if mqtt_is_connected() and 0 <= light_idx < 4:
        topic = LIGHT_TOPICS[light_idx]
        state = str(light_status[light_idx]).encode()
        try:
            mqtt_client.publish(topic, state, qos=1, retain=True)
        except:
            pass

def query_all_platform_state():
    """
    连上平台后查询云端状态，本项目MixIO组件为按钮型，
    原理：平台保留retain消息，订阅后会自动推送最新状态，
    无需额外查询指令，此处等待1s接收平台留存状态同步硬件
    """
    print("等待同步MixIO云端灯光状态...")
    wait_start = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), wait_start) < 1000:
        mqtt_check_msg()
        time.sleep(0.05)

# ===================== 物理按键扫描消抖 =====================
key_last_state = [0,0,0,0]
key_trigger_flag = [False,False,False,False]

def scan_key():
    global key_last_state, key_trigger_flag
    for idx in range(4):
        current = key_objs[idx].value()
        # 下降沿触发：按下翻转灯光
        if current == 1 and key_last_state[idx] == 0:
            time.sleep(0.02) # 消抖
            if key_objs[idx].value() == 1:
                key_trigger_flag[idx] = True
        key_last_state[idx] = current
    # 处理按键触发
    for idx in range(4):
        if key_trigger_flag[idx]:
            key_trigger_flag[idx] = False
            # 翻转本地灯光状态
            new_state = 1 - light_status[idx]
            set_light(idx, new_state)
            # 同步更新到MixIO平台
            publish_light_state(idx)
            write_log("INFO", EVENT_KEY_TRIGGER, f"物理按键 L{idx+1} 触发，灯光切换为 {new_state}")