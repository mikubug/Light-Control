# -*- coding: utf-8 -*-
"""
func.py —— 硬件驱动 + 网络冗余管理（main.py 中以 light_func 之名使用）

冗余设计主线：
1. WiFi 多 AP 轮转：主 AP 挂了自动换下一个，弱信号也会主动漫游。
2. MQTT 多 Broker 轮转：主 Broker 不可用时切换备机。
3. 指数退避 + 随机抖动重连：避免网络闪断时疯狂重连打爆设备，也避免多设备同
   时上线造成重连风暴。
4. 分级降级链：重连 → 换 AP/Broker → 重建网卡 → 重建 MQTT 对象 → 系统复位。
5. 状态本地持久化：掉电重启后灯光状态不丢。
6. 离线补发：断网期间的本地操作打标记，恢复上线后自动补报到平台。
7. 周期性状态重发 + 继电器状态回写：纠正平台侧状态漂移和 GPIO 干扰。
"""

import gc
import os
import time
import machine
import network
import random

try:
    import ujson as json
except ImportError:
    import json

from umqtt.simple import MQTTClient

from config import *
from log import write_log, sync_ntp_time, maybe_sync_ntp, rebind_daily_file

# ===================== 全局状态 =====================
light_status = [0, 0, 0, 0]      # 四路灯光逻辑状态 0关 1开
relay_objs = []                  # 继电器 Pin 对象
key_objs = []                    # 按键 Pin 对象
wlan = None
mqtt_client = None

# 网络状态机
NET_DOWN = 0        # WiFi 未连接
NET_WIFI_WAIT = 1   # 正在等待 WiFi 握手
NET_MQTT_WAIT = 2   # WiFi 已通，正在连接 MQTT
NET_ONLINE = 3      # 全链路就绪

_n = {
    "state": NET_DOWN,
    "wifi_idx": 0,
    "broker_idx": 0,
    "fail_streak": 0,        # 当前链路连续失败次数
    "wifi_fail_streak": 0,
    "next_try_ms": 0,        # 退避到期时间
    "wifi_deadline": 0,      # 单次 WiFi 握手超时
    "last_wifi_check": 0,
    "last_ping": 0,
    "last_rx": 0,            # 最近一次成功收到/发送数据的时间
    "last_resub": 0,
    "last_heartbeat": 0,
    "last_republish": 0,
    "last_telemetry": 0,
    "last_relay_refresh": 0,
    "last_roam": 0,
    "down_since": 0,
    "wifi_rebuild_at": 0,
    "online_since": 0,
    "wifi_up_since": 0,      # WiFi 最近一次恢复的时刻
    "backoff_set_at": 0,     # 最近一次设定退避的时刻
}

_pending = [False, False, False, False]   # 待补发到平台的通道
_state_dirty = False
_state_due_ms = 0
_blocking_check_msg = False               # 该端口的 check_msg 是否会阻塞
_online_announced = False


# ===================== 工具函数 =====================
def _now():
    return time.ticks_ms()


def _backoff_delay(streak):
    """指数退避 + 抖动"""
    exp = streak if streak < BACKOFF_MAX_EXPONENT else BACKOFF_MAX_EXPONENT
    delay = BACKOFF_MIN * (BACKOFF_FACTOR ** exp)
    if delay > BACKOFF_MAX:
        delay = BACKOFF_MAX
    try:
        span = int(delay * BACKOFF_JITTER)
        if span > 0:
            delay += random.randint(-span, span)
    except Exception:
        pass
    return delay if delay > 0 else BACKOFF_MIN


def _sleep_backoff(streak, reason):
    delay = _backoff_delay(streak)
    _n["next_try_ms"] = time.ticks_add(_now(), delay * 1000)
    _n["backoff_set_at"] = _now()
    write_log("WARN", EVENT_MIXIO_DISCONNECT, "%s，%.1f 秒后重试（第 %d 次）" % (reason, delay, streak + 1))


def _in_backoff():
    return time.ticks_diff(_n["next_try_ms"], _now()) > 0


