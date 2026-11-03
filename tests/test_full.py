import asyncio
from pathlib import Path
import json
import subprocess

import pytest

from redline import config, overleaf, registry, sync

PAPER = "\\documentclass{article}\n\\begin{document}\nHello \\added{new} world.\n\\end{document}\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "papers"
    root.mkdir()
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(f'roots = ["{root}"]\n')
    monkeypatch.setattr(config, "GLOBAL_CONFIG", cfg)
    monkeypatch.setattr(registry, "sessions", lambda: set())
    import os
    os.environ.pop("REDLINE_ROOT", None)   # `redline -C` of an earlier test sets it for the whole process
    yield root
    os.environ.pop("REDLINE_ROOT", None)


def make_paper(root, name="alpha"):
    d = root / name
    d.mkdir()
    (d / "main.tex").write_text(PAPER)
    config.write_default(d)
    return d


def test_scan_and_listing(home):
    make_paper(home, "alpha")
    (home / "plain").mkdir()  # no .redline.toml: not a paper
    assert [p.name for p in registry.scan()] == ["alpha"]
    [it] = registry.listing()
    assert (it.name, it.pending, it.unrated, it.sync) == ("alpha", 1, 1, "no base")
    assert registry.find("alp") == home / "alpha"


def test_default_code_dir_is_created_and_ignored(home):
    d = make_paper(home)
    assert (d / "analysis").is_dir()
    sync.ensure_ignores(d)
    assert "analysis/*" in (d / ".olignore").read_text().splitlines()
    assert config.load(d).code_dir == str(d / "analysis")


def test_explicit_code_dir_is_not_created_elsewhere(home, tmp_path):
    d = home / "b"
    d.mkdir()
    (d / "main.tex").write_text(PAPER)
    config.write_default(d, str(tmp_path / "code"))
    assert not (d / "analysis").exists()
    assert (tmp_path / "code").is_dir()


def test_pull_existing(home, monkeypatch):
    def fetch(name, dest, ols="ols"):
        (dest / "main.tex").write_text(PAPER)
        return ""
    monkeypatch.setattr(sync, "fetch_project", fetch)
    dest = registry.pull_existing("My Paper")
    assert dest == home / "My Paper"
    cfg = config.load(dest)
    assert cfg.main == "main.tex" and cfg.project_name == "My Paper"
    assert (dest / ".redline" / "base" / "main.tex").read_text() == PAPER
    assert (dest / "analysis").is_dir()
    assert "{changes}" in (dest / "main.tex").read_text()  # added to the preamble, not to the base
    assert registry.listing()[0].sync == "local edits"


def test_create_blank_pushes_the_starter(home, monkeypatch):
    created, pushed = [], []
    monkeypatch.setattr(overleaf, "create", lambda name: created.append(name) or "id1")
    monkeypatch.setattr(sync, "fetch_project",
                        lambda name, dest, ols="ols": (dest / "main.tex").write_text("blank\n") or "")

    class Rep:
        pushed = True
        def text(self): return ""
    monkeypatch.setattr(sync, "push", lambda proj, force=False, skip_pull=False: pushed.append(proj.cfg.main_path.read_text()) or Rep())
    dest = registry.create_blank("Fresh")
    assert created == ["Fresh"]
    assert "\\usepackage" in pushed[0] and "{changes}" in pushed[0]
    assert (dest / ".redline" / "base" / "main.tex").read_text() == "blank\n"  # base = what Overleaf had
    with pytest.raises(Exception):
        registry.create_blank("Fresh")  # the directory exists now


def test_refuses_nonempty_destination(home):
    d = make_paper(home)
    with pytest.raises(Exception):
        registry.pull_existing("alpha", d)


def test_tui_lists_papers(home):
    make_paper(home, "alpha")
    make_paper(home, "beta")
    from redline.fulltui import Full

    async def go():
        app = Full()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.5)
            t = app.query_one("#papers")
            assert t.row_count == 2
            await pilot.press("n")
            await pilot.pause(0.2)
            assert app.screen.__class__.__name__ == "NewPaper"
            await pilot.press("escape")
            await pilot.pause(0.1)
            await pilot.press("enter")  # outside tmux: remember the paper and quit
            await pilot.pause(0.2)
        return app.open_root
    import os
    os.environ.pop("TMUX", None)
    assert asyncio.run(go()).name in ("alpha", "beta")


def test_preamble_adds_changes_once(tmp_path):
    from redline import preamble
    f = tmp_path / "p.tex"
    f.write_text("\\documentclass{article}\n\\usepackage{amsmath}\n% \\usepackage{changes}\n\\usepackage{graphicx}\n\\begin{document}\nx\n\\end{document}\n")
    assert preamble.ensure(f) == ["changes", "chcomment"]
    lines = f.read_text().splitlines()
    assert lines[3].endswith("{graphicx}") and "{changes}" in lines[4]
    assert lines[5] == "\\providecommand{\\chcomment}{\\comment}" and lines[6] == "\\begin{document}"
    assert preamble.ensure(f) == []  # already there


def test_preamble_respects_existing_and_bare_class(tmp_path):
    from redline import preamble
    f = tmp_path / "p.tex"
    f.write_text("\\documentclass{article}\n\\usepackage[draft]{xcolor,changes}\n\\begin{document}\n\\end{document}\n")
    assert preamble.ensure(f) == ["chcomment"]  # changes is loaded, its comment alias is not
    assert preamble.ensure(f) == []
    g = tmp_path / "q.tex"
    g.write_text("\\documentclass[11pt]{article}\n\n\\begin{document}\n\\end{document}\n")
    assert preamble.ensure(g) == ["changes", "chcomment"]
    assert g.read_text().splitlines()[1].startswith("\\usepackage[markup")


CHANGES = ("\\documentclass{article}\n\\begin{document}\n\\section{A}\n"
           + "".join(f"Line {i} \\added{{new {i}}} here.\n\n" for i in range(30)) + "\\end{document}\n")


def test_quick_decisions_and_messages(home):
    from redline import quick
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(CHANGES)
    proj = Project.open(d)
    ch = quick.list_changes(proj)
    assert len(ch) == 30 and ch[0]["kind"] == "added"
    quick.accept(proj, ch[0]["id"])
    quick.reject(proj, ch[1]["id"])
    src = (d / "main.tex").read_text()
    assert "Line 0 new 0 here." in src and "\\added{new 1}" not in src and "\\added{new 2}" in src
    quick.undo(proj)
    assert "\\added{new 1}" in (d / "main.tex").read_text()  # the reject is undone
    quick.mark_note(proj, ch[2]["id"], "shorter please")
    again = {c["id"]: c for c in quick.list_changes(proj)}
    assert again[ch[2]["id"]]["marker"]["note"] == "shorter please"
    assert "shorter please" in quick.note_message(again[ch[2]["id"]], "shorter please")
    assert "do not edit" in quick.ask_message(ch[3], "why?")


