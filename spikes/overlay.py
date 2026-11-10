"""Spike overlay in Tk or PySide6. Coordinates in are PHYSICAL virtual-screen px.

Usage: python overlay.py qt|tk accuracy|quality X Y W H
Prints READY {"hwnds": [...]} once drawn. Ctrl+Alt+Shift+F12 (emergency) quits.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

BACKEND, MODE = sys.argv[1], sys.argv[2]
RX, RY, RW, RH = (int(v) for v in sys.argv[3:7])
LIME, ORANGE, WHITE = "#00FF00", "#FF9F0A", "#FFFFFF"
HOTKEY_ID, MOD = 0xB001, 0x0001 | 0x0002 | 0x0004 | 0x4000  # ALT|CTRL|SHIFT|NOREPEAT
VK_F12 = 0x7B


def start_hotkey(on_fire) -> None:
    """Emergency hotkey on its own thread + message loop (backend-agnostic)."""
    def run() -> None:
        if not common.user32.RegisterHotKey(None, HOTKEY_ID, MOD, VK_F12):
            print("HOTKEY_FAIL", flush=True)
            return
        msg = wintypes.MSG()
        while common.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and msg.wParam == HOTKEY_ID:  # WM_HOTKEY
                on_fire()
                return
    threading.Thread(target=run, daemon=True).start()


def geometry():
    """Shape geometry in physical px, shared by both backends."""
    pad = 4 if MODE == "accuracy" else 8
    box = (RX - pad, RY - pad, RW + 2 * pad, RH + 2 * pad)
    tail = (RX + RW + 170, RY + RH + 110)
    head = (RX + RW + 10, RY + RH + 10)
    return box, tail, head


# ----------------------------------------------------------------- PySide6
def run_qt() -> None:
    from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
    from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication(sys.argv)
    mons = {m["device"]: m for m in common.monitors()}
    box, tail, head = geometry()

    class Layer(QWidget):
        def __init__(self, screen, mon):
            super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                             | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            self.mon = mon
            self.dpr = screen.devicePixelRatio()
            self.setScreen(screen)
            self.setGeometry(screen.geometry())

        def L(self, x, y):  # physical -> this layer's logical coords
            return QPointF((x - self.mon["x"]) / self.dpr, (y - self.mon["y"]) / self.dpr)

        def R(self, r):
            p = self.L(r[0], r[1])
            return QRectF(p.x(), p.y(), r[2] / self.dpr, r[3] / self.dpr)

        def paintEvent(self, _e):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, MODE == "quality")
            if MODE == "accuracy":
                pen = QPen(QColor(LIME), 3 / self.dpr)
                pen.setJoinStyle(Qt.MiterJoin)
                p.setPen(pen)
                r = self.R(box)
                h = 1.5 / self.dpr  # stroke centred inside the outer edge
                p.drawRect(r.adjusted(-h, -h, h, h))
                return
            hole = QPainterPath()
            hole.addRoundedRect(self.R(box), 10, 10)
            full = QPainterPath()
            full.addRect(QRectF(self.rect()))
            p.fillPath(full.subtracted(hole), QColor(0, 0, 0, 140))
            p.setPen(QPen(QColor(ORANGE), 3))
            p.drawPath(hole)
            # arrow
            a, b = self.L(*tail), self.L(*head)
            p.setPen(QPen(QColor(ORANGE), 4, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(a, b)
            import math
            ang = math.atan2(b.y() - a.y(), b.x() - a.x())
            tri = QPolygonF([b, QPointF(b.x() - 16 * math.cos(ang - 0.45), b.y() - 16 * math.sin(ang - 0.45)),
                             QPointF(b.x() - 16 * math.cos(ang + 0.45), b.y() - 16 * math.sin(ang + 0.45))])
            p.setBrush(QColor(ORANGE)); p.setPen(Qt.NoPen); p.drawPolygon(tri)
            # step badge
            c = self.L(box[0], box[1])
            p.setBrush(QColor(ORANGE)); p.drawEllipse(c, 16, 16)
            p.setPen(QColor("#111")); p.setFont(QFont("Segoe UI", 11, QFont.Bold))
            p.drawText(QRectF(c.x() - 16, c.y() - 16, 32, 32), Qt.AlignCenter, "1")
            # label pill
            p.setFont(QFont("Segoe UI", 11, QFont.DemiBold))
            text = "Click Save to continue"
            tw = p.fontMetrics().horizontalAdvance(text) + 28
            pill = QRectF(a.x() - 10, a.y() + 6, tw, 34)
            p.setPen(Qt.NoPen); p.setBrush(QColor(20, 20, 24, 235)); p.drawRoundedRect(pill, 17, 17)
            p.setPen(QColor(WHITE)); p.drawText(pill, Qt.AlignCenter, text)

    layers = []
    for s in app.screens():
        info = common.MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        common.user32.GetMonitorInfoW(ctypes.c_void_p(int(s.nativeInterface().handle())), ctypes.byref(info))
        mon = mons.get(info.szDevice)
        if not mon:
            continue
        lay = Layer(s, mon)
        lay.show()
        common.make_click_through(int(lay.winId()))
        layers.append(lay)

    fired = threading.Event()
    start_hotkey(fired.set)

    def ready():
        info = [{"hwnd": int(l.winId()), "dpr": l.dpr, "device": l.mon["device"],
                 "exstyle": hex(common.exstyle(int(l.winId())))} for l in layers]
        print("READY " + json.dumps({"layers": info}), flush=True)
    QTimer.singleShot(300, ready)
    # keep python responsive to the hotkey thread
    t = QTimer(); t.start(50); t.timeout.connect(lambda: fired.is_set() and app.quit())
    app.exec()


# ----------------------------------------------------------------- Tk
def run_tk() -> None:
    import tkinter as tk

    common.per_monitor_aware()
    vx, vy, vw, vh = common.virtual_screen()
    key = "#010203"
    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    root.attributes("-topmost", True)
    root.configure(bg=key)
    root.attributes("-transparentcolor", key)
    cv = tk.Canvas(root, bg=key, highlightthickness=0, bd=0)
    cv.pack(fill="both", expand=True)
    box, tail, head = geometry()

    def L(x, y):
        return x - vx, y - vy

    x0, y0 = L(box[0], box[1])
    x1, y1 = x0 + box[2], y0 + box[3]
    if MODE == "accuracy":
        # 3px stroke whose outer edge is box-3 .. inner edge box
        cv.create_rectangle(x0 - 2, y0 - 2, x1 + 1, y1 + 1, outline=LIME, width=3)
    else:
        # Tk has no per-pixel alpha: best available dim is a 50% stipple.
        for r in [(0, 0, vw, y0), (0, y1, vw, vh), (0, y0, x0, y1), (x1, y0, vw, y1)]:
            cv.create_rectangle(*r, fill="black", outline="", stipple="gray50")
        cv.create_rectangle(x0, y0, x1, y1, outline=ORANGE, width=3)
        a, b = L(*tail), L(*head)
        cv.create_line(*a, *b, fill=ORANGE, width=4, arrow="last", arrowshape=(16, 18, 7), capstyle="round")
        cv.create_oval(x0 - 16, y0 - 16, x0 + 16, y0 + 16, fill=ORANGE, outline="")
        cv.create_text(x0, y0, text="1", fill="#111111", font=("Segoe UI", 11, "bold"))
        tid = cv.create_text(a[0] + 4, a[1] + 23, text="Click Save to continue", fill=WHITE,
                             font=("Segoe UI", 11, "bold"), anchor="w")
        bx = cv.bbox(tid)
        rid = cv.create_rectangle(bx[0] - 14, bx[1] - 8, bx[2] + 14, bx[3] + 8, fill="#141418", outline="")
        cv.tag_lower(rid, tid)

    root.update_idletasks()
    root.update()
    hwnd = int(root.wm_frame(), 16)
    common.make_click_through(hwnd)
    fired = threading.Event()
    start_hotkey(fired.set)
    print("READY " + json.dumps({"layers": [{"hwnd": hwnd, "dpr": 1.0, "exstyle": hex(common.exstyle(hwnd))}]}),
          flush=True)

    def tick():
        if fired.is_set():
            root.destroy()
            return
        root.after(50, tick)
    tick()
    root.mainloop()


if __name__ == "__main__":
    run_qt() if BACKEND == "qt" else run_tk()
