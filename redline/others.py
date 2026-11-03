"""Reading the other papers of `redline full`, for an agent working on one paper.

Read-only: an agent can look at a sibling paper (what it says, its pending changes) to keep
papers consistent or reuse text, but every edit still goes through that paper's own
Redline session. A paper can opt out (`cross_paper_access = false` in its .redline.toml, or
in its edit screen): its agent then sees no other paper and no other agent sees it.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import registry
from .model import RedlineError
from .service import Service
from .state import Project

MAX_HITS = 60


def _check_self(me: Project) -> None:
    if not me.cfg.cross_paper_access:
        raise RedlineError("this paper has cross-paper access turned off (cross_paper_access = false)")


def _open(me: Project, name: str) -> Project:
    _check_self(me)
    root = registry.find(name)
    proj = Project.open(root)
    if not proj.cfg.cross_paper_access and root.resolve() != me.cfg.root.resolve():
        raise RedlineError(f"{proj.cfg.project_name} is private (cross_paper_access = false)")
    return proj


def papers(me: Project) -> str:
    """One line per paper: name, category, pending changes, notes."""
    _check_self(me)
    out = []
    for it in registry.listing():
        try:
            shared = Project.open(it.path).cfg.cross_paper_access
        except RedlineError:
            shared = False
        me_mark = "  (this paper)" if it.path.resolve() == me.cfg.root.resolve() else ""
        if not shared and not me_mark:
            out.append(f"{it.name}  [private]")
            continue
        out.append(f"{it.name}{me_mark}  [{it.category or 'uncategorized'}]  {it.pending if it.pending is not None else '?'} "
                   f"pending / {it.unrated if it.unrated is not None else '?'} unrated  main={Path(it.path).name}/"
                   + (f"  notes: {it.notes}" if it.notes else ""))
    return "\n".join(out) or "no papers"


def outline(me: Project, paper: str) -> str:
    return Service(_open(me, paper)).outline()


def read(me: Project, paper: str, section: str | None = None, lines: list[int] | None = None) -> str:
    return Service(_open(me, paper)).read(section, lines)


def changes(me: Project, paper: str, selector: dict | None = None, full: bool = False) -> str:
    return Service(_open(me, paper)).list_changes(selector, full)


def search(me: Project, query: str, paper: str | None = None, regex: bool = False, limit: int = MAX_HITS) -> str:
    """Matching lines of the .tex/.bib files of one paper, or of every shared paper."""
    _check_self(me)
    if not query:
        raise RedlineError("give a search text")
    pat = re.compile(query if regex else re.escape(query), re.I)
    roots = [_open(me, paper).cfg.root] if paper else []
    if not paper:
        for p in registry.scan():
            try:
                if Project.open(p).cfg.cross_paper_access:
                    roots.append(p)
            except RedlineError:
                continue
    hits = []
    for root in roots:
        for f in sorted([*root.rglob("*.tex"), *root.rglob("*.bib")]):
            rel = f.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            try:
                lines = f.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for n, ln in enumerate(lines, 1):
                if pat.search(ln):
                    hits.append(f"{root.name}/{rel}:{n}: {ln.strip()[:200]}")
                    if len(hits) >= limit:
                        return "\n".join(hits) + f"\n... stopped at {limit} matches; narrow the search"
    return "\n".join(hits) or "no matches"