def test_tui_vim_motions_and_review(home):
    d = make_paper(home, "alpha")
    (d / "main.tex").write_text(CHANGES)
    for n in ("b", "c", "d"):
        make_paper(home, n)
    from redline.fulltui import Full
    import os
    os.environ.pop("TMUX", None)

    async def go():
        app = Full()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.6)
            t = app.query_one("#papers")
            assert t.row_count == 4
            await pilot.press("G")
            assert t.cursor_row == 3
            await pilot.press("g", "g")
            assert t.cursor_row == 0
            await pilot.press("j", "j", "k")
            assert t.cursor_row == 1
            await pilot.press("slash", *"alpha", "enter")
            await pilot.pause(0.2)
            assert t.row_count == 1
            await pilot.press("l")
            await pilot.pause(1.0)
            assert app.level == "changes" and app.paper.name == "alpha"
            c = app.query_one("#changes")
            assert c.row_count == 30
            await pilot.press("ctrl+d")
            assert c.cursor_row > 5
            await pilot.press("H")
            top = c.cursor_row
            await pilot.press("L")
            assert c.cursor_row >= top
            await pilot.press("g", "g", "a")
            await pilot.pause(1.0)
            assert c.row_count == 29 and "Line 0 new 0 here." in (d / "main.tex").read_text()
            await pilot.press("x")
            await pilot.pause(1.0)
            assert c.row_count == 28
            await pilot.press("u")
            await pilot.pause(1.0)
            assert c.row_count == 29
            await pilot.press("h")
            await pilot.pause(0.5)
            assert app.level == "papers"
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert app.query_one("#papers").row_count == 4
    asyncio.run(go())


def test_context_colours_each_kind(home):
    from redline import quick
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\section{A}\nOne \\added{plus} two \\deleted{minus} "
        "three \\replaced{new}{old} four \\chcomment{why}.\n\\end{document}\n")
    rows, doc = quick.load(Project.open(d))
    rep = next(r for r in rows if r["kind"] == "replaced")
    text = quick.context(doc, rep["id"], 2, 2)
    assert "[green]plus[/]" in text and "[red strike]minus[/]" in text
    assert "bold red strike underline]old[/]" in text and "bold green underline]new" in text  # the chosen one is bold
    assert "cyan" in text and "\\[why]" in text                        # comment, shown in brackets
    assert "▶" in text and "[dim]" in text


def test_stale_pdf_is_not_highlighted(home):
    import os, time
    from redline import quick, compile as C
    from redline.state import Project
    d = make_paper(home)
    proj = Project.open(d)
    pdf = C.view_pdf(proj)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4")
    old = time.time() - 3600
    os.utime(pdf, (old, old))
    assert quick.stale(proj)
    rows, _ = quick.load(proj)
    pane = quick.change_pane(proj)
    msg = quick.change_image(pane, rows[0], 800, 1000)
    assert isinstance(msg, str) and "out of date" in msg


def test_tui_text_view_and_chat(home, monkeypatch):
    from redline import quick
    d = make_paper(home, "alpha")
    (d / "main.tex").write_text(CHANGES)
    sent, started = [], []
    monkeypatch.setattr(quick, "alive", lambda root: False)
    monkeypatch.setattr(quick, "ensure_session", lambda root: started.append(root) or "s")
    monkeypatch.setattr(quick, "send_to_agent", lambda root, text: sent.append(text) or "s")
    import os
    os.environ.pop("TMUX", None)
    from redline.fulltui import Full

    async def go():
        app = Full()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("l")
            await pilot.pause(1.0)
            assert app.text_view and app.query_one("#cscroll").display and not app.query_one("#cpage").display
            await pilot.press("v")
            assert not app.text_view and app.query_one("#cpage").display
            await pilot.press("v")
            await pilot.press("t")
            await pilot.pause(0.3)
            for ch in "hello":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause(1.0)
    asyncio.run(go())
    assert started and "Message about the paper" in sent[0] and "hello" in sent[0]




def test_enforcement_off_lets_citations_through(home):
    from redline.service import Service
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\n\\section{A}\nStars form.\n\nGas flows.\n\\end{document}\n")
    # on (default): a new citation without evidence is refused and nothing is written
    svc = Service(Project.open(d))
    with pytest.raises(Exception, match="refcheck"):
        svc.propose_replace("Stars form.", "Stars form \\citep{smith20}.")
    assert "smith20" not in (d / "main.tex").read_text()
    config.set_verification(d, False)
    proj = Project.open(d)
    assert proj.cfg.enforce_verification is False
    out = Service(proj).propose_replace("Stars form.", "Stars form \\citep{smith20}.")
    src = (d / "main.tex").read_text()
    assert "smith20" in src and "% VERIFIED" not in src and "% UNVERIFIED" not in src
    assert "refcheck" not in out
    assert not [i for i in __import__("redline.lint", fromlist=["lint"]).lint(proj.doc(), False)
                if i.code == "unverified-citation"]
    Service(proj).plain_edit("Gas flows.", "Gas flows \\citep{jones21}.")  # plain edits may cite too
    assert "jones21" in (d / "main.tex").read_text()
    from redline import mcp_server
    import os
    os.environ["REDLINE_ROOT"] = str(d)
    try:
        assert "Reference checking is OFF" in mcp_server.instructions()
    finally:
        os.environ.pop("REDLINE_ROOT")


def test_bulk_accept_ignores_markers(home):
    from redline import quick
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\section{A}\n"
        "One \\added{a} %N later please\n\nTwo \\deleted{b} %X\n\nThree \\added{c}\n\n\\skcomment{keep me}\n\\end{document}\n")
    proj = Project.open(d)
    rows = quick.list_changes(proj)
    n = quick.bulk_summary(rows)
    assert (n["edits"], n["notes"], n["rejects"], n["comments"]) == (3, 1, 1, 1)
    quick.accept_all(proj)
    src = (d / "main.tex").read_text()
    assert "\\added" not in src and "\\deleted" not in src and "%N" not in src and "%X" not in src
    assert "One a" in src and "Two" in src and " b" not in src.split("Two")[1].split("Three")[0]
    assert "\\skcomment{keep me}" in src
    quick.undo(proj)
    assert "\\added{a}" in (d / "main.tex").read_text()