def _backoff_interrupted():
    """
    退避期提前结束判定。
    WiFi 是在退避设定之后才恢复的，说明断网诱因已消失，没必要干等到退避期满，
    立即重试可以让设备在链路恢复后 1 秒内重新上线，而不是继续等 120 秒。
    """
    if not _in_backoff():
        return False
    up = _n["wifi_up_since"]
    if not up:
        return False
    if time.ticks_diff(up, _n["backoff_set_at"]) > 0:
        _n["next_try_ms"] = 0
        _n["wifi_up_since"] = 0
        write_log("INFO", EVENT_MIXIO_CONNECT, "链路已恢复，提前结束退避立即重连")
        return False
    return True


# ===================== 硬件层 =====================
def io_init():
    """初始化继电器与按键；失败不抛异常，交由上层降级"""
    global relay_objs, key_objs
    relay_objs = []
    key_objs = []
    ok = True
    for pin_num in RELAY_PINS:
        try:
            # 默认输出"关"电平，避免上电瞬间误动作
            idle = 1 if RELAY_ACTIVE_LOW else 0
            relay_objs.append(machine.Pin(pin_num, machine.Pin.OUT, value=idle))
        except Exception as e:
            ok = False
            write_log("ERROR", EVENT_SYS_BOOT, "继电器引脚 %d 初始化失败:%s" % (pin_num, e))
    for pin_num in KEY_PINS:
        try:
            key_objs.append(machine.Pin(pin_num, machine.Pin.IN, KEY_PULL))
        except Exception as e:
            ok = False
            write_log("ERROR", EVENT_SYS_BOOT, "按键引脚 %d 初始化失败:%s" % (pin_num, e))
    return ok


def set_light(light_idx, state, announce=True):
    """
    控制单路灯光并同步缓存。
    :param light_idx: 0~3
    :param state: 0 关 / 1 开
    :param announce: 是否打上"待补发到平台"标记
    """
    global light_status, _state_dirty, _state_due_ms
    if not (0 <= light_idx < len(RELAY_PINS)):
        return False

    state = 1 if state else 0
    changed = light_status[light_idx] != state
    light_status[light_idx] = state

    # 逻辑状态 -> 物理电平
    if RELAY_ACTIVE_LOW:
        hw = 0 if state == 1 else 1
    else:
        hw = 1 if state == 1 else 0
    try:
        relay_objs[light_idx].value(hw)
    except Exception as e:
        write_log("ERROR", EVENT_LIGHT_SWITCH, "L%d 继电器写入失败:%s" % (light_idx + 1, e))
        return False

    if changed:
        if announce:
            _pending[light_idx] = True
        _mark_state_dirty()
    return True


def set_all_lights(state, announce=True):
    """四路统一开关（供全开/全关指令使用）"""
    for idx in range(len(RELAY_PINS)):
        set_light(idx, state, announce)


def relay_refresh(force=False):
    """把缓存状态回写 GPIO，抗干扰漂移"""
    if not force:
        if time.ticks_diff(_now(), _n["last_relay_refresh"]) < RELAY_REFRESH_INTERVAL * 1000:
            return
    _n["last_relay_refresh"] = _now()
    for idx in range(len(relay_objs)):
        try:
            hw = (0 if light_status[idx] == 1 else 1) if RELAY_ACTIVE_LOW else light_status[idx]
            if relay_objs[idx].value() != hw:
                relay_objs[idx].value(hw)
        except Exception:
            pass


def _mark_state_dirty():
    """延迟落盘：短时间内多次操作只写一次，延长 flash 寿命"""
    global _state_dirty, _state_due_ms
    _state_dirty = True
    _state_due_ms = time.ticks_add(_now(), 2000)


def flush_state(force=False):
    """到期的状态持久化"""
    global _state_dirty
    if not _state_dirty:
        return
    if not force and time.ticks_diff(_state_due_ms, _now()) > 0:
        return
    try:
        with open(STATE_FILE, "w") as f:
            f.write(json.dumps({"v": 1, "lights": light_status}))
        _state_dirty = False
    except Exception as e:
        write_log("WARN", EVENT_STATE_RESTORE, "状态持久化失败:%s" % str(e))
        _state_dirty = False


