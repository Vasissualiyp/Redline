import json
import shutil
import sys
from pathlib import Path

import pytest

from redline import ops, sync, verify
from redline.model import RedlineError
from redline.parse import parse
from redline.service import Service
from redline.state import Project

FIX = Path(__file__).parent / "fixtures" / "mini.tex"


# ---- verification ---------------------------------------------------------------
def test_request_roundtrip():
    src = FIX.read_text()
    doc = parse(src)
    ln = doc.line_of(src.index("Pop III stars form"))
    out = ops.apply(src, verify.add_request(doc, ln, "refcheck", "Pop III stars form in minihaloes"))
    assert f"\\citep{{abel02}}.\n% VERIFY refcheck: «Pop III stars form in minihaloes»\n" in out
    d2 = parse(out)
    [r] = verify.requests(d2)
    assert r.method == "refcheck" and r.claim == "Pop III stars form in minihaloes" and r.claim_line == ln
    done = ops.apply(out, verify.record(d2, "VERIFIED", "abel02", '"first stars form in minihaloes" — §1, p.2',
                                        request_id=r.id))
    assert '% VERIFIED abel02: "first stars form in minihaloes" — §1, p.2\n' in done
    assert not verify.requests(parse(done))
    assert [x.status for x in verify.results(parse(done))] == ["VERIFIED"]
    # the request/result comment lines never start a new paragraph
    assert "\n\n% VERIFIED" not in done


def test_request_without_quote_uses_claim_line():
    src = FIX.read_text().replace("at a high rate.", "at a high rate.\n% VERIFY code")
    [r] = verify.requests(parse(src))
    assert r.method == "code" and "high rate" in r.claim


def test_result_needs_evidence():
    doc = parse(FIX.read_text())
    with pytest.raises(RedlineError):
        verify.record(doc, "VERIFIED", "", "", after_line=13)
    verify.record(doc, "UNVERIFIED", "", "no PDF in Zotero", after_line=13)


