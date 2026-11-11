"""Shape validation and screen geometry. Pure Python, shared by the tool and the daemon.

All geometry here is in PHYSICAL virtual-screen pixels: the same space as Win32
(per-monitor-v2 aware) and UI Automation bounding rectangles.
"""
from __future__ import annotations

import math
import random
import re
import zlib
from typing import Any

SHAPE_TYPES = ("box", "spotlight", "arrow", "label", "step", "pointer", "note", "ink", "flow")
PLACEMENTS = ("auto", "below", "above", "right", "left")
INK_MARKS = ("circle", "underline", "box", "arrow", "cross", "check")
GUIDE_MARKS = ("step", "spotlight", "box", "none")
FLOW_DIRECTIONS = ("right", "down")
FREE_TYPES = ("note", "flow")  # may omit target; they find a clear spot themselves
MAX_SHAPES = 24
MAX_TEXT = 200
MAX_NOTE = 800
MAX_FLOW_NODES = 6
MAX_NODE_TEXT = 60
MAX_STEPS = 20
MAX_COORD = 1e6  # far beyond any real screen; keeps hand-drawn strokes from exploding in size
MAX_STEP_TITLE = 80
MAX_STEP_TEXT = 300
MAX_STICKIES = 12
MAX_STICKY_TEXT = 400
STICKY_COLOR = "yellow"
PALETTE = {
    "orange": "#FF9F0A",
    "blue": "#0A84FF",
    "green": "#30D158",
    "red": "#FF453A",
    "yellow": "#FFD60A",
    "purple": "#BF5AF2",
    "pink": "#FF375F",
    "teal": "#64D2FF",
    "white": "#FFFFFF",
}
DEFAULT_COLOR = "orange"
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

Rect = tuple  # (x, y, w, h)


class ShapeError(ValueError):
    """Bad tool input. The message is shown to the model, so it says how to fix it."""


# --------------------------------------------------------------------------- validation
def color(value: Any) -> str:
    if value is None or value == "":
        return PALETTE[DEFAULT_COLOR]
    v = str(value).strip()
    if v.lower() in PALETTE:
        return PALETTE[v.lower()]
    if _HEX.match(v):
        if len(v) == 4:
            v = "#" + "".join(c * 2 for c in v[1:])
        return v.upper()
    raise ShapeError(f"color {value!r} is not a palette name ({', '.join(PALETTE)}) or #RRGGBB")


