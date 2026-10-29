"""Pane arrangement presets for the "paper" window.

Panes are found by their @redline_role (paper, agent, preview), rearranged with
swap-pane and resized with a generated tmux layout string, so nothing is
restarted. tmux assigns layout cells to panes in window order (the ids in the
string are ignored), hence the swaps.
"""
from __future__ import annotations

import os
import re
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
    return tree_layout_string(PRESETS[name][1], ids_by_role, extra, w, h)


def tree_layout_string(node, ids_by_role: dict[str, str], extra: list[str], w: int, h: int) -> tuple[str, list[str]]:
    tree = _prune(node, set(ids_by_role))
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
    _arrange(window, PRESETS[name][1], current, by_role)
    _tmux("set-option", "-w", "-t", window, "@redline_layout", name)
    return f"layout {name}: {PRESETS[name][0]}"


def _leaf_ids(current: list[tuple[str, str]]) -> dict[str, str]:
    """Leaf names -> pane ids: roles, and "#k" for the k-th pane without a role."""
    ids = {role: pid for pid, role in current if role in ("paper", "agent", "preview")}
    plain = [pid for pid, role in current if pid not in ids.values()]
    ids.update({f"#{k}": pid for k, pid in enumerate(plain)})
    return ids


def _leaves(node) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, str):
        return {node}
    return set().union(*(_leaves(n) for n, _ in node[1]))


def _arrange(window: str, node, current: list[tuple[str, str]], ids: dict[str, str]) -> None:
    if _tmux("display", "-p", "-t", window, "#{window_zoomed_flag}") == "1":
        _tmux("resize-pane", "-Z", "-t", current[0][0])  # unzoom first
    used = _leaves(_prune(node, set(ids)))
    ids = {leaf: pid for leaf, pid in ids.items() if leaf in used}
    extra = [pid for pid, _ in current if pid not in ids.values()]  # panes the tree doesn't know
    w, h = (int(v) for v in _tmux("display", "-p", "-t", window, "#{window_width} #{window_height}").split())
    lay, order = tree_layout_string(node, ids, extra, w, h)
    # put panes into the order the layout assigns them
    cur = [pid for pid, _ in current]
    for i, pid in enumerate(order):
        if cur[i] != pid:
            j = cur.index(pid)
            _tmux("swap-pane", "-d", "-s", pid, "-t", cur[i])
            cur[i], cur[j] = cur[j], cur[i]
    _tmux("select-layout", "-t", window, lay)


# ---- saving / restoring the arrangement -------------------------------------------
def _from_json(node: dict, names: dict[str, str]):
    if node.get("t") == "p":
        return names.get(node.get("I", ""), "")
    d = "h" if node.get("t") == "h" else "v"
    key = "w" if d == "h" else "h"
    return (d, [(_from_json(c, names), int(c.get(key, 1)) or 1) for c in node.get("c", [])])


def _from_legacy(text: str, names: dict[str, str]):
    """Parse the classic `csum,WxH,X,Y{...}` layout (pane ids are positional)."""
    body = text.split(",", 1)[1]
    pos = 0
    order = list(names.values())
    counter = iter(range(10_000))

    def cell():
        nonlocal pos
        m = re.compile(r"(\d+)x(\d+),\d+,\d+").match(body, pos)
        w, h = int(m.group(1)), int(m.group(2))
        pos = m.end()
        if pos < len(body) and body[pos] in "{[":
            d = "h" if body[pos] == "{" else "v"
            close = "}" if d == "h" else "]"
            pos += 1
            kids = []
            while True:
                node, cw, ch = cell()
                kids.append((node, cw if d == "h" else ch))
                if body[pos] == ",":
                    pos += 1
                    continue
                assert body[pos] == close
                pos += 1
                break
            return (d, kids), w, h
        m = re.compile(r",(\d+)").match(body, pos)
        pos = m.end()
        k = next(counter)
        return (order[k] if k < len(order) else ""), w, h

    return cell()[0]


def capture(window: str) -> dict:
    """The window's arrangement as a role tree with relative sizes."""
    current = panes(window)
    names = {pid: leaf for leaf, pid in _leaf_ids(current).items()}
    raw = _tmux("display", "-p", "-t", window, "#{window_layout}")
    if raw.startswith("{"):
        import json
        tree = _from_json(json.loads(raw)["L"], names)
    else:
        tree = _from_legacy(raw, {pid: names[pid] for pid, _ in current})
    try:
        preset = _tmux("show-options", "-wv", "-t", window, "@redline_layout")
    except RedlineError:
        preset = ""
    return {"tree": tree, "preset": preset}


def _as_node(t):
    """JSON lists back into the (direction, [(node, weight)]) tuples."""
    if isinstance(t, str):
        return t
    d, kids = t
    return (d, [(_as_node(n), w) for n, w in kids])


def restore(window: str, saved: dict) -> str:
    current = panes(window)
    ids = _leaf_ids(current)
    node = _as_node(saved["tree"])
    _arrange(window, node, current, ids)
    if saved.get("preset"):
        _tmux("set-option", "-w", "-t", window, "@redline_layout", saved["preset"])
    return "restored the saved layout"


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
