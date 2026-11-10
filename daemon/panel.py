"""The floating step guide: the one overlay window that takes clicks.

It accepts mouse clicks without ever activating, so whatever the user is typing in keeps focus.
All drawing here is in Qt logical pixels; Qt scales the window for the monitor it is on.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

import winapi

W = 340.0
PAD = 18.0
BTN_H = 34.0
BG = QColor(22, 22, 26, 246)
EDGE = QColor(255, 255, 255, 46)
MUTED = QColor(255, 255, 255, 150)
TEXT = QColor(255, 255, 255)
BTN = QColor(255, 255, 255, 26)
BTN_HOVER = QColor(255, 255, 255, 52)


def _font(px: float, weight=QFont.Normal) -> QFont:
    f = QFont("Segoe UI")
    f.setPixelSize(round(px))
    f.setWeight(weight)
    return f


def _height(text: str, font: QFont, width: float) -> float:
    if not text:
        return 0.0
    return QFontMetricsF(font).boundingRect(QRectF(0, 0, width, 10_000), Qt.TextWordWrap, text).height()


class Panel(QWidget):
    def __init__(self, on_back: Callable[[], None], on_next: Callable[[], None], on_close: Callable[[], None],
                 on_dragged: Callable[[], None]):
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                 | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)
        self.setWindowTitle("Hermes Step Guide")
        self.actions_ = {"back": on_back, "next": on_next, "close": on_close}
        self.on_dragged = on_dragged
        self.c = {"guide": "", "index": 0, "total": 1, "title": "", "text": "", "note": "", "accent": "#FF9F0A"}
        self.hover: str | None = None
        self.pressed: str | None = None
        self.drag_from: tuple[int, int] | None = None
        self.dragging = False
        winapi.make_no_activate(self.hwnd())

    def hwnd(self) -> int:
        return int(self.winId())

    # ------------------------------------------------------------------ content
    def set_content(self, **content) -> None:
        self.c.update(content)
        self.setFixedSize(self.sizeHint())
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        inner = W - 2 * PAD
        h = PAD + 18 + 8
        h += self._title_h(inner)
        if self.c["text"]:
            h += 6 + _height(self.c["text"], _font(14), inner)
        if self.c["note"]:
            h += 8 + _height(self.c["note"], _font(12.5), inner)
        return QSize(int(W), int(h + 14 + 4 + 14 + BTN_H + PAD + 1))

    def _title_h(self, inner: float) -> float:
        return _height(self.c["title"], _font(17, QFont.DemiBold), inner)

    def _buttons(self) -> dict[str, QRectF]:
        h = float(self.height())
        y = h - PAD - BTN_H
        nw = 96.0
        out = {"next": QRectF(W - PAD - nw, y, nw, BTN_H)}
        if self.c["index"] > 0:
            out["back"] = QRectF(W - PAD - nw - 8 - 76, y, 76, BTN_H)
        out["close"] = QRectF(W - 10 - 26, 10, 26, 26)
        return out

    def _hit(self, pos: QPointF) -> str | None:
        return next((k for k, r in self._buttons().items() if r.contains(pos)), None)

    # ------------------------------------------------------------------ paint
    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        card = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(EDGE, 1))
        p.setBrush(BG)
        p.drawRoundedRect(card, 14, 14)
        accent = QColor(self.c["accent"])
        inner = W - 2 * PAD
        total, idx = max(1, self.c["total"]), self.c["index"]

        p.setFont(_font(12, QFont.DemiBold))
        p.setPen(MUTED)
        head = f"Step {idx + 1} of {total}"
        if self.c["guide"]:
            head = f"{self.c['guide']}  ·  {head}"
        head = QFontMetricsF(p.font()).elidedText(head, Qt.ElideRight, inner - 34)
        p.drawText(QRectF(PAD, PAD - 2, inner - 34, 18), Qt.AlignLeft | Qt.AlignVCenter, head)

        y = PAD + 18 + 8
        tf = _font(17, QFont.DemiBold)
        th = self._title_h(inner)
        p.setFont(tf)
        p.setPen(TEXT)
        p.drawText(QRectF(PAD, y, inner, th), Qt.TextWordWrap, self.c["title"])
        y += th
        if self.c["text"]:
            f = _font(14)
            h = _height(self.c["text"], f, inner)
            y += 6
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 215))
            p.drawText(QRectF(PAD, y, inner, h), Qt.TextWordWrap, self.c["text"])
            y += h
        if self.c["note"]:
            f = _font(12.5)
            h = _height(self.c["note"], f, inner)
            y += 8
            p.setFont(f)
            p.setPen(accent)
            p.drawText(QRectF(PAD, y, inner, h), Qt.TextWordWrap, self.c["note"])
            y += h

        bar_y = self.height() - PAD - BTN_H - 14 - 4
        track = QRectF(PAD, bar_y, inner, 4)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 34))
        p.drawRoundedRect(track, 2, 2)
        p.setBrush(accent)
        p.drawRoundedRect(QRectF(PAD, bar_y, inner * (idx + 1) / total, 4), 2, 2)

        last = idx >= total - 1
        for key, r in self._buttons().items():
            hot = self.hover == key
            if key == "close":
                if hot:
                    p.setPen(Qt.NoPen)
                    p.setBrush(BTN_HOVER)
                    p.drawEllipse(r)
                self._cross(p, r)
                continue
            p.setPen(Qt.NoPen)
            if key == "next":
                col = QColor(accent)
                p.setBrush(col.lighter(112) if hot else col)
                lum = 0.2126 * col.redF() + 0.7152 * col.greenF() + 0.0722 * col.blueF()
                ink = QColor("#111111") if lum > 0.55 else TEXT
                label = "Finish" if last else "Next"
            else:
                p.setBrush(BTN_HOVER if hot else BTN)
                ink, label = TEXT, "Back"
            p.drawRoundedRect(r, 9, 9)
            p.setPen(ink)
            p.setFont(_font(14, QFont.DemiBold))
            p.drawText(r, Qt.AlignCenter, label)
        p.end()

    @staticmethod
    def _cross(p: QPainter, r: QRectF) -> None:
        c, d = r.center(), 4.5
        path = QPainterPath()
        path.moveTo(c.x() - d, c.y() - d)
        path.lineTo(c.x() + d, c.y() + d)
        path.moveTo(c.x() + d, c.y() - d)
        path.lineTo(c.x() - d, c.y() + d)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 190), 1.6, Qt.SolidLine, Qt.RoundCap))
        p.drawPath(path)

    # ------------------------------------------------------------------ mouse (never keyboard)
    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() != Qt.LeftButton:
            return
        self.pressed = self._hit(e.position())
        if self.pressed is None:
            cx, cy = winapi.cursor_pos()
            wx, wy, _, _ = winapi.window_rect(self.hwnd())
            self.drag_from = (cx - wx, cy - wy)
            self.dragging = False

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self.drag_from and e.buttons() & Qt.LeftButton:
            cx, cy = winapi.cursor_pos()
            rect = winapi.window_rect(self.hwnd())
            winapi.place_topmost(self.hwnd(), [cx - self.drag_from[0], cy - self.drag_from[1], rect[2], rect[3]])
            self.dragging = True
            return
        hover = self._hit(e.position())
        if hover != self.hover:
            self.hover = hover
            self.setCursor(Qt.PointingHandCursor if hover else Qt.ArrowCursor)
            self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() != Qt.LeftButton:
            return
        if self.dragging:
            self.on_dragged()
        elif self.pressed and self._hit(e.position()) == self.pressed:
            self.actions_[self.pressed]()
        self.pressed, self.drag_from, self.dragging = None, None, False

    def leaveEvent(self, _e) -> None:  # noqa: N802
        if self.hover:
            self.hover = None
            self.update()