def restore_state():
    """上电恢复灯光状态；配置关闭恢复则强制全关"""
    global light_status
    loaded = None
    try:
        with open(STATE_FILE, "r") as f:
            data = json.loads(f.read())
        if isinstance(data, dict) and isinstance(data.get("lights"), list):
            loaded = data["lights"]
    except Exception:
        loaded = None

    if RESTORE_STATE_ON_BOOT and loaded:
        for idx in range(len(RELAY_PINS)):
            val = 1 if idx < len(loaded) and loaded[idx] else 0
            light_status[idx] = val
        write_log("INFO", EVENT_STATE_RESTORE, "恢复上次灯光状态:%s" % str(light_status))
    else:
        light_status = [0] * len(RELAY_PINS)
        write_log("INFO", EVENT_STATE_RESTORE, "上电默认全关")

    for idx in range(len(RELAY_PINS)):
        set_light(idx, light_status[idx], announce=False)
    _n["last_relay_refresh"] = _now()


# ===================== WiFi =====================
def _ensure_wlan():
    global wlan
    if wlan is None:
        wlan = network.WLAN(network.STA_IF)
    try:
        if not wlan.active():
            wlan.active(True)
            time.sleep_ms(100)
    except Exception as e:
        write_log("ERROR", EVENT_WIFI_CONNECT, "网卡激活失败:%s" % str(e))
        return False
    return True


def wifi_is_up():
    """三重判定：对象存在 + 已关联 + 拿到有效 IP"""
    if wlan is None:
        return False
    try:
        if not wlan.isconnected():
            return False
        ip = wlan.ifconfig()[0]
        if not ip or ip == "0.0.0.0":
            return False
        return True
    except Exception:
        return False


# ESP32 端口的 wlan.status() 在未连上时返回 ESP-IDF 的断连原因码（>=200），
# 连上时返回 MicroPython 的 STAT_*。这里把常见的翻译成人话。
WIFI_REASONS = {
    0: "空闲",
    1: "正在连接",
    2: "密码错误",
    3: "找不到该 AP",
    4: "连接失败",
    5: "已获取 IP",
    200: "信标超时（信号太弱）",
    201: "找不到该 AP（SSID 不存在或不在覆盖范围内）",
    202: "认证失败（密码错误）",
    203: "关联失败（AP 拒绝）",
    204: "四次握手超时（密码错误）",
    205: "握手被 AP 丢弃",
    206: "802.1X 认证失败",
    207: "AP 已满载",
    210: "Beacon 超时",
}


def wifi_reason(code):
    """把 wlan.status() 的数字翻译成可读原因"""
    return WIFI_REASONS.get(code, "原因码 %s" % code)


def wifi_rssi():
    try:
        return wlan.status("rssi")
    except Exception:
        try:
            return wlan.status("rssi")
        except Exception:
            return None


def _wifi_start_connect(idx):
    cfg = WIFI_NETWORKS[idx]
    if not _ensure_wlan():
        return False
    try:
        try:
            wlan.disconnect()
            time.sleep_ms(50)
        except Exception:
            pass
        write_log("INFO", EVENT_WIFI_CONNECT, "开始连接 WiFi:%s" % cfg["ssid"])
        wlan.connect(cfg["ssid"], cfg["password"])
        _n["wifi_idx"] = idx
        _n["wifi_deadline"] = time.ticks_add(_now(), WIFI_CONNECT_TIMEOUT * 1000)
        _n["state"] = NET_WIFI_WAIT
        return True
    except Exception as e:
        write_log("ERROR", EVENT_WIFI_CONNECT, "WiFi 连接发起失败:%s" % str(e))
        return False