def _nums(value: Any, n: int, field: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != n:
        raise ShapeError(f"{field} must be a list of {n} numbers")
    out = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > MAX_COORD:
            raise ShapeError(f"{field} must contain only finite numbers no larger than {MAX_COORD:g}")
        out.append(float(v))
    return out


def normalize_space(space: Any) -> dict | None:
    """Coordinate space for rect/point targets. None means physical screen pixels."""
    if space in (None, "", "screen"):
        return None
    if not isinstance(space, dict):
        raise ShapeError("space must be an object like {frame_width, frame_height, region}")
    fw, fh = space.get("frame_width"), space.get("frame_height")
    for name, v in (("frame_width", fw), ("frame_height", fh)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 < v <= MAX_COORD:
            raise ShapeError(f"space.{name} must be a positive number (the screenshot size you measured on)")
    region = space.get("region", "primary")
    if isinstance(region, str):
        if region not in ("primary", "virtual"):
            raise ShapeError("space.region must be 'primary', 'virtual', {monitor: n} or [x, y, w, h]")
    elif isinstance(region, dict):
        mi = region.get("monitor")
        if isinstance(mi, bool) or not isinstance(mi, int) or mi < 0:
            raise ShapeError("space.region.monitor must be a monitor index from inspect(what='screens')")
        region = {"monitor": mi}
    else:
        x, y, w, h = _nums(region, 4, "space.region")
        if w <= 0 or h <= 0:
            raise ShapeError("space.region width and height must be positive")
        region = [x, y, w, h]
    return {"frame_width": float(fw), "frame_height": float(fh), "region": region}


def normalize_target(t: Any, where: str) -> dict:
    if not isinstance(t, dict) or not t:
        raise ShapeError(f"{where}: target must be an object with one of rect, point, window, element")
    keys = [k for k in ("rect", "point", "window", "element") if k in t]
    if len(keys) != 1:
        raise ShapeError(f"{where}: target needs exactly one of rect, point, window, element (got {sorted(t)})")
    k = keys[0]
    if k == "rect":
        x, y, w, h = _nums(t["rect"], 4, f"{where}.rect")
        if w < 0 or h < 0:
            raise ShapeError(f"{where}.rect width/height must be >= 0")
        return {"rect": [x, y, w, h]}
    if k == "point":
        return {"point": _nums(t["point"], 2, f"{where}.point")}
    if k == "window":
        return {"window": _window_ref(t["window"], f"{where}.window")}
    el = t["element"]
    if not isinstance(el, dict):
        raise ShapeError(f"{where}.element must be an object like {{window, name, control_type}}")
    out: dict[str, Any] = {}
    if el.get("window") not in (None, ""):
        out["window"] = _window_ref(el["window"], f"{where}.element.window")
    for f in ("name", "control_type", "automation_id"):
        v = el.get(f)
        if v not in (None, ""):
            if not isinstance(v, str) or len(v) > MAX_TEXT:
                raise ShapeError(f"{where}.element.{f} must be a string up to {MAX_TEXT} chars")
            out[f] = v
    if not any(f in out for f in ("name", "control_type", "automation_id")):
        raise ShapeError(f"{where}.element needs at least one of name, control_type, automation_id")
    idx = el.get("index", 0)
    if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < 50:
        raise ShapeError(f"{where}.element.index must be an integer 0-49")
    out["index"] = idx
    return {"element": out}


def _window_ref(v: Any, where: str) -> Any:
    if isinstance(v, bool):
        raise ShapeError(f"{where} must be a title substring, process name, or window handle")
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip() and len(v) <= MAX_TEXT:
        return v.strip()
    raise ShapeError(f"{where} must be a title substring, process name (e.g. notepad.exe), or window handle")


def normalize_shapes(raw: Any) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ShapeError("shapes must be a non-empty list")
    if len(raw) > MAX_SHAPES:
        raise ShapeError(f"at most {MAX_SHAPES} shapes per call")
    out: list[dict] = []
    next_step = 1
    for i, s in enumerate(raw):
        where = f"shapes[{i}]"
        if not isinstance(s, dict):
            raise ShapeError(f"{where} must be an object")
        typ = s.get("type")
        if typ not in SHAPE_TYPES:
            raise ShapeError(f"{where}.type must be one of {', '.join(SHAPE_TYPES)}")
        text = s.get("text")
        if text is not None:
            if not isinstance(text, str):
                raise ShapeError(f"{where}.text must be a string")
            text = text.strip()[:MAX_NOTE if typ == "note" else MAX_TEXT] or None
        if typ in ("label", "note") and not text:
            raise ShapeError(f"{where}: a {typ} needs text")
        placement = s.get("placement", "auto")
        if placement not in PLACEMENTS:
            raise ShapeError(f"{where}.placement must be one of {', '.join(PLACEMENTS)}")
        pad = s.get("pad", 6)
        if isinstance(pad, bool) or not isinstance(pad, (int, float)) or not 0 <= pad <= 80:
            raise ShapeError(f"{where}.pad must be a number 0-80")
        shape: dict[str, Any] = {
            "id": str(s.get("id") or f"s{i + 1}")[:40],
            "type": typ,
            "target": None if typ in FREE_TYPES and s.get("target") is None
            else normalize_target(s.get("target"), f"{where}.target"),
            "text": text,
            "color": color(s.get("color") or ("white" if typ in FREE_TYPES else None)),
            "placement": placement,
            "pad": float(pad),
        }
        if typ == "step":
            n = s.get("number", next_step)
            if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 99:
                raise ShapeError(f"{where}.number must be an integer 0-99")
            shape["number"] = n
            next_step = n + 1
        if typ in ("arrow", "ink") and s.get("from") is not None:
            shape["from"] = _nums(s["from"], 2, f"{where}.from")
        if typ == "note" and s.get("title") not in (None, ""):
            if not isinstance(s["title"], str):
                raise ShapeError(f"{where}.title must be a string")
            shape["title"] = s["title"].strip()[:MAX_STEP_TITLE]
        if typ == "ink":
            if s.get("mark") not in INK_MARKS:
                raise ShapeError(f"{where}.mark must be one of {', '.join(INK_MARKS)}")
            shape["mark"] = s["mark"]
        if typ == "flow":
            shape.update(_flow(s, where))
        out.append(shape)
    return out


def normalize_stickies(raw: Any) -> list[dict]:
    """Sticky notes to add or update: [{id?, text, title?, color?, target?}]."""
    if not isinstance(raw, list) or not raw:
        raise ShapeError("notes must be a non-empty list")
    if len(raw) > MAX_STICKIES:
        raise ShapeError(f"at most {MAX_STICKIES} sticky notes")
    out: list[dict] = []
    seen: set[str] = set()
    for i, n in enumerate(raw):
        where = f"notes[{i}]"
        if not isinstance(n, dict):
            raise ShapeError(f"{where} must be an object")
        text = n.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ShapeError(f"{where}: a sticky note needs text")
        title = n.get("title")
        if title not in (None, "") and not isinstance(title, str):
            raise ShapeError(f"{where}.title must be a string")
        nid = str(n.get("id") or f"sticky{i + 1}")[:40]
        if nid in seen:
            raise ShapeError(f"{where}.id {nid!r} is used twice")
        seen.add(nid)
        out.append({
            "id": nid,
            "text": text.strip()[:MAX_STICKY_TEXT],
            "title": (title or "").strip()[:MAX_STEP_TITLE],
            "color": color(n.get("color") or STICKY_COLOR),
            "target": None if n.get("target") is None else normalize_target(n["target"], f"{where}.target"),
        })
    return out


def sticky_spot(size: tuple[float, float], target: Rect | None, work: Rect, taken: list, margin: float = 20) -> Rect:
    """Where a new sticky goes: beside its target when there is room, else a free corner."""
    w, h = size
    if target and (target[2] or target[3]):
        tx, ty, tw, th = target
        for x, y in ((tx + tw + margin, ty), (tx - margin - w, ty), (tx, ty + th + margin), (tx, ty - margin - h)):
            r = (x, y, w, h)
            inside = work[0] <= x and x + w <= work[0] + work[2] and work[1] <= y and y + h <= work[1] + work[3]
            if inside and not any(intersects(r, tuple(t)) for t in taken):
                return r
    avoid = list(taken) + ([inflate(target, margin)] if target else [])
    return place_free(size, work, avoid, margin)


def _flow(s: dict, where: str) -> dict:
    nodes = s.get("nodes")
    if not isinstance(nodes, list) or not 2 <= len(nodes) <= MAX_FLOW_NODES:
        raise ShapeError(f"{where}.nodes must be a list of 2-{MAX_FLOW_NODES} short strings")
    clean = []
    for n in nodes:
        if not isinstance(n, str) or not n.strip():
            raise ShapeError(f"{where}.nodes must contain only non-empty strings")
        clean.append(n.strip()[:MAX_NODE_TEXT])
    direction = s.get("direction", "right")
    if direction not in FLOW_DIRECTIONS:
        raise ShapeError(f"{where}.direction must be right or down")
    return {"nodes": clean, "direction": direction}


def normalize_guide(raw: Any, title: Any = None) -> dict:
    """Validate a step guide. Targets are optional: a step may just be an instruction."""
    if not isinstance(raw, list) or not raw:
        raise ShapeError("steps must be a non-empty list")
    if len(raw) > MAX_STEPS:
        raise ShapeError(f"at most {MAX_STEPS} steps in a guide")
    if title is not None and not isinstance(title, str):
        raise ShapeError("title must be a string")
    steps = []
    for i, st in enumerate(raw):
        where = f"steps[{i}]"
        if not isinstance(st, dict):
            raise ShapeError(f"{where} must be an object")
        for f in ("title", "text"):
            if st.get(f) is not None and not isinstance(st[f], str):
                raise ShapeError(f"{where}.{f} must be a string")
        head = (st.get("title") or "").strip()[:MAX_STEP_TITLE]
        body = (st.get("text") or "").strip()[:MAX_STEP_TEXT]
        if not head and not body:
            raise ShapeError(f"{where} needs a title or text")
        mark = st.get("mark", "step")
        if mark not in GUIDE_MARKS:
            raise ShapeError(f"{where}.mark must be one of {', '.join(GUIDE_MARKS)}")
        pad = st.get("pad", 6)
        if isinstance(pad, bool) or not isinstance(pad, (int, float)) or not 0 <= pad <= 80:
            raise ShapeError(f"{where}.pad must be a number 0-80")
        target = st.get("target")
        steps.append({
            "title": head, "text": body, "mark": mark, "pad": float(pad), "color": color(st.get("color")),
            "target": None if target is None else normalize_target(target, f"{where}.target"),
        })
    return {"title": (title or "").strip()[:MAX_STEP_TITLE], "steps": steps}


_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def guide_slug(name: Any) -> str:
    """File-safe name for a saved guide."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:60].strip("-")
    if not slug:
        raise ShapeError("a saved guide needs a name with letters or digits")
    if slug in _RESERVED_NAMES:  # "con.json" would open the console device on Windows
        slug = f"guide-{slug}"
    return slug


def seed_for(text: str) -> int:
    return zlib.crc32(text.encode("utf-8", "replace"))


def after_clear(items: list[dict], ids: list | None = None, owner: str | None = None) -> list[dict]:
    """Items that survive a clear. `ids` limits it to those ids, `owner` to one session's marks."""
    keep = []
    for it in items:
        hit = (not ids or it["id"] in ids) and (owner is None or it.get("owner") == owner)
        if not hit:
            keep.append(it)
    return keep


# --------------------------------------------------------------------------- geometry
def frame_region(space: dict, monitors: list[dict], virtual: Rect) -> Rect:
    region = space["region"]
    if region == "virtual":
        return tuple(virtual)
    if region == "primary":
        prim = next((m for m in monitors if m.get("primary")), monitors[0])
        return tuple(prim["rect"])
    if isinstance(region, dict):
        mi = region["monitor"]
        if mi >= len(monitors):
            raise ShapeError(f"space.region.monitor {mi} does not exist ({len(monitors)} monitors)")
        return tuple(monitors[mi]["rect"])
    return tuple(region)


def map_from_frame(x: float, y: float, space: dict, region: Rect) -> tuple[float, float]:
    """Screenshot-frame coordinates -> physical screen pixels."""
    rx, ry, rw, rh = region
    return rx + x * rw / space["frame_width"], ry + y * rh / space["frame_height"]


def map_rect_from_frame(r: list[float], space: dict, region: Rect) -> Rect:
    x0, y0 = map_from_frame(r[0], r[1], space, region)
    x1, y1 = map_from_frame(r[0] + r[2], r[1] + r[3], space, region)
    return (round(x0), round(y0), round(x1 - x0), round(y1 - y0))


def intersects(a: Rect, b: Rect) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def contains_point(r: Rect, x: float, y: float) -> bool:
    return r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]


def monitor_for(rect: Rect, monitors: list[dict]) -> dict:
    """Monitor holding the rect's centre, else the one it overlaps most, else primary."""
    cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
    for m in monitors:
        if contains_point(m["rect"], cx, cy):
            return m
    best, area = None, 0.0
    for m in monitors:
        mx, my, mw, mh = m["rect"]
        w = min(rect[0] + rect[2], mx + mw) - max(rect[0], mx)
        h = min(rect[1] + rect[3], my + mh) - max(rect[1], my)
        if w > 0 and h > 0 and w * h > area:
            best, area = m, w * h
    return best or next((m for m in monitors if m.get("primary")), monitors[0])


def free_monitor(monitors: list[dict], cursor: tuple[int, int], fullscreen: Rect | None = None,
                 anchor: Rect | None = None) -> dict:
    """Monitor for something with no target of its own: beside the other marks, else where the user is.

    Never a monitor a fullscreen app fills while another is free: that is a game or a video, and the
    mouse being there says nothing about wanting a note on top of it.
    """
    if anchor:
        return monitor_for(anchor, monitors)
    mon = monitor_for((cursor[0], cursor[1], 1, 1), monitors)
    if fullscreen and list(fullscreen) == list(mon["rect"]):
        return next((m for m in monitors if m is not mon), mon)
    return mon


def inflate(r: Rect, d: float) -> Rect:
    return (r[0] - d, r[1] - d, r[2] + 2 * d, r[3] + 2 * d)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi)) if hi >= lo else lo


