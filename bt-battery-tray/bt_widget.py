#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
蓝牙设备电量 · 右下角桌面小组件 (Windows 10/11)

固定在屏幕工作区右下角（任务栏正上方）的置顶圆角卡片，每个设备一行：
    [线框简笔画设备图标]  设备名(截断)          85%
                         └──── 电量条 ────┘

纯 Win32 分层窗口 (UpdateLayeredWindow) + Pillow 绘制，不依赖 tkinter / Qt。
数据与配置复用 bt_battery.py（SetupAPI PnP 电量属性）。

入口：start.vbs / launch.bat     调试：python bt_widget.py --selftest --save-preview
左键拖动可改变位置（会记住），双击刷新，右键弹出菜单。
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import json
import os
import sys
import time
from datetime import datetime, timezone

if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(BASE, "vendor")
if os.path.isdir(VENDOR):
    sys.path.insert(0, VENDOR)

from PIL import Image, ImageChops, ImageDraw, ImageFont

import bt_battery as core

BASE = core.BASE          # 与数据层共用同一个「exe 所在目录」判定

# 低电量报警记录：设备名 -> 上次报警的 Unix 时间戳（存盘，跨重启有效）
ALERT_STATE_PATH = os.path.join(BASE, "alert_state.json")

# ---------------------------------------------------------------------------
# 外观
# ---------------------------------------------------------------------------
CARD = (250, 251, 253, 255)
BORDER = (217, 223, 232, 255)
TEXT = (30, 40, 54, 255)
SUBTEXT = (138, 147, 163, 255)
LINE = (233, 237, 243, 255)
ICON_INK = (90, 102, 120, 255)

CARD_W = 300
HEADER_H = 38
ROW_H = 34
PAD = 14

WIDGET_DEFAULTS = {
    "_widget_help": "display_mode: taskbar_child=任务栏真子窗口(推荐) / taskbar=同色浮窗横条 / card=悬浮卡片；hide_disconnected 默认隐藏未连接及久未连接的设备；tb_max_items 控制任务栏最多画几台，超出的显示在「+N」菜单里（点菜单项可置顶）；device_order 是拖动排序后的设备顺序，留空=在线优先再按电量；tb_show_name=false 时任务栏只画图标+电量；把 PNG/ICO/WebP 放进 exe 同目录的 icons\\ 子目录（按类型或设备名命名）即可替换图标；device_icons 可手动指定某设备图标，如 {\"设备名\": \"mouse\"}，可选 headset/gamepad/speaker/mouse/keyboard/bluetooth；widget_pos 为 null 时自动贴右下角。",
    "display_mode": "taskbar_child",  # "taskbar_child" | "taskbar" | "card"
    "hide_disconnected": True,
    "tb_show_name": False,            # 任务栏里是否显示设备名（默认只留图标+电量）
    "device_order": [],               # 拖动排序后的设备名顺序；空=在线优先+电量
    "hover_tips": True,               # 悬停设备时显示完整名称/状态/电量
    "icon_style": "auto",             # 自定义图标：auto=彩色原样/单色染色，color=一律原色，mono=一律染色
    "device_icons": {},               # 设备名 -> 图标类型，手动覆盖自动识别
    "tb_name_chars": 9,
    "tb_max_items": 3,
    "taskbar_reserve_right": 260,     # 任务栏右侧留给系统托盘/时钟的像素
    "taskbar_child_right_gap": 260,  # 真子窗口的兜底避让距离（动态测量失败时用）
    "taskbar_overlay": True,          # 仅 taskbar 浮窗模式：True=覆盖任务栏，False=贴上边缘
    # --- 与天气小组件的相处方式 ---
    # 天气/资讯按钮是 Explorer 的 XAML 内容，没有独立 HWND，拿不到它的矩形。
    # 启动时会在本窗口出现之前扫描任务栏像素，量出它最左边界，再预留文字
    # 变长 / 悬停展开的余量，避免两者叠在一起。
    "weather_growth_allowance": 56,   # 天气文字变长、图标动画的预留宽度
    "weather_safe_margin": 24,        # 与天气组件之间至少保留的空隙
    "weather_reserve_max": 380,       # 动态避让上限，防止把组件挤到任务栏中间
    "smooth_transition": True,        # 设备上下线时淡入淡出 + 轻微滑动
    "transition_ms": 200,             # 过渡时长（毫秒）
    "hover_fade": True,               # 鼠标移到天气组件上时本组件淡出，避免动画重叠
    "layout_watch_ms": 100,           # 托盘宽度变化 / 悬停监视周期
    "name_max_chars": 14,
    "max_rows": 6,
    "widget_pos": None,
    "always_on_top": True,
    "stale_days": 2,
    "hide_stale": False,
    "alert_threshold": 10,            # 电量 ≤ 这个值时进入报警判定
    "alert_duration_seconds": 8,      # 气泡停留秒数
    "alert_cooldown_hours": 24,       # 同一台设备两次报警的最小间隔（1~720 小时）
    "quick_check_seconds": 2,          # 轻量连接探测间隔；变化时立即刷新
}


