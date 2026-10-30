"""JSON-lines RPC over stdio for the nvim plugin.

The plugin sends its *buffer* text, so positions always match what is on
screen, and gets back TextEdits as (row, byte col) ranges that it applies with
nvim_buf_set_text: undo/redo stay native.

request:  {"id": 1, "method": "parse", "params": {"text": "..."}}
response: {"id": 1, "result": {...}}  or  {"id": 1, "error": "message"}
"""
from __future__ import annotations

import json
import sys
import traceback

from . import author, lint, ops, select, verify
from .model import Change, Document, RedlineError, TextEdit
from .parse import parse
from .service import describe
from .state import Project


def _pos(doc: Document, off: int) -> list[int]:
    return list(doc.line_col(off))


def _rng(doc: Document, a: int, b: int) -> list[int]:
    return _pos(doc, a) + _pos(doc, b)


def _item(doc: Document, it: Change, origins: dict) -> dict:
    d = describe(doc, it, origins, width=0)
    d["range"] = _rng(doc, it.start, it.end)
    d["full_range"] = _rng(doc, it.start, it.full_end)
    first = it.args[0].start
    d["conceal"] = [_rng(doc, it.start, first)]  # "\added[..]{"
    for a, b in zip(it.args, it.args[1:]):
        d["conceal"].append(_rng(doc, a.end, b.start))  # "}{" in \replaced
    d["conceal"].append(_rng(doc, it.args[-1].end, it.end))  # final "}"
    d["args"] = [_rng(doc, a.start, a.end) for a in it.args]
    if it.comment:
        c = it.comment
        d["comment_range"] = _rng(doc, c.start, c.end)
        d["comment_conceal"] = [_rng(doc, c.start, c.args[0].start), _rng(doc, c.args[0].end, c.end)]
        d["comment_arg"] = _rng(doc, c.args[0].start, c.args[0].end)
    if it.marker:
        d["marker_range"] = _rng(doc, it.marker.start, it.marker.end)
    return d


def _edits(doc: Document, edits: list[TextEdit]) -> list[dict]:
    out = []
    for e in sorted(edits, key=lambda e: (e.start, e.end), reverse=True):
        out.append({"range": _rng(doc, e.start, e.end), "text": e.text})
    return out


class Rpc:
    def __init__(self, proj: Project | None):
        self.proj = proj

    def _doc(self, p: dict) -> Document:
        legacy = self.proj.cfg.legacy if self.proj else None
        return parse(p["text"], legacy)

    def _origins(self) -> dict:
        return self.proj.origins() if self.proj else {}

    def parse(self, p: dict) -> dict:
        doc = self._doc(p)
        org = self._origins()
        return {
            "items": [_item(doc, it, org) for it in doc.items],
            "sections": [{"label": s.label, "title": s.title, "number": s.number, "level": s.level,
                          "line": s.line, "end_line": doc.line_of(max(s.start, s.end - 1))}
                         for s in doc.sections],
            "orphan_markers": [{"line": m.line, "kind": m.kind, "note": m.note}
                               for m in doc.markers if m.item is None],
            "lint": [i.as_dict() for i in lint.lint(doc)],
            "verify": [r.as_dict() for r in verify.requests(doc)],
            "results": [r.as_dict() for r in verify.results(doc)],
        }

    def add_verify(self, p: dict) -> dict:
        doc = self._doc(p)
        edits = verify.add_request(doc, int(p["after_line"]), p.get("method", "any"),
                                   p.get("claim", ""), p.get("note", ""))
        new = ops.apply(doc.src, edits)
        req = [r for r in verify.requests(self._doc({"text": new})) if r.line == doc.line_of(edits[0].start)]
        return {"edits": _edits(doc, edits), "id": req[0].id if req else None}

    def verify_message(self, p: dict) -> dict:
        from .service import Service
        if not self.proj:
            raise RedlineError("no project")
        return {"message": Service(self.proj).verify_message(p.get("ids"), text=p["text"])}

    def op(self, p: dict) -> dict:
        doc = self._doc(p)
        sel = p.get("selector") or {}
        items = select.select(doc, sel, self._origins())
        explicit = bool(p.get("explicit"))
        decs = ops.decide(doc, items, p["action"], honor_markers=p.get("honor_markers", True))
        keep = self.proj.cfg.keep_comment_regex if self.proj else "TODO"
        edits = ops.resolve(doc, decs, comments=p.get("comments", "auto"), keep_regex=keep,
                            explicit=explicit)
        return {"edits": _edits(doc, edits),
                "report": [{"label": d.item.label, "action": d.action, "reason": d.reason}
                           for d in decs]}

    def set_marker(self, p: dict) -> dict:
        doc = self._doc(p)
        return {"edits": _edits(doc, ops.set_marker(doc, doc.find(p["id"]), p.get("kind"),
                                                    p.get("note", "")))}

    def revise(self, p: dict) -> dict:
        doc = self._doc(p)
        return {"edits": _edits(doc, author.revise_change(doc, doc.find(p["id"]),
                                                          p.get("new_text"), p.get("comment")))}

    def lint_fix(self, p: dict) -> dict:
        new, fixed = lint.fix_all(p["text"], self.proj.cfg.legacy if self.proj else None)
        doc = self._doc(p)
        edits = [] if new == p["text"] else [TextEdit(0, len(p["text"]), new)]
        return {"edits": _edits(doc, edits), "fixed": [i.as_dict() for i in fixed]}

    def ping(self, p: dict) -> dict:
        return {"ok": True, "root": str(self.proj.cfg.root) if self.proj else None}


def serve(proj: Project | None) -> None:
    rpc = Rpc(proj)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            fn = getattr(rpc, req["method"], None)
            if fn is None or req["method"].startswith("_"):
                raise RedlineError(f"unknown method {req['method']!r}")
            resp = {"id": rid, "result": fn(req.get("params") or {})}
        except RedlineError as e:
            resp = {"id": rid, "error": str(e)}
        except Exception as e:  # keep serving; report the bug
            resp = {"id": rid, "error": f"{type(e).__name__}: {e}",
                    "trace": traceback.format_exc()[-1500:]}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
