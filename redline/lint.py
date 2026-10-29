"""Checks for markup that breaks the build, with automatic fixes where safe.

Every rule here was reproduced against the Pop III preamble
(markup=default, commentmarkup=footnote, ulem strike-out):

  cite-in-strikeout   \\citep/\\citet/\\autoref inside \\deleted{} or the old
                      argument of \\replaced{} -> "Extra }"   (fix: \\mbox{})
  par-in-change       a blank line inside any changes macro  (fix: split)
  env-in-strikeout    \\begin{..} or \\[ inside struck-out text  (no fix)
  footnote-in-strikeout \\footnote inside struck-out text      (no fix)
  label-after-markup  \\label after markup in a \\caption makes \\autoref print
                      "section"                        (fix: move \\label first)
  comment-in-float    \\chcomment footnotes inside figures are lost
                      (fix: move after \\end{figure})
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import Change, Document, Span, TextEdit
from .ops import apply, tidy_lines
from .parse import mask, match_group, parse

STRIKE_CMDS = r"(?:cite[a-zA-Z]*|[aA]utoref|[cC]ref|pageref)"
_STRIKE_CMD = re.compile(r"\\" + STRIKE_CMDS + r"\*?")
_PAR = re.compile(r"\n[ \t]*\n")
_PAR_SPLIT = re.compile(r"(\n[ \t]*\n\s*)")


@dataclass
class Issue:
    code: str
    severity: str          # error | warning
    line: int
    message: str
    item: str = ""         # change label, e.g. C12
    fix: list[TextEdit] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "line": self.line,
                "item": self.item, "message": self.message, "fixable": bool(self.fix)}


def _strike_args(ch: Change) -> list[Span]:
    if ch.kind == "deleted":
        return [ch.args[0]]
    if ch.kind == "replaced":
        return [ch.args[1]]
    return []


def _mbox_spans(masked: str, arg: Span) -> list[tuple[int, int]]:
    spans = []
    for m in re.finditer(r"\\mbox\s*\{", masked[:arg.end]):
        if m.start() < arg.start:
            continue
        try:
            e = match_group(masked, m.end() - 1)
        except Exception:
            continue
        spans.append((m.start(), e + 1))
    return spans


def cmd_end(masked: str, pos: int) -> int | None:
    """End of a \\cmd[..][..]{..} invocation starting at pos (after the name)."""
    p = pos
    while p < len(masked) and masked[p] in " \t":
        p += 1
    while p < len(masked) and masked[p] == "[":
        e = masked.find("]", p)
        if e < 0:
            return None
        p = e + 1
    if p >= len(masked) or masked[p] != "{":
        return None
    try:
        return match_group(masked, p) + 1
    except Exception:
        return None


def mbox_fixes(src: str, masked: str, arg: Span) -> list[TextEdit]:
    boxed = _mbox_spans(masked, arg)
    edits = []
    for m in _STRIKE_CMD.finditer(masked, arg.start, arg.end):
        if any(a <= m.start() < b for a, b in boxed):
            continue
        e = cmd_end(masked, m.end())
        if e is None or e > arg.end:
            continue
        edits.append(TextEdit(m.start(), m.start(), "\\mbox{"))
        edits.append(TextEdit(e, e, "}"))
    return edits


def split_paragraphs(text: str) -> list[str]:
    """['para', 'sep', 'para', ...] (odd indexes are the blank-line separators)."""
    return _PAR_SPLIT.split(text)


def balanced(text: str) -> bool:
    masked, _, _ = mask(text)
    depth = 0
    for m in re.finditer(r"\\.|[{}]", masked, re.S):
        t = m.group(0)
        if t == "{":
            depth += 1
        elif t == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def wrap(macro: str, *args: str) -> str:
    return "\\" + macro + "".join("{" + a + "}" for a in args)


def split_change(src: str, ch: Change) -> str | None:
    """Rewrite a change that spans paragraphs as one change per paragraph."""
    if ch.kind in ("added", "deleted", "highlight"):
        parts = split_paragraphs(ch.args[0].text(src))
        if not all(balanced(p) for p in parts[::2]):
            return None
        out = []
        for i, p in enumerate(parts):
            out.append(p if i % 2 or not p.strip() else wrap(ch.macro, p))
        return "".join(out)
    if ch.kind == "replaced":
        new = split_paragraphs(ch.args[0].text(src))
        old = split_paragraphs(ch.args[1].text(src))
        np_, op_ = new[::2], old[::2]
        if not all(balanced(p) for p in np_ + op_):
            return None
        out = []
        for i in range(max(len(np_), len(op_))):
            n = np_[i] if i < len(np_) else ""
            o = op_[i] if i < len(op_) else ""
            if i:
                seps = new[1::2] if i - 1 < len(new[1::2]) else old[1::2]
                out.append(seps[i - 1] if i - 1 < len(seps) else "\n\n")
            if n.strip() and o.strip():
                out.append(wrap(ch.macro, n, o))
            elif n.strip():
                out.append(wrap("added", n))
            elif o.strip():
                out.append(wrap("deleted", o))
        return "".join(out)
    return None


def lint(doc: Document) -> list[Issue]:
    src = doc.src
    masked, _, _ = mask(src)
    issues: list[Issue] = []

    for p in doc.problems:
        ln = int(m.group(1)) if (m := re.match(r"line (\d+)", p)) else 0
        sev = "error" if "unbalanced" in p or "expected" in p else "warning"
        issues.append(Issue("parse", sev, ln, p))

    for m in re.finditer(r"^(<{7}|>{7}|={7})( |$)", src, re.M):
        issues.append(Issue("merge-conflict", "error", doc.line_of(m.start()),
                            "unresolved merge conflict marker"))

    for ch in doc.items + doc.nested:
        tag = ch.label if ch.ordinal else ""
        for arg in _strike_args(ch):
            fixes = mbox_fixes(src, masked, arg)
            if fixes:
                issues.append(Issue("cite-in-strikeout", "error", ch.line_start,
                                    "\\cite/\\autoref in struck-out text must be wrapped in \\mbox{}",
                                    tag, fixes))
            body = masked[arg.start:arg.end]
            if re.search(r"\\begin\{|\\\[|\$\$", body):
                issues.append(Issue("env-in-strikeout", "error", ch.line_start,
                                    "environments/display math cannot be struck out; edit them plainly "
                                    "and explain with \\chcomment", tag))
            if "\\footnote" in body:
                issues.append(Issue("footnote-in-strikeout", "error", ch.line_start,
                                    "\\footnote cannot be struck out; move it out of the deletion", tag))
        if any(_PAR.search(src, a.start, a.end) for a in ch.args):
            new = split_change(src, ch) if ch.kind != "comment" else None
            fix = [TextEdit(ch.start, ch.end, new)] if new is not None else []
            issues.append(Issue("par-in-change", "error", ch.line_start,
                                "a changes macro cannot contain a blank line"
                                + ("; split it per paragraph" if fix else ""), tag, fix))
        if ch.nested_in is not None:
            issues.append(Issue("nested-markup", "warning", ch.line_start,
                                f"\\{ch.macro} inside {ch.nested_in.label}; accepting the outer "
                                "change keeps the inner markup", ch.nested_in.label))
        if ch.kind == "comment" and re.match(r"\s*\\cite", ch.args[0].text(src)):
            a = ch.args[0].start
            issues.append(Issue("comment-starts-with-cite", "warning", ch.line_start,
                                "a \\chcomment starting with \\citet has aborted the build "
                                "(Incomplete \\iffalse) in some contexts", tag,
                                [TextEdit(a, a, "Note: ")]))

    # comments inside floats: the footnote text is lost
    for fl in doc.floats:
        for ch in doc.items + [i.comment for i in doc.items if i.comment]:
            if ch is None or ch.kind != "comment" or not (fl.start <= ch.start < fl.end):
                continue
            tag = ch.label if ch.ordinal else (ch.attached_to.label if ch.attached_to else "")
            text = src[ch.start:ch.end]
            fix = tidy_lines(src, [TextEdit(ch.start, ch.end, "")]) + [TextEdit(fl.end, fl.end, text)]
            issues.append(Issue("comment-in-float", "warning", ch.line_start,
                                "\\chcomment inside a float is lost; moving it after the float",
                                tag, fix))

    # \label after markup inside a caption
    for m in re.finditer(r"\\caption\s*(?:\[[^\]]*\])?\s*\{", masked):
        try:
            e = match_group(masked, m.end() - 1)
        except Exception:
            continue
        body_start = m.end()
        has_markup = any(body_start <= ch.start < e for ch in doc.items)
        lab = re.search(r"\\label\{[^{}]*\}", masked[body_start:e])
        if not has_markup or not lab:
            continue
        ls, le = body_start + lab.start(), body_start + lab.end()
        first_markup = min(ch.start for ch in doc.items if body_start <= ch.start < e)
        if ls < first_markup:
            continue
        if any(ch.start < ls < ch.end for ch in doc.items):
            issues.append(Issue("label-after-markup", "warning", doc.line_of(ls),
                                "\\label inside a change in a caption; move it out and to the front"))
            continue
        label = src[ls:le]
        issues.append(Issue("label-after-markup", "warning", doc.line_of(ls),
                            "\\label after changes markup in a caption makes \\autoref print "
                            "'section'; moving it first", "",
                            [TextEdit(body_start, body_start, label), TextEdit(ls, le, "")]))

    for mk in doc.markers:
        if mk.item is None:
            issues.append(Issue("orphan-marker", "warning", mk.line,
                                f"%{mk.kind} marker is not attached to any change"))
    issues.sort(key=lambda i: (i.line, i.code))
    return issues


def fix_all(src: str, legacy=None, rounds: int = 5) -> tuple[str, list[Issue]]:
    """Apply every available fix (re-linting between rounds). Returns (text, fixed issues)."""
    fixed: list[Issue] = []
    for _ in range(rounds):
        doc = parse(src, legacy)
        edits: list[TextEdit] = []
        taken: list[tuple[int, int]] = []
        for iss in lint(doc):
            if not iss.fix:
                continue
            lo = min(e.start for e in iss.fix)
            hi = max(e.end for e in iss.fix)
            if any(not (hi < a or lo > b) for a, b in taken):
                continue  # overlaps another fix this round; next round
            taken.append((lo, hi))
            edits.extend(iss.fix)
            fixed.append(iss)
        if not edits:
            break
        src = apply(src, edits)
    return src, fixed
