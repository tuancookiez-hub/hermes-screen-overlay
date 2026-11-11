"""Pinned glass notes: handwritten notes on dark glass that stay, and that the user can drag and edit.

They look like the glass notes `show` draws, but each is its own small window (the tool calls them
"sticky").

A sticky takes mouse clicks without activating, like the step guide, so dragging one never steals
focus. Editing is the one exception in the whole overlay: a double-click (or the pencil) lets the
note take the keyboard until Esc, Ctrl+Enter or a click elsewhere, then focus goes back to the app
the user was in. All drawing is in Qt logical pixels.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QLinearGradient, QPainter, QPainterPath,
                           QPen)
from PySide6.QtWidgets import QPlainTextEdit, QWidget

import shapes as S
import winapi

W = 248.0
PAD = 14.0
HEAD = 30.0
MIN_BODY = 64.0
GLASS = QColor(12, 14, 22, 156)  # see-through, like the glass notes `show` draws
GLASS_EDGE = QColor(255, 255, 255, 96)
GLASS_SHEEN = QColor(255, 255, 255, 26)
SHADOW = QColor(0, 0, 0, 170)
INK = QColor(255, 255, 255)
SOFT = QColor(255, 255, 255, 215)
INK_FAMILIES = ["Ink Free", "Segoe Print", "Segoe UI"]


def _body_font(px: float = 17.0) -> QFont:
    have = set(QFontDatabase.families())
    f = QFont(next((n for n in INK_FAMILIES if n in have), "Segoe UI"))
    f.setPixelSize(round(px))
    return f


def _ui_font(px: float, weight=QFont.DemiBold) -> QFont:
    f = QFont("Segoe UI")
    f.setPixelSize(round(px))
    f.setWeight(weight)
    return f


def accent(color: str) -> QColor:
    """The note's accent, lifted if it is too dark to read on the glass."""
    c = QColor(color)
    lum = 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
    return c.lighter(170) if lum < 0.35 else c


