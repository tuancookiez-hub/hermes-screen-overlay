"""The `screen_overlay` tool: schema and handler."""
from __future__ import annotations

import json
import logging
import math
import sys
from typing import Any, Callable

from . import client
from .shapes import (GUIDE_MARKS, INK_MARKS, PALETTE, PLACEMENTS, SHAPE_TYPES, ShapeError, guide_slug, normalize_guide,
                     normalize_shapes, normalize_space)

log = logging.getLogger(__name__)

TARGET = {
    "type": "object",
    "description": (
        "Exactly one of: rect [x,y,w,h] | point [x,y] (physical screen pixels, or screenshot pixels when "
        "`space` is given) | window (title substring, process name like 'notepad.exe', or hwnd) | "
        "element {window?, name?, control_type?, automation_id?, index?} found live through UI Automation "
        "(name is a case-insensitive substring; window defaults to the foreground window)."
    ),
    "properties": {
        "rect": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "point": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
        "window": {"type": ["string", "integer"]},
        "element": {
            "type": "object",
            "properties": {
                "window": {"type": ["string", "integer"]},
                "name": {"type": "string"},
                "control_type": {"type": "string", "description": "e.g. Button, MenuItem, Edit, TabItem, ListItem"},
                "automation_id": {"type": "string"},
                "index": {"type": "integer", "description": "Which match to use, 0 = best"},
            },
        },
    },
}

SCHEMA: dict[str, Any] = {
    "name": "screen_overlay",
    "description": (
        "Draw on the user's real Windows screen to show them where to look or click, and teach with it: "
        "spotlight, box, arrow, label, numbered step, a drawn pointer that glides to a target, handwritten "
        "glass notes, ink marks and small flow diagrams. A guide is a floating panel that walks the user "
        "through steps with Next and Back. Marks are click-through and nothing ever takes keyboard focus; "
        "marks clear after ttl_seconds, and the user can clear everything with Ctrl+Alt+Shift+F12. "
        "Prefer element targets (UI Automation, pixel-exact, survives DPI scaling) over coordinates: call "
        "inspect what='find' first when unsure of the name. Use `space` only for coordinates read off a "
        "screenshot. Actions: inspect (screens | windows | element_at | find), show, guide, guide_move, "
        "guides, clear, status, stop. Load skill 'screen-overlay:screen-overlay' for worked examples."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["inspect", "show", "guide", "guide_move", "guides", "clear",
                                                  "status", "stop"]},
            "what": {"type": "string", "enum": ["screens", "windows", "element_at", "find"],
                     "description": "inspect only"},
            "window": {"type": ["string", "integer"],
                       "description": "inspect windows/find: title substring, process name, or hwnd"},
            "name": {"type": "string", "description": "inspect find: element name substring"},
            "control_type": {"type": "string", "description": "inspect find: e.g. Button"},
            "automation_id": {"type": "string", "description": "inspect find"},
            "x": {"type": "number", "description": "inspect element_at: physical screen x"},
            "y": {"type": "number", "description": "inspect element_at: physical screen y"},
            "limit": {"type": "integer", "description": "inspect: max results"},
            "shapes": {
                "type": "array",
                "description": "show: marks to draw (max 24)",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(SHAPE_TYPES)},
                        "target": TARGET,
                        "text": {"type": "string", "description": ("Caption (required for label). For note: the "
                                                                   "note, up to 800 chars; lines starting '- ' are "
                                                                   "bullets.")},
                        "title": {"type": "string", "description": "note: heading"},
                        "mark": {"type": "string", "enum": list(INK_MARKS), "description": "ink: what to draw"},
                        "nodes": {"type": "array", "items": {"type": "string"},
                                  "description": "flow: 2-6 short box labels, joined by arrows in order"},
                        "direction": {"type": "string", "enum": ["right", "down"], "description": "flow layout"},
                        "number": {"type": "integer", "description": "step: badge number (auto 1, 2, 3...)"},
                        "color": {"type": "string", "description": f"{', '.join(PALETTE)} or #RRGGBB"},
                        "placement": {"type": "string", "enum": list(PLACEMENTS)},
                        "pad": {"type": "number", "description": "Space around the target, px (default 6)"},
                        "from": {"type": "array", "items": {"type": "number"},
                                 "description": "arrow, ink arrow: tail point; default picks one automatically"},
                        "id": {"type": "string", "description": "Name it to replace or clear it later"},
                    },
                    "required": ["type"],
                },
            },
            "steps": {
                "type": "array",
                "description": "guide: up to 20 steps, shown one at a time",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "What to do, short"},
                        "text": {"type": "string", "description": "More detail, optional"},
                        "target": TARGET,
                        "mark": {"type": "string", "enum": list(GUIDE_MARKS),
                                 "description": "How to mark the target (default step)"},
                        "color": {"type": "string"},
                        "pad": {"type": "number"},
                    },
                },
            },
            "title": {"type": "string", "description": "guide: name shown on the panel"},
            "pointer": {"type": "boolean", "description": "guide: glide a drawn pointer to each step (default true)"},
            "save_as": {"type": "string", "description": "guide: also keep it under this name to replay later"},
            "guide_name": {"type": "string", "description": "guide: replay a saved guide instead of giving steps"},
            "to": {"type": ["string", "integer"],
                   "description": "guide_move: 'next', 'back', or a step number from 1. 'next' on the last step finishes."},
            "delete": {"type": "string", "description": "guides: remove the saved guide with this name"},
            "space": {
                "type": "object",
                "description": ("show: rect/point/from are in screenshot pixels. frame_width/frame_height = size "
                                "of the image you measured on; region = what it covered: 'primary' (default), "
                                "'virtual', {monitor: n}, or [x,y,w,h] physical."),
                "properties": {"frame_width": {"type": "number"}, "frame_height": {"type": "number"},
                               "region": {}},
            },
            "replace": {"type": "boolean", "description": "show: clear existing marks first (default true)"},
            "ttl_seconds": {"type": "number", "description": ("show: auto-clear after this long; 0 = never. guide: "
                                                              "end after this long with no Next or Back (default 30 min)")},
            "ids": {"type": "array", "items": {"type": "string"}, "description": "clear: only these ids"},
        },
        "required": ["action"],
    },
}


