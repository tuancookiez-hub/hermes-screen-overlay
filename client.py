"""Hermes-side client: starts the overlay daemon on demand and talks to it over a named pipe."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN_NAME = "screen-overlay"
DAEMON_VERSION = "0.3.0"  # must match daemon/main.py; an older running daemon is replaced
DAEMON = Path(__file__).resolve().parent / "daemon" / "main.py"
START_TIMEOUT_S = 20.0
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
# Not DETACHED_PROCESS: a console launcher with no console makes its child allocate a new one,
# and on Windows 11 that opens a focused Windows Terminal window over whatever the user is doing.
SPAWN_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
SW_HIDE = 0


class OverlayUnavailable(RuntimeError):
    pass


def data_dir() -> Path:
    override = os.environ.get("SCREEN_OVERLAY_DATA_DIR")  # tests only
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME)


def _ident(d: Path) -> str:
    return hashlib.sha256(f"{os.environ.get('USERNAME', '')}|{d.resolve()}".encode()).hexdigest()[:16]


def pipe_name(d: Path) -> str:
    return rf"\\.\pipe\hermes-screen-overlay-{_ident(d)}"


def mutex_name(d: Path) -> str:
    return rf"Local\hermes-screen-overlay-{_ident(d)}"


def _key(d: Path) -> bytes:
    f = d / "ipc.key"
    try:
        return bytes.fromhex(f.read_text(encoding="ascii").strip()) or _new_key(f)
    except (OSError, ValueError):  # missing, empty or corrupt
        return _new_key(f)


def _new_key(f: Path) -> bytes:
    """Create the key file atomically, so two callers never read a half-written key."""
    tmp = f.with_name(f"{f.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(secrets.token_hex(32), encoding="ascii")
    try:
        try:
            os.link(tmp, f)  # fails if another caller got there first; then theirs wins
        except OSError:
            try:
                if bytes.fromhex(f.read_text(encoding="ascii").strip()):
                    return bytes.fromhex(f.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                pass
            os.replace(tmp, f)  # existing file is empty or corrupt
    finally:
        tmp.unlink(missing_ok=True)
    return bytes.fromhex(f.read_text(encoding="ascii").strip())


def _python() -> str:
    """Windowless pythonw.exe when available: a GUI-subsystem process cannot open a console."""
    exe = Path(sys.executable)
    dirs = [exe.parent] if exe.name.lower().startswith("python") else []
    dirs += [Path(sys.prefix) / "Scripts", Path(sys.prefix)]
    for d in dirs:
        for name in ("pythonw.exe", "python.exe"):
            if (d / name).exists():
                return str(d / name)
    raise OverlayUnavailable(f"cannot find a Python interpreter next to {exe}")


def daemon_env(base: dict | None = None, paths: list | None = None) -> dict:
    """Environment for the daemon. Hermes puts its dependency environment on sys.path at startup and
    clears PYTHONPATH, so a child interpreter would not find PySide6 unless we hand the path on."""
    env = dict(os.environ if base is None else base)
    for k in ("QT_SCALE_FACTOR", "QT_SCREEN_SCALE_FACTORS", "QT_QPA_PLATFORM", "QT_ENABLE_HIGHDPI_SCALING",
              "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(k, None)
    real = [str(p) for p in (sys.path if paths is None else paths) if p and os.path.isdir(p)]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(real))
    return env


def missing_dependencies() -> list[str]:
    return [m for m in ("PySide6", "comtypes") if importlib.util.find_spec(m) is None]


def request(payload: dict, timeout: float = 15.0) -> dict:
    """Send one request. Raises ConnectionError if the daemon is not running."""
    from multiprocessing.connection import Client

    d = data_dir()
    try:
        conn = Client(pipe_name(d), family="AF_PIPE", authkey=_key(d))
    except (FileNotFoundError, OSError) as exc:
        raise ConnectionError(str(exc)) from exc
    try:
        conn.send(payload)
        if not conn.poll(timeout):
            raise TimeoutError(f"overlay daemon did not answer within {timeout:.0f}s")
        reply = conn.recv()
    except EOFError as exc:
        raise ConnectionError("overlay daemon closed the connection") from exc
    finally:
        conn.close()
    return reply if isinstance(reply, dict) else {"ok": False, "error": "bad reply from daemon"}


def running() -> dict | None:
    try:
        return request({"op": "ping"}, timeout=2.0)
    except Exception:
        return None


def _retire_old_daemon(alive: dict) -> dict | None:
    """After a plugin update, a daemon from the old version may still be up and lack new operations."""
    if alive.get("version") == DAEMON_VERSION:
        return alive
    try:
        request({"op": "stop"}, timeout=3.0)
    except Exception:  # it died on its own
        pass
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and running():
        time.sleep(0.1)
    return None


def ensure(settings: dict) -> dict:
    """Ping, or start the daemon and wait until it answers. Pushes current settings."""
    alive = running()
    if alive:
        alive = _retire_old_daemon(alive)
    if alive:
        request({"op": "settings", "settings": settings}, timeout=3.0)
        return alive
    missing = missing_dependencies()
    if missing:
        raise OverlayUnavailable(
            f"missing Python packages in this Hermes process: {', '.join(missing)}. If the plugin was just "
            "enabled, a running gateway keeps its old dependency environment until it is restarted "
            "(hermes gateway restart). Otherwise run: hermes plugins enable screen-overlay")
    d = data_dir()
    _key(d)
    err = open(d / "daemon.stderr.log", "w", encoding="utf-8")
    hidden = subprocess.STARTUPINFO()
    hidden.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    hidden.wShowWindow = SW_HIDE
    try:
        proc = subprocess.Popen(
            [_python(), str(DAEMON), "--data-dir", str(d), "--pipe", pipe_name(d), "--mutex", mutex_name(d),
             "--settings", json.dumps(settings)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, env=daemon_env(), close_fds=True,
            creationflags=SPAWN_FLAGS, startupinfo=hidden,
        )
    finally:
        err.close()
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        alive = running()
        if alive:
            return alive
        if proc.poll() is not None:
            # another daemon won the race, or startup crashed
            alive = running()
            if alive:
                return alive
            tail = (d / "daemon.stderr.log").read_text(encoding="utf-8", errors="replace")[-600:]
            raise OverlayUnavailable(f"overlay daemon exited during startup: {tail.strip() or 'no output'}")
        time.sleep(0.15)
    raise OverlayUnavailable(f"overlay daemon did not start within {START_TIMEOUT_S:.0f}s")


# --------------------------------------------------------------------------- saved guides
def _guides_dir() -> Path:
    d = data_dir() / "guides"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_guide(slug: str, guide: dict) -> None:
    f = _guides_dir() / f"{slug}.json"
    tmp = f.with_name(f"{f.stem}.{os.getpid()}.{secrets.token_hex(4)}.tmp")  # unique: concurrent saves
    try:
        tmp.write_text(json.dumps(guide, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, f)
    finally:
        tmp.unlink(missing_ok=True)


def load_guide(slug: str) -> dict | None:
    f = _guides_dir() / f"{slug}.json"
    if not f.exists():
        return None
    try:
        g = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # corrupt or unreadable: treat as missing rather than crash
        return None
    return g if isinstance(g, dict) else None


def delete_guide(slug: str) -> bool:
    f = _guides_dir() / f"{slug}.json"
    if not f.exists():
        return False
    f.unlink()
    return True


def list_guides() -> list[dict]:
    out = []
    for f in sorted(_guides_dir().glob("*.json")):
        try:
            g = json.loads(f.read_text(encoding="utf-8"))
            out.append({"name": f.stem, "title": str(g.get("title", "")), "steps": len(g.get("steps", []))})
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return out


def call(payload: dict, settings: dict, start: bool = True, timeout: float = 15.0) -> dict[str, Any]:
    if start:
        ensure(settings)
    elif running() is None:
        return {"ok": True, "running": False, "note": "overlay is not running; nothing on screen"}
    try:
        return request(payload, timeout=timeout)
    except ConnectionError:  # the daemon exited (idle, hotkey, stop) between the ping and the request
        if not start:
            return {"ok": True, "running": False, "note": "overlay is not running; nothing on screen"}
        ensure(settings)
        return request(payload, timeout=timeout)
