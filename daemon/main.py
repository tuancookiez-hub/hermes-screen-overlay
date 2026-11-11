"""Screen-overlay daemon. Started by the plugin; talks over a local named pipe.

    python daemon/main.py --data-dir <plugin-data dir> --pipe <name> --settings <json>

The pipe needs the authkey stored in <data-dir>/ipc.key. Nothing here touches the network.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

import shapes as S  # noqa: E402
import winapi  # noqa: E402

VERSION = "0.3.0"
log = logging.getLogger("screen-overlay")


# --------------------------------------------------------------------------- qt bridge
class QtBridge:
    """Run callables on the Qt thread and wait for their result."""

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()

    def call(self, fn, timeout: float = 5.0):
        done = threading.Event()
        box: dict = {}
        self.q.put((fn, done, box))
        if not done.wait(timeout):
            box["cancelled"] = True  # never apply a change the caller was already told had failed
            raise TimeoutError("overlay UI thread did not respond")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def drain(self) -> None:
        while True:
            try:
                fn, done, box = self.q.get_nowait()
            except queue.Empty:
                return
            if box.get("cancelled"):
                continue
            try:
                box["value"] = fn()
            except Exception as exc:  # surfaced to the caller
                log.exception("qt call failed")
                box["error"] = exc
            done.set()


# --------------------------------------------------------------------------- requests
class Handler:
    def __init__(self, bridge: QtBridge, overlay_ref: dict, settings: dict, hotkey_ok: bool):
        self.bridge = bridge
        self.ref = overlay_ref
        self.settings = settings
        self.hotkey_ok = hotkey_ok
        self._tls = threading.local()
        self.stop_requested = 0.0
        self.wake = threading.Event()

    @property
    def uia(self):
        """One UI Automation client per thread (IPC thread and anchor thread)."""
        if getattr(self._tls, "uia", None) is None:
            from uia import UIA
            self._tls.uia = UIA()
        return self._tls.uia

    def handle(self, req: dict) -> dict:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "pid": os.getpid(), "version": VERSION, "hotkey": self.hotkey_ok}
        if op == "settings":
            self.settings.update(req.get("settings") or {})
            return {"ok": True}
        if op == "status":
            st = self.bridge.call(lambda: self.ref["overlay"].status())
            return {"ok": True, "pid": os.getpid(), "version": VERSION,
                    "emergency_hotkey": winapi.HOTKEY_LABEL if self.hotkey_ok else None, **st}
        if op == "inspect":
            return self.inspect(req)
        if op == "show":
            return self.show(req)
        if op == "guide":
            return self.guide(req)
        if op == "guide_move":
            to = req.get("to")
            if isinstance(to, str) and to.strip().isdigit():
                to = int(to.strip())
            to = to - 1 if isinstance(to, int) and not isinstance(to, bool) else to  # steps are 1-based outside
            return self.bridge.call(lambda: self.ref["overlay"].guide_move(to))
        if op == "sticky":
            return self.sticky(req)
        if op == "stickies":
            return {"ok": True, "stickies": self.bridge.call(lambda: self.ref["overlay"].sticky_list())}
        if op == "clear":
            ids, owner = req.get("ids") or None, req.get("owner") or None
            stickies = req.get("stickies") is True
            return {"ok": True, **self.bridge.call(lambda: self.ref["overlay"].clear(ids, owner, stickies))}
        if op == "stop":
            self.stop_requested = time.monotonic()
            return {"ok": True, "stopped": True}
        return {"ok": False, "error": f"unknown op {op!r}"}

    # ------------------------------------------------------------------ inspect
    def inspect(self, req: dict) -> dict:
        what = req.get("what", "screens")
        own = os.getpid()
        if what == "screens":
            return {"ok": True, "monitors": winapi.monitors(), "virtual_screen": winapi.virtual_screen(),
                    "note": "All coordinates are physical pixels on the Windows virtual screen."}
        if what == "windows":
            q = (req.get("window") or "")
            wins = winapi.top_level_windows(skip_pid=own)
            if q:
                ql = str(q).lower()
                wins = [w for w in wins if ql in w["title"].lower() or ql in w["process"].lower()]
            return {"ok": True, "windows": wins[: max(1, min(int(req.get("limit") or 25), 200))]}
        if what == "element_at":
            x, y = req.get("x"), req.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                return {"ok": False, "error": "element_at needs numeric x and y (physical pixels)"}
            win = winapi.window_at(int(x), int(y))
            out = {"ok": True, "point": [int(x), int(y)]}
            if win:
                out["window"] = {"hwnd": win, "title": winapi.window_title(win),
                                 "process": winapi.process_name(winapi.window_pid(win)),
                                 "rect": winapi.frame_bounds(win)}
            try:
                out["element"] = self.uia.element_at(int(x), int(y))
            except Exception as exc:
                out["element_error"] = str(exc)
            return out
        if what == "find":
            w = self._window(req.get("window"))
            if "error" in w:
                return {"ok": False, **w}
            try:
                found = self.uia.find(w["hwnd"], req.get("name"), req.get("control_type"),
                                      req.get("automation_id"), limit=max(1, min(int(req.get("limit") or 15), 100)))
            except Exception as exc:
                return {"ok": False, "error": str(exc), "window": w}
            return {"ok": True, "window": w, "elements": found,
                    "hint": None if found else "nothing matched; try a shorter name, or omit control_type"}
        return {"ok": False, "error": "inspect.what must be screens, windows, element_at or find"}

    def _window(self, ref) -> dict:
        if ref in (None, ""):
            fg = winapi.foreground()
            if not fg:
                return {"error": "no window given and no foreground window"}
            w = winapi.find_window(int(fg), skip_pid=os.getpid())
            return w or {"error": "the foreground window is not a normal app window; pass window="}
        w = winapi.find_window(ref, skip_pid=os.getpid())
        if not w:
            return {"error": f"no visible window matches {ref!r}; call inspect(what='windows') to list them"}
        return w

    # ------------------------------------------------------------------ show
    def resolve(self, target: dict | None, space: dict | None, monitors: list, virtual: list) -> dict:
        if target is None:  # a note or flow with no anchor; show() picks its monitor
            return {"rect": [0, 0, 0, 0], "is_point": False, "free": True}
        if "rect" in target or "point" in target:
            region = S.frame_region(space, monitors, virtual) if space else None
            if "rect" in target:
                r = target["rect"]
                rect = S.map_rect_from_frame(r, space, region) if space else tuple(round(v) for v in r)
                _in_range(rect)
                return {"rect": list(rect), "is_point": rect[2] == 0 and rect[3] == 0}
            x, y = target["point"]
            if space:
                x, y = S.map_from_frame(x, y, space, region)
            _in_range((x, y))
            return {"rect": [round(x), round(y), 0, 0], "is_point": True}
        if "window" in target:
            w = self._window(target["window"])
            if "error" in w:
                raise S.ShapeError(w["error"])
            if w["minimized"]:
                raise S.ShapeError(f"window {w['title']!r} is minimized")
            return {"rect": w["rect"], "is_point": False, "resolved": {"window": w["title"], "hwnd": w["hwnd"]}}
        el = target["element"]
        w = self._window(el.get("window"))
        if "error" in w:
            raise S.ShapeError(w["error"])
        if w["minimized"]:
            raise S.ShapeError(f"window {w['title']!r} is minimized")
        found = self.uia.find(w["hwnd"], el.get("name"), el.get("control_type"), el.get("automation_id"),
                              limit=el["index"] + 1)
        if len(found) <= el["index"]:
            loose = []
            if el.get("name") and el.get("control_type"):
                loose = [f"{d['control_type']}:{d['name']}" for d in
                         self.uia.find(w["hwnd"], el.get("name"), None, None, limit=6)]
            msg = f"no visible element matches {el} in {w['title']!r}"
            if loose:
                msg += f"; similar: {loose}"
            raise S.ShapeError(msg + ". Use inspect(what='find') to see what is there.")
        d = found[el["index"]]
        return {"rect": d["rect"], "is_point": False,
                "resolved": {"window": w["title"], "element": f"{d['control_type']}:{d['name']}"}}

    def show(self, req: dict) -> dict:
        try:
            items = S.normalize_shapes(req.get("shapes"))
            space = S.normalize_space(req.get("space"))
        except S.ShapeError as exc:
            return {"ok": False, "error": str(exc)}
        monitors, virtual = winapi.monitors(), winapi.virtual_screen()
        if not monitors:
            return {"ok": False, "error": "no monitor is attached right now"}
        resolved, errors, notes = [], [], []
        for it in items:
            try:
                r = self.resolve(it["target"], space, monitors, virtual)
            except Exception as exc:
                errors.append({"id": it["id"], "error": str(exc)})
                continue
            if it.get("from") and space:
                region = S.frame_region(space, monitors, virtual)
                it["from"] = list(S.map_from_frame(it["from"][0], it["from"][1], space, region))
            rect = tuple(r["rect"])
            probe = rect if rect[2] and rect[3] else (rect[0], rect[1], 1, 1)
            if not r.get("free") and not any(S.intersects(probe, tuple(m["rect"])) for m in monitors):
                errors.append({"id": it["id"], "error": f"target {list(rect)} is off every monitor"})
                continue
            if r.get("resolved"):
                notes.append({"id": it["id"], **r["resolved"]})
            resolved.append({**it, "rect": list(rect), "is_point": r["is_point"], "free": r.get("free", False),
                             "owner": req.get("owner")})
        if not resolved:
            return {"ok": False, "error": "nothing could be drawn", "errors": errors}
        anchor = next((tuple(it["rect"]) for it in resolved if not it["free"]), None)
        for it in resolved:
            if it["free"]:
                mon = S.free_monitor(monitors, winapi.cursor_pos(), winapi.foreground_rect(), anchor)
                it["rect"] = list(mon["work"])
        ttl = req.get("ttl_seconds")
        ttl = float(self.settings.get("default_ttl_seconds", 300)) if ttl is None else float(ttl)
        replace = req.get("replace", True) is not False
        out = self.bridge.call(lambda: self.ref["overlay"].show(resolved, replace, max(0.0, min(ttl, 3600))))
        res = {"ok": True, **out, "expires_in_s": ttl or None,
               "emergency_hotkey": winapi.HOTKEY_LABEL if self.hotkey_ok else None}
        if notes:
            res["resolved"] = notes
        if errors:
            res["errors"] = errors
        if self.settings.get("exclude_from_capture", True):
            res["capture_note"] = "Overlay is hidden from screenshots, so your own captures will not show it."
        return res


    # ------------------------------------------------------------------ sticky notes
    def sticky(self, req: dict) -> dict:
        try:
            notes = S.normalize_stickies(req.get("notes"))
            space = S.normalize_space(req.get("space"))
        except S.ShapeError as exc:
            return {"ok": False, "error": str(exc)}
        monitors, virtual = winapi.monitors(), winapi.virtual_screen()
        if not monitors:
            return {"ok": False, "error": "no monitor is attached right now"}
        errors = []
        for n in notes:
            n["rect"] = None
            if n["target"] is None:
                continue
            try:
                n["rect"] = self.resolve(n["target"], space, monitors, virtual)["rect"]
            except Exception as exc:  # the note still appears, just not beside its target
                errors.append({"id": n["id"], "error": str(exc)})
        owner = req.get("owner")
        out = self.bridge.call(lambda: self.ref["overlay"].sticky_show(notes, owner))
        if errors and out.get("ok"):
            out["errors"] = errors
            out["errors_note"] = "These notes were placed in a free corner because their target was not found."
        return out

    # ------------------------------------------------------------------ guide
    def _pin(self, target: dict | None) -> dict | None:
        """A step with no window means "the app in front now", not whatever is in front later."""
        if not target or "element" not in target or target["element"].get("window") not in (None, ""):
            return target
        w = self._window(None)
        return {"element": {**target["element"], "window": w["hwnd"]}} if "hwnd" in w else target

    def guide(self, req: dict) -> dict:
        try:
            g = S.normalize_guide(req.get("steps"), req.get("title"))
            space = S.normalize_space(req.get("space"))
        except S.ShapeError as exc:
            return {"ok": False, "error": str(exc)}
        g["space"] = space
        for st in g["steps"]:
            st["target"] = self._pin(st["target"])
        ttl = req.get("ttl_seconds")
        ttl = render_ttl() if ttl is None else max(0.0, min(float(ttl), 14_400))
        pointer = req.get("pointer", True) is not False
        monitors, virtual = winapi.monitors(), winapi.virtual_screen()
        if not monitors:
            return {"ok": False, "error": "no monitor is attached right now"}
        checks, deadline = [], time.monotonic() + 6
        for i, st in enumerate(g["steps"]):  # later steps may only exist after earlier ones, so this only warns
            if st["target"] is None:
                continue
            if time.monotonic() > deadline:
                break
            try:
                self.resolve(st["target"], space, monitors, virtual)
            except Exception as exc:
                checks.append({"step": i + 1, "not_on_screen_yet": str(exc)[:200]})
        status = self.bridge.call(lambda: self.ref["overlay"].start_guide(g, req.get("owner"), ttl, pointer))
        self.wake.set()
        res = {"ok": True, "guide": status, "emergency_hotkey": winapi.HOTKEY_LABEL if self.hotkey_ok else None}
        if checks:
            res["checks"] = checks
            res["checks_note"] = ("These targets were not found now. That is fine if an earlier step opens them; "
                                  "the guide keeps looking when it reaches them.")
        return res

    def anchor_loop(self) -> None:
        """Follows the current guide step: finds it when it appears and tracks it as its window moves.

        Runs on its own thread because a UI Automation call can take seconds and the UI must not wait.
        """
        misses, last_gen = 0, 0
        while True:
            self.wake.wait(1.0)
            self.wake.clear()
            try:
                if self.ref["overlay"].guide is None:  # plain attribute read; skips a Qt round trip when idle
                    continue
                snap = self.bridge.call(lambda: self.ref["overlay"].guide_snapshot())
                if not snap or not snap["target"]:
                    continue
                static = "rect" in snap["target"] or "point" in snap["target"]
                if static and snap["found"]:
                    continue
                if snap["gen"] != last_gen:
                    misses, last_gen = 0, snap["gen"]
                began = time.monotonic()
                try:
                    r = self.resolve(snap["target"], snap["space"], winapi.monitors(), winapi.virtual_screen())
                    rect, is_point = r["rect"], r["is_point"]
                    misses = 0
                except Exception as exc:
                    log.debug("guide target not found: %s", exc)
                    misses += 1
                    if snap["found"] and misses < 3:  # one slow answer must not make the mark blink
                        continue
                    rect, is_point = None, False
                gen = snap["gen"]
                self.bridge.call(lambda: self.ref["overlay"].guide_anchor(gen, rect, is_point))
                time.sleep(min(3.0, (time.monotonic() - began) * 3))  # back off on an app that answers slowly
            except Exception:
                log.exception("anchor loop")
                time.sleep(1.0)


def _in_range(vals) -> None:
    if any(abs(v) > S.MAX_COORD for v in vals):
        raise S.ShapeError("target is far outside any screen; check `space` (frame_width/frame_height)")


def render_ttl() -> float:
    import render
    return float(render.GUIDE_TTL_S)


# --------------------------------------------------------------------------- ipc
def _serve_one(conn, handler: Handler) -> None:
    try:
        if conn.poll(5):
            req = conn.recv()
            try:
                resp = handler.handle(req if isinstance(req, dict) else {})
            except Exception as exc:
                log.exception("request failed")
                resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            conn.send(resp)
    except Exception as exc:
        log.warning("connection error: %s", exc)
    finally:
        conn.close()


def serve(pipe: str, key: bytes, handler: Handler) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from multiprocessing.connection import Listener

    try:
        listener = Listener(address=pipe, family="AF_PIPE", authkey=key)
    except Exception:
        log.exception("cannot open the pipe; exiting so the next call can start a clean daemon")
        os._exit(1)
    # A pool, so a slow request (UI Automation can take seconds) never makes a ping time out. Each pool
    # thread keeps its own UI Automation client.
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="overlay-ipc-req")
    while True:
        try:
            conn = listener.accept()
        except Exception as exc:  # bad authkey, client vanished
            log.warning("rejected connection: %s", exc)
            time.sleep(0.05)
            continue
        pool.submit(_serve_one, conn, handler)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--pipe", required=True)
    ap.add_argument("--mutex", required=True)
    ap.add_argument("--settings", default="{}")
    args = ap.parse_args()
    data = Path(args.data_dir)
    handler_ = logging.handlers.RotatingFileHandler(data / "daemon.log", maxBytes=256_000, backupCount=1,
                                                    encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler_],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not winapi.single_instance(args.mutex):
        log.info("another overlay daemon is running; exiting")
        return 0
    key = bytes.fromhex((data / "ipc.key").read_text(encoding="ascii").strip())
    settings = json.loads(args.settings)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    import render

    app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)
    fired = threading.Event()
    hk = winapi.start_hotkey(fired.set)
    hotkey_ok = bool(getattr(hk, "ok", False))
    if not hotkey_ok:
        log.warning("emergency hotkey %s is taken by another app", winapi.HOTKEY_LABEL)
    overlay = render.Overlay(app, settings, hotkey_ok)
    bridge = QtBridge()
    handler = Handler(bridge, {"overlay": overlay}, settings, hotkey_ok)
    overlay.on_guide_change = handler.wake.set
    threading.Thread(target=serve, args=(args.pipe, key, handler), name="overlay-ipc", daemon=True).start()
    threading.Thread(target=handler.anchor_loop, name="overlay-anchor", daemon=True).start()
    (data / "daemon.pid").write_text(str(os.getpid()), encoding="ascii")

    idle_limit = max(60.0, float(settings.get("idle_exit_minutes", 10)) * 60)

    def pump() -> None:
        bridge.drain()
        if fired.is_set() or (handler.stop_requested and time.monotonic() - handler.stop_requested > 0.3):
            log.info("emergency hotkey or stop: clearing and exiting")
            try:
                overlay.clear(stickies=True)
            except Exception:
                log.exception("clear failed while exiting")
            finally:
                app.quit()  # must happen even if clearing failed
        elif overlay.idle_seconds() > idle_limit:
            log.info("idle for %.0fs: exiting", overlay.idle_seconds())
            app.quit()

    t = QTimer()
    t.timeout.connect(pump)
    t.start(30)
    log.info("daemon %s up (pid %s, hotkey %s)", VERSION, os.getpid(), hotkey_ok)
    app.exec()
    try:
        (data / "daemon.pid").unlink()
    except OSError:
        pass
    os._exit(0)  # the IPC thread is blocked in accept(); do not wait for it


if __name__ == "__main__":
    sys.exit(main())
