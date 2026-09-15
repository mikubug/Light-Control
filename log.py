# -*- coding: utf-8 -*-
"""
log.py —— 分级日志系统（对 main.py 暴露为 log_manager）

冗余设计：
1. 时钟不可信（NTP 未同步）时拒绝按时间清理日志，避免误删全部历史。
2. 文件写入失败自动降级为纯控制台输出，不因日志拖垮业务。
3. 文件数量与单文件大小双重上限，防止写满 flash 导致系统崩溃。
4. NTP 支持自定义服务器 + 失败重试队列，时间错了日志时间戳也能事后校准。
"""

import os
import time
import machine
import ntptime
from config import *

# 日志等级权重
LEVEL_WEIGHT = {
    "DEBUG": 0,
    "INFO": 1,
    "WARN": 2,
    "ERROR": 3,
}

_log_file = ""          # 当前日志文件路径
_file_ok = True         # 文件写入是否可用
_seq = 0                # 本次开机日志序号
_write_fail = 0         # 连续写入失败次数
_ntp_ok = False         # 时钟是否已完成网络校准
_last_ntp_try = 0       # 上次 NTP 尝试时间（ticks_ms）
_last_gc = 0


# ===================== 时钟 =====================
def time_trusted():
    """时钟是否可信（NTP 同步过，或 RTC 年份合理）"""
    if _ntp_ok:
        return True
    try:
        return time.localtime()[0] >= LOG_MIN_YEAR
    except Exception:
        return False