def place_label(target: Rect, size: tuple[float, float], area: Rect, placement: str = "auto",
                gap: float = 10) -> Rect:
    """Put a label of `size` next to `target`, inside `area` (a monitor work area)."""
    w, h = size
    tx, ty, tw, th = target
    ax, ay, aw, ah = area
    spots = {
        "below": (tx + tw / 2 - w / 2, ty + th + gap),
        "above": (tx + tw / 2 - w / 2, ty - gap - h),
        "right": (tx + tw + gap, ty + th / 2 - h / 2),
        "left": (tx - gap - w, ty + th / 2 - h / 2),
    }

    def fits(p):
        x, y = p
        return x >= ax and y >= ay and x + w <= ax + aw and y + h <= ay + ah

    order = [placement] if placement != "auto" else []
    order += [k for k in ("below", "above", "right", "left") if k not in order]
    for k in order:
        if fits(spots[k]):
            x, y = spots[k]
            return (x, y, w, h)
    x, y = spots[order[0]]
    return (_clamp(x, ax, ax + aw - w), _clamp(y, ay, ay + ah - h), w, h)


def edge_point(r: Rect, toward: tuple[float, float]) -> tuple[float, float]:
    """Where the ray from the rect's centre toward `toward` leaves the rect."""
    cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == 0 and dy == 0:
        return cx, cy
    hw, hh = r[2] / 2, r[3] / 2
    tx = hw / abs(dx) if dx else math.inf
    ty = hh / abs(dy) if dy else math.inf
    t = min(tx, ty)
    return cx + dx * t, cy + dy * t