def _ok(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False)


def _err(msg: str, **extra) -> str:
    return json.dumps({"ok": False, "error": msg, **extra}, ensure_ascii=False)


def is_windows() -> bool:
    return sys.platform == "win32"


def _ttl_ok(ttl: Any) -> bool:
    return ttl is None or (not isinstance(ttl, bool) and isinstance(ttl, (int, float)) and math.isfinite(ttl)
                           and ttl >= 0)


def _guide(args: dict, settings: dict, session_id: str | None) -> dict:
    if args.get("guide_name"):
        slug = guide_slug(args["guide_name"])
        saved = client.load_guide(slug)
        if saved is None:
            return {"ok": False, "error": f"no saved guide named {slug!r}", "saved": client.list_guides()}
        steps, title, space = saved.get("steps"), saved.get("title"), saved.get("space")
    else:
        steps, title, space = args.get("steps"), args.get("title"), args.get("space")
    normalize_guide(steps, title)  # fail fast, before starting anything
    normalize_space(space)
    ttl = args.get("ttl_seconds")
    if not _ttl_ok(ttl):
        return {"ok": False, "error": "ttl_seconds must be a number >= 0"}
    slug = guide_slug(args["save_as"]) if args.get("save_as") else None  # validate before starting anything
    res = client.call({"op": "guide", "steps": steps, "title": title, "space": space, "ttl_seconds": ttl,
                       "pointer": args.get("pointer", True), "owner": session_id}, settings)
    if res.get("ok") and slug:
        try:
            client.save_guide(slug, {"title": title or "", "steps": steps, "space": space})
            res["saved_as"] = slug
        except OSError as exc:  # the guide is already running; do not report that as a failure
            res["save_error"] = f"could not save the guide: {exc}"
    return res


def make_handler(get_settings: Callable[[], dict]):
    def handle(args: dict, **kw) -> str:
        if not is_windows():
            return _err("screen_overlay only works on Windows")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _err("arguments must be an object with an action")
        action = args.get("action")
        try:
            settings = get_settings()
            if action == "inspect":
                payload = {"op": "inspect", **{k: args[k] for k in
                           ("what", "window", "name", "control_type", "automation_id", "x", "y", "limit")
                           if args.get(k) is not None}}
                return _ok(client.call(payload, settings))
            if action == "show":
                normalize_shapes(args.get("shapes"))  # fail fast, before starting anything
                normalize_space(args.get("space"))
                ttl = args.get("ttl_seconds")
                if not _ttl_ok(ttl):
                    return _err("ttl_seconds must be a number >= 0")
                payload = {"op": "show", "shapes": args["shapes"], "space": args.get("space"),
                           "replace": args.get("replace", True), "ttl_seconds": ttl,
                           "owner": kw.get("session_id") or None}
                return _ok(client.call(payload, settings))
            if action == "guide":
                return _ok(_guide(args, settings, kw.get("session_id")))
            if action == "guide_move":
                return _ok(client.call({"op": "guide_move", "to": args.get("to", "next")}, settings, start=False))
            if action == "guides":
                if args.get("delete"):
                    return _ok({"ok": True, "deleted": client.delete_guide(guide_slug(args["delete"]))})
                return _ok({"ok": True, "guides": client.list_guides()})
            if action == "clear":
                return _ok(client.call({"op": "clear", "ids": args.get("ids")}, settings, start=False))
            if action == "status":
                return _ok(client.call({"op": "status"}, settings, start=False))
            if action == "stop":
                return _ok(client.call({"op": "stop"}, settings, start=False))
            return _err("action must be one of inspect, show, guide, guide_move, guides, clear, status, stop")
        except ShapeError as exc:
            return _err(str(exc))
        except client.OverlayUnavailable as exc:
            return _err(str(exc))
        except Exception as exc:  # never raise into Hermes
            log.exception("screen_overlay failed")
            return _err(f"{type(exc).__name__}: {exc}")

    return handle