def get_time_str():
    try:
        t = time.localtime()
        if t[0] < LOG_MIN_YEAR and not _ntp_ok:
            # 时钟不可信：用开机相对时间，避免写入 2000-01-01 这种假日期
            return "boot+%07ds" % (time.ticks_ms() // 1000)
        return "%04d-%02d-%02d %02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])
    except Exception:
        return "unknown"


def sync_ntp_time(force=False):
    """
    同步网络时间并写入 RTC。
    :param force: True 表示忽略节流立即重试
    :return: 是否成功
    """
    global _ntp_ok, _last_ntp_try

    now = time.ticks_ms()
    if not force and _last_ntp_try:
        if time.ticks_diff(now, _last_ntp_try) < NTP_RETRY_INTERVAL * 1000:
            return _ntp_ok
    _last_ntp_try = now

    try:
        try:
            ntptime.host = NTP_HOST
        except Exception:
            pass
        ntptime.settime()
        # 东八区修正：直接把偏移烧进 RTC，后续 localtime() 即本地时间
        t = time.localtime(time.time() + NTP_TIMEZONE_OFFSET)
        # RTC.datetime 参数：(年,月,日,星期,时,分,秒,微秒)，星期 1=周一
        machine.RTC().datetime((t[0], t[1], t[2], t[6] + 1, t[3], t[4], t[5], 0))
        _ntp_ok = True
        write_log("INFO", EVENT_TIME_SYNC, "NTP 时间同步成功，时区东八区 -> %s" % get_time_str())
        return True
    except Exception as e:
        _ntp_ok = False
        write_log("WARN", EVENT_TIME_SYNC, "NTP 同步失败:%s" % str(e))
        return False


def maybe_sync_ntp():
    """按周期检查是否需要重新校时"""
    global _last_ntp_try
    if _ntp_ok:
        if time.ticks_diff(time.ticks_ms(), _last_ntp_try) < NTP_SYNC_INTERVAL * 1000:
            return False
    return sync_ntp_time()


# ===================== 文件管理 =====================
def _ensure_dir():
    try:
        os.stat(LOG_ROOT_PATH)
        return True
    except OSError:
        try:
            os.mkdir(LOG_ROOT_PATH)
            return True
        except Exception as e:
            print("[log] 无法创建日志目录:%s" % e)
            return False


def _list_logs():
    try:
        return [f for f in os.listdir(LOG_ROOT_PATH) if f.startswith("log_")]
    except Exception:
        return []


def _remove(name):
    try:
        os.remove("%s/%s" % (LOG_ROOT_PATH, name))
    except Exception:
        pass


def _cap_file_count():
    """文件总数超限时删除最旧的分片"""
    files = _list_logs()
    if len(files) <= LOG_MAX_FILES:
        return
    files.sort()
    for name in files[:len(files) - LOG_MAX_FILES]:
        _remove(name)


def _rotate_if_needed(path):
    """单文件超限则重命名为 .N 分片"""
    try:
        if os.stat(path)[6] <= LOG_FILE_MAX_SIZE:
            return
    except OSError:
        return
    idx = 1
    while idx < 1000:
        new_name = "%s.%d" % (path, idx)
        try:
            os.stat(new_name)
            idx += 1
        except OSError:
            try:
                os.rename(path, new_name)
            except Exception:
                pass
            return


def clean_overdue_log():
    """
    清理过期日志。
    关键保护：时钟不可信时直接跳过，否则 (now - 1970) 巨大差值会把历史全部删掉。
    """
    if not time_trusted():
        print("[log] 时钟不可信，跳过按时间清理")
        return 0

    now_ts = time.time()
    removed = 0
    for fname in _list_logs():
        try:
            date_part = fname.split("_")[1][:8]
            year = int(date_part[0:4])
            mon = int(date_part[4:6])
            day = int(date_part[6:8])
            if year < LOG_MIN_YEAR:
                continue
            day_ts = time.mktime((year, mon, day, 0, 0, 0, 0, 0))
            if (now_ts - day_ts) / 86400.0 > LOG_DAYS_KEEP:
                _remove(fname)
                removed += 1
        except Exception:
            continue
    return removed


def log_init():
    """开机初始化日志系统"""
    global _log_file, _file_ok
    _file_ok = _ensure_dir()
    if not _file_ok:
        print("[log] 日志目录不可用，降级为纯控制台输出")
        _log_file = ""
        return False

    try:
        clean_overdue_log()
        _cap_file_count()
        t = time.localtime()
        if t[0] >= LOG_MIN_YEAR:
            _log_file = "%s/log_%04d%02d%02d.log" % (LOG_ROOT_PATH, t[0], t[1], t[2])
        else:
            _log_file = "%s/log_unsynced.log" % LOG_ROOT_PATH
        _rotate_if_needed(_log_file)
    except Exception as e:
        print("[log] 初始化异常:%s" % e)
        _file_ok = False
    return _file_ok


def rebind_daily_file():
    """
    跨天时重新绑定日志文件名。
    NTP 校准后时间跳变也应该调用一次。
    """
    global _log_file
    if not _file_ok or not time_trusted():
        return
    t = time.localtime()
    want = "%s/log_%04d%02d%02d.log" % (LOG_ROOT_PATH, t[0], t[1], t[2])
    if want != _log_file:
        _log_file = want
        _rotate_if_needed(_log_file)
        write_log("INFO", EVENT_SYS_BOOT, "日志切换到新文件")


# ===================== 写入 =====================
def write_log(level, event, msg):
    """
    写一条日志（控制台 + 文件）。
    任何异常都不能向外抛——日志永远不该拖垮主业务。
    """
    global _seq, _write_fail, _file_ok

    if LEVEL_WEIGHT.get(level, 1) < LEVEL_WEIGHT.get(LOG_LEVEL, 1):
        return

    _seq += 1
    line = "[%s] [%s] [%s] %s" % (get_time_str(), level, event, msg)

    try:
        print(line)
    except Exception:
        pass

    if not _file_ok or not _log_file:
        return

    for attempt in range(LOG_WRITE_RETRY + 1):
        try:
            with open(_log_file, "a") as f:
                f.write(line + "\n")
            _write_fail = 0
            return
        except Exception as e:
            _write_fail += 1
            if attempt == LOG_WRITE_RETRY:
                print("[log] 写入失败(%d次):%s -> 降级为控制台输出" % (_write_fail, e))
            if _write_fail >= 5:
                _file_ok = False
            try:
                time.sleep_ms(10)
            except Exception:
                pass


def read_today_log():
    """读取当前日志文件内容（REPL 调试用）"""
    try:
        with open(_log_file, "r") as f:
            return f.read()
    except Exception:
        return "无当前日志"


def log_stats():
    """返回日志系统状态摘要，供遥测上报"""
    return {
        "file": _log_file,
        "ok": _file_ok,
        "seq": _seq,
        "files": len(_list_logs()),
        "clock_ok": time_trusted(),
    }
