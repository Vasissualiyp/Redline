"""Claim verification through `%`-comment lines.

Each line sits directly after the claim it refers to:

    The sink accretes at 1e-3 Msun/yr \\citep{abel02}.
    % VERIFY refcheck: «The sink accretes at 1e-3 Msun/yr»    <- request (human)
    % VERIFIED abel02#1                                       <- result (agent)

Requests name a method (refcheck: the cited/added literature; code: the
analysis repo; any). The agent replaces each request by a result line:
VERIFIED, REFUTED (the source says otherwise; a correction is proposed
separately) or UNVERIFIED (nothing found). A result names an evidence id
(`bibkey#n`, `code#n`, `nosrc#n`; other sources as ` (+key2)`); the quotes,
pages and Zotero highlights behind it live in the evidence library
(evidence.py). Older results carry the evidence inline and still parse.
The same lines work when typed in the Overleaf editor.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .model import Document, RedlineError, TextEdit

METHODS = ("refcheck", "code", "any")
STATUSES = ("VERIFIED", "REFUTED", "UNVERIFIED")

_REQ = re.compile(r"^[ \t]*%[ \t]*VERIFY(?![A-Za-z])[ \t]*(refcheck|code|any)?[ \t]*:?[ \t]*(.*)$", re.M)
_RES = re.compile(r"^[ \t]*%[ \t]*(VERIFIED|REFUTED|UNVERIFIED)(?![A-Za-z])[ \t]*(.*)$", re.M)
_CLAIM = re.compile(r"«(.*?)»")
ID_RE = r"[A-Za-z0-9_:.\-]+#\d+"
_SHORT = re.compile(rf"^({ID_RE})(?:[ \t]+\(\+([^)]*)\))?[ \t]*$")


@dataclass
class Request:
    id: str
    line: int          # 1-based line of the % VERIFY comment
    method: str
    note: str
    claim: str         # quoted claim, or the claim line(s) when none was quoted
    claim_line: int    # last line of the claim
    start: int
    end: int           # offsets of the comment line, newline included

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("id", "line", "method", "note", "claim", "claim_line")}


@dataclass
class Result:
    status: str
    text: str
    line: int
    start: int = 0
    end: int = 0                  # offsets of the comment line, newline included
    id: str | None = None         # evidence id; None for an old inline result
    also: tuple[str, ...] = ()    # further sources named on the line

    def as_dict(self) -> dict:
        return {"status": self.status, "text": self.text, "line": self.line, "id": self.id}


def _claim_line_before(src: str, line_starts: list[int], line_no: int) -> int:
    """Nearest preceding line that is not blank and not a pure comment."""
    n = line_no - 1
    while n >= 1:
        a = line_starts[n - 1]
        b = line_starts[n] - 1 if n < len(line_starts) else len(src)
        t = src[a:b].strip()
        if t and not t.startswith("%"):
            return n
        if not t:
            return 0  # blank line: the request is not attached to a claim
        n -= 1
    return 0


def requests(doc: Document) -> list[Request]:
    src = doc.src
    out = []
    for m in _REQ.finditer(src, doc.body_start, doc.body_end):
        line = doc.line_of(m.start())
        method = m.group(1) or "any"
        rest = m.group(2).strip()
        q = _CLAIM.search(rest)
        claim_line = _claim_line_before(src, doc.line_starts, line)
        if q:
            claim = q.group(1).strip()
            note = (rest[:q.start()] + rest[q.end():]).strip(" :-")
        else:
            note = rest
            claim = ""
            if claim_line:
                a = doc.line_starts[claim_line - 1]
                claim = src[a:src.find("\n", a)].strip()
        end = src.find("\n", m.end())
        end = len(src) if end < 0 else end + 1
        rid = hashlib.sha1(f"{method}\0{claim}\0{note}".encode()).hexdigest()[:6]
        out.append(Request(rid, line, method, note, claim, claim_line, m.start(), end))
    return out


def results(doc: Document) -> list[Result]:
    out = []
    for m in _RES.finditer(doc.src, doc.body_start, doc.body_end):
        text = m.group(2).strip()
        end = doc.src.find("\n", m.end())
        end = len(doc.src) if end < 0 else end + 1
        short = _SHORT.match(text)
        also = tuple(k.strip() for k in re.split(r"[,+]", short.group(2)) if k.strip()) \
            if short and short.group(2) else ()
        out.append(Result(m.group(1), text, doc.line_of(m.start()), m.start(), end,
                          short.group(1) if short else None, also))
    return out


def _one_line(s: str) -> str:
    return " ".join((s or "").split())


def request_line(method: str, claim: str, note: str = "") -> str:
    if method not in METHODS:
        raise RedlineError(f"method must be one of {METHODS}")
    claim = _one_line(claim).replace("«", "\"").replace("»", "\"")
    if len(claim) > 300:
        claim = claim[:299] + "…"
    note = _one_line(note)
    return f"% VERIFY {method}:" + (f" {note}" if note else "") + (f" «{claim}»" if claim else "")


def add_request(doc: Document, after_line: int, method: str, claim: str, note: str = "") -> list[TextEdit]:
    """Insert a request after `after_line` (1-based, the claim's last line)."""
    if not 1 <= after_line <= len(doc.line_starts):
        raise RedlineError(f"line {after_line} out of range")
    pos = doc.line_starts[after_line] if after_line < len(doc.line_starts) else len(doc.src)
    # skip result/request lines already attached to this claim
    while True:
        m = re.compile(r"[ \t]*%[ \t]*(VERIFY|VERIFIED|REFUTED|UNVERIFIED)\b[^\n]*\n").match(doc.src, pos)
        if not m:
            break
        pos = m.end()
    text = request_line(method, claim, note) + "\n"
    if pos == len(doc.src) and not doc.src.endswith("\n"):
        text = "\n" + text
    return [TextEdit(pos, pos, text)]


def result_line(status: str, source: str, evidence: str) -> str:
    status = status.upper()
    if status not in STATUSES:
        raise RedlineError(f"status must be one of {STATUSES}")
    source, evidence = _one_line(source), _one_line(evidence)
    if status != "UNVERIFIED" and not (source and evidence):
        raise RedlineError("VERIFIED/REFUTED need both a source (bib key, or code:path:line) "
                           "and the evidence (exact quote with §/page, or the computed value)")
    body = f"{source}: {evidence}" if source else evidence
    return f"% {status} {body}".rstrip()


def short_line(status: str, eid: str, also: list[str] | tuple[str, ...] = ()) -> str:
    status = status.upper()
    if status not in STATUSES:
        raise RedlineError(f"status must be one of {STATUSES}")
    if not re.fullmatch(ID_RE, eid):
        raise RedlineError(f"bad evidence id {eid!r}")
    return f"% {status} {eid}" + (f" (+{', '.join(also)})" if also else "")


def record(doc: Document, status: str, source: str, evidence: str, request_id: str | None = None,
           after_line: int | None = None) -> list[TextEdit]:
    """Replace a request by an inline result, or add one after a claim line."""
    return place(doc, result_line(status, source, evidence), request_id, after_line)


def place(doc: Document, line: str, request_id: str | None = None,
          after_line: int | None = None) -> list[TextEdit]:
    """Put a result line in place of a request, or after a claim line."""
    line = line.rstrip("\n") + "\n"
    if request_id:
        hits = [r for r in requests(doc) if r.id == request_id or r.id.startswith(request_id)]
        if not hits:
            raise RedlineError(f"no open verification request {request_id!r}; it may be resolved already")
        r = hits[0]
        indent = re.match(r"[ \t]*", doc.src[r.start:]).group(0)
        return [TextEdit(r.start, r.end, indent + line)]
    if after_line is None:
        raise RedlineError("pass request_id, or the claim (anchor) to verify")
    edits = add_request(doc, after_line, "any", "")  # position only
    pos = edits[0].start
    return [TextEdit(pos, pos, line)]


def report(doc: Document) -> str:
    reqs = requests(doc)
    res = results(doc)
    counts = {s: sum(1 for r in res if r.status == s) for s in STATUSES}
    lines = [f"{counts['VERIFIED']} verified, {counts['REFUTED']} refuted, "
             f"{counts['UNVERIFIED']} unverified, {len(reqs)} open request(s)"]
    for r in reqs:
        sec = doc.section_at(doc.line_starts[r.line - 1])
        lines.append(f"  [{r.id}] l.{r.line} {r.method}{' §' + sec.label if sec else ''}: «{r.claim}»"
                     + (f"  note: {r.note}" if r.note else ""))
    for r in res:
        if r.status != "VERIFIED":
            lines.append(f"  l.{r.line} {r.status}: {r.text}")
    old = sum(1 for r in res if r.id is None)
    if old:
        lines.append(f"  {old} result(s) still carry their evidence inline "
                     "(`redline evidence migrate` moves it into the library)")
    return "\n".join(lines)


# ---- citations a change introduces ------------------------------------------------
_CITE = re.compile(r"\\[A-Za-z]*cite[A-Za-z]*\*?(?:\s*\[[^\]]*\]){0,2}\s*\{([^{}]*)\}")


def cite_keys(text: str | None) -> set[str]:
    return {k.strip() for m in _CITE.finditer(text or "") for k in m.group(1).split(",") if k.strip()}


def results_after(doc: Document, pos: int) -> list[Result]:
    """Result lines in the block of %-comment lines right after the line holding pos."""
    by_line = {r.line: r for r in results(doc)}
    lines = doc.src.splitlines()
    n, out = doc.line_of(pos) + 1, []
    while n <= len(lines) and lines[n - 1].lstrip().startswith("%"):
        if n in by_line:
            out.append(by_line[n])
        n += 1
    return out


def result_keys(r: Result) -> set[str]:
    return ({r.id.rsplit("#", 1)[0], *r.also} if r.id else set())


def new_citations(doc: Document, it) -> set[str]:
    """Keys a pending change cites that the text it replaces did not."""
    if it.kind not in ("added", "replaced"):
        return set()
    return cite_keys(it.new(doc.src)) - cite_keys(it.old(doc.src))


def unverified_citations(doc: Document, it) -> set[str]:
    """New keys of a change with no % VERIFIED/REFUTED result naming them right after it."""
    new = new_citations(doc, it)
    if not new:
        return set()
    done = set().union(*(result_keys(r) for r in results_after(doc, it.full_end - 1)
                         if r.status in ("VERIFIED", "REFUTED")))
    return new - done
