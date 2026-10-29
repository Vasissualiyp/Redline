import shutil
from pathlib import Path

import pytest

from redline import compile as C
from redline.service import Service
from redline.state import Project

FIX = Path(__file__).parent / "fixtures" / "mini.tex"


@pytest.fixture
def svc(tmp_path, monkeypatch):
    shutil.copy(FIX, tmp_path / "paper.tex")
    monkeypatch.setenv("REDLINE_ROOT", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))  # no nvim socket
    return Service(Project.open())


def test_propose_records_origin_and_lists(svc):
    out = svc.propose_replace("high rate", "rate of $10^{-3}$", comment="Quantify.", origin="claude-code")
    assert "created" in out and "[claude-code]" in out
    listing = svc.list_changes({"origin": "claude-code"})
    assert "rate of $10^{-3}$" in listing and listing.count("\n") == 0


def test_undo_keeps_later_unrelated_edit(svc):
    proj = svc.proj
    svc.propose_delete("Pop III stars form in minihaloes")
    # a later edit by someone else, elsewhere in the file
    text = proj.read().replace("More text", "MORE TEXT").replace("more text.", "MORE text.")
    proj.main.write_text(text)
    svc.undo()
    final = proj.read()
    assert "\\deleted{Pop III" not in final
    assert "MORE text." in final


def test_bulk_accept_until_section(svc):
    rep = svc.resolve("accept", {"until_section": "Simulation and Methods"})
    assert "skip (%N: kept pending)" in rep
    text = svc.proj.read()
    assert "\\added{an inserted clause, }" in text      # %N kept
    assert "\\added{with MFM}" in text                  # after the section: untouched
    assert "The sink accretes" in text
    assert "\\chcomment{TODO: check later}" in text     # TODO comments survive


def test_explicit_id_drops_standalone_comment(svc):
    doc = svc.proj.doc()
    ym = [i for i in doc.items if i.kind == "comment"][0]
    svc.resolve("accept", {"ids": [ym.id]})
    assert "YM: standalone" not in svc.proj.read()


def test_review_notes(svc):
    notes = svc.review_notes()
    assert "%N \"why this clause?\"" in notes and "%X" in notes


def test_read_and_outline(svc):
    assert "1.1 Details" in svc.outline()
    assert "Here \\added" in svc.read(section="Details")


@pytest.mark.latex
@pytest.mark.skipif(not shutil.which("latexmk"), reason="no latexmk")
def test_compile_maps_error_to_change(svc):
    res = C.run(svc.proj)
    assert res.ok, C.format_report(res)
    # break it inside a change: a citation in struck-out text
    t = svc.proj.read().replace("\\deleted{This sentence goes away.}",
                                "\\deleted{This sentence \\citep{abel02} goes away.}")
    t = t.replace("\\end{document}", "\\begin{thebibliography}{1}\\bibitem[Abel(2002)]{abel02} A.\\end{thebibliography}\n\\end{document}")
    svc.proj.main.write_text(t)
    res = C.run(svc.proj)
    res2 = C.run(svc.proj)  # second pass: citation defined -> ulem breaks
    bad = res if not res.ok else res2
    assert not bad.ok
    assert any(e.item for e in bad.errors), C.format_report(bad)
    assert "cite-in-strikeout" in svc.lint()
    svc.lint(fix=True)
    assert C.run(svc.proj).ok
