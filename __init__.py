"""screen-overlay: Hermes points at things on your real Windows screen and walks you through them."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import client
from .tool import SCHEMA, is_windows, make_handler

log = logging.getLogger(__name__)

DEFAULTS = {"exclude_from_capture": True, "default_ttl_seconds": 300, "idle_exit_minutes": 10}


def _settings_reader(ctx):
    def read() -> dict:
        out = dict(DEFAULTS)
        for k, default in DEFAULTS.items():
            try:
                v = ctx.get_config(k, default)
            except Exception:
                v = default
            if isinstance(default, bool):
                out[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            else:
                try:
                    out[k] = max(0, int(v))
                except (TypeError, ValueError):
                    out[k] = default
        return out
    return read


def _on_session_finalize(session_id=None, **_kw) -> None:
    """Session torn down: remove that session's marks only, if the daemon is up. Never starts it.

    Not on_session_end: despite the name that fires after every turn, which would wipe marks the
    moment Hermes replies. A one-shot run exits right after this hook, so the call is synchronous.
    """
    if not session_id or not is_windows():
        return
    try:
        client.request({"op": "clear", "owner": session_id}, timeout=2.0)
    except Exception as exc:  # daemon not running is the normal case
        log.debug("screen-overlay: finalize clear skipped: %s", exc)


def _command(raw: str = "", **_kw) -> str:
    """/overlay [clear|status|stop|next|back]"""
    if not is_windows():
        return "screen-overlay only works on Windows."
    sub = (raw or "clear").strip().lower() or "clear"
    if sub not in ("clear", "status", "stop", "next", "back"):
        return "Usage: /overlay clear | status | stop | next | back"
    op = {"op": "guide_move", "to": sub} if sub in ("next", "back") else {"op": sub}
    try:
        res = client.call(op, {}, start=False, timeout=5.0)
    except Exception as exc:
        return f"screen-overlay: {exc}"
    if sub == "status" and res.get("visible_shapes") is not None:
        return (f"Overlay running (pid {res.get('pid')}), {len(res['visible_shapes'])} mark(s) on screen, "
                f"hotkey {res.get('emergency_hotkey') or 'unavailable'}.")
    return json.dumps(res)


def register(ctx) -> None:
    ctx.register_tool(
        name="screen_overlay",
        toolset="screen_overlay",
        schema=SCHEMA,
        handler=make_handler(_settings_reader(ctx)),
        check_fn=is_windows,
        description=SCHEMA["description"],
        emoji="🎯",
    )
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    ctx.register_command("overlay", _command, description="Clear, inspect or stop the screen overlay",
                         args_hint="clear|status|stop|next|back")
    ctx.register_skill("screen-overlay", Path(__file__).parent / "skills" / "screen-overlay" / "SKILL.md",
                       description="How to point at things on the user's Windows screen with screen_overlay")
