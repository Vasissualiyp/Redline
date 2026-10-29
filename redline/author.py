"""Turn anchor-based edit requests into well-formed `changes` markup.

The agent never writes \\added/\\deleted by hand: it names the exact text to
change (like an editor's search/replace) and the markup is generated here,
with the lint fixes applied up front so the result compiles.
"""
from __future__ import annotations

import re

from .lint import _PAR, balanced, mbox_fixes, split_paragraphs, wrap
from .model import Change, Document, RedlineError, Span, TextEdit
from .ops import apply
from .parse import _ATTACH_GAP, mask
from .select import find_section


def _region(doc: Document, section: str | None) -> tuple[int, int]:
    if section:
        s = find_section(doc, section)
        return s.start, s.end
    return doc.body_start, doc.body_end


def _context(doc: Document, pos: int) -> str:
    ln = doc.line_of(pos)
    return f"line {ln}"


def locate(doc: Document, anchor: str, section: str | None = None,
           occurrence: int | None = None) -> tuple[int, int]:
    """Find the unique occurrence of `anchor` (whitespace-tolerant fallback)."""
    if not anchor or not anchor.strip():
        raise RedlineError("anchor text is empty")
    lo, hi = _region(doc, section)
    src = doc.src
    hits = []
    i = src.find(anchor, lo, hi)
    while i >= 0:
        hits.append((i, i + len(anchor)))
        i = src.find(anchor, i + 1, hi)
    if not hits:
        pat = r"\s+".join(re.escape(w) for w in anchor.split())
        hits = [(m.start(), m.end()) for m in re.finditer(pat, src[:hi]) if m.start() >= lo]
    if not hits:
        raise RedlineError("anchor text not found" + (f" in section {section!r}" if section else "")
                           + ". Copy it exactly from read() output (without line numbers).")
    if occurrence is not None:
        if not 1 <= occurrence <= len(hits):
            raise RedlineError(f"occurrence {occurrence} out of range (found {len(hits)})")
        return hits[occurrence - 1]
    if len(hits) > 1:
        lines = ", ".join(str(doc.line_of(s)) for s, _ in hits[:10])
        raise RedlineError(f"anchor text occurs {len(hits)} times (lines {lines}); "
                           "make it longer, pass section=, or pass occurrence=N")
    return hits[0]


def _check_span(doc: Document, s: int, e: int, *, what: str) -> None:
    """The span must not cut through or sit inside pending markup or a % comment."""
    for it in doc.items:
        lo, hi = it.start, it.full_end
        if lo <= s and e <= hi and not (s == e and s in (lo, hi)):
            raise RedlineError(f"{what} lies inside pending change {it.label} ({it.id}); "
                               "use revise_change, or accept/reject it first")
        if s < lo < e or s < hi < e:
            if not (s <= lo and hi <= e):
                raise RedlineError(f"{what} cuts through pending change {it.label} ({it.id}); "
                                   "widen or narrow it to avoid the markup")
            raise RedlineError(f"{what} contains pending change {it.label} ({it.id}); "
                               "accept/reject it first, or edit around it")
    masked, _, comments = mask(doc.src)
    for (c, le) in comments:
        if c < e <= le:
            raise RedlineError(f"{what} ends inside a % comment (line {doc.line_of(c)}); "
                               "end it before the %, or the closing brace would be commented out")


def _clean_arg(text: str, what: str) -> str:
    if not balanced(text):
        raise RedlineError(f"{what} has unbalanced braces")
    masked, _, comments = mask(text)
    if comments and comments[-1][1] == len(text):
        text += "\n"  # a trailing % comment would swallow the closing brace
    return text


def _protect_old(text: str) -> str:
    """Wrap citations in \\mbox so ulem can strike them out."""
    masked, _, _ = mask(text)
    return apply(text, mbox_fixes(text, masked, Span(0, len(text))))


def _check_strikeable(text: str) -> None:
    masked, _, _ = mask(text)
    if re.search(r"\\begin\{|\\\[|\$\$", masked):
        raise RedlineError("the text to remove contains an environment or display math, which "
                           "cannot be struck out. Use plain_edit for it and add_comment to explain.")
    if "\\footnote" in masked:
        raise RedlineError("the text to remove contains a \\footnote, which cannot be struck out; "
                           "keep the footnote outside the deleted span or use plain_edit")


def _comment_markup(text: str) -> str:
    text = _clean_arg(text.strip(), "comment")
    if _PAR.search(text):
        text = re.sub(r"\n[ \t]*\n\s*", " ", text)
    if text.startswith("\\cite"):
        text = "Note: " + text
    return "\\chcomment{" + text + "}"


def _paired(new: str, old: str | None) -> str:
    """Markup for new/old text, split per paragraph when needed."""
    if old is None:
        parts = split_paragraphs(new)
        return "".join(p if i % 2 or not p.strip() else wrap("added", p) for i, p in enumerate(parts))
    if not new:
        parts = split_paragraphs(old)
        return "".join(p if i % 2 or not p.strip() else wrap("deleted", _protect_old(p))
                       for i, p in enumerate(parts))
    np_ = split_paragraphs(new)
    op_ = split_paragraphs(old)
    if len(np_) == 1 and len(op_) == 1:
        return wrap("replaced", new, _protect_old(old))
    out = []
    n_par, o_par = np_[::2], op_[::2]
    seps = np_[1::2] or op_[1::2]
    for i in range(max(len(n_par), len(o_par))):
        if i:
            out.append(seps[i - 1] if i - 1 < len(seps) else "\n\n")
        n = n_par[i] if i < len(n_par) else ""
        o = o_par[i] if i < len(o_par) else ""
        if n.strip() and o.strip():
            out.append(wrap("replaced", n, _protect_old(o)))
        elif n.strip():
            out.append(wrap("added", n))
        elif o.strip():
            out.append(wrap("deleted", _protect_old(o)))
    return "".join(out)


