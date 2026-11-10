"""Live end-to-end check through the real tool handler and daemon (dev only).

    SPIKE_MONITOR=1 python spikes/e2e.py
Needs PySide6 + comtypes + pillow + numpy. Draws on screen for a few seconds.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["SCREEN_OVERLAY_DATA_DIR"] = tempfile.mkdtemp(prefix="so-e2e-")
spec = importlib.util.spec_from_file_location("screen_overlay", ROOT / "__init__.py",
                                              submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec)
sys.modules["screen_overlay"] = pkg
spec.loader.exec_module(pkg)
sys.path.insert(0, str(ROOT / "spikes"))
import common  # noqa: E402
import run as harness  # noqa: E402  (spawn, grab, bbox helpers)

OUT = ROOT / "spikes" / "out"
SETTINGS = {"exclude_from_capture": False, "default_ttl_seconds": 60, "idle_exit_minutes": 2}
handle = pkg.make_handler(lambda: SETTINGS)


def call(**args):
    t0 = time.perf_counter()
    res = json.loads(handle(args))
    res["_ms"] = round((time.perf_counter() - t0) * 1000)
    return res


def main():
    report = {}
    mons = common.monitors()
    mi = int(os.environ.get("SPIKE_MONITOR", "1"))
    mon = mons[mi]
    target, tinfo, _ = harness.spawn([ROOT / "spikes" / "target.py", mon["x"] + 300, mon["y"] + 300])
    try:
        time.sleep(0.6)
        img, vx, vy = harness.grab()
        f = common.frame_bounds(tinfo["hwnd"])
        truth = harness.bbox_of(img, vx, vy, harness.is_magenta, (f["x"], f["y"], f["w"], f["h"]))
        report["truth"] = truth

        report["status_before"] = call(action="status")
        r = call(action="inspect", what="screens")
        report["screens"] = {"ok": r["ok"], "n": len(r.get("monitors", [])), "ms": r["_ms"]}
        r = call(action="inspect", what="windows", window="Spike Target")
        report["windows"] = {"ok": r["ok"], "hits": [w["title"] for w in r.get("windows", [])], "ms": r["_ms"]}
        r = call(action="inspect", what="find", window="Spike Target", name="target button")
        report["find"] = {"ok": r["ok"], "elements": [(e["control_type"], e["name"], e["rect"])
                                                    for e in r.get("elements", [])], "ms": r["_ms"]}
        r = call(action="inspect", what="element_at", x=truth["x"] + 10, y=truth["y"] + 10)
        report["element_at"] = {"ok": r["ok"], "element": (r.get("element") or {}).get("name"),
                                "window": (r.get("window") or {}).get("title"), "err": r.get("element_error")}

        # pixel accuracy: box with pad 4 => 3px ring centred on the inflated rect
        r = call(action="show", shapes=[{"type": "box", "color": "#00FF00", "pad": 8,
                                         "target": {"element": {"window": "Spike Target", "name": "Spike Target Button",
                                                                "control_type": "Button"}}}])
        report["show_box"] = {k: r.get(k) for k in ("ok", "shapes", "resolved", "errors", "_ms")}
        time.sleep(0.5)
        img, vx, vy = harness.grab()
        ring = harness.bbox_of(img, vx, vy, harness.is_lime,
                               (truth["x"] - 40, truth["y"] - 40, truth["w"] + 80, truth["h"] + 80))
        exp = {"x": truth["x"] - 8, "y": truth["y"] - 8, "w": truth["w"] + 16, "h": truth["h"] + 16}
        report["ring_vs_expected_centreline"] = {"ring_bbox": ring, "expected_centreline": exp}
        cx, cy = truth["x"] + truth["w"] // 2, truth["y"] + truth["h"] // 2
        report["click_through"] = common.window_at(cx, cy) == tinfo["hwnd"]

        # full composition for a visual check
        r = call(action="show", ttl_seconds=30, shapes=[
            {"type": "spotlight", "target": {"element": {"window": "Spike Target", "name": "Spike Target Button"}}},
            {"type": "step", "target": {"element": {"window": "Spike Target", "name": "Spike Target Button"}},
             "text": "Click the magenta button to continue"},
            {"type": "arrow", "target": {"window": "Spike Target"}, "text": "This is the target app",
             "color": "blue"},
            {"type": "label", "target": {"point": [mon["x"] + 60, mon["y"] + 60]}, "text": "Free label at a point",
             "color": "teal"},
        ])
        report["show_full"] = {k: r.get(k) for k in ("ok", "shapes", "errors", "_ms")}
        time.sleep(0.6)
        from PIL import ImageGrab
        ImageGrab.grab(bbox=(mon["x"], mon["y"], mon["x"] + mon["w"], mon["y"] + mon["h"]),
                       all_screens=True).save(OUT / "e2e_full.png")
        report["status_during"] = {k: v for k, v in call(action="status").items() if k in
                                   ("visible_shapes", "expires_in_s", "layers", "emergency_hotkey")}

        # bad input is reported, not raised
        report["bad_input"] = call(action="show", shapes=[{"type": "box", "target": {"element": {"window": "Spike Target",
                                                                                                "name": "nope-xyz"}}}])
        report["clear"] = call(action="clear")

        # emergency hotkey kills the daemon
        call(action="show", shapes=[{"type": "box", "target": {"window": "Spike Target"}}])
        time.sleep(0.4)
        harness.send_hotkey()
        time.sleep(1.0)
        report["after_hotkey_status"] = call(action="status")
        report["restart_after_hotkey"] = call(action="inspect", what="screens")["ok"]
        report["stop"] = call(action="stop")
    finally:
        target.kill()
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
