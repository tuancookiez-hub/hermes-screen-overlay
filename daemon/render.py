"""PySide6 overlay: one transparent, click-through, top-most window per monitor.

Layout happens once in PHYSICAL pixels; each layer maps physical -> its own
logical coordinates with (p - monitor_origin) / devicePixelRatio.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

import shapes as S
import winapi
from panel import Panel

log = logging.getLogger("screen-overlay.render")

FONT_FAMILY = "Segoe UI"
DIM = QColor(0, 0, 0, 140)
HALO = QColor(0, 0, 0, 150)
PILL_BG = QColor(22, 22, 26, 240)
TEXT = QColor(255, 255, 255)
GLASS = QColor(14, 16, 22, 232)
GLASS_EDGE = QColor(255, 255, 255, 30)
INK_FAMILIES = ["Ink Free", "Segoe Print", "Segoe UI"]  # the first two ship with Windows
POINTER_SHAPE = ((0, 0), (0, 33), (8.2, 25.7), (13.6, 38), (19.1, 35.6), (13.8, 23.5), (24.2, 23.5))
RIPPLE_S = 0.9
LATE_S = 0.14
GUIDE_TTL_S = 1800


def _ink_font(px: float, bold: bool = False) -> QFont:
    f = QFont()
    f.setFamilies(INK_FAMILIES)
    f.setPixelSize(max(1, round(px)))
    f.setWeight(QFont.Bold if bold else QFont.Medium)
    f.setStyleStrategy(QFont.StyleStrategy(QFont.PreferAntialias | QFont.NoSubpixelAntialias))  # no colour fringe on glass
    return f


def _ink_width(text: str, px: float, bold: bool = False) -> float:
    return QFontMetricsF(_ink_font(px, bold)).horizontalAdvance(text)


def wrap_ink(text: str, px: float, max_w: float, bold: bool = False) -> list[str]:
    """Word-wrap one paragraph, breaking a word only if it is wider than the line."""
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}" if cur else word
        if _ink_width(trial, px, bold) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = word
        while _ink_width(cur, px, bold) > max_w and len(cur) > 1:
            n = len(cur) - 1
            while n > 1 and _ink_width(cur[:n], px, bold) > max_w:
                n -= 1
            lines.append(cur[:n])
            cur = cur[n:]
    if cur:
        lines.append(cur)
    return lines or [""]


def _luma(hex_color: str) -> float:
    c = QColor(hex_color)
    return 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()


def _font(px: float, bold: bool = False) -> QFont:
    f = QFont(FONT_FAMILY)
    f.setPixelSize(max(1, round(px)))
    f.setWeight(QFont.Bold if bold else QFont.DemiBold)
    return f


def measure(text: str, px: float, max_w: float) -> tuple[float, float]:
    fm = QFontMetricsF(_font(px))
    r = fm.boundingRect(QRectF(0, 0, max_w, 10_000), Qt.TextWordWrap | Qt.AlignLeft, text)
    return math.ceil(r.width()), math.ceil(r.height())


# --------------------------------------------------------------------------- layout
def layout(items: list[dict], monitors: list[dict]) -> dict:
    """Turn resolved shapes into a physical-pixel display list."""
    dl: dict[str, Any] = {"holes": [], "rings": [], "arrows": [], "badges": [], "pills": [], "ink": [],
                          "pointer": None}
    taken: list[tuple] = []
    summary = []

    def pill(text, anchor, area, placement, s, accent, late=False):
        fpx = 15 * s
        tw, th = measure(text, fpx, 380 * s)
        size = (tw + 28 * s, th + 16 * s)
        order = [placement] if placement != "auto" else []
        order += [p for p in ("below", "above", "right", "left") if p not in order]
        chosen = None
        for p in order:
            r = S.place_label(anchor, size, area, p, gap=12 * s)
            if not any(S.intersects(r, t) for t in taken):
                chosen = r
                break
        chosen = chosen or S.place_label(anchor, size, area, order[0], gap=12 * s)
        taken.append(chosen)
        dl["pills"].append({"rect": chosen, "text": text, "px": fpx, "accent": accent, "s": s, "late": late})
        return chosen

    def spot(size, it, rect, area, s):
        """Where a note or flow goes: at a point, beside a target, or in a clear corner."""
        if it.get("free"):
            return S.place_free(size, area, taken, 24 * s)
        if it.get("is_point"):
            x, y = S.clamp_point(rect[0], rect[1], (area[0], area[1], area[2] - size[0], area[3] - size[1]))
            return (x, y, size[0], size[1])
        return S.place_label(rect, size, area, it["placement"], gap=16 * s)

    for it in sorted(items, key=lambda i: i["type"] in S.FREE_TYPES):  # notes go round marks already placed
        rect = tuple(it["rect"])
        mon = S.monitor_for(rect, monitors)
        s = mon["scale"]
        area = tuple(mon["work"])
        is_point = it.get("is_point")
        base = S.inflate(rect, 12 * s) if is_point else rect
        ring = S.inflate(base, it["pad"] * s)
        typ, color = it["type"], it["color"]
        late = bool(it.get("late"))
        entry: dict[str, Any] = {"id": it["id"], "type": typ, "target_rect": list(rect),
                                 "monitor": mon["index"]}
        if typ in ("box", "spotlight", "step"):
            if typ == "spotlight":
                dl["holes"].append({"rect": ring, "r": 10 * s, "late": late})
            dl["rings"].append({"rect": ring, "color": color, "w": 3 * s, "r": 10 * s, "late": late})
            taken.append(ring)
            if typ == "step":
                c = (ring[0], ring[1])
                dl["badges"].append({"c": c, "n": it["number"], "color": color, "rad": 15 * s, "s": s, "late": late})
                taken.append((c[0] - 15 * s, c[1] - 15 * s, 30 * s, 30 * s))
            if it.get("text"):
                entry["label_rect"] = [round(v) for v in pill(it["text"], ring, area, it["placement"], s, color)]
            entry["ring_rect"] = [round(v) for v in ring]
        elif typ == "pointer":
            tip = (rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)
            flip = S.pointer_flip(tip, tuple(mon["rect"]), s)
            body = S.pointer_rect(tip, flip, s)
            taken.append(body)
            dl["pointer"] = {"tip": tip, "flip": flip, "color": color, "s": s, "rect": body,
                             "monitor": mon["index"]}
            if it.get("text"):
                entry["label_rect"] = [round(v) for v in pill(it["text"], S.inflate(body, 10 * s), area,
                                                              it["placement"], s, color, late=True)]
            entry["tip"] = [round(tip[0]), round(tip[1])]
        elif typ == "ink":
            tail = None
            if it["mark"] == "arrow":
                tail = tuple(it["from"]) if it.get("from") else S.arrow_tail(ring, area, 170 * s)
            strokes = S.ink_strokes(it["mark"], ring, S.seed_for(it["id"]), s, tail)
            bounds = S.strokes_bounds(strokes, 6 * s)
            dl["ink"].append({"id": it["id"], "rect": bounds, "plate": False, "color": color, "w": 3.2 * s, "s": s,
                              "dur": 0.5, "parts": [{"strokes": strokes, "dur": 0.5}]})
            taken.append(bounds)
            if it.get("text"):
                entry["label_rect"] = [round(v) for v in pill(it["text"], bounds, area, it["placement"], s, color)]
        elif typ == "note":
            entry["note_rect"] = _note(it, rect, area, s, dl, taken, spot)
        elif typ == "flow":
            entry["flow_rect"] = _flow(it, rect, area, s, dl, taken, spot)
        elif typ == "arrow":
            tail = tuple(it["from"]) if it.get("from") else S.arrow_tail(ring, area, 170 * s)
            head = S.edge_point(S.inflate(ring, 4 * s), tail)
            dl["arrows"].append({"a": tail, "b": head, "color": color, "w": 4 * s, "s": s})
            if it.get("text"):
                anchor = (tail[0] - 1, tail[1] - 1, 2, 2)
                dx, dy = tail[0] - head[0], tail[1] - head[1]
                plc = it["placement"]
                if plc == "auto":
                    plc = ("right" if dx > 0 else "left") if abs(dx) > abs(dy) else ("below" if dy > 0 else "above")
                entry["label_rect"] = [round(v) for v in pill(it["text"], anchor, area, plc, s, color)]
            entry["arrow"] = [round(tail[0]), round(tail[1]), round(head[0]), round(head[1])]
        elif typ == "label":
            entry["label_rect"] = [round(v) for v in pill(it["text"], base, area, it["placement"], s, color)]
        summary.append(entry)
    rects = [r["rect"] for r in dl["rings"]] + [p["rect"] for p in dl["pills"]] + [e["rect"] for e in dl["ink"]]
    if dl["pointer"]:
        rects.append(dl["pointer"]["rect"])
    rects += [(b["c"][0] - b["rad"], b["c"][1] - b["rad"], 2 * b["rad"], 2 * b["rad"]) for b in dl["badges"]]
    for a in dl["arrows"]:
        (ax, ay), (bx, by) = a["a"], a["b"]
        rects.append((min(ax, bx), min(ay, by), abs(ax - bx) + 1, abs(ay - by) + 1))
    used = {m["index"] for m in monitors for r in rects if S.intersects(tuple(r), tuple(m["rect"]))}
    return {"dl": dl, "summary": summary, "used": used}


def _timed(parts: list[dict], limit: float) -> float:
    """Keep long notes from taking forever: scale every part so the whole fits `limit` seconds."""
    total = sum(p["dur"] for p in parts)
    k = min(1.0, limit / total) if total else 1.0
    for part in parts:
        part["dur"] *= k
    return total * k


def _note(it: dict, rect, area, s: float, dl: dict, taken: list, spot) -> list[int]:
    fpx, tpx = 23 * s, 28 * s
    pad_x, pad_y = 22 * s, 16 * s
    gap, lh = 4 * s, QFontMetricsF(_ink_font(fpx)).height()
    max_w = min(520 * s, area[2] - 2 * pad_x - 40 * s)
    bullet_w = _ink_width("•  ", fpx)
    rows: list[dict] = []
    if it.get("title"):
        for line in wrap_ink(it["title"], tpx, max_w, bold=True):
            rows.append({"text": line, "px": tpx, "bold": True, "dx": 0.0, "h": QFontMetricsF(_ink_font(tpx, True)).height()})
    for para in it["text"].split("\n"):
        para = para.strip()
        if not para:
            rows.append({"text": "", "px": fpx, "bold": False, "dx": 0.0, "h": lh * 0.5})
            continue
        bullet = para[:2] in ("- ", "* ", "• ")
        body = para[2:].strip() if bullet else para
        for i, line in enumerate(wrap_ink(body, fpx, max_w - (bullet_w if bullet else 0))):
            rows.append({"text": f"•  {line}" if bullet and i == 0 else line, "px": fpx, "bold": False,
                         "dx": bullet_w if bullet and i else 0.0, "h": lh})
    width = max((_ink_width(r["text"], r["px"], r["bold"]) + r["dx"] for r in rows), default=0)
    size = (math.ceil(width + 2 * pad_x), math.ceil(sum(r["h"] + gap for r in rows) - gap + 2 * pad_y))
    box = spot(size, it, rect, area, s)
    taken.append(box)
    y = box[1] + pad_y
    parts = []
    for r in rows:
        if r["text"]:
            parts.append({"text": r["text"], "x": box[0] + pad_x + r["dx"], "y": y, "h": r["h"], "px": r["px"],
                          "bold": r["bold"], "w": _ink_width(r["text"], r["px"], r["bold"]) + 6 * s,
                          "dur": max(0.1, len(r["text"]) / 42)})
        y += r["h"] + gap
    dur = _timed(parts, 3.5)
    dl["ink"].append({"id": it["id"], "rect": box, "plate": True, "color": it["color"], "w": 3 * s, "s": s,
                      "dur": dur, "parts": parts})
    return [round(v) for v in box]


def _flow(it: dict, rect, area, s: float, dl: dict, taken: list, spot) -> list[int]:
    fpx = 21 * s
    pad_x, pad_y, margin, gap = 18 * s, 11 * s, 18 * s, 52 * s
    lh = QFontMetricsF(_ink_font(fpx)).height()
    nodes = [(n, _ink_width(n, fpx) + 2 * pad_x) for n in it["nodes"]]
    node_h = lh + 2 * pad_y
    right = it["direction"] == "right"
    if right and sum(w for _, w in nodes) + gap * (len(nodes) - 1) + 2 * margin > area[2] - 40 * s:
        right = False  # too wide for this monitor: stack it
    if right:
        size = (sum(w for _, w in nodes) + gap * (len(nodes) - 1) + 2 * margin, node_h + 2 * margin)
    else:
        size = (max(w for _, w in nodes) + 2 * margin, node_h * len(nodes) + gap * (len(nodes) - 1) + 2 * margin)
    size = (math.ceil(size[0]), math.ceil(size[1]))
    box = spot(size, it, rect, area, s)
    taken.append(box)
    seed = S.seed_for(it["id"])
    x, y = box[0] + margin, box[1] + margin
    parts: list[dict] = []
    prev = None
    for i, (text, w) in enumerate(nodes):
        nw = w if right else size[0] - 2 * margin
        node = (x, y, nw, node_h)
        if prev:
            a = S.edge_point(prev, (node[0] + nw / 2, y + node_h / 2))
            b = S.edge_point(S.inflate(node, 3 * s), (prev[0] + prev[2] / 2, prev[1] + prev[3] / 2))
            parts.append({"strokes": S.arrow_strokes(a, b, seed + 40 + i, s), "dur": 0.22})
        parts.append({"strokes": S.box_strokes(node, seed + i * 7, 1.5 * s), "dur": 0.32})
        parts.append({"text": text, "x": x + (nw - w) / 2 + pad_x, "y": y + pad_y, "h": lh, "px": fpx, "bold": False,
                      "w": w, "dur": max(0.12, len(text) / 42)})
        prev = node
        if right:
            x += nw + gap
        else:
            y += node_h + gap
    dur = _timed(parts, 4.0)
    dl["ink"].append({"id": it["id"], "rect": box, "plate": True, "color": it["color"], "w": 2.6 * s, "s": s,
                      "dur": dur, "parts": parts})
    return [round(v) for v in box]


# --------------------------------------------------------------------------- windows
class Layer(QWidget):
    def __init__(self, controller: "Overlay", screen, mon: dict):
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                 | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("Hermes Screen Overlay")
        self.c = controller
        self.mon = mon
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.dpr = screen.devicePixelRatio()
        winapi.make_click_through(self.hwnd())  # never activatable, from before the first show

    def hwnd(self) -> int:
        return int(self.winId())

    def present(self, exclude_capture: bool) -> None:
        if not self.isVisible():
            self.show()
        h = self.hwnd()
        winapi.exclude_from_capture(h, exclude_capture)
        winapi.place_topmost(h, self.mon["rect"])  # exact physical bounds, whatever Qt chose
        self.dpr = self.screen().devicePixelRatio() if self.screen() else self.dpr
        self.update()

    # physical -> local logical
    def P(self, x: float, y: float) -> QPointF:
        return QPointF((x - self.mon["rect"][0]) / self.dpr, (y - self.mon["rect"][1]) / self.dpr)

    def R(self, r) -> QRectF:
        p = self.P(r[0], r[1])
        return QRectF(p.x(), p.y(), r[2] / self.dpr, r[3] / self.dpr)

    def L(self, v: float) -> float:
        return v / self.dpr

    def paintEvent(self, _e) -> None:  # noqa: N802
        dl = self.c.display
        if not dl:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        fade = self.c.fade
        p.setOpacity(fade)

        def alpha(d: dict) -> float:
            a = fade * (self.c.late if d.get("late") else 1.0)
            p.setOpacity(a)
            return a

        holes_ = [h for h in dl["holes"] if alpha(h) > 0]
        if holes_:
            full = QPainterPath()
            full.addRect(QRectF(self.rect()))
            holes = QPainterPath()
            for h in holes_:
                holes.addRoundedRect(self.R(h["rect"]), self.L(h["r"]), self.L(h["r"]))
            alpha(holes_[0])
            p.fillPath(full.subtracted(holes), DIM)
        for r in dl["rings"]:
            alpha(r)
            rr = self.R(r["rect"])
            rad = self.L(r["r"])
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(HALO, self.L(r["w"] + 3)))
            p.drawRoundedRect(rr, rad, rad)
            p.setPen(QPen(QColor(r["color"]), self.L(r["w"])))
            p.drawRoundedRect(rr, rad, rad)
        p.setOpacity(fade)
        for a in dl["arrows"]:
            self._arrow(p, a)
        for b in dl["badges"]:
            alpha(b)
            self._badge(p, b)
        for pl in dl["pills"]:
            alpha(pl)
            self._pill(p, pl)
        p.setOpacity(fade)
        for e in dl["ink"]:
            self._ink(p, e, fade)
        p.setOpacity(1.0)
        ptr = self.c.ptr
        if ptr and ptr["monitor"] == self.mon["index"]:
            self._pointer(p, ptr)
        p.setOpacity(fade)
        if self.c.chip and self.mon["index"] in self.c.chip_monitors():
            self._chip(p)
        p.end()

    def _ink(self, p: QPainter, e: dict, fade: float) -> None:
        prog = self.c.ink_prog.get(e["id"], 0.0)
        if prog <= 0:
            return
        if e["plate"]:
            p.setOpacity(fade * min(1.0, prog * e["dur"] / 0.16))
            p.setPen(QPen(GLASS_EDGE, self.L(1)))
            p.setBrush(GLASS)
            rad = self.L(14 * e["s"])
            p.drawRoundedRect(self.R(e["rect"]), rad, rad)
            p.setOpacity(fade)
        left = prog * e["dur"]
        for part in e["parts"]:
            f = 1.0 if left >= part["dur"] else (left / part["dur"] if part["dur"] else 1.0)
            left -= part["dur"]
            if f <= 0:
                break
            if "strokes" in part:
                self._strokes(p, S.clip_strokes(part["strokes"], f), e)
            else:
                self._write(p, part, f, e)

    def _strokes(self, p: QPainter, strokes: list, e: dict) -> None:
        polys = [QPolygonF([self.P(x, y) for x, y in st]) for st in strokes if len(st) > 1]
        p.setBrush(Qt.NoBrush)
        w = self.L(e["w"])
        if not e["plate"]:  # on bare glass it needs a dark edge to read over any app
            p.setPen(QPen(HALO, w + self.L(3 * e["s"]), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            for poly in polys:
                p.drawPolyline(poly)
        p.setPen(QPen(QColor(e["color"]), w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        for poly in polys:
            p.drawPolyline(poly)

    def _write(self, p: QPainter, part: dict, f: float, e: dict) -> None:
        """Text appears left to right behind a moving edge."""
        r = self.R((part["x"], part["y"], part["w"], part["h"]))
        p.save()
        p.setClipRect(QRectF(r.x() - self.L(4), r.y() - self.L(4), (r.width() + self.L(8)) * f, r.height() + self.L(8)))
        p.setPen(QColor(e["color"]))
        p.setFont(_ink_font(part["px"] / self.dpr, part["bold"]))
        p.drawText(r.adjusted(0, 0, self.L(200), 0), Qt.AlignLeft | Qt.AlignVCenter | Qt.TextDontClip, part["text"])
        p.restore()

    def _pointer(self, p: QPainter, ptr: dict) -> None:
        s, (fx, fy) = ptr["s"], ptr["flip"]
        tip = self.P(*ptr["pos"])
        k = self.L(s)
        if ptr["ripple"] is not None:
            for delay in (0.0, 0.28):
                t = (ptr["ripple"] - delay) / (RIPPLE_S - 0.28)
                if 0 < t < 1:
                    e = 1 - (1 - t) ** 3
                    p.setOpacity(0.75 * (1 - t))
                    p.setBrush(Qt.NoBrush)
                    p.setPen(QPen(QColor(ptr["color"]), self.L(3 * s)))
                    rad = self.L((8 + 34 * e) * s)
                    p.drawEllipse(tip, rad, rad)
            p.setOpacity(1.0)
        path = QPainterPath()
        path.moveTo(0, 0)
        for x, y in POINTER_SHAPE[1:]:
            path.lineTo(x, y)
        path.closeSubpath()
        p.save()
        p.translate(tip.x() + self.L(1.5 * s), tip.y() + self.L(2.5 * s))
        p.scale(k * fx, k * fy)
        p.setPen(QPen(QColor(0, 0, 0, 90), 3.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(0, 0, 0, 90))
        p.drawPath(path)
        p.restore()
        p.save()
        p.translate(tip)
        p.scale(k * fx, k * fy)
        p.setPen(QPen(QColor(255, 255, 255), 1.9, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(ptr["color"]))
        p.drawPath(path)
        p.restore()

    def _arrow(self, p: QPainter, a: dict) -> None:
        A, B = self.P(*a["a"]), self.P(*a["b"])
        w = self.L(a["w"])
        ang = math.atan2(B.y() - A.y(), B.x() - A.x())
        hl = self.L(18 * a["s"])
        shaft_end = QPointF(B.x() - math.cos(ang) * hl * 0.8, B.y() - math.sin(ang) * hl * 0.8)
        head = QPolygonF([B,
                          QPointF(B.x() - hl * math.cos(ang - 0.45), B.y() - hl * math.sin(ang - 0.45)),
                          QPointF(B.x() - hl * math.cos(ang + 0.45), B.y() - hl * math.sin(ang + 0.45))])
        p.setPen(QPen(HALO, w + self.L(3), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawLine(A, shaft_end)
        p.drawPolygon(head)
        col = QColor(a["color"])
        p.setPen(QPen(col, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawLine(A, shaft_end)
        p.setBrush(col)
        p.drawPolygon(head)

    def _badge(self, p: QPainter, b: dict) -> None:
        c = self.P(*b["c"])
        rad = self.L(b["rad"])
        p.setPen(QPen(QColor(255, 255, 255), self.L(2 * b["s"])))
        p.setBrush(QColor(b["color"]))
        p.drawEllipse(c, rad, rad)
        p.setPen(QColor("#111111") if _luma(b["color"]) > 0.55 else TEXT)
        f = _font(15 * b["s"] / self.dpr, bold=True)
        p.setFont(f)
        p.drawText(QRectF(c.x() - rad, c.y() - rad, 2 * rad, 2 * rad), Qt.AlignCenter, str(b["n"]))

    def _pill(self, p: QPainter, pl: dict) -> None:
        r = self.R(pl["rect"])
        rad = min(r.height() / 2, self.L(16 * pl["s"]))
        p.setPen(QPen(QColor(255, 255, 255, 40), self.L(1)))
        p.setBrush(PILL_BG)
        p.drawRoundedRect(r, rad, rad)
        bar = QRectF(r.x() + self.L(8 * pl["s"]), r.y() + self.L(8 * pl["s"]),
                     self.L(3 * pl["s"]), r.height() - self.L(16 * pl["s"]))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pl["accent"]))
        p.drawRoundedRect(bar, bar.width() / 2, bar.width() / 2)
        p.setPen(TEXT)
        p.setFont(_font(pl["px"] / self.dpr))
        inner = r.adjusted(self.L(17 * pl["s"]), self.L(8 * pl["s"]), -self.L(11 * pl["s"]), -self.L(8 * pl["s"]))
        p.drawText(inner, Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignVCenter, pl["text"])

    def _chip(self, p: QPainter) -> None:
        s = self.mon["scale"]
        text = f"Hermes overlay  ·  {winapi.HOTKEY_LABEL} clears" if self.c.hotkey_ok else \
            "Hermes overlay  ·  ask Hermes to clear (hotkey unavailable)"
        fpx = 12 * s
        tw, th = measure(text, fpx, 2000)
        w, h = tw + 24 * s, th + 10 * s
        wx, wy, ww, wh = self.mon["work"]
        rect = (wx + ww / 2 - w / 2, wy + wh - h - 10 * s, w, h)
        r = self.R(rect)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(22, 22, 26, 200))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setPen(QColor(255, 255, 255, 220))
        p.setFont(_font(fpx / self.dpr))
        p.drawText(r, Qt.AlignCenter, text)


class Overlay:
    """Owns the layers and the current shapes. Every method runs on the Qt thread."""

    def __init__(self, app, settings: dict, hotkey_ok: bool):
        self.app = app
        self.settings = settings
        self.hotkey_ok = hotkey_ok
        self.items: list[dict] = []
        self.display: dict | None = None
        self.summary: list[dict] = []
        self.used: set[int] = set()
        self.layers: list[Layer] = []
        self.monitors: list[dict] = []
        self.fade = 1.0
        self.chip = True
        self.expires_at: float | None = None
        self.last_activity = time.monotonic()
        self.ptr: dict | None = None  # the drawn pointer: {pos, a, b, t, dur, ripple, ...}
        self.ptr_last: tuple | None = None
        self.late = 1.0  # opacity of marks that wait for the pointer to arrive
        self.ink_prog: dict[str, float] = {}  # shape id -> 0..1 written so far
        self.guide: dict | None = None
        self._gen = 0  # never reused, so a late lookup for a replaced guide can't land on the new one
        self._nscreens = 0
        self.guide_result: dict | None = None  # how the last guide ended, for status
        self.on_guide_change = lambda: None  # set by the daemon: wakes the anchor thread
        self.panel: Panel | None = None
        self.animate = True
        self._anim: QVariantAnimation | None = None
        self._frame_at = 0.0
        self._frames = QTimer()
        self._frames.setTimerType(Qt.PreciseTimer)
        self._frames.setInterval(16)
        self._frames.timeout.connect(self._frame)
        self._ttl = QTimer()
        self._ttl.setSingleShot(True)
        self._ttl.timeout.connect(self._expire)
        self._guide_ttl = QTimer()
        self._guide_ttl.setSingleShot(True)
        self._guide_ttl.timeout.connect(lambda: self.end_guide("expired"))
        self._keepalive = QTimer()
        self._keepalive.timeout.connect(self._tick)
        self._keepalive.start(2000)

    # ------------------------------------------------------------------ layers
    def _sync_layers(self) -> None:
        mons = winapi.monitors()
        sig = [(m["device"], tuple(m["rect"]), m["dpi"]) for m in mons]
        nscreens = len(QGuiApplication.screens())  # Qt may learn of a plugged-in monitor after Win32 does
        if sig == [(m["device"], tuple(m["rect"]), m["dpi"]) for m in self.monitors] and self.layers                 and nscreens == self._nscreens:
            return
        self._nscreens = nscreens
        for l in self.layers:
            l.hide()
            l.deleteLater()
        self.layers, self.monitors = [], mons
        by_dev = {m["device"]: m for m in mons}
        for screen in QGuiApplication.screens():
            mon = None
            try:
                hmon = screen.nativeInterface().handle()
                mon = by_dev.get(winapi.monitor_info(hmon)["device"])
            except Exception:
                g = screen.geometry()
                mon = next((m for m in mons if m["rect"][0] == g.x() and m["rect"][1] == g.y()), None)
            if mon:
                self.layers.append(Layer(self, screen, mon))
        log.info("layers: %s", [(l.mon["device"], l.mon["rect"], l.mon["scale"]) for l in self.layers])

    def _present(self) -> None:
        """Show layers only where there are marks, so other monitors (a game, say) stay untouched."""
        excl = bool(self.settings.get("exclude_from_capture", True))
        for l in self.layers:
            if l.mon["index"] in self.used:
                l.present(excl)
            elif l.isVisible():
                l.hide()
        self._raise_panel()

    def _raise_panel(self) -> None:
        if self.panel and self.panel.isVisible():
            winapi.place_topmost(self.panel.hwnd())  # above the layers, which pass clicks through anyway

    def _tick(self) -> None:
        if not self.items and not self.guide:
            return
        before = [(m["device"], tuple(m["rect"]), m["dpi"]) for m in self.monitors]
        layers_before = self.layers
        self._sync_layers()
        if [(m["device"], tuple(m["rect"]), m["dpi"]) for m in self.monitors] != before                 or self.layers is not layers_before:
            self._relayout()
            if self.guide and self.panel and self.panel.isVisible():  # its monitor may be gone
                self.guide["dragged"] = False
                self._place_panel()
        for l in self.layers:  # stay above windows that grabbed TOPMOST after us
            if l.isVisible():
                winapi.place_topmost(l.hwnd())
        self._raise_panel()

    def _relayout(self) -> None:
        if not self.monitors:  # every monitor gone (e.g. a remote session ended): nothing to draw on
            self.display, self.summary, self.used = None, [], set()
            self._frames.stop()
            return
        out = layout(self.items, self.monitors)
        self.display, self.summary, self.used = out["dl"], out["summary"], out["used"]
        self.ink_prog = {k: v for k, v in self.ink_prog.items() if any(e["id"] == k for e in self.display["ink"])}
        self._sync_pointer()
        if self.items:
            self._present()
        for l in self.layers:
            l.update()
        self._run_frames()

    # ------------------------------------------------------------------ animation
    def _sync_pointer(self) -> None:
        want = self.display["pointer"] if self.display else None
        if not want:
            if self.ptr:
                self.ptr_last = self.ptr["pos"]
            self.ptr = None
            self.late = 1.0
            return
        if self.ptr and self.ptr["b"] == want["tip"] and self.ptr["monitor"] == want["monitor"]:
            self.ptr.update(color=want["color"], flip=want["flip"], s=want["s"])
            return
        mon = next(m for m in self.monitors if m["index"] == want["monitor"])
        last = self.ptr["pos"] if self.ptr else self.ptr_last
        start = S.pointer_start(want["tip"], tuple(mon["rect"]), last, winapi.cursor_pos())
        dur = S.glide_seconds(start, want["tip"]) if self.animate else 0.0
        self.ptr = {**want, "pos": start, "a": start, "b": want["tip"], "t": 0.0, "dur": dur, "ripple": None}
        self.late = 0.0

    def _run_frames(self) -> None:
        moving = self.ptr and (self.ptr["t"] < self.ptr["dur"] or self.ptr["ripple"] is not None
                               or (self.ptr["dur"] == 0 and self.ptr["t"] == 0))
        writing = self.display and any(self.ink_prog.get(e["id"], 0.0) < 1 for e in self.display["ink"])
        if (moving or writing or self.late < 1) and not self._frames.isActive():
            self._frame_at = time.monotonic()
            self._frames.start()

    def _dirty(self, rect, pad: float = 0) -> None:
        for l in self.layers:
            if l.isVisible() and S.intersects(tuple(rect), tuple(l.mon["rect"])):
                r = l.R(S.inflate(rect, pad)).toAlignedRect().adjusted(-2, -2, 2, 2)
                l.update(r)

    def _ptr_bounds(self, ptr: dict, ripple: bool) -> tuple:
        body = S.pointer_rect(ptr["pos"], ptr["flip"], ptr["s"])
        if not ripple:
            return S.inflate(body, 8 * ptr["s"])
        x, y, s = ptr["pos"][0], ptr["pos"][1], ptr["s"]
        return (x - 50 * s, y - 50 * s, 100 * s, 100 * s)

    def _frame(self) -> None:
        now = time.monotonic()
        dt = min(now - self._frame_at, 0.05)
        self._frame_at = now
        ptr = self.ptr
        gliding = False
        if ptr:
            if ptr["t"] < ptr["dur"] or ptr["t"] == 0:
                self._dirty(self._ptr_bounds(ptr, False))
                ptr["t"] = min(ptr["dur"], ptr["t"] + dt) if ptr["dur"] else 1.0
                f = ptr["t"] / ptr["dur"] if ptr["dur"] else 1.0
                ptr["pos"] = S.glide_point(ptr["a"], ptr["b"], f)
                self._dirty(self._ptr_bounds(ptr, False))
                gliding = f < 1
                if not gliding:
                    ptr["ripple"] = 0.0 if self.animate else None
            elif ptr["ripple"] is not None:
                self._dirty(self._ptr_bounds(ptr, True))
                ptr["ripple"] += dt
                if ptr["ripple"] >= RIPPLE_S:
                    ptr["ripple"] = None
                self._dirty(self._ptr_bounds(ptr, True))
        if not gliding and self.late < 1:
            self.late = min(1.0, self.late + (dt / LATE_S if self.animate else 1.0))
            for l in self.layers:
                if l.isVisible():
                    l.update()
        writing = False
        if self.display and not gliding:
            for e in self.display["ink"]:
                prog = self.ink_prog.get(e["id"], 0.0)
                if prog < 1:
                    self.ink_prog[e["id"]] = min(1.0, prog + (dt / e["dur"] if self.animate and e["dur"] else 1.0))
                    self._dirty(e["rect"], 6)
                    writing = True
                    break  # one at a time, in order, like a hand
        if not (gliding or writing or self.late < 1 or (self.ptr and self.ptr["ripple"] is not None)):
            self._frames.stop()

    # ------------------------------------------------------------------ api
    def show(self, items: list[dict], replace: bool, ttl: float) -> dict:
        self.last_activity = time.monotonic()
        self.animate = winapi.animations_enabled()
        self._sync_layers()
        if replace:
            self.items = [it for it in self.items if it.get("guide")]
        ids = {it["id"] for it in items}
        for i in ids:
            self.ink_prog.pop(i, None)  # same id, new content: write it again
        self.items = [it for it in self.items if it["id"] not in ids] + items
        self._relayout()
        if ttl > 0:
            self.expires_at = time.monotonic() + ttl
            self._ttl.start(int(ttl * 1000))
        else:
            self.expires_at = None
            self._ttl.stop()
        self._fade_in()
        return {"shapes": [e for e in self.summary if e["id"] in ids]}

    def clear(self, ids: list[str] | None = None, owner: str | None = None) -> dict:
        had_guide = bool(self.guide)
        if self.guide and not ids and (owner is None or self.guide["owner"] == owner):
            self._drop_guide("cleared")
        before = len(self.items)
        self.items = S.after_clear(self.items, ids, owner)
        if len(self.items) != before or had_guide != bool(self.guide):
            self.last_activity = time.monotonic()  # a no-op clear must not hold the idle timer open
        self._after_change()
        return {"cleared": before - len(self.items), "remaining": len(self.items)}

    def _after_change(self) -> None:
        if self.items:
            self._relayout()
            return
        self.display, self.summary = None, []
        self.used = set()
        self._sync_pointer()
        self.ink_prog = {}
        self._ttl.stop()
        self.expires_at = None
        self._frames.stop()
        for l in self.layers:
            l.hide()

    def _expire(self) -> None:
        """Timed-out marks go; a running guide has its own timer and stays."""
        self.items = [it for it in self.items if it.get("guide")]
        self.expires_at = None
        self.last_activity = time.monotonic()
        self._after_change()

    # ------------------------------------------------------------------ guide
    def start_guide(self, guide: dict, owner: str | None, ttl: float, pointer: bool) -> dict:
        """`guide` is normalized, and its targets already pinned to windows."""
        self._drop_guide("replaced")
        self.items = [it for it in self.items if not it.get("guide")]
        self.last_activity = time.monotonic()
        self.animate = winapi.animations_enabled()
        self._sync_layers()
        self.guide = {**guide, "index": 0, "owner": owner, "ttl": ttl, "pointer": pointer, "gen": self._next_gen(),
                      "found": None, "looking": False, "dragged": False, "started": time.monotonic()}
        self.guide_result = None
        if self.panel is None:
            self.panel = Panel(lambda: self.guide_move("back"), lambda: self.guide_move("next"),
                               lambda: self.end_guide("closed"), self._panel_dragged)
        self._touch_guide()
        self._refresh_guide()
        return self.guide_status()

    def _next_gen(self) -> int:
        self._gen += 1
        return self._gen

    def _panel_dragged(self) -> None:
        if self.guide:
            self.guide["dragged"] = True

    def _touch_guide(self) -> None:
        self.last_activity = time.monotonic()
        ttl = self.guide["ttl"] if self.guide else 0
        if ttl > 0:
            self._guide_ttl.start(int(ttl * 1000))
        else:
            self._guide_ttl.stop()

    def guide_move(self, to: Any) -> dict:
        g = self.guide
        if not g:
            return {"ok": False, "error": "no guide is running"}
        n = len(g["steps"])
        i = {"next": g["index"] + 1, "back": g["index"] - 1}.get(to, to)
        if to in ("next",) and i >= n:
            self.end_guide("finished")
            return {"ok": True, "guide": self.guide_status()}
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < n:
            return {"ok": False, "error": f"step must be next, back or 1-{n}"}
        g["index"] = i
        g["gen"] = self._next_gen()
        g["found"], g["looking"] = None, False
        self._touch_guide()
        self._refresh_guide()
        self.on_guide_change()
        return {"ok": True, "guide": self.guide_status()}

    def guide_snapshot(self) -> dict | None:
        """What the anchor thread should look for right now."""
        g = self.guide
        if not g:
            return None
        st = g["steps"][g["index"]]
        return {"gen": g["gen"], "target": st["target"], "space": g.get("space"), "found": g["found"]}

    def guide_anchor(self, gen: int, rect: list | None, is_point: bool) -> None:
        """Result of a live lookup for the current step. rect None = not on screen."""
        g = self.guide
        if not g or g["gen"] != gen:
            return  # the user moved on while it was looking
        found = {"rect": list(rect), "is_point": is_point} if rect else None
        if found == g["found"] and g["looking"] == (rect is None):
            return
        g["found"], g["looking"] = found, rect is None
        self._refresh_guide()

    def _guide_items(self) -> list[dict]:
        g = self.guide
        if not g or not g["found"]:
            return []
        st = g["steps"][g["index"]]
        base = {"rect": g["found"]["rect"], "is_point": g["found"]["is_point"], "owner": g["owner"], "guide": True,
                "text": None, "color": st["color"], "placement": "auto", "pad": st["pad"]}
        out = []
        if st["mark"] != "none":
            out.append({**base, "id": "guide-mark", "type": st["mark"], "number": g["index"] + 1,
                        "late": g["pointer"]})
        if g["pointer"]:
            out.append({**base, "id": "guide-pointer", "type": "pointer"})
        return out

    def _refresh_guide(self) -> None:
        g = self.guide
        if not g:
            return
        self.items = [it for it in self.items if not it.get("guide")] + self._guide_items()
        self._sync_layers()
        self._after_change()
        st = g["steps"][g["index"]]
        note = "Looking for this on screen..." if g["looking"] else ""
        self.panel.set_content(guide=g["title"], index=g["index"], total=len(g["steps"]), accent=st["color"],
                               title=st["title"] or st["text"], text=st["text"] if st["title"] else "", note=note)
        self._place_panel()

    def _place_panel(self) -> None:
        g, panel = self.guide, self.panel
        if not self.monitors:
            return
        found = g["found"]
        rect = tuple(found["rect"]) if found else None
        if rect:
            mon = S.monitor_for(rect, self.monitors)
        else:
            mon = S.free_monitor(self.monitors, winapi.cursor_pos(), winapi.foreground_rect())
        s = mon["scale"]
        size = (panel.width() * s, panel.height() * s)
        excl = bool(self.settings.get("exclude_from_capture", True))
        if not panel.isVisible():
            panel.show()
        elif g["dragged"]:
            winapi.place_topmost(panel.hwnd(), winapi.window_rect(panel.hwnd())[:2] + [round(size[0]), round(size[1])])
            return
        winapi.exclude_from_capture(panel.hwnd(), excl)
        avoid = [S.inflate(rect, 40 * s)] if rect else []
        spot = S.place_free(size, tuple(mon["work"]), avoid, 24 * s, order=("br", "bl", "tr", "tl"))
        winapi.place_topmost(panel.hwnd(), [round(v) for v in spot])

    def _drop_guide(self, how: str) -> None:
        g = self.guide
        if not g:
            return
        self.guide_result = {"title": g["title"], "ended": how, "step": g["index"] + 1, "total": len(g["steps"])}
        self.guide = None
        self._guide_ttl.stop()
        self.items = [it for it in self.items if not it.get("guide")]
        if self.panel:
            self.panel.hide()

    def end_guide(self, how: str) -> dict:
        self._drop_guide(how)
        self.last_activity = time.monotonic()
        self._after_change()
        return {"ok": True}

    def guide_status(self) -> dict | None:
        g = self.guide
        if not g:
            return self.guide_result and {"running": False, **self.guide_result}
        st = g["steps"][g["index"]]
        return {"running": True, "title": g["title"], "step": g["index"] + 1, "total": len(g["steps"]),
                "step_title": st["title"] or st["text"], "target_on_screen": bool(g["found"]) if st["target"] else None}

    def status(self) -> dict:
        left = None if self.expires_at is None else max(0, round(self.expires_at - time.monotonic()))
        panel = None
        if self.panel and self.panel.isVisible():
            panel = {"hwnd": self.panel.hwnd(), "rect": winapi.window_rect(self.panel.hwnd())}
        return {"visible_shapes": [{"id": e["id"], "type": e["type"], "monitor": e["monitor"],
                                    "rect": e["target_rect"]} for e in self.summary],
                "guide": self.guide_status(), "panel": panel, "expires_in_s": left, "monitors": self.monitors or winapi.monitors(),
                "layers": [{"device": l.mon["device"], "dpr": l.dpr, "hwnd": l.hwnd(),
                            "window_rect": winapi.window_rect(l.hwnd())} for l in self.layers if l.isVisible()],
                "settings": self.settings}

    def chip_monitors(self) -> set[int]:
        used = {e["monitor"] for e in self.summary}
        return used or {m["index"] for m in self.monitors if m["primary"]}

    def idle_seconds(self) -> float:
        return 0 if self.items or self.guide else time.monotonic() - self.last_activity

    def _fade_in(self) -> None:
        if self._anim:
            self._anim.stop()
        self._anim = QVariantAnimation()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(160)

        def step(v):
            self.fade = float(v)
            for l in self.layers:
                l.update()
        self._anim.valueChanged.connect(step)
        self._anim.start()
