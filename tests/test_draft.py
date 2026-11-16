"""Initial drafting: the brief, the sandboxed materials, literature search, the phases and the agent runs."""
import json
import stat
import sys
from pathlib import Path

import pytest

from redline import config, draft, draft_agent
from redline.model import RedlineError
from redline.state import Project
from test_full import home, make_paper  # noqa: F401  (fixtures)

REPO = Path(__file__).resolve().parent.parent

DRAFT_TEX = ("\\documentclass{article}\n\\begin{document}\nStars form \\cite{smith20}. See \\cite{ghost99}.\n"
             "\\bibliography{references}\n\\end{document}\n")


def setbrief(d, **kv):
    """Set single-line keys of draft.toml (values are TOML text)."""
    import re
    t = d / draft.BRIEF
    text = t.read_text()
    for k, v in kv.items():
        text, n = re.subn(rf"^{k} = .*$", f"{k} = {v}", text, count=1, flags=re.M)
        assert n == 1, k
    t.write_text(text)


def paper(home, goal="paper", **kv):
    d = make_paper(home)
    (d / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\n\\end{document}\n")
    proj = Project.open(d)
    draft.init(proj, topic="cooling of first stars", goal=goal)
    setbrief(d, **kv)
    return d, Project.open(d)


def fake_model(tmp_path, d):
    """A model that does what the task asks through the library: notes for a review, draft_write for a write."""
    fake = tmp_path / "fakemodel"
    fake.write_text(f"#!{sys.executable}\nimport sys, pathlib\nsys.path.insert(0, {str(REPO)!r})\n"
                    "from redline import draft\nfrom redline.state import Project\n"
                    "p = Project.open(pathlib.Path.cwd())\ntask = ' '.join(sys.argv)\n"
                    "if 'literature review' in task:\n"
                    "    for n in ('litreview', 'outline', 'claims'):\n"
                    "        print(draft.write_note(p, n, 'smith20 says stars form. quote p.3'))\n"
                    "else:\n"
                    f"    print(draft.write_tex(p, {DRAFT_TEX!r}))\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    t = d / ".redline.toml"
    t.write_text(t.read_text() + f'\n[models.fake]\nlabel = "Fake"\ncmd = "{fake}"\n')
    setbrief(d, model='"fake"')
    return Project.open(d)


def test_init_and_brief(home):
    d, proj = paper(home)
    b = draft.load_brief(proj)
    assert (b.goal, b.topic, b.checkpoint, b.search) == ("paper", "cooling of first stars", True, ["openalex", "arxiv"])
    with pytest.raises(RedlineError, match="already exists"):
        draft.init(proj)
    (d / draft.BRIEF).write_text('goal = "novel"\n')
    with pytest.raises(RedlineError, match="goal must be"):
        draft.load_brief(proj)
    (d / draft.BRIEF).write_text('search = ["bing"]\n')
    with pytest.raises(RedlineError, match="unknown search source"):
        draft.load_brief(proj)
    assert "no draft.toml" in draft.status(Project.open(make_paper(home, "beta")))


def test_materials_are_sandboxed(home, tmp_path):
    res = tmp_path / "results"
    (res / "sub").mkdir(parents=True)
    (res / "table.csv").write_text("mass,rate\n10,0.5\n20,0.9\n")
    (res / "sub" / "log.txt").write_text("done\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("no")
    d, proj = paper(home, results_dir=json.dumps(str(res)))
    assert "table.csv" in draft.listing(proj, str(res))
    assert "0.9" in draft.read(proj, "table.csv")                       # relative to results_dir
    assert "log.txt" in draft.search(proj, "done")
    with pytest.raises(RedlineError, match="not inside"):
        draft.read(proj, str(secret))
    with pytest.raises(RedlineError, match="not inside"):
        draft.read(proj, "../secret.txt")
    assert "results:" in draft.overview(proj) and "MISSING" not in draft.overview(proj)


def test_literature_search_and_add(home, monkeypatch):
    d, proj = paper(home)
    oa = {"results": [{"title": "Cooling", "publication_year": 2020, "doi": "https://doi.org/10.1/x",
                       "authorships": [{"author": {"display_name": "A. Smith"}}], "cited_by_count": 7,
                       "abstract_inverted_index": {"Gas": [0], "cools": [1]},
                       "primary_location": {"source": {"display_name": "ApJ"}}}]}
    arx = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2001.00001v1</id>'
           b'<title>Fragmentation</title><published>2020-01-01</published><summary>Text</summary>'
           b'<author><name>B. Jones</name></author></entry></feed>')
    monkeypatch.setattr(draft, "_get", lambda url, headers=None, timeout=25: json.dumps(oa).encode()
                        if "openalex" in url else arx)
    r = draft.lit_search(proj, "cooling")
    assert "doi=10.1/x" in r and "Gas cools" in r and "ApJ" in r
    assert "eprint=2001.00001v1" in draft.lit_search(proj, "frag", "arxiv")
    with pytest.raises(RedlineError, match="not enabled"):
        draft.lit_search(proj, "x", "ads")
    from redline import zotero
    pulled = []
    monkeypatch.setattr(zotero.Zotero, "pull", lambda self, key: pulled.append(key) or "ITEM")
    assert "in Zotero" in draft.lit_add(proj, "smith20", "Cooling", ["A. Smith"], "2020", "ApJ", doi="10.1/x")
    assert pulled == ["smith20"] and "@article{smith20," in (d / "references.bib").read_text()
    with pytest.raises(RedlineError, match="already in"):
        draft.lit_add(proj, "smith20", "Cooling", ["A. Smith"], "2020", doi="10.1/x")
    with pytest.raises(RedlineError, match="doi or an arXiv"):
        draft.lit_add(proj, "nodoi", "T", ["A"], "2020")

    def boom(self, key):
        raise zotero.ZoteroError("paywalled")
    monkeypatch.setattr(zotero.Zotero, "pull", boom)
    assert "NOT in Zotero" in draft.lit_add(proj, "jones20", "Frag", ["B. Jones"], "2020", eprint="2001.00001")


def test_max_new_papers(home, monkeypatch):
    d, proj = paper(home, max_new_papers="1")
    from redline import zotero
    monkeypatch.setattr(zotero.Zotero, "pull", lambda self, key: "ITEM")
    draft.lit_add(proj, "a1", "T", ["A"], "2020", doi="10.1/a")
    with pytest.raises(RedlineError, match="max_new_papers"):
        draft.lit_add(proj, "a2", "T", ["A"], "2020", doi="10.1/b")


def test_review_then_checkpoint_then_write(home, tmp_path):
    d, _ = paper(home)
    proj = fake_model(tmp_path, d)
    with pytest.raises(RedlineError, match="not approved"):
        draft_agent.write(proj)
    msg = draft_agent.review(proj)
    assert "notes in draft/" in msg and draft.state(proj)["phase"] == "reviewed"
    assert (d / "draft" / "litreview.md").exists() and (d / "draft" / "outline.md").exists()
    assert "\\cite" not in (d / "main.tex").read_text()                 # the review run wrote no paper
    with pytest.raises(RedlineError, match="not approved"):
        draft_agent.write(proj)                                           # the checkpoint is real
    draft.approve(proj)
    with pytest.raises(RedlineError, match="already approved"):
        draft_agent.review(proj)                                          # approved notes are not overwritten
    out = draft_agent.write(proj)
    assert "main.tex written" in out and "ghost99" in out                 # the unknown key is reported
    text = (d / "main.tex").read_text()
    assert "\\cite{smith20}" in text and "changes" in text               # the preamble got Redline's package
    assert draft.state(proj)["phase"] == "written"


def test_write_reports_unknown_and_unread_citations(home, monkeypatch):
    d, proj = paper(home)
    draft.write_note(proj, "litreview", "smith20: stars form")
    (d / "references.bib").write_text("@article{smith20, title={T}}\n@article{other, title={U}}\n")
    draft._set_phase(proj, "approved")
    out = draft.write_tex(proj, DRAFT_TEX)
    assert "ghost99" in out.split("CITED BUT NOT IN ANY .bib")[1]
    assert "never mentioned" not in out                                   # smith20 is in the notes
    tex = DRAFT_TEX.replace("ghost99", "other")
    draft._set_phase(proj, "approved")
    assert "never mentioned in the lit review notes (justify or remove): other" in draft.write_tex(proj, tex)


def test_write_will_not_replace_a_paper_without_force(home, tmp_path):
    d, _ = paper(home)
    (d / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\n" + "Real text. " * 300 + "\n\\end{document}\n")
    proj = fake_model(tmp_path, d)
    draft._set_phase(proj, "approved")
    with pytest.raises(RedlineError, match="--force"):
        draft_agent.write(proj)
    draft_agent.write(proj, force=True)
    assert "\\cite{smith20}" in (d / "main.tex").read_text()
    assert draft.state(proj)["force"] is False


def test_review_run_cannot_write_the_draft(home, monkeypatch):
    d, proj = paper(home)
    draft._set_phase(proj, "approved")
    monkeypatch.setenv("REDLINE_READONLY", "1")
    with pytest.raises(RedlineError, match="read-only"):
        draft.write_tex(proj, DRAFT_TEX)
    assert "written" in draft.write_note(proj, "litreview", "notes still work")


def test_survey_goal_writes_survey_tex_and_leaves_main_alone(home, tmp_path):
    d, _ = paper(home, goal="survey")
    before = (d / "main.tex").read_text()
    proj = fake_model(tmp_path, d)
    draft_agent.review(proj)
    assert (d / "draft" / "claims.md").exists()
    assert "claims" in draft.review_task(draft.load_brief(proj))
    draft.approve(proj)
    draft_agent.write(proj)
    assert "\\cite{smith20}" in (d / "survey.tex").read_text()
    assert (d / "main.tex").read_text() == before


def test_no_checkpoint_runs_straight_through(home, tmp_path):
    d, _ = paper(home)
    setbrief(d, checkpoint="false")
    proj = fake_model(tmp_path, d)
    out = draft_agent.run_all(proj)
    assert "main.tex written" in out and draft.state(proj)["phase"] == "written"


def test_review_needs_a_topic_and_the_notes(home, tmp_path):
    d, _ = paper(home)
    setbrief(d, topic='""')
    with pytest.raises(RedlineError, match="no topic"):
        draft_agent.review(Project.open(d))
    setbrief(d, topic='"x"')
    lazy = tmp_path / "lazy"
    lazy.write_text(f"#!{sys.executable}\nprint('nothing')\n")
    lazy.chmod(lazy.stat().st_mode | stat.S_IXUSR)
    t = d / ".redline.toml"
    t.write_text(t.read_text() + f'\n[models.lazy]\nlabel = "Lazy"\ncmd = "{lazy}"\n')
    with pytest.raises(RedlineError, match="did not write"):
        draft_agent.review(Project.open(d), "lazy")


def test_mcp_exposes_the_draft_tools():
    from redline import mcp_server
    for name in ("draft_brief", "draft_list", "draft_read", "draft_search", "lit_search", "lit_add", "draft_note",
                 "draft_write"):
        assert hasattr(mcp_server, name)


def test_cli_draft_commands(home, monkeypatch, capsys):
    from redline import cli
    d = make_paper(home)
    monkeypatch.setenv("REDLINE_ROOT", str(d))
    assert cli.main(["draft", "init", "--topic", "t", "--goal", "survey"]) == 0
    assert 'goal = "survey"' in (d / draft.BRIEF).read_text()
    assert cli.main(["draft", "status"]) == 0
    assert "goal: survey   phase: brief" in capsys.readouterr().out
    assert cli.main(["draft", "approve"]) != 0                    # nothing reviewed yet: an error, not a crash
    assert "nothing to approve" in capsys.readouterr().err


def test_full_new_draft_flag(home, monkeypatch):
    from redline import cli, registry
    monkeypatch.setattr(registry, "create_blank", lambda name, dest, code_dir, verify, overwrite=False: make_paper(home, name))
    assert cli.main(["full", "new", "gamma", "--draft", "paper", "--topic", "x"]) == 0
    assert 'topic = "x"' in (home / "gamma" / draft.BRIEF).read_text()


def test_tui_draft_screen_and_new_paper_option(home, monkeypatch):
    import asyncio
    import os
    from redline import fulltui
    d = make_paper(home, "alpha")
    os.environ.pop("TMUX", None)
    calls = []

    async def go():
        app = fulltui.Full()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("w")
            await pilot.pause(0.2)
            assert app.screen.__class__.__name__ == "DraftScreen"
            await pilot.press("s")                                 # a survey brief
            await pilot.pause(1.0)
            assert (d / draft.BRIEF).exists() and 'goal = "survey"' in (d / draft.BRIEF).read_text()
            await pilot.press("w")
            await pilot.pause(0.2)
            await pilot.press("a")                                 # nothing to approve yet: reported, not a crash
            await pilot.pause(0.8)
            monkeypatch.setattr(draft_agent, "review", lambda proj, *a, **k: calls.append("review") or "ok")
            await pilot.press("w")
            await pilot.pause(0.2)
            await pilot.press("r")
            await pilot.pause(0.8)
            await pilot.press("n")
            await pilot.pause(0.3)
            assert app.screen.__class__.__name__ == "NewPaper"
            assert any(b.key == "ctrl+d" for b in app.screen.BINDINGS)
    asyncio.run(go())
    assert calls == ["review"]
    assert fulltui.GoalChoice and fulltui.DraftScreen


def test_local_only_paper_has_no_overleaf(home, monkeypatch, capsys):
    from redline import cli, registry, sync
    monkeypatch.setattr(sync, "fetch_project", lambda *a, **k: pytest.fail("a local paper must not touch Overleaf"))
    d = registry.create_local("notes")
    proj = Project.open(d)
    assert proj.cfg.local_only and not proj.cfg.overleaf_project and "\\begin{document}" in (d / "main.tex").read_text()
    assert "{changes}" in (d / "main.tex").read_text()                     # Redline's package is in the preamble
    [it] = registry.listing()
    assert it.sync == "local"
    for fn in (sync.pull, sync.push, sync.init_base, sync.check):
        with pytest.raises(RedlineError, match="local-only"):
            fn(proj)
    assert sync.auto(proj)[0] == "idle"
    from redline import launcher
    monkeypatch.setattr(launcher, "_session_root", lambda s: d)
    monkeypatch.setattr(launcher, "_tmux", lambda *a, **k: "paper")
    assert launcher.quit_status("s")["unpushed"] == []                      # nothing is "not on Overleaf yet"


def test_cli_new_local_and_set(home, monkeypatch, capsys):
    from redline import cli, sync
    monkeypatch.setattr(sync, "fetch_project", lambda *a, **k: pytest.fail("Overleaf touched"))
    assert cli.main(["full", "new", "solo", "--local", "--draft", "survey", "--topic", "q"]) == 0
    d = home / "solo"
    assert Project.open(d).cfg.local_only and 'goal = "survey"' in (d / draft.BRIEF).read_text()
    other = make_paper(home, "beta")
    assert cli.main(["full", "set", "beta", "local_only=true"]) == 0
    assert Project.open(other).cfg.local_only
