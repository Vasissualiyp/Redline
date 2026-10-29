"""Scanner for the LaTeX `changes` markup (plus legacy coauthor macros).

The scanner works on a *masked* copy of the source in which `%` comments and
verbatim-like environments are blanked out with spaces (newlines kept), so all
offsets stay identical to the original text while brace matching never trips
over braces inside comments.
"""
from __future__ import annotations

import hashlib
import re

from .model import Change, Document, Marker, RedlineError, Section, Span

# macro name -> (kind, number of mandatory args)
CHANGES_MACROS: dict[str, tuple[str, int]] = {
    "added": ("added", 1), "chadded": ("added", 1),
    "deleted": ("deleted", 1), "chdeleted": ("deleted", 1),
    "replaced": ("replaced", 2), "chreplaced": ("replaced", 2),
    "chcomment": ("comment", 1),
    "highlight": ("highlight", 1), "chhighlight": ("highlight", 1),
}

# Hand-rolled macros used by coauthors in older drafts (configurable).
DEFAULT_LEGACY: dict[str, str] = {
    "skadd": "added", "add": "added",
    "skcut": "deleted", "cut": "deleted",
    "skcomment": "comment", "ymcomment": "comment", "vpcomment": "comment",
    "nwm": "comment", "note": "comment",
}

VERBATIM_ENVS = ("verbatim", "verbatim*", "comment", "lstlisting", "minted")
FLOAT_ENVS = ("figure", "figure*", "table", "table*")
SECTION_LEVELS = {"section": 1, "subsection": 2, "subsubsection": 3}

_TOKEN = re.compile(r"\\(?:([A-Za-z@]+)|.)|[{}]", re.S)
_MARKER = re.compile(r"%([YNX])(?![A-Za-z0-9])[ \t:,-]*(.*)")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
# Gap allowed between a change and a \chcomment that explains it.
_ATTACH_GAP = re.compile(r"[ \t]*\n?[ \t]*[.,;:)]?[ \t]*\n?[ \t]*")


def _line_starts(src: str) -> list[int]:
    starts = [0]
    for m in re.finditer(r"\n", src):
        starts.append(m.end())
    return starts


def mask(src: str) -> tuple[str, list[Span], list[tuple[int, int]]]:
    """Blank out comments and verbatim bodies.

    Returns (masked text, verbatim spans, comment ranges).
    """
    out = list(src)
    verb: list[Span] = []
    env_re = re.compile(r"\\begin\{(" + "|".join(re.escape(e) for e in VERBATIM_ENVS) + r")\}")
    pos = 0
    while True:
        m = env_re.search(src, pos)
        if not m:
            break
        # ignore a \begin{...} that sits inside a % comment
        ls = src.rfind("\n", 0, m.start()) + 1
        if _comment_start(src, ls, m.start()) is not None:
            pos = m.end()
            continue
        end_tag = "\\end{" + m.group(1) + "}"
        e = src.find(end_tag, m.end())
        e = len(src) if e < 0 else e
        verb.append(Span(m.end(), e))
        for i in range(m.end(), e):
            if out[i] != "\n":
                out[i] = " "
        pos = e + len(end_tag)

    comments: list[tuple[int, int]] = []
    vi = 0
    ls = 0
    n = len(src)
    while ls < n:
        le = src.find("\n", ls)
        le = n if le < 0 else le
        while vi < len(verb) and verb[vi].end < ls:
            vi += 1
        if not (vi < len(verb) and verb[vi].start <= ls < verb[vi].end):
            c = _comment_start(src, ls, le)
            if c is not None:
                comments.append((c, le))
                for i in range(c, le):
                    out[i] = " "
        ls = le + 1
    return "".join(out), verb, comments


def _comment_start(src: str, ls: int, le: int) -> int | None:
    i = src.find("%", ls, le)
    while i >= 0:
        bs = 0
        j = i - 1
        while j >= ls and src[j] == "\\":
            bs += 1
            j -= 1
        if bs % 2 == 0:
            return i
        i = src.find("%", i + 1, le)
    return None


