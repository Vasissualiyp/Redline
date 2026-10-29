"""High-level operations shared by the MCP server, the CLI and the nvim RPC."""
from __future__ import annotations

from . import author, ops, select
from . import compile as _compile
from . import lint as _lint
from . import sync as _sync
from .model import Change, Document, RedlineError
from .state import Project


def _short(s: str | None, n: int) -> str:
    if s is None:
        return ""
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe(doc: Document, it: Change, origins: dict[str, str], width: int = 160) -> dict:
    src = doc.src
    d = {
        "label": it.label, "id": it.id, "kind": it.kind, "macro": it.macro,
        "section": it.section.label if it.section else "",
        "lines": [it.line_start, it.line_end],
        "origin": origins.get(it.id, "manual"),
        "new": it.new(src), "old": it.old(src),
        "comment": it.body(src) if it.kind == "comment" else (it.comment.body(src) if it.comment else None),
        "marker": ({"kind": it.marker.kind, "note": it.marker.note} if it.marker else None),
    }
    if width:
        for k in ("new", "old", "comment"):
            if d[k] is not None:
                d[k] = _short(d[k], width)
    return d


def format_item(d: dict) -> str:
    head = f"{d['label']} {d['id']} {d['kind']} l.{d['lines'][0]}"
    if d["lines"][1] != d["lines"][0]:
        head += f"-{d['lines'][1]}"
    head += f" §{d['section']}" if d["section"] else ""
    head += f" [{d['origin']}]"
    if d["marker"]:
        head += f" %{d['marker']['kind']}" + (f" \"{d['marker']['note']}\"" if d["marker"]["note"] else "")
    parts = [head]
    if d["new"] is not None:
        parts.append(f"new: «{d['new']}»")
    if d["old"] is not None:
        parts.append(f"old: «{d['old']}»")
    if d["comment"] is not None:
        parts.append(f"comment: «{d['comment']}»")
    return " | ".join(parts)


