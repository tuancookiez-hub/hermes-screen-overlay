import importlib.util
import math
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("shapes", ROOT / "shapes.py")
S = importlib.util.module_from_spec(spec)
sys.modules["shapes"] = S
spec.loader.exec_module(S)

MON = (3440, 0, 1920, 1080)


def test_note_and_flow_may_omit_target_but_others_may_not():
    out = S.normalize_shapes([
        {"type": "note", "text": "hello\n- a bullet", "title": "Plan"},
        {"type": "flow", "nodes": ["a", "b"], "direction": "down"},
    ])
    assert out[0]["target"] is None and out[0]["title"] == "Plan" and out[0]["color"] == S.PALETTE["white"]
    assert out[1]["nodes"] == ["a", "b"] and out[1]["direction"] == "down"
    with pytest.raises(S.ShapeError):
        S.normalize_shapes([{"type": "pointer"}])


@pytest.mark.parametrize("bad", [
    {"type": "note"},
    {"type": "ink", "target": {"point": [0, 0]}},
    {"type": "ink", "target": {"point": [0, 0]}, "mark": "scribble"},
    {"type": "flow", "nodes": ["only one"]},
    {"type": "flow", "nodes": ["a", "b"], "direction": "up"},
    {"type": "flow", "nodes": ["a", ""]},
])
def test_rejects_bad_glass_shapes(bad):
    with pytest.raises(S.ShapeError):
        S.normalize_shapes([bad])


def test_note_keeps_long_text_other_shapes_do_not():
    long = "x" * 2000
    assert len(S.normalize_shapes([{"type": "note", "text": long}])[0]["text"]) == S.MAX_NOTE
    assert len(S.normalize_shapes([{"type": "label", "text": long, "target": {"point": [0, 0]}}])[0]["text"]) == S.MAX_TEXT


def test_guide_normalizes_and_allows_untargeted_steps():
    g = S.normalize_guide([{"title": "Open it", "target": {"window": "Notepad"}}, {"text": "Wait a moment"}], "Setup")
    assert g["title"] == "Setup"
    assert g["steps"][0]["mark"] == "step" and g["steps"][1]["target"] is None


@pytest.mark.parametrize("bad", [
    [],
    [{}],
    [{"title": "x", "mark": "glow"}],
    [{"title": "x", "target": {}}],
    [{"title": 3}],
    [{"title": "x"}] * (S.MAX_STEPS + 1),
])
def test_guide_rejects_bad_steps(bad):
    with pytest.raises(S.ShapeError):
        S.normalize_guide(bad)


def test_guide_slug_is_file_safe():
    assert S.guide_slug("Turn on Bluetooth!") == "turn-on-bluetooth"
    assert S.guide_slug("../../etc/passwd") == "etc-passwd"
    with pytest.raises(S.ShapeError):
        S.guide_slug("../..")


def test_pointer_start_never_leaves_the_target_monitor():
    tip = (4000.0, 500.0)
    x, y = S.pointer_start(tip, MON, None, (100, 700))  # real cursor on the game monitor
    assert S.contains_point(MON, x, y) and x == MON[0]
    assert S.pointer_start(tip, MON, (4500, 900), (100, 700)) == (4500.0, 900.0)  # continues from last step


def test_pointer_always_travels():
    tip = (4000.0, 500.0)
    assert math.dist(S.pointer_start(tip, MON, None, (4001, 501)), tip) > 100


def test_pointer_flips_near_edges():
    assert S.pointer_flip((3500, 100), MON, 1.0) == (1, 1)
    assert S.pointer_flip((5355, 1075), MON, 1.0) == (-1, -1)
    x, y, w, h = S.pointer_rect((5355, 1075), (-1, -1), 1.0)
    assert x + w <= MON[0] + MON[2] and y + h <= MON[1] + MON[3]


