"""Feasibility harness: Tk vs PySide6 overlay, accuracy, click-through, hotkey, routes.

Run with a Python that has PySide6, comtypes, pillow, numpy, psutil.
Writes spikes/out/results.json and quality crops.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import psutil
from PIL import Image, ImageGrab

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

common.per_monitor_aware()
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
PY = sys.executable


def spawn(args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    p = subprocess.Popen([PY, *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, env=env, creationflags=0x08000000)  # CREATE_NO_WINDOW
    t0 = time.perf_counter()
    while True:
        line = p.stdout.readline()
        if not line:
            raise RuntimeError(f"{args} died before READY")
        if line.startswith("READY "):
            return p, json.loads(line[6:]), time.perf_counter() - t0


def grab() -> np.ndarray:
    vx, vy, _, _ = common.virtual_screen()
    assert (vx, vy) == (0, 0) or True
    return np.asarray(ImageGrab.grab(all_screens=True).convert("RGB")).astype(np.int16), vx, vy


def bbox_of(img, vx, vy, pred, region):
    x, y, w, h = region
    sub = img[y - vy:y - vy + h, x - vx:x - vx + w]
    m = pred(sub)
    ys, xs = np.nonzero(m)
    if len(xs) == 0:
        return None
    return {"x": int(xs.min()) + x, "y": int(ys.min()) + y,
            "w": int(xs.max() - xs.min() + 1), "h": int(ys.max() - ys.min() + 1)}


def is_magenta(a):
    return (a[..., 0] > 230) & (a[..., 1] < 30) & (a[..., 2] > 230)


def is_lime(a):
    return (a[..., 0] < 60) & (a[..., 1] > 200) & (a[..., 2] < 60)


def uia_rect(hwnd, name):
    import comtypes.client
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as U
    uia = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
    root = uia.ElementFromHandle(hwnd)
    cond = uia.CreatePropertyCondition(U.UIA_NamePropertyId, name)
    el = root.FindFirst(U.TreeScope_Descendants, cond)
    if not el:
        return None, None
    r = el.CurrentBoundingRectangle
    rect = {"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top}
    pt = U.tagPOINT(r.left + 5, r.top + 5)
    try:
        hit = uia.ElementFromPoint(pt)
        name_hit = hit.CurrentName if hit else None
    except Exception as e:  # UIPI: point covered by an elevated window
        name_hit = f"ERROR {e}"
    return rect, name_hit


def send_hotkey():
    ki = ctypes.windll.user32.keybd_event
    for vk in (0x11, 0x12, 0x10, 0x7B):
        ki(vk, 0, 0, 0)
    for vk in (0x7B, 0x10, 0x12, 0x11):
        ki(vk, 0, 2, 0)


def edges(found, exp):
    if not found:
        return None
    return {"left": found["x"] - exp["x"], "top": found["y"] - exp["y"],
            "right": (found["x"] + found["w"]) - (exp["x"] + exp["w"]),
            "bottom": (found["y"] + found["h"]) - (exp["y"] + exp["h"])}


def expand(r, d):
    return {"x": r["x"] - d, "y": r["y"] - d, "w": r["w"] + 2 * d, "h": r["h"] + 2 * d}


def run_case(mon, mi):
    res = {"monitor": mon}
    tx, ty = mon["x"] + 300, mon["y"] + 300
    target, tinfo, _ = spawn([HERE / "target.py", tx, ty])
    try:
        time.sleep(0.4)
        hwnd = tinfo["hwnd"]
        res["target_placed_at"] = common.window_rect(hwnd)
        frame = common.frame_bounds(hwnd)
        res["win32"] = {"GetWindowRect": common.window_rect(hwnd), "DwmFrameBounds": frame}
        truth = None
        for _ in range(15):
            img, vx, vy = grab()
            truth = bbox_of(img, vx, vy, is_magenta, (frame["x"], frame["y"], frame["w"], frame["h"]))
            if truth:
                break
            time.sleep(0.3)
        if not truth:
            Image.fromarray(img.astype(np.uint8)).crop((frame["x"] - vx, frame["y"] - vy, frame["x"] - vx + frame["w"],
                                                       frame["y"] - vy + frame["h"])).save(OUT / "dbg_missing.png")
            res["error"] = "target not visible"
            return res
        res["truth_pixels"] = truth
        urect, hit = uia_rect(hwnd, "Spike Target Button")
        res["uia"] = {"rect": urect, "edge_error_px": edges(urect, truth), "element_from_point": hit}

        # vision route: downscale this monitor's shot to 1280 wide, locate, map back
        mshot = Image.fromarray(img[mon["y"] - vy:mon["y"] - vy + mon["h"],
                                    mon["x"] - vx:mon["x"] - vx + mon["w"]].astype(np.uint8))
        k = 1280 / mon["w"]
        small = np.asarray(mshot.resize((1280, round(mon["h"] * k)), Image.LANCZOS)).astype(np.int16)
        sb = bbox_of(small, 0, 0, is_magenta, (0, 0, small.shape[1], small.shape[0]))
        if sb:
            back = {"x": round(sb["x"] / k) + mon["x"], "y": round(sb["y"] / k) + mon["y"],
                    "w": round(sb["w"] / k), "h": round(sb["h"] / k)}
            res["vision_1280"] = {"scale": round(k, 4), "rect": back, "edge_error_px": edges(back, truth)}

        rect = urect or truth
        variants = [("tk", {}), ("qt", {}), ("qt_scale1.5", {"QT_SCALE_FACTOR": "1.5"})]
        for name, env in variants:
            backend = name.split("_")[0]
            ov, oinfo, startup = spawn([HERE / "overlay.py", backend, "accuracy",
                                        rect["x"], rect["y"], rect["w"], rect["h"]], env)
            time.sleep(0.5)
            r = {"startup_s": round(startup, 2), "layers": oinfo["layers"],
                 "rss_mb": round(max(c.memory_info().rss for c in [psutil.Process(ov.pid), *psutil.Process(ov.pid).children(recursive=True)]) / 2**20, 1)}
            img, vx, vy = grab()
            exp = expand(rect, 7)
            ring = bbox_of(img, vx, vy, is_lime, (exp["x"] - 30, exp["y"] - 30, exp["w"] + 60, exp["h"] + 60))
            r["ring_found"] = ring
            r["ring_edge_error_px"] = edges(ring, exp)
            # magenta must still be fully visible (overlay did not paint over the target)
            r["target_untouched"] = bbox_of(img, vx, vy, is_magenta,
                                            (frame["x"], frame["y"], frame["w"], frame["h"])) == truth
            cx, cy = rect["x"] + rect["w"] // 2, rect["y"] + rect["h"] // 2
            r["click_through_center"] = common.window_at(cx, cy) == hwnd
            r["click_through_on_ring"] = common.window_at(exp["x"] + 1, cy) == hwnd
            t0 = time.perf_counter()
            send_hotkey()
            try:
                ov.wait(3)
                r["hotkey_exit_s"] = round(time.perf_counter() - t0, 2)
            except subprocess.TimeoutExpired:
                r["hotkey_exit_s"] = None
                ov.kill()
            res[name] = r

        if mi == 0:
            for backend in ("tk", "qt"):
                ov, _, _ = spawn([HERE / "overlay.py", backend, "quality",
                                  rect["x"], rect["y"], rect["w"], rect["h"]])
                time.sleep(0.6)
                box = (rect["x"] - 60, rect["y"] - 60, rect["x"] + rect["w"] + 360, rect["y"] + rect["h"] + 220)
                ImageGrab.grab(bbox=box, all_screens=True).save(OUT / f"quality_{backend}.png")
                ov.kill()
    finally:
        target.kill()
    return res


def main():
    mons = common.monitors()
    results = {"monitors": mons, "virtual_screen": common.virtual_screen(), "cases": []}
    only = os.environ.get("SPIKE_MONITORS")
    picks = [int(v) for v in only.split(",")] if only else range(len(mons))
    for n, i in enumerate(picks):
        results["cases"].append(run_case(mons[i], n))
    (OUT / "results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
