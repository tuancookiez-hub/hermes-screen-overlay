"""Ground-truth target: a framed Qt window with a solid magenta button.

Usage: python target.py X Y   (physical top-left of the window)
Prints READY {"hwnd": ...} once shown.
"""
from __future__ import annotations

import json
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

app = QApplication(sys.argv)
w = QWidget()
w.setWindowTitle("Spike Target")
w.setStyleSheet("background:#202020;")
btn = QPushButton("Spike Target Button", w)
btn.setAccessibleName("Spike Target Button")
btn.setStyleSheet("background:#FF00FF;color:#FF00FF;border:none;")
btn.setGeometry(60, 50, 220, 70)
w.resize(420, 220)
w.setAttribute(Qt.WA_ShowWithoutActivating)
w.show()
x, y = int(sys.argv[1]), int(sys.argv[2])
import ctypes  # noqa: E402

hwnd = int(w.winId())
# Move in physical pixels via Win32 so Qt scaling cannot shift it.


def ready() -> None:
    ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), x, y, 0, 0, 0x0011)  # HWND_TOPMOST, NOSIZE
    print("READY " + json.dumps({"hwnd": hwnd}), flush=True)


QTimer.singleShot(400, ready)
app.exec()
