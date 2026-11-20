#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
蓝牙设备电量 · 系统托盘工具 (Windows 10/11)

数据来源：Windows PnP 设备属性 DEVPKEY_Device_BatteryPercentage
          {104EA319-6EE2-4701-BD47-8DDBF425BBE5} pid=2
          这是「设置 -> 蓝牙和其他设备」里那个百分比的同一份数据，
          经典蓝牙 (BTHENUM) 与低功耗蓝牙 (BTHLE) 都能覆盖。

运行：双击 start.vbs（无控制台窗口）
调试：python bt_battery.py --selftest   或  双击 run.bat
配置：同目录 config.json（托盘菜单里可直接打开）

无需管理员权限；依赖放在 vendor/ 目录，整个文件夹可以自由搬动。
"""
from __future__ import annotations

import ctypes as C
import json
import os
import sys
import threading
import time
import winreg
from ctypes import wintypes as W
from datetime import datetime

# 打包成单文件 exe 后 __file__ 指向临时解压目录，配置必须落在 exe 旁边，
# 否则用户改不了 config.json，拖动记住的位置也会在重启后丢失。
def _runtime_dir() -> str:
    """打包版返回 exe 的真实所在目录。

    不用 sys.executable：PyInstaller onefile 的子进程里它可能指向临时解压
    目录，导致 config.json 读到别处（用户改的配置不生效）。GetModuleFileNameW
    拿的是进程映像路径，最权威。
    """
    try:
        k32 = C.WinDLL("kernel32.dll", use_last_error=True)
        k32.GetModuleFileNameW.argtypes = [C.c_void_p, W.LPWSTR, W.DWORD]
        k32.GetModuleFileNameW.restype = W.DWORD
        buf = C.create_unicode_buffer(1024)
        if k32.GetModuleFileNameW(None, buf, 1024):
            return os.path.dirname(buf.value)
    except Exception:
        pass
    return os.path.dirname(os.path.abspath(sys.executable))


if getattr(sys, "frozen", False):
    BASE = _runtime_dir()
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(BASE, "vendor")
if os.path.isdir(VENDOR):
    sys.path.insert(0, VENDOR)
CONFIG_PATH = os.path.join(BASE, "config.json")
RUN_VALUE_NAME = "BtBatteryWidget"
MUTEX_NAME = "BtBatteryWidgetSingleton"

DEFAULTS = {
    "_help": "改完保存后点托盘「立即刷新」生效。primary_device 填设备名片段（留空=自动显示最低电量）；hidden_devices 填不显示的设备名。",
    "refresh_seconds": 60,          # 轮询间隔（秒）
    "low_battery_threshold": 20,    # 低电量提醒阈值（%）
    "low_battery_alert": True,      # 是否弹低电量通知
    "primary_device": "",           # 图标固定显示哪个设备；留空 = 自动显示最低电量
    "hidden_devices": [],           # 不参与统计/显示的设备名，精确匹配
    "clean_names": True,            # 去掉 "Hands-Free AG" 之类的服务后缀
}

NAME_SUFFIXES = (
    " Hands-Free AG", " Hands-Free", " Avrcp 传输", " Avrcp Transport",
    " A2DP", " Headset", " Stereo", " Audio", " 免提", " 音频",
)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        if isinstance(user, dict):
            for k in DEFAULTS:
                if k in user:
                    cfg[k] = user[k]
    except (OSError, ValueError):
        pass
    # 收敛非法值，避免配置写错导致工具崩掉
    try:
        cfg["refresh_seconds"] = max(10, min(3600, int(cfg["refresh_seconds"])))
    except (TypeError, ValueError):
        cfg["refresh_seconds"] = DEFAULTS["refresh_seconds"]
    try:
        cfg["low_battery_threshold"] = max(1, min(99, int(cfg["low_battery_threshold"])))
    except (TypeError, ValueError):
        cfg["low_battery_threshold"] = DEFAULTS["low_battery_threshold"]
    if not isinstance(cfg["hidden_devices"], list):
        cfg["hidden_devices"] = []
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def ensure_config() -> dict:
    cfg = load_config()
    if not os.path.exists(CONFIG_PATH):
        save_config(cfg)
    return cfg


# ---------------------------------------------------------------------------
# 数据层：SetupAPI / ctypes，不启动子进程、不需要 PowerShell
# ---------------------------------------------------------------------------

class GUID(C.Structure):
    _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD),
                ("Data4", C.c_ubyte * 8)]


class SP_DEVINFO_DATA(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("ClassGuid", GUID),
                ("DevInst", W.DWORD), ("Reserved", C.c_void_p)]


class DEVPROPKEY(C.Structure):
    _fields_ = [("fmtid", GUID), ("pid", W.DWORD)]


# {104EA319-6EE2-4701-BD47-8DDBF425BBE5} / 2
BATTERY_KEY = DEVPROPKEY(
    GUID(0x104EA319, 0x6EE2, 0x4701,
         (C.c_ubyte * 8)(0xBD, 0x47, 0x8D, 0xDB, 0xF4, 0x25, 0xBB, 0xE5)), 2,
)

# DEVPKEY_Bluetooth_LastConnectedTime: FILETIME (UTC)，仅 BLE 设备有。
# 电量属性是「最后一次上报的缓存值」，断开不会清空；用这个时间才能分辨新鲜度。
LAST_CONNECTED_KEY = DEVPROPKEY(
    GUID(0x2BD67D8B, 0x8BEB, 0x48D5,
         (C.c_ubyte * 8)(0x87, 0xE0, 0x6C, 0xDA, 0x34, 0x28, 0x04, 0x0A)), 11,
)

# DEVPKEY_Bluetooth_ClassOfDevice: 24 位 CoD，主/次设备类可直接判断鼠标/耳机。
CLASS_OF_DEVICE_KEY = DEVPROPKEY(
    GUID(0x2BD67D8B, 0x8BEB, 0x48D5,
         (C.c_ubyte * 8)(0x87, 0xE0, 0x6C, 0xDA, 0x34, 0x28, 0x04, 0x0A)), 10,
)
# DEVPKEY_Device_ContainerId: 一台物理设备会生成多个 PnP 节点，
# BLE 主节点 (BTHLE\Dev_xxx) 本身只有 "Bluetooth" 类，鼠标/键盘的真实
# 类别写在同 ContainerId 的 HID 子节点上，必须靠它才能认出是鼠标。
CONTAINER_ID_KEY = DEVPROPKEY(
    GUID(0x8C7ED206, 0x3F8A, 0x4827,
         (C.c_ubyte * 8)(0xB3, 0xAB, 0xAE, 0x9E, 0x1F, 0xAE, 0xFC, 0x6C)), 2,
)

# DEVPKEY_Device_IsConnected: DEVPROP_TYPE_BOOLEAN (1 字节，0/255)。
# 这是 Windows 对每个 PnP 节点维护的「此刻是否连着」，断开时会立刻翻成 0，
# 但电量属性 DEVPKEY_Device_BatteryPercentage 只是最后一次上报的缓存、不会清空。
# 一台物理设备会拆成很多节点（蓝牙主节点 / HID / AudioEndpoint），必须按
# ContainerId 把整组节点的 IsConnected 取 OR，只看带电量那个节点会误判。
IS_CONNECTED_KEY = DEVPROPKEY(
    GUID(0x83DA6326, 0x97A6, 0x4088,
         (C.c_ubyte * 8)(0x94, 0x53, 0xA1, 0x92, 0x3F, 0x57, 0x3B, 0x29)), 15,
)

DIGCF_PRESENT = 0x02
DIGCF_ALLCLASSES = 0x04
SPDRP_CLASS = 0x07
SPDRP_FRIENDLYNAME = 0x0C
INVALID_HANDLE_VALUE = W.HANDLE(-1).value

VALID_KINDS = ("headset", "gamepad", "speaker", "mouse", "keyboard", "bluetooth")

_sa = None


def _setupapi():
    global _sa
    if _sa is not None:
        return _sa
    sa = C.WinDLL("SetupAPI.dll", use_last_error=True)
    sa.SetupDiGetClassDevsW.argtypes = [W.LPCWSTR, W.LPCWSTR, W.HWND, W.DWORD]
    sa.SetupDiGetClassDevsW.restype = W.HANDLE
    sa.SetupDiEnumDeviceInfo.argtypes = [W.HANDLE, W.DWORD, C.POINTER(SP_DEVINFO_DATA)]
    sa.SetupDiEnumDeviceInfo.restype = W.BOOL
    sa.SetupDiGetDeviceInstanceIdW.argtypes = [
        W.HANDLE, C.POINTER(SP_DEVINFO_DATA), W.LPWSTR, W.DWORD, C.POINTER(W.DWORD)]
    sa.SetupDiGetDeviceInstanceIdW.restype = W.BOOL
    sa.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        W.HANDLE, C.POINTER(SP_DEVINFO_DATA), W.DWORD, C.POINTER(W.DWORD),
        C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    sa.SetupDiGetDeviceRegistryPropertyW.restype = W.BOOL
    sa.SetupDiGetDevicePropertyW.argtypes = [
        W.HANDLE, C.POINTER(SP_DEVINFO_DATA), C.POINTER(DEVPROPKEY),
        C.POINTER(W.DWORD), C.c_void_p, W.DWORD, C.POINTER(W.DWORD), W.DWORD]
    sa.SetupDiGetDevicePropertyW.restype = W.BOOL
    sa.SetupDiDestroyDeviceInfoList.argtypes = [W.HANDLE]
    sa.SetupDiDestroyDeviceInfoList.restype = W.BOOL
    _sa = sa
    return sa


def _clean_name(name: str, enabled: bool) -> str:
    if not enabled:
        return name
    changed = True
    while changed:
        changed = False
        for suffix in NAME_SUFFIXES:
            if name.lower().endswith(suffix.lower()):
                name = name[: -len(suffix)].strip()
                changed = True
    return name or "蓝牙设备"


# ---------------------------------------------------------------------------
# 实时连接状态
#
# 电量是「最后一次上报后留在 PnP 节点里的缓存值」，断开不会清空，所以必须
# 另找信号判断设备此刻到底连没连：
#   1) BluetoothAPIs.dll!BluetoothFindFirstDevice 的 fConnected（经典蓝牙实时）
#   2) MMDevices 音频端点 DeviceState（1=ACTIVE，8=UNPLUGGED，4=NOTPRESENT）
#   3) 都没有时退回 DEVPKEY_Bluetooth_LastConnectedTime 的新鲜度（BLE 缓存）
# ---------------------------------------------------------------------------

class SYSTEMTIME(C.Structure):
    _fields_ = [("wYear", W.WORD), ("wMonth", W.WORD), ("wDayOfWeek", W.WORD),
                ("wDay", W.WORD), ("wHour", W.WORD), ("wMinute", W.WORD),
                ("wSecond", W.WORD), ("wMilliseconds", W.WORD)]


class BT_DEVICE_INFO(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("Address", C.c_ulonglong),
                ("ulClassofDevice", W.ULONG), ("fConnected", W.BOOL),
                ("fRemembered", W.BOOL), ("fAuthenticated", W.BOOL),
                ("stLastSeen", SYSTEMTIME), ("stLastUsed", SYSTEMTIME),
                ("szName", W.WCHAR * 248)]


class BT_FIND_PARAMS(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("fReturnAuthenticated", W.BOOL),
                ("fReturnRemembered", W.BOOL), ("fReturnUnknown", W.BOOL),
                ("fReturnConnected", W.BOOL), ("fIssueInquiry", W.BOOL),
                ("cTimeoutMultiplier", C.c_ubyte), ("hRadio", C.c_void_p)]


def read_classic_connections() -> dict[str, bool]:
    """{小写设备名: 当前是否连接}；只枚举已配对设备，不发起蓝牙扫描。"""
    out: dict[str, bool] = {}
    try:
        dll = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                           "System32", "BluetoothAPIs.dll")
        bth = C.WinDLL(dll, use_last_error=True)
        bth.BluetoothFindFirstDevice.argtypes = [C.POINTER(BT_FIND_PARAMS),
                                                 C.POINTER(BT_DEVICE_INFO)]
        bth.BluetoothFindFirstDevice.restype = C.c_void_p
        bth.BluetoothFindNextDevice.argtypes = [C.c_void_p,
                                                C.POINTER(BT_DEVICE_INFO)]
        bth.BluetoothFindNextDevice.restype = W.BOOL
        bth.BluetoothFindDeviceClose.argtypes = [C.c_void_p]
        bth.BluetoothFindDeviceClose.restype = W.BOOL

        params = BT_FIND_PARAMS()
        params.dwSize = C.sizeof(params)
        params.fReturnAuthenticated = 1
        params.fReturnRemembered = 1
        params.fReturnUnknown = 1
        params.fReturnConnected = 1
        params.fIssueInquiry = 0
        params.cTimeoutMultiplier = 1
        info = BT_DEVICE_INFO()
        info.dwSize = C.sizeof(info)
        handle = bth.BluetoothFindFirstDevice(C.byref(params), C.byref(info))
        if not handle:
            return out
        try:
            while True:
                if info.szName:
                    key = info.szName.lower()
                    # 同名多个记录时，只要有一条是已连接就算连接
                    out[key] = out.get(key, False) or bool(info.fConnected)
                info.dwSize = C.sizeof(info)
                if not bth.BluetoothFindNextDevice(handle, C.byref(info)):
                    break
        finally:
            bth.BluetoothFindDeviceClose(handle)
    except Exception:
        pass
    return out


AUDIO_NAME_KEYS = (r"{b3f8fa53-0004-438e-9003-51a46e139bfc},6",
                   r"{a45c254e-df1c-4efd-8020-67d146a850e0},14")


def read_audio_endpoint_states() -> dict[str, int]:
    """{小写音频端点名: DeviceState 低位}；同名多端点时 ACTIVE 优先。"""
    out: dict[str, int] = {}
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
    for flow in ("Render", "Capture"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                base + "\\" + flow) as root:
                guids = [winreg.EnumKey(root, i)
                         for i in range(winreg.QueryInfoKey(root)[0])]
            for guid in guids:
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                        base + "\\" + flow + "\\" + guid) as sk:
                        state = winreg.QueryValueEx(sk, "DeviceState")[0] & 0xF
                        name = ""
                        try:
                            with winreg.OpenKey(sk, "Properties") as pk:
                                for key in AUDIO_NAME_KEYS:
                                    try:
                                        raw = winreg.QueryValueEx(pk, key)[0]
                                        name = (raw.decode("utf-16-le", "ignore")
                                                .strip("\x00") if isinstance(raw, bytes)
                                                else str(raw))
                                        if name:
                                            break
                                    except OSError:
                                        pass
                        except OSError:
                            pass
                    if name:
                        k = name.lower()
                        # 1 = ACTIVE，优先级最高
                        if k not in out or state == 1:
                            out[k] = state
                except OSError:
                    continue
        except OSError:
            continue
    return out


def enrich_connections(devices: list[dict], cfg: dict) -> None:
    """给每台设备写 connected / age_days / offline / offline_reason。

    判据优先级：
      1) 容器级 DEVPKEY_Device_IsConnected —— Windows 实时维护，断开立刻变 0，
         覆盖经典蓝牙 / BLE / HID / 音频端点，是唯一能通杀所有设备的信号。
      2) BluetoothAPIs 的 fConnected（经典蓝牙实时）。
      3) 音频端点 DeviceState（1=ACTIVE / 8=UNPLUGGED）。
      4) 以上都缺时才看 DEVPKEY_Bluetooth_LastConnectedTime 的新鲜度。

    任何「没有明确在线证据」的设备都判为离线。旧逻辑把这类设备当作
    「近期连过 → 显示」，结果刚断开的手柄/音箱会一直挂在任务栏上。
    """
    classic = read_classic_connections()
    audio = read_audio_endpoint_states()
    stale_days = int(cfg.get("stale_days", 2))
    now = datetime.utcnow()

    def lookup(table: dict, name: str):
        for key, value in table.items():
            if key and (key in name or name in key):
                return value
        return None

    for dev in devices:
        name = dev["name"].lower()
        pnp = dev.get("pnp_connected")            # True / False / None
        classic_v = lookup(classic, name)          # True / False / None
        audio_state = lookup(audio, name)          # int / None
        audio_v = (audio_state == 1) if audio_state is not None else None

        # 任一信号明确在线就算在线（不同 Windows 版本下信号覆盖不一致）
        signals = (pnp, classic_v, audio_v)
        if True in signals:
            conn = True
        elif False in signals:
            conn = False
        else:
            conn = None

        lc = dev.get("last_connected")
        age = (now - lc).days if isinstance(lc, datetime) else None
        dev["connected"] = conn
        dev["age_days"] = age
        dev["signal"] = "pnp=%s bt=%s audio=%s" % (pnp, classic_v, audio_state)

        # offline 一律是布尔：UI 的 hide_disconnected 只认 offline is True。
        # 这样「查不到实时信号」不会再把断开设备漏进任务栏。
        if conn is True:
            dev["offline"] = False
            dev["offline_reason"] = ""
        elif conn is False:
            dev["offline"] = True
            dev["offline_reason"] = "未连接"
        elif age is not None and age >= stale_days:
            dev["offline"] = True
            dev["offline_reason"] = "缓存 %d 天" % age
        else:
            dev["offline"] = True
            dev["offline_reason"] = "无连接信号"


def _reg_string(sa, handle, dev, prop) -> str:
    buf = C.create_unicode_buffer(512)
    reg_type, needed = W.DWORD(0), W.DWORD(0)
    if sa.SetupDiGetDeviceRegistryPropertyW(
            handle, C.byref(dev), prop, C.byref(reg_type),
            buf, C.sizeof(buf), C.byref(needed)):
        return buf.value
    return ""


def _guid_str(g: GUID) -> str:
    d = bytes(g.Data4)
    return "{%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X}" % (
        g.Data1, g.Data2, g.Data3, *d)


def _dev_prop(sa, handle, dev, key, buf):
    prop_type, prop_size = W.DWORD(0), W.DWORD(0)
    ok = sa.SetupDiGetDevicePropertyW(
        handle, C.byref(dev), C.byref(key), C.byref(prop_type),
        C.byref(buf), C.sizeof(buf), C.byref(prop_size), 0)
    return (buf if ok else None), prop_size.value


def _kind_from_name(name: str):
    n = name.lower()
    if any(k in n for k in ("controller", "gamepad", "dualsense", "xbox", "手柄")):
        return "gamepad"
    if any(k in n for k in ("speaker", "音箱", "srs-", "jbl ", "soundlink")):
        return "speaker"
    if any(k in n for k in ("headset", "headphone", "tws", "buds", "earbud",
                            "耳机", "airpods", "wh-", "wf-", "freebuds")):
        return "headset"
    if any(k in n for k in ("mouse", "鼠标", "mx master")):
        return "mouse"
    if any(k in n for k in ("keyboard", "键盘")):
        return "keyboard"
    return None


def infer_device_kind(name: str, classes: set, cod, cfg: dict) -> str:
    """用户指定 → 设备名 → PnP 真实类别 → 蓝牙 CoD → 通用蓝牙符文。"""
    overrides = cfg.get("device_icons")
    if isinstance(overrides, dict) and overrides.get(name) in VALID_KINDS:
        return overrides[name]
    k = _kind_from_name(name)
    if k:
        return k
    if "Mouse" in classes:
        return "mouse"
    if "Keyboard" in classes:
        return "keyboard"
    if "AudioEndpoint" in classes or "MEDIA" in classes:
        return "headset"
    if "HIDClass" in classes:
        return "mouse"
    if cod:
        major, minor = (cod >> 8) & 0x1F, (cod >> 2) & 0x3F
        if major == 0x05:                    # Peripheral (HID)
            sub = (minor >> 4) & 0x03       # 1=键盘 2=鼠标 3=键鼠一体
            return {1: "keyboard", 2: "mouse", 3: "mouse"}.get(sub, "bluetooth")
        if major == 0x04:                    # Audio/Video
            return "speaker" if minor == 0x05 else "headset"
    return "bluetooth"


def _channel_base(name: str) -> str:
    """某双通道接收器 这类多通道设备归一到同一台物理设备。"""
    import re
    return re.sub(r"\s*bt\d+\s*$", "", name.lower()).strip()


def connection_signature() -> tuple:
    """轻量连接快照，用于高频探测设备上下线（实测约 6ms，可每秒跑）。

    只做一次 SetupDi 遍历读 PnP 属性，不枚举 BluetoothAPIs / COM 音频端点。
    返回按设备名排序的 ((name, level, connected), ...) 元组；任何一台设备
    上线、下线、电量变化或出现新设备，签名都会变。
    """
    sa = _setupapi()
    handle = sa.SetupDiGetClassDevsW(
        None, None, None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if not handle or handle == INVALID_HANDLE_VALUE:
        return ()

    container_conn: dict[str, bool] = {}
    devices: dict[str, dict] = {}
    try:
        index = 0
        while True:
            dev = SP_DEVINFO_DATA()
            dev.cbSize = C.sizeof(SP_DEVINFO_DATA)
            if not sa.SetupDiEnumDeviceInfo(handle, index, C.byref(dev)):
                break
            index += 1
            instance = C.create_unicode_buffer(512)
            needed = W.DWORD(0)
            if not sa.SetupDiGetDeviceInstanceIdW(
                    handle, C.byref(dev), instance, 512, C.byref(needed)):
                continue
            iid = instance.value

            cid_raw, cid_size = _dev_prop(
                sa, handle, dev, CONTAINER_ID_KEY, GUID())
            cid = _guid_str(cid_raw) if cid_raw and cid_size == 16 else None
            if cid:
                isc, _ = _dev_prop(sa, handle, dev, IS_CONNECTED_KEY,
                                   C.c_uint8())
                if isc is not None:
                    container_conn[cid] = (container_conn.get(cid, False)
                                           or bool(isc.value))

            if not iid.upper().startswith(("BTHENUM\\", "BTHLE\\")):
                continue
            raw = (C.c_uint32 * 2)()
            pt, sz = W.DWORD(0), W.DWORD(0)
            if not sa.SetupDiGetDevicePropertyW(
                    handle, C.byref(dev), C.byref(BATTERY_KEY),
                    C.byref(pt), raw, C.sizeof(raw), C.byref(sz), 0):
                continue
            level = int(raw[0])
            if not 0 <= level <= 100:
                continue
            name = _clean_name(
                _reg_string(sa, handle, dev, SPDRP_FRIENDLYNAME)
                or iid.split("\\")[-1], True)
            d = devices.setdefault(name, {"level": level, "cids": set()})
            d["level"] = min(d["level"], level)
            if cid:
                d["cids"].add(cid)
    finally:
        sa.SetupDiDestroyDeviceInfoList(handle)

    rows = []
    for name, d in devices.items():
        vals = [container_conn[c] for c in d["cids"] if c in container_conn]
        rows.append((name, d["level"], True if any(vals) else False))
    return tuple(sorted(rows))


def read_batteries(cfg: dict) -> list[dict]:
    """返回 [{'name','level','last_connected','connected','offline','kind',...}]。"""
    sa = _setupapi()
    handle = sa.SetupDiGetClassDevsW(None, None, None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise OSError(C.get_last_error(), "SetupDiGetClassDevsW 失败")

    found: dict[str, dict] = {}
    meta: dict[str, dict] = {}
    container_classes: dict[str, set] = {}
    container_conn: dict[str, bool] = {}
    try:
        index = 0
        while True:
            dev = SP_DEVINFO_DATA()
            dev.cbSize = C.sizeof(SP_DEVINFO_DATA)
            if not sa.SetupDiEnumDeviceInfo(handle, index, C.byref(dev)):
                break
            index += 1

            instance = C.create_unicode_buffer(512)
            needed = W.DWORD(0)
            if not sa.SetupDiGetDeviceInstanceIdW(
                    handle, C.byref(dev), instance, 512, C.byref(needed)):
                continue
            iid = instance.value

            # 建立 ContainerId -> {设备类}：BLE 主节点的真实类别写在同容器的
            # HID / AudioEndpoint 子节点里，例如 示例鼠标 下有 Mouse 节点。
            cid_raw, cid_size = _dev_prop(sa, handle, dev, CONTAINER_ID_KEY, GUID())
            cid = _guid_str(cid_raw) if cid_raw and cid_size == 16 else None
            cls = _reg_string(sa, handle, dev, SPDRP_CLASS)
            if cid and cls:
                container_classes.setdefault(cid, set()).add(cls)
            if cid:
                # 每个节点的 IsConnected 单独看都会误判（带电量那个蓝牙节点
                # 常常是 0，而真正连着的是 HID / AudioEndpoint 子节点），
                # 所以整组容器取 OR：有一个节点连着就算这台设备在线。
                isc, _ = _dev_prop(sa, handle, dev, IS_CONNECTED_KEY,
                                   C.c_uint8())
                if isc is not None:
                    container_conn[cid] = (container_conn.get(cid, False)
                                           or bool(isc.value))

            if not iid.upper().startswith(("BTHENUM\\", "BTHLE\\")):
                continue

            raw = (C.c_uint32 * 2)()
            prop_type = W.DWORD(0)
            prop_size = W.DWORD(0)
            if not sa.SetupDiGetDevicePropertyW(
                    handle, C.byref(dev), C.byref(BATTERY_KEY),
                    C.byref(prop_type), raw, C.sizeof(raw),
                    C.byref(prop_size), 0):
                continue
            level = int(raw[0])
            if not 0 <= level <= 100:
                continue

            # 最后连接时间（FILETIME，100ns since 1601-01-01 UTC）
            last_connected = None
            ft = (C.c_uint64 * 1)()
            pt2, sz2 = W.DWORD(0), W.DWORD(0)
            if sa.SetupDiGetDevicePropertyW(
                    handle, C.byref(dev), C.byref(LAST_CONNECTED_KEY),
                    C.byref(pt2), ft, C.sizeof(ft), C.byref(sz2), 0) and ft[0]:
                try:
                    unix_ts = ft[0] / 10_000_000 - 11644473600
                    last_connected = datetime.utcfromtimestamp(unix_ts)
                except (OverflowError, OSError, ValueError):
                    pass

            name = _clean_name(
                _reg_string(sa, handle, dev, SPDRP_FRIENDLYNAME)
                or iid.split("\\")[-1],
                bool(cfg.get("clean_names", True)),
            )
            cod_raw, _ = _dev_prop(sa, handle, dev, CLASS_OF_DEVICE_KEY,
                                   C.c_uint32())

            m = meta.setdefault(name, {"cids": set(), "cod": None})
            if cid:
                m["cids"].add(cid)
            if cod_raw and cod_raw.value:
                m["cod"] = int(cod_raw.value)
            # 同名多个部件（左右耳等）取较低电量；LastConnectedTime 常只写在
            # 另一个节点上，所以跨节点取最新的一次，避免经典蓝牙被漏判成"无缓存信息"。
            if name not in found:
                found[name] = {"name": name, "level": level,
                               "last_connected": last_connected}
            else:
                cur = found[name]
                cur["level"] = min(cur["level"], level)
                if last_connected and (cur["last_connected"] is None
                                       or last_connected > cur["last_connected"]):
                    cur["last_connected"] = last_connected
    finally:
        sa.SetupDiDestroyDeviceInfoList(handle)

    for name, d in found.items():
        classes = set()
        for c in meta[name]["cids"]:
            classes |= container_classes.get(c, set())
        d["kind"] = infer_device_kind(name, classes, meta[name]["cod"], cfg)
        d["pnp_classes"] = sorted(classes)
        vals = [container_conn[c] for c in meta[name]["cids"]
                if c in container_conn]
        d["pnp_connected"] = True if any(vals) else (False if vals else None)

    # 同一台设备的其它蓝牙通道（离线时没有 HID 子节点）沿用已识别的类型
    by_base: dict[str, str] = {}
    for d in found.values():
        if d["kind"] != "bluetooth":
            by_base.setdefault(_channel_base(d["name"]), d["kind"])
    for d in found.values():
        if d["kind"] == "bluetooth":
            d["kind"] = by_base.get(_channel_base(d["name"]), "bluetooth")

    devices = sorted(found.values(), key=lambda d: (d["level"], d["name"]))
    enrich_connections(devices, cfg)
    return devices


# ---------------------------------------------------------------------------
# 图标绘制
# ---------------------------------------------------------------------------

def color_for(level: int | None) -> tuple[int, int, int, int]:
    if level is None:
        return (150, 156, 166, 255)
    if level < 20:
        return (235, 76, 68, 255)       # 红：快没电
    if level < 50:
        return (242, 168, 44, 255)      # 橙：该充电了
    return (70, 205, 126, 255)         # 绿：健康


def _font(size: int):
    from PIL import ImageFont
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for filename in ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"):
        path = os.path.join(windir, "Fonts", filename)
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def make_icon(level: int | None):
    """64x64 RGBA：深色圆角徽章 + 大号百分比 + 底部电量条。"""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([1, 1, 62, 62], radius=15,
                          fill=(26, 29, 35, 246), outline=(238, 241, 246, 110), width=1)

    color = color_for(level)
    text = "--" if level is None else str(level)
    font = _font(34 if level is not None and level < 100 else 30)
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    draw.text(((size - tw) / 2 - box[0], 9 - box[1]), text, font=font,
              fill=color, stroke_width=1, stroke_fill=color)

    # 底部细电量条，缩到 16px 时仍能看出大致比例和颜色
    draw.rounded_rectangle([10, 49, 53, 56], radius=3, fill=(255, 255, 255, 48))
    if level is not None:
        width = int(43 * max(0, min(100, level)) / 100)
        if width > 0:
            draw.rounded_rectangle([10, 49, 10 + width, 56], radius=3, fill=color)
    return img


# ---------------------------------------------------------------------------
# 开机自启 / 单例
# ---------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    # 安装版（单文件 exe）直接指向自身，不再依赖脚本宿主或解释器路径；
    # 源码调试版仍走 launch.bat，它每次运行都会重新探测解释器。
    if getattr(sys, "frozen", False):
        target = os.path.abspath(sys.executable)
    else:
        target = os.path.join(BASE, "launch.bat")
    command = '"%s"' % target
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except OSError:
                    pass
    except OSError:
        pass


_MUTEX_HANDLE = None


def acquire_singleton() -> bool:
    global _MUTEX_HANDLE
    k32 = C.WinDLL("kernel32.dll", use_last_error=True)
    k32.CreateMutexW.argtypes = [C.c_void_p, W.BOOL, W.LPCWSTR]
    k32.CreateMutexW.restype = W.HANDLE
    _MUTEX_HANDLE = k32.CreateMutexW(None, False, MUTEX_NAME)
    return C.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def fatal(message: str) -> None:
    """pythonw 没有控制台，出错时弹窗告知。"""
    try:
        C.windll.user32.MessageBoxW(0, message, "蓝牙设备电量", 0x10)
    except Exception:
        pass
    try:
        if sys.stderr is not None:
            sys.stderr.write(message + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 托盘应用
# ---------------------------------------------------------------------------

class TrayApp:
    def __init__(self):
        import pystray
        self.pystray = pystray
        self.cfg = ensure_config()
        self.devices: list[dict] = []
        self.display: dict | None = None
        self.visible: list[dict] = []
        self.updated_at = "--:--"
        self.alerted: set[str] = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.icon = pystray.Icon(
            RUN_VALUE_NAME,
            make_icon(None),
            "蓝牙设备电量 · 正在读取…",
            menu=self.build_menu(),
        )

    # -- 计算 ---------------------------------------------------------------
    def recalc(self):
        self.visible = [d for d in self.devices
                        if d["name"] not in self.cfg["hidden_devices"]]
        primary = str(self.cfg.get("primary_device", "")).strip()
        self.display = None
        if primary:
            for d in self.visible:
                if primary.lower() in d["name"].lower():
                    self.display = d
                    break
        if self.display is None and self.visible:
            self.display = min(self.visible, key=lambda d: d["level"])

    def tooltip(self) -> str:
        level = self.display["level"] if self.display else None
        head = "蓝牙电量 %s · %s" % (
            ("最低 %d%%" % level) if level is not None else "未检测到设备",
            self.updated_at,
        )
        detail = " ".join("%s %d%%" % (d["name"], d["level"]) for d in self.visible)
        text = head + ("\n" + detail if detail else "")
        return text[:120]

    # -- 刷新 ---------------------------------------------------------------
    def refresh(self, user_initiated: bool = False):
        # 手改 config.json 后，下一个轮询周期（或点「立即刷新」）即可生效；
        # 菜单里的开关都会即时写回文件，所以这里重载不会丢状态。
        self.cfg = load_config()
        try:
            devices = read_batteries(self.cfg)
        except Exception as exc:                      # 单次失败不影响托盘存活
            self.icon.title = "蓝牙电量读取失败：%s" % exc
            return
        with self.lock:
            self.devices = devices
            self.updated_at = datetime.now().strftime("%H:%M")
            self.recalc()
            level = self.display["level"] if self.display else None
            low = [d for d in self.visible
                   if d["level"] <= self.cfg["low_battery_threshold"]]
            if self.cfg["low_battery_alert"] and not user_initiated:
                current = {d["name"] for d in low}
                fresh = current - self.alerted
                if fresh:
                    names = ", ".join(
                        "%s %d%%" % (d["name"], d["level"])
                        for d in low if d["name"] in fresh)
                    try:
                        self.icon.notify("电量偏低：" + names, "蓝牙设备电量提醒")
                    except Exception:
                        pass
                self.alerted = current
            self.icon.icon = make_icon(level)
            self.icon.title = self.tooltip()
            self.icon.menu = self.build_menu()

    # -- 菜单 ---------------------------------------------------------------
    def build_menu(self):
        pystray = self.pystray
        M = pystray.MenuItem
        SEP = pystray.Menu.SEPARATOR
        noop = lambda icon, item: None

        items = [M("蓝牙设备电量 · %s 更新" % self.updated_at, noop, enabled=False), SEP]
        if self.visible:
            for d in self.visible:
                items.append(M("%s   %d%%" % (self._short(d["name"]), d["level"]),
                              noop, enabled=False))
        else:
            items.append(M("（未检测到上报电量的蓝牙设备）", noop, enabled=False))
        items.append(SEP)

        # 图标显示哪台设备
        primary_items = [M("自动（显示最低电量）",
                           self._make_primary_action(""),
                           checked=lambda i, cur=self.cfg: not cur.get("primary_device"))]
        for d in self.devices:
            name = d["name"]
            primary_items.append(M("%s (%d%%)" % (self._short(name), d["level"]),
                                   self._make_primary_action(name),
                                   checked=lambda i, n=name, cur=self.cfg:
                                   cur.get("primary_device") == n))
        items.append(M("图标显示", pystray.Menu(*primary_items)))

        # 断开的设备可能仍留着上一次的电量缓存，这里可以按需屏蔽
        if self.devices:
            manage_items = [M(self._short(d["name"]), self._make_hidden_action(d["name"]),
                              checked=lambda i, n=d["name"], cur=self.cfg:
                              n not in cur["hidden_devices"])
                            for d in self.devices]
            items.append(M("显示的设备", pystray.Menu(*manage_items)))

        items.append(M("立即刷新", self._on_refresh))
        items.append(M("开机自动启动", self._on_autostart,
                       checked=lambda i: autostart_enabled()))
        items.append(M("低电量提醒（≤%d%%）" % self.cfg["low_battery_threshold"],
                       self._on_alert_toggle,
                       checked=lambda i, cur=self.cfg: bool(cur["low_battery_alert"])))
        items.append(M("打开配置文件", self._on_open_config))
        items.append(SEP)
        items.append(M("退出", self._on_quit))
        return pystray.Menu(*items)

    @staticmethod
    def _short(name: str) -> str:
        return name if len(name) <= 28 else name[:26] + "…"

    def _make_primary_action(self, name: str):
        def action(icon, item):
            self.cfg["primary_device"] = name
            save_config(self.cfg)
            self.recalc()
            level = self.display["level"] if self.display else None
            self.icon.icon = make_icon(level)
            self.icon.title = self.tooltip()
            self.icon.menu = self.build_menu()
        return action

    def _make_hidden_action(self, name: str):
        def action(icon, item):
            hidden = set(self.cfg["hidden_devices"])
            if name in hidden:
                hidden.discard(name)
            else:
                hidden.add(name)
            self.cfg["hidden_devices"] = sorted(hidden)
            save_config(self.cfg)
            self.recalc()
            level = self.display["level"] if self.display else None
            self.icon.icon = make_icon(level)
            self.icon.title = self.tooltip()
            self.icon.menu = self.build_menu()
        return action

    def _on_refresh(self, icon, item):
        threading.Thread(target=self.refresh, kwargs={"user_initiated": True},
                         daemon=True).start()

    def _on_autostart(self, icon, item):
        set_autostart(not autostart_enabled())
        self.icon.menu = self.build_menu()

    def _on_alert_toggle(self, icon, item):
        self.cfg["low_battery_alert"] = not self.cfg["low_battery_alert"]
        save_config(self.cfg)
        self.icon.menu = self.build_menu()

    def _on_open_config(self, icon, item):
        try:
            os.startfile(CONFIG_PATH)
        except OSError:
            pass

    def _on_quit(self, icon, item):
        self.stop_event.set()
        icon.stop()

    # -- 生命周期 -----------------------------------------------------------
    def run(self):
        def poller():
            self.refresh()
            while not self.stop_event.wait(self.cfg["refresh_seconds"]):
                self.refresh()

        threading.Thread(target=poller, daemon=True).start()
        self.icon.run()


# ---------------------------------------------------------------------------
# 自检 / 入口
# ---------------------------------------------------------------------------

def selftest() -> int:
    cfg = ensure_config()
    print("配置文件:", CONFIG_PATH)
    try:
        devices = read_batteries(cfg)
    except Exception as exc:
        print("读取失败:", repr(exc))
        return 1
    print("读到 %d 个上报电量的蓝牙设备：" % len(devices))
    for d in devices:
        print("  %-38s %3d%%" % (d["name"], d["level"]))
    try:
        img = make_icon(devices[0]["level"] if devices else None)
        print("图标:", img.size, img.mode, "左上角像素:", img.getpixel((0, 0)))
        if "--save-icon" in sys.argv:
            path = os.path.join(BASE, "icon_preview.png")
            img.save(path)
            print("图标预览:", path)
    except Exception as exc:
        print("图标生成失败:", repr(exc))
        return 1
    print("自启状态:", autostart_enabled())
    print("自检通过。")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        fatal("缺少依赖。请在工具目录执行：\npython -m pip install --target vendor pystray pillow")
        return 1
    if not acquire_singleton():
        return 0
    try:
        app = TrayApp()
        if "--smoke" in sys.argv:
            # 真实启动托盘并在 3 秒后优雅退出，用于排查「起不来」的问题
            threading.Timer(3.0, lambda: (app.stop_event.set(), app.icon.stop())).start()
        app.run()
    except Exception as exc:
        fatal("托盘启动失败：%r" % (exc,))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
