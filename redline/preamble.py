"""Packages a paper needs for Redline's markup (`\\added`, `\\deleted`, `\\replaced`, `\\chcomment`).

Setting a paper up adds any that are missing to the main file's preamble, so the
first tracked edit compiles. A paper that already loads `changes` (with any
options) is left alone.
"""
from __future__ import annotations

import re
from pathlib import Path

# package -> \usepackage line
REQUIRED = {
    "changes": r"\usepackage[markup=default,commentmarkup=footnote,authormarkup=none,commandnameprefix=ifneeded]{changes}",
}
# Redline writes \\chcomment; `changes` only defines it when \\comment is taken by another package
# (commandnameprefix=ifneeded), so alias it otherwise.
GUARDS = {"chcomment": r"\providecommand{\chcomment}{\comment}"}
_USE = re.compile(r"^[ \t]*\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", re.M)


def loaded(preamble: str) -> set[str]:
    """Package names the preamble loads (commented-out lines don't count)."""
    code = "\n".join(re.sub(r"(?<!\\)%.*", "", ln) for ln in preamble.splitlines())
    return {p.strip() for m in _USE.finditer(code) for p in m.group(1).split(",")}


def _code(pre: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*", "", ln) for ln in pre.splitlines())


def ensure(main: Path) -> list[str]:
    """Add what's missing to the preamble: packages after the last package line, then the
    command guards right after the `changes` line. Returns what was added."""
    text = main.read_text()
    begin = re.search(r"^[ \t]*\\begin\{document\}", text, re.M)
    if not begin:
        return []
    pre = text[:begin.start()]
    added = []
    missing = [p for p in REQUIRED if p not in loaded(pre)]
    if missing:
        last = None
        for m in _USE.finditer(re.sub(r"(?m)^[ \t]*%.*$", lambda x: " " * len(x.group(0)), pre)):
            last = m
        if last:  # after the whole line holding the last package
            at = pre.find("\n", last.end())
            at = len(pre) if at < 0 else at + 1
        else:
            cls = re.search(r"\\documentclass(?:\[[^\]]*\])?\{[^}]*\}[^\n]*\n", pre)
            at = cls.end() if cls else 0
        add = "".join(REQUIRED[p] + "\n" for p in missing)
        text = text[:at] + add + text[at:]
        added += missing
        pre = text[:text.index("\\begin{document}", at)]
    for name, line in GUARDS.items():
        if "\\" + name not in _code(pre):
            m = re.search(r"^[^\n%]*\\usepackage[^\n]*\{[^}\n]*\bchanges\b[^}\n]*\}[^\n]*\n", text, re.M)
            if m:
                text = text[:m.end()] + line + "\n" + text[m.end():]
                added.append(name)
                pre = text[:text.index("\\begin{document}")]
    if added:
        main.write_text(text)
    return added
