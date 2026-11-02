"""Categories for `redline full`: a tree the papers hang from as leaves.

The tree is yours, in ~/.config/redline/categories.json:

    {"categories": [{"name": "Papers", "color": "cyan"},
                    {"name": "Papers/Pop III", "color": "#e5c07b"},
                    {"name": "Proposals", "color": "green"}],
     "order": {"Papers/Pop III": ["Pop III m12f"]}}

A category is a "/"-separated path; its order among its siblings is its order in the list;
parents that are not listed exist implicitly (default colour). A paper's category is the
`category` key of its .redline.toml (the edit screen sets it). `order` lists the papers of
a category by directory name, first to last; papers not listed follow, newest edit first.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from rich.color import Color, ColorParseError

from . import config
from .model import RedlineError

NONE = "Uncategorized"
DEFAULT_COLOR = "white"
PALETTE = ["cyan", "green", "yellow", "magenta", "blue", "red", "orange3", "spring_green2", "deep_pink3"]


def path() -> Path:
    return config.GLOBAL_CONFIG.parent / "categories.json"


def valid_color(c: str) -> bool:
    try:
        Color.parse(c)
        return True
    except ColorParseError:
        return False


def clean(name: str) -> str:
    return "/".join(p.strip() for p in name.replace("\\", "/").split("/") if p.strip())


def load() -> dict:
    try:
        data = json.loads(path().read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("categories", [])
    data.setdefault("order", {})
    return data


def save(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, p)


@dataclass
class Node:
    path: str
    color: str = DEFAULT_COLOR
    children: list["Node"] = field(default_factory=list)
    papers: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def depth(self) -> int:
        return self.path.count("/") if self.path else -1


def paths(data: dict | None = None) -> list[str]:
    """Every category path, parents before their children, in the user's order."""
    data = data or load()
    out: list[str] = []
    for c in data["categories"]:
        parts = clean(c["name"]).split("/")
        for i in range(1, len(parts) + 1):
            p = "/".join(parts[:i])
            if p and p not in out:
                out.append(p)
    return out


def build(infos: list, data: dict | None = None) -> Node:
    """The tree: a root whose children are the top categories; infos (registry.Info) become leaves."""
    data = data or load()
    root = Node("")
    nodes: dict[str, Node] = {"": root}
    colors = {clean(c["name"]): c.get("color") for c in data["categories"] if c.get("color")}

    def node(p: str) -> Node:
        if p not in nodes:
            parent = node(p.rsplit("/", 1)[0] if "/" in p else "")
            n = Node(p, colors.get(p) or DEFAULT_COLOR)
            nodes[p] = n
            parent.children.append(n)
        return nodes[p]

    for p in paths(data):
        node(p)
    unc = None
    for it in infos:
        cat = clean(getattr(it, "category", "") or "")
        if not cat:
            if unc is None:
                unc = Node(NONE, "bright_black")
            unc.papers.append(it)
        else:
            node(cat).papers.append(it)
    order = data.get("order", {})
    for p, n in list(nodes.items()) + ([(NONE, unc)] if unc else []):
        names = order.get(p, [])
        n.papers.sort(key=lambda i: (names.index(i.path.name) if i.path.name in names else len(names), -i.active))
    if unc:
        root.children.append(unc)
    return root


def flatten(root: Node, collapsed: set[str], show_all: bool = False):
    """Rows of the tree in display order: ("cat", Node, depth, Node) and ("paper", Info, depth, its Node).

    Categories with nothing in them are shown (they are yours); show_all=True ignores `collapsed`."""
    out = []

    def walk(n: Node, depth: int) -> None:
        for c in n.children:
            out.append(("cat", c, depth, c))
            if show_all or c.path not in collapsed:
                for it in c.papers:
                    out.append(("paper", it, depth + 1, c))
                walk(c, depth + 1)
    walk(root, 0)
    return out


def count(n: Node) -> int:
    return len(n.papers) + sum(count(c) for c in n.children)


def subtree_papers(n: Node) -> list:
    return [*n.papers, *(p for c in n.children for p in subtree_papers(c))]


# ---- editing -------------------------------------------------------------------
def _entry(data: dict, name: str) -> dict | None:
    return next((c for c in data["categories"] if clean(c["name"]) == name), None)