def _wifi_rebuild():
    """重建网卡：连续失败后的强硬手段"""
    global wlan
    write_log("WARN", EVENT_WIFI_CONNECT, "连续失败，重建 WiFi 网卡")
    try:
        wlan.active(False)
        time.sleep_ms(300)
    except Exception:
        pass
    try:
        wlan.active(True)
        time.sleep_ms(300)
    except Exception:
        pass
    _n["wifi_rebuild_at"] = _now()
    _n["wifi_fail_streak"] = 0


def wifi_down(reason):
    """WiFi 掉线统一入口"""
    _n["wifi_fail_streak"] += 1
    _n["fail_streak"] += 1
    _n["state"] = NET_DOWN
    _n["down_since"] = _n["down_since"] or _now()
    _drop_mqtt()
    if wifi_is_up():
        write_log("WARN", EVENT_WIFI_DISCONNECT, "WiFi 异常:%s（当前仍关联，尝试保持）" % reason)
    else:
        write_log("WARN", EVENT_WIFI_DISCONNECT, "WiFi 离线:%s" % reason)

    if _n["wifi_fail_streak"] >= WIFI_MAX_CONSECUTIVE_FAIL:
        _wifi_rebuild()
    _sleep_backoff(_n["fail_streak"], "WiFi 不可用")


# ===================== MQTT =====================
def _create_client(broker_idx):
    global mqtt_client
    cfg = MQTT_BROKERS[broker_idx]
    try:
        client = MQTTClient(
            client_id=MQTT_CLIENT_ID,
            server=cfg["host"],
            port=cfg["port"],
            user=MQTT_USER,
            password=MQTT_PWD,
            keepalive=MQTT_KEEPALIVE,
        )
        client.set_last_will(WILL_TOPIC, WILL_MSG, WILL_QOS, WILL_RETAIN)
        client.set_callback(mqtt_callback)
        mqtt_client = client
        return True
    except Exception as e:
        write_log("ERROR", EVENT_MIXIO_CONNECT, "创建 MQTT 客户端失败:%s" % str(e))
        mqtt_client = None
        return False


def _drop_mqtt():
    global mqtt_client, _online_announced
    if mqtt_client is None:
        _online_announced = False
        return
    try:
        mqtt_client.disconnect()
    except Exception:
        pass
    mqtt_client = None
    _online_announced = False
    for i in range(len(_pending)):
        _pending[i] = True   # 离线期间状态可能已变，上线后统一补发


def mqtt_callback(topic, msg):
    """平台下发指令；任何解析异常都被隔离，绝不让脏数据冲垮主循环"""
    try:
        topic_str = topic.decode() if isinstance(topic, bytes) else str(topic)
        msg_str = msg.decode() if isinstance(msg, bytes) else str(msg)
        write_log("INFO", EVENT_CLOUD_CTRL, "云端下发 主题:%s 指令:%s" % (topic_str, msg_str))

        if topic_str == CMD_ALL_TOPIC:
            cmd = 1 if msg_str.strip() in ("1", "on", "ON", "true") else 0
            set_all_lights(cmd, announce=True)
            write_log("INFO", EVENT_LIGHT_SWITCH, "云端全控指令 -> 全部置 %d" % cmd)
            return

        for idx, t in enumerate(LIGHT_TOPICS):
            if t == topic_str:
                cmd = 1 if msg_str.strip() in ("1", "on", "ON", "true") else 0
                set_light(idx, cmd, announce=True)
                write_log("INFO", EVENT_LIGHT_SWITCH, "L%d 云端控制切换至 %d" % (idx + 1, cmd))
                return
    except Exception as e:
        write_log("ERROR", EVENT_CLOUD_CTRL, "指令处理异常:%s" % str(e))


