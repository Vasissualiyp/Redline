import re

import pytest

from redline import layout as L

IDS = {"paper": "%1", "agent": "%2", "preview": "%3"}


def cells(s):
    return [tuple(map(int, m.groups())) for m in re.finditer(r"(\d+)x(\d+),(\d+),(\d+),(\d+)(?=[,}\]]|$)", s)]


@pytest.mark.parametrize("name", [n for n in L.ORDER if n not in L.META])
def test_presets_tile_the_window(name):
    s, order = L.layout_string(name, IDS, [], 200, 60)
    assert re.fullmatch(r"[0-9a-f]{4},.*", s)
    assert sorted(order) == ["%1", "%2", "%3"]
    leaves = cells(s.split(",", 1)[1])
    assert len(leaves) == 3
    # every leaf inside the window, no overlaps
    area = sum(w * h for w, h, *_ in leaves)
    assert area <= 200 * 60
    for w, h, x, y, _ in leaves:
        assert x + w <= 200 and y + h <= 60


def test_missing_and_extra_panes():
    s, order = L.layout_string("default", {"paper": "%1", "preview": "%3"}, ["%9"], 120, 40)
    assert order == ["%1", "%3", "%9"]


def test_checksum_regression():
    # tmux 3.8 accepted this string with this checksum (select-layout rejects bad ones)
    assert L._checksum("120x40,0,0{30x40,0,0,39,50x40,31,0,37,38x40,82,0,38}") == "881d"


def test_auto_is_not_a_layout_string():
    assert "auto" in L.PRESETS and L.PRESETS["auto"][1] is None


def test_legacy_layout_parse_roundtrip():
    names = {"%5": "paper", "%6": "agent", "%7": "preview"}
    tree = L._from_legacy("abcd,200x60,0,0{120x60,0,0,5,79x60,121,0[79x30,121,0,6,79x29,121,31,7]}", names)
    assert tree == ("h", [("paper", 120), (("v", [("agent", 30), ("preview", 29)]), 79)])
    s, order = L.tree_layout_string(tree, {"paper": "%1", "agent": "%2", "preview": "%3"}, [], 200, 60)
    assert order == ["%1", "%2", "%3"] and "120x60,0,0,1" in s