def arrow_tail(target: Rect, area: Rect, length: float) -> tuple[float, float]:
    """Auto tail: diagonal from the target toward the middle of the monitor, kept on screen."""
    cx, cy = target[0] + target[2] / 2, target[1] + target[3] / 2
    ax, ay, aw, ah = area
    sx = 1 if cx < ax + aw / 2 else -1
    sy = 1 if cy < ay + ah / 2 else -1
    ex, ey = edge_point(target, (cx + sx * 1000, cy + sy * 1000))
    d = length / math.sqrt(2)
    return _clamp(ex + sx * d, ax + 8, ax + aw - 8), _clamp(ey + sy * d, ay + 8, ay + ah - 8)


def clamp_point(x: float, y: float, r: Rect) -> tuple[float, float]:
    return _clamp(x, r[0], r[0] + r[2] - 1), _clamp(y, r[1], r[1] + r[3] - 1)


def place_free(size: tuple[float, float], area: Rect, taken: list, margin: float = 24,
               order: tuple = ("tr", "br", "tl", "bl")) -> Rect:
    """A clear spot in a corner of `area`, stacking away from the corner when it is occupied."""
    w, h = size
    ax, ay, aw, ah = area
    for corner in order:
        x = ax + margin if corner[1] == "l" else ax + aw - margin - w
        top = corner[0] == "t"
        y = ay + margin if top else ay + ah - margin - h
        while ay <= y and y + h <= ay + ah:
            r = (x, y, w, h)
            if not any(intersects(r, tuple(t)) for t in taken):
                return r
            y += (h + margin) if top else -(h + margin)
    return (_clamp(ax + aw - margin - w, ax, ax + aw - w), _clamp(ay + margin, ay, ay + ah - h), w, h)


