"""Restructuring (splitting list items, moving text) must stay tracked."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from redline import author, ops
from redline.model import RedlineError
from redline.parse import parse

PRE = Path(__file__).parent / "fixtures" / "mini.tex"

LIST = r"""
\section{Conclusions}
\begin{enumerate}
    \item \textbf{Cosmological ICs set the boundary conditions.}
          The environment sets the mass supply and its angular momentum,
          with a broad spread. The fragmentation sites move outward: the first
          burst forms at 500 AU. The processes inside the disk are similar to
          those in idealised calculations. Fragmentation ends when the disk is
          depleted. The host is an atomic-cooling halo.
\end{enumerate}
"""


def same_tex(a: str, b: str) -> bool:
    """Equal up to whitespace inside paragraphs (TeX typesets them identically)."""
    paras = lambda t: [" ".join(p.split()) for p in re.split(r"\n[ \t]*\n", t) if p.strip()]
    return paras(a) == paras(b)


def paper() -> str:
    pre = PRE.read_text()
    return pre.replace(r"\end{document}", LIST + "\n\\end{document}")


def apply_all(src: str, action: str, only_new_vs: str | None = None) -> str:
    """Accept/reject every change (or only those not present in only_new_vs)."""
    doc = parse(src)
    old_ids = {i.id for i in parse(only_new_vs).items} if only_new_vs else set()
    items = [i for i in doc.items if i.id not in old_ids]
    return ops.apply(src, ops.resolve(doc, ops.decide(doc, items, action, honor_markers=False),
                                      comments="drop"))


def split_point_six(src: str) -> str:
    steps = [
        lambda d: author.propose_insert(d, "\n\n    \\item \\textbf{Fragmentation moves outward.}\n          ",
                                        after="with a broad spread."),
        lambda d: author.propose_move(d, "The processes inside the disk are similar to\n"
                                         "          those in idealised calculations.",
                                      after="The host is an atomic-cooling halo.",
                                      comment="split point 6"),
        lambda d: author.propose_insert(d, "\n    \\item \\textbf{Limitations.} ", before="The host is an"),
    ]
    for step in steps:
        src = ops.apply(src, step(parse(src)))
    return src


def test_new_item_keeps_item_outside_markup():
    out = ops.apply(paper(), author.propose_insert(parse(paper()), "\n    \\item \\textbf{New point.} Text.",
                                                   after="atomic-cooling halo."))
    assert "\\item \\added{\\textbf{New point.} Text.}" in out
    assert "\\added{\\item" not in out


def test_split_is_fully_tracked_and_reversible():
    src = paper()
    out = split_point_six(src)
    assert "\\added{\\item" not in out and "\\deleted{\\item" not in out
    assert out.count("\\item \\added{") == 2
    assert "Moved to" in out and "Moved here from" in out
    # every new or moved word is inside markup: rejecting everything restores the original
    assert same_tex(apply_all(out, "reject", only_new_vs=src), src)
    accepted = apply_all(out, "accept", only_new_vs=src)
    assert accepted.count("\\item") == 3
    assert "\\item \\textbf{Fragmentation moves outward.}" in accepted
    assert "atomic-cooling halo. The processes inside the disk" in " ".join(accepted.split())


def test_rejecting_a_new_item_removes_its_bullet():
    src = paper()
    out = ops.apply(src, author.propose_insert(parse(src), "\n    \\item \\textbf{Extra.} Words.",
                                               after="atomic-cooling halo."))
    assert apply_all(out, "reject", only_new_vs=src).count("\\item") == 1


def test_accepting_a_deleted_item_removes_its_bullet():
    src = paper().replace(r"\end{enumerate}", "    \\item Second point to drop.\n\\end{enumerate}")
    out = ops.apply(src, author.propose_delete(parse(src), "Second point to drop."))
    assert "\\item \\deleted{Second point to drop.}" in out
    assert apply_all(out, "accept", only_new_vs=src).count("\\item") == 1


def test_headings_mark_their_title():
    src = paper()
    out = ops.apply(src, author.propose_insert(parse(src), "\n\\subsection{New part}\nText here.",
                                               after="atomic-cooling halo."))
    assert "\\subsection{\\added{New part}}" in out
    back = apply_all(out, "reject", only_new_vs=src)
    assert "\\section{Conclusions}" in back and "New part" not in back


def test_plain_edit_refuses_prose():
    d = parse(paper())
    with pytest.raises(RedlineError, match="Track it"):
        author.plain_edit(d, "The host is an atomic-cooling halo.",
                          "The host is a massive atomic-cooling halo in a dense filament.")
    # a tiny typo fix is fine, and a forced edit leaves a visible comment
    author.plain_edit(d, "atomic-cooling halo", "atomic cooling halo")
    out = ops.apply(paper(), author.plain_edit(d, "The host is an atomic-cooling halo.",
                                               "The host is a massive halo in a dense filament.",
                                               reason="author dictated", force=True))
    assert "\\chcomment{Untracked edit: author dictated}" in out


@pytest.mark.latex
@pytest.mark.skipif(not shutil.which("pdflatex"), reason="no pdflatex")
def test_split_compiles(tmp_path):
    tex = tmp_path / "p.tex"
    tex.write_text(split_point_six(paper()))
    r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", tex.name],
                       cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-1500:]
