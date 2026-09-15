# -*- coding: utf-8 -*-
"""
main.py —— 系统调度中心

启动链路：
    boot.py（维护模式窗口）
      -> log_init（日志先就位，后续任何环节都能留痕）
      -> 崩溃计数核算（判断是否进入安全模式）
      -> OTA 回滚判定
      -> 硬件初始化 + 灯光状态恢复
      -> 看门狗启动
      -> 网络状态机 + 主循环

主循环每 10ms 一拍，所有任务靠 ticks_diff 节流，全程非阻塞。

冗余与自愈：
    - 看门狗：任何单点卡死都能拉回复位。
    - 崩溃循环检测：连续快速重启自动进入安全模式（只保本地按键控制）。
    - 长断网复位：网络全断超过阈值，主动复位重来。
    - 低内存复位：堆空间长期不足，复位释放。
"""

import gc
import os
import time
import machine

try:
    import ujson as json
except ImportError:
    import json

from config import *


# ---------- 日志模块兜底（自愈关键路径）----------
# log.py 是唯一在 import 期就要碰文件系统的模块。一旦它自身出问题
# （典型：被同名目录遮蔽 → no module named 'log.log_init'；或 flash 损坏），
# 顶层 import 失败会让 main.py 根本跑不起来，设备直接进入崩溃循环、OTA 也救不回来。
# 这里兜一层：装一个只往串口打的极简替代品，保证设备照样启动、照样能 OTA 修复。
class _FallbackLog:
    """日志模块不可用时的最小替代品，接口与 log.py 保持同名"""

    def log_init(self, *a, **k):
        pass

    def write_log(self, level, tag, msg, *a, **k):
        print("[FB][%s][%s] %s" % (level, tag, msg))

    def sync_ntp_time(self, *a, **k):
        return False

    def maybe_sync_ntp(self, *a, **k):
        return False

    def time_trusted(self):
        return False

    def rebind_daily_file(self, *a, **k):
        pass

    def get_time_str(self, *a, **k):
        return "boot+%07ds" % (time.ticks_ms() // 1000)


def _install_fallback_log(reason):
    import sys
    sys.modules["log"] = _FallbackLog()
    print("[boot] 警告：日志模块不可用，已降级为串口输出 -> %s" % reason)


try:
    from log import log_init, write_log, sync_ntp_time, get_time_str, rebind_daily_file
except Exception as _e:                      # noqa: F821
    # 兜底装进 sys.modules，后面 func.py / ota.py 的 "from log import ..." 才能继续
    _install_fallback_log(_e)
    from log import log_init, write_log, sync_ntp_time, get_time_str, rebind_daily_file

import func
import ota

MAINT_FLAG_FILE = "/MAINT_MODE"

# ===================== 全局运行时 =====================
wdt = None
_wdt_feed_ms = 0
_safe_mode = False
_crash_count = 0
_retry_window_until = 0
_next_safe_retry = 0
_low_mem_streak = 0
_boot_ms = 0


# ===================== 看门狗 =====================
def init_wdt():
    """逐级降级尝试启动看门狗，端口不支持则放弃"""
    global wdt
    if not WDT_ENABLE:
        write_log("WARN", EVENT_WDT, "看门狗已在配置中关闭")
        return None
    for timeout in (WDT_TIMEOUT_MS, 8000, 5000, 3000, 1000):
        try:
            wdt = machine.WDT(timeout=timeout)
            write_log("INFO", EVENT_SYS_BOOT, "看门狗已启动，超时 %d ms" % timeout)
            return wdt
        except Exception:
            continue
    write_log("WARN", EVENT_WDT, "当前端口不支持看门狗，跳过")
    wdt = None
    return None


def feed_wdt(force=False):
    global _wdt_feed_ms
    if wdt is None:
        return
    now = time.ticks_ms()
    if not force and time.ticks_diff(now, _wdt_feed_ms) < WDT_FEED_INTERVAL * 1000:
        return
    try:
        wdt.feed()
    except Exception:
        pass
    _wdt_feed_ms = now


# ===================== 崩溃计数（不依赖时钟） =====================
def _flag_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _write_flag(path, data="1"):
    try:
        with open(path, "w") as f:
            f.write(data)
        return True
    except Exception:
        return False


def _crash_load():
    """从 RTC 内存读取 (崩溃计数, 上次存活秒数)；不可用时降级到文件"""
    try:
        raw = machine.RTC().memory()
        if raw:
            parts = raw.decode().split("|")
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    try:
        with open(CRASH_STATE_FILE, "r") as f:
            data = json.loads(f.read())
        return int(data.get("n", 0)), int(data.get("u", 0))
    except Exception:
        return 0, 0


def _crash_save(count, uptime_sec):
    payload = "%d|%d" % (count, uptime_sec)
    try:
        machine.RTC().memory(payload.encode())
        return
    except Exception:
        pass
    try:
        with open(CRASH_STATE_FILE, "w") as f:
            f.write(json.dumps({"n": count, "u": uptime_sec}))
    except Exception:
        pass


def account_crash():
    """
    核算崩溃次数。
    判据是"上次运行存活了多久"，与系统时钟无关，NTP 未同步也能正确工作。
    """
    global _crash_count, _safe_mode
    count, last_uptime = _crash_load()

    if last_uptime > 0 and last_uptime < CRASH_WINDOW_SEC:
        count += 1
        write_log("WARN", EVENT_SYS_RESET,
                  "上次仅存活 %d 秒即重启，崩溃计数 %d/%d" % (last_uptime, count, CRASH_RESET_LIMIT))
    else:
        count = 1 if last_uptime else 0

    _crash_count = count
    _crash_save(count, 0)   # 存活时长先清零，运行中定期更新

    if count >= CRASH_RESET_LIMIT:
        _write_flag(SAFE_MODE_FLAG, "crash-loop")
        _safe_mode = True
        write_log("ERROR", EVENT_SAFE_MODE,
                  "检测到崩溃循环（%d 次），进入安全模式" % count)
    return count


def update_uptime_marker():
    """运行期定期上报存活时长，让下次启动能判断本次是否跑得够久"""
    uptime = time.ticks_ms() // 1000
    _crash_save(_crash_count if _crash_count else 1, uptime)


def clear_crash():
    _crash_save(0, 0)


# ===================== 维护 / 安全模式 =====================
def enter_reason():
    if _flag_exists(MAINT_FLAG_FILE):
        return "维护模式（开机按住 KEY1）"
    if _flag_exists(SAFE_MODE_FLAG):
        return "安全模式（崩溃循环或人工标记）"
    return ""


def exit_safe_mode():
    global _safe_mode
    _safe_mode = False
    for path in (SAFE_MODE_FLAG, MAINT_FLAG_FILE):
        try:
            os.remove(path)
        except Exception:
            pass
    clear_crash()
    write_log("INFO", EVENT_SAFE_MODE, "已退出安全模式，恢复正常运行")


# ===================== 复位 =====================
def do_reset(reason):
    write_log("ERROR", EVENT_SYS_RESET, "系统复位，原因:%s" % reason)
    try:
        func.safe_shutdown()
    except Exception:
        pass
    try:
        func.flush_state(force=True)
    except Exception:
        pass
    feed_wdt(force=True)
    time.sleep(2)
    machine.reset()


# ===================== 内存巡检 =====================
def check_memory():
    global _low_mem_streak
    free = gc.mem_free()
    if free >= MEM_FREE_FLOOR:
        _low_mem_streak = 0
        return True
    gc.collect()
    free2 = gc.mem_free()
    _low_mem_streak += 1
    write_log("WARN", EVENT_MEM_LOW,
              "可用内存偏低 %d -> %d 字节（阈值 %d，连续 %d 次）"
              % (free, free2, MEM_FREE_FLOOR, _low_mem_streak))
    if _low_mem_streak >= MEM_LOW_STREAK_RESET:
        return False
    return True


# ===================== 主循环 =====================
def main_loop():
    global _retry_window_until, _next_safe_retry

    _t = {
        "key": 0,
        "mem": 0,
        "gc": 0,
        "ota": 0,
        "uptime": 0,
        "netlog": 0,
    }
    _last_online = False
    _last_verify_ms = 0

    while True:
        now = time.ticks_ms()
        feed_wdt()

        # ---------- 物理按键：任何模式下都必须可用（最后一道人工防线） ----------
        if time.ticks_diff(now, _t["key"]) > KEY_SCAN_INTERVAL * 1000:
            _t["key"] = now
            try:
                func.scan_key()
            except Exception as e:
                write_log("ERROR", EVENT_KEY_TRIGGER, "按键扫描异常:%s" % str(e))

        # ---------- 网络 ----------
        if _safe_mode:
            # 安全模式：定期开一个探测窗口，能连上就自动恢复正常
            if _retry_window_until and time.ticks_diff(_retry_window_until, now) > 0:
                try:
                    func.net_service()
                except Exception as e:
                    write_log("WARN", EVENT_SAFE_MODE, "探测窗口内网络异常:%s" % str(e))
                if func.is_online():
                    write_log("INFO", EVENT_SAFE_MODE, "探测窗口内联网成功")
                    exit_safe_mode()
                    func.net_init()
            elif time.ticks_diff(now, _next_safe_retry) > 0:
                _next_safe_retry = time.ticks_add(now, SAFE_MODE_RETRY_INTERVAL * 1000)
                _retry_window_until = time.ticks_add(now, 60000)
                func.net_init()
                write_log("INFO", EVENT_SAFE_MODE, "开启 60 秒联网探测窗口")
        else:
            try:
                func.net_service()
            except Exception as e:
                write_log("ERROR", EVENT_SYS_RESET, "网络状态机异常:%s" % str(e))
                try:
                    func.mqtt_down("状态机异常")
                except Exception:
                    pass

            online = func.is_online()
            if online != _last_online:
                _last_online = online
                if online:
                    info = func.net_info()
                    write_log("INFO", EVENT_MIXIO_CONNECT,
                              "平台已上线 AP:%s Broker:%s RSSI:%s"
                              % (info["ap"], info["broker"], info["rssi"]))
                    # 上线即确认 OTA，顺便清理备份
                    try:
                        ota.confirm_ota()
                    except Exception as e:
                        write_log("WARN", EVENT_OTA_FINISH, "OTA 确认异常:%s" % str(e))
                    clear_crash()
                elif time.ticks_diff(now, _last_verify_ms) > 0:
                    _last_verify_ms = time.ticks_add(now, 60000)
                    write_log("WARN", EVENT_MIXIO_DISCONNECT, "平台连接断开，进入自愈重连流程")

        # ---------- 周期任务 ----------
        try:
            func.periodic_service()
        except Exception as e:
            write_log("WARN", EVENT_SYS_BOOT, "周期任务异常:%s" % str(e))

        # ---------- 内存 / GC ----------
        if time.ticks_diff(now, _t["mem"]) > MEM_CHECK_INTERVAL * 1000:
            _t["mem"] = now
            if not check_memory():
                do_reset("内存持续不足")

        if time.ticks_diff(now, _t["gc"]) > GC_INTERVAL * 1000:
            _t["gc"] = now
            gc.collect()

        # ---------- 崩溃存活标记 ----------
        if time.ticks_diff(now, _t["uptime"]) > 30000:
            _t["uptime"] = now
            update_uptime_marker()

        # ---------- 长断网复位 ----------
        if not _safe_mode and func.down_seconds() > NET_DOWN_RESET_THRESHOLD:
            do_reset("网络全断超过 %d 秒" % NET_DOWN_RESET_THRESHOLD)

        # ---------- OTA ----------
        if not _safe_mode and OTA_ENABLE and func.is_online():
            if time.ticks_diff(now, _t["ota"]) > OTA_CHECK_INTERVAL * 1000:
                _t["ota"] = now
                try:
                    rc = ota.check_and_upgrade()
                    if rc == 1:
                        # 已写入待确认标记并即将重启
                        pass
                except Exception as e:
                    write_log("ERROR", EVENT_OTA_START, "OTA 流程异常:%s" % str(e))

        # ---------- 打拍 ----------
        # 长时间断网时降速，减少无谓空转
        if _safe_mode or func.down_seconds() > NET_DEGRADED_THRESHOLD:
            time.sleep_ms(50)
        else:
            time.sleep_ms(10)


# ===================== 启动 =====================
def startup():
    global _safe_mode, _boot_ms

    _boot_ms = time.ticks_ms()
    log_init()
    write_log("INFO", EVENT_SYS_BOOT,
              "固件 v%s 启动，机型 %s，MAC %s" % (FIRMWARE_VERSION, DEVICE_MODEL, MAC_HEX))

    # OTA 回滚：新固件起不来就退回旧版本
    try:
        if ota.check_pending_rollback():
            write_log("WARN", EVENT_OTA_ROLLBACK, "已完成固件回滚，继续启动")
    except Exception as e:
        write_log("WARN", EVENT_OTA_ROLLBACK, "回滚检查异常:%s" % str(e))

    # 崩溃核算 + 安全模式判定
    account_crash()
    reason = enter_reason()
    if reason and not _safe_mode:
        _safe_mode = True
    if _safe_mode:
        write_log("WARN", EVENT_SAFE_MODE, "进入安全模式：%s（仅本地按键可用）" % (reason or "崩溃循环"))

    # 硬件与状态
    if not func.io_init():
        write_log("ERROR", EVENT_SYS_BOOT, "部分硬件初始化失败，降级运行")
    try:
        func.restore_state()
    except Exception as e:
        write_log("ERROR", EVENT_STATE_RESTORE, "状态恢复失败:%s" % str(e))

    # 看门狗
    init_wdt()
    ota.set_feeder(feed_wdt)

    if not _safe_mode:
        func.net_init()

    write_log("INFO", EVENT_SYS_BOOT, "初始化完成，进入主循环")
    return _safe_mode


if __name__ == "__main__":
    safe = False
    try:
        safe = startup()
        main_loop()
    except KeyboardInterrupt:
        print("\n[main] 收到中断，退出到 REPL")
        try:
            func.flush_state(force=True)
        except Exception:
            pass
    except Exception as err:
        try:
            write_log("ERROR", EVENT_SYS_RESET, "主程序异常:%s" % str(err))
        except Exception:
            print("[main] 主程序异常:", err)
        try:
            func.safe_shutdown()
        except Exception:
            pass
        feed_wdt(force=True)
        time.sleep(3)
        machine.reset()
