import asyncio
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
    return root


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
