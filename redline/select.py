"""Selectors: which changes an operation applies to.

A selector is a plain dict (what MCP / RPC clients send):

    {"ids": ["C3", "a3f9c2"], "section": "3.2", "until_section": "Simulation and Methods",
     "from_section": "...", "lines": [100, 250], "kinds": ["added"], "marker": "N",
     "origin": "claude", "contains": "LTE", "except_ids": [...], "all": true}

All given filters are ANDed. An empty selector selects nothing unless "all" is
set, so a typo never accepts the whole paper by accident.
"""
from __future__ import annotations

import re
from typing import Any

from .model import Change, Document, RedlineError, Section

FILTER_KEYS = {"ids", "section", "until_section", "from_section", "lines", "kinds",
               "marker", "origin", "contains", "except_ids", "all"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", s.lower()).strip()


def find_section(doc: Document, query: str) -> Section:
    q = _norm(query)
    if not q:
        raise RedlineError("empty section name")
    exact = [s for s in doc.sections if _norm(s.title) == q or s.number == query.strip()
             or _norm(s.label) == q]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # prefer the highest-level heading (a \section over a same-named \subsection)
        exact.sort(key=lambda s: s.level)
        if exact[0].level < exact[1].level:
            return exact[0]
        hits = exact
    else:
        words = q.split()
        hits = [s for s in doc.sections if all(w in _norm(s.label) for w in words)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            hits.sort(key=lambda s: s.level)
            if hits[0].level < hits[1].level:
                return hits[0]
    if not hits:
        raise RedlineError(f"no section matches {query!r}. Sections: "
                           + "; ".join(s.label for s in doc.sections))
    raise RedlineError(f"section {query!r} is ambiguous: " + "; ".join(s.label for s in hits))


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def select(doc: Document, sel: dict | None, origins: dict[str, str] | None = None) -> list[Change]:
    sel = dict(sel or {})
    unknown = set(sel) - FILTER_KEYS
    if unknown:
        raise RedlineError(f"unknown selector keys {sorted(unknown)}; valid: {sorted(FILTER_KEYS)}")
    if not any(k in sel and sel[k] not in (None, [], "") for k in FILTER_KEYS - {"except_ids"}):
        raise RedlineError("empty selector; pass {\"all\": true} to select every change")

    items = list(doc.items)
    src = doc.src

    if sel.get("ids"):
        wanted = {id(doc.find(k)) for k in _as_list(sel["ids"])}
        items = [it for it in items if id(it) in wanted]
    for key in _as_list(sel.get("section")):
        s = find_section(doc, key)
        items = [it for it in items if s.start <= it.start < s.end]
    if sel.get("until_section"):
        s = find_section(doc, sel["until_section"])
        items = [it for it in items if it.start < s.start]
    if sel.get("from_section"):
        s = find_section(doc, sel["from_section"])
        items = [it for it in items if it.start >= s.start]
    if sel.get("lines"):
        a, b = sel["lines"]
        items = [it for it in items if it.line_end >= a and it.line_start <= b]
    if sel.get("kinds"):
        kinds = set(_as_list(sel["kinds"]))
        items = [it for it in items if it.kind in kinds]
    if sel.get("marker"):
        m = sel["marker"]
        if m == "none":
            items = [it for it in items if it.marker is None]
        elif m == "any":
            items = [it for it in items if it.marker is not None]
        else:
            items = [it for it in items if it.marker and it.marker.kind == m]
    if sel.get("origin"):
        origins = origins or {}
        want = set(_as_list(sel["origin"]))
        items = [it for it in items if origins.get(it.id, "manual") in want]
    if sel.get("contains"):
        needle = sel["contains"].lower()
        items = [it for it in items
                 if needle in src[it.start:it.full_end].lower()]
    if sel.get("except_ids"):
        drop = {id(doc.find(k)) for k in _as_list(sel["except_ids"])}
        items = [it for it in items if id(it) not in drop]
    return items
