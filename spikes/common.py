"""Win32 helpers shared by the feasibility spikes (not plugin code)."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
shcore = ctypes.windll.shcore
dwmapi = ctypes.windll.dwmapi

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
DWMWA_EXTENDED_FRAME_BOUNDS = 9

user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


def per_monitor_aware() -> bool:
    """Opt this process into Per-Monitor-V2 DPI awareness (physical pixels)."""
    try:
        return bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except Exception:
        return False


def monitors() -> list[dict]:
    out: list[dict] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(h, _dc, r, _l):
        x, y = ctypes.c_uint(), ctypes.c_uint()
        shcore.GetDpiForMonitor(h, 0, ctypes.byref(x), ctypes.byref(y))
        rc = r.contents
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        user32.GetMonitorInfoW(h, ctypes.byref(info))
        out.append({"device": info.szDevice, "x": rc.left, "y": rc.top, "w": rc.right - rc.left,
                    "h": rc.bottom - rc.top, "dpi": x.value, "scale": x.value / 96})
        return True

    user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    return out


def virtual_screen() -> tuple[int, int, int, int]:
    g = user32.GetSystemMetrics
    return g(76), g(77), g(78), g(79)


def window_rect(hwnd: int) -> dict:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return {"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top}


def frame_bounds(hwnd: int) -> dict:
    """Visible window bounds (excludes the invisible resize border/shadow)."""
    r = wintypes.RECT()
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r))
    return {"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top}


def make_click_through(hwnd: int) -> int:
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
    return ex


def exstyle(hwnd: int) -> int:
    return user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)


def window_at(x: int, y: int) -> int:
    h = user32.WindowFromPoint(wintypes.POINT(x, y))
    return user32.GetAncestor(h, 2) or 0  # GA_ROOT


def find_window(title: str) -> int:
    return user32.FindWindowW(None, title) or 0