def test_tui_accept_all_asks_first(home):
    from redline.fulltui import Full
    d = make_paper(home, "alpha")
    (d / "main.tex").write_text(CHANGES)
    import os
    os.environ.pop("TMUX", None)

    async def go():
        app = Full()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("l")
            await pilot.pause(1.0)
            await pilot.press("A")
            await pilot.pause(0.3)
            assert app.screen.__class__.__name__ == "Confirm"
            await pilot.press("n")
            await pilot.pause(0.5)
            assert "\\added" in (d / "main.tex").read_text()   # declined: untouched
            await pilot.press("A")
            await pilot.pause(0.3)
            await pilot.press("y")
            await pilot.pause(1.5)
            assert "\\added" not in (d / "main.tex").read_text()
    asyncio.run(go())


def test_new_papers_can_opt_out_of_refcheck_at_creation(home, monkeypatch):
    monkeypatch.setattr(sync, "fetch_project", lambda name, dest, ols="ols": (dest / "main.tex").write_text(PAPER) or "")
    assert config.load(registry.pull_existing("Kept")).enforce_verification is True
    assert config.load(registry.pull_existing("Loose", verify=False)).enforce_verification is False
    config.set_verification(home / "Loose", True)  # editable later
    assert config.load(home / "Loose").enforce_verification is True


def test_categories_tree_order_colours(home):
    from redline import categories
    a, b, c = make_paper(home, "alpha"), make_paper(home, "beta"), make_paper(home, "gamma")
    categories.add("Proposals", "green")
    categories.add("Papers/Pop III", "#e5c07b")
    config.set_key(a, "category", "Papers/Pop III")
    config.set_key(b, "category", "Proposals")
    with pytest.raises(Exception):
        categories.add("x", "notacolour")
    assert categories.paths() == ["Proposals", "Papers", "Papers/Pop III"]
    tree = categories.build(registry.listing())
    rows = [(k, o.path if k == "cat" else o.name, d) for k, o, d, _ in categories.flatten(tree, set())]
    assert rows == [("cat", "Proposals", 0), ("paper", "beta", 1), ("cat", "Papers", 0), ("cat", "Papers/Pop III", 1),
                    ("paper", "alpha", 2), ("cat", "Uncategorized", 0), ("paper", "gamma", 1)]
    assert tree.children[0].color == "green" and tree.children[1].children[0].color == "#e5c07b"
    assert [r[1].name for r in categories.flatten(tree, {"Proposals"}) if r[0] == "paper"] == ["alpha", "gamma"]
    # reorder siblings: Papers before Proposals
    assert categories.move_category("Papers", -1)
    assert categories.paths()[0] == "Papers"
    # papers inside a category: user order
    config.set_key(c, "category", "Proposals")
    before = [p.path.name for p in categories.build(registry.listing()).children[1].papers]
    assert categories.move_paper("Proposals", list(before), before[1], -1)
    again = [p.path.name for p in categories.build(registry.listing()).children[1].papers]
    assert again == before[::-1]
    # rename moves the subtree; remove drops it
    assert "Papers/Pop III" in categories.rename("Papers", "Work")
    assert categories.paths()[:2] == ["Work", "Work/Pop III"]
    assert "Work/Pop III" in categories.remove("Work")
    assert "Work" not in categories.paths()


def test_tui_tree_edit_and_reorder(home):
    from redline import categories
    a, b = make_paper(home, "alpha"), make_paper(home, "beta")
    categories.add("Papers", "cyan")
    config.set_key(a, "category", "Papers")
    from redline.fulltui import Full
    import os
    os.environ.pop("TMUX", None)

    async def go():
        app = Full()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.8)
            t = app.query_one("#papers")
            assert t.row_count == 4  # Papers, alpha, Uncategorized, beta
            assert app.cat_path() == "Papers"
            await pilot.press("h")  # closes the category
            await pilot.pause(0.2)
            assert t.row_count == 3
            await pilot.press("l")
            await pilot.pause(0.2)
            assert t.row_count == 4
            await pilot.press("j", "h")  # from the paper up to its category
            assert app.cat_path() == "Papers"
            await pilot.press("j", "e")  # edit alpha
            await pilot.pause(0.3)
            assert app.screen.__class__.__name__ == "EditPaper"
            app.screen.query_one("#e-verify").value = False
            app.screen.query_one("#e-notes").value = "draft for the group"
            await pilot.press("ctrl+s")
            await pilot.pause(1.0)
            cfg = config.load(a)
            assert cfg.enforce_verification is False and cfg.notes == "draft for the group" and cfg.category == "Papers"
            # beta -> category via the edit screen
            await pilot.press("G", "e")
            await pilot.pause(0.3)
            app.screen.query_one("#e-cat").value = "Papers"
            await pilot.press("ctrl+s")
            await pilot.pause(1.0)
            assert config.load(b).category == "Papers"
            names = lambda: [p.path.name for p in app.cat_nodes["Papers"].papers]  # noqa: E731
            first = names()
            await pilot.press("g", "g", "j")  # on the first paper of Papers
            await pilot.press("J")
            await pilot.pause(0.5)
            assert names() == first[::-1]
    asyncio.run(go())


def test_cli_categories_and_set(home, capsys):
    from redline import cli
    a = make_paper(home, "alpha")
    assert cli.main(["full", "cat-add", "Papers/Pop III", "--color", "cyan"]) == 0
    assert cli.main(["full", "set", "alpha", "category=Papers/Pop III"]) == 0
    assert cli.main(["full", "set", "alpha", "enforce_verification=false"]) == 0
    cfg = config.load(a)
    assert cfg.category == "Papers/Pop III" and cfg.enforce_verification is False
    capsys.readouterr()
    assert cli.main(["full", "categories"]) == 0
    out = capsys.readouterr().out
    assert "[Papers] (white)" in out and "  [Papers/Pop III] (cyan)" in out and "    - alpha" in out
    assert cli.main(["full", "cat-rm", "Papers"]) == 0
    assert config.load(a).category == ""


def test_colour_picker_and_new_category_from_the_edit_screen(home):
    from redline import categories
    a = make_paper(home, "alpha")
    from redline.fulltui import Full
    import os
    os.environ.pop("TMUX", None)

    async def go():
        app = Full()
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(0.8)
            await pilot.press("e")  # edit alpha (flat list: first row)
            await pilot.pause(0.3)
            assert app.screen.__class__.__name__ == "EditPaper"
            sel = app.screen.query_one("#e-cat")
            sel.value = "\x00new"                      # "+ New category…"
            await pilot.pause(0.4)
            assert app.screen.__class__.__name__ == "CategoryForm"
            form = app.screen
            form.query_one("#c-name").value = "Papers/Pop III"
            lst = form.query_one("#swatches")
            lst.highlighted = 5                         # cyan
            await pilot.press("ctrl+s")
            await pilot.pause(0.5)
            assert app.screen.__class__.__name__ == "EditPaper"
            assert app.screen.query_one("#e-cat").value == "Papers/Pop III"
            await pilot.press("ctrl+s")
            await pilot.pause(1.0)
    asyncio.run(go())
    assert config.load(a).category == "Papers/Pop III"
    assert categories.load()["categories"] == [{"name": "Papers/Pop III", "color": "cyan"}]


