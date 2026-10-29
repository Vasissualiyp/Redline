"""High-level operations shared by the MCP server, the CLI and the nvim RPC."""
from __future__ import annotations

from . import author, ops, select
from . import compile as _compile
from . import lint as _lint
from . import sync as _sync
from . import verify as _verify
from .model import Change, Document, RedlineError, TextEdit
from .state import Project


def _diff_edit(old: str, new: str) -> TextEdit:
    """One edit turning old into new (common prefix and suffix left alone)."""
    a = 0
    while a < min(len(old), len(new)) and old[a] == new[a]:
        a += 1
    b = 0
    while b < min(len(old), len(new)) - a and old[-1 - b] == new[-1 - b]:
        b += 1
    return TextEdit(a, len(old) - b, new[a:len(new) - b])


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

    def plain_edit(self, old_text, new_text, section=None, occurrence=None, origin=None,
                   reason=None, force=False):
        self._edit(lambda d: author.plain_edit(d, old_text, new_text, section, occurrence, reason, force),
                   "plain_edit" + (f": {reason}" if reason else ""), origin)
        return "replaced (untracked" + (", explained in a \\chcomment)" if reason else ")")

    def propose_move(self, text, after=None, before=None, comment=None, section=None,
                     to_section=None, occurrence=None, origin=None):
        return self._edit(lambda d: author.propose_move(d, text, after, before, comment, section,
                                                        to_section, occurrence),
                          "propose_move", origin)

    def revise_change(self, id, new_text=None, comment=None, origin=None, keep_note=False):
        """Revising answers a %N note, so the note is cleared and the change is unrated again."""
        def fn(d: Document):
            it = d.find(id)
            edits = author.revise_change(d, it, new_text, comment)
            if keep_note or it.marker is None or it.marker.kind != "N":
                return edits
            mid = ops.apply(d.src, edits)
            d2 = self.proj.doc(mid)
            it2 = next((i for i in d2.items if i.start == it.start), None)
            if it2 is None or it2.marker is None:
                return edits
            return [_diff_edit(d.src, ops.apply(mid, ops.set_marker(d2, it2, None)))]
        return self._edit(fn, f"revise {id}", origin)

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

    # ---- sync to Overleaf -----------------------------------------------------------
    def sync_check(self, reuse: bool = False) -> _sync.SyncCheck:
        return _sync.check(self.proj, reuse)

    def sync_to_overleaf(self, agent: str | None = None, push: bool = True, stream=None,
                         reuse: bool = False) -> str:
        """Push if Overleaf is unchanged since the last sync; otherwise merge first.

        agent=None with a real merge needed: prepares the workspace and explains
        the options instead of choosing for the human.
        """
        chk = _sync.check(self.proj, reuse)
        out = [chk.text()]
        if chk.remote_unchanged:
            if not chk.local_edits:
                return "\n".join(out + ["already in sync with Overleaf"])
            return "\n".join(out + [self.push_only()] if push else out)
        if chk.both_changed:
            names = _sync.prepare_merge(self.proj, chk)
            if chk.clean:
                agent = "git"  # no conflicts: nothing for an agent to decide
                out.append("no conflicts: merged automatically")
            elif agent is None or agent == "git":
                out.append(f"merge workspace: {_sync.merge_dir(self.proj)} ({', '.join(names)})")
                out.append("conflicts need a decision; choose who merges: " + ", ".join(self.proj.cfg.agents)
                           + "; or resolve the files there yourself and call sync_finish")
                return "\n".join(out)
            else:
                rc = _sync.run_agent(self.proj, agent, stream)
                if rc != 0:
                    return "\n".join(out + [f"agent {agent} exited with {rc}; nothing applied"])
        rep = _sync.finish_merge(self.proj, chk, use_git=agent == "git")
        out.append(rep.text())
        if not chk.local_edits and not rep.conflicts:
            # Overleaf was simply ahead: the local copy is now identical, nothing to push
            return "\n".join(out + ["local copy updated from Overleaf; nothing to push"])
        if rep.conflicts or not push:
            return "\n".join(out)
        return "\n".join(out + [self.push_only()])

    def sync_finish(self, push: bool = True) -> str:
        chk = _sync.load_check(self.proj)
        rep = _sync.finish_merge(self.proj, chk)
        out = [rep.text()]
        if push and not rep.conflicts:
            out.append(self.push_only())
        return "\n".join(out)

    def push_only(self) -> str:
        return _sync.push(self.proj, skip_pull=True).text()

    # ---- claim verification ------------------------------------------------------
    def verification_requests(self) -> str:
        doc = self.proj.doc()
        reqs = _verify.requests(doc)
        if not reqs:
            return _verify.report(doc).splitlines()[0] + "; no open requests"
        return self.verify_message([r.id for r in reqs], doc=doc)

    def verify_message(self, ids: list[str] | None = None, doc=None, text: str | None = None) -> str:
        """The task text an agent gets for open verification requests."""
        doc = doc or self.proj.doc(text)
        reqs = [r for r in _verify.requests(doc) if not ids or r.id in ids]
        if not reqs:
            raise RedlineError("no open verification requests")
        lines = doc.src.splitlines()
        code = self.proj.cfg.code_dir or "(no code_dir configured)"
        out = [f"[redline] Verify {len(reqs)} claim(s). For each: check it, then call "
               "record_verification(request_id=..., status=VERIFIED|REFUTED|UNVERIFIED, source=..., "
               "evidence=...). If REFUTED, also propose_replace a correction with a comment.",
               "- refcheck: only PDF-backed sources from Zotero (paper-refcheck skill rules); "
               "source = bib key, evidence = exact quote + §/page.",
               f"- code: read the analysis code in {code} (and its outputs); source = "
               "code:<path>:<line>, evidence = what it computes and the value you got.",
               "- any: whichever fits; say which in the evidence.", ""]
        for r in reqs:
            a = max(1, r.claim_line - 2)
            ctx = "\n".join(f"    {n:5d}  {lines[n - 1]}" for n in range(a, (r.claim_line or r.line) + 1))
            out.append(f"request {r.id} (method {r.method}, line {r.line})"
                       + (f", note: {r.note}" if r.note else "") + f"\n  claim: «{r.claim}»\n{ctx}")
        return "\n".join(out)

    def record_verification(self, status: str, source: str = "", evidence: str = "",
                            request_id: str | None = None, anchor: str | None = None,
                            section: str | None = None) -> str:
        def fn(doc):
            after = None
            if not request_id:
                if not anchor:
                    raise RedlineError("pass request_id, or anchor= (exact text of the claim)")
                _, e = author.locate(doc, anchor, section)
                after = doc.line_of(e - 1)
            return _verify.record(doc, status, source, evidence, request_id, after)
        self.proj.transact(fn, reason=f"verification {status}")
        return "recorded"

    def verification_report(self) -> str:
        return _verify.report(self.proj.doc())
