import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("shapes", ROOT / "shapes.py")
S = importlib.util.module_from_spec(spec)
sys.modules["shapes"] = S
spec.loader.exec_module(S)

MONS = [
    {"index": 0, "rect": [0, 0, 3440, 1440], "work": [0, 0, 3440, 1392], "scale": 1.0, "primary": True},
    {"index": 1, "rect": [3440, 0, 1920, 1080], "work": [3440, 0, 1920, 1032], "scale": 1.5, "primary": False},
]


def test_colors():
    assert S.color(None) == S.PALETTE["orange"]
    assert S.color("Blue") == S.PALETTE["blue"]
    assert S.color("#abc") == "#AABBCC"
    with pytest.raises(S.ShapeError):
        S.color("chartreuse")


def test_step_numbers_autoincrement_and_continue_after_explicit():
    out = S.normalize_shapes([
        {"type": "step", "target": {"point": [1, 2]}},
        {"type": "step", "target": {"point": [1, 2]}, "number": 5},
        {"type": "step", "target": {"point": [1, 2]}},
    ])
    assert [s["number"] for s in out] == [1, 5, 6]


@pytest.mark.parametrize("bad", [
    [],
    [{"type": "circle", "target": {"point": [0, 0]}}],
    [{"type": "box", "target": {}}],
    [{"type": "box", "target": {"rect": [0, 0, 1], "point": [0, 0]}}],
    [{"type": "box", "target": {"rect": [0, 0, -1, 4]}}],
    [{"type": "label", "target": {"point": [0, 0]}}],
    [{"type": "box", "target": {"element": {"window": "x"}}}],
    [{"type": "box", "target": {"point": [0, float("nan")]}}],
    [{"type": "box", "target": {"window": True}}],
])
def test_rejects_bad_shapes(bad):
    with pytest.raises(S.ShapeError):
        S.normalize_shapes(bad)


def test_too_many_shapes():
    with pytest.raises(S.ShapeError):
        S.normalize_shapes([{"type": "box", "target": {"point": [0, 0]}}] * 25)


def test_space_mapping_primary_and_monitor():
    sp = S.normalize_space({"frame_width": 1280, "frame_height": 536})
    region = S.frame_region(sp, MONS, (0, 0, 5360, 1440))
    assert region == (0, 0, 3440, 1440)
    assert S.map_rect_from_frame([640, 268, 128, 53.6], sp, region) == (1720, 720, 344, 144)
    sp2 = S.normalize_space({"frame_width": 1280, "frame_height": 720, "region": {"monitor": 1}})
    assert S.frame_region(sp2, MONS, (0, 0, 5360, 1440)) == (3440, 0, 1920, 1080)
    assert S.map_from_frame(0, 0, sp2, (3440, 0, 1920, 1080)) == (3440, 0)
    with pytest.raises(S.ShapeError):
        S.normalize_space({"frame_width": 0, "frame_height": 10})


def test_monitor_for_prefers_centre_then_overlap():
    assert S.monitor_for((3500, 100, 50, 50), MONS)["index"] == 1
    assert S.monitor_for((3400, 100, 60, 10), MONS)["index"] == 0  # centre at 3430
    assert S.monitor_for((-500, -500, 10, 10), MONS)["primary"]


def test_place_label_flips_when_no_room_and_stays_inside():
    area = (0, 0, 1000, 800)
    below = S.place_label((100, 100, 50, 20), (80, 30), area)
    assert below[1] > 120
    above = S.place_label((100, 760, 50, 30), (80, 30), area)
    assert above[1] + above[3] <= 760
    huge = S.place_label((0, 0, 1000, 800), (300, 40), area)
    assert huge[0] >= 0 and huge[1] >= 0 and huge[0] + huge[2] <= 1000 and huge[1] + huge[3] <= 800


def test_arrow_tail_points_toward_monitor_centre_and_stays_on_screen():
    area = (0, 0, 1000, 800)
    tx, ty = S.arrow_tail((900, 700, 40, 40), area, 170)
    assert tx < 900 and ty < 700
    tx, ty = S.arrow_tail((0, 0, 10, 10), area, 170)
    assert tx > 10 and ty > 10
    hx, hy = S.edge_point((100, 100, 100, 50), (500, 125))
    assert (hx, hy) == (200, 125)


