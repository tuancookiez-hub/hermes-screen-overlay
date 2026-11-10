"""Records every foreground/focus change while the overlay runs (dev only).

    python spikes/focus_test.py [monitor_index]

Pass = the foreground window and focused element never change because of the overlay.
Needs PySide6 + comtypes in the running Python. Draws on screen for about 15 seconds.
"""
from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["SCREEN_OVERLAY_DATA_DIR"] = tempfile.mkdtemp(prefix="so-focus-")
spec = importlib.util.spec_from_file_location("screen_overlay", ROOT / "__init__.py",
                                              submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec)
sys.modules["screen_overlay"] = pkg
spec.loader.exec_module(pkg)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

u = ctypes.WinDLL("user32", use_last_error=True)
k = ctypes.WinDLL("kernel32")
u.GetForegroundWindow.restype = wintypes.HWND
u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_FOCUS = 0x0003, 0x8005
k.OpenProcess.restype = wintypes.HANDLE
k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                         ctypes.POINTER(wintypes.DWORD)]


def who(hwnd) -> str:
    if not hwnd:
        return "(none)"
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = "?"
    h = k.OpenProcess(0x1000, False, pid.value)
    if h:
        size = wintypes.DWORD(600)
        buf = ctypes.create_unicode_buffer(600)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            name = os.path.basename(buf.value)
        k.CloseHandle(h)
    t = ctypes.create_unicode_buffer(120)
    u.GetWindowTextW(hwnd, t, 120)
    return f"{name}/pid{pid.value}/{t.value[:40]!r}"


events: list[dict] = []
stage = ["baseline"]
PROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG,
                          wintypes.DWORD, wintypes.DWORD)


def on_event(_hook, ev, hwnd, _obj, _child, _thread, _time):
    kind = "FOREGROUND" if ev == EVENT_SYSTEM_FOREGROUND else "focus"
    events.append({"t": round(time.perf_counter(), 2), "stage": stage[0], "kind": kind, "who": who(hwnd)})


cb = PROC(on_event)


def pump():
    a = u.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, cb, 0, 0, 0)
    b = u.SetWinEventHook(EVENT_OBJECT_FOCUS, EVENT_OBJECT_FOCUS, None, cb, 0, 0, 0)
    assert a and b
    msg = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u.DispatchMessageW(ctypes.byref(msg))


threading.Thread(target=pump, daemon=True).start()
time.sleep(1.5)

mi = int(sys.argv[1]) if len(sys.argv) > 1 else 1
c = pkg.client
s = {"exclude_from_capture": True, "default_ttl_seconds": 60, "idle_exit_minutes": 2}


def go(name, fn, wait=1.0):
    stage[0] = name
    fn()
    time.sleep(wait)


start_fg = who(u.GetForegroundWindow())
_r = wintypes.RECT()
u.GetWindowRect(u.GetForegroundWindow(), ctypes.byref(_r))
if not u.GetForegroundWindow() or any(m == (_r.right - _r.left, _r.bottom - _r.top) for m in ((3440, 1440), (1920, 1080))):
    sys.exit(f"refusing to run: fullscreen app in front: {start_fg}")
mons = None


def boot():
    global mons
    c.ensure(s)
    mons = c.request({"op": "inspect", "what": "screens"})["monitors"]


def show(x):
    m = mons[mi]["rect"]
    assert c.request({"op": "show", "replace": True, "ttl_seconds": 30, "shapes": [
        {"type": "step", "target": {"rect": [m[0] + 200 + x, m[1] + 200, 260, 80]}, "text": "Focus test"},
        {"type": "spotlight", "target": {"rect": [m[0] + 200 + x, m[1] + 200, 260, 80]}},
    ]})["ok"]


go("daemon-start", boot, 1.5)
go("first-show", lambda: show(0), 2.5)
go("topmost-tick", lambda: None, 4.5)
go("second-show", lambda: show(60), 1.5)
go("clear", lambda: c.request({"op": "clear"}), 1.0)
go("third-show", lambda: show(120), 1.5)
go("stop", lambda: c.request({"op": "stop"}), 1.0)
end_fg = who(u.GetForegroundWindow())

overlay_hits = [e for e in events if "python" in e["who"].lower() or "Hermes Screen Overlay" in e["who"]]
fg = [e for e in events if e["kind"] == "FOREGROUND"]
print(json.dumps({"foreground_start": start_fg, "foreground_end": end_fg, "total_events": len(events),
                  "foreground_changes": fg, "events_from_python": overlay_hits[:30]}, indent=1))
print("by stage:", {st: sum(1 for e in events if e["stage"] == st) for st in dict.fromkeys(e["stage"] for e in events)})