def test_category_form_typed_colour_wins_and_is_validated(home):
    from redline.fulltui import CategoryForm, Full
    import os
    os.environ.pop("TMUX", None)
    result = []

    async def go():
        app = Full()
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(0.5)
            app.push_screen(CategoryForm("New category", "X"), result.append)
            await pilot.pause(0.3)
            app.screen.query_one("#c-color").value = "nonsense"
            await pilot.press("ctrl+s")
            await pilot.pause(0.2)
            assert app.screen.__class__.__name__ == "CategoryForm"   # refused
            app.screen.query_one("#c-color").value = "#e5c07b"
            await pilot.press("ctrl+s")
            await pilot.pause(0.3)
    asyncio.run(go())
    assert result == [("X", "#e5c07b")]


def test_agents_can_read_other_papers_unless_private(home):
    from redline import others
    from redline.state import Project
    a, b, c = make_paper(home, "alpha"), make_paper(home, "beta"), make_paper(home, "gamma")
    (b / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\n\\section{Method}\nThe Toomre parameter is Q.\n\\end{document}\n")
    config.set_key(b, "category", "Papers")
    config.set_key(c, "cross_paper_access", False)
    me = Project.open(a)
    listing = others.papers(me)
    assert "beta" in listing and "[Papers]" in listing and "gamma  [private]" in listing
    assert "Toomre parameter" in others.read(me, "beta", "Method")
    assert "Method" in others.outline(me, "beta")
    assert "main.tex:4" in others.search(me, "toomre") and "gamma" not in others.search(me, "Hello")
    with pytest.raises(Exception, match="private"):
        others.read(me, "gamma")
    config.set_key(a, "cross_paper_access", False)       # a closed paper's own agent sees nothing either
    with pytest.raises(Exception, match="turned off"):
        others.papers(Project.open(a))
    from redline import mcp_server
    assert "paper_read" in mcp_server.INSTRUCTIONS


def test_single_editor_flag(home, capsys):
    from redline import cli
    a = make_paper(home, "alpha")
    assert config.load(a).single_editor is False
    assert cli.main(["-C", str(a), "sync", "auto"]) == 5          # off: does nothing
    assert cli.main(["full", "set", "alpha", "single_editor=true"]) == 0
    cfg = config.load(a)
    assert cfg.single_editor is True and (cfg.auto_sync_seconds, cfg.auto_sync_poll) == (20, 120)
    capsys.readouterr()
    assert cli.main(["-C", str(a), "sync", "auto", "--enabled"]) == 0
    assert '"enabled": true' in capsys.readouterr().out


@pytest.mark.skipif(not __import__("shutil").which("tmux") or not __import__("shutil").which("nvim"), reason="needs tmux and nvim")
def test_two_sessions_open_and_quit_together(tmp_path, monkeypatch):
    import subprocess, time
    from redline import launcher
    import tempfile
    sock_dir = Path(tempfile.mkdtemp(prefix="rl", dir="/tmp"))   # tmux socket paths are short
    monkeypatch.setenv("TMUX_TMPDIR", str(sock_dir))
    monkeypatch.delenv("REDLINE_ROOT", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", tempfile.mkdtemp(prefix="rl", dir="/tmp"))  # nvim's socket path must be short
    root = tmp_path / "pair"
    root.mkdir()
    (root / "main.tex").write_text(PAPER)
    config.write_default(root)
    config.set_key(root, "agent", "cat")
    config.set_key(root, "two_sessions", True)
    t = lambda *a: subprocess.run(["tmux", *a], capture_output=True, text=True).stdout.split()  # noqa: E731
    try:
        assert launcher.open_workspace(root, attach=False) == 0
        assert sorted(t("list-sessions", "-F", "#{session_name}")) == ["redline-pair", "redline-pair-pdf"]
        assert sorted(t("list-panes", "-s", "-t", "=redline-pair", "-F", "#{@redline_role}")) == ["agent", "paper"]
        assert t("list-panes", "-s", "-t", "=redline-pair-pdf", "-F", "#{@redline_role}") == ["preview"]
        st = launcher.quit_status("redline-pair")
        assert any("pdf" in w for w in st["windows"])
        launcher.open_workspace(root, attach=False)  # again: nothing is duplicated
        assert len(t("list-sessions", "-F", "#{session_name}")) == 2
        launcher.quit_now("redline-pair-pdf")        # either side closes both
        time.sleep(0.5)
        assert t("list-sessions", "-F", "#{session_name}") == []
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


def test_two_sessions_is_a_paper_setting(home, capsys):
    from redline import cli
    a = make_paper(home, "alpha")
    assert config.load(a).two_sessions is False
    assert cli.main(["full", "set", "alpha", "two_sessions=true"]) == 0
    assert config.load(a).two_sessions is True


def test_tui_open_in_two_sessions(home, monkeypatch):
    from redline import launcher
    make_paper(home, "alpha")
    from redline.fulltui import Full
    import os
    os.environ.pop("TMUX", None)
    app = None

    async def go():
        nonlocal app
        app = Full()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("O")
            await pilot.pause(0.5)
    asyncio.run(go())
    assert app.open_root.name == "alpha" and app.open_two is True
    calls = []
    monkeypatch.setattr(launcher, "open_workspace", lambda root, attach=True, pdf=False, two=None: calls.append(two) or 0)
    monkeypatch.setattr(os, "execvp", lambda *a: None)
    from redline import fulltui
    monkeypatch.setattr(fulltui.Full, "run", lambda self, *a, **k: setattr(self, "open_root", Path("/x/alpha")) or setattr(self, "open_two", True))
    fulltui.run()
    assert calls == [True]


def test_open_cli_flags(monkeypatch):
    from redline import cli, launcher
    got = []
    monkeypatch.setattr(launcher, "open_workspace", lambda root, attach=True, pdf=False, two=None: got.append((attach, pdf, two)) or 0)
    cli.main(["open", "--two-sessions", "--no-attach"])
    cli.main(["open", "--one-session", "--no-attach"])
    cli.main(["open", "--pdf", "--no-attach"])
    assert got == [(False, False, True), (False, False, False), (False, True, None)]


@pytest.mark.skipif(not __import__("shutil").which("tmux") or not __import__("shutil").which("nvim"), reason="needs tmux and nvim")
def test_switch_between_one_and_two_sessions_while_running(tmp_path, monkeypatch):
    import subprocess, tempfile, time
    from redline import launcher
    monkeypatch.setenv("TMUX_TMPDIR", tempfile.mkdtemp(prefix="rl", dir="/tmp"))
    for var in ("TMUX", "TMUX_PANE", "REDLINE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", tempfile.mkdtemp(prefix="rl", dir="/tmp"))  # nvim's socket path must be short
    root = tmp_path / "live"
    root.mkdir()
    (root / "main.tex").write_text(PAPER)
    config.write_default(root)
    config.set_key(root, "agent", "cat")
    t = lambda *a: subprocess.run(["tmux", *a], capture_output=True, text=True).stdout.split()  # noqa: E731
    roles = lambda s: sorted(t("list-panes", "-s", "-t", "=" + s, "-F", "#{@redline_role}"))  # noqa: E731
    try:
        launcher.open_workspace(root, attach=False)                      # one session
        assert t("list-sessions", "-F", "#{session_name}") == ["redline-live"]
        assert roles("redline-live") == ["agent", "paper", "preview"]
        prev = launcher._role_pane("redline-live", "preview")
        pid = t("display", "-p", "-t", prev, "#{pane_pid}")
        launcher.switch_sessions(root, "two")                            # ... to two, while running
        assert sorted(t("list-sessions", "-F", "#{session_name}")) == ["redline-live", "redline-live-pdf"]
        assert roles("redline-live") == ["agent", "paper"] and roles("redline-live-pdf") == ["preview"]
        assert launcher._role_pane("redline-live-pdf", "preview") == prev          # moved, not restarted
        assert t("display", "-p", "-t", prev, "#{pane_pid}") == pid
        assert launcher.switch_sessions(root, "two").startswith("already")
        launcher.switch_sessions(root, "one")                            # ... and back
        assert t("list-sessions", "-F", "#{session_name}") == ["redline-live"]
        assert roles("redline-live") == ["agent", "paper", "preview"]
        assert t("display", "-p", "-t", prev, "#{pane_pid}") == pid
        launcher.switch_sessions(root, "toggle")
        assert len(t("list-sessions", "-F", "#{session_name}")) == 2
        launcher.quit_now("redline-live")                                # still closes both
        time.sleep(0.5)
        assert t("list-sessions", "-F", "#{session_name}") == []
        with pytest.raises(Exception, match="not running"):
            launcher.switch_sessions(root, "two")
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


OUTLINE_TEX = r"""\documentclass{article}
\begin{document}
\title{T}
\maketitle
\begin{abstract}
We study many things in this short abstract about stars forming.
\end{abstract}
\section{Intro}
Stars form from gas clouds \citep{abel02} and this is a paragraph with enough words.

Second paragraph here talks about turbulence and magnetic fields in detail.
\begin{figure}
\includegraphics{plots/dens.png}
\caption{Density of the cloud.}\label{fig:d}
\end{figure}
\begin{itemize}
\item First point about accretion rates and their scaling.
\item Second point about radiative feedback from the protostar.
\end{itemize}
\subsection{Details}
\begin{equation}
a = b + c
\end{equation}
Third paragraph \added{with a pending addition} ends here.
\end{document}
"""


def test_outline_blocks_hashes_and_stale_thoughts(home):
    from redline import outline
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(OUTLINE_TEX)
    proj = Project.open(d)
    o = outline.build(proj)
    kinds = [(n["kind"], n["depth"]) for n in o["nodes"]]
    assert kinds == [("abstract", 0), ("heading", 0), ("paragraph", 1), ("paragraph", 1), ("figure", 1), ("list", 1),
                     ("item", 2), ("item", 2), ("heading", 1), ("equation", 2), ("paragraph", 2)]   # no title block
    assert o["total"] == 9 and o["missing"] == 9
    fig = next(n for n in o["nodes"] if n["kind"] == "figure")
    assert "Caption: Density" in fig["text"] and fig["nlines"] == 4 and fig["words"] > 3
    # thoughts attach to hashes; an agent writes them through record
    hs = [n["hash"] for n in o["nodes"] if n["kind"] != "heading"]
    assert outline.record(proj, [{"hash": hs[0], "thought": "Why stars matter"}, {"hash": "nope", "thought": "x"}], "m1").endswith("nope")
    assert outline.build(proj)["missing"] == 8
    assert hs[1] in outline.pending(proj)
    # editing a paragraph keeps its old thought, stale; the hash decides
    para = next(n for n in o["nodes"] if n["kind"] == "paragraph")
    outline.record(proj, [{"hash": para["hash"], "thought": "Stars form from gas"}])
    (d / "main.tex").write_text(OUTLINE_TEX.replace("this is a paragraph with enough words", "this sentence changed a lot"))
    o2 = outline.build(proj)
    p2 = next(n for n in o2["nodes"] if n["kind"] == "paragraph")
    assert p2["hash"] != para["hash"] and p2["thought"] == "Stars form from gas" and p2["stale"] is True
    outline.record(proj, [{"hash": p2["hash"], "thought": "Stars form from clouds"}])
    assert next(n for n in outline.build(proj)["nodes"] if n["hash"] == p2["hash"])["stale"] is False
    # accepting the pending addition does not change the paragraph's hash (hash = accepted text)
    last = next(n for n in o2["nodes"] if n["kind"] == "paragraph" and "pending addition" in n["text"])
    (d / "main.tex").write_text((d / "main.tex").read_text().replace("\\added{with a pending addition}", "with a pending addition"))
    assert next(n for n in outline.build(proj)["nodes"] if n["lines"][0] == last["lines"][0])["hash"] == last["hash"]


def test_outline_flow_plan_and_agent_messages(home):
    from redline import outline
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(OUTLINE_TEX)
    proj = Project.open(d)
    o = outline.build(proj)
    body = [n for n in o["nodes"] if n["kind"] != "heading"]
    outline.record(proj, [{"hash": n["hash"], "thought": f"thought {i}"} for i, n in enumerate(body)])
    assert "thought 0" in outline.overview(proj)
    assert outline.record_flow(proj, [{"hashes": [body[1]["hash"], body[2]["hash"]], "kind": "gap", "note": "missing step"}]) == "recorded 1 flow flag(s)"
    flagged = [n for n in outline.build(proj)["nodes"] if n["flags"]]
    assert len(flagged) == 2 and flagged[0]["flags"][0]["kind"] == "gap"
    # a plan: the figure moves before the second paragraph
    order = [n["hash"] for n in o["nodes"]]
    fig_i = next(i for i, n in enumerate(o["nodes"]) if n["kind"] == "figure")
    plan = order[:3] + [order[fig_i]] + order[3:fig_i] + order[fig_i + 1:]
    moves = outline.plan_moves(proj, plan)
    assert [m["hash"] for m in moves] == [order[fig_i]] and moves[0]["after"] == order[2]
    outline.set_plan(proj, plan)
    assert "propose_move" in outline.plan_message(proj) and order[fig_i] in outline.plan_message(proj)
    # merge / split messages
    m = outline.edit_message(proj, "merge", [body[0]["hash"], body[1]["hash"]], "make it one paragraph")
    assert "ONE paragraph" in m and "make it one paragraph" in m and body[1]["hash"] in m
    s = outline.edit_message(proj, "split", [body[2]["hash"]])
    assert "Split this block" in s
    with pytest.raises(Exception):
        outline.edit_message(proj, "merge", [body[0]["hash"]])
    from redline import mcp_server
    assert "record_thoughts" in mcp_server.INSTRUCTIONS


def test_outline_refresh_runs_the_chosen_model_headless(home, tmp_path):
    import stat, sys
    from redline import outline, outline_agent
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(OUTLINE_TEX)
    fake = tmp_path / "fakemodel"
    repo = Path(__file__).resolve().parent.parent
    fake.write_text(f"#!{sys.executable}\nimport sys, pathlib\nsys.path.insert(0, {str(repo)!r})\n"
                    "from redline import outline\nfrom redline.state import Project\n"
                    "p = Project.open(pathlib.Path.cwd())\n"
                    "import re\nitems = [{'hash': n['hash'], 'thought': 'about ' + n['text'][:20]} for n in outline.build(p)['nodes'] if n['kind'] != 'heading']\n"
                    "print(outline.record(p, items, 'fake'))\nprint('args:', sys.argv[1:4])\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    t = d / ".redline.toml"
    t.write_text(t.read_text() + f'\n[models.fake]\nlabel = "Fake"\ncmd = "{fake}"\n')
    proj = Project.open(d)
    assert proj.cfg.outline_model == "deepseek" and "fake" in proj.cfg.models
    lines = []
    msg = outline_agent.refresh(proj, "fake", stream=lines.append)
    assert "9 of 9 thought(s) written" in msg and any("recorded 9" in l for l in lines)
    assert any("-p" in l for l in lines)                      # a claude-style entry runs headless with -p
    assert outline.build(proj)["missing"] == 0
    assert outline_agent.refresh(proj, "fake") == "every thought is up to date"
    with pytest.raises(Exception, match="not in \\[models\\]"):
        outline_agent.refresh(proj, "nope") if False else outline_agent.run(proj, "x", "nope")
    # the flow check needs thoughts first
    first = next(n for n in outline.build(proj)["nodes"] if n["kind"] != "heading")
    (d / "main.tex").write_text(OUTLINE_TEX.replace("We study many things", "We study entirely different things"))
    with pytest.raises(Exception, match="up-to-date thought"):
        outline_agent.flow(proj, "fake")


def test_thoughts_must_be_short(home):
    from redline import outline
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(OUTLINE_TEX)
    proj = Project.open(d)
    h = [n["hash"] for n in outline.build(proj)["nodes"] if n["kind"] != "heading"]
    assert "10 words or fewer" in outline.pending(proj)
    ok = outline.record(proj, [{"hash": h[0], "thought": "Stars form in gas clouds"}])
    assert ok == "recorded 1 thought(s)"
    long = " ".join(["word"] * 20)
    msg = outline.record(proj, [{"hash": h[1], "thought": long}, {"hash": h[2], "thought": "Fine and short"}])
    assert msg.startswith("recorded 1 thought(s)") and "too long" in msg and h[1] in msg
    assert outline.build(proj)["missing"] == len(h) - 2
    config.set_key(d, "outline_words", 6)
    assert "6 words or fewer" in outline.pending(Project.open(d))
    assert outline.forget(Project.open(d)).startswith("2 thought")


def test_agentpick_headless_commands():
    from redline import agentpick
    oc = {"cmd": "opencode --standalone", "model": "deepseek/deepseek-flash#high"}
    assert agentpick.headless_command(oc, "do it") == ["opencode", "run", "--standalone", "--auto", "-m",
                                                       "deepseek/deepseek-flash#high", "do it"]
    assert agentpick.headless_command({"cmd": "claude", "model": "haiku"}, "hi") == [
        "claude", "-p", "--allowedTools", "mcp__redline", "--model", "haiku", "hi"]


def test_overview_runs_cannot_edit_the_paper(home, monkeypatch):
    import json
    from redline import launcher
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(OUTLINE_TEX)
    proj = Project.open(d)
    launcher.write_mcp_config(proj, readonly=True)
    assert json.loads((d / ".mcp.json").read_text())["mcpServers"]["redline"]["env"]["REDLINE_READONLY"] == "1"
    assert json.loads((d / "opencode.json").read_text())["mcp"]["redline"]["environment"]["REDLINE_READONLY"] == "1"
    launcher.write_mcp_config(proj)
    assert "REDLINE_READONLY" not in json.loads((d / ".mcp.json").read_text())["mcpServers"]["redline"]["env"]
    monkeypatch.setenv("REDLINE_READONLY", "1")
    from redline.service import Service
    with pytest.raises(Exception, match="read-only"):
        Service(proj).propose_replace("Second paragraph here", "Changed paragraph here")
    assert "Second paragraph here" in (d / "main.tex").read_text()
    from redline import outline
    h = [n["hash"] for n in outline.build(proj)["nodes"] if n["kind"] != "heading"][0]
    assert outline.record(proj, [{"hash": h, "thought": "still allowed"}]) == "recorded 1 thought(s)"


def test_agent_can_find_and_read_the_code_dir(home, tmp_path, monkeypatch):
    from redline import codeaccess, mcp_server
    from redline.state import Project
    code = tmp_path / "analysis"
    (code / "plots").mkdir(parents=True)
    (code / "plots" / "plot_mass.py").write_text("import numpy as np\nM = np.load('m.npy')\nplt.plot(M)  # mass\n")
    (code / "notes.md").write_text("# notes\nSFE is M_star / (M_gas + M_star)\n")
    (code / "data.bin").write_bytes(b"\x00\x01")
    (tmp_path / "secret.txt").write_text("not for the agent")
    d = make_paper(home)
    config.set_key(d, "code_dir", str(code))
    proj = Project.open(d)
    ov = codeaccess.overview(proj)
    assert str(code) in ov and "plot_mass.py" in ov
    assert "plots/plot_mass.py" in codeaccess.listing(proj) and "data.bin" not in codeaccess.listing(proj)
    assert "plt.plot(M)" in codeaccess.read(proj, "plots/plot_mass.py", 1, 3)
    assert "notes.md:2" in codeaccess.search(proj, "sfe")
    with pytest.raises(Exception, match="outside"):
        codeaccess.read(proj, "../secret.txt")
    (code / "link").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(Exception, match="outside"):
        codeaccess.read(proj, "link")
    # the agent is told where it is, and status says so
    monkeypatch.setenv("REDLINE_ROOT", str(d))
    assert str(code) in mcp_server.instructions() and "never ask the human where it is" in mcp_server.instructions()
    from redline.service import Service
    assert Service(proj).status()["code_dir"] == str(code)
    # no code_dir: a clear answer
    config.set_key(d, "code_dir", "")
    with pytest.raises(Exception, match="no code_dir"):
        codeaccess.overview(Project.open(d))


def test_a_hung_mount_does_not_block(tmp_path, monkeypatch):
    import time
    from redline import codeaccess
    with monkeypatch.context() as m:
        m.setattr(codeaccess, "_probe_cmd", lambda p: ["sleep", "30"])   # a probe stuck in I/O
        t0 = time.time()
        assert codeaccess.is_dir_within(tmp_path, 0.3) is None and time.time() - t0 < 3
    assert codeaccess.is_dir_within(tmp_path, 5) is True
    assert codeaccess.is_dir_within(tmp_path / "missing", 5) is False


PLAN_TEX = r"""\documentclass{article}
\begin{document}
\section{One}
Alpha paragraph says the first thing about stars forming in gas.

Bravo paragraph says the second thing about turbulence and fields.

Charlie paragraph says the third thing about radiation feedback.

Delta paragraph says the fourth thing about the final mass function.
\section{Two}
Echo paragraph opens the second section with a different topic here.
\end{document}
"""


def test_plan_applied_declaratively_as_tracked_markup(home):
    from redline import outline
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(PLAN_TEX)
    proj = Project.open(d)
    o = outline.build(proj)
    by = {n["text"].split()[0]: n["hash"] for n in o["nodes"] if n["kind"] == "paragraph"}
    order = [n["hash"] for n in o["nodes"]]
    # Delta moves up after Alpha; Charlie goes away
    order.remove(by["Delta"])
    order.insert(order.index(by["Alpha"]) + 1, by["Delta"])
    outline.set_plan(proj, {"order": [h for h in order if h != by["Charlie"]], "deleted": [by["Charlie"]]})
    msg = outline.apply_direct(proj)
    src = (d / "main.tex").read_text()
    assert msg.startswith("2 change(s) written"), msg   # Delta moved, Charlie deleted
    assert "\\added{Delta paragraph" in src and "\\deleted{Delta paragraph" in src
    assert "\\deleted{Charlie paragraph" in src and "\\added{Charlie" not in src
    assert src.index("\\added{Delta paragraph") < src.index("Bravo paragraph") < src.index("\\deleted{Delta paragraph")
    assert "Moved to" in src and "Moved here from" in src             # each move explains itself
    assert outline.load_store(proj)["plan"] is None
    # the reading order is now the plan: hashes follow the accepted text across the tracked move
    after = [n["text"].split()[0] for n in outline.build(proj)["nodes"] if n["kind"] == "paragraph"]
    assert after == ["Alpha", "Delta", "Bravo", "Echo"]
    # one write, one undo
    proj.undo()
    assert (d / "main.tex").read_text() == PLAN_TEX


def test_declarative_apply_reports_what_it_cannot_do(home):
    from redline import outline
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(PLAN_TEX.replace("the third thing about radiation", "the third \\added{new} thing about radiation"))
    proj = Project.open(d)
    o = outline.build(proj)
    by = {n["text"].split()[0]: n["hash"] for n in o["nodes"] if n["kind"] == "paragraph"}
    order = [n["hash"] for n in o["nodes"] if n["hash"] != by["Charlie"]]
    outline.set_plan(proj, {"order": order + [by["Charlie"]], "deleted": []})   # Charlie holds a pending change
    msg = outline.apply_direct(proj)
    assert "SKIPPED" in msg and "pending change" in msg or "nothing could be applied" in msg
    assert "Charlie" in (d / "main.tex").read_text()


def test_headings_carry_their_section_numbers(home):
    from redline import outline
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(r"""\documentclass{article}
\begin{document}
\section{One}
Alpha paragraph says the first thing about stars forming in gas.
\subsection{Sub}
Bravo paragraph says the second thing about turbulence and fields.
\subsubsection{Deep}
Charlie paragraph says the third thing about radiation feedback.
\section*{Starred}
Delta paragraph says the fourth thing about the final mass function.
\section{Two}
Echo paragraph opens the second section with a different topic here.
\end{document}
""")
    nums = [(n["number"], n["title"]) for n in outline.build(Project.open(d))["nodes"] if n["kind"] == "heading"]
    assert nums == [("1", "One"), ("1.1", "Sub"), ("1.1.1", "Deep"), ("", "Starred"), ("2", "Two")]


def test_split_recommended_only_for_long_blocks_with_separate_ideas(home):
    from redline import outline
    from redline.state import Project
    d = make_paper(home)
    long = " ".join(["Panel A shows the density"] * 40)   # ~200 words
    short = "Panel A shows the density and panel B the temperature of the gas."
    (d / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\n\\section{S}\n" + long + "\n\n" + short + "\n\\end{document}\n")
    proj = Project.open(d)
    nodes = [n for n in outline.build(proj)["nodes"] if n["kind"] == "paragraph"]
    big, small = nodes
    assert big["words"] >= 200 > small["words"] and "SPLIT RECOMMENDED" in outline.pending(proj)
    msg = outline.record(proj, [
        {"hash": big["hash"], "thought": "Two panels described", "split": ["Panel A density", "Panel B temperature"]},
        {"hash": small["hash"], "thought": "Both panels in brief", "split": ["panel A", "panel B"]}])
    assert "split ignored for 1 block" in msg                       # the 12-word paragraph is fine as it is
    o = {n["hash"]: n for n in outline.build(proj)["nodes"]}
    assert o[big["hash"]]["split"] == ["Panel A density", "Panel B temperature"] and o[small["hash"]]["split"] is None
    assert "split recommended" in outline.overview(proj)
    assert "suggested split into: Panel A density | Panel B temperature" in outline.edit_message(proj, "split", [big["hash"]])
    # one idea is never a split, whatever the length; a stale thought drops the recommendation
    outline.record(proj, [{"hash": big["hash"], "thought": "One idea", "split": ["only one"]}])
    assert outline.build(proj)["nodes"][1]["split"] is None
    outline.record(proj, [{"hash": big["hash"], "thought": "Two panels", "split": "A | B"}])
    (d / "main.tex").write_text((d / "main.tex").read_text().replace("Panel A shows the density Panel A", "Changed wording. Panel A", 1))
    changed = [n for n in outline.build(proj)["nodes"] if n["kind"] == "paragraph"][0]
    assert changed["stale"] and changed["split"] is None


def test_split_dismissal_suggestions_and_rerun(home):
    from redline import outline
    from redline.state import Project
    d = make_paper(home)
    long = " ".join(["Panel A shows the density"] * 40)
    (d / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\n\\section{S}\n" + long + "\n\nSecond block says something quite different here today.\n\nThird block closes the argument with a summary of it.\n\\end{document}\n")
    proj = Project.open(d)
    a, b, c = [n["hash"] for n in outline.build(proj)["nodes"] if n["kind"] == "paragraph"]
    outline.record(proj, [{"hash": a, "thought": "Two panels", "split": ["A", "B"]}, {"hash": b, "thought": "Different topic"},
                          {"hash": c, "thought": "Conclusion"}])
    assert outline.build(proj)["splits"] == 1
    # dismissed: gone, and it stays gone
    assert outline.dismiss(proj, [a]) == "dismissed 1 recommendation(s)"
    assert outline.build(proj)["splits"] == 0 and outline.build(proj)["nodes"][1]["split"] is None
    assert "split recommended" not in outline.overview(proj)
    # ... until the block is run through the model again
    outline.forget_blocks(proj, [a])
    assert outline.build(proj)["missing"] == 1
    outline.record(proj, [{"hash": a, "thought": "Two panels again", "split": ["A", "B"]}])
    assert outline.build(proj)["splits"] == 1
    # the review pass: moves and merges, validated against the paper; a dismissed one stays away
    msg = outline.record_suggestions(proj, [
        {"op": "move", "hash": c, "after": None, "note": "conclusion belongs first"},
        {"op": "merge", "hashes": [b, a], "note": "same topic"},
        {"op": "move", "hash": "nope", "after": a, "note": "x"}, {"op": "delete", "hash": a}])
    assert msg.startswith("recorded 2 suggestion(s)") and "invalid ignored" in msg
    o = outline.build(proj)
    assert o["suggestions"] == 2
    by = {n["hash"]: n for n in o["nodes"]}
    assert by[c]["suggestions"][0]["op"] == "move" and by[a]["suggestions"][0]["hashes"] == [a, b]  # paper order
    assert "knows the splits" or "split recommended: A | B" in outline.overview(proj)
    outline.dismiss(proj, [c])
    assert outline.build(proj)["suggestions"] == 1
    assert "'split recommended" in outline.review_task(proj)
    # a stale thought keeps its (old) text but loses its recommendations: carry works for long blocks too
    (d / "main.tex").write_text((d / "main.tex").read_text().replace("Panel A shows the density Panel A", "Changed wording. Panel A", 1))
    changed = [n for n in outline.build(proj)["nodes"] if n["kind"] == "paragraph"][0]
    assert changed["stale"] and changed["thought"] == "Two panels again" and changed["split"] is None


def test_blocks_know_what_waits_for_review(home):
    from redline import outline
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\section{S}\n"
        "First paragraph has \\added{a new bit} and \\deleted{an old bit} here today.\n\n"
        "Second paragraph \\replaced{better}{worse} text %Y with enough words around it.\n\n"
        "Third paragraph carries \\skcomment{a coauthor question} only a comment and words.\n\n"
        "Fourth paragraph is clean and has no markup at all in it whatsoever.\n\\end{document}\n")
    nodes = [n for n in outline.build(Project.open(d))["nodes"] if n["kind"] == "paragraph"]
    r = [n["review"] for n in nodes]
    assert (r[0]["edits"], r[0]["unrated"], r[0]["comments"]) == (2, 2, 0)
    assert (r[1]["edits"], r[1]["accepted"], r[1]["unrated"]) == (1, 1, 0)
    assert (r[2]["edits"], r[2]["comments"]) == (0, 1)
    assert not any(r[3].values())
    sec = next(n for n in outline.build(Project.open(d))["nodes"] if n["kind"] == "heading")["review"]
    assert sec["edits"] == 3 and sec["comments"] == 1                 # a section sums its blocks
    assert outline.build(Project.open(d))["to_review"] == 3


def test_full_reclassification_resets_everything_that_depends_on_thoughts(home):
    from redline import outline
    from redline.state import Project
    d = make_paper(home)
    (d / "main.tex").write_text(PLAN_TEX)
    proj = Project.open(d)
    h = [n["hash"] for n in outline.build(proj)["nodes"] if n["kind"] == "paragraph"]
    outline.record(proj, [{"hash": x, "thought": f"t{i}"} for i, x in enumerate(h)])
    outline.record_suggestions(proj, [{"op": "move", "hash": h[0], "after": h[3], "note": "x"}])
    outline.record_flow(proj, [{"hash": h[1], "kind": "gap", "note": "g"}])
    outline.dismiss(proj, [h[0]])
    outline.set_plan(proj, {"order": list(reversed(h)), "deleted": []})
    o = outline.build(proj)
    assert (o["thoughts"], o["suggestions"], o["flags"], o["dismissed"], o["model"]) == (5, 0, 1, 1, "deepseek")
    outline.forget(proj)
    o = outline.build(proj)
    assert (o["thoughts"], o["missing"], o["suggestions"], o["flags"], o["dismissed"]) == (0, 5, 0, 0, 0)
    assert outline.load_store(proj)["plan"] is not None          # your draft plan is not touched


def test_a_hung_code_dir_mount_is_never_touched_by_setup(home, monkeypatch):
    """code_dir on a stale sshfs mount froze `redline open` (resolve() in code_ignores): setup must not touch it."""
    from pathlib import Path as P
    d = make_paper(home)
    hung = "/mnt/hung-cluster/analysis"
    config.set_key(d, "code_dir", hung)
    touched = []
    for name in ("resolve", "exists", "is_dir", "stat", "lstat", "iterdir"):
        real = getattr(P, name)

        def guard(self, *a, __real=real, __name=name, **k):
            if "hung-cluster" in str(self):
                touched.append(__name)
                raise RuntimeError("touched the hung mount")
            return __real(self, *a, **k)
        monkeypatch.setattr(P, name, guard)
    assert config.code_ignores(d, hung) == []
    assert config.code_ignores(d, "analysis") == ["analysis", "analysis/*"]
    assert sync.ensure_ignores(d) is not None
    assert config.load(d).code_dir == hung
    assert not touched, touched