# --------------------------------------------------------------------------- pointer
POINTER_W, POINTER_H = 27.0, 41.0  # body size at 100% scaling, tip at the corner


def pointer_flip(tip: tuple[float, float], area: Rect, s: float) -> tuple[int, int]:
    """Body direction from the tip: (1, 1) is down-right. Mirrors near an edge so it stays on screen."""
    fx = -1 if tip[0] + POINTER_W * s + 8 > area[0] + area[2] else 1
    fy = -1 if tip[1] + POINTER_H * s + 8 > area[1] + area[3] else 1
    return fx, fy


def pointer_rect(tip: tuple[float, float], flip: tuple[int, int], s: float) -> Rect:
    w, h = POINTER_W * s, POINTER_H * s
    return (tip[0] if flip[0] > 0 else tip[0] - w, tip[1] if flip[1] > 0 else tip[1] - h, w, h)


def pointer_start(tip: tuple[float, float], mon: Rect, last, cursor) -> tuple[float, float]:
    """Where a glide starts: stay on the target's monitor so a game on another one is never covered."""
    if last and contains_point(mon, last[0], last[1]):
        start = (float(last[0]), float(last[1]))
    else:
        start = clamp_point(cursor[0], cursor[1], mon)
    if math.dist(start, tip) < 60:  # so it is seen to travel
        cx, cy = mon[0] + mon[2] / 2, mon[1] + mon[3] / 2
        start = clamp_point(tip[0] + (160 if tip[0] < cx else -160), tip[1] + (120 if tip[1] < cy else -120), mon)
    return start


