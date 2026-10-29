"""Pane arrangement presets for the "paper" window.

Panes are found by their @redline_role (paper, agent, preview), rearranged with
swap-pane and resized with a generated tmux layout string, so nothing is
restarted. tmux assigns layout cells to panes in window order (the ids in the
string are ignored), hence the swaps.
"""
from __future__ import annotations

import os
import subprocess

from .model import RedlineError

# node: a role name, or (direction, [(node, weight), ...]); "h" = side by side, "v" = stacked
PRESETS: dict[str, tuple[str, object]] = {
    "default": ("paper | agent over PDF",
                ("h", [("paper", 60), (("v", [("agent", 50), ("preview", 50)]), 40)])),
    "columns": ("paper | agent | PDF",
                ("h", [("paper", 44), ("agent", 30), ("preview", 26)])),
    "rows": ("paper / agent / PDF, stacked",
             ("v", [("paper", 50), ("agent", 25), ("preview", 25)])),
    "pdf-tall": ("paper over agent | full-height PDF",
                 ("h", [(("v", [("paper", 65), ("agent", 35)]), 62), ("preview", 38)])),
    "agent-bottom": ("paper | PDF, agent full width below",
                     ("v", [(("h", [("paper", 60), ("preview", 40)]), 70), ("agent", 30)])),
    "pdf-left": ("PDF | paper | agent",
                 ("h", [("preview", 30), ("paper", 45), ("agent", 25)])),
    "paper-top": ("paper on top, agent | PDF below",
                  ("v", [("paper", 62), (("h", [("agent", 55), ("preview", 45)]), 38)])),
    "focus": ("toggle: paper pane fullscreen (tmux zoom)", None),
    "auto": ("pdf-tall on a landscape window, paper-top on a portrait one", None),
}
META = ("focus", "auto")  # not arrangements themselves
ORDER = list(PRESETS)


def _tmux(*args: str) -> str:
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RedlineError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def find_window(session: str | None = None) -> str:
    """The window (id) holding the paper/agent/preview panes."""
    if session is None and os.environ.get("TMUX_PANE"):
        session = _tmux("display", "-p", "-t", os.environ["TMUX_PANE"], "#{session_id}")
    if session is None:
        raise RedlineError("not inside tmux; pass the session (e.g. redline-Pop-III-m12f)")
    for line in _tmux("list-panes", "-s", "-t", session, "-F", "#{window_id} #{@redline_role}").splitlines():
        wid, _, role = line.partition(" ")
        if role == "paper":
            return wid
    raise RedlineError(f"no redline paper pane in session {session}")


def panes(window: str) -> list[tuple[str, str]]:
    out = _tmux("list-panes", "-t", window, "-F", "#{pane_id} #{@redline_role}")
    return [(ln.split(" ")[0], ln.partition(" ")[2]) for ln in out.splitlines()]


def _prune(node, present: set[str]):
    if isinstance(node, str):
        return node if node in present else None
    d, kids = node
    kept = [(n, w) for n, w in ((_prune(n, present), w) for n, w in kids) if n is not None]
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0][0]
    return (d, kept)


def _split(total: int, weights: list[float]) -> list[int]:
    avail = total - (len(weights) - 1)  # one cell of border between siblings
    s = sum(weights)
    sizes = [max(2, int(avail * w / s)) for w in weights]
    sizes[-1] = max(2, avail - sum(sizes[:-1]))
    return sizes


def _render(node, x: int, y: int, w: int, h: int, ids: dict[str, str], order: list[str]) -> str:
    if isinstance(node, str):
        pid = ids[node]
        order.append(pid)
        return f"{w}x{h},{x},{y},{pid.lstrip('%')}"
    d, kids = node
    sizes = _split(w if d == "h" else h, [k[1] for k in kids])
    parts, off = [], (x if d == "h" else y)
    for (child, _), size in zip(kids, sizes):
        if d == "h":
            parts.append(_render(child, off, y, size, h, ids, order))
        else:
            parts.append(_render(child, x, off, w, size, ids, order))
        off += size + 1
    o, c = ("{", "}") if d == "h" else ("[", "]")
    return f"{w}x{h},{x},{y}{o}{','.join(parts)}{c}"


def _checksum(body: str) -> str:
    c = 0
    for ch in body:
        c = (c >> 1) + ((c & 1) << 15)
        c = (c + ord(ch)) & 0xFFFF
    return f"{c:04x}"


def layout_string(name: str, ids_by_role: dict[str, str], extra: list[str], w: int, h: int) -> tuple[str, list[str]]:
    tree = _prune(PRESETS[name][1], set(ids_by_role))
    if tree is None:
        raise RedlineError("no redline panes found")
    ids = dict(ids_by_role)
    if extra:  # panes the user added: give them a column on the right
        for i, pid in enumerate(extra):
            ids[f"extra{i}"] = pid
        tree = ("h", [(tree, 80)] + [(f"extra{i}", 20 / len(extra)) for i in range(len(extra))])
    order: list[str] = []
    body = _render(tree, 0, 0, w, h, ids, order)
    return f"{_checksum(body)},{body}", order


def apply(name: str, window: str | None = None) -> str:
    if name not in PRESETS:
        raise RedlineError(f"unknown layout {name!r}; presets: {', '.join(ORDER)}")
    window = window or find_window()
    current = panes(window)
    by_role = {role: pid for pid, role in current if role in ("paper", "agent", "preview")}
    if name == "focus":
        _tmux("resize-pane", "-Z", "-t", by_role.get("paper") or current[0][0])
        return "toggled zoom on the paper pane"
    if name == "auto":
        return apply(auto_choice(window), window)
    if _tmux("display", "-p", "-t", window, "#{window_zoomed_flag}") == "1":
        _tmux("resize-pane", "-Z", "-t", current[0][0])  # unzoom first
    extra = [pid for pid, role in current if pid not in by_role.values()]
    w, h = (int(v) for v in _tmux("display", "-p", "-t", window, "#{window_width} #{window_height}").split())
    lay, order = layout_string(name, by_role, extra, w, h)
    # put panes into the order the layout assigns them
    cur = [pid for pid, _ in current]
    for i, pid in enumerate(order):
        if cur[i] != pid:
            j = cur.index(pid)
            _tmux("swap-pane", "-d", "-s", pid, "-t", cur[i])
            cur[i], cur[j] = cur[j], cur[i]
    _tmux("select-layout", "-t", window, lay)
    _tmux("set-option", "-w", "-t", window, "@redline_layout", name)
    return f"layout {name}: {PRESETS[name][0]}"


def auto_choice(window: str) -> str:
    """pdf-tall when the window is landscape in pixels, paper-top otherwise."""
    w, h, cw, ch = (_tmux("display", "-p", "-t", window,
                          "#{window_width} #{window_height} #{client_cell_width} #{client_cell_height}")
                    .split() + ["0", "0"])[:4]
    cw, ch = (int(cw) or 10), (int(ch) or 20)
    return "pdf-tall" if int(w) * cw >= int(h) * ch else "paper-top"


def cycle(step: int = 1, window: str | None = None) -> str:
    window = window or find_window()
    try:
        cur = _tmux("show-options", "-wv", "-t", window, "@redline_layout")
    except RedlineError:
        cur = "default"
    names = [n for n in ORDER if n not in META]
    i = names.index(cur) if cur in names else 0
    return apply(names[(i + step) % len(names)], window)