def _mqtt_connect(broker_idx):
    global mqtt_client
    if mqtt_client is None:
        if not _create_client(broker_idx):
            return False
    cfg = MQTT_BROKERS[broker_idx]
    try:
        mqtt_client.connect()
        write_log("INFO", EVENT_MIXIO_CONNECT, "MQTT TCP 握手完成 %s:%d" % (cfg["host"], cfg["port"]))
    except Exception as e:
        write_log("ERROR", EVENT_MIXIO_CONNECT, "MQTT 连接失败 %s:%d -> %s" % (cfg["host"], cfg["port"], str(e)))
        mqtt_client = None
        return False

    # 订阅：逐条间隔，防止平台限流
    try:
        for topic in LIGHT_TOPICS:
            mqtt_client.subscribe(topic, qos=MQTT_QOS)
            time.sleep_ms(30)
        mqtt_client.subscribe(CMD_ALL_TOPIC, qos=MQTT_QOS)
        time.sleep_ms(30)
    except Exception as e:
        write_log("ERROR", EVENT_MIXIO_CONNECT, "订阅失败:%s" % str(e))
        try:
            mqtt_client.disconnect()
        except Exception:
            pass
        mqtt_client = None
        return False

    _n["broker_idx"] = broker_idx
    _n["last_rx"] = _now()
    _n["last_resub"] = _now()
    return True


def mqtt_publish(topic, payload, qos=MQTT_QOS, retain=False):
    """统一发布入口，失败即标记链路异常"""
    if mqtt_client is None:
        return False
    try:
        if isinstance(payload, str):
            payload = payload.encode()
        mqtt_client.publish(topic, payload, qos=qos, retain=retain)
        _n["last_rx"] = _now()
        return True
    except Exception as e:
        write_log("WARN", EVENT_MIXIO_DISCONNECT, "发布失败 %s:%s" % (topic, str(e)))
        return False


def publish_light_state(idx):
    if 0 <= idx < len(LIGHT_TOPICS):
        if mqtt_publish(LIGHT_TOPICS[idx], str(light_status[idx]), retain=True):
            _pending[idx] = False


def _announce_online():
    global _online_announced
    if mqtt_publish(ONLINE_TOPIC, "1", retain=True):
        _online_announced = True


def _publish_telemetry():
    try:
        rssi = wifi_rssi()
        payload = {
            "ver": FIRMWARE_VERSION,
            "mac": MAC_HEX,
            "rssi": rssi if rssi is not None else 0,
            "free": gc.mem_free(),
            "uptime": time.ticks_ms() // 1000,
            "lights": light_status,
            "ap": WIFI_NETWORKS[_n["wifi_idx"]]["ssid"],
            "broker": _n["broker_idx"],
            "streak": _n["fail_streak"],
        }
        mqtt_publish(TELEMETRY_TOPIC, json.dumps(payload), qos=0, retain=False)
    except Exception as e:
        write_log("WARN", EVENT_MIXIO_CONNECT, "遥测上报异常:%s" % str(e))


def _drain_pending():
    """补发离线期间积压的状态"""
    for idx in range(len(_pending)):
        if _pending[idx]:
            publish_light_state(idx)


def mqtt_down(reason, failover_now=False):
    """
    MQTT 掉线统一入口
    :param reason: 原因描述
    :param failover_now: True 表示立即换 Broker。
           TCP 握手被拒/超时属于"这台主机明确不可用"，重试同一台没有意义，
           立刻轮转可以把故障恢复时间从十几秒压到几秒。
    """
    _n["fail_streak"] += 1
    _n["state"] = NET_MQTT_WAIT
    _n["down_since"] = _n["down_since"] or _now()
    write_log("WARN", EVENT_MIXIO_DISCONNECT, "MQTT 离线:%s" % reason)
    _drop_mqtt()

    if failover_now or _n["fail_streak"] % MQTT_MAX_CONSECUTIVE_FAIL == 0:
        _n["broker_idx"] = (_n["broker_idx"] + 1) % len(MQTT_BROKERS)
        write_log("WARN", EVENT_MIXIO_FAILOVER, "切换备用 Broker -> %s" % MQTT_BROKERS[_n["broker_idx"]]["host"])
    _sleep_backoff(_n["fail_streak"], "MQTT 不可用")


