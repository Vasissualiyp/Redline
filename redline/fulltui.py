"""`redline full`: every paper in one full-screen list (Textual).

enter/o open the paper's tmux workspace   n new paper (create, or pull from Overleaf)
p pull from Overleaf   P push to Overleaf   c compile   r refresh   q quit
Accepting and rejecting changes stays inside a paper's own workspace.
"""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path

from rich.markup import escape
from rich.segment import Segment
from rich.style import Style
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import DataTable, Footer, Header, Input, Label, OptionList, RichLog, Static

from . import kittygfx, overleaf, registry, thumb
from .model import RedlineError

SYNC_STYLE = {"in sync": "green", "local edits": "yellow", "no base": "red"}


def _cell(n: int | None, warn: bool = False) -> str:
    if n is None:
        return "?"
    return f"[b yellow]{n}[/]" if n and warn else str(n)


class _DriverOut:
    """Where kitty graphics escapes go: through Textual's own output, so they stay in order."""

    def __init__(self, app: App):
        self.app = app

    def write(self, data: bytes) -> None:
        drv = getattr(self.app, "_driver", None)
        if drv is not None:
            drv.write(data.decode("latin-1"))

    def flush(self) -> None:
        pass


class PagePreview(Widget):
    """First page of the selected paper, drawn with kitty graphics (unicode placeholders)."""

    DEFAULT_CSS = "PagePreview { height: 1fr; }"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.gfx: kittygfx.Kitty | None = None
        self.pdf: Path | None = None
        self.note = ""
        self.placed: str | None = None
        self.ticket = 0

    def graphics_ok(self) -> bool:
        term = os.environ.get("TERM", "")
        return bool(os.environ.get("REDLINE_PREVIEW", "1") != "0"
                    and ("kitty" in term or os.environ.get("KITTY_WINDOW_ID") or os.environ.get("TMUX")))

    def show_pdf(self, pdf: Path | None, why: str = "") -> None:
        self.pdf, self.note = pdf, why
        self.placed = None
        self.refresh()
        if pdf and self.graphics_ok():
            self.ticket += 1
            self.draw(self.ticket, pdf)

    def on_resize(self) -> None:
        if self.pdf and self.graphics_ok():
            self.ticket += 1
            self.draw(self.ticket, self.pdf)

    @work(thread=True, exclusive=True, group="thumb")
    def draw(self, ticket: int, pdf: Path) -> None:
        import time
        time.sleep(0.15)  # scrolling through the list: only the paper you stop on is drawn
        if ticket != self.ticket:
            return
        cols, rows = self.size.width, self.size.height
        if cols < 4 or rows < 3:
            return
        cw, ch = thumb.cell_px()
        key = f"{pdf}:{pdf.stat().st_mtime_ns}:{cols}x{rows}"
        if self.gfx and self.gfx.has(key):
            self.app.call_from_thread(self.place, ticket, key)
            return
        img = thumb.first_page(pdf, cols * cw)
        if img is None:
            self.app.call_from_thread(self.nothing, ticket, "cannot render the PDF")
            return
        c, r = thumb.fit(img.width / img.height, cols, rows, cw, ch)
        img = img.resize((c * cw, r * ch))
        png = kittygfx.encode_png(img)
        self.app.call_from_thread(self.send, ticket, key, png, c, r)

    def send(self, ticket: int, key: str, png: bytes, c: int, r: int) -> None:
        if self.gfx is None:
            self.gfx = kittygfx.Kitty(out=_DriverOut(self.app), tmux=bool(os.environ.get("TMUX")))
        if ticket != self.ticket:
            return
        self.gfx.send(key, png, c, r)
        self.place(ticket, key)

    def place(self, ticket: int, key: str) -> None:
        if ticket == self.ticket:
            self.placed = key
            self.refresh()

    def nothing(self, ticket: int, why: str) -> None:
        if ticket == self.ticket:
            self.note = why
            self.refresh()

    def render_line(self, y: int) -> Strip:
        w = self.size.width
        if self.placed and self.gfx and self.placed in self.gfx.images:
            iid, cols, rows = self.gfx.images[self.placed]
            if y < rows:
                r, g, b = (iid >> 16) & 255, (iid >> 8) & 255, iid & 255
                text = (kittygfx.PLACEHOLDER + chr(kittygfx.DIACRITICS[y]) + chr(kittygfx.DIACRITICS[0])
                        + kittygfx.PLACEHOLDER * (cols - 1))
                return Strip([Segment(text, Style.parse(f"rgb({r},{g},{b})")), Segment(" " * (w - cols))])
            return Strip.blank(w)
        if y == 0:
            msg = self.note or ("no PDF yet: press c to compile" if not self.pdf else
                                "page preview needs a kitty terminal")
            return Strip([Segment(msg[:w].ljust(w), Style(dim=True))])
        return Strip.blank(w)

    def forget(self) -> None:
        if self.gfx:
            self.gfx.forget()