class Service:
    def __init__(self, proj: Project):
        self.proj = proj

    # ---- reading ---------------------------------------------------------------
    def outline(self) -> str:
        doc = self.proj.doc()
        lines = [f"{self.proj.main.name}: {len(doc.src.splitlines())} lines, "
                 f"{len(doc.items)} pending items"]
        for s in doc.sections:
            its = [i for i in doc.items if s.start <= i.start < s.end and doc.section_at(i.start) is s]
            pend = sum(1 for i in its if i.kind != "comment")
            com = sum(1 for i in its if i.kind == "comment")
            mk = sum(1 for i in its if i.marker)
            stats = ", ".join(x for x in (f"{pend} changes" if pend else "",
                                          f"{com} comments" if com else "",
                                          f"{mk} marked" if mk else "") if x)
            lines.append(f"{'  ' * (s.level - 1)}{s.label}  (l.{s.line}-{doc.line_of(max(s.start, s.end - 1))})"
                         + (f"  [{stats}]" if stats else ""))
        return "\n".join(lines)

    def read(self, section: str | None = None, lines: list[int] | None = None) -> str:
        doc = self.proj.doc()
        src_lines = doc.src.splitlines()
        if section:
            s = select.find_section(doc, section)
            a, b = s.line, doc.line_of(max(s.start, s.end - 1))
        elif lines:
            a, b = lines
        else:
            raise RedlineError("pass section= or lines=[first, last]")
        a, b = max(1, a), min(len(src_lines), b)
        if b - a > 600:
            raise RedlineError(f"range too large ({b - a + 1} lines); read at most 600 at once")
        return "\n".join(f"{n:5d}  {src_lines[n - 1]}" for n in range(a, b + 1))

    def list_changes(self, selector: dict | None = None, full: bool = False) -> str:
        doc = self.proj.doc()
        items = select.select(doc, selector or {"all": True}, self.proj.origins())
        org = self.proj.origins()
        if not items:
            return "no matching changes"
        return "\n".join(format_item(describe(doc, it, org, 0 if full else 160)) for it in items)

    def review_notes(self) -> str:
        doc = self.proj.doc()
        org = self.proj.origins()
        rows = [format_item(describe(doc, it, org, 0)) for it in doc.items if it.marker]
        orphans = [f"l.{m.line}: %{m.kind} {m.note} (not attached to a change)"
                   for m in doc.markers if m.item is None]
        return "\n".join(rows + orphans) or "no review markers in the paper"

    def status(self) -> dict:
        doc = self.proj.doc()
        st = {
            "main": str(self.proj.main),
            "items": len(doc.items),
            "changes": sum(1 for i in doc.items if i.kind != "comment"),
            "comments": sum(1 for i in doc.items if i.kind == "comment"),
            "markers": {k: sum(1 for i in doc.items if i.marker and i.marker.kind == k) for k in "YNX"},
            "lint_errors": sum(1 for i in _lint.lint(doc) if i.severity == "error"),
            "has_base": (self.proj.dir / "base").exists(),
            "pdf": str(_compile.pdf_path(self.proj)),
        }
        return st

    # ---- proposing --------------------------------------------------------------
    def _edit(self, fn, reason: str, origin: str | None) -> str:
        before, after = self.proj.transact(fn, reason=reason, origin=origin)
        old_ids = {i.id for i in before.items}
        new = [i for i in after.items if i.id not in old_ids]
        org = self.proj.origins()
        errs = [i for i in _lint.lint(after) if i.severity == "error"
                and any(n.line_start <= i.line <= n.line_end for n in new)]
        out = "\n".join("created " + format_item(describe(after, i, org)) for i in new) or "done"
        if errs:
            out += "\nLINT ERRORS:\n" + "\n".join(f"  l.{e.line} {e.code}: {e.message}" for e in errs)
        return out

    def propose_replace(self, old_text, new_text, comment=None, section=None, occurrence=None, origin=None):
        return self._edit(lambda d: author.propose_replace(d, old_text, new_text, comment, section, occurrence),
                          "propose_replace", origin)

    def propose_delete(self, old_text, comment=None, section=None, occurrence=None, origin=None):
        return self._edit(lambda d: author.propose_delete(d, old_text, comment, section, occurrence),
                          "propose_delete", origin)

    def propose_insert(self, text, after=None, before=None, comment=None, section=None,
                       occurrence=None, origin=None):
        return self._edit(lambda d: author.propose_insert(d, text, after, before, comment, section, occurrence),
                          "propose_insert", origin)

    def add_comment(self, anchor, text, section=None, occurrence=None, origin=None):
        return self._edit(lambda d: author.add_comment(d, anchor, text, section, occurrence),
                          "add_comment", origin)

    def plain_edit(self, old_text, new_text, section=None, occurrence=None, origin=None):
        self._edit(lambda d: author.plain_edit(d, old_text, new_text, section, occurrence),
                   "plain_edit", origin)
        return "replaced (untracked)"

    def revise_change(self, id, new_text=None, comment=None, origin=None):
        return self._edit(lambda d: author.revise_change(d, d.find(id), new_text, comment),
                          f"revise {id}", origin)

    # ---- resolving -------------------------------------------------------------------
    def resolve(self, action: str, selector: dict, comments: str = "auto",
                honor_markers: bool = True) -> str:
        report: list = []

        def fn(doc: Document):
            items = select.select(doc, selector, self.proj.origins())
            explicit = bool(selector.get("ids")) and len(selector) == 1
            decs = ops.decide(doc, items, action, honor_markers=honor_markers and not explicit)
            edits = ops.resolve(doc, decs, comments=comments,
                                keep_regex=self.proj.cfg.keep_comment_regex, explicit=explicit)
            report.extend(decs)
            return edits

        self.proj.transact(fn, reason=f"{action} {selector}")
        counts: dict[str, list[str]] = {}
        for d in report:
            counts.setdefault(d.action + (f" ({d.reason})" if d.reason else ""), []).append(d.item.label)
        if not counts:
            return "no matching changes"
        return "\n".join(f"{k}: {len(v)}  {' '.join(v[:40])}" for k, v in counts.items()) + \
            "\n(labels refer to the numbering before this operation)"

    def set_marker(self, id: str, kind: str | None, note: str = "") -> str:
        self.proj.transact(lambda d: ops.set_marker(d, d.find(id), kind, note),
                           reason=f"marker {id} {kind}")
        return "ok"

    # ---- build & sync -----------------------------------------------------------------
    def lint(self, fix: bool = False) -> str:
        if fix:
            fixed: list = []

            def fn(doc: Document):
                new, f = _lint.fix_all(doc.src, self.proj.cfg.legacy)
                fixed.extend(f)
                return [ops.TextEdit(0, len(doc.src), new)] if new != doc.src else []

            self.proj.transact(fn, reason="lint --fix")
        doc = self.proj.doc()
        issues = _lint.lint(doc)
        out = []
        if fix:
            out.append(f"fixed {len(fixed)} issue(s)")
        out += [f"{i.severity.upper()} l.{i.line} {i.code}{' ' + i.item if i.item else ''}: {i.message}"
                + (" [fixable]" if i.fix else "") for i in issues]
        return "\n".join(out) or "clean"

    def compile(self, isolate: bool = False) -> str:
        return _compile.format_report(_compile.run(self.proj, isolate=isolate))

    def sync_init(self) -> str:
        _sync.ensure_ignores(self.proj.cfg.root)
        return _sync.init_base(self.proj).text()

    def pull(self) -> str:
        return _sync.pull(self.proj).text()

    def push(self, force: bool = False) -> str:
        return _sync.push(self.proj, force=force).text()

    def undo(self) -> str:
        return "undid: " + self.proj.undo()