# ===================== 网络状态机 =====================
def net_init():
    _ensure_wlan()
    _n["state"] = NET_DOWN
    _n["next_try_ms"] = 0
    write_log("INFO", EVENT_SYS_BOOT, "网络状态机初始化，WiFi %d 个 / Broker %d 个"
              % (len(WIFI_NETWORKS), len(MQTT_BROKERS)))


def _poll_messages():
    """
    非阻塞收包。
    自适应：若某次 check_msg 耗时过长（说明该端口实现会阻塞），
    之后降频调用，避免主循环被卡住。
    """
    global _blocking_check_msg
    if _blocking_check_msg:
        if time.ticks_diff(_now(), _n["last_ping"]) < MQTT_MSG_TIMEOUT * 1000:
            return True
    t0 = _now()
    try:
        mqtt_client.check_msg()
    except Exception as e:
        mqtt_down("收包异常:%s" % str(e))
        return False
    cost = time.ticks_diff(_now(), t0)
    if cost > 500 and not _blocking_check_msg:
        _blocking_check_msg = True
        write_log("WARN", EVENT_MIXIO_CONNECT, "check_msg 阻塞 %d ms，已自动降频" % cost)
    return True


def _online_tasks():
    """在线状态下的周期性任务"""
    now = _now()

    # 心跳：让平台和遗嘱机制知道设备还活着
    if time.ticks_diff(now, _n["last_heartbeat"]) > HEARTBEAT_INTERVAL * 1000:
        _n["last_heartbeat"] = now
        _announce_online()
        if not mqtt_publish(ONLINE_TOPIC, "1", retain=True):
            mqtt_down("心跳发送失败")
            return

    # 真·PINGREQ：按节流发送，不再每拍一次
    if time.ticks_diff(now, _n["last_ping"]) > MQTT_PING_INTERVAL * 1000:
        _n["last_ping"] = now
        try:
            mqtt_client.ping()
            _n["last_rx"] = now
        except Exception as e:
            mqtt_down("PING 失败:%s" % str(e))
            return

    # 周期性重订阅：防止平台侧订阅丢失
    if time.ticks_diff(now, _n["last_resub"]) > MQTT_RESUB_INTERVAL * 1000:
        _n["last_resub"] = now
        try:
            for topic in LIGHT_TOPICS:
                mqtt_client.subscribe(topic, qos=MQTT_QOS)
                time.sleep_ms(20)
            mqtt_client.subscribe(CMD_ALL_TOPIC, qos=MQTT_QOS)
        except Exception as e:
            write_log("WARN", EVENT_MIXIO_CONNECT, "重订阅失败:%s" % str(e))

    # 全量状态重发：纠正平台状态漂移
    if time.ticks_diff(now, _n["last_republish"]) > STATE_REPUBLISH_INTERVAL * 1000:
        _n["last_republish"] = now
        for idx in range(len(LIGHT_TOPICS)):
            _pending[idx] = True

    # 遥测
    if time.ticks_diff(now, _n["last_telemetry"]) > TELEMETRY_INTERVAL * 1000:
        _n["last_telemetry"] = now
        _publish_telemetry()

    _drain_pending()