def match_group(masked: str, pos: int) -> int:
    """`masked[pos]` must be '{'; return the offset of its matching '}'."""
    if masked[pos] != "{":
        raise RedlineError(f"expected '{{' at offset {pos}")
    depth = 0
    for m in _TOKEN.finditer(masked, pos):
        t = m.group(0)
        if t == "{":
            depth += 1
        elif t == "}":
            depth -= 1
            if depth == 0:
                return m.start()
    raise RedlineError(f"unbalanced braces: group opened at offset {pos} is never closed")


def _skip_arg_space(masked: str, pos: int) -> int:
    """TeX skips spaces and at most one newline between macro arguments."""
    m = re.compile(r"[ \t]*(?:\n[ \t]*)?").match(masked, pos)
    return m.end() if m else pos


def _match_bracket(masked: str, pos: int) -> int:
    depth = 0
    for i in range(pos, len(masked)):
        c = masked[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "]" and depth == 0:
            return i
    raise RedlineError(f"unclosed '[' at offset {pos}")


def strip_tex(s: str) -> str:
    """Rough plain-text rendering of a heading for display."""
    s = re.sub(r"\\(?:label|footnote)\{[^{}]*\}", "", s)
    s = re.sub(r"\\[A-Za-z@]+\*?", "", s)
    s = s.replace("{", "").replace("}", "").replace("~", " ")
    return re.sub(r"\s+", " ", s).strip()


def parse(src: str, legacy: dict[str, str] | None = None) -> Document:
    legacy = DEFAULT_LEGACY if legacy is None else legacy
    macros = dict(CHANGES_MACROS)
    for name, kind in legacy.items():
        macros.setdefault(name, (kind, 2 if kind == "replaced" else 1))

    masked, _verb, comments = mask(src)
    doc = Document(src=src, line_starts=_line_starts(src))

    bd = re.search(r"\\begin\{document\}", masked)
    doc.body_start = bd.end() if bd else 0
    ed = re.search(r"\\end\{document\}", masked)
    doc.body_end = ed.start() if ed else len(src)

    # ---- markers --------------------------------------------------------
    for (c, le) in comments:
        if c < doc.body_start:
            continue
        m = _MARKER.match(src, c, le)
        if m:
            doc.markers.append(Marker(kind=m.group(1), note=m.group(2).strip(),
                                      start=c, end=le, line=doc.line_of(c)))

    # ---- sections & floats ----------------------------------------------
    _parse_sections(doc, masked)
    for m in re.finditer(r"\\begin\{(" + "|".join(re.escape(e) for e in FLOAT_ENVS) + r")\}", masked):
        e = masked.find("\\end{" + m.group(1) + "}", m.end())
        if e >= 0:
            doc.floats.append(Span(m.start(), e + len("\\end{" + m.group(1) + "}")))

    # ---- changes ----------------------------------------------------------
    found: list[Change] = []
    for m in _TOKEN.finditer(masked, doc.body_start, doc.body_end):
        name = m.group(1)
        if not name or name not in macros:
            continue
        kind, nargs = macros[name]
        try:
            ch = _read_macro(masked, m.start(), m.end(), name, kind, nargs)
        except RedlineError as e:
            doc.problems.append(f"line {doc.line_of(m.start())}: \\{name}: {e}")
            continue
        if ch is None:
            continue
        ch.legacy = name not in CHANGES_MACROS
        found.append(ch)

    # nesting: anything inside an earlier top-level span is nested
    top: list[Change] = []
    for ch in found:  # found is ordered by start
        if top and ch.start < top[-1].end:
            ch.nested_in = top[-1]
            doc.nested.append(ch)
        else:
            top.append(ch)

    # attach comments that directly follow a change
    items: list[Change] = []
    for ch in top:
        if (ch.kind == "comment" and items and items[-1].kind != "comment"
                and items[-1].comment is None):
            prev = items[-1]
            g = _ATTACH_GAP.match(masked, prev.end)
            if g and g.end() == ch.start and not _BLANK_LINE.search(src, prev.end, ch.start):
                prev.comment = ch
                ch.attached_to = prev
                _finish(doc, ch)
                continue
        items.append(ch)

    seen: dict[str, int] = {}
    for i, ch in enumerate(items, 1):
        _finish(doc, ch)
        ch.ordinal = i
        h = hashlib.sha1("\0".join([ch.kind, ch.new(src) or "", ch.old(src) or "",
                                     ch.body(src) or ""]).encode()).hexdigest()[:6]
        seen[h] = seen.get(h, 0) + 1
        ch.id = h if seen[h] == 1 else f"{h}-{seen[h]}"
    doc.items = items
    for ch in doc.nested:
        _finish(doc, ch)

    _bind_markers(doc)
    return doc


def _finish(doc: Document, ch: Change) -> None:
    ch.line_start = doc.line_of(ch.start)
    ch.line_end = doc.line_of(max(ch.start, ch.full_end - 1))
    ch.section = doc.section_at(ch.start)


def _read_macro(masked: str, start: int, pos: int, name: str, kind: str, nargs: int) -> Change | None:
    opt = None
    p = _skip_arg_space(masked, pos)
    if p < len(masked) and masked[p] == "[":
        e = _match_bracket(masked, p + 1)
        opt = Span(p + 1, e)
        p = _skip_arg_space(masked, e + 1)
    args: list[Span] = []
    for k in range(nargs):
        if p >= len(masked) or masked[p] != "{":
            if k == 0:
                return None  # e.g. "\note" used as a word, or a definition; not markup
            raise RedlineError(f"expected {nargs} arguments, found {k}")
        e = match_group(masked, p)
        args.append(Span(p + 1, e))
        p = e + 1
        if k + 1 < nargs:
            p = _skip_arg_space(masked, p)
    return Change(kind=kind, macro=name, start=start, end=p, args=args, opt=opt)


def _parse_sections(doc: Document, masked: str) -> None:
    pat = re.compile(r"\\(section|subsection|subsubsection)(\*?)\s*(?:\[[^\]]*\])?\s*\{"
                     r"|\\appendix\b|\\begin\{abstract\}|\\end\{abstract\}")
    counters = [0, 0, 0]
    appendix = False
    stack: list[Section] = []
    for m in pat.finditer(masked, doc.body_start, doc.body_end):
        tok = m.group(0)
        if tok.startswith("\\appendix"):
            appendix = True
            counters = [0, 0, 0]
            continue
        if tok == "\\begin{abstract}":
            sec = Section(level=1, title="Abstract", number="", start=m.start())
            doc.sections.append(sec)
            stack = [sec]
            continue
        if tok == "\\end{abstract}":
            continue
        level = SECTION_LEVELS[m.group(1)]
        try:
            e = match_group(masked, m.end() - 1)
        except RedlineError:
            continue
        title = strip_tex(doc.src[m.end():e])
        number = ""
        if not m.group(2):
            counters[level - 1] += 1
            for i in range(level, 3):
                counters[i] = 0
            first = chr(ord("A") + counters[0] - 1) if appendix else str(counters[0])
            number = ".".join([first] + [str(c) for c in counters[1:level]])
        while stack and stack[-1].level >= level:
            stack.pop()
        sec = Section(level=level, title=title, number=number, start=m.start(),
                      parent=stack[-1] if stack else None)
        doc.sections.append(sec)
        stack.append(sec)
    # section ends: next heading of the same or a higher level
    for i, s in enumerate(doc.sections):
        s.line = doc.line_of(s.start)
        s.end = doc.body_end
        for t in doc.sections[i + 1:]:
            if t.level <= s.level or s.title == "Abstract":
                s.end = t.start
                break


def _bind_markers(doc: Document) -> None:
    items = doc.items
    for mk in doc.markers:
        target = None
        for it in items:
            if it.start <= mk.start < it.full_end:
                target = it
                break
        if target is None:
            prev = [it for it in items if it.full_end <= mk.start]
            if prev and not _BLANK_LINE.search(doc.src, prev[-1].full_end, mk.start):
                target = prev[-1]
        if target is not None:
            if target.marker is not None:
                doc.problems.append(f"line {mk.line}: second marker for {target.label}; "
                                    f"the later one (%{mk.kind}) wins")
            target.marker = mk
            mk.item = target