def test_after_clear_scopes_by_owner_and_ids():
    items = [{"id": "a", "owner": "s1"}, {"id": "b", "owner": "s2"}, {"id": "c", "owner": None}]
    assert S.after_clear(items) == []
    assert [i["id"] for i in S.after_clear(items, owner="s1")] == ["b", "c"]
    assert [i["id"] for i in S.after_clear(items, ids=["b"])] == ["a", "c"]
    assert [i["id"] for i in S.after_clear(items, ids=["a"], owner="s2")] == ["a", "b", "c"]


def test_huge_and_nonfinite_coordinates_are_rejected():
    for bad in (1e12, float("inf"), float("nan")):
        with pytest.raises(S.ShapeError):
            S.normalize_shapes([{"type": "box", "target": {"rect": [0, 0, bad, 10]}}])
        with pytest.raises(S.ShapeError):
            S.normalize_space({"frame_width": bad, "frame_height": 100})
    with pytest.raises(S.ShapeError):
        S.normalize_space({"frame_width": 1920, "frame_height": 1080, "region": [0, 0, 1e9, 10]})
    assert S.normalize_space({"frame_width": 1920, "frame_height": 1080})["frame_width"] == 1920.0


def test_guide_slug_avoids_windows_device_names():
    assert S.guide_slug("CON") == "guide-con"
    assert S.guide_slug("lpt1") == "guide-lpt1"
    assert S.guide_slug("My Guide!") == "my-guide"


def test_stroke_point_count_is_bounded():
    assert len(S.wobble_line((0, 0), (1e6, 0), 1, 2.0)) <= 801
    assert len(S.ink_strokes("circle", (0, 0, 1e6, 1e6), 1, 1.0)[0]) <= 801


def test_sticky_notes_are_validated():
    out = S.normalize_stickies([{"text": "  buy milk  "}, {"text": "x", "title": "T", "color": "blue", "id": "a",
                                                           "target": {"point": [5, 6]}}])
    assert out[0] == {"id": "sticky1", "text": "buy milk", "title": "", "color": S.PALETTE["white"], "target": None}
    assert out[1]["id"] == "a" and out[1]["color"] == S.PALETTE["blue"] and out[1]["target"] == {"point": [5.0, 6.0]}
    assert len(S.normalize_stickies([{"text": "y" * 5000}])[0]["text"]) == S.MAX_STICKY_TEXT
    for bad in (None, [], "x", [1], [{}], [{"text": "   "}], [{"text": 5}], [{"text": "a", "title": 3}],
                [{"text": "a", "color": "nope"}], [{"text": "a", "id": "d"}, {"text": "b", "id": "d"}],
                [{"text": "a", "target": {"rect": [0, 0, float("inf"), 1]}}],
                [{"text": "a"}] * (S.MAX_STICKIES + 1)):
        with pytest.raises(S.ShapeError):
            S.normalize_stickies(bad)


def test_sticky_spot_sits_beside_its_target_or_in_a_free_corner():
    work = (0, 0, 1000, 800)
    assert S.sticky_spot((200, 100), (100, 100, 50, 20), work, []) == (170, 100, 200, 100)  # right of it
    assert S.sticky_spot((200, 100), (850, 100, 100, 20), work, []) == (630, 100, 200, 100)  # no room: left
    first = S.sticky_spot((200, 100), None, work, [])
    second = S.sticky_spot((200, 100), None, work, [first])
    assert first != second and not S.intersects(first, second)
    for r in (first, second, S.sticky_spot((200, 100), (100, 100, 50, 20), work, [(170, 100, 200, 100)])):
        assert 0 <= r[0] and r[0] + r[2] <= 1000 and 0 <= r[1] and r[1] + r[3] <= 800
    busy = S.sticky_spot((200, 100), (400, 300, 50, 20), work, [(470, 300, 200, 100)])
    assert not S.intersects(busy, (470, 300, 200, 100))
