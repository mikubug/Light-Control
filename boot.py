# -*- coding: utf-8 -*-
"""
boot.py —— 最早执行的启动脚本

设计原则：越简单越安全。本文件绝不做任何可能抛异常导致无法进入 REPL 的复杂操作，
所有逻辑都包在 try/except 里。

关键功能：
1. 打印启动横幅（固件版本、复位原因、内存），便于串口诊断。
2. 维护模式窗口：开机 3 秒内按住 KEY1(GPIO17) 上电，写入 /MAINT_MODE 标记，
   main.py 检测到后只跑本地按键控制、不联网。固件出问题时靠这个入口抢救。
3. 清除与模块同名的目录（如 /log），避免遮蔽 import 导致崩溃循环。
"""

import time
import machine

MAINT_FLAG_FILE = "/MAINT_MODE"
BOOT_WINDOW_MS = 3000
BOOT_KEY_PIN = 17


def _banner():
    try:
        reset_cause = machine.reset_cause()
        causes = {
            machine.PWRON_RESET: "上电复位",
            machine.HARD_RESET: "硬件复位",
            machine.WDT_RESET: "看门狗复位",
            machine.DEEPSLEEP_RESET: "深度睡眠唤醒",
            machine.SOFT_RESET: "软复位",
        }
        print("=" * 46)
        print(" ESP32-S3 教室灯光控制 / ZZ-ESP32-S3-Nano-EB-V20")
        print(" 复位原因: %s(%d)" % (causes.get(reset_cause, "未知"), reset_cause))
        try:
            import gc
            print(" 可用内存: %d bytes" % gc.mem_free())
        except Exception:
            pass
        try:
            mac = machine.unique_id()
            print(" 设备 MAC: %s" % "".join(["%02X" % b for b in mac]))
        except Exception:
            pass
        print("=" * 46)
    except Exception:
        pass


def _check_maint_key():
    """开机窗口内检测维护按键是否被按住"""
    try:
        key = machine.Pin(BOOT_KEY_PIN, machine.Pin.IN, machine.Pin.PULL_DOWN)
        deadline = time.ticks_add(time.ticks_ms(), BOOT_WINDOW_MS)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            if key.value() == 1:
                return True
            time.sleep_ms(50)
    except Exception:
        return False
    return False


def _clear_module_shadow():
    """
    清掉与模块同名的目录。

    MicroPython 里目录包优先级高于 .py 文件：根目录一旦出现 /log 目录，
    "from log import ..." 就会去找 /log/log_xxx.py 而不是 /log.py，
    直接抛 ImportError 把设备打进崩溃循环，且没法靠 OTA 自愈。
    这里在 boot 阶段（main.py 之前、且全程 try 包裹）把它清掉。
    """
    import os
    for name in ("log", "func", "ota", "config", "main"):
        try:
            path = "/" + name
            if not (os.stat(path)[0] & 0x4000):   # 0x4000 = S_IFDIR
                continue                          # 是文件不是目录，正常
            left = []
            for f in os.listdir(path):
                try:
                    os.remove("%s/%s" % (path, f))
                except Exception:
                    left.append(f)
            if left:
                continue                          # 里面有清不掉的东西，别硬删
            os.rmdir(path)
            print("[boot] 已清除与模块同名的目录 %s（会遮蔽 import）" % path)
        except Exception:
            continue


try:
    _banner()
    _clear_module_shadow()
    print("[boot] 开机窗口 3 秒：按住 KEY1 可进入维护模式")
    if _check_maint_key():
        try:
            with open(MAINT_FLAG_FILE, "w") as f:
                f.write("1")
            print("[boot] >>> 已写入维护模式标记，本次跳过联网 <<<")
        except Exception as e:
            print("[boot] 维护标记写入失败:%s" % e)
    else:
        # 正常启动，清除遗留标记
        try:
            import os
            os.remove(MAINT_FLAG_FILE)
        except Exception:
            pass
except Exception:
    # boot.py 绝不能因异常阻断后续启动
    pass
