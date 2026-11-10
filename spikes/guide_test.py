"""Live check of the pointer, step guide and glass notes (dev only).

    python spikes/guide_test.py [monitor_index]

Draws on one monitor for about 25 seconds, next to a small test window. Refuses to run over a
fullscreen app. Records every focus change while it runs. The Next button is pressed with one real
mouse click only if the user has been idle for 20 seconds; otherwise that part is reported as skipped.
Needs PySide6, comtypes, mss, pillow and psutil in the running Python.
"""
from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

import mss
import psutil
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "spikes" / "out"
os.environ["SCREEN_OVERLAY_DATA_DIR"] = tempfile.mkdtemp(prefix="so-guide-")
spec = importlib.util.spec_from_file_location("screen_overlay", ROOT / "__init__.py",
                                              submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec)
sys.modules["screen_overlay"] = pkg
spec.loader.exec_module(pkg)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

u = ctypes.WinDLL("user32", use_last_error=True)
k = ctypes.WinDLL("kernel32")
u.GetForegroundWindow.restype = wintypes.HWND
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u.SendMessageW.restype = ctypes.c_ssize_t
u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           ctypes.c_uint]
k.OpenProcess.restype = wintypes.HANDLE
k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                         ctypes.POINTER(wintypes.DWORD)]
WM_MOUSEACTIVATE, MA_NOACTIVATE = 0x21, 3
EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_FOCUS = 0x0003, 0x8005


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    u.GetLastInputInfo(ctypes.byref(li))
    return (k.GetTickCount() - li.dwTime) / 1000


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
    return f"{name}/pid{pid.value}"


events: list[dict] = []
stage = ["baseline"]
PROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG,
                          wintypes.DWORD, wintypes.DWORD)


def on_event(_hook, ev, hwnd, _obj, _child, _thread, _time):
    events.append({"stage": stage[0], "kind": "FOREGROUND" if ev == EVENT_SYSTEM_FOREGROUND else "focus",
                   "who": who(hwnd)})


cb = PROC(on_event)


def pump():
    u.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, cb, 0, 0, 0)
    u.SetWinEventHook(EVENT_OBJECT_FOCUS, EVENT_OBJECT_FOCUS, None, cb, 0, 0, 0)
    msg = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u.DispatchMessageW(ctypes.byref(msg))


threading.Thread(target=pump, daemon=True).start()
time.sleep(1.0)

HIDDEN = bool(os.environ.get("GUIDE_TEST_HIDDEN"))  # running on a private desktop: logic only, no pixels
fg = u.GetForegroundWindow()
r = wintypes.RECT()
u.GetWindowRect(fg, ctypes.byref(r))
if not HIDDEN and (not fg or any((r.right - r.left, r.bottom - r.top) == m for m in ((3440, 1440), (1920, 1080)))):
    sys.exit(f"refusing to run: fullscreen app or no foreground window: {who(fg)}")
start_fg = who(fg)