def test_service_record_by_anchor(tmp_path, monkeypatch):
    shutil.copy(FIX, tmp_path / "paper.tex")
    monkeypatch.setenv("REDLINE_ROOT", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    s = Service(Project.open())
    s.record_verification("REFUTED", "code:analysis/sinks.py:40", "rate is 2e-4, not high", anchor="at a high rate.")
    assert "at a high rate.\n% REFUTED code:analysis/sinks.py:40: rate is 2e-4, not high\n" in s.proj.read()
    assert "1 refuted" in s.verification_report()


# ---- sync to Overleaf --------------------------------------------------------------
@pytest.fixture
def paper(tmp_path, monkeypatch):
    root = tmp_path / "paper"
    root.mkdir()
    shutil.copy(FIX, root / "paper.tex")
    (root / ".olignore").write_text("*.log\n")
    monkeypatch.setenv("REDLINE_ROOT", str(root))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    overleaf = tmp_path / "overleaf"   # what the fake `ols -r` downloads
    overleaf.mkdir()
    shutil.copy(FIX, overleaf / "paper.tex")

    def fake_download(proj):
        remote = proj.dir / "remote"
        if remote.exists():
            shutil.rmtree(remote)
        shutil.copytree(overleaf, remote)
        return remote

    monkeypatch.setattr(sync, "download", fake_download)
    proj = Project.open()
    base = proj.dir / "base"
    shutil.copytree(overleaf, base)
    return Service(proj), overleaf


def test_unchanged_overleaf(paper):
    s, _ = paper
    chk = s.sync_check()
    assert chk.remote_unchanged


def test_only_overleaf_changed_is_taken(paper):
    s, overleaf = paper
    (overleaf / "paper.tex").write_text(FIX.read_text().replace("more text.", "Coauthor text."))
    out = s.sync_to_overleaf(push=False)
    assert "Coauthor text" in s.proj.read()
    assert "Coauthor text" in (s.proj.dir / "base" / "paper.tex").read_text()
    assert "Overleaf has new edits" in out


def test_both_changed_clean_git(paper):
    s, overleaf = paper
    (overleaf / "paper.tex").write_text(FIX.read_text().replace("more text.", "Coauthor text."))
    s.proj.main.write_text(FIX.read_text().replace("Pop III stars", "Population III stars"))
    chk = s.sync_check()
    assert chk.both_changed == ["paper.tex"] and chk.clean
    out = s.sync_to_overleaf(push=False, reuse=True)   # no agent asked for
    assert "merged automatically" in out
    t = s.proj.read()
    assert "Coauthor text" in t and "Population III stars" in t


def test_conflict_resolved_by_agent(paper, tmp_path):
    s, overleaf = paper
    (overleaf / "paper.tex").write_text(FIX.read_text().replace("at a high rate.", "at a very high rate."))
    s.proj.main.write_text(FIX.read_text().replace("at a high rate.", "at a moderate rate."))
    # a fake agent: resolve every conflict block by joining both sides
    script = tmp_path / "agent.py"
    script.write_text(
        "import re, json, pathlib\n"
        "for name in json.load(open('files.json')):\n"
        "    p = pathlib.Path(name); t = p.read_text()\n"
        "    t = re.sub(r'<<<<<<< local\\n(.*?)=======\\n(.*?)>>>>>>> overleaf\\n',"
        " lambda m: m.group(2).replace('very high', 'moderate-to-high'), t, flags=re.S)\n"
        "    p.write_text(t)\n"
        "pathlib.Path('RESOLUTION.md').write_text('took Overleaf wording, softened')\n")
    s.proj.cfg.agents["fake"] = [sys.executable, str(script), "{prompt}"]
    chk = s.sync_check()
    assert chk.both_changed == ["paper.tex"] and chk.conflicts["paper.tex"] == 1
    out = s.sync_to_overleaf("fake", push=False, reuse=True)
    assert "agent notes" in out and "softened" in out
    assert "at a moderate-to-high rate." in s.proj.read()
    assert s.sync_check(reuse=True).remote_unchanged  # base adopted the Overleaf version


def test_agent_leaving_markers_applies_nothing(paper, tmp_path):
    s, overleaf = paper
    (overleaf / "paper.tex").write_text(FIX.read_text().replace("at a high rate.", "at a very high rate."))
    s.proj.main.write_text(FIX.read_text().replace("at a high rate.", "at a moderate rate."))
    s.proj.cfg.agents["lazy"] = [sys.executable, "-c", "pass", "{prompt}"]
    before = s.proj.read()
    with pytest.raises(RedlineError, match="conflict markers"):
        s.sync_to_overleaf("lazy", push=False)
    assert s.proj.read() == before
    prompt = (s.proj.dir / "merge" / "INSTRUCTIONS.md").read_text()
    assert "paper.tex" in prompt and ".REMOTE" in prompt


def test_local_edits_only_means_push(paper):
    s, _ = paper
    s.proj.main.write_text(FIX.read_text().replace("Pop III stars", "Population III stars"))
    chk = s.sync_check()
    assert chk.remote_unchanged and chk.local_changed == ["paper.tex"]


def test_plain_ols_push_is_not_an_overleaf_change(paper):
    """Pushed with `ols -l` behind redline's back: Overleaf == local, snapshot stale."""
    s, overleaf = paper
    edited = FIX.read_text().replace("Pop III stars", "Population III stars")
    s.proj.main.write_text(edited)
    (overleaf / "paper.tex").write_text(edited)
    chk = s.sync_check()
    assert chk.remote_unchanged and chk.caught_up == ["paper.tex"]
    assert (s.proj.dir / "base" / "paper.tex").read_text() == edited  # snapshot refreshed


def test_overleaf_edits_already_contained_locally(paper):
    s, overleaf = paper
    (overleaf / "paper.tex").write_text(FIX.read_text().replace("more text.", "Coauthor text."))
    # local has Overleaf's edit plus more
    s.proj.main.write_text(FIX.read_text().replace("more text.", "Coauthor text.")
                           .replace("Pop III stars", "Population III stars"))
    chk = s.sync_check()
    assert chk.remote_unchanged and "paper.tex" in chk.caught_up


def test_interactive_agent_without_tmux(paper, tmp_path, monkeypatch):
    s, overleaf = paper
    (overleaf / "paper.tex").write_text(FIX.read_text().replace("at a high rate.", "at a very high rate."))
    s.proj.main.write_text(FIX.read_text().replace("at a high rate.", "at a moderate rate."))
    script = tmp_path / "tui.py"
    script.write_text(
        "import re, json, pathlib\n"
        "for name in json.load(open('files.json')):\n"
        "    p = pathlib.Path(name)\n"
        "    p.write_text(re.sub(r'<<<<<<< local\\n(.*?)=======\\n(.*?)>>>>>>> overleaf\\n', r'\\2', p.read_text(), flags=re.S))\n"
        "pathlib.Path('RESOLUTION.md').write_text('took Overleaf')\n")
    s.proj.cfg.agents["tui-fake"] = {"cmd": [sys.executable, str(script), "{prompt}"], "tui": True}
    monkeypatch.delenv("TMUX", raising=False)
    out = s.sync_to_overleaf("tui-fake", push=False)
    assert "at a very high rate." in s.proj.read() and "took Overleaf" in out