def add(name: str, color: str = "") -> str:
    name = clean(name)
    if not name:
        raise RedlineError("a category needs a name")
    if name == NONE:
        raise RedlineError(f"{NONE!r} is the built-in group of papers without a category")
    if color and not valid_color(color):
        raise RedlineError(f"{color!r} is not a colour (a name like cyan, or #rrggbb)")
    data = load()
    if _entry(data, name):
        raise RedlineError(f"category {name!r} exists")
    n = len(data["categories"])
    data["categories"].append({"name": name, "color": color or PALETTE[n % len(PALETTE)]})
    save(data)
    return name


def set_color(name: str, color: str) -> None:
    if not valid_color(color):
        raise RedlineError(f"{color!r} is not a colour (a name like cyan, or #rrggbb)")
    data = load()
    e = _entry(data, name)
    if e is None:  # an implicit parent becomes explicit
        e = {"name": name}
        data["categories"].append(e)
    e["color"] = color
    save(data)


def rename(old: str, new: str) -> list[str]:
    """Rename a category and its subtree; returns the old paths whose papers must be re-pointed."""
    new = clean(new)
    if not new or new == old:
        return []
    data = load()
    moved = []
    for c in data["categories"]:
        n = clean(c["name"])
        if n == old or n.startswith(old + "/"):
            moved.append(n)
            c["name"] = new + n[len(old):]
    if not moved:
        data["categories"].append({"name": new, "color": DEFAULT_COLOR})
        moved = [old]
    data["order"] = {(new + k[len(old):] if k == old or k.startswith(old + "/") else k): v
                     for k, v in data["order"].items()}
    save(data)
    return moved


def remove(name: str) -> list[str]:
    """Delete a category and its subcategories; returns the removed paths (papers there become uncategorized)."""
    data = load()
    gone = {clean(c["name"]) for c in data["categories"] if clean(c["name"]) == name or clean(c["name"]).startswith(name + "/")}
    gone.add(name)
    data["categories"] = [c for c in data["categories"] if clean(c["name"]) not in gone]
    for g in gone:
        data["order"].pop(g, None)
    save(data)
    return sorted(gone)


def move_category(name: str, delta: int) -> bool:
    """Move a category earlier/later among its siblings (its subtree goes with it)."""
    data = load()
    for p in paths(data):  # make implicit parents explicit so the order is stored
        if not _entry(data, p):
            data["categories"].append({"name": p, "color": DEFAULT_COLOR})
    # reorder: keep each subtree contiguous, ordered by first appearance
    cats = data["categories"]
    parent = name.rsplit("/", 1)[0] if "/" in name else ""
    sibs = [clean(c["name"]) for c in cats if (clean(c["name"]).rsplit("/", 1)[0] if "/" in clean(c["name"]) else "") == parent]
    if name not in sibs:
        return False
    i = sibs.index(name)
    j = i + delta
    if not 0 <= j < len(sibs):
        return False
    sibs[i], sibs[j] = sibs[j], sibs[i]
    by_root: dict[str, list[dict]] = {}
    for c in cats:
        n = clean(c["name"])
        cut = n[len(parent) + 1:] if parent else n
        by_root.setdefault((parent + "/" if parent else "") + cut.split("/")[0], []).append(c)
    first = next((k for k, c in enumerate(cats) if (clean(c["name"]).rsplit("/", 1)[0] if "/" in clean(c["name"]) else "") == parent
                  or clean(c["name"]).startswith(parent + "/" if parent else "")), 0)
    block = [c for s in sibs for c in by_root.get(s, [])]
    rest = [c for c in cats if all(c is not b for b in block)]
    data["categories"] = rest[:first] + block + rest[first:]
    save(data)
    return True


def move_paper(cat: str, papers: list[str], name: str, delta: int) -> bool:
    """Reorder a paper within its category; `papers` is the category's current display order (names)."""
    if name not in papers:
        return False
    i = papers.index(name)
    j = i + delta
    if not 0 <= j < len(papers):
        return False
    papers[i], papers[j] = papers[j], papers[i]
    data = load()
    data["order"][cat or NONE] = papers
    save(data)
    return True
