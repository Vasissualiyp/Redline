"""Pure operations on a parsed document. Every function returns TextEdits.

Nothing here touches the disk; `apply()` turns edits into new text, and the
nvim front end applies the same edits to its buffer so undo keeps working.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .model import Change, Document, Marker, RedlineError, TextEdit

CITE_CMDS = r"(?:cite[a-zA-Z]*|autoref|ref|eqref|cref|Cref|pageref)"
_UNMBOX = re.compile(r"\\mbox\{(\\" + CITE_CMDS + r"\*?(?:\[[^\]]*\])*\{[^{}]*\})\}")


@dataclass
class Decision:
    item: Change
    action: str   # accept | reject | skip | drop
    reason: str = ""


def apply(src: str, edits: list[TextEdit]) -> str:
    out, pos = [], 0
    for e in sorted(edits, key=lambda e: (e.start, e.end)):
        if e.start < pos:
            raise RedlineError(f"overlapping edits at offset {e.start}")
        out.append(src[pos:e.start])
        out.append(e.text)
        pos = e.end
    out.append(src[pos:])
    return "".join(out)


def unmbox(s: str) -> str:
    """Undo the \\mbox{\\citep{..}} protection added for strike-out text."""
    return _UNMBOX.sub(r"\1", s)


def _strip_markers(src: str, start: int, end: int, markers: list[Marker]) -> str:
    """Text of src[start:end] with review markers inside it removed."""
    pieces, pos = [], start
    for mk in markers:
        if start <= mk.start < end:
            pieces.append(src[pos:mk.start])
            pos = min(mk.end, end)
    pieces.append(src[pos:end])
    return "".join(pieces)


def _marker_removal(src: str, mk: Marker) -> TextEdit:
    """Remove a marker comment without changing how TeX joins the lines."""
    before = src[mk.start - 1] if mk.start > 0 else "\n"
    if before not in " \t\n":
        # "word%N\n   next" behaves like "wordnext"; drop the newline and indent too
        m = re.compile(r"\n[ \t]*").match(src, mk.end)
        return TextEdit(mk.start, m.end() if m else mk.end, "")
    ws = mk.start
    while ws > 0 and src[ws - 1] in " \t":
        ws -= 1
    return TextEdit(ws, mk.end, "")


def decide(doc: Document, items: list[Change], action: str, honor_markers: bool = True) -> list[Decision]:
    out = []
    for it in items:
        mk = it.marker
        if action == "markers":
            if mk is None:
                out.append(Decision(it, "skip", "no marker"))
            elif mk.kind == "N":
                out.append(Decision(it, "skip", "%N: kept pending"))
            else:
                out.append(Decision(it, "accept" if mk.kind == "Y" else "reject", f"%{mk.kind}"))
        elif honor_markers and mk is not None:
            if mk.kind == "N":
                out.append(Decision(it, "skip", "%N: kept pending"))
            elif mk.kind == "X":
                out.append(Decision(it, "reject", "%X"))
            else:
                out.append(Decision(it, "accept", "%Y"))
        else:
            out.append(Decision(it, action))
    return out


def resolve(doc: Document, decisions: list[Decision], *, comments: str = "auto",
            keep_regex: str = r"TODO", explicit: bool = False) -> list[TextEdit]:
    """Edits that accept/reject the decided items.

    comments: "auto" drops comments unless they match keep_regex, "keep" keeps
    them, "drop" drops all. A standalone comment selected explicitly (by id) is
    always dropped: resolving it is the point.
    """
    src = doc.src
    keep_re = re.compile(keep_regex) if keep_regex else None
    edits: list[TextEdit] = []

    def keep_comment(c: Change) -> bool:
        if comments == "keep":
            return True
        if comments == "drop":
            return False
        return bool(keep_re and keep_re.search(c.body(src) or ""))

    for d in decisions:
        it = d.item
        if d.action == "skip":
            continue
        if it.kind == "comment":
            if explicit or not keep_comment(it):
                d.action = "drop"
                edits.append(TextEdit(it.start, it.end, ""))
            else:
                d.action = "skip"
                d.reason = "comment kept"
                continue
        else:
            if it.kind == "added":
                arg = it.args[0] if d.action == "accept" else None
            elif it.kind == "deleted":
                arg = None if d.action == "accept" else it.args[0]
            elif it.kind == "replaced":
                arg = it.args[0] if d.action == "accept" else it.args[1]
            else:  # highlight
                arg = it.args[0]
            text = "" if arg is None else _strip_markers(src, arg.start, arg.end, doc.markers)
            if d.action == "reject" or it.kind == "deleted":
                text = unmbox(text)
            drop_comment = it.comment is not None and not keep_comment(it.comment)
            end = it.comment.end if drop_comment else it.end
            new_struct = _new_struct(src, it.start, end) if it.kind == "added" else None
            shell = None
            if new_struct and d.action == "reject":
                shell = new_struct[1]   # the \item/heading came with the change: drop it too
            elif new_struct:
                edits.append(TextEdit(*new_struct[0], ""))   # accepted: only the tag line goes
            if shell is None and not text:
                shell = _empty_shell(src, it.start, end)
            if shell:
                # the whole item / heading text went away: remove its \item or heading too
                edits.append(TextEdit(shell[0], shell[1], ""))
            else:
                edits.append(TextEdit(it.start, it.end, text))
                if drop_comment:
                    edits.append(TextEdit(it.comment.start, it.comment.end, ""))
        mk = it.marker
        if mk is not None and not (it.start <= mk.start < it.end):
            if not (it.comment and it.comment.start <= mk.start < it.comment.end):
                edits.append(_marker_removal(src, mk))
    return tidy_lines(src, edits)


_ITEM_BEFORE = re.compile(r"\\item\b(?:[ \t]*\[[^\]]*\])?[ \t]*$")
_ITEM_AFTER = re.compile(r"\s*(?=\\item\b|\\end\{)")
_HEAD_BEFORE = re.compile(r"\\(?:sub)*section\*?[ \t]*(?:\[[^\]]*\])?[ \t]*\{[ \t]*$")
_HEAD_AFTER = re.compile(r"[ \t]*\}(?:[ \t]*\\label\{[^{}]*\})?")


_NEW_STRUCT = re.compile(r"\n[ \t]*%\+(item|heading)[ \t]*\n[ \t]*"
                         r"(\\item\b(?:[ \t]*\[[^\]]*\])?[ \t]*|\\(?:sub)*section\*?[ \t]*(?:\[[^\]]*\])?[ \t]*\{[ \t]*)$")


def _new_struct(src: str, start: int, end: int):
    r"""For an added change right after a `%+item`/`%+heading` tagged \item or heading:
    ((tag line span), (span removing tag + \item/heading + change)), else None."""
    before = src[max(0, start - 300):start]
    m = _NEW_STRUCT.search(before)
    if not m:
        return None
    base = start - len(before)
    tag = (base + m.start(), base + m.start() + len(m.group(0).split("\n")[1]) + 1)
    stop = end
    if m.group(1) == "heading":
        a = _HEAD_AFTER.match(src, end)
        if not a:
            return None
        stop = a.end()
    return tag, (base + m.start(), stop)


def _empty_shell(src: str, start: int, end: int) -> tuple[int, int] | None:
    r"""Span to delete when removing src[start:end] would leave an empty \item
    or an empty heading behind (the \item/heading included), else None."""
    before = src[max(0, start - 200):start]
    m = _ITEM_BEFORE.search(before)
    if m and _ITEM_AFTER.match(src, end):
        return start - (len(before) - m.start()), end
    m = _HEAD_BEFORE.search(before)
    if m:
        a = _HEAD_AFTER.match(src, end)
        if a:
            return start - (len(before) - m.start()), a.end()
    return None


def tidy_lines(src: str, edits: list[TextEdit]) -> list[TextEdit]:
    """Never turn a text line into a blank line (that would start a new paragraph).

    Edits are grouped into clusters of touching lines; if a cluster leaves its
    lines whitespace-only, the whole region including its newline is removed.
    """
    edits = sorted(edits, key=lambda e: (e.start, e.end))
    if not edits:
        return []
    clusters: list[list[TextEdit]] = [[edits[0]]]
    for e in edits[1:]:
        last = clusters[-1][-1]
        gap = src[last.end:e.start]
        if "\n" not in gap:
            clusters[-1].append(e)
        else:
            clusters.append([e])
    out: list[TextEdit] = []
    for cl in clusters:
        ls = src.rfind("\n", 0, cl[0].start) + 1
        le = src.find("\n", cl[-1].end)
        if le < 0:
            out.extend(cl)
            continue
        region = src[ls:le]
        new = apply(src[ls:le], [TextEdit(e.start - ls, e.end - ls, e.text) for e in cl])
        if region.strip() and not new.strip():
            out.append(TextEdit(ls, le + 1, ""))
        else:
            out.extend(cl)
    return out


def set_marker(doc: Document, it: Change, kind: str | None, note: str = "") -> list[TextEdit]:
    """Add, change or clear (kind=None) the review marker of an item."""
    src = doc.src
    if it.marker is not None:
        mk = it.marker
        if kind is None:
            return tidy_lines(src, [_marker_removal(src, mk)])
        return [TextEdit(mk.start, mk.end, _marker_text(kind, note))]
    if kind is None:
        return []
    pos = it.full_end
    m = re.compile(r"[ \t]*").match(src, pos)
    after_ws = m.end()
    nxt = src[after_ws] if after_ws < len(src) else "\n"
    if nxt == "\n":
        return [TextEdit(after_ws, after_ws, (" " if after_ws == pos else "") + _marker_text(kind, note))]
    # Prefer the end of the line (exactly reversible) when nothing else on the
    # line could claim the marker: no later change and no existing % comment.
    le = src.find("\n", pos)
    le = len(src) if le < 0 else le
    later = any(pos <= o.start < le for o in doc.items if o is not it)
    has_comment = any(pos <= c < le for c in (mk.start for mk in doc.markers)) or \
        _has_comment(src, pos, le)
    if not later and not has_comment:
        tail = src[pos:le].rstrip(" \t")
        at = pos + len(tail)
        return [TextEdit(at, at, " " + _marker_text(kind, note))]
    # text follows on the same line: end the line with the marker. "x %N\nrest"
    # typesets like "x rest", and "x%N\nrest" like "xrest".
    return [TextEdit(after_ws, after_ws, _marker_text(kind, note) + "\n")]


def _has_comment(src: str, lo: int, hi: int) -> bool:
    from .parse import _comment_start
    ls = src.rfind("\n", 0, lo) + 1
    c = _comment_start(src, ls, hi)
    return c is not None


def _marker_text(kind: str, note: str) -> str:
    if kind not in ("Y", "N", "X"):
        raise RedlineError(f"marker must be Y, N or X, not {kind!r}")
    note = " ".join(note.split())
    return f"%{kind}" + (f" {note}" if note else "")
