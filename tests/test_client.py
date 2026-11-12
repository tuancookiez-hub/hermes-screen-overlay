import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("overlay_client", ROOT / "client.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)


def test_daemon_env_hands_on_sys_path_and_drops_overrides(tmp_path):
    env = C.daemon_env({"PATH": "x", "PYTHONHOME": "bad", "VIRTUAL_ENV": "bad", "QT_SCALE_FACTOR": "2"},
                       [str(tmp_path), str(tmp_path), "__editable__.finder", "", str(tmp_path / "gone")])
    assert env["PYTHONPATH"] == str(tmp_path)
    assert env["PATH"] == "x"
    for k in ("PYTHONHOME", "VIRTUAL_ENV", "QT_SCALE_FACTOR"):
        assert k not in env


def test_pipe_name_depends_on_data_dir(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert C.pipe_name(a) != C.pipe_name(b)
    assert C.pipe_name(a).endswith(C.pipe_name(a).rsplit("-", 1)[1]) and "hermes-screen-overlay-" in C.pipe_name(a)


def test_spawn_never_detaches_a_console_process():
    assert C.SPAWN_FLAGS & C.CREATE_NO_WINDOW
    assert not C.SPAWN_FLAGS & 0x00000008  # DETACHED_PROCESS opens a terminal for console launchers


def test_prefers_windowless_python(tmp_path, monkeypatch):
    (tmp_path / "python.exe").write_text("")
    monkeypatch.setattr(C.sys, "executable", str(tmp_path / "python.exe"))
    assert C._python().endswith("python.exe")
    (tmp_path / "pythonw.exe").write_text("")
    assert C._python().endswith("pythonw.exe")


def test_guide_storage_survives_corrupt_files(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREEN_OVERLAY_DATA_DIR", str(tmp_path))
    C.save_guide("ok", {"title": "T", "steps": [{"title": "a"}]})
    g = C._guides_dir()
    (g / "bad.json").write_text("{not json", encoding="utf-8")
    (g / "list.json").write_text("[1, 2]", encoding="utf-8")
    (g / "steps.json").write_text('{"steps": 5}', encoding="utf-8")
    assert C.load_guide("bad") is None and C.load_guide("list") is None and C.load_guide("missing") is None
    assert C.load_guide("ok")["title"] == "T"
    assert [x["name"] for x in C.list_guides()] == ["ok"]
    assert list(g.glob("*.tmp")) == []  # no temp files left behind
    assert C.delete_guide("ok") and not C.delete_guide("ok")


def test_ipc_key_is_created_once_and_repaired(tmp_path):
    k = C._key(tmp_path)
    assert len(k) == 32 and C._key(tmp_path) == k
    (tmp_path / "ipc.key").write_text("", encoding="ascii")
    k2 = C._key(tmp_path)
    assert len(k2) == 32 and C._key(tmp_path) == k2
    (tmp_path / "ipc.key").write_text("zz-not-hex", encoding="ascii")
    assert len(C._key(tmp_path)) == 32
    assert list(tmp_path.glob("*.tmp")) == []


def test_call_restarts_daemon_that_exited_between_calls(monkeypatch):
    calls = []

    def fake_request(payload, timeout=15.0):
        calls.append(payload)
        if len(calls) == 1:
            raise ConnectionError("gone")
        return {"ok": True}

    monkeypatch.setattr(C, "ensure", lambda settings: {"ok": True})
    monkeypatch.setattr(C, "request", fake_request)
    assert C.call({"op": "x"}, {}, start=True) == {"ok": True} and len(calls) == 2
    calls.clear()
    monkeypatch.setattr(C, "running", lambda: {"ok": True})
    assert C.call({"op": "x"}, {}, start=False)["running"] is False


def test_daemon_env_passes_no_secrets():
    """The overlay process must never inherit Hermes's provider keys or tokens."""
    base = {"Path": r"C:\Windows", "SystemRoot": r"C:\Windows", "TEMP": "t", "TMP": "t", "USERNAME": "u",
            "USERPROFILE": "p", "LOCALAPPDATA": "l", "APPDATA": "a", "windir": r"C:\Windows", "SYSTEMDRIVE": "C:",
            "OPENAI_API_KEY": "sk-x", "ANTHROPIC_API_KEY": "sk-y", "OPENROUTER_API_KEY": "z", "FISH_API_KEY": "f",
            "GITHUB_TOKEN": "g", "HERMES_HOME": "h", "AWS_SECRET_ACCESS_KEY": "s", "DATABASE_URL": "d",
            "PYTHONHOME": "bad", "QT_QPA_PLATFORM": "offscreen", "SOMETHING_ELSE": "1"}
    env = C.daemon_env(base, [])
    assert set(k.upper() for k in env) == set(C.DAEMON_ENV_ALLOW) | {"PYTHONPATH"}
    assert env["Path"] == r"C:\Windows" and env["SystemRoot"] == r"C:\Windows"  # names keep their own case
    assert not any(any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "HERMES", "URL")) for k in env)


def test_daemon_env_uses_the_real_environment_by_default(monkeypatch):
    monkeypatch.setenv("OVERLAY_TEST_SECRET_KEY", "nope")
    env = C.daemon_env()
    assert "OVERLAY_TEST_SECRET_KEY" not in env
    assert all(k.upper() in C.DAEMON_ENV_ALLOW or k == "PYTHONPATH" for k in env)
