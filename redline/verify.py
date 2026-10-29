"""Claim verification through `%`-comment lines.

Each line sits directly after the claim it refers to:

    The sink accretes at 1e-3 Msun/yr \\citep{abel02}.
    % VERIFY refcheck: «The sink accretes at 1e-3 Msun/yr»    <- request (human)
    % VERIFIED abel02: "accretion rates of ~1e-3" — §3, p.5   <- result (agent)

Requests name a method (refcheck: the cited/added literature; code: the
analysis repo; any). The agent replaces each request by a result line:
VERIFIED (source + quote/evidence), REFUTED (the source says otherwise; a
correction is proposed separately) or UNVERIFIED (nothing found, reason).
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
    return [Result(m.group(1), m.group(2).strip(), doc.line_of(m.start()))
            for m in _RES.finditer(doc.src, doc.body_start, doc.body_end)]


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


def record(doc: Document, status: str, source: str, evidence: str, request_id: str | None = None,
           after_line: int | None = None) -> list[TextEdit]:
    """Replace a request by its result, or add a result after a claim line."""
    line = result_line(status, source, evidence) + "\n"
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
    return "\n".join(lines)