def connection_age(dev: dict):
    """距最后一次连接的天数；经典蓝牙没有该属性时返回 None（无法判断）。"""
    lc = dev.get("last_connected")
    if not isinstance(lc, datetime):
        return None
    delta = (datetime.now(timezone.utc).replace(tzinfo=None) - lc).total_seconds()
    if delta < 0:  # 设备/系统时钟偏差，保守视为新鲜
        return 0
    return int(delta // 86400)


def level_color(level: int) -> tuple:
    if level < 20:
        return (196, 59, 50, 255)
    if level < 50:
        return (184, 114, 10, 255)
    return (22, 129, 76, 255)


def load_all() -> dict:
    cfg = dict(core.DEFAULTS)
    cfg.update(WIDGET_DEFAULTS)
    try:
        with open(core.CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    try:
        cfg["name_max_chars"] = max(4, min(40, int(cfg["name_max_chars"])))
        cfg["max_rows"] = max(1, min(12, int(cfg["max_rows"])))
    except (TypeError, ValueError):
        cfg["name_max_chars"], cfg["max_rows"] = 14, 6
    pos = cfg.get("widget_pos")
    if not (isinstance(pos, list) and len(pos) == 2
            and all(isinstance(v, int) for v in pos)):
        cfg["widget_pos"] = None
    return cfg


def save_all(cfg: dict) -> None:
    try:
        with open(core.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---- 报警冷却记录 ----------------------------------------------------------
# 只在内存里记「已报过」是不够的：程序重启、设备休眠后重连、电量回涨再跌破
# 都会把集合清空。把每台设备上次报警的时间戳写进磁盘，才能真正确保
# 「一台设备在一个较长时间段内只报警一次」。
def alert_cooldown_seconds(cfg: dict) -> float:
    try:
        hours = float(cfg.get("alert_cooldown_hours", 24))
    except (TypeError, ValueError):
        hours = 24
    return max(1.0, min(720.0, hours)) * 3600.0


def load_alert_state() -> dict[str, float]:
    try:
        with open(ALERT_STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return {str(k): float(v) for k, v in raw.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
                and 0 < v < 1e12}
    except (OSError, ValueError, TypeError):
        return {}


def save_alert_state(state: dict[str, float]) -> None:
    """原子写，避免写一半损坏；失败时静默降级为「本轮内存有效」。"""
    tmp = ALERT_STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ALERT_STATE_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def font(size: int, bold: bool = False):
    windir = os.environ.get("WINDIR", r"C:\Windows")
    names = (("msyhbd.ttc", "seguisb.ttf", "arialbd.ttf") if bold
             else ("msyh.ttc", "segoeui.ttf", "arial.ttf"))
    for name in names:
        path = os.path.join(windir, "Fonts", name)
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


# ---------------------------------------------------------------------------
# 设备类型 → 线框简笔画图标
# ---------------------------------------------------------------------------

def device_kind(dev) -> str:
    """优先用数据层据 PnP 真实类别判定的 kind，退化时才按设备名猜。"""
    if isinstance(dev, dict):
        kind = dev.get("kind")
        if kind in core.VALID_KINDS:
            return kind
        name = dev.get("name", "")
    else:
        name = str(dev)
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
    return "bluetooth"


def line_icon(kind: str, size: int = 22) -> Image.Image:
    """单色线框设备图标，透明底。

    固定 22 单位设计坐标系 + 8 倍超采样 + LANCZOS 缩小：画布不能再按 size
    缩放，否则 size=16/20 时右侧与底部会被裁掉（图标看起来缺一块）。
    """
    s = 8
    S = 22 * s
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(1, int(1.7 * s))

    def pt(*v):
        return tuple(int(x * s) for x in v)

    def stroke(points, width=None):
        """带圆头端点的折线，避免细线稿在缩小后显得毛糙。"""
        ww = width or w
        pts = [pt(*p) for p in points]
        d.line(pts, fill=ICON_INK, width=ww, joint="curve")
        r = ww / 2
        for x, y in (pts[0], pts[-1]):
            d.ellipse((x - r, y - r, x + r, y + r), fill=ICON_INK)

    if kind == "headset":
        d.arc(pt(3, 2, 19, 20), start=180, end=360, fill=ICON_INK, width=w)
        stroke([(3, 11), (3, 14)])
        stroke([(19, 11), (19, 14)])
        d.rounded_rectangle(pt(1, 12, 6, 20), radius=2 * s,
                            outline=ICON_INK, width=w)
        d.rounded_rectangle(pt(16, 12, 21, 20), radius=2 * s,
                            outline=ICON_INK, width=w)
    elif kind == "gamepad":
        d.rounded_rectangle(pt(1, 7, 21, 18), radius=6 * s,
                            outline=ICON_INK, width=w)
        stroke([(6, 12), (10, 12)])
        stroke([(8, 10), (8, 14)])
        d.ellipse(pt(14, 10, 16, 12), fill=ICON_INK)
        d.ellipse(pt(17, 12, 19, 14), fill=ICON_INK)
    elif kind == "speaker":
        d.rounded_rectangle(pt(5, 1, 17, 21), radius=2 * s,
                            outline=ICON_INK, width=w)
        d.ellipse(pt(8, 12, 14, 18), outline=ICON_INK, width=w)
        d.ellipse(pt(9, 4, 13, 8), fill=ICON_INK)
    elif kind == "mouse":
        # 上窄下宽的圆润剪影 + 左右键中缝 + 按键分界 + 滚轮
        stroke([(8, 2), (14, 2), (16, 5), (17, 9), (17, 14),
                (16, 19), (6, 19), (5, 14), (5, 9), (6, 5), (8, 2)])
        stroke([(11, 2), (11, 10)])
        stroke([(6, 10), (16, 10)])
        d.rounded_rectangle(pt(10, 4, 12, 7), radius=s, fill=ICON_INK)
    elif kind == "keyboard":
        d.rounded_rectangle(pt(1, 6, 21, 17), radius=2 * s,
                            outline=ICON_INK, width=w)
        for row in (9, 12):
            for col in (4, 8, 12, 16):
                d.rounded_rectangle(pt(col, row, col + 2, row + 2),
                                    radius=s // 2, fill=ICON_INK)
        stroke([(8, 15), (14, 15)])
    else:  # 蓝牙通用符文
        stroke([(11, 2), (11, 20)])
        stroke([(11, 2), (17, 7), (11, 12), (17, 17), (11, 20)])
        stroke([(5, 7), (17, 17)])
        stroke([(5, 17), (17, 7)])

    return img.resize((size, size), Image.Resampling.LANCZOS)


# ---------------------------------------------------------------------------
# 任务栏条模式：与任务栏同色同高、无边框的紧凑横条
# ---------------------------------------------------------------------------
TB_FALLBACK_BG = (28, 32, 40, 255)
TB_DARK_TEXT = (236, 239, 245, 255)
TB_LIGHT_TEXT = (30, 40, 54, 255)
TB_DIVIDER_DARK = (255, 255, 255, 45)
TB_DIVIDER_LIGHT = (0, 0, 0, 35)


def level_color_on(level: int, light_bg: bool) -> tuple:
    if light_bg:
        if level < 20:
            return (196, 59, 50, 255)
        if level < 50:
            return (184, 114, 10, 255)
        return (22, 129, 76, 255)
    if level < 20:
        return (255, 118, 108, 255)
    if level < 50:
        return (255, 196, 92, 255)
    return (92, 224, 150, 255)


def tint_icon(icon: Image.Image, color: tuple, alpha_scale: float = 1.0) -> Image.Image:
    out = Image.new("RGBA", icon.size, color)
    a = icon.getchannel("A")
    if alpha_scale < 1.0:
        a = a.point(lambda v: int(v * alpha_scale))
    out.putalpha(a)
    return out


# ---------------------------------------------------------------------------
# 自定义图标：用户把图片放进 BASE/icons/ 即优先使用，找不到才回退自绘线稿
# ---------------------------------------------------------------------------
ICON_DIR = os.path.join(BASE, "icons")
ICON_EXTS = (".png", ".ico", ".webp")
_user_icon_cache: dict = {}


def _safe_filename(name: str) -> str:
    import re
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()


def _find_user_icon(kind: str, name: str | None = None):
    """先按设备名找，再按设备类型找；都没有返回 None。"""
    if not os.path.isdir(ICON_DIR):
        return None
    bases = []
    if name:
        bases.append(_safe_filename(name))
    bases.append(kind)
    for base in bases:
        for ext in ICON_EXTS:
            path = os.path.join(ICON_DIR, base + ext)
            if os.path.isfile(path):
                return path
    return None


def _is_colorful(img: Image.Image) -> bool:
    """判断图片是否带彩色：决定原样显示还是染成任务栏前景色。"""
    small = img.resize((32, 32), Image.Resampling.NEAREST)
    colored = total = 0
    for r, g, b, a in small.getdata():
        if a < 32:
            continue
        total += 1
        if max(r, g, b) - min(r, g, b) > 24:
            colored += 1
    return total > 0 and colored / total > 0.06


def user_icon(kind: str, size: int, name: str | None = None):
    """返回 (图片|None, 是否彩色)，按文件 mtime 缓存，换图后刷新即生效。"""
    path = _find_user_icon(kind, name)
    if not path:
        return None, False
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
        key = (path, size)
        hit = _user_icon_cache.get(key)
        if hit and hit[0] == stamp:
            return hit[1], hit[2]
        im = Image.open(path).convert("RGBA")
        # 非正方形时补透明边到正方形，避免图标被拉伸变形
        w, h = im.size
        if w != h:
            side = max(w, h)
            canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            canvas.paste(im, ((side - w) // 2, (side - h) // 2))
            im = canvas
        colorful = _is_colorful(im)
        im = im.resize((size, size), Image.Resampling.LANCZOS)
        _user_icon_cache[key] = (stamp, im, colorful)
        return im, colorful
    except (OSError, ValueError):
        return None, False


def device_icon(kind: str, size: int, color: tuple, alpha_scale: float = 1.0,
                name: str | None = None, style: str = "auto") -> Image.Image:
    """统一图标入口。style: auto=彩色原样/单色染色，color=一律原色，mono=一律染色。"""
    img, colorful = user_icon(kind, size, name)
    if img is not None:
        if style == "color" or (style == "auto" and colorful):
            out = img.copy()
            if alpha_scale < 1.0:
                out.putalpha(out.getchannel("A").point(lambda a: int(a * alpha_scale)))
            return out
        return tint_icon(img, color, alpha_scale)
    return tint_icon(line_icon(kind, size), color, alpha_scale)


def taskbar_rect():
    """Shell_TrayWnd 的屏幕矩形；取不到返回 None。"""
    try:
        user32.FindWindowW.argtypes = [W.LPCWSTR, W.LPCWSTR]
        user32.FindWindowW.restype = C.c_void_p
        h = user32.FindWindowW("Shell_TrayWnd", None)
        r = RECT()
        if h and user32.GetWindowRect(h, C.byref(r)):
            return r
    except Exception:
        pass
    return None


def sample_taskbar_bg(r) -> tuple:
    """用 GDI GetPixel 采样任务栏中段的空白像素，避免截全屏或额外依赖。"""
    try:
        gdi32.GetPixel.argtypes = [C.c_void_p, C.c_int, C.c_int]
        gdi32.GetPixel.restype = W.DWORD
        hdc = user32.GetDC(None)
        h = r.bottom - r.top
        y = r.top + max(1, h // 2)
        colors = []
        for frac in (0.10, 0.14, 0.18, 0.22, 0.26, 0.30, 0.34, 0.38):
            x = r.left + int((r.right - r.left) * frac)
            c = gdi32.GetPixel(hdc, x, y)
            colors.append((c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF))
        user32.ReleaseDC(None, hdc)
        neutral = [c for c in colors if max(c) - min(c) < 45]
        if neutral:
            from collections import Counter
            c = Counter(neutral).most_common(1)[0][0]
            return (c[0], c[1], c[2], 255)
    except Exception:
        pass
    return TB_FALLBACK_BG


def taskbar_position(w: int, h: int, r, cfg: dict, sw: int, sh: int):
    reserve = max(120, int(cfg.get("taskbar_reserve_right", 260)))
    overlay = bool(cfg.get("taskbar_overlay", True))
    if r and r.top > sh * 0.6:                    # 任务栏在底部
        x = r.right - reserve - w - 6
        y = (r.top + max(0, (r.bottom - r.top - h) // 2)) if overlay else (r.top - h)
        return max(r.left + 4, x), max(0, y)
    if r and r.bottom < sh * 0.4:                 # 任务栏在顶部
        x = r.right - reserve - w - 6
        return max(r.left + 4, x), max(0, r.bottom + 4)
    return sw - w - 16, max(0, sh - 48 - h - 8)


def sort_devices(devices: list[dict], cfg: dict) -> list[dict]:
    """在线优先 + 拖动顺序。

    - device_order 为空：在线在前、电量从低到高、同名按名称，保证最需要
      关注的设备排在前面；
    - device_order 非空（用户拖过一次）：完全按记录的顺序，未记录的新设备
      追加在末尾（仍是在线优先、电量从低到高）。
    """
    order = cfg.get("device_order", [])
    if not isinstance(order, list):
        order = []
    rank = {str(n): i for i, n in enumerate(order)}

    def key(d):
        idx = rank.get(d["name"])
        if idx is not None:
            return (0, idx, 0, "")
        return (1, 1 if d.get("offline") else 0,
                int(d.get("level", 0)), d["name"])

    return sorted(devices, key=key)


def render_taskbar(cfg: dict, devices: list[dict], bg: tuple,
                   height: int, transparent_bg: bool = False,
                   hover_index: int | None = None,
                   hit_out: dict | None = None) -> Image.Image:
    light_bg = sum(bg[:3]) / 3 > 140
    fg = TB_LIGHT_TEXT if light_bg else TB_DARK_TEXT
    muted = (120, 130, 145, 255) if light_bg else (146, 155, 170, 255)
    divider = TB_DIVIDER_LIGHT if light_bg else TB_DIVIDER_DARK

    # 任务栏里默认只画「设备简笔画 + 电量」，不画设备名（名字在悬停提示里看）
    show_name = bool(cfg.get("tb_show_name", False))
    style = str(cfg.get("icon_style", "auto"))
    limit = min(int(cfg.get("max_rows", 6)), int(cfg.get("tb_max_items", 3)))
    limit = max(1, limit)
    shown = devices[:limit]
    overflow_n = max(0, len(devices) - len(shown))
    f_name, f_lvl, f_small = font(11), font(11, True), font(10)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    def tw(text, f):
        b = probe.textbbox((0, 0), text, font=f)
        return b[2] - b[0]

    ICON, GAP, PAD, TAIL, MORE_W = 20, 5, 12, 14, 34
    items = []
    for dev in shown:
        name = truncate(dev["name"], int(cfg.get("tb_name_chars", 9))) if show_name else ""
        pct = "%d%%" % dev["level"]
        nw = tw(name, f_name) if show_name else 0
        pw = tw(pct, f_lvl)
        text_gap = (nw + 5) if show_name else 3
        items.append((dev, name, pct, text_gap,
                      ICON + GAP + text_gap + pw + TAIL))
    left = PAD + ICON + 10
    total = max(96, left + sum(i[4] for i in items)
                + (MORE_W if overflow_n else 0) + 6)
    if not items:
        total = max(total, left + tw("无已连接蓝牙设备", f_small) + 6)

    # 真子窗口：背景 alpha=1（几乎全透明但仍能命中鼠标），让任务栏自己的
    # 亚克力/渐变底色透出来，视觉上彻底合并；浮窗条则需要不透明底色。
    bg_alpha = 1 if transparent_bg else 255
    img = Image.new("RGBA", (total, height), (bg[0], bg[1], bg[2], bg_alpha))
    d = ImageDraw.Draw(img)
    img.alpha_composite(device_icon("bluetooth", ICON, fg, style=style),
                        (PAD, (height - ICON) // 2))
    x = left
    d.line([(x - 6, 9), (x - 6, height - 9)], fill=divider, width=1)

    if not items:
        d.text((x, (height - 12) // 2 - 1), "无已连接蓝牙设备",
               font=f_small, fill=muted)
    hit_items = []
    for idx, (dev, name, pct, text_gap, item_w) in enumerate(items):
        if hover_index == idx:
            d.rounded_rectangle([x - 4, 5, x + item_w - 8, height - 5],
                                radius=6, fill=(fg[0], fg[1], fg[2], 42))
        offline = dev.get("offline") is True
        icon = device_icon(device_kind(dev), ICON,
                           muted if offline else fg,
                           0.5 if offline else 1.0,
                           name=dev["name"], style=style)
        img.alpha_composite(icon, (x, (height - ICON) // 2))
        tx = x + ICON + GAP
        if show_name:
            d.text((tx, (height - 12) // 2 - 1), name, font=f_name,
                   fill=muted if offline else fg)
        d.text((tx + text_gap, (height - 12) // 2 - 1), pct, font=f_lvl,
               fill=muted if offline else level_color_on(dev["level"], light_bg))
        hit_items.append((x - 4, x + item_w - 8))
        x += item_w
        if idx < len(items) - 1:
            d.line([(x - 8, 10), (x - 8, height - 10)], fill=divider, width=1)

    more_hit = None
    if overflow_n:
        mx = x - 6
        d.rounded_rectangle([mx, 7, mx + MORE_W - 6, height - 7], radius=6,
                            fill=(fg[0], fg[1], fg[2], 30), outline=divider,
                            width=1)
        label = "+%d" % overflow_n
        lw = tw(label, f_small)
        d.text((mx + (MORE_W - 6 - lw) // 2, (height - 12) // 2 - 1),
               label, font=f_small, fill=muted)
        more_hit = (mx, mx + MORE_W - 6)

    if hit_out is not None:
        hit_out.clear()
        hit_out["items"] = hit_items
        hit_out["more"] = more_hit
        hit_out["shown"] = len(shown)
    return img


# ---------------------------------------------------------------------------
# 卡片渲染（返回带 alpha 的 RGBA 图）
# ---------------------------------------------------------------------------

def render_card(cfg: dict, devices: list[dict], stamp: str) -> Image.Image:
    max_rows = int(cfg["max_rows"])
    shown = devices[:max_rows]
    overflow = max(0, len(devices) - len(shown))
    height = (HEADER_H + max(len(shown), 1) * ROW_H + 10
              + (18 if overflow else 0))
    if not devices:
        height = HEADER_H + 40

    card = Image.new("RGBA", (CARD_W, height), CARD)
    d = ImageDraw.Draw(card)
    d.rounded_rectangle([0, 0, CARD_W - 1, height - 1], radius=12,
                        outline=BORDER, width=1)

    f_title, f_small = font(12, True), font(10)
    f_name, f_level = font(12), font(12, True)

    d.text((PAD, 11), "蓝牙设备电量", font=f_title, fill=TEXT)
    cached = sum(1 for dev in devices if dev.get("_stale"))
    label = "更新 " + stamp + ((" · %d 台离线" % cached) if cached else "")
    tw = d.textbbox((0, 0), label, font=f_small)[2]
    d.text((CARD_W - PAD - tw, 13), label, font=f_small, fill=SUBTEXT)
    d.line([(PAD, HEADER_H - 6), (CARD_W - PAD, HEADER_H - 6)], fill=LINE, width=1)

    if not devices:
        d.text((PAD, HEADER_H + 8), "未检测到上报电量的蓝牙设备",
               font=f_name, fill=SUBTEXT)

    icons: dict[tuple, Image.Image] = {}
    style = str(cfg.get("icon_style", "auto"))
    for i, dev in enumerate(shown):
        top = HEADER_H + i * ROW_H
        stale = bool(dev.get("_stale"))
        kind = device_kind(dev)
        key = (kind, dev["name"])
        if key not in icons:
            icons[key] = device_icon(kind, 22, ICON_INK, 1.0,
                                     name=dev["name"], style=style)
        icon = icons[key]
        if stale:
            icon = icon.copy()
            icon.putalpha(icon.getchannel("A").point(lambda a: int(a * 0.4)))
        card.alpha_composite(icon, (PAD, top + 5))

        faded = (166, 174, 185, 255)
        name_fg = faded if stale else TEXT
        level_fg = faded if stale else level_color(dev["level"])
        bar_fg = (206, 212, 220, 255) if stale else level_color(dev["level"])

        limit = int(cfg["name_max_chars"])
        name = truncate(dev["name"], max(4, limit - 6) if stale else limit)
        d.text((PAD + 30, top + 5), name, font=f_name, fill=name_fg)
        if stale:
            nx = PAD + 30 + d.textbbox((0, 0), name, font=f_name)[2]
            note = "·" + (dev.get("_offline_reason") or "缓存")
            d.text((nx + 2, top + 6), note, font=f_small, fill=SUBTEXT)

        level = dev["level"]
        pct = "%d%%" % level
        pw = d.textbbox((0, 0), pct, font=f_level)[2]
        d.text((CARD_W - PAD - pw, top + 5), pct, font=f_level, fill=level_fg)

        bar_x0, bar_x1, bar_y = PAD + 30, CARD_W - PAD, top + 26
        d.rounded_rectangle([bar_x0, bar_y, bar_x1, bar_y + 3], radius=2, fill=LINE)
        filled = bar_x0 + int((bar_x1 - bar_x0) * max(0, min(100, level)) / 100)
        if filled > bar_x0:
            d.rounded_rectangle([bar_x0, bar_y, filled, bar_y + 3], radius=2, fill=bar_fg)

    if overflow:
        d.text((PAD, HEADER_H + len(shown) * ROW_H + 2),
               "…另有 %d 台设备" % overflow, font=f_small, fill=SUBTEXT)

    # 圆角遮罩：4 倍超采样得到平滑 alpha 边缘
    S = 4
    big = Image.new("L", (CARD_W * S, height * S), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, CARD_W * S - 1, height * S - 1], radius=12 * S, fill=255)
    mask = big.resize((CARD_W, height), Image.Resampling.LANCZOS)
    card.putalpha(mask)
    return card


def premultiply_bgra(img: Image.Image) -> bytes:
    r, g, b, a = img.split()
    premul = Image.merge("RGBA", (
        ImageChops.multiply(r, a),
        ImageChops.multiply(g, a),
        ImageChops.multiply(b, a),
        a,
    ))
    return premul.tobytes("raw", "BGRA")


def render_bubble(devices: list[dict], threshold: int) -> Image.Image:
    """低电量报警气泡：深色圆角卡片 + 底部指向任务栏的小三角。"""
    rows = max(1, min(3, len(devices)))
    W = 306
    H = 90 + (rows - 1) * 22
    TAIL = 11
    img = Image.new("RGBA", (W, H + TAIL), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    dark = (38, 42, 52, 255)          # 不透明，避免壁纸花纹透过气泡干扰文字
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=14,
                        fill=dark, outline=(100, 108, 124, 255), width=1)
    cx = W // 2
    d.polygon([(cx - 10, H - 3), (cx + 10, H - 3), (cx, H + TAIL - 1)],
              fill=dark)

    # 红色警告圆点 + 感叹号
    d.ellipse((14, 17, 42, 45), fill=(235, 76, 68, 255))
    f_bang = font(19, True)
    bb = d.textbbox((0, 0), "!", font=f_bang)
    d.text((28 - (bb[2] - bb[0]) / 2, 27 - (bb[3] - bb[1]) / 2), "!",
           font=f_bang, fill=(255, 255, 255, 255))

    f_title, f_sub, f_row, f_pct = font(13, True), font(11), font(11), font(11, True)
    d.text((52, 15), "电量过低", font=f_title, fill=(240, 243, 248, 255))
    d.text((52, 36), "≤ %d%% · 请及时充电" % threshold, font=f_sub,
           fill=(160, 170, 186, 255))

    # 右上角关闭叉
    d.line([(W - 23, 15), (W - 15, 23)], fill=(156, 166, 182, 255), width=2)
    d.line([(W - 15, 15), (W - 23, 23)], fill=(156, 166, 182, 255), width=2)

    d.line([(14, 58), (W - 14, 58)], fill=(74, 82, 96, 255), width=1)
    y = 64
    for dev in devices[:3]:
        name = truncate(str(dev.get("name", "蓝牙设备")), 20)
        pct = "%d%%" % int(dev.get("level", 0))
        d.text((16, y), name, font=f_row, fill=(228, 233, 240, 255))
        pw = d.textbbox((0, 0), pct, font=f_pct)[2]
        d.text((W - 16 - pw, y), pct, font=f_pct, fill=(242, 168, 44, 255))
        y += 22
    return img


def render_tip(dev: dict, cfg: dict | None = None) -> Image.Image:
    """设备悬停提示：完整设备名 + 在线状态 + 电量 + 操作说明。"""
    name = str(dev.get("name", "蓝牙设备"))
    offline = bool(dev.get("offline"))
    level = int(dev.get("level", 0))
    reason = str(dev.get("offline_reason") or ("缓存电量" if offline else ""))
    age = dev.get("_age_days")

    f_name = font(13, True)
    f_line = font(11)
    f_pct = font(14, True)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    max_name = probe.textbbox((0, 0), name, font=f_name)[2]
    if not offline:
        status = "已连接 · 实时电量"
    elif reason == "未连接" or not reason:
        status = "未连接 · 仅缓存电量"
    elif "未连接" in reason or "缓存" in reason:
        status = reason
    else:
        status = "未连接 · " + reason
    lines = [
        status + ((" · %d 天前" % age) if isinstance(age, int) and age > 0 else ""),
        "左键拖动排序 · 可调节显示顺序",
    ]
    max_line = max(probe.textbbox((0, 0), t, font=f_line)[2] for t in lines)
    pct_txt = "%d%%" % level
    W = max(240, min(420, max(max_name, max_line) + 92))
    H = 86
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    dark = (38, 42, 52, 255)
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=11, fill=dark,
                        outline=(100, 108, 124, 255), width=1)

    d.text((14, 11), truncate(name, 26), font=f_name,
           fill=(240, 243, 248, 255))
    level_fg = ((242, 168, 44, 255) if level < 20
                else (224, 186, 72, 255) if level < 50
                else (110, 206, 148, 255))
    pw = probe.textbbox((0, 0), pct_txt, font=f_pct)[2]
    d.text((W - 14 - pw, 8), pct_txt, font=f_pct, fill=level_fg)
    d.line([(14, 38), (W - 14, 38)], fill=(74, 82, 96, 255), width=1)
    d.text((14, 44), lines[0], font=f_line, fill=(178, 188, 203, 255))
    d.text((14, 64), lines[1], font=f_line, fill=(138, 150, 168, 255))
    return img




# ---------------------------------------------------------------------------
# Win32 分层窗口
# ---------------------------------------------------------------------------

user32 = C.WinDLL("user32.dll", use_last_error=True)
gdi32 = C.WinDLL("gdi32.dll", use_last_error=True)
kernel32 = C.WinDLL("kernel32.dll", use_last_error=True)

WPARAM_T, LPARAM_T = C.c_size_t, C.c_ssize_t
LRESULT_T = C.c_ssize_t


class POINT(C.Structure):
    _fields_ = [("x", W.LONG), ("y", W.LONG)]


class SIZE(C.Structure):
    _fields_ = [("cx", W.LONG), ("cy", W.LONG)]


class RECT(C.Structure):
    _fields_ = [("left", W.LONG), ("top", W.LONG),
                ("right", W.LONG), ("bottom", W.LONG)]


class BLENDFUNCTION(C.Structure):
    _fields_ = [("BlendOp", C.c_ubyte), ("BlendFlags", C.c_ubyte),
                ("SourceConstantAlpha", C.c_ubyte), ("AlphaFormat", C.c_ubyte)]


class BITMAPINFOHEADER(C.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG),
                ("biPlanes", W.WORD), ("biBitCount", W.WORD),
                ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG),
                ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]


class BITMAPINFO(C.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", W.DWORD)]


class MSG(C.Structure):
    _fields_ = [("hwnd", C.c_void_p), ("message", W.UINT),
                ("wParam", WPARAM_T), ("lParam", LPARAM_T),
                ("time", W.DWORD), ("pt", POINT), ("lPrivate", W.DWORD)]


class TRACKMOUSEEVENT(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("dwFlags", W.DWORD),
                ("hwndTrack", C.c_void_p), ("dwHoverTime", W.DWORD)]


WNDPROC = C.WINFUNCTYPE(LRESULT_T, C.c_void_p, W.UINT, WPARAM_T, LPARAM_T)


class WNDCLASSEXW(C.Structure):
    _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", C.c_int), ("cbWndExtra", C.c_int),
                ("hInstance", C.c_void_p), ("hIcon", C.c_void_p),
                ("hCursor", C.c_void_p), ("hbrBackground", C.c_void_p),
                ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR),
                ("hIconSm", C.c_void_p)]


class DEV_BROADCAST_DEVICEINTERFACE_W(C.Structure):
    _fields_ = [("dbcc_size", W.DWORD), ("dbcc_devicetype", W.DWORD),
                ("dbcc_reserved", W.DWORD), ("dbcc_classguid", core.GUID),
                ("dbcc_name", W.WCHAR * 1)]


# 常用消息 / 样式
WM_DESTROY, WM_CLOSE, WM_TIMER, WM_NULL = 0x0002, 0x0010, 0x0113, 0x0000
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0203, 0x0205
WM_MOUSELEAVE = 0x02A3
TME_LEAVE = 0x00000002
WM_SETCURSOR = 0x0020
WM_DEVICECHANGE = 0x0219          # 蓝牙设备插拔广播
IDC_ARROW = 32512                 # MAKEINTRESOURCE(IDC_ARROW)：标准箭头光标
IDC_HAND = 32649                  # 悬停设备项时显示「可点击」手型
WM_APP_REFRESH = 0x8000 + 1
WM_APP_ALERT_DEMO = 0x8000 + 2       # 供菜单「测试气泡」与自动化验证触发
WM_APP_DEVICE_EVENT = 0x8000 + 3     # 设备事件已到达，请求合并刷新
ALERT_TIMER_ID = 6                   # 气泡自动关闭定时器
QUICK_TIMER_ID = 7                   # 轻量连接探测定时器
EVENT_TIMER_ID = 8                   # 设备事件合并刷新定时器
ANIM_TIMER_ID = 9                    # 设备上下线过渡动画帧
LAYOUT_TIMER_ID = 10                 # 托盘布局 / 天气悬停监视
ANIM_FRAME_MS = 20
DEVICE_WATCHER_CLASS = "BtBatteryDeviceWatcherClass"
DBT_DEVTYP_DEVICEINTERFACE = 5
CS_DBLCLKS = 0x0008
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_CLIPSIBLINGS = 0x04000000
WS_VISIBLE = 0x10000000
WS_EX_LAYERED, WS_EX_TOPMOST = 0x00080000, 0x00000008
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000
WS_EX_TRANSPARENT = 0x00000020          # 提示窗鼠标穿透，不抢设备项的悬停/点击
SWP_NOACTIVATE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0010, 0x0001, 0x0040
SWP_NOZORDER = 0x0004
SW_SHOW = 5
_PTR_MAX = (1 << (C.sizeof(C.c_void_p) * 8)) - 1
HWND_TOPMOST = C.c_void_p(_PTR_MAX)
HWND_NOTOPMOST = C.c_void_p(_PTR_MAX - 1)
HWND_TOP = C.c_void_p(0)
ULW_ALPHA = 0x00000002
MF_STRING, MF_CHECKED, MF_SEPARATOR, MF_POPUP = 0x0000, 0x0008, 0x0800, 0x0010
TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x0100, 0x0002
SPI_GETWORKAREA = 0x0030

CLASS_NAME = "BtBatteryWidgetClass"
APP = None
MODE_LABELS = {
    "taskbar_child": "任务栏子窗口",
    "taskbar": "任务栏浮窗条",
    "card": "悬浮卡片",
}
MODE_ORDER = ("taskbar_child", "taskbar", "card")


def blit_layered(hwnd, img: Image.Image) -> None:
    """把 RGBA 图像按逐像素 alpha 贴到分层窗口（主窗口与气泡共用）。"""
    w, h = img.size
    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    user32.ReleaseDC(None, hdc_screen)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = C.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h                   # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0              # BI_RGB
    bits = C.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc_mem, C.byref(bmi), 0,
                                  C.byref(bits), None, 0)
    if not hbmp or not bits.value:
        gdi32.DeleteDC(hdc_mem)
        raise OSError(C.get_last_error(), "CreateDIBSection 失败")
    old = gdi32.SelectObject(hdc_mem, hbmp)
    raw = premultiply_bgra(img)
    C.memmove(bits.value, raw, len(raw))
    size = SIZE(w, h)
    src = POINT(0, 0)
    blend = BLENDFUNCTION(0, 0, 255, 1)          # AC_SRC_ALPHA
    ok = user32.UpdateLayeredWindow(
        hwnd, None, None, C.byref(size), hdc_mem, C.byref(src), 0,
        C.byref(blend), ULW_ALPHA)
    err = C.get_last_error()
    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    if not ok:
        raise OSError(err, "UpdateLayeredWindow 失败")


class LowBatteryBubble:
    """独立 WS_POPUP 分层窗口：低电量气泡，超时或点击后自动关闭。

    不用托盘 balloon（本工具没有托盘图标，且 Win11 会把它折叠成通知中心），
    也不用 WinRT Toast（本环境被安全策略拦截）。真气泡自绘，风格与任务栏
    小组件一致，从组件正上方冒出。
    """

    def __init__(self, app):
        self.app = app
        self.hwnd = None

    def show(self, devices, threshold, cfg):
        if not devices:
            return
        self.close()                              # 不叠加多个气泡
        img = render_bubble(devices, threshold)
        w, h = img.size
        hinst = kernel32.GetModuleHandleW(None)
        ex = (WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
              | WS_EX_TOPMOST)
        self.hwnd = user32.CreateWindowExW(
            ex, CLASS_NAME, "bt-battery-alert", WS_POPUP,
            0, 0, w, h, None, None, hinst, None)
        if not self.hwnd:
            return
        x, y = self._position(w, h)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0,
                            SWP_NOACTIVATE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.ShowWindow(self.hwnd, SW_SHOW)
        try:
            blit_layered(self.hwnd, img)
        except Exception as exc:
            print("bubble blit failed:", repr(exc))
            self.close()
            return
        try:
            dur = max(2, int(cfg.get("alert_duration_seconds", 8))) * 1000
        except (TypeError, ValueError):
            dur = 8000
        user32.SetTimer(self.hwnd, ALERT_TIMER_ID, dur, None)

    def _position(self, w, h):
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        host = find_taskbar_host()
        top = host[1].top if host else sh - 48
        wr = RECT()
        if self.app.hwnd and user32.GetWindowRect(self.app.hwnd, C.byref(wr)):
            cx = (wr.left + wr.right) // 2
        else:
            cx = sw - 260
        return (max(8, min(cx - w // 2, sw - w - 8)),
                max(8, top - h - 8))

    def on_message(self, msg, wparam):
        if msg == WM_DESTROY:
            self.hwnd = None
            return 0
        if msg == WM_SETCURSOR:
            if self.app.hcursor:
                user32.SetCursor(self.app.hcursor)
            return 1
        if msg == WM_TIMER and wparam == ALERT_TIMER_ID:
            self.close()
            return 0
        if msg in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK, WM_RBUTTONUP):
            self.close()
            return 0
        return None

    def close(self):
        if self.hwnd:
            user32.KillTimer(self.hwnd, ALERT_TIMER_ID)
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None


class DeviceTip:
    """设备悬停提示窗：鼠标穿透、不抢焦点，跟随任务栏设备项显示完整信息。"""

    def __init__(self, app):
        self.app = app
        self.hwnd = None
        self.name = None

    def show(self, dev, cfg, widget_rect, hit_x0, hit_x1):
        if not dev:
            return
        name = dev.get("name")
        if self.hwnd and self.name == name:
            return
        self.close()
        img = render_tip(dev, cfg)
        w, h = img.size
        hinst = kernel32.GetModuleHandleW(None)
        ex = (WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
              | WS_EX_TOPMOST | WS_EX_TRANSPARENT)
        self.hwnd = user32.CreateWindowExW(
            ex, CLASS_NAME, "bt-battery-tip", WS_POPUP,
            0, 0, w, h, None, None, hinst, None)
        if not self.hwnd:
            return
        self.name = name
        center = widget_rect.left + (hit_x0 + hit_x1) // 2
        x = max(8, min(center - w // 2, user32.GetSystemMetrics(0) - w - 8))
        y = max(8, widget_rect.top - h - 6)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0,
                            SWP_NOACTIVATE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.ShowWindow(self.hwnd, SW_SHOW)
        try:
            blit_layered(self.hwnd, img)
        except Exception as exc:
            print("tip blit failed:", repr(exc))
            self.close()

    def close(self):
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
            self.name = None

    def on_message(self, msg, wparam):
        if msg == WM_DESTROY:
            self.hwnd = None
            self.name = None
            return 0
        if msg == WM_SETCURSOR:
            if self.app.hcursor:
                user32.SetCursor(self.app.hcursor)
            return 1
        if msg in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK, WM_RBUTTONUP):
            self.close()
            return 0
        return None




def find_taskbar_host():
    """定位 Shell_TrayWnd 与 TrayNotifyWnd，返回 (tray_hwnd, tray_rect, tn_rect)。"""
    try:
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if not tray:
            return None
        tr = RECT()
        if not user32.GetWindowRect(tray, C.byref(tr)):
            return None
        tn_rect = None
        tn = user32.FindWindowExW(tray, 0, "TrayNotifyWnd", None)
        if tn:
            q = RECT()
            if user32.GetWindowRect(tn, C.byref(q)) and q.right > q.left:
                tn_rect = q
        return tray, tr, tn_rect
    except Exception:
        return None


def child_position(w: int, h: int, tray, tray_rect, tn_rect, cfg=None,
                   gap_override=None):
    """算出子窗口在 Shell_TrayWnd 客户区里的 (x, y)；竖直任务栏返回 None。"""
    tb_w = tray_rect.right - tray_rect.left
    tb_h = tray_rect.bottom - tray_rect.top
    if tb_w <= tb_h:                      # 竖直任务栏：交给浮窗回退
        return None
    if gap_override is not None:
        gap = max(0, int(gap_override))
    else:
        try:
            gap = max(0, int((cfg or {}).get("taskbar_child_right_gap", 260)))
        except (TypeError, ValueError):
            gap = 260
    if tn_rect:
        # 天气/资讯组件紧贴托盘区左侧，往左让出实测距离，避免文字叠在一起
        p = POINT(tn_rect.left - gap, tn_rect.top)
        user32.ScreenToClient(tray, C.byref(p))
        anchor = p.x
    else:
        p = POINT(tray_rect.right - 260 - gap, tray_rect.top)
        user32.ScreenToClient(tray, C.byref(p))
        anchor = p.x
    x = anchor - w - 3
    y = max(0, (tb_h - h) // 2)
    return max(0, x), y


def scale_alpha(img: Image.Image, k: float) -> Image.Image:
    """整图按倍率缩放 alpha（淡入淡出用）；alpha=0 的区域鼠标可穿透。"""
    if k >= 0.999:
        return img
    out = img.copy()
    if k <= 0.001:
        out.putalpha(Image.new("L", img.size, 0))
    else:
        out.putalpha(img.getchannel("A").point(lambda v: int(v * k)))
    return out


def measure_weather_reserve(tray_rect, tn_rect, max_scan=420):
    """扫描任务栏像素，量出天气/资讯组件的最左边界。

    Win11 的天气按钮是 Explorer 的 XAML 合成内容，没有独立 HWND，
    FindWindowEx / GetWindowRect 都拿不到它的矩形，只能从屏幕像素反推。
    必须在本组件窗口「尚未创建」或「已隐藏」时调用，否则自己的图标会
    被当成天气组件。返回 (需要让开的像素, 天气左边界, 天气右边界)，
    量不到则返回 None。
    """
    hdc = None
    try:
        from collections import Counter
        gdi32.GetPixel.argtypes = [C.c_void_p, C.c_int, C.c_int]
        gdi32.GetPixel.restype = W.DWORD
        hdc = user32.GetDC(None)
        if not hdc:
            return None
        top, bottom = tray_rect.top, tray_rect.bottom
        mid = (top + bottom) // 2
        rows = [max(top + 5, min(bottom - 5, mid + dy)) for dy in (-11, 0, 11)]

        def rgb(x, y):
            c = gdi32.GetPixel(hdc, x, y)
            return (c & 255, (c >> 8) & 255, (c >> 16) & 255)

        # 托盘左侧通常是一段纯色背景，取多点众数当作参考色
        bg = Counter(rgb(tn_rect.left - d, mid)
                     for d in (18, 26, 34, 42, 50)).most_common(1)[0][0]
        x0 = tn_rect.left - 8
        x1 = max(tray_rect.left + 8, tn_rect.left - max_scan)
        hot = []
        for x in range(x0, x1, -1):
            n = 0
            for y in rows:
                c = rgb(x, y)
                if max(abs(c[i] - bg[i]) for i in range(3)) > 22:
                    n += 1
            hot.append((x, n > 0))
        # 汉字笔画之间有空隙，把间距 <=14px 的内容合并成一块
        clusters, cur, last_hot = [], None, None
        for x, is_hot in hot:
            if not is_hot:
                continue
            if cur is None:
                cur = [x, x]
            elif last_hot is not None and last_hot - x <= 14:
                cur[0] = x
            else:
                clusters.append(tuple(cur))
                cur = [x, x]
            last_hot = x
        if cur:
            clusters.append(tuple(cur))
        # 最靠近托盘、宽度 >=20px 的内容块就是天气组件
        cand = [c for c in clusters
                if c[1] - c[0] >= 20 and c[1] <= tn_rect.left - 16]
        if not cand:
            return None
        left, right = max(cand, key=lambda c: c[1])
        return tn_rect.left - left, left, right
    except Exception:
        return None
    finally:
        if hdc:
            try:
                user32.ReleaseDC(None, hdc)
            except Exception:
                pass


def _declare_api():
    user32.DefWindowProcW.argtypes = [C.c_void_p, W.UINT, WPARAM_T, LPARAM_T]
    user32.DefWindowProcW.restype = LRESULT_T
    user32.RegisterClassExW.argtypes = [C.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = W.ATOM
    user32.CreateWindowExW.argtypes = [
        W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int, C.c_int, C.c_int,
        C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p]
    user32.CreateWindowExW.restype = C.c_void_p
    user32.SetWindowPos.argtypes = [C.c_void_p, C.c_void_p, C.c_int, C.c_int,
                                    C.c_int, C.c_int, W.UINT]
    user32.SetWindowPos.restype = W.BOOL
    user32.ShowWindow.argtypes = [C.c_void_p, C.c_int]
    user32.UpdateLayeredWindow.argtypes = [
        C.c_void_p, C.c_void_p, C.POINTER(POINT), C.POINTER(SIZE),
        C.c_void_p, C.POINTER(POINT), W.DWORD, C.POINTER(BLENDFUNCTION), W.DWORD]
    user32.UpdateLayeredWindow.restype = W.BOOL
    user32.GetMessageW.argtypes = [C.POINTER(MSG), C.c_void_p, W.UINT, W.UINT]
    user32.GetMessageW.restype = C.c_int
    user32.TranslateMessage.argtypes = [C.POINTER(MSG)]
    user32.DispatchMessageW.argtypes = [C.POINTER(MSG)]
    user32.PostQuitMessage.argtypes = [C.c_int]
    user32.DestroyWindow.argtypes = [C.c_void_p]
    user32.SetTimer.argtypes = [C.c_void_p, C.c_size_t, W.UINT, C.c_void_p]
    user32.SetTimer.restype = C.c_size_t
    user32.KillTimer.argtypes = [C.c_void_p, C.c_size_t]
    user32.GetCursorPos.argtypes = [C.POINTER(POINT)]
    user32.SetCapture.argtypes = [C.c_void_p]
    user32.ReleaseCapture.restype = W.BOOL
    user32.SetForegroundWindow.argtypes = [C.c_void_p]
    user32.GetDC.argtypes = [C.c_void_p]
    user32.GetDC.restype = C.c_void_p
    user32.ReleaseDC.argtypes = [C.c_void_p, C.c_void_p]
    user32.GetWindowRect.argtypes = [C.c_void_p, C.POINTER(RECT)]
    user32.GetSystemMetrics.argtypes = [C.c_int]
    user32.SystemParametersInfoW.argtypes = [W.UINT, W.UINT, C.c_void_p, W.UINT]
    user32.CreatePopupMenu.restype = C.c_void_p
    user32.AppendMenuW.argtypes = [C.c_void_p, W.UINT, C.c_size_t, W.LPCWSTR]
    user32.AppendMenuW.restype = W.BOOL
    user32.TrackPopupMenu.argtypes = [C.c_void_p, W.UINT, C.c_int, C.c_int,
                                      C.c_int, C.c_void_p, C.c_void_p]
    user32.TrackPopupMenu.restype = C.c_int
    user32.DestroyMenu.argtypes = [C.c_void_p]
    user32.PostMessageW.argtypes = [C.c_void_p, W.UINT, WPARAM_T, LPARAM_T]
    # --- 任务栏子窗口 / Explorer 重启自愈 ---
    user32.FindWindowW.argtypes = [W.LPCWSTR, W.LPCWSTR]
    user32.FindWindowW.restype = C.c_void_p
    user32.FindWindowExW.argtypes = [C.c_void_p, C.c_void_p, W.LPCWSTR, W.LPCWSTR]
    user32.FindWindowExW.restype = C.c_void_p
    user32.RegisterWindowMessageW.argtypes = [W.LPCWSTR]
    user32.RegisterWindowMessageW.restype = W.UINT
    user32.IsWindow.argtypes = [C.c_void_p]
    user32.IsWindow.restype = W.BOOL
    user32.GetParent.argtypes = [C.c_void_p]
    user32.GetParent.restype = C.c_void_p
    user32.SetParent.argtypes = [C.c_void_p, C.c_void_p]
    user32.SetParent.restype = C.c_void_p
    user32.IsWindowVisible.argtypes = [C.c_void_p]
    user32.IsWindowVisible.restype = W.BOOL
    user32.ScreenToClient.argtypes = [C.c_void_p, C.POINTER(POINT)]
    user32.ScreenToClient.restype = W.BOOL
    user32.GetClassNameW.argtypes = [C.c_void_p, W.LPWSTR, C.c_int]
    user32.GetClassNameW.restype = C.c_int
    gdi32.CreateCompatibleDC.argtypes = [C.c_void_p]
    gdi32.CreateCompatibleDC.restype = C.c_void_p
    gdi32.CreateDIBSection.argtypes = [C.c_void_p, C.POINTER(BITMAPINFO),
                                       W.UINT, C.POINTER(C.c_void_p),
                                       C.c_void_p, W.DWORD]
    gdi32.CreateDIBSection.restype = C.c_void_p
    gdi32.SelectObject.argtypes = [C.c_void_p, C.c_void_p]
    gdi32.SelectObject.restype = C.c_void_p
    gdi32.DeleteObject.argtypes = [C.c_void_p]
    gdi32.DeleteDC.argtypes = [C.c_void_p]
    kernel32.GetModuleHandleW.argtypes = [W.LPCWSTR]
    kernel32.GetModuleHandleW.restype = C.c_void_p
    # 窗口类若把 hCursor 留空，光标会由父窗口 Shell_TrayWnd 决定，
    # 悬停在这个分层子窗口上时可能不是标准箭头。显式加载并自行处理。
    user32.LoadCursorW.argtypes = [C.c_void_p, C.c_void_p]
    user32.LoadCursorW.restype = C.c_void_p
    user32.SetCursor.argtypes = [C.c_void_p]
    user32.SetCursor.restype = C.c_void_p
    user32.TrackMouseEvent.argtypes = [C.POINTER(TRACKMOUSEEVENT)]
    user32.TrackMouseEvent.restype = W.BOOL
    # --- 设备插拔事件（隐藏顶层窗口代收广播） ---
    user32.RegisterDeviceNotificationW.argtypes = [
        C.c_void_p, C.c_void_p, W.DWORD]
    user32.RegisterDeviceNotificationW.restype = C.c_void_p
    user32.UnregisterDeviceNotification.argtypes = [C.c_void_p]
    user32.UnregisterDeviceNotification.restype = W.BOOL


@WNDPROC
def _wnd_proc(hwnd, msg, wparam, lparam):
    app = APP
    if app is not None:
        try:
            result = app.on_message(hwnd, msg, wparam, lparam)
            if result is not None:
                return result
        except Exception as exc:
            print("wnd_proc error:", repr(exc))
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


@WNDPROC
def _device_wnd_proc(hwnd, msg, wparam, lparam):
    """隐藏窗口只做一件事：把设备事件转交给主窗口，不参与主窗口消息分发。"""
    app = APP
    if msg == WM_DEVICECHANGE and app is not None and app.hwnd:
        user32.PostMessageW(app.hwnd, WM_APP_DEVICE_EVENT, 0, 0)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class DeviceWatcher:
    """代收 WM_DEVICECHANGE 的隐藏顶层窗口。

    主窗口在 taskbar_child 模式下是挂到 Shell_TrayWnd 的 WS_CHILD，
    而系统把设备插拔广播只发给顶层窗口 —— 这是「新设备连上要等十几秒」
    的根因。这里建一个 WS_EX_TOOLWINDOW、不显示的顶层窗口代收，再用
    RegisterDeviceNotification(GUID_NULL) 订阅所有设备接口类，覆盖蓝牙 /
    HID / USB 的到达与移除，收到即唤醒主窗口刷新。
    """

    def __init__(self):
        self.hwnd = None
        self.hnotify = None
        self.registered = False
        try:
            self._create()
        except Exception as exc:
            print("DeviceWatcher init failed:", repr(exc))

    def _create(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = C.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = _device_wnd_proc
        wc.hInstance = hinst
        wc.lpszClassName = DEVICE_WATCHER_CLASS
        atom = user32.RegisterClassExW(C.byref(wc))
        if not atom and C.get_last_error() != 1410:  # ERROR_CLASS_ALREADY_EXISTS
            raise OSError(C.get_last_error(), "RegisterClassExW(DeviceWatcher) 失败")
        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, DEVICE_WATCHER_CLASS,
            "bt-battery-device-watcher", 0, 0, 0, 0, 0,
            None, None, hinst, None)
        if not self.hwnd:
            raise OSError(C.get_last_error(), "CreateWindowExW(DeviceWatcher) 失败")
        filt = DEV_BROADCAST_DEVICEINTERFACE_W()
        filt.dbcc_size = C.sizeof(filt)
        filt.dbcc_devicetype = DBT_DEVTYP_DEVICEINTERFACE
        filt.dbcc_classguid = core.GUID()   # GUID_NULL → 所有设备接口类
        self.hnotify = user32.RegisterDeviceNotificationW(
            self.hwnd, C.byref(filt), 0)
        self.registered = bool(self.hnotify)

    def close(self):
        if self.hnotify:
            try:
                user32.UnregisterDeviceNotification(self.hnotify)
            except Exception:
                pass
            self.hnotify = None
        if self.hwnd:
            try:
                user32.DestroyWindow(self.hwnd)
            except Exception:
                pass
            self.hwnd = None


class Widget:
    def __init__(self):
        _declare_api()
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        self.cfg = load_all()
        self.devices: list[dict] = []
        self.stamp = "--:--"
        self.hwnd = None
        self.dragging = False
        self.drag_start = (0, 0)
        self.win_start = (0, 0)
        self.width, self.height = CARD_W, 100
        self._tb_bg = None
        self.smoke = "--smoke" in sys.argv
        # --- 任务栏真子窗口 / Explorer 重启自愈 ---
        self.tray_hwnd = None
        self.child_mode = False
        self._rebuilding = False
        self.wm_taskbar_created = 0
        self.hcursor = user32.LoadCursorW(None, C.c_void_p(IDC_ARROW))
        self.hhand = user32.LoadCursorW(None, C.c_void_p(IDC_HAND))
        self.bubble = LowBatteryBubble(self)
        self.tip = DeviceTip(self)
        self._hit: dict = {"items": [], "more": None, "shown": 0}
        self._hover_index: int | None = None
        self._over_more = False
        self._item_drag: dict | None = None   # 设备项拖动排序状态
        self._tracking_mouse = False
        # 设备名 -> 上次报警时间戳；启动时从磁盘恢复，跨重启仍记得
        self.alert_state: dict[str, float] = load_alert_state()
        # 设备事件监听 + 上次轻量连接快照（用于判断是否需要立即刷新）
        self.watcher = DeviceWatcher()
        self._conn_sig: tuple | None = None
        # --- 与天气小组件平滑共处 / 过渡动画 ---
        self._weather_gap = None       # 启动时实测出的动态避让距离
        self._weather_box = None       # 天气组件的屏幕 (left, right)
        self._layout_sig = None        # 当前显示的设备名集合（决定是否做过渡）
        self._prev_card = None         # 上一帧内容，用于交叉淡化
        self._final_frame = None       # (最终图, x, y, z)，供淡出/重绘复用
        self._last_pos = None          # 上次 SetWindowPos 的 (x, y, z)
        self._anim = None              # 正在播放的设备过渡动画
        self._vis_alpha = 1.0          # 整体可见度（悬停天气时降到 0）
        self._vis_target = 1.0
        self._last_tn_left = None      # 托盘区左边缘，变化即重新贴位

    def _measure_alignment(self):
        """在干净的任务栏上量一次天气组件边界，算出安全避让距离。"""
        host = find_taskbar_host()
        if not host or not host[2]:
            return
        measured = measure_weather_reserve(host[1], host[2])
        if measured:
            self._weather_box = (measured[1], measured[2])
        base = measured[0] if measured else 0
        try:
            allowance = max(0, int(self.cfg.get("weather_growth_allowance", 56)))
            margin = max(0, int(self.cfg.get("weather_safe_margin", 24)))
            fallback = max(0, int(self.cfg.get("taskbar_child_right_gap", 260)))
            cap = max(fallback, int(self.cfg.get("weather_reserve_max", 380)))
        except (TypeError, ValueError):
            allowance, margin, fallback, cap = 56, 24, 260, 380
        gap = fallback if not measured else base + allowance + margin
        self._weather_gap = max(fallback, min(cap, gap))

    def _gap(self):
        return self._weather_gap if self._weather_gap is not None else int(
            self.cfg.get("taskbar_child_right_gap", 260))

    def is_child_mode(self) -> bool:
        return self.cfg.get("display_mode", "taskbar_child") == "taskbar_child"

    # -- 窗口创建 / 重建 ---------------------------------------------------
    def create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        use_child = False
        parent, x, y = None, 0, 0
        self._last_pos = None
        if self.is_child_mode():
            host = find_taskbar_host()
            if host:
                # 此时本窗口还没创建，任务栏是「干净」的，能量到天气组件真实边界
                self._measure_alignment()
                cp = child_position(self.width, self.height,
                                    host[0], host[1], host[2], self.cfg,
                                    gap_override=self._gap())
                if cp:
                    use_child = True
                    parent = host[0]
                    x, y = cp
        self.child_mode = use_child
        self.tray_hwnd = parent

        ex_style = WS_EX_LAYERED | WS_EX_NOACTIVATE
        if not use_child:
            ex_style |= WS_EX_TOOLWINDOW
            if self.cfg.get("always_on_top", True):
                ex_style |= WS_EX_TOPMOST
            win_style = WS_POPUP
        else:
            # 真子窗口：父窗口是 Explorer 的 Shell_TrayWnd，坐标用其客户区坐标
            win_style = WS_CHILD | WS_CLIPSIBLINGS

        def make(ex, style, par, px, py):
            return user32.CreateWindowExW(
                ex, CLASS_NAME, "bt-battery-widget", style,
                px, py, self.width, self.height, par, None, hinst, None)

        self.hwnd = make(ex_style, win_style, parent, x, y)
        if not self.hwnd and use_child:
            # 极少数环境禁止跨进程分层子窗口：自动退回同色浮窗条，保证能用
            self.child_mode = False
            self.tray_hwnd = None
            fb = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            if self.cfg.get("always_on_top", True):
                fb |= WS_EX_TOPMOST
            self.hwnd = make(fb, WS_POPUP, None, 0, 0)
        if not self.hwnd:
            raise OSError(C.get_last_error(), "CreateWindowExW 失败")
        user32.ShowWindow(self.hwnd, SW_SHOW)
        if use_child:
            # 新建子窗口默认在 Z 顶，仍显式置顶一次，避免被 Composition 桥盖住
            user32.SetWindowPos(self.hwnd, HWND_TOP, 0, 0, 0, 0,
                                SWP_NOACTIVATE | SWP_NOSIZE | 0x0002)  # SWP_NOMOVE

    def rebuild(self):
        """Explorer 重启 / 模式切换后重建窗口；WM_DESTROY 期间不退出消息循环。"""
        self._stop_anim()
        self._prev_card = None
        self._layout_sig = None
        self._rebuilding = True
        try:
            if self.hwnd:
                user32.DestroyWindow(self.hwnd)
        finally:
            self.hwnd = None
            self._last_pos = None
            self._rebuilding = False
        self.create_window()
        self._arm_timers()
        self.paint()

    def _arm_timers(self):
        """窗口重建后所有定时器都会失效，必须重新挂上。"""
        if not self.hwnd:
            return
        ms = max(10, int(self.cfg.get("refresh_seconds", 60))) * 1000
        user32.SetTimer(self.hwnd, 1, ms, None)      # 全量刷新兜底
        user32.SetTimer(self.hwnd, 3, 1000, None)    # 子窗口自愈自检
        try:
            quick = max(1, min(60, int(self.cfg.get("quick_check_seconds", 2))))
        except (TypeError, ValueError):
            quick = 2
        user32.SetTimer(self.hwnd, QUICK_TIMER_ID, quick * 1000, None)
        if self.child_mode:
            try:
                watch = max(40, min(1000, int(self.cfg.get("layout_watch_ms", 100))))
            except (TypeError, ValueError):
                watch = 100
            user32.SetTimer(self.hwnd, LAYOUT_TIMER_ID, watch, None)
        if self.smoke:
            user32.SetTimer(self.hwnd, 2, 3000, None)

    def reconcile(self):
        """每秒自检：Shell_TrayWnd 句柄变化或子窗口被销毁就立刻重建。"""
        if not self.child_mode or not self.hwnd or self._rebuilding:
            return
        host = find_taskbar_host()
        if not host:                 # Explorer 正在重启，等下一拍
            return
        tray = host[0]
        if (tray != self.tray_hwnd
                or not user32.IsWindow(self.hwnd)
                or user32.GetParent(self.hwnd) != tray):
            self.rebuild()

    # -- 位置 ---------------------------------------------------------------
    def default_pos(self, w: int, h: int):
        r = RECT()
        if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, C.byref(r), 0):
            r = RECT(0, 0, 2560, 1440)
        return r.right - w - 16, r.bottom - h - 12

    def current_pos(self, w: int, h: int):
        # 记住的位置是绝对屏幕坐标，换电脑 / 改分辨率后可能落到屏幕外。
        # 只有仍完整落在当前工作区内才沿用，否则自动回落到右下角。
        pos = self.cfg.get("widget_pos")
        if pos:
            try:
                px, py = int(pos[0]), int(pos[1])
            except (TypeError, ValueError, IndexError):
                px = py = None
            if px is not None:
                r = RECT()
                if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, C.byref(r), 0):
                    # 允许右/下边缘各留一点余量：部分完整可见即认为坐标仍有效
                    if (r.left <= px <= r.right - w
                            and r.top <= py <= r.bottom - h):
                        return px, py
        return self.default_pos(w, h)

    # -- 绘制 ---------------------------------------------------------------
    def refresh(self, reload: bool = False):
        if reload:
            self.cfg = load_all()
        try:
            devices = core.read_batteries(self.cfg)
        except Exception as exc:
            devices = []
            self.stamp = "读取失败"
            print("read_batteries failed:", repr(exc))
        else:
            self.stamp = datetime.now().strftime("%H:%M")
        hidden = set(self.cfg.get("hidden_devices", []))
        # 只保留一个开关：hide_disconnected。旧的 hide_stale 不再单独生效，
        # 避免两个开关叠加导致「显示未连接设备」点了却仍然看不见设备。
        hide_offline = bool(self.cfg.get("hide_disconnected", True))
        self.devices = []
        for dev in devices:
            if dev["name"] in hidden:
                continue
            dev["_age_days"] = dev.get("age_days")
            dev["_stale"] = dev.get("offline") is True
            dev["_offline_reason"] = dev.get("offline_reason", "")
            if hide_offline and dev["_stale"]:
                continue
            self.devices.append(dev)
        # 在线优先（可拖动覆盖）：默认低电量排前面，用户拖过之后按自定义顺序
        self.devices = sort_devices(self.devices, self.cfg)
        self._hover_index = None
        self.tip.close()
        self.paint()
        self._check_low_battery()
        self._sync_signature()

    def _sync_signature(self):
        """刷新后同步轻量快照，避免下一拍探测误判为「又变了」。"""
        try:
            self._conn_sig = core.connection_signature()
        except Exception:
            pass

    def quick_check(self):
        """高频轻量探测（约 6ms）：只有设备上下线 / 电量变化才做完整刷新。

        事件驱动的 WM_DEVICECHANGE 覆盖「有 PnP 插拔事件」的情况；BLE 设备
        连接有时只是 IsConnected 属性翻转、系统不发广播，就靠这个兜底。
        """
        try:
            sig = core.connection_signature()
        except Exception:
            return
        if sig == self._conn_sig:
            return
        self.refresh(reload=True)

    # -- 低电量气泡报警 -----------------------------------------------------
    def _alert_threshold(self) -> int:
        try:
            return max(1, min(99, int(self.cfg.get("alert_threshold", 10))))
        except (TypeError, ValueError):
            return 10

    def _check_low_battery(self, now: float | None = None):
        """每台设备在一个冷却窗口（默认 24 小时）内最多弹一次气泡。

        报警时间戳持久化到 alert_state.json，所以以下情况都**不会**让同一台
        设备重复弹窗：电量一直低于阈值、设备断开后重连、充回阈值以上再跌破、
        关闭再打开提醒开关、程序重启 / 重新登录。
        断开设备只有缓存电量，明确排除，避免误报。
        """
        if not self.cfg.get("low_battery_alert", True):
            return
        threshold = self._alert_threshold()
        cooldown = alert_cooldown_seconds(self.cfg)
        now = time.time() if now is None else float(now)
        low = [d for d in self.devices
               if not d.get("_stale") and int(d.get("level", 100)) <= threshold]
        if not low:
            return

        # 以磁盘记录为准取较晚的时间戳（状态文件可能被另一实例或手工改过）；
        # 明显属于未来的记录（系统时钟回拨）视为无效，避免永久锁死不报警。
        for name, ts in load_alert_state().items():
            if (0 < ts <= now + cooldown
                    and ts > self.alert_state.get(name, 0.0)):
                self.alert_state[name] = ts

        def in_cooldown(ts: float) -> bool:
            return 0 < ts <= now + cooldown and (now - ts) < cooldown

        fresh = [d for d in low
                 if not in_cooldown(self.alert_state.get(d["name"], 0.0))]
        if not fresh:
            return

        # 二次读盘，避免两个进程恰好同时判定为「该报」而各弹一次
        disk = load_alert_state()
        fresh = [d for d in fresh
                 if not in_cooldown(disk.get(d["name"], 0.0))]
        if not fresh:
            return

        # 顺手清理很久以前 / 异常未来的记录，避免文件无限增长或锁死
        cutoff = now - max(cooldown * 2, 30 * 86400)
        state = {k: v for k, v in self.alert_state.items()
                 if cutoff <= v <= now + cooldown}
        for d in fresh:
            state[d["name"]] = now
        save_alert_state(state)
        self.alert_state = state
        self.bubble.show(fresh, threshold, self.cfg)

    def _reset_alert_state(self):
        """手动清除冷却记录（菜单/排障用），清完后可立即重新报警。"""
        self.alert_state = {}
        try:
            os.remove(ALERT_STATE_PATH)
        except OSError:
            pass

    def _demo_bubble(self):
        """菜单「测试气泡提醒」：用假数据演示，不写入真实报警状态。"""
        self.bubble.show([{"name": "OMR251 BT2", "level": 8}],
                         self._alert_threshold(), self.cfg)

    # -- 绘制：移动与像素分离，消除「先拉伸旧图再贴新图」的一帧撕裂 ---------
    def _apply(self, img, x, y, z):
        """只移动窗口；尺寸和像素由 UpdateLayeredWindow 一次性原子更新。

        旧做法 SetWindowPos(..., w, h) 再 blit：尺寸先变、新像素后到，DWM
        会把旧位图拉伸显示一帧，设备上下线时就表现为「啪」地跳一下。
        """
        if not self.hwnd:
            return
        pos = (x, y, z)
        if pos != self._last_pos:
            user32.SetWindowPos(self.hwnd, z, x, y, 0, 0,
                                SWP_NOACTIVATE | SWP_NOSIZE | SWP_SHOWWINDOW)
            self._last_pos = pos
        blit_layered(self.hwnd, scale_alpha(img, self._vis_alpha))

    def _stop_anim(self):
        if self._anim and self.hwnd:
            try:
                user32.KillTimer(self.hwnd, ANIM_TIMER_ID)
            except Exception:
                pass
        self._anim = None

    def _anim_frame(self, e: float) -> Image.Image:
        """旧内容淡出并左移、新内容淡入并从右侧滑入；画布右边缘固定。"""
        a = self._anim
        old, new = a["old"], a["new"]
        cw, ch = a["canvas_size"]
        frame = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        b = a["baseline"]
        ox = b - old.width - int(8 * e)
        nx = b - new.width + int(8 * (1 - e))
        frame.alpha_composite(scale_alpha(old, 1 - e),
                              (max(0, ox), (ch - old.height) // 2))
        frame.alpha_composite(scale_alpha(new, e),
                              (max(0, nx), (ch - new.height) // 2))
        return frame

    def _start_anim(self, old, new, x, y, z):
        w = max(old.width, new.width)
        h = max(old.height, new.height)
        baseline = w + 8
        canvas_w = baseline + 16
        self._anim = {
            "old": old, "new": new, "final": new,
            "canvas_size": (canvas_w, h), "baseline": baseline,
            # 画布额外宽度全是 alpha=0，鼠标可以穿透，不会抢天气组件的 hover
            "canvas_x": x + new.width - baseline,
            "geom": (x, y, z), "t0": time.perf_counter(),
            "dur": max(60, min(600, int(self.cfg.get("transition_ms", 200)))) / 1000.0,
        }
        try:
            user32.SetTimer(self.hwnd, ANIM_TIMER_ID, ANIM_FRAME_MS, None)
        except Exception:
            self._stop_anim()
            self._apply(new, x, y, z)
            self._prev_card = new

    def _anim_step(self):
        a = self._anim
        if not a:
            return
        try:
            t = (time.perf_counter() - a["t0"]) / a["dur"]
            if t >= 1.0:
                self._stop_anim()
                self._apply(a["final"], *a["geom"])
                self._prev_card = a["final"]
                return
            e = 1 - (1 - t) ** 3                 # ease-out
            self._apply(self._anim_frame(e), a["canvas_x"],
                        a["geom"][1], a["geom"][2])
        except Exception as exc:
            # 任何一帧出错都必须收敛到最终画面，不能让 20ms 定时器一直空转
            print("transition frame failed:", repr(exc))
            self._stop_anim()
            self._apply(a["final"], *a["geom"])
            self._prev_card = a["final"]

    def layout_watch(self):
        """100ms 一拍：托盘宽度变了立刻贴位；鼠标进入天气组件时淡出本组件。"""
        if not self.hwnd or not self.child_mode or self._anim:
            return
        host = find_taskbar_host()
        if not host:
            return
        r = host[1]
        tn_left = host[2].left if host[2] else r.right - 260
        if self._last_tn_left is not None and tn_left != self._last_tn_left:
            self.paint(animate=False)       # 不等 10s 全量刷新，避免短暂错位
        self._last_tn_left = tn_left
        target = 1.0
        if self.cfg.get("hover_fade", True) and self._weather_box:
            pt = POINT()
            user32.GetCursorPos(C.byref(pt))
            left, right = self._weather_box
            if (left - 16 <= pt.x <= right + 16
                    and r.top - 8 <= pt.y <= r.bottom + 8):
                target = 0.0
        self._vis_target = target
        if abs(self._vis_alpha - target) < 0.01:
            if self._vis_alpha != target:
                self._vis_alpha = target
                self._redraw_visible()
            return
        self._vis_alpha = max(0.0, min(1.0, self._vis_alpha +
                                       (0.3 if target > self._vis_alpha else -0.3)))
        self._redraw_visible()

    def _redraw_visible(self):
        if self._final_frame:
            try:
                self._apply(*self._final_frame)
            except Exception as exc:
                print("redraw failed:", repr(exc))

    def realign(self):
        """右键「重新对齐」：隐藏窗口后重新量一次天气组件边界。"""
        self._stop_anim()
        if self.hwnd and self.child_mode:
            user32.ShowWindow(self.hwnd, 0)      # SW_HIDE，别让自己的像素干扰测量
            time.sleep(0.06)                    # 等任务栏 XAML 重绘一帧
        self._measure_alignment()
        if self.hwnd and self.child_mode:
            user32.ShowWindow(self.hwnd, SW_SHOW)
        self._last_pos = None
        self._last_tn_left = None
        self.paint(animate=False)

    def paint(self, animate: bool = True):
        if not self.hwnd:
            return
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        mode = self.cfg.get("display_mode", "taskbar_child")
        if mode in ("taskbar", "taskbar_child"):
            host = find_taskbar_host()
            r = host[1] if host else None
            tb_h = (r.bottom - r.top) if r else 40
            if not 20 <= tb_h <= 90:
                tb_h = 40
            if self._tb_bg is None:
                self._tb_bg = sample_taskbar_bg(r) if r else TB_FALLBACK_BG
            want_child = mode == "taskbar_child" and self.child_mode
            card = render_taskbar(self.cfg, self.devices, self._tb_bg, tb_h,
                                  transparent_bg=want_child,
                                  hover_index=self._hover_index,
                                  hit_out=self._hit)
            w, h = card.size
            x, y = taskbar_position(w, h, r, self.cfg, sw, sh)
            z = HWND_TOPMOST if self.cfg.get("always_on_top", True) else HWND_NOTOPMOST
            is_child = False
            if want_child and host:
                cp = child_position(w, h, host[0], r, host[2], self.cfg,
                                    gap_override=self._gap())
                if cp:
                    x, y = cp                 # 父窗口客户区坐标
                    z = HWND_TOP              # 子窗口置于同级 Z 顶
                    is_child = True
        else:
            card = render_card(self.cfg, self.devices, self.stamp)
            w, h = card.size
            x, y = self.current_pos(w, h)
            z = HWND_TOPMOST if self.cfg.get("always_on_top", True) else HWND_NOTOPMOST
            is_child = False
        self.width, self.height = w, h
        if is_child:
            x, y = max(0, x), max(0, y)
        else:
            x = max(0, min(x, max(0, sw - w)))
            y = max(0, min(y, max(0, sh - h)))
        self._final_frame = (card, x, y, z)

        sig = tuple((d["name"], bool(d.get("offline"))) for d in self.devices)
        changed = self._layout_sig is not None and sig != self._layout_sig
        self._layout_sig = sig
        if self._anim:                      # 动画中又变了：收敛到最新状态，不叠帧
            self._stop_anim()
            animate = False
        smooth = (animate and bool(self.cfg.get("smooth_transition", True))
                  and mode in ("taskbar", "taskbar_child"))
        if smooth and changed and self._prev_card is not None:
            self._start_anim(self._prev_card, card, x, y, z)
            return
        self._apply(card, x, y, z)
        self._prev_card = card

    # -- 设备项交互：命中 / 悬停 / 拖动排序 -------------------------------
    @staticmethod
    def _xy(lparam) -> tuple[int, int]:
        return int(lparam) & 0xFFFF, (int(lparam) >> 16) & 0xFFFF

    def _hit_test(self, x: int, y: int):
        """返回 ("item", 序号) / ("more", None) / None；动画期间不命中。"""
        if self._anim or not self.hwnd:
            return None
        if not 0 <= y <= self.height:
            return None
        for i, (x0, x1) in enumerate(self._hit.get("items", [])):
            if x0 <= x <= x1:
                return ("item", i)
        more = self._hit.get("more")
        if more and more[0] <= x <= more[1]:
            return ("more", None)
        return None

    def _clear_hover(self):
        if self._hover_index is not None or self._over_more:
            self._hover_index = None
            self._over_more = False
            self.tip.close()
            self.paint(animate=False)
        else:
            self.tip.close()

    def _hover_at(self, idx: int | None, more: bool = False):
        if self._hover_index == idx and self._over_more == more:
            if idx is None:
                self.tip.close()
            return
        self._hover_index = idx
        self._over_more = more
        if (idx is not None and idx < len(self.devices)
                and idx < len(self._hit.get("items", [])) and self.hwnd):
            r = RECT()
            if user32.GetWindowRect(self.hwnd, C.byref(r)):
                x0, x1 = self._hit["items"][idx]
                if self.cfg.get("hover_tips", True):
                    self.tip.show(self.devices[idx], self.cfg, r, x0, x1)
                else:
                    self.tip.close()
        else:
            self.tip.close()
        self.paint(animate=False)

    def _arm_mouse_leave(self):
        if self._tracking_mouse or not self.hwnd:
            return
        tme = TRACKMOUSEEVENT()
        tme.cbSize = C.sizeof(TRACKMOUSEEVENT)
        tme.dwFlags = TME_LEAVE
        tme.hwndTrack = self.hwnd
        if user32.TrackMouseEvent(C.byref(tme)):
            self._tracking_mouse = True

    def _move_drag_item(self, x: int):
        drag = self._item_drag
        if not drag or not self.devices:
            return
        regions = self._hit.get("items", [])
        if not regions:
            return
        target = None
        for i, (x0, x1) in enumerate(regions):
            if x0 <= x <= x1:
                target = i
                break
        if target is None:
            target = 0 if x < regions[0][0] else len(regions) - 1
        name = drag["name"]
        cur = next((i for i, d in enumerate(self.devices)
                    if d["name"] == name), None)
        if cur is None or cur == target:
            return
        self.devices.insert(target, self.devices.pop(cur))
        drag["moved"] = True
        self._hover_index = target
        self.tip.close()
        self.paint(animate=False)

    def popup_overflow_menu(self):
        """「+N」点击后列出未显示的其余设备，点击项把它置顶显示。"""
        shown = self._hit.get("shown", len(self.devices))
        rest = self.devices[shown:]
        if not rest:
            return
        pt = POINT()
        user32.GetCursorPos(C.byref(pt))
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, 0, "更多蓝牙设备（点击置顶）")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        for i, dev in enumerate(rest):
            label = truncate(dev["name"], 22) + "  %d%%" % dev["level"]
            if dev.get("offline"):
                label += "  (未连接)"
            user32.AppendMenuW(menu, MF_STRING, 800 + i, label)
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(
            menu, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if 800 <= cmd < 800 + len(rest):
            dev = rest[cmd - 800]
            names = [dev["name"]] + [d["name"] for d in self.devices
                                     if d["name"] != dev["name"]]
            self.cfg["device_order"] = names
            save_all(self.cfg)
            self.refresh()

    # -- 菜单 ---------------------------------------------------------------
    def popup_menu(self):
        pt = POINT()
        user32.GetCursorPos(C.byref(pt))
        menu = user32.CreatePopupMenu()
        # 主菜单只留高频项；低频开关收进「设置」级联菜单，鼠标悬停即展开，
        # 点一下子菜单项立即生效，不必额外弹窗或多做动作。
        user32.AppendMenuW(menu, MF_STRING, 1, "立即刷新")
        mode = self.cfg.get("display_mode", "taskbar_child")
        mode_label = "显示方式：%s（点击切换）" % MODE_LABELS.get(mode, mode)
        user32.AppendMenuW(menu, MF_STRING, 6, mode_label)
        show_flag = MF_STRING | (MF_CHECKED
                                 if not self.cfg.get("hide_disconnected", True) else 0)
        user32.AppendMenuW(menu, show_flag, 7, "显示未连接设备")

        # 「设置」级联子菜单：平时用不上 / 调整频率低的选项都收在这里
        setting = user32.CreatePopupMenu()
        autostart_flag = MF_STRING | (MF_CHECKED if core.autostart_enabled() else 0)
        user32.AppendMenuW(setting, autostart_flag, 2, "开机自动启动")
        name_flag = MF_STRING | (MF_CHECKED
                                 if self.cfg.get("tb_show_name", False) else 0)
        user32.AppendMenuW(setting, name_flag, 8, "任务栏显示设备名称")
        tip_flag = MF_STRING | (MF_CHECKED
                                if self.cfg.get("hover_tips", True) else 0)
        user32.AppendMenuW(setting, tip_flag, 15, "设备悬停提示")
        user32.AppendMenuW(setting, MF_SEPARATOR, 0, None)
        alert_flag = MF_STRING | (MF_CHECKED
                                  if self.cfg.get("low_battery_alert", True) else 0)
        user32.AppendMenuW(setting, alert_flag, 9,
                           "低电量提醒（≤%d%%）" % self._alert_threshold())
        user32.AppendMenuW(setting, MF_STRING, 10, "测试气泡提醒")
        user32.AppendMenuW(setting, MF_STRING, 11, "重置报警记录（清除冷却）")
        user32.AppendMenuW(setting, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(setting, MF_STRING, 12, "重新对齐（避开天气小组件）")
        anim_flag = MF_STRING | (MF_CHECKED
                                 if self.cfg.get("smooth_transition", True) else 0)
        user32.AppendMenuW(setting, anim_flag, 13, "设备上下线平滑过渡")
        fade_flag = MF_STRING | (MF_CHECKED
                                 if self.cfg.get("hover_fade", True) else 0)
        user32.AppendMenuW(setting, fade_flag, 14, "悬停天气时自动淡出")
        user32.AppendMenuW(setting, MF_STRING, 16, "恢复默认设备顺序")

        all_devices = core.read_batteries(self.cfg)
        if all_devices:
            dev_sub = user32.CreatePopupMenu()
            hidden = set(self.cfg.get("hidden_devices", []))
            stale_days = int(self.cfg.get("stale_days", 2))
            for i, dev in enumerate(all_devices):
                f = MF_STRING | (MF_CHECKED if dev["name"] not in hidden else 0)
                label = truncate(dev["name"], 22) + "  %d%%" % dev["level"]
                age = connection_age(dev)
                if age is not None and age >= stale_days:
                    label += "  (缓存 %d 天)" % age
                user32.AppendMenuW(dev_sub, f, 100 + i, label)
            user32.AppendMenuW(setting, MF_POPUP | MF_STRING,
                               int(dev_sub or 0), "显示的设备")

        user32.AppendMenuW(setting, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(setting, MF_STRING, 3, "打开配置文件")
        user32.AppendMenuW(menu, MF_POPUP | MF_STRING, int(setting or 0), "设置")

        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 4, "退出")

        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(
            menu, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)

        if cmd == 1:
            self.refresh(reload=True)
        elif cmd == 2:
            core.set_autostart(not core.autostart_enabled())
        elif cmd == 6:
            cur = self.cfg.get("display_mode", "taskbar_child")
            idx = MODE_ORDER.index(cur) if cur in MODE_ORDER else -1
            self.cfg["display_mode"] = MODE_ORDER[(idx + 1) % len(MODE_ORDER)]
            self.cfg["widget_pos"] = None
            self._tb_bg = None
            save_all(self.cfg)
            self.rebuild()          # WS_CHILD / WS_POPUP 不能热切换，必须重建
        elif cmd == 7:
            self.cfg["hide_disconnected"] = not self.cfg.get("hide_disconnected", True)
            save_all(self.cfg)
            self.refresh()
        elif cmd == 8:
            self.cfg["tb_show_name"] = not self.cfg.get("tb_show_name", False)
            save_all(self.cfg)
            self.refresh()
        elif cmd == 9:
            self.cfg["low_battery_alert"] = not self.cfg.get("low_battery_alert", True)
            save_all(self.cfg)
            # 不清空冷却记录：开关拨回开启也不该让同一台设备重复弹窗
            self.bubble.close()
        elif cmd == 10:
            self._demo_bubble()
        elif cmd == 11:
            self._reset_alert_state()
        elif cmd == 12:
            self.realign()
        elif cmd == 13:
            self.cfg["smooth_transition"] = not self.cfg.get("smooth_transition", True)
            save_all(self.cfg)
            self._stop_anim()
            self.paint(animate=False)
        elif cmd == 14:
            self.cfg["hover_fade"] = not self.cfg.get("hover_fade", True)
            save_all(self.cfg)
            self._vis_target = 1.0
            self._vis_alpha = 1.0
            self.paint(animate=False)
        elif cmd == 15:
            self.cfg["hover_tips"] = not self.cfg.get("hover_tips", True)
            save_all(self.cfg)
            if not self.cfg["hover_tips"]:
                self.tip.close()
        elif cmd == 16:
            self.cfg["device_order"] = []
            save_all(self.cfg)
            self.refresh()
        elif cmd == 3:
            try:
                os.startfile(core.CONFIG_PATH)
            except OSError:
                pass
        elif cmd == 4:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        elif 100 <= cmd < 100 + len(all_devices):
            name = all_devices[cmd - 100]["name"]
            hidden = set(self.cfg.get("hidden_devices", []))
            hidden.symmetric_difference_update({name})
            self.cfg["hidden_devices"] = sorted(hidden)
            save_all(self.cfg)
            self.refresh()

    # -- 消息 ---------------------------------------------------------------
    def on_message(self, hwnd, msg, wparam, lparam):
        # 本程序只创建两种窗口：主窗口与报警气泡。凡不是主窗口的消息一律
        # 转给气泡处理，尤其 WM_DESTROY —— 否则气泡关闭会把整个消息循环退出。
        if hwnd != self.hwnd:
            if self.tip.hwnd and hwnd == self.tip.hwnd:
                return self.tip.on_message(msg, wparam)
            return self.bubble.on_message(msg, wparam)
        if msg == WM_SETCURSOR:
            # 自己设光标：设备项/「+N」用手型提示可交互，其余用标准箭头。
            hand = (self._hover_index is not None or self._over_more
                    or self._item_drag is not None)
            cur = self.hhand if hand else self.hcursor
            if cur:
                user32.SetCursor(cur)
                return 1
            return None
        if msg == WM_DESTROY:
            if not self._rebuilding:      # 重建窗口时不能结束消息循环
                user32.PostQuitMessage(0)
            return 0
        if msg == WM_CLOSE:
            user32.PostQuitMessage(0)
            return 0
        if self.wm_taskbar_created and msg == self.wm_taskbar_created:
            if self.is_child_mode():
                user32.SetTimer(self.hwnd, 4, 1500, None)   # 等 Explorer 起来
            return 0
        if msg == WM_DEVICECHANGE or msg == WM_APP_DEVICE_EVENT:
            # 设备事件常成批到达（一台蓝牙设备会连发多个接口通知），
            # 合并成 150ms 后一次刷新，既快又不会重复重绘。
            user32.SetTimer(self.hwnd, EVENT_TIMER_ID, 150, None)
            return 0
        if msg == WM_TIMER and wparam == EVENT_TIMER_ID:
            user32.KillTimer(self.hwnd, EVENT_TIMER_ID)
            self.refresh(reload=True)
            return 0
        if msg == WM_TIMER and wparam == QUICK_TIMER_ID:
            self.quick_check()
            return 0
        if msg == WM_TIMER and wparam == 1:
            self.refresh(reload=True)
            return 0
        if msg == WM_TIMER and wparam == 2:  # --smoke 自动退出
            user32.PostQuitMessage(0)
            return 0
        if msg == WM_TIMER and wparam == 3:
            self.reconcile()
            return 0
        if msg == WM_TIMER and wparam == ANIM_TIMER_ID:
            self._anim_step()
            return 0
        if msg == WM_TIMER and wparam == LAYOUT_TIMER_ID:
            self.layout_watch()
            return 0
        if msg == WM_TIMER and wparam == 4:
            user32.KillTimer(self.hwnd, 4)
            if self.is_child_mode():
                self.rebuild()
            return 0
        if msg == WM_APP_REFRESH:
            self.refresh(reload=True)
            return 0
        if msg == WM_APP_ALERT_DEMO:
            self._demo_bubble()
            return 0
        if msg == WM_MOUSELEAVE:
            self._tracking_mouse = False
            self._clear_hover()
            return 0
        if msg == WM_LBUTTONDOWN:
            x, y = self._xy(lparam)
            hit = self._hit_test(x, y)
            if hit and hit[0] == "more":
                self.popup_overflow_menu()
                return 0
            if hit and hit[0] == "item":
                dev = self.devices[hit[1]]
                self._item_drag = {"name": dev["name"], "moved": False}
                self.tip.close()
                user32.SetCapture(hwnd)
                return 0
            if self.child_mode:           # 子窗口不许被拖出任务栏
                return 0
            pt = POINT()
            user32.GetCursorPos(C.byref(pt))
            r = RECT()
            user32.GetWindowRect(hwnd, C.byref(r))
            self.dragging = True
            self.drag_start = (pt.x, pt.y)
            self.win_start = (r.left, r.top)
            user32.SetCapture(hwnd)
            return 0
        if msg == WM_MOUSEMOVE:
            x, y = self._xy(lparam)
            if self._item_drag is not None:
                self._move_drag_item(x)
                return 0
            if self.dragging:
                pt = POINT()
                user32.GetCursorPos(C.byref(pt))
                nx = self.win_start[0] + pt.x - self.drag_start[0]
                ny = self.win_start[1] + pt.y - self.drag_start[1]
                user32.SetWindowPos(hwnd, HWND_TOPMOST, nx, ny, 0, 0,
                                    SWP_NOACTIVATE | SWP_NOSIZE)
                return 0
            self._arm_mouse_leave()
            hit = self._hit_test(x, y)
            if hit and hit[0] == "item":
                self._hover_at(hit[1], False)
            elif hit and hit[0] == "more":
                self._hover_at(None, True)
            else:
                self._hover_at(None, False)
            return 0
        if msg == WM_LBUTTONUP:
            if self._item_drag is not None:
                drag = self._item_drag
                self._item_drag = None
                user32.ReleaseCapture()
                if drag.get("moved"):
                    self.cfg["device_order"] = [d["name"] for d in self.devices]
                    save_all(self.cfg)
                self.paint(animate=False)
                return 0
            if self.dragging:
                self.dragging = False
                user32.ReleaseCapture()
                r = RECT()
                user32.GetWindowRect(hwnd, C.byref(r))
                self.cfg["widget_pos"] = [r.left, r.top]
                save_all(self.cfg)
                return 0
            return 0
        if msg == WM_LBUTTONDBLCLK:
            self.refresh(reload=True)
            return 0
        if msg == WM_RBUTTONUP:
            self.tip.close()
            self.popup_menu()
            return 0
        return None

    # -- 生命周期 -----------------------------------------------------------
    def run(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = C.sizeof(WNDCLASSEXW)
        wc.style = CS_DBLCLKS
        wc.lpfnWndProc = _wnd_proc
        wc.hInstance = hinst
        wc.hCursor = self.hcursor or 0   # 标准箭头，不再让任务栏替我们决定光标
        wc.lpszClassName = CLASS_NAME
        atom = user32.RegisterClassExW(C.byref(wc))
        if not atom and C.get_last_error() != 1410:  # ERROR_CLASS_ALREADY_EXISTS
            raise OSError(C.get_last_error(), "RegisterClassExW 失败")

        self.wm_taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self.create_window()
        self.refresh()
        self._arm_timers()

        message = MSG()
        while True:
            ret = user32.GetMessageW(C.byref(message), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(C.byref(message))
            user32.DispatchMessageW(C.byref(message))
        self.bubble.close()
        self.tip.close()
        self.watcher.close()
        for tid in (1, 2, 3, 4, 5, QUICK_TIMER_ID, EVENT_TIMER_ID,
                    ANIM_TIMER_ID, LAYOUT_TIMER_ID):
            try:
                user32.KillTimer(self.hwnd, tid)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def selftest() -> int:
    cfg = load_all()
    print("BASE =", core.BASE)
    print("CONFIG_PATH =", core.CONFIG_PATH)
    print("cfg: display_mode=%s hide_disconnected=%s tb_show_name=%s tb_max_items=%s" % (
        cfg.get("display_mode"), cfg.get("hide_disconnected"),
        cfg.get("tb_show_name"), cfg.get("tb_max_items")))
    print("报警: 阈值≤%s%% 冷却=%.1f 小时  记录=%s（%d 条）" % (
        cfg.get("alert_threshold"), alert_cooldown_seconds(cfg) / 3600,
        ALERT_STATE_PATH, len(load_alert_state())))
    print("刷新: 事件驱动 150ms / 轻量探测 %ss / 全量兜底 %ss  监听窗口类=%s" % (
        cfg.get("quick_check_seconds"), cfg.get("refresh_seconds"),
        DEVICE_WATCHER_CLASS))
    print("过渡: 平滑动画=%s/%sms  悬停淡出=%s  天气避让兜底=%s 余量=%s+%s 上限=%s" % (
        cfg.get("smooth_transition"), cfg.get("transition_ms"),
        cfg.get("hover_fade"), cfg.get("taskbar_child_right_gap"),
        cfg.get("weather_growth_allowance"), cfg.get("weather_safe_margin"),
        cfg.get("weather_reserve_max")))
    if "--scan-taskbar" in sys.argv:
        try:
            _declare_api()
            C.windll.user32.SetProcessDPIAware()
            host = find_taskbar_host()
            m = measure_weather_reserve(host[1], host[2]) if host and host[2] else None
            print("天气组件扫描（需本组件未运行才准确）:", m)
        except Exception as exc:
            print("天气组件扫描失败:", repr(exc))
    try:
        t = time.perf_counter()
        sig = core.connection_signature()
        print("轻量签名: %d 台, %.4fs" % (len(sig), time.perf_counter() - t))
    except Exception as exc:
        print("轻量签名读取失败:", repr(exc))
    devices = core.read_batteries(cfg)
    print("读到 %d 台设备：" % len(devices))
    print("  %-28s %5s %-9s %-9s %-8s %s" % (
        "name", "level", "kind", "connected", "offline", "reason"))
    for d in devices:
        print("  %-28s %4d%% %-9s %-9s %-8s %s" % (
            d["name"][:28], d["level"], str(d.get("kind")),
            str(d.get("connected")), str(d.get("offline")),
            d.get("offline_reason", "")))
        d["_stale"] = d.get("offline") is True
        d["_offline_reason"] = d.get("offline_reason", "")
    visible = [d for d in devices
               if not (cfg.get("hide_disconnected", True) and d["_stale"])]
    print("过滤后显示 %d 台：" % len(visible))
    card = render_card(cfg, visible, datetime.now().strftime("%H:%M"))
    print("卡片渲染成功:", card.size, card.mode)
    cfg5 = dict(cfg)
    cfg5["device_order"] = []
    ordered = sort_devices(devices, cfg5)
    print("在线优先排序:", " -> ".join(
        "%s%s" % (d["name"][:10], "(离线)" if d.get("offline") else "")
        for d in ordered))
    fake = [
        {"name": "A", "level": 90, "offline": False},
        {"name": "B", "level": 10, "offline": False},
        {"name": "C", "level": 20, "offline": True,
         "offline_reason": "缓存 2 天"},
    ]
    got_default = [d["name"] for d in sort_devices(fake, dict(cfg, device_order=[]))]
    assert got_default == ["B", "A", "C"], got_default
    got = [d["name"] for d in sort_devices(
        fake, dict(cfg, device_order=["A", "C", "B"]))]
    assert got == ["A", "C", "B"], got
    print("排序自检: 在线优先=%s 自定义=%s" % (got_default, got))
    hit = {}
    tb = render_taskbar(cfg, visible, TB_FALLBACK_BG, 40, hit_out=hit,
                        hover_index=0 if visible else None)
    print("任务栏渲染: %s 命中区=%d 更多=%s 显示=%d" % (
        tb.size, len(hit["items"]), hit["more"], hit["shown"]))
    if visible:
        tip_img = render_tip(visible[0], cfg)
        print("悬停提示渲染成功:", tip_img.size, tip_img.mode)


    print("BGRA 字节数:", len(premultiply_bgra(card)))
    if "--save-preview" in sys.argv:
        card.save(os.path.join(BASE, "widget_preview.png"))
        tb.save(os.path.join(BASE, "taskbar_preview.png"))
        print("预览图: widget_preview.png / taskbar_preview.png")
    return 0


def main() -> int:
    global APP
    if "--selftest" in sys.argv:
        return selftest()
    if not core.acquire_singleton():
        return 0
    try:
        APP = Widget()
        APP.run()
    except Exception as exc:
        core.fatal("小组件启动失败：%r" % (exc,))
        return 1
    finally:
        APP = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
