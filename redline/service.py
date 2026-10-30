"""High-level operations shared by the MCP server, the CLI and the nvim RPC."""
from __future__ import annotations

import re
from pathlib import Path

from . import author, ops, select
from . import compile as _compile
from . import lint as _lint
from . import sync as _sync
from . import evidence as _evidence
from . import verify as _verify
from . import zotero as _zotero
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
               "record_verification(request_id=..., status=VERIFIED|REFUTED|UNVERIFIED, sources=[...] "
               "or code={...}, reason=...). Redline writes a short id line (e.g. % VERIFIED ho25b#1), "
               "highlights the quotes in Zotero and files the evidence note; never write % VERIFIED "
               "lines yourself. If REFUTED, also propose_replace a correction with a comment.",
               "- refcheck: only PDF-backed sources from Zotero (paper-refcheck skill rules). Each source: "
               "{bibkey, page, quote, section, role}: quote = the exact sentence(s) copied from the PDF "
               "(they are matched on that page and highlighted), page = 1-based PDF page, role = "
               "supports|contradicts|context. A quote that is not found is refused with the page text, "
               "so you can correct it.",
               f"- code: read the analysis code in {code} (and its outputs); code = "
               "{path, line, value}: what it computes and the value you got.",
               "- any: whichever fits. UNVERIFIED: reason = what you looked at and why it is not "
               "settled.", ""]
        for r in reqs:
            a = max(1, r.claim_line - 2)
            ctx = "\n".join(f"    {n:5d}  {lines[n - 1]}" for n in range(a, (r.claim_line or r.line) + 1))
            out.append(f"request {r.id} (method {r.method}, line {r.line})"
                       + (f", note: {r.note}" if r.note else "") + f"\n  claim: «{r.claim}»\n{ctx}")
        return "\n".join(out)

    # ---- evidence library (Zotero-backed) -----------------------------------------
    @property
    def paper(self) -> str:
        return self.proj.cfg.root.name

    def library(self) -> _evidence.Library:
        return _evidence.Library(self.proj.dir / "evidence.json")

    def zot(self) -> _zotero.Zotero:
        if getattr(self, "_zot", None) is None:
            self._zot = _zotero.Zotero(self._bibfile())
        return self._zot

    def _bibfile(self) -> Path | None:
        root = self.proj.cfg.root
        m = re.search(r"\\(?:bibliography|addbibresource)\{([^}]+)\}", self.proj.read())
        for name in (m.group(1).split(",") if m else []):
            p = root / name.strip()
            p = p if p.suffix == ".bib" else p.with_suffix(".bib")
            if p.exists():
                return p
        p = self.proj.main.with_suffix(".bib")
        if p.exists():
            return p
        bibs = sorted(root.glob("*.bib"))
        return bibs[0] if bibs else None

    def _tags(self, eid: str) -> list[str]:
        return ["redline", f"redline:{self.paper}", f"rl:{eid}"]

    def _check_sources(self, sources: list, strict: bool = True) -> bool:
        """Find each quoted source in Zotero and its quote on the page.

        strict: any problem raises (nothing is recorded). Otherwise problems are
        noted on the source (`highlight = "none: ..."`). False when Zotero is down.
        """
        z = self.zot()
        quoted = [s for s in sources if s.quote and not s.code]
        if not quoted:
            return True
        if not z.available():
            for s in quoted:
                s.highlight = s.highlight or "pending"
            return False
        problems, by_att = [], {}
        for s in quoted:
            try:
                s.item_key, s.att_key = z.resolve(s.bibkey, s.item_key)
            except _zotero.ZoteroError as e:
                problems.append((s, str(e)))
                continue
            text = _evidence.match_text(s.quote)
            if not s.page:
                s.page = z.find_page(s.item_key, text)
                if not s.page:
                    problems.append((s, "quote not found in the PDF; give its page"))
                    continue
            by_att.setdefault(s.att_key, []).append(s)
        for att, ss in by_att.items():
            try:
                res = z.check(att, [(s.page, _evidence.match_text(s.quote)) for s in ss])
            except _zotero.ZoteroError as e:
                problems += [(s, str(e)) for s in ss]
                continue
            for s, r in zip(ss, res):
                if not r.get("ok"):
                    # the page may be the journal's numbering (p.488 of a 20-page PDF),
                    # or just wrong: look for the quote in the whole PDF
                    pg = z.find_page(s.item_key, _evidence.match_text(s.quote))
                    if pg and pg != s.page:
                        try:
                            again = z.check(att, [(pg, _evidence.match_text(s.quote))])[0]
                        except _zotero.ZoteroError:
                            again = {}
                        if again.get("ok"):
                            s.page, r = pg, again
                if r.get("ok"):
                    s.page = r.get("page_found") or s.page
                    if s.highlight.startswith("none"):
                        s.highlight = ""
                else:
                    err = (r.get("error") or "not found").replace("Error: ", "")
                    best = re.search(r"Best match text: (.*)", err)
                    problems.append((s, f"not found on p.{s.page} or the pages around it"
                                     + (f"; closest text: {best.group(1)[:160]}" if best else "")))
        if problems and strict:
            raise RedlineError("nothing recorded: every quote must be exact text from the source PDF\n"
                               + "\n".join(f"  {s.bibkey} p.{s.page}: «{_short(s.quote, 80)}»: {why}"
                                           for s, why in problems))
        for s, why in problems:
            s.highlight = "none: " + why.splitlines()[0][:200]
        return True

    def _push(self, e: _evidence.Entry) -> None:
        """Highlights for the entry's quotes, then its note, in Zotero."""
        z = self.zot()
        if not z.available():
            e.zotero = "pending"
            return
        try:
            if getattr(self, "_coll", None) is None:
                self._coll = z.collection(self.proj.cfg.evidence_collection.format(paper=self.paper))
            for s in e.sources:
                if s.code or not s.quote or s.ann_key or s.highlight.startswith("none"):
                    continue
                try:
                    if not s.att_key:
                        s.item_key, s.att_key = z.resolve(s.bibkey, s.item_key)
                    h = z.highlight(s.att_key, s.page or 1, _evidence.match_text(s.quote),
                                    f"{e.id} {s.role}: {_short(e.claim, 120)}", self._tags(e.id),
                                    _evidence.COLORS.get(s.role, "yellow"))
                    s.ann_key, s.page_index, s.rects, s.highlight = h["key"], h["page_index"], h["rects"], "ok"
                except _zotero.ZoteroError as ex:
                    s.highlight = "none: " + str(ex).splitlines()[0][:200]
            e.note_key = z.save_note(_evidence.note_html(e, self.paper),
                                     ["redline-evidence", f"redline:{self.paper}", f"rl:{e.id}"],
                                     self._coll, e.note_key)
            e.zotero = "synced"
        except _zotero.ZoteroError as ex:
            e.zotero = "failed: " + str(ex).splitlines()[0][:200]

    def record_verification(self, status: str, sources: list[dict] | None = None, code: dict | None = None,
                            reason: str = "", request_id: str | None = None, anchor: str | None = None,
                            section: str | None = None) -> str:
        status = status.upper()
        if status not in _verify.STATUSES:
            raise RedlineError(f"status must be one of {_verify.STATUSES}")
        srcs = [_evidence.Source.of(x) for x in (sources or [])]
        if code:
            srcs.append(_evidence.Source(code=dict(code)))
        for s in srcs:
            if s.role not in _evidence.ROLES:
                raise RedlineError(f"role must be one of {_evidence.ROLES}")
            if not s.code and not s.bibkey:
                raise RedlineError("every source needs its bibkey")
        if status != "UNVERIFIED" and not any((s.quote and s.bibkey) or s.code for s in srcs):
            raise RedlineError("VERIFIED/REFUTED need a source with the exact quote (and its page), "
                               "or code={path, line, value}")
        if status == "UNVERIFIED" and not (reason or srcs):
            raise RedlineError("UNVERIFIED needs the reason")
        online = self._check_sources(srcs)
        doc = self.proj.doc()
        if request_id:
            hits = [r for r in _verify.requests(doc) if r.id == request_id or r.id.startswith(request_id)]
            if not hits:
                raise RedlineError(f"no open verification request {request_id!r}; it may be resolved already")
            req = hits[0]
            claim, at = req.claim, doc.line_starts[req.line - 1]
            after = None
        else:
            if not anchor:
                raise RedlineError("pass request_id, or anchor= (exact text of the claim)")
            a, e = author.locate(doc, anchor, section)
            after, claim, at = doc.line_of(e - 1), doc.src[a:e], a
        lib = self.library()
        used = {r.id for r in _verify.results(doc) if r.id}
        eid = lib.next_id(_evidence.id_prefix(srcs) if status != "UNVERIFIED" or srcs else "nosrc", used)
        sec = doc.section_at(at)
        entry = _evidence.Entry(eid, status, " ".join(claim.split()), sec.label if sec else "", reason,
                                srcs, _evidence.now())
        line = _verify.short_line(status, eid, entry.also)
        self.proj.transact(lambda d: _verify.place(d, line, request_id, after), reason=f"verification {eid}")
        lib.put(entry)
        lib.save()
        if online:
            self._push(entry)
            lib.put(entry)
            lib.save()
        bad = [f"{s.bibkey}: {s.highlight}" for s in srcs if s.highlight.startswith("none")]
        return (f"recorded {line.removeprefix('% ')}; Zotero: {entry.zotero}"
                + ("".join(f"\n  no highlight for {b}" for b in bad)))

    def evidence(self, eid: str) -> str:
        return self.library().get(eid).text()

    def evidence_list(self) -> str:
        lib = self.library()
        doc = self.proj.doc()
        rows = []
        for r in _verify.results(doc):
            if not r.id:
                continue
            e = lib.entries.get(r.id)
            what = (f"{len(e.sources)} source(s), Zotero {e.zotero}" if e else "not in the local library")
            rows.append(f"l.{r.line} {r.status} {r.id}: {what}")
        old = sum(1 for r in _verify.results(doc) if not r.id)
        if old:
            rows.append(f"{old} result(s) still inline (redline evidence migrate)")
        return "\n".join(rows) or "no verification results in the paper"

    def evidence_sync(self, retry: bool = False) -> str:
        """Push entries Zotero lacks, then pull entries other machines made."""
        z = self.zot()
        if not z.available():
            raise RedlineError("Zotero is not running (or not reachable on 127.0.0.1:23119)")
        lib = self.library()
        pushed = pulled = 0
        for e in list(lib.entries.values()):
            pending = [s for s in e.sources if s.highlight == "pending" or (retry and s.highlight.startswith("none"))]
            if e.zotero != "synced" or pending:
                for s in pending:
                    s.highlight = ""
                self._check_sources(pending, strict=False)
                self._push(e)
                lib.put(e)
                pushed += 1
        for n in z.notes_tagged(f"redline:{self.paper}"):
            got = _evidence.entry_from_note(n.get("data", {}).get("note", ""))
            if got is None:
                continue
            got.note_key, got.zotero = n["key"], "synced"
            if got.id not in lib.entries:
                lib.put(got)
                pulled += 1
        lib.save()
        return f"evidence: pushed {pushed} entr{'y' if pushed == 1 else 'ies'} to Zotero, pulled {pulled}"

    def migrate_verifications(self, apply: bool = False, progress=None) -> str:
        """Move inline `% VERIFIED key: "quote" — p.N` results into the evidence library."""
        say = progress or (lambda msg: None)
        doc = self.proj.doc()
        olds = [r for r in _verify.results(doc) if not r.id]
        if not olds:
            return "nothing to migrate: every result already names an evidence id"
        plan = []
        for r in olds:
            srcs, reason = _evidence.parse_legacy(r.status, r.text)
            cl = _verify._claim_line_before(doc.src, doc.line_starts, r.line)
            claim = doc.src[doc.line_starts[cl - 1]:doc.src.find("\n", doc.line_starts[cl - 1])].strip() if cl else ""
            sec = doc.section_at(r.start)
            plan.append((r, _evidence.Entry("", r.status, claim, sec.label if sec else "", reason, srcs,
                                            _evidence.now(), legacy=r.text)))
        bib = self.zot()._bib_entries() if self._bibfile() else {}
        for _, e in plan:
            for s in e.sources:
                if bib and s.bibkey and s.bibkey not in bib:
                    # a typo'd key in an old line (`mckee_ostriker074`): the .bib key it starts with
                    near = [k for k in bib if s.bibkey.startswith(k) and len(s.bibkey) - len(k) <= 2]
                    if near:
                        s.bibkey = max(near, key=len)
        say(f"{len(plan)} inline result(s); finding the quotes in Zotero…")
        online = self._check_sources([s for _, e in plan for s in e.sources], strict=False)
        quotes = [s for _, e in plan for s in e.sources if s.quote]
        found = [s for s in quotes if not s.highlight.startswith("none")]
        report = [f"{len(plan)} result(s), {len(quotes)} quote(s): "
                  + (f"{len(found)} found in their PDFs, {len(quotes) - len(found)} not"
                     if online else "Zotero is not running, quotes not checked")]
        for r, e in plan:
            for s in e.sources:
                if s.highlight.startswith("none"):
                    report.append(f"  l.{r.line} {s.bibkey} p.{s.page}: {s.highlight[6:]}  «{_short(s.quote, 70)}»")
            if not e.sources and e.status != "UNVERIFIED":
                report.append(f"  l.{r.line}: no source/quote recognised: {_short(r.text, 100)}")
        if not apply:
            return "\n".join(report + ["dry run: nothing changed (apply=True / --apply migrates)"])
        lib = self.library()
        used = {r.id for r in _verify.results(doc) if r.id}
        for _, e in plan:
            e.id = lib.next_id(_evidence.id_prefix(e.sources), used)
            used.add(e.id)
        lines = {}
        for r, e in plan:
            indent = re.match(r"[ \t]*", doc.src[r.start:]).group(0)
            lines.setdefault((r.status, r.text), []).append(indent + _verify.short_line(e.status, e.id, e.also) + "\n")

        def fn(d):
            queue = {k: list(v) for k, v in lines.items()}
            return [TextEdit(r.start, r.end, queue[(r.status, r.text)].pop(0))
                    for r in _verify.results(d) if not r.id and queue.get((r.status, r.text))]
        self.proj.transact(fn, reason="migrate verifications to the evidence library")
        for _, e in plan:
            lib.put(e)
        lib.save()
        if online:
            for i, (_, e) in enumerate(plan, 1):
                say(f"[{i}/{len(plan)}] {e.id}: Zotero highlights and note")
                self._push(e)
                lib.put(e)
                lib.save()
        synced = sum(1 for _, e in plan if e.zotero == "synced")
        return "\n".join(report + [f"migrated {len(plan)} result(s) to short ids; {synced} filed in Zotero"
                                   + ("" if online else " (Zotero offline: run `redline evidence sync`)")])

    def verification_report(self) -> str:
        return _verify.report(self.proj.doc())