mi = int(sys.argv[1]) if len(sys.argv) > 1 else 1
c = pkg.client
settings = {"exclude_from_capture": False, "default_ttl_seconds": 60, "idle_exit_minutes": 2}
c.ensure(settings)
daemon_pid = c.request({"op": "ping"})["pid"]
mon = c.request({"op": "inspect", "what": "screens"})["monitors"][mi]
mx, my, mw, mh = mon["rect"]
tx, ty = mx + 240, my + 300
target = subprocess.Popen([sys.executable, str(ROOT / "spikes" / "target.py"), str(tx), str(ty)],
                          stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
hwnd = json.loads(target.stdout.readline().split(" ", 1)[1])["hwnd"]
time.sleep(0.4)
results: dict = {"start_foreground": start_fg}


def go(name):
    stage[0] = name


def status():
    return c.request({"op": "status"})


def shot(name):
    if HIDDEN:
        return
    with mss.mss() as s:
        g = s.grab({"left": mx, "top": my, "width": mw, "height": mh})
    Image.frombytes("RGB", g.size, g.rgb).save(OUT / name)


BTN = {"element": {"window": "Spike Target", "name": "Spike Target Button"}}
try:
    go("guide-start")
    res = c.request({"op": "guide", "title": "Spike guide", "steps": [
        {"title": "Press the magenta button", "text": "The pointer should glide to it.", "target": BTN},
        {"title": "This target does not exist yet", "target": {"element": {"window": "Spike Target", "name": "Nope"}}},
        {"title": "Last step has no target"},
    ]})
    results["guide_checks"] = res.get("checks")
    assert res["ok"], res
    time.sleep(2.2)
    st = status()
    results["step1"] = st["guide"]
    mark = next(s for s in st["visible_shapes"] if s["id"] == "guide-mark")
    found = c.request({"op": "inspect", "what": "find", "window": "Spike Target", "name": "Spike Target Button"})
    want = found["elements"][0]["rect"]
    results["mark_on_element"] = mark["rect"] == want
    shot("guide_1.png")

    go("window-moved")
    u.SetWindowPos(hwnd, None, tx + 180, ty + 60, 0, 0, 0x0015)  # NOSIZE|NOZORDER|NOACTIVATE
    time.sleep(2.6)
    st = status()
    mark = next(s for s in st["visible_shapes"] if s["id"] == "guide-mark")
    results["followed_window"] = mark["rect"] == [want[0] + 180, want[1] + 60, want[2], want[3]]
    shot("guide_2.png")

    go("panel-click")
    ph = st["panel"]["hwnd"]
    px, py, pw, ph_h = st["panel"]["rect"]
    results["mouse_activate_answer"] = u.SendMessageW(ph, WM_MOUSEACTIVATE, ph, (0x0201 << 16) | 1)
    results["panel_refuses_activation"] = results["mouse_activate_answer"] == MA_NOACTIVATE
    idle = idle_seconds()
    results["user_idle_s"] = round(idle)
    if idle >= 20 and not HIDDEN:
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetCursorPos(px + pw - 18 - 48, py + ph_h - 18 - 17)  # middle of Next
        time.sleep(0.12)
        u.mouse_event(0x2, 0, 0, 0, 0)
        time.sleep(0.05)
        u.mouse_event(0x4, 0, 0, 0, 0)
        time.sleep(0.15)
        u.SetCursorPos(pt.x, pt.y)
        results["real_click"] = True
    else:
        c.request({"op": "guide_move", "to": "next"})
        results["real_click"] = "skipped: user is active"
    time.sleep(1.6)
    st = status()
    results["step2_after_next"] = st["guide"]
    shot("guide_3.png")

    go("guide-move-api")
    results["back"] = c.request({"op": "guide_move", "to": "back"})["guide"]["step"]
    results["jump"] = c.request({"op": "guide_move", "to": 3})["guide"]["step"]
    time.sleep(0.5)
    results["finish"] = c.request({"op": "guide_move", "to": "next"})["guide"]
    time.sleep(0.3)

    go("glass")
    proc = psutil.Process(daemon_pid)
    proc.cpu_percent(None)
    res = c.request({"op": "show", "ttl_seconds": 30, "shapes": [
        {"type": "pointer", "target": BTN, "text": "This button"},
        {"type": "note", "title": "Why this button", "text": "It is the only one here.\n- Magenta on purpose\n- Easy to find in a screenshot"},
        {"type": "flow", "nodes": ["Find", "Point", "Explain"]},
        {"type": "ink", "mark": "circle", "target": BTN, "color": "yellow"},
    ]})
    assert res["ok"], res
    results["glass_errors"] = res.get("errors")
    time.sleep(1.2)
    shot("glass_1.png")
    time.sleep(4.5)
    results["cpu_percent_of_one_core_while_animating"] = round(proc.cpu_percent(None))
    results["rss_mb"] = round(proc.memory_info().rss / 1e6)
    shot("glass_2.png")
    results["layers_on"] = [l["device"] for l in status()["layers"]]

    go("clear")
    results["clear"] = c.request({"op": "clear"})
    time.sleep(0.5)
    results["after_clear"] = {"shapes": status()["visible_shapes"], "layers": status()["layers"]}
finally:
    go("stop")
    target.kill()
    try:
        c.request({"op": "stop"})
    except Exception:
        pass
    time.sleep(1.0)

results["foreground_end"] = who(u.GetForegroundWindow())
results["daemon_gone"] = not psutil.pid_exists(daemon_pid)
own = str(daemon_pid)
results["events_from_overlay"] = [e for e in events if f"pid{own}" in e["who"]]
results["foreground_changes"] = [e for e in events if e["kind"] == "FOREGROUND"]
print(json.dumps(results, indent=1))
if len(sys.argv) > 2:
    Path(sys.argv[2]).write_text(json.dumps(results, indent=1), encoding="utf-8")
