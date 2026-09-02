import urequests
import os
import machine
import time
from config import *
from log_manager import write_log

# OTA文件服务器地址，自行替换你的文件服务地址
OTA_SERVER = "http://192.168.1.100/esp32s3/"
FILE_LIST = ["config.py", "light_func.py"]

def download_file(filename):
    url = OTA_SERVER + filename
    try:
        write_log("INFO", EVENT_OTA_START, f"开始下载文件 {url}")
        resp = urequests.get(url, timeout=5)
        if resp.status_code == 200:
            with open(filename, "wb") as f:
                f.write(resp.content)
            write_log("INFO", EVENT_OTA_FINISH, f"{filename} 更新完成")
            resp.close()
            return True
        else:
            write_log("ERROR", EVENT_OTA_START, f"{filename} 下载失败，状态码:{resp.status_code}")
            resp.close()
            return False
    except Exception as e:
        write_log("ERROR", EVENT_OTA_START, f"OTA下载异常:{str(e)}")
        return False

def check_and_upgrade():
    if not wifi_is_online():
        write_log("WARN", EVENT_OTA_START, "WiFi离线，无法OTA升级")
        return False
    update_success = True
    for file in FILE_LIST:
        if not download_file(file):
            update_success = False
    if update_success:
        write_log("INFO", EVENT_OTA_FINISH, "全部文件更新完毕，3秒后重启设备")
        time.sleep(3)
        machine.reset()
    else:
        write_log("WARN", EVENT_OTA_FINISH, "部分文件更新失败，跳过重启")
    return update_success