class Sticky(QWidget):
    def __init__(self, data: dict, on_change: Callable[[str], None], on_close: Callable[[str], None]):
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setWindowTitle("Hermes Note")
        self.d = dict(data)
        self.on_change, self.on_close = on_change, on_close
        self.hover: str | None = None
        self.pressed: str | None = None
        self.drag_from: tuple[int, int] | None = None
        self.dragging = False
        self.moved = False  # the user placed it; Hermes must not move it back
        self.editing = False
        self._prev_fg = 0
        self.editor = QPlainTextEdit(self)
        self.editor.setFrameStyle(0)
        self.editor.setFont(_body_font())
        self.editor.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.editor.setStyleSheet("QPlainTextEdit { background: transparent; color: #FFFFFF; "
                                  "selection-background-color: rgba(255,255,255,70); selection-color: #FFFFFF; }")
        self.editor.document().setDocumentMargin(0)
        self.editor.hide()
        self.editor.installEventFilter(self)
        self.editor.textChanged.connect(self._typed)
        winapi.make_no_activate(self.hwnd())
        self.setFixedSize(self.sizeHint())

    def hwnd(self) -> int:
        return int(self.winId())

    # ------------------------------------------------------------------ content
    def set_data(self, **changes) -> None:
        if self.editing:
            self.end_edit(commit=False)
        self.d.update(changes)
        self.setFixedSize(self.sizeHint())
        self.update()

    def _text_h(self, text: str) -> float:
        fm = QFontMetricsF(_body_font())
        h = fm.boundingRect(QRectF(0, 0, W - 2 * PAD, 10_000), Qt.TextWordWrap, text or " ").height()
        return max(MIN_BODY, h + 4)

    def sizeHint(self) -> QSize:  # noqa: N802
        text = self.editor.toPlainText() if self.editing else self.d["text"]
        return QSize(int(W), int(HEAD + 8 + self._text_h(text) + PAD))

    def _body(self) -> QRectF:
        return QRectF(PAD, HEAD + 8, W - 2 * PAD, self.height() - HEAD - 8 - PAD)

    def _buttons(self) -> dict[str, QRectF]:
        return {"close": QRectF(W - 8 - 22, 4, 22, 22), "edit": QRectF(W - 8 - 22 - 4 - 22, 4, 22, 22)}

    def _hit(self, pos: QPointF) -> str | None:
        return next((k for k, r in self._buttons().items() if r.contains(pos)), None)

    # ------------------------------------------------------------------ paint
    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        tint = accent(self.d["color"])
        card = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(GLASS_EDGE, 1.2))
        p.setBrush(GLASS)
        p.drawRoundedRect(card, 12, 12)
        sheen = QLinearGradient(card.topLeft(), card.bottomLeft())
        sheen.setColorAt(0.0, GLASS_SHEEN)
        sheen.setColorAt(0.45, QColor(255, 255, 255, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(sheen)
        p.drawRoundedRect(card, 12, 12)
        p.setPen(Qt.NoPen)
        p.setBrush(tint)
        p.drawRoundedRect(QRectF(PAD, HEAD + 1, 34, 3), 1.5, 1.5)  # a short accent rule under the heading
        if self.editing:
            p.setPen(QPen(tint, 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)

        p.setPen(SOFT)
        p.setFont(_ui_font(12))
        title = self.d.get("title") or ("Editing: Esc cancels, Ctrl+Enter saves" if self.editing else "Note")
        if self.editing and self.d.get("title"):
            title = "Editing: Ctrl+Enter saves"
        title = QFontMetricsF(p.font()).elidedText(title, Qt.ElideRight, W - PAD - 60)
        head = QRectF(PAD, 0, W - PAD - 60, HEAD)
        p.setPen(SHADOW)
        p.drawText(head.translated(1, 1), Qt.AlignLeft | Qt.AlignVCenter, title)
        p.setPen(SOFT)
        p.drawText(head, Qt.AlignLeft | Qt.AlignVCenter, title)

        for key, r in self._buttons().items():
            if self.hover == key:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 44))
                p.drawRoundedRect(r, 6, 6)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 190), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            c = r.center()
            path = QPainterPath()
            if key == "close":
                for a, b in ((-4, -4), (4, -4)):
                    path.moveTo(c.x() + a, c.y() + b)
                    path.lineTo(c.x() - a, c.y() - b)
            else:  # a pencil
                path.moveTo(c.x() - 5, c.y() + 5)
                path.lineTo(c.x() - 3.6, c.y() + 1.2)
                path.lineTo(c.x() + 3, c.y() - 5.4)
                path.lineTo(c.x() + 5.4, c.y() - 3)
                path.lineTo(c.x() - 1.2, c.y() + 3.6)
                path.closeSubpath()
            p.drawPath(path)

        if not self.editing:
            p.setFont(_body_font())
            p.setPen(SHADOW)
            p.drawText(self._body().translated(1.2, 1.2), Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, self.d["text"])
            p.setPen(INK)
            p.drawText(self._body(), Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, self.d["text"])
        p.end()

    # ------------------------------------------------------------------ mouse: drag, buttons, double-click
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
            self.setCursor(Qt.PointingHandCursor if hover else Qt.SizeAllCursor)
            self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() != Qt.LeftButton:
            return
        if self.dragging:
            self.moved = True
            self.on_change(self.d["id"])
        elif self.pressed and self._hit(e.position()) == self.pressed:
            if self.pressed == "close":
                self.on_close(self.d["id"])
            else:
                self.end_edit(True) if self.editing else self.begin_edit()
        self.pressed, self.drag_from, self.dragging = None, None, False

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton and self._hit(e.position()) is None:
            self.drag_from = None
            self.begin_edit()

    def leaveEvent(self, _e) -> None:  # noqa: N802
        if self.hover:
            self.hover = None
            self.update()

    # ------------------------------------------------------------------ editing (the only time it takes focus)
    def begin_edit(self) -> None:
        if self.editing:
            return
        self.editing = True
        self._prev_fg = winapi.foreground()
        self.editor.blockSignals(True)
        self.editor.setPlainText(self.d["text"])
        self.editor.blockSignals(False)
        self.editor.setGeometry(self._body().toAlignedRect())
        self.editor.show()
        winapi.set_no_activate(self.hwnd(), False)
        winapi.activate(self.hwnd())
        self.activateWindow()
        self.editor.setFocus(Qt.MouseFocusReason)
        cur = self.editor.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        self.editor.setTextCursor(cur)
        self.update()

    def end_edit(self, commit: bool = True) -> None:
        if not self.editing:
            return
        self.editing = False
        text = self.editor.toPlainText().strip()[:S.MAX_STICKY_TEXT]
        changed = commit and bool(text) and text != self.d["text"]
        if changed:
            self.d["text"] = text
            self.d["edited"] = True
        self.editor.hide()
        hwnd = self.hwnd()
        winapi.set_no_activate(hwnd, True)
        if self._prev_fg and winapi.foreground() == hwnd and winapi.is_window(self._prev_fg):
            winapi.activate(self._prev_fg)  # hand the keyboard back to where the user was
        self._prev_fg = 0
        self.setFixedSize(self.sizeHint())
        self.update()
        if changed:
            self.on_change(self.d["id"])

    def _typed(self) -> None:
        if not self.editing:
            return
        text = self.editor.toPlainText()
        if len(text) > S.MAX_STICKY_TEXT:
            self.editor.blockSignals(True)
            self.editor.setPlainText(text[:S.MAX_STICKY_TEXT])
            cur = self.editor.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            self.editor.setTextCursor(cur)
            self.editor.blockSignals(False)
        self.setFixedSize(self.sizeHint())
        self.editor.setGeometry(self._body().toAlignedRect())
        self.update()

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if obj is self.editor and self.editing:
            if ev.type() == QEvent.KeyPress:
                if ev.key() == Qt.Key_Escape:
                    self.end_edit(False)
                    return True
                if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and ev.modifiers() & Qt.ControlModifier:
                    self.end_edit(True)
                    return True
            elif ev.type() == QEvent.FocusOut:
                self.end_edit(True)
        return super().eventFilter(obj, ev)

    def changeEvent(self, ev) -> None:  # noqa: N802
        if ev.type() == QEvent.ActivationChange and self.editing and not self.isActiveWindow():
            self.end_edit(True)
        super().changeEvent(ev)
