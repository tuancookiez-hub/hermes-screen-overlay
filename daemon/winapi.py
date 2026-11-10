"""Win32 calls used by the overlay daemon (ctypes only, no pywin32).

Every coordinate is in physical pixels: the daemon runs Per-Monitor-V2 DPI aware
(Qt 6 sets that before any window exists).
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shcore = ctypes.WinDLL("shcore")
dwmapi = ctypes.WinDLL("dwmapi")

HWND = wintypes.HWND
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
MONITORINFOF_PRIMARY = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183
SPI_GETCLIENTAREAANIMATION = 0x1042

user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowDisplayAffinity.argtypes = [HWND, wintypes.DWORD]
user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [HWND]
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [HWND]
user32.IsIconic.argtypes = [HWND]
user32.IsWindow.argtypes = [HWND]
user32.GetForegroundWindow.restype = HWND
user32.GetAncestor.restype = HWND
user32.GetAncestor.argtypes = [HWND, ctypes.c_uint]
user32.WindowFromPoint.restype = HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.MonitorFromWindow.argtypes = [HWND, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
dwmapi.DwmGetWindowAttribute.argtypes = [HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


def _rect(r: wintypes.RECT) -> list[int]:
    return [r.left, r.top, r.right - r.left, r.bottom - r.top]


def single_instance(name: str) -> bool:
    """Hold a named mutex for the life of the process. False if another daemon has it."""
    handle = kernel32.CreateMutexW(None, False, name)
    single_instance._handle = handle  # keep it alive
    return bool(handle) and ctypes.get_last_error() != ERROR_ALREADY_EXISTS


# --------------------------------------------------------------------------- monitors
def monitor_info(hmon) -> dict:
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    user32.GetMonitorInfoW(hmon, ctypes.byref(info))
    x, y = ctypes.c_uint(), ctypes.c_uint()
    try:
        shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(x), ctypes.byref(y))
        dpi = x.value or 96
    except OSError:
        dpi = 96
    return {"device": info.szDevice, "rect": _rect(info.rcMonitor), "work": _rect(info.rcWork),
            "dpi": dpi, "scale": round(dpi / 96, 3), "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY)}


def monitors() -> list[dict]:
    handles: list[int] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(h, _dc, _r, _l):
        handles.append(h)
        return True

    user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    out = [monitor_info(h) for h in handles]
    out.sort(key=lambda m: (not m["primary"], m["rect"][0], m["rect"][1]))
    for i, m in enumerate(out):
        m["index"] = i
    return out


def virtual_screen() -> list[int]:
    g = user32.GetSystemMetrics
    return [g(76), g(77), g(78), g(79)]


# --------------------------------------------------------------------------- windows
def window_title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_name(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def frame_bounds(hwnd) -> list[int]:
    """Visible bounds. GetWindowRect includes ~7 px invisible resize borders on Windows 10/11."""
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return _rect(r)


def is_cloaked(hwnd) -> bool:
    v = wintypes.DWORD()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) != 0:
        return False
    return bool(v.value)


def is_minimized(hwnd) -> bool:
    return bool(user32.IsIconic(hwnd))


def is_window(hwnd) -> bool:
    return bool(user32.IsWindow(hwnd))


def foreground() -> int:
    return user32.GetForegroundWindow() or 0


def foreground_rect() -> list[int] | None:
    fg = foreground()
    return frame_bounds(fg) if fg else None


def top_level_windows(skip_pid: int | None = None) -> list[dict]:
    """Visible, uncloaked, titled top-level windows, in z-order (front first)."""
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, HWND, wintypes.LPARAM)

    def cb(h, _l):
        if user32.IsWindowVisible(h) and not is_cloaked(h) and user32.GetWindowTextLengthW(h) > 0:
            found.append(h)
        return True

    user32.EnumWindows(proc(cb), 0)
    fg = foreground()
    pid_names: dict[int, str] = {}
    out = []
    for z, h in enumerate(found):
        pid = window_pid(h)
        if skip_pid and pid == skip_pid:
            continue
        if pid not in pid_names:
            pid_names[pid] = process_name(pid)
        out.append({"hwnd": int(h), "title": window_title(h)[:160], "process": pid_names[pid], "pid": pid,
                    "rect": frame_bounds(h), "minimized": is_minimized(h), "foreground": int(h) == fg,
                    "z": z})
    return out


def find_window(ref, skip_pid: int | None = None) -> dict | None:
    """Window by handle, exact process name (notepad.exe), or title substring (front-most wins)."""
    wins = top_level_windows(skip_pid)
    if isinstance(ref, int):
        return next((w for w in wins if w["hwnd"] == ref), None)
    q = str(ref).lower()
    for w in wins:
        if w["process"].lower() == q or w["process"].lower() == q + ".exe":
            return w
    for w in wins:
        if q in w["title"].lower():
            return w
    return None


def window_at(x: int, y: int) -> int:
    h = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return int(user32.GetAncestor(h, 2) or 0)  # GA_ROOT


# --------------------------------------------------------------------------- overlay windows
def make_click_through(hwnd: int) -> None:
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                             ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)


def make_no_activate(hwnd: int) -> None:
    """Takes clicks, but Windows never gives it focus or the foreground."""
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def animations_enabled() -> bool:
    """False when the user turned animation off in Windows accessibility settings."""
    v = wintypes.BOOL(True)
    if user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(v), 0):
        return bool(v.value)
    return True


def exclude_from_capture(hwnd: int, on: bool) -> bool:
    return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE))


def place_topmost(hwnd: int, rect: list[int] | None = None) -> None:
    """Pin to the top band without activating; optionally force exact physical bounds."""
    if rect:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, rect[0], rect[1], rect[2], rect[3], SWP_NOACTIVATE)
    else:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def window_rect(hwnd: int) -> list[int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return _rect(r)


# --------------------------------------------------------------------------- emergency hotkey
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
VK_F12 = 0x7B
HOTKEY_LABEL = "Ctrl+Alt+Shift+F12"


def start_hotkey(on_fire: Callable[[], None]) -> threading.Event:
    """Register the global emergency hotkey on its own message-loop thread.

    Returns an Event that is set once registration is decided; check `.ok` on it.
    `on_fire` runs on the hotkey thread, so it must only set flags.
    """
    decided = threading.Event()
    decided.ok = False  # type: ignore[attr-defined]

    def run() -> None:
        ok = user32.RegisterHotKey(None, 0xB00F, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, VK_F12)
        decided.ok = bool(ok)  # type: ignore[attr-defined]
        decided.set()
        if not ok:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                on_fire()

    threading.Thread(target=run, name="overlay-hotkey", daemon=True).start()
    decided.wait(2)
    return decided
