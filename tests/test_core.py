import re
from pathlib import Path

import pytest

from redline import author, lint, ops, select
from redline.model import RedlineError
from redline.parse import parse

FIX = Path(__file__).parent / "fixtures" / "mini.tex"
REAL = Path.home() / "Software/Overleaf-nix-flake/Pop III m12f/oja_template.tex"


@pytest.fixture
def src():
    return FIX.read_text()


def kinds(doc):
    return [it.kind for it in doc.items]


def test_parse_items(src):
    doc = parse(src)
    assert kinds(doc) == ["added", "replaced", "deleted", "added", "deleted", "comment",
                          "added", "added", "replaced"]
    rep = doc.items[1]
    assert rep.new(src) == "accretes"
    assert rep.comment is not None and rep.comment.body(src) == "Shorter wording."
    assert doc.items[4].comment.body(src) == "TODO: check later"
    assert doc.items[6].legacy and doc.items[6].macro == "skadd"
    assert not any("not markup" in (it.new(src) or "") for it in doc.items)  # verbatim skipped


def test_sections(src):
    doc = parse(src)
    labels = [s.label for s in doc.sections]
    assert labels == ["Abstract", "1 Introduction", "1.1 Details", "2 Simulation and Methods"]
    assert doc.items[0].section.title == "Abstract"
    assert doc.items[3].section.label == "1.1 Details"


def test_markers(src):
    doc = parse(src)
    n = doc.items[3]
    assert n.marker and n.marker.kind == "N" and n.marker.note == "why this clause?"
    x = doc.items[7]
    assert x.marker and x.marker.kind == "X"


def test_accept_all_removes_only_markup(src):
    doc = parse(src)
    items = select.select(doc, {"all": True})
    edits = ops.resolve(doc, ops.decide(doc, items, "accept", honor_markers=False), comments="drop")
    out = ops.apply(src, edits)
    body = out.split("\\begin{document}")[1]
    for m in ("\\added{", "\\deleted{", "\\replaced{", "\\chcomment{", "\\skadd{"):
        assert m not in body.replace("\\added{not markup}", "")
    assert "The sink accretes at a high rate." in out
    assert "This sentence goes away" not in out
    assert "Here an inserted clause, the cooling rate rises." in out
    assert "%N" not in out and "%X" not in out
    assert "\n\n\n" not in out.replace("\\end{verbatim}\n\n", "")  # no new paragraph breaks
    # the deleted line vanished entirely rather than leaving a blank line
    assert "at a high rate.\nText with" in out


def test_reject_all_restores(src):
    doc = parse(src)
    edits = ops.resolve(doc, ops.decide(doc, select.select(doc, {"all": True}), "reject",
                                        honor_markers=False), comments="drop")
    out = ops.apply(src, edits)
    assert "The sink grows at a high rate." in out
    assert "And old \\citep{abel02} claim done." in out  # \mbox protection removed
    assert re.search(r"We study\s+discs\.", out)
    assert "Old method is used." in out


def test_markers_honored(src):
    doc = parse(src)
    items = select.select(doc, {"all": True})
    dec = ops.decide(doc, items, "accept")
    acts = {d.item.label: d.action for d in dec}
    assert acts["C4"] == "skip" and acts["C8"] == "reject"
    out = ops.apply(src, ops.resolve(doc, dec))
    assert "We use GIZMO ." in out or "We use GIZMO .\n" in out or "We use GIZMO" in out
    assert "with MFM" not in out
    assert "\\added{an inserted clause, }" in out and "%N why this clause?" in out
    # TODO comment kept under the default policy, others dropped
    assert "\\chcomment{TODO: check later}" in out
    assert "Shorter wording" not in out


def test_apply_markers_only(src):
    doc = parse(src)
    dec = ops.decide(doc, select.select(doc, {"all": True}), "markers")
    out = ops.apply(src, ops.resolve(doc, dec))
    assert "\\replaced{accretes}{grows}" in out  # unmarked: untouched
    assert "with MFM" not in out


def test_selectors(src):
    doc = parse(src)
    assert [i.label for i in select.select(doc, {"until_section": "simulation and methods"})] == \
        [f"C{i}" for i in range(1, 8)]
    assert [i.label for i in select.select(doc, {"section": "Details"})] == ["C4", "C5", "C6", "C7"]
    assert [i.label for i in select.select(doc, {"section": "1"})][:1] == ["C2"]
    assert [i.label for i in select.select(doc, {"marker": "N"})] == ["C4"]
    assert [i.label for i in select.select(doc, {"contains": "MFM"})] == ["C8"]
    got = select.select(doc, {"section": "Introduction", "except_ids": ["C2"]})
    assert "C2" not in [i.label for i in got]
    with pytest.raises(RedlineError):
        select.select(doc, {})
    with pytest.raises(RedlineError):
        select.select(doc, {"section": "nonexistent heading"})


def test_ids_stable_across_unrelated_edits(src):
    a = parse(src)
    b = parse(src.replace("Text with a 100", "Some text with a 100"))
    assert [i.id for i in a.items] == [i.id for i in b.items]


def test_set_marker_roundtrip(src):
    doc = parse(src)
    it = doc.items[1]  # replaced + attached comment, at end of line
    out = ops.apply(src, ops.set_marker(doc, it, "N", "too terse"))
    d2 = parse(out)
    assert d2.items[1].marker.kind == "N" and d2.items[1].marker.note == "too terse"
    back = ops.apply(out, ops.set_marker(d2, d2.items[1], None))
    assert back == src


