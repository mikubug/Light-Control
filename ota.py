# -*- coding: utf-8 -*-
"""
ota.py —— 远程固件升级（原子更新 + 失败回滚）

安全设计：
1. 只升级白名单内的文件，且**绝不包含 config.py**（凭据文件）。
2. 下载到 .tmp → 语法编译校验 → 备份原文件为 .bak → 原子替换。
3. 替换前写入 /OTA_PENDING 标记；下次启动成功联网后由 confirm_ota() 清除标记
   并删除备份。
4. 若带着标记启动且连续多次未确认，判定新固件有问题，自动回滚到 .bak。
"""

import os
import time
import machine

try:
    import ujson as json
except ImportError:
    import json

from config import *
from log import write_log

try:
    import urequests
except ImportError:
    urequests = None


# 由 main.py 注入的看门狗喂狗回调，下载期间定期调用防止误复位
_feed = None


def set_feeder(fn):
    global _feed
    _feed = fn


def _tick():
    if _feed is not None:
        try:
            _feed()
        except Exception:
            pass
def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _remove(path):
    try:
        os.remove(path)
        return True
    except Exception:
        return False


def _read(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception:
        return ""


def _write(path, data):
    try:
        with open(path, "w") as f:
            f.write(data)
        return True
    except Exception:
        return False


# ===================== 版本清单 =====================
def fetch_remote_version():
    """获取服务器版本清单；失败返回 None（不阻塞升级流程）"""
    if urequests is None or not OTA_VERSION_FILE:
        return None
    try:
        resp = urequests.get(OTA_SERVER + OTA_VERSION_FILE, timeout=OTA_TIMEOUT)
        if resp.status_code != 200:
            resp.close()
            return None
        data = json.loads(resp.content.decode())
        resp.close()
        if isinstance(data, dict):
            return data
        return None
    except Exception as e:
        write_log("WARN", EVENT_OTA_START, "版本清单获取失败:%s" % str(e))
        return None


def local_version_map():
    """本地已确认的升级版本记录"""
    try:
        return json.loads(_read(OTA_PENDING_FLAG))
    except Exception:
        return {}


# ===================== 下载与校验 =====================
def _download(filename, tmp_path):
    """下载单文件到临时路径，带重试"""
    if urequests is None:
        write_log("ERROR", EVENT_OTA_START, "urequests 不可用，跳过 OTA")
        return False

    url = OTA_SERVER + filename
    for attempt in range(OTA_MAX_RETRY):
        _tick()
        try:
            write_log("INFO", EVENT_OTA_START, "下载 %s（第 %d 次）" % (url, attempt + 1))
            resp = urequests.get(url, timeout=OTA_TIMEOUT)
            if resp.status_code != 200:
                resp.close()
                write_log("WARN", EVENT_OTA_START, "%s 状态码 %d" % (filename, resp.status_code))
                time.sleep(1)
                continue
            content = resp.content
            resp.close()
            if not content or len(content) < OTA_MIN_FILE_SIZE:
                write_log("WARN", EVENT_OTA_START, "%s 内容异常，长度 %d" % (filename, len(content) or 0))
                time.sleep(1)
                continue
            with open(tmp_path, "wb") as f:
                f.write(content)
            return True
        except Exception as e:
            write_log("WARN", EVENT_OTA_START, "%s 下载异常:%s" % (filename, str(e)))
            time.sleep(1)
    return False


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


def _same_as_local(tmp_path, filename):
    """
    与本地文件逐字节比对。
    没有 version.json 时这是唯一的"是否需要更新"判据——缺了它，每次巡检都会
    认定有更新、替换文件并重启，设备会变成每 10 分钟重启一次。
    """
    if not _exists(filename):
        return False
    new = _read_bytes(tmp_path)
    old = _read_bytes(filename)
    return new is not None and old is not None and new == old


def _verify_python(filename):
    """语法校验：下载的文件必须能被编译，否则拒绝替换"""
    try:
        src = _read(filename)
        if not src:
            return False
        compile(src, filename, "exec")
        return True
    except Exception as e:
        write_log("ERROR", EVENT_OTA_START, "%s 语法校验失败:%s" % (filename, str(e)))
        return False


def _backup(filename):
    bak = filename + OTA_BACKUP_SUFFIX
    _remove(bak)
    try:
        os.rename(filename, bak)
        return True
    except Exception as e:
        write_log("WARN", EVENT_OTA_START, "%s 备份失败:%s" % (filename, str(e)))
        return False


def _rollback_file(filename):
    bak = filename + OTA_BACKUP_SUFFIX
    if not _exists(bak):
        return False
    if _remove(filename):
        try:
            os.rename(bak, filename)
            write_log("WARN", EVENT_OTA_ROLLBACK, "%s 已回滚到备份版本" % filename)
            return True
        except Exception as e:
            write_log("ERROR", EVENT_OTA_ROLLBACK, "%s 回滚失败:%s" % (filename, str(e)))
    return False


# ===================== 回滚判定 =====================
def check_pending_rollback():
    """
    启动时调用：判断是否需要回滚。
    返回 True 表示已执行回滚（调用方应重新初始化）。
    """
    if not _exists(OTA_PENDING_FLAG):
        return False

    info = {}
    try:
        info = json.loads(_read(OTA_PENDING_FLAG))
    except Exception:
        info = {}

    boots = int(info.get("boots", 0)) + 1
    info["boots"] = boots
    _write(OTA_PENDING_FLAG, json.dumps(info))

    if boots < 2:
        # 首次启动，给新固件一次机会
        write_log("INFO", EVENT_OTA_START, "检测到待确认升级，第 %d 次启动" % boots)
        return False

    write_log("ERROR", EVENT_OTA_ROLLBACK, "新固件连续 %d 次未确认，执行回滚" % boots)
    for filename in info.get("files", []):
        _rollback_file(filename)
    _remove(OTA_PENDING_FLAG)
    return True


def confirm_ota():
    """升级后首次成功联网时调用：确认成功，清理备份与标记"""
    if not _exists(OTA_PENDING_FLAG):
        return False
    info = {}
    try:
        info = json.loads(_read(OTA_PENDING_FLAG))
    except Exception:
        info = {}
    for filename in info.get("files", []):
        _remove(filename + OTA_BACKUP_SUFFIX)
    _remove(OTA_PENDING_FLAG)
    write_log("INFO", EVENT_OTA_FINISH, "OTA 升级已确认，清理备份完成")
    return True


# ===================== 主流程 =====================
def check_and_upgrade():
    """
    检查并执行升级。
    返回：
      0 = 无更新或无需升级
      1 = 升级完成即将重启（调用方无需再处理）
     -1 = 升级失败
    """
    if not OTA_ENABLE:
        return 0
    if urequests is None:
        return 0

    remote = fetch_remote_version()
    if remote:
        if str(remote.get("version", "")) == FIRMWARE_VERSION:
            return 0
        write_log("INFO", EVENT_OTA_START, "发现新版本 %s（当前 %s）"
                  % (remote.get("version"), FIRMWARE_VERSION))

    updated = []
    failed = []

    for filename in OTA_FILE_LIST:
        _tick()
        if filename == "config.py":
            write_log("WARN", EVENT_OTA_START, "跳过 config.py（安全策略：禁止远程改写配置）")
            continue

        tmp_path = filename + OTA_TMP_SUFFIX
        if not _download(filename, tmp_path):
            failed.append(filename)
            _remove(tmp_path)
            continue
        if not _verify_python(tmp_path):
            failed.append(filename)
            _remove(tmp_path)
            continue

        if _same_as_local(tmp_path, filename):
            _remove(tmp_path)
            write_log("INFO", EVENT_OTA_START, "%s 与本地一致，无需更新" % filename)
            continue

        # 原子替换：先备份，再改名
        if _exists(filename):
            _backup(filename)
        _remove(filename)
        try:
            os.rename(tmp_path, filename)
            updated.append(filename)
            write_log("INFO", EVENT_OTA_FINISH, "%s 更新完成" % filename)
        except Exception as e:
            write_log("ERROR", EVENT_OTA_FINISH, "%s 替换失败:%s" % (filename, str(e)))
            failed.append(filename)
            _rollback_file(filename)
            _remove(tmp_path)

    if failed:
        write_log("WARN", EVENT_OTA_FINISH, "部分文件升级失败:%s" % ",".join(failed))
        for filename in updated:
            _rollback_file(filename)
        return -1

    if not updated:
        write_log("INFO", EVENT_OTA_START, "无文件需要更新")
        return 0

    # 写入待确认标记后重启
    _write(OTA_PENDING_FLAG, json.dumps({
        "files": updated,
        "boots": 0,
        "version": FIRMWARE_VERSION,
        "ts": time.time(),
    }))
    write_log("INFO", EVENT_OTA_FINISH, "全部文件更新完毕，3 秒后重启: %s" % ",".join(updated))
    time.sleep(3)
    machine.reset()
    return 1