def net_service():
    """
    网络状态机主入口，主循环每拍调用一次。
    全程非阻塞（单次可能耗时的是 TCP 握手，已由看门狗兜底）。
    """
    now = _now()

    # ---- 弱网漫游：已连接但信号太差，主动换 AP ----
    if WIFI_SCAN_ROAM_ENABLE and _n["state"] in (NET_MQTT_WAIT, NET_ONLINE) and len(WIFI_NETWORKS) > 1:
        if time.ticks_diff(now, _n["last_roam"]) > 120000:
            _n["last_roam"] = now
            rssi = wifi_rssi()
            if rssi is not None and rssi < WIFI_RSSI_FLOOR:
                write_log("WARN", EVENT_WIFI_ROAM, "信号弱 %d dBm，尝试切换 AP" % rssi)
                _n["wifi_idx"] = (_n["wifi_idx"] + 1) % len(WIFI_NETWORKS)
                _drop_mqtt()
                _wifi_start_connect(_n["wifi_idx"])
                return

    st = _n["state"]

    # ---------- NET_DOWN：等待退避结束，发起 WiFi 连接 ----------
    if st == NET_DOWN:
        if _in_backoff():
            return
        if wifi_is_up():
            # 软复位时 ESP-IDF 的 WiFi 链路可能还在，这里会直接跳过握手阶段。
            # 不打这行日志的话，串口上就看不出 WiFi 到底是新连的还是复用的。
            cfg = WIFI_NETWORKS[_n["wifi_idx"] % len(WIFI_NETWORKS)]
            write_log("INFO", EVENT_WIFI_CONNECT,
                      "WiFi 已连接（复用现有链路）%s IP:%s" % (cfg["ssid"], wlan.ifconfig()[0]))
            _n["state"] = NET_MQTT_WAIT
            return
        _wifi_start_connect(_n["wifi_idx"] % len(WIFI_NETWORKS))
        return

    # ---------- NET_WIFI_WAIT：等待 WiFi 握手完成 ----------
    if st == NET_WIFI_WAIT:
        if wifi_is_up():
            cfg = WIFI_NETWORKS[_n["wifi_idx"]]
            write_log("INFO", EVENT_WIFI_CONNECT, "WiFi 已连接 %s IP:%s" % (cfg["ssid"], wlan.ifconfig()[0]))
            _n["wifi_fail_streak"] = 0
            # 链路已恢复：重置退避阶梯，否则断网越久、恢复后等得越久
            _n["fail_streak"] = 0
            _n["wifi_up_since"] = now
            _n["state"] = NET_MQTT_WAIT
            _n["next_try_ms"] = 0
            sync_ntp_time(force=True)
            rebind_daily_file()
            return
        if time.ticks_diff(now, _n["wifi_deadline"]) > 0:
            # 超时：把失败 AP 与 ESP-IDF 具体原因打出来。
            # 201 = 搜不到该 AP（名字错 / 不在覆盖区），202/204 = 密码错，
            # 不打出来就只能靠人去现场 scan，排障成本极高。
            try:
                _code = wlan.status()
            except Exception:
                _code = -1
            _cfg = WIFI_NETWORKS[_n["wifi_idx"] % len(WIFI_NETWORKS)]
            write_log("WARN", EVENT_WIFI_CONNECT,
                      "AP %s 连接超时 status=%s(%s)" %
                      (_cfg["ssid"], _code, wifi_reason(_code)))
            # 换下一个 AP
            _n["wifi_idx"] = (_n["wifi_idx"] + 1) % len(WIFI_NETWORKS)
            if _n["wifi_idx"] == 0:
                # 所有 AP 都试过一轮
                wifi_down("全部 AP 连接失败")
            else:
                write_log("WARN", EVENT_WIFI_CONNECT, "AP 连接超时，切换到下一个")
                _wifi_start_connect(_n["wifi_idx"])
        return

    # ---------- NET_MQTT_WAIT / NET_ONLINE：先确认 WiFi 还在 ----------
    if st in (NET_MQTT_WAIT, NET_ONLINE):
        if time.ticks_diff(now, _n["last_wifi_check"]) > WIFI_RECHECK_INTERVAL * 1000:
            _n["last_wifi_check"] = now
            if not wifi_is_up():
                wifi_down("在线巡检发现 WiFi 断开")
                return

    if st == NET_MQTT_WAIT:
        if _backoff_interrupted():
            return
        if _mqtt_connect(_n["broker_idx"] % len(MQTT_BROKERS)):
            _n["state"] = NET_ONLINE
            _n["fail_streak"] = 0
            _n["down_since"] = 0
            _n["online_since"] = now
            _n["last_heartbeat"] = now
            _n["last_ping"] = now
            _n["last_republish"] = now
            _n["last_telemetry"] = now
            write_log("INFO", EVENT_MIXIO_CONNECT, "MixIO 平台就绪，可收发指令")
            _announce_online()
            # 上线后等待平台推 retain 状态，避免本地状态被覆盖为旧值
            _wait_retain(800)
        else:
            # TCP/订阅失败说明这台 Broker 明确不可用，立即轮转而不是原地重试
            mqtt_down("Broker 连接失败", failover_now=True)
        return

    if st == NET_ONLINE:
        if not _poll_messages():
            return
        _online_tasks()
        return


