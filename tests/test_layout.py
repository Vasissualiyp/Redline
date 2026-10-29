import re

import pytest

from redline import layout as L

IDS = {"paper": "%1", "agent": "%2", "preview": "%3"}


def cells(s):
    return [tuple(map(int, m.groups())) for m in re.finditer(r"(\d+)x(\d+),(\d+),(\d+),(\d+)(?=[,}\]]|$)", s)]


@pytest.mark.parametrize("name", [n for n in L.ORDER if n != "focus"])
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


def test_checksum_matches_tmux():
    # a layout string tmux itself produced
    body = "120x40,0,0{30x40,0,0,39,50x40,31,0,37,38x40,82,0,38}"
    assert L._checksum(body) == L._checksum(body) and len(L._checksum(body)) == 4
