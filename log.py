import os
import time
import machine
import ntptime
from config import LOG_ROOT_PATH, LOG_DAYS_KEEP, LOG_FILE_MAX_SIZE, LOG_LEVEL

# 日志等级权重
LEVEL_WEIGHT = {
    "ERROR": 3,
    "WARN": 2,
    "INFO": 1
}
current_log_file = ""

# 初始化日志文件夹
def log_init():
    try:
        os.stat(LOG_ROOT_PATH)
    except OSError:
        os.mkdir(LOG_ROOT_PATH)
    # 开机清理7天前日志
    clean_overdue_log()
    # 生成当日日志文件名
    global current_log_file
    t = time.localtime()
    current_log_file = f"{LOG_ROOT_PATH}/log_{t[0]}{t[1]:02d}{t[2]:02d}.log"
    # 判断文件大小，超过阈值新建分片
    try:
        stat = os.stat(current_log_file)
        if stat[6] > LOG_FILE_MAX_SIZE:
            idx = 1
            while True:
                new_name = f"{current_log_file}.{idx}"
                try:
                    os.stat(new_name)
                    idx += 1
                except:
                    os.rename(current_log_file, new_name)
                    break
    except OSError:
        pass

# NTP同步网络时间（WiFi联网后调用）
def sync_ntp_time():
    try:
        ntptime.settime()
        # 东八区时区修正
        t = time.localtime(time.time() + NTP_TIMEZONE_OFFSET)
        machine.RTC().datetime((t[0], t[1], t[2], t[6]+1, t[3], t[4], t[5], 0))
        write_log("INFO", "TIME_SYNC", f"网络NTP时间同步成功，时区修正东八区")
        return True
    except Exception as e:
        write_log("WARN", "TIME_SYNC", f"时间同步失败:{str(e)}")
        return False

# 获取格式化时间戳字符串
def get_time_str():
    t = time.localtime()
    return f"{t[0]}-{t[1]:02d}-{t[2]:02d} {t[3]:02d}:{t[4]:02d}:{t[5]:02d}"

# 清理超过7天的日志文件
def clean_overdue_log():
    now_ts = time.time()
    file_list = os.listdir(LOG_ROOT_PATH)
    for fname in file_list:
        try:
            # 提取文件名日期 log_20260714.log
            date_part = fname.split("_")[1][:8]
            year = int(date_part[0:4])
            mon = int(date_part[4:6])
            day = int(date_part[6:8])
            # 构造对应日期时间戳
            day_ts = time.mktime((year, mon, day, 0,0,0,0,0))
            diff_day = (now_ts - day_ts) / (24 * 3600)
            if diff_day > LOG_DAYS_KEEP:
                os.remove(f"{LOG_ROOT_PATH}/{fname}")
        except Exception:
            continue

# 写入日志到文件+控制台打印
def write_log(level: str, event: str, msg: str):
    # 等级过滤
    if LEVEL_WEIGHT[level] < LEVEL_WEIGHT[LOG_LEVEL]:
        return
    ts = get_time_str()
    log_line = f"[{ts}] [{level}] [{event}] {msg}\n"
    # 控制台输出
    print(log_line.strip())
    # 写入本地日志文件
    try:
        with open(current_log_file, "a", encoding="utf-8") as f:
            f.write(log_line)
    except Exception as e:
        print("日志写入失败:", e)

# 读取日志（调试用，可在REPL调用）
def read_today_log():
    try:
        with open(current_log_file, "r", encoding="utf-8") as f:
            return f.read()
    except:
        return "无今日日志"