def _wait_retain(ms):
    """上线后短等平台推送 retain 状态"""
    deadline = time.ticks_add(_now(), ms)
    while time.ticks_diff(deadline, _now()) > 0:
        try:
            mqtt_client.check_msg()
        except Exception:
            break
        time.sleep_ms(50)


# ===================== 按键 =====================
_key_raw = [0, 0, 0, 0]
_key_stable = [0, 0, 0, 0]
_key_change_ms = [0, 0, 0, 0]


def on_key_press(idx):
    """按键触发：翻转本地灯光，并打上补发标记"""
    new_state = 1 - light_status[idx]
    set_light(idx, new_state, announce=True)
    write_log("INFO", EVENT_KEY_TRIGGER, "物理按键 L%d 触发，灯光切换为 %d" % (idx + 1, new_state))


def _read_key(idx):
    """读取按键电平并归一化为 0/1（1 = 按下）；读取失败返回 None"""
    try:
        raw = key_objs[idx].value()
    except Exception:
        return None
    pressed = (raw == 1) if KEY_ACTIVE_HIGH else (raw == 0)
    return 1 if pressed else 0


def scan_key():
    """
    扫描按键。

    消抖策略：在一次扫描内完成「检测电平变化 -> 短延时重采样 -> 确认」，
    而不是跨多个周期计数确认。好处是 200ms 扫描周期下按下去立刻响应；
    同时释放沿也会被确认，避免稳定态卡在"按下"导致后续按键失灵。
    """
    now = _now()
    for idx in range(len(key_objs)):
        cur = _read_key(idx)
        if cur is None:
            continue

        if cur == _key_raw[idx]:
            # 电平未变：稳定够久则回写稳定态（掉电抖动后的兜底回位）
            if time.ticks_diff(now, _key_change_ms[idx]) >= KEY_DEBOUNCE_MS:
                _key_stable[idx] = cur
            continue

        # 电平变化：记录后延时重采样，滤掉机械抖动
        _key_raw[idx] = cur
        _key_change_ms[idx] = now
        time.sleep_ms(KEY_DEBOUNCE_MS)
        if _read_key(idx) != cur:
            continue                       # 抖动，不采信
        _key_change_ms[idx] = _now()
        _key_stable[idx] = cur
        if cur == 1:
            on_key_press(idx)


# ===================== 对外查询接口 =====================
def is_online():
    return _n["state"] == NET_ONLINE and mqtt_client is not None


def net_info():
    return {
        "state": _n["state"],
        "online": is_online(),
        "ap": WIFI_NETWORKS[_n["wifi_idx"]]["ssid"] if WIFI_NETWORKS else "",
        "broker": MQTT_BROKERS[_n["broker_idx"]]["host"] if MQTT_BROKERS else "",
        "streak": _n["fail_streak"],
        "down_secs": (time.ticks_diff(_now(), _n["down_since"]) // 1000) if _n["down_since"] else 0,
        "ip": (wlan.ifconfig()[0] if wifi_is_up() else ""),
        "rssi": wifi_rssi(),
    }


def down_seconds():
    if not _n["down_since"]:
        return 0
    return time.ticks_diff(_now(), _n["down_since"]) // 1000


def periodic_service():
    """
    与网络无关但需周期性执行的任务。
    由主循环调用，内部自行节流。
    """
    maybe_sync_ntp()
    relay_refresh()
    flush_state()


def safe_shutdown():
    """复位前的收尾：落盘状态 + 尽量发出离线通知"""
    flush_state(force=True)
    if is_online():
        try:
            mqtt_publish(ONLINE_TOPIC, "0", retain=True)
        except Exception:
            pass
        _drop_mqtt()