class NewPaper(ModalScreen):
    """Type a name to create a paper (and its Overleaf project), or pick an Overleaf project to pull."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
    CSS = """
    NewPaper { align: center middle; }
    #box { width: 80; height: 80%; border: round $accent; background: $surface; padding: 1 2; }
    #box OptionList { height: 1fr; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("[b]New paper[/]  type a name and press enter to [b]create[/] it on Overleaf and here;\n"
                        "or choose an existing Overleaf project below to [b]pull[/] it.")
            yield Input(placeholder="New paper name (typing also filters the list)", id="name")
            yield OptionList(id="remote")
            yield Label("esc cancels", id="hint")

    def on_mount(self) -> None:
        self.remote: list[dict] = []
        self.query_one("#remote", OptionList).add_option("loading the Overleaf project list...")
        self.query_one("#remote").disabled = True
        self.fetch()

    @work(thread=True)
    def fetch(self) -> None:
        try:
            self.remote = overleaf.projects()
            err = ""
        except RedlineError as e:
            self.remote, err = [], str(e)
        self.app.call_from_thread(self.show, err)

    def show(self, err: str = "") -> None:
        lst = self.query_one("#remote", OptionList)
        lst.disabled = False
        lst.clear_options()
        local = {p.name for p in registry.scan()}
        want = self.query_one("#name", Input).value.strip().lower()
        self.shown = [p for p in self.remote if want in p["name"].lower()]
        for p in self.shown:
            mark = "  [dim](already here)[/]" if p["name"] in local else ""
            name = p["name"] if len(p["name"]) <= 44 else p["name"][:43] + "…"
            lst.add_option(f"{escape(name):44}  [dim]{overleaf.pretty(p['updated'])}[/]{mark}")
        if err:
            lst.add_option(f"[red]{escape(err)}[/]")
        if self.shown:
            lst.highlighted = 0

    def on_input_changed(self, ev: Input.Changed) -> None:
        if self.remote:
            self.show()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        name = ev.value.strip()
        if name:
            self.dismiss(("new", name))

    def on_option_list_option_selected(self, ev: OptionList.OptionSelected) -> None:
        if ev.option_index < len(getattr(self, "shown", [])):
            self.dismiss(("pull", self.shown[ev.option_index]["name"]))