def glide_seconds(a: tuple[float, float], b: tuple[float, float]) -> float:
    return _clamp(0.35 + math.dist(a, b) / 2200, 0.45, 0.95)


def glide_point(a: tuple[float, float], b: tuple[float, float], t: float) -> tuple[float, float]:
    """Position t (0-1) along a gently bowed path, easing in and out."""
    t = _clamp(t, 0.0, 1.0)
    e = 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2
    dx, dy = b[0] - a[0], b[1] - a[1]
    d = math.hypot(dx, dy)
    bow = min(70.0, d * 0.12) * math.sin(math.pi * e)
    nx, ny = (-dy / d, dx / d) if d else (0.0, 0.0)
    return a[0] + dx * e + nx * bow, a[1] + dy * e + ny * bow


# --------------------------------------------------------------------------- hand-drawn strokes
Stroke = list  # [(x, y), ...]


def _noise(seed: int):
    rng = random.Random(seed)
    a, b = rng.uniform(0, 6.28), rng.uniform(0, 6.28)
    f, g = rng.uniform(0.008, 0.016), rng.uniform(0.03, 0.05)
    return lambda d: 0.7 * math.sin(a + f * d) + 0.3 * math.sin(b + g * d)


def wobble_line(a: tuple[float, float], b: tuple[float, float], seed: int, amp: float, step: float = 6.0) -> Stroke:
    """A straight line as a steady hand would draw it: the ends land exactly, the middle drifts."""
    d = math.dist(a, b)
    n = min(800, max(2, int(d / max(step, 1.0))))
    nx, ny = (-(b[1] - a[1]) / d, (b[0] - a[0]) / d) if d else (0.0, 0.0)
    noise = _noise(seed)
    out = []
    for i in range(n + 1):
        t = i / n
        off = amp * noise(d * t) * math.sin(math.pi * t) ** 0.5
        out.append((a[0] + (b[0] - a[0]) * t + nx * off, a[1] + (b[1] - a[1]) * t + ny * off))
    out[-1] = (b[0], b[1])
    return out


def _ellipse(r: Rect, seed: int, amp: float) -> Stroke:
    cx, cy, rx, ry = r[0] + r[2] / 2, r[1] + r[3] / 2, r[2] / 2, r[3] / 2
    noise = _noise(seed)
    start = -2.4
    n = min(800, max(36, int((rx + ry) / 2.2)))
    out = []
    for i in range(n + 1):
        t = i / n
        ang = start + t * 2 * math.pi * 1.09  # overshoots the start, like a real circled word
        grow = 1 + 0.05 * t + amp * noise(t * 900) / max(rx + ry, 1)
        out.append((cx + rx * grow * math.cos(ang), cy + ry * grow * math.sin(ang)))
    return out