def _float_end(doc: Document, pos: int) -> int | None:
    for fl in doc.floats:
        if fl.start <= pos < fl.end:
            return fl.end
    return None


def _guard(doc: Document, pos: int) -> str:
    """'{}' if new markup at pos would swallow an existing standalone comment.

    The parser attaches a \\chcomment that directly follows a change to it, so
    resolving the new change would drop a comment that belongs to someone else.
    """
    m = _ATTACH_GAP.match(doc.src, pos)
    if m and doc.src.startswith("\\chcomment", m.end()):
        return "{}"
    return ""


def _with_comment(doc: Document, pos: int, markup: str, comment: str | None) -> list[TextEdit]:
    """Edits inserting `markup` at pos, followed by its explaining comment."""
    if not comment:
        return [TextEdit(pos, pos, markup + _guard(doc, pos))]
    c = _comment_markup(comment)
    fe = _float_end(doc, pos)
    if fe is not None:
        return [TextEdit(pos, pos, markup), TextEdit(fe, fe, c)]
    return [TextEdit(pos, pos, markup + c)]


def propose_replace(doc: Document, old_text: str, new_text: str, comment: str | None = None,
                    section: str | None = None, occurrence: int | None = None) -> list[TextEdit]:
    s, e = locate(doc, old_text, section, occurrence)
    _check_span(doc, s, e, what="old_text")
    old = doc.src[s:e]
    if new_text == old:
        raise RedlineError("new_text is identical to old_text")
    _check_strikeable(old)
    new = _clean_arg(new_text, "new_text")
    markup = _paired(new, old)
    edits = _with_comment(doc, e, markup, comment)
    edits[0] = TextEdit(s, e, edits[0].text)
    return edits


def propose_delete(doc: Document, old_text: str, comment: str | None = None,
                   section: str | None = None, occurrence: int | None = None) -> list[TextEdit]:
    s, e = locate(doc, old_text, section, occurrence)
    _check_span(doc, s, e, what="old_text")
    old = doc.src[s:e]
    _check_strikeable(old)
    edits = _with_comment(doc, e, _paired("", old), comment)
    edits[0] = TextEdit(s, e, edits[0].text)
    return edits


def propose_insert(doc: Document, text: str, after: str | None = None, before: str | None = None,
                   comment: str | None = None, section: str | None = None,
                   occurrence: int | None = None) -> list[TextEdit]:
    if bool(after) == bool(before):
        raise RedlineError("give exactly one of after= or before=")
    s, e = locate(doc, after or before, section, occurrence)
    pos = e if after else s
    _check_span(doc, pos, pos, what="insertion point")
    new = _clean_arg(text, "text")
    return _with_comment(doc, pos, _paired(new, None), comment)


def add_comment(doc: Document, anchor: str, text: str, section: str | None = None,
                occurrence: int | None = None) -> list[TextEdit]:
    s, e = locate(doc, anchor, section, occurrence)
    # commenting right after a pending change attaches the comment to it
    for it in doc.items:
        if it.start <= s and e <= it.end and it.comment is None and it.kind != "comment":
            e = it.end
            break
    else:
        _check_span(doc, e, e, what="comment position")
    fe = _float_end(doc, e)
    pos = fe if fe is not None else e
    return [TextEdit(pos, pos, _comment_markup(text))]


def plain_edit(doc: Document, old_text: str, new_text: str, section: str | None = None,
               occurrence: int | None = None) -> list[TextEdit]:
    s, e = locate(doc, old_text, section, occurrence)
    _check_span(doc, s, e, what="old_text")
    _clean_arg(new_text, "new_text")
    return [TextEdit(s, e, new_text)]


def revise_change(doc: Document, it: Change, new_text: str | None = None,
                  comment: str | None = None) -> list[TextEdit]:
    edits: list[TextEdit] = []
    src = doc.src
    if new_text is not None:
        new = _clean_arg(new_text, "new_text")
        if _PAR.search(new):
            raise RedlineError("revised text cannot contain a blank line; propose the extra "
                               "paragraph as a separate insertion")
        if it.kind in ("added", "replaced"):
            a = it.args[0]
            edits.append(TextEdit(a.start, a.end, new))
        elif it.kind == "deleted":
            edits.append(TextEdit(it.start, it.end,
                                  wrap("replaced", new, it.args[0].text(src))))
        elif it.kind == "comment":
            a = it.args[0]
            edits.append(TextEdit(a.start, a.end, _comment_markup(new)[len("\\chcomment{"):-1]))
        else:
            raise RedlineError(f"cannot revise a {it.kind}")
    if comment is not None:
        if it.kind == "comment":
            raise RedlineError("use new_text to revise a standalone comment")
        if it.comment is not None:
            a = it.comment.args[0]
            edits.append(TextEdit(a.start, a.end, _comment_markup(comment)[len("\\chcomment{"):-1]))
        else:
            fe = _float_end(doc, it.end)
            pos = fe if fe is not None else it.end
            edits.append(TextEdit(pos, pos, _comment_markup(comment)))
    if not edits:
        raise RedlineError("nothing to revise: pass new_text and/or comment")
    return edits