def test_glide_starts_and_lands_exactly():
    a, b = (100.0, 100.0), (900.0, 400.0)
    assert S.glide_point(a, b, 0) == a
    assert S.glide_point(a, b, 1) == pytest.approx(b)
    mid = S.glide_point(a, b, 0.5)
    chord = (500.0, 250.0)
    assert 0 < math.dist(mid, chord) <= 70.1  # bowed, not straight


def test_wobble_is_repeatable_and_lands_on_both_ends():
    a, b = (0.0, 0.0), (300.0, 40.0)
    one, two = S.wobble_line(a, b, 7, 3), S.wobble_line(a, b, 7, 3)
    assert one == two and one[0] == pytest.approx(a) and one[-1] == b


@pytest.mark.parametrize("mark", ["circle", "underline", "box", "cross", "check"])
def test_ink_marks_draw_near_their_target(mark):
    r = (500, 300, 120, 40)
    strokes = S.ink_strokes(mark, r, 3, 1.0)
    assert S.intersects(S.strokes_bounds(strokes, 2), S.inflate(r, 30))


def test_ink_arrow_lands_on_target_edge():
    r = (500.0, 300.0, 100.0, 50.0)
    strokes = S.ink_strokes("arrow", r, 3, 1.0, (200.0, 100.0))
    end = strokes[0][-1]
    assert end == pytest.approx(S.edge_point(r, (200.0, 100.0)))


def test_clip_strokes_grows_with_progress_and_finishes_whole():
    strokes = S.box_strokes((0, 0, 100, 60), 1, 1.5)
    total = sum(S.stroke_length(st) for st in strokes)
    half = sum(S.stroke_length(st) for st in S.clip_strokes(strokes, 0.5))
    assert half == pytest.approx(total / 2, rel=0.01)
    assert S.clip_strokes(strokes, 1) == strokes
    assert S.clip_strokes(strokes, 0) == []


def test_place_free_avoids_taken_and_stays_inside():
    area = (0, 0, 1000, 800)
    first = S.place_free((300, 200), area, [])
    second = S.place_free((300, 200), area, [first])
    assert not S.intersects(first, second)
    for r in (first, second):
        assert r[0] >= 0 and r[1] >= 0 and r[0] + r[2] <= 1000 and r[1] + r[3] <= 800


def test_place_free_moves_panel_off_the_target():
    area = (0, 0, 1000, 800)
    target = (600, 500, 400, 300)  # bottom-right corner
    spot = S.place_free((340, 180), area, [target], order=("br", "bl", "tr", "tl"))
    assert not S.intersects(spot, target)


TWO = [{"index": 0, "rect": [0, 0, 3440, 1440], "primary": True}, {"index": 1, "rect": [3440, 0, 1920, 1080], "primary": False}]


def test_free_notes_avoid_a_fullscreen_game_under_the_mouse():
    assert S.free_monitor(TWO, (100, 100))["index"] == 0
    assert S.free_monitor(TWO, (100, 100), (0, 0, 3440, 1440))["index"] == 1
    assert S.free_monitor(TWO, (100, 100), (50, 50, 800, 600))["index"] == 0  # an ordinary window
    assert S.free_monitor(TWO[:1], (100, 100), (0, 0, 3440, 1440))["index"] == 0  # nowhere else to go


def test_free_notes_stay_with_the_other_marks():
    assert S.free_monitor(TWO, (100, 100), None, (4000, 300, 50, 50))["index"] == 1


def test_client_and_daemon_versions_match():
    daemon = re.search(r'^VERSION = "(.+)"', (ROOT / "daemon" / "main.py").read_text(encoding="utf-8"), re.M).group(1)
    client = re.search(r'^DAEMON_VERSION = "(.+)"', (ROOT / "client.py").read_text(encoding="utf-8"), re.M).group(1)
    manifest = re.search(r"^version: (.+)$", (ROOT / "plugin.yaml").read_text(encoding="utf-8"), re.M).group(1)
    assert daemon == client == manifest.strip()