def box_strokes(r: Rect, seed: int, amp: float) -> list[Stroke]:
    x, y, w, h = r
    a, b, c, d = (x, y), (x + w, y), (x + w, y + h), (x, y + h)
    o = amp * 0.8
    return [wobble_line((a[0] - o, a[1]), (b[0] + o, b[1]), seed, amp),
            wobble_line((b[0], b[1] - o), (c[0], c[1] + o), seed + 1, amp),
            wobble_line((c[0] + o, c[1]), (d[0] - o, d[1]), seed + 2, amp),
            wobble_line((d[0], d[1] + o), (a[0], a[1] - o), seed + 3, amp)]


def arrow_strokes(a: tuple[float, float], b: tuple[float, float], seed: int, s: float) -> list[Stroke]:
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    hl = 17 * s
    wings = [(b[0] - hl * math.cos(ang + d), b[1] - hl * math.sin(ang + d)) for d in (-0.5, 0.5)]
    return [wobble_line(a, b, seed, 2.2 * s), wobble_line(wings[0], b, seed + 1, 0.8 * s),
            wobble_line(b, wings[1], seed + 2, 0.8 * s)]


def ink_strokes(mark: str, r: Rect, seed: int, s: float, tail: tuple[float, float] | None = None) -> list[Stroke]:
    x, y, w, h = r
    amp = 1.8 * s
    if mark == "circle":
        return [_ellipse(inflate(r, 9 * s), seed, amp * 2)]
    if mark == "underline":
        return [wobble_line((x - 4 * s, y + h + 5 * s), (x + w + 4 * s, y + h + 3 * s), seed, amp)]
    if mark == "box":
        return box_strokes(r, seed, amp)
    if mark == "cross":
        return [wobble_line((x, y), (x + w, y + h), seed, amp), wobble_line((x + w, y), (x, y + h), seed + 1, amp)]
    if mark == "check":
        side = max(min(w, h), 28 * s)
        cx, cy = x + w / 2, y + h / 2
        p = (cx - side * 0.45, cy + side * 0.02)
        q = (cx - side * 0.12, cy + side * 0.36)
        e = (cx + side * 0.5, cy - side * 0.42)
        return [wobble_line(p, q, seed, amp * 0.5) + wobble_line(q, e, seed + 1, amp)[1:]]
    if mark == "arrow":
        if tail is None:
            raise ShapeError("ink arrow needs a tail point")
        return arrow_strokes(tail, edge_point(r, tail), seed, s)
    raise ShapeError(f"unknown ink mark {mark!r}")


def stroke_length(st: Stroke) -> float:
    return sum(math.dist(st[i], st[i + 1]) for i in range(len(st) - 1))


def clip_strokes(strokes: list[Stroke], f: float) -> list[Stroke]:
    """The part of `strokes` drawn after fraction f of their total length, ending mid-segment."""
    if f >= 1:
        return strokes
    left = max(0.0, f) * sum(stroke_length(st) for st in strokes)
    out: list[Stroke] = []
    for st in strokes:
        if left <= 0:
            break
        part = [st[0]]
        for i in range(len(st) - 1):
            seg = math.dist(st[i], st[i + 1])
            if seg >= left:
                t = left / seg if seg else 0.0
                part.append((st[i][0] + (st[i + 1][0] - st[i][0]) * t, st[i][1] + (st[i + 1][1] - st[i][1]) * t))
                left = 0
                break
            part.append(st[i + 1])
            left -= seg
        out.append(part)
    return out


def strokes_bounds(strokes: list[Stroke], margin: float = 0) -> Rect:
    xs = [p[0] for st in strokes for p in st]
    ys = [p[1] for st in strokes for p in st]
    return inflate((min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)), margin)