class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]
    CSS = "Confirm { align: center middle; } #ask { width: 70; border: round $warning; background: $surface; padding: 1 2; }"

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        yield Label(self.text + "\n\n[b]y[/] yes   [b]n[/] no", id="ask")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class Full(App):
    TITLE = "Redline: all papers"
    BINDINGS = [
        Binding("o", "open", "Open"),
        Binding("n", "new", "New"),
        Binding("p", "pull", "Pull"),
        Binding("P", "push", "Push"),
        Binding("c", "compile", "Compile"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]
    CSS = """
    #papers { width: 3fr; }
    #side { width: 2fr; border-left: solid $primary; padding: 0 1; }
    #detail { height: 8; }
    #log { height: 9; border-top: solid $primary; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, registry.Info] = {}
        self.open_root: Path | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="papers", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                yield Static(id="detail")
                yield PagePreview(id="page")
        yield RichLog(id="log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#papers", DataTable)
        t.add_columns("Paper", "Pending", "Unrated", "Overleaf", "tmux", "Active")
        t.focus()
        self.action_refresh()

    # ---- the list ----------------------------------------------------------------
    def action_refresh(self) -> None:
        self.load()

    @work(thread=True, exclusive=True, group="load")
    def load(self) -> None:
        rows = registry.listing()
        self.call_from_thread(self.fill, rows)

    def fill(self, rows: list[registry.Info]) -> None:
        t = self.query_one("#papers", DataTable)
        keep = self.current_path()
        t.clear()
        self.rows = {}
        for it in rows:
            self.rows[str(it.path)] = it
            sync = f"[{SYNC_STYLE.get(it.sync, 'red')}]{it.sync or 'error'}[/]"
            t.add_row(escape(it.name), _cell(it.pending), _cell(it.unrated, warn=True), sync,
                      "[green]running[/]" if it.running else "[dim]-[/]", registry.ago(it.active),
                      key=str(it.path))
        if keep and keep in self.rows:
            t.move_cursor(row=t.get_row_index(keep))
        self.show_detail()

    def current_path(self) -> str | None:
        t = self.query_one("#papers", DataTable)
        if not t.row_count:
            return None
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None

    def selected(self) -> registry.Info | None:
        p = self.current_path()
        return self.rows.get(p) if p else None

    def on_data_table_row_highlighted(self, ev: DataTable.RowHighlighted) -> None:
        self.show_detail()

    def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:  # enter
        self.action_open()

    def show_detail(self) -> None:
        it = self.selected()
        side = self.query_one("#detail", Static)
        page = self.query_one("#page", PagePreview)
        if not it:
            page.show_pdf(None, "")
            side.update("No papers under " + ", ".join(str(r) for r in registry.roots())
                        + "\n\nPress [b]n[/] to make one.")
            return
        lines = [f"[b]{escape(it.name)}[/]", f"[dim]{escape(str(it.path))}[/]", ""]
        if it.error:
            lines.append(f"[red]{escape(it.error)}[/]")
        else:
            lines += [f"pending changes   {it.pending}", f"without a marker  {it.unrated}",
                      f"Overleaf          {it.sync}", f"tmux session      {registry.session_name(it.path)}"
                      + (" (running)" if it.running else ""), f"last edit         {registry.ago(it.active)}"]
        side.update("\n".join(lines))
        pdf = None
        if not it.error:
            from .state import Project
            try:
                pdf = thumb.paper_pdf(Project.open(it.path))
            except Exception:
                pdf = None
        if pdf is None or pdf != page.pdf:
            page.show_pdf(pdf)

    # ---- actions -----------------------------------------------------------------
    def say(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def action_quit(self) -> None:
        self.query_one("#page", PagePreview).forget()
        self.exit()

    def action_open(self) -> None:
        it = self.selected()
        if not it:
            return
        if os.environ.get("TMUX"):
            self.say(f"opening {it.name} in tmux...")
            self.attach(it.path)
        else:
            self.open_root = it.path
            self.action_quit()

    @work(thread=True)
    def attach(self, root: Path) -> None:
        from .launcher import open_workspace
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                open_workspace(root, attach=True)
            self.call_from_thread(self.say, f"switched to {registry.session_name(root)}")
        except Exception as e:
            self.call_from_thread(self.say, f"cannot open: {e}")
        self.call_from_thread(self.action_refresh)

    def action_new(self) -> None:
        def done(res) -> None:
            if res:
                self.make(*res)
        self.push_screen(NewPaper(), done)

    @work(thread=True, group="job")
    def make(self, how: str, name: str) -> None:
        say = lambda s: self.call_from_thread(self.say, s)  # noqa: E731
        say(f"{'creating' if how == 'new' else 'pulling'} {name!r} ...")
        try:
            dest = (registry.create_blank if how == "new" else registry.pull_existing)(name)
            say(f"ready: {dest}")
        except RedlineError as e:
            say(f"failed: {e}")
        self.call_from_thread(self.action_refresh)

    def _job(self, label: str, fn) -> None:
        it = self.selected()
        if not it:
            return
        self.run_job(label, it.path, fn)

    @work(thread=True, group="job")
    def run_job(self, label: str, root: Path, fn) -> None:
        say = lambda s: self.call_from_thread(self.say, s)  # noqa: E731
        say(f"{label} {root.name} ...")
        try:
            from .state import Project
            say(fn(Project.open(root)))
        except RedlineError as e:
            say(f"{label} failed: {e}")
        self.call_from_thread(self.action_refresh)

    def action_pull(self) -> None:
        from . import sync
        self._job("pull", lambda proj: sync.pull(proj).text())

    def action_push(self) -> None:
        it = self.selected()
        if not it:
            return

        def go(yes: bool | None) -> None:
            if yes:
                from . import sync
                self._job("push", lambda proj: sync.push(proj).text())
        self.push_screen(Confirm(f"Push [b]{escape(it.name)}[/] to Overleaf?"), go)

    def action_compile(self) -> None:
        from . import compile as C
        self._job("compile", lambda proj: C.format_report(C.run(proj)))


def run() -> int:
    app = Full()
    app.run()
    if app.open_root:
        from .launcher import open_workspace
        return open_workspace(app.open_root, attach=True)
    return 0