def test_set_marker_midline(src):
    doc = parse(src)
    it = doc.items[0]  # "We study \added{magnetised} discs."
    out = ops.apply(src, ops.set_marker(doc, it, "Y"))
    d2 = parse(out)
    assert d2.items[0].marker.kind == "Y"
    assert ops.apply(out, ops.set_marker(d2, d2.items[0], None)) == src


def test_lint_and_fix():
    bad = (FIX.read_text().replace("This sentence goes away.", "Gone \\citep{abel02} and \\autoref{sec:intro}.")
           .replace("\\skadd{legacy addition}", "\\added{para one.\n\npara two.}"))
    doc = parse(bad)
    codes = {i.code for i in lint.lint(doc)}
    assert {"cite-in-strikeout", "par-in-change"} <= codes
    fixed, applied = lint.fix_all(bad)
    assert "\\deleted{Gone \\mbox{\\citep{abel02}} and \\mbox{\\autoref{sec:intro}}.}" in fixed
    assert "\\added{para one.}\n\n\\added{para two.}" in fixed
    assert not [i for i in lint.lint(parse(fixed)) if i.severity == "error"]


def test_lint_caption_label():
    s = ("\\begin{document}\n\\begin{figure}\n\\caption{Cap \\added{new} text.\\label{fig:a}}\n"
         "\\end{figure}\n\\end{document}\n")
    fixed, applied = lint.fix_all(s)
    assert "\\caption{\\label{fig:a}Cap \\added{new} text.}" in fixed


def test_lint_comment_in_float():
    s = ("\\begin{document}\n\\begin{figure}\n\\caption{\\label{f}Cap.}\\chcomment{note}\n"
         "\\end{figure}\nNext.\n\\end{document}\n")
    fixed, _ = lint.fix_all(s)
    assert "\\end{figure}\\chcomment{note}" in fixed
    assert "Cap.}\n\\end{figure}" in fixed


def test_author_replace_protects_citations(src):
    doc = parse(src)
    edits = author.propose_replace(doc, "form in minihaloes \\citep{abel02}", "form in haloes",
                                   comment="\\citet{abel02} is old")
    out = ops.apply(src, edits)
    assert ("\\replaced{form in haloes}{form in minihaloes \\mbox{\\citep{abel02}}}"
            "\\chcomment{Note: \\citet{abel02} is old}") in out
    assert parse(out).items[1].comment is not None


def test_author_paragraph_split(src):
    doc = parse(src)
    out = ops.apply(src, author.propose_insert(doc, " New one.\n\nNew two.", after="more text."))
    assert " \\added{New one.}\n\n\\added{New two.}" in out
    assert not [i for i in lint.lint(parse(out)) if i.severity == "error"]


def test_author_refuses_bad_anchors(src):
    doc = parse(src)
    with pytest.raises(RedlineError, match="inside pending change"):
        author.propose_replace(doc, "accretes", "x")
    with pytest.raises(RedlineError, match="not found"):
        author.propose_delete(doc, "no such text")
    with pytest.raises(RedlineError, match="occurs"):
        author.propose_delete(doc, "rate")
    with pytest.raises(RedlineError, match="% comment"):
        author.propose_delete(doc, "a comment with")
    with pytest.raises(RedlineError, match="environment"):
        s2 = src.replace("more text.", "more text.\n\\begin{equation}x\\end{equation}")
        author.propose_delete(parse(s2), "\\begin{equation}x\\end{equation}")


def test_author_whitespace_tolerant(src):
    doc = parse(src)
    edits = author.propose_delete(doc, "Pop III stars   form\nin minihaloes")
    assert "\\deleted{Pop III stars form in minihaloes}" in ops.apply(src, edits)


def test_revise(src):
    doc = parse(src)
    out = ops.apply(src, author.revise_change(doc, doc.items[2], "Replacement sentence.",
                                              comment="Keeps the point."))
    assert "\\replaced{Replacement sentence.}{This sentence goes away.}\\chcomment{Keeps the point.}" in out


@pytest.mark.skipif(not REAL.exists(), reason="real paper not available")
def test_real_paper_counts():
    s = REAL.read_text()
    doc = parse(s)
    body = s[doc.body_start:]
    for macro, kind in (("added", "added"), ("replaced", "replaced"), ("deleted", "deleted")):
        uncommented = len(re.findall(r"(?m)^[^%\n]*?\\" + macro + r"\{", body))
        assert sum(1 for i in doc.items + doc.nested if i.macro == macro) >= uncommented - 2
    comments = sum(1 for i in doc.items if i.kind == "comment" and i.macro == "chcomment") \
        + sum(1 for i in doc.items if i.comment and i.comment.macro == "chcomment")
    assert comments == len(re.findall(r"\\chcomment\{", body))
    assert not doc.problems
    # accept-all then reject-all only touch markup: the unmarked text survives
    for action in ("accept", "reject"):
        out = ops.apply(s, ops.resolve(doc, ops.decide(doc, doc.items, action, honor_markers=False),
                                       comments="drop"))
        d2 = parse(out)
        assert not [i for i in d2.items if not i.legacy]  # coauthors' \\skcomment etc. may surface
        assert "\\section{Simulation and Methods}" in out


def test_insert_does_not_capture_existing_comment(src):
    doc = parse(src)
    out = ops.apply(src, author.propose_insert(doc, " extra", after="And \\deleted{old \\mbox{\\citep{abel02}} claim}\\chcomment{TODO: check later} done."))
    d2 = parse(out)
    ym = [i for i in d2.items if i.kind == "comment" and "YM" in (i.body(out) or "")]
    assert ym, "standalone YM comment must stay standalone"
    new = [i for i in d2.items if i.kind == "added" and i.new(out) == "extra"][0]
    assert new.comment is None
