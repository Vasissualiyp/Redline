"""`redline full`: every paper in one full-screen list (Textual), with vim motions.

Papers:   j k gg G ctrl-d ctrl-u ctrl-e ctrl-y H M L move, / filters, l reviews the paper's changes,
          enter/o opens its tmux workspace, n new paper, p pull, P push, c compile, r refresh, q quit
Changes:  the same motions; a accept, x reject, n note for the agent (stays pending as %N),
          d ask the agent, u undo, enter opens the editor at the change, c compile, h back
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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import ContentSwitcher
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
    """A picture drawn with kitty graphics (unicode placeholders): a paper's first page, or the
    PDF page holding a change. `show_job` takes a key (same key = same picture) and a function
    (pixel width, pixel height) -> PIL image, or a string saying why there is none."""

    DEFAULT_CSS = "PagePreview { height: 1fr; }"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.gfx: kittygfx.Kitty | None = None
        self.job: tuple[str, object] | None = None
        self.note = ""
        self.placed: str | None = None
        self.ticket = 0

    def graphics_ok(self) -> bool:
        term = os.environ.get("TERM", "")
        return bool(os.environ.get("REDLINE_PREVIEW", "1") != "0"
                    and ("kitty" in term or os.environ.get("KITTY_WINDOW_ID") or os.environ.get("TMUX")))

    def clear(self, why: str = "") -> None:
        self.job, self.note, self.placed = None, why, None
        self.ticket += 1
        self.refresh()

    def show_job(self, key: str, fn) -> None:
        if self.job and self.job[0] == key and self.placed:
            return
        self.job, self.note, self.placed = (key, fn), "", None
        self.refresh()
        if self.graphics_ok():
            self.ticket += 1
            self.draw(self.ticket, key, fn)

    def show_pdf(self, pdf: Path | None, why: str = "") -> None:
        if pdf is None:
            return self.clear(why or "no PDF yet: press c to compile")
        self.show_job(f"{pdf}:{pdf.stat().st_mtime_ns}", lambda w, h: thumb.first_page(pdf, w) or "cannot render the PDF")

    def on_resize(self) -> None:
        if self.job and self.graphics_ok():
            self.ticket += 1
            self.draw(self.ticket, *self.job)

    @work(thread=True, exclusive=True, group="thumb")
    def draw(self, ticket: int, key0: str, fn) -> None:
        import time
        time.sleep(0.15)  # scrolling through a list: only the item you stop on is drawn
        if ticket != self.ticket:
            return
        cols, rows = self.size.width, self.size.height
        if cols < 4 or rows < 3:
            return
        cw, ch = thumb.cell_px()
        key = f"{key0}:{cols}x{rows}"
        if self.gfx and self.gfx.has(key):
            self.app.call_from_thread(self.place, ticket, key)
            return
        try:
            img = fn(cols * cw, rows * ch)
        except Exception as e:  # a broken PDF must not take the list down
            img = f"preview failed: {e}"
        if isinstance(img, str) or img is None:
            self.app.call_from_thread(self.nothing, ticket, img or "cannot render the PDF")
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
            msg = self.note or ("page preview needs a kitty terminal" if self.job else "")
            return Strip([Segment(msg[:w].ljust(w), Style(dim=True))])
        return Strip.blank(w)

    def forget(self) -> None:
        if self.gfx:
            self.gfx.forget()


class VimTable(DataTable):
    """A DataTable with vim motions: j k gg G ctrl-d ctrl-u ctrl-e ctrl-y H M L."""

    BINDINGS = [
        Binding("j", "cursor_down", show=False), Binding("k", "cursor_up", show=False),
        Binding("G", "last", show=False), Binding("g", "g", show=False),
        Binding("ctrl+d", "half(1)", show=False), Binding("ctrl+u", "half(-1)", show=False),
        Binding("ctrl+e", "scroll_line(1)", show=False), Binding("ctrl+y", "scroll_line(-1)", show=False),
        Binding("H", "screen_row('top')", show=False), Binding("M", "screen_row('middle')", show=False),
        Binding("L", "screen_row('bottom')", show=False),
    ]
    _g = 0.0

    def _row(self, row: int) -> None:
        if self.row_count:
            self.move_cursor(row=max(0, min(self.row_count - 1, row)))

    def action_g(self) -> None:
        import time
        if time.monotonic() - self._g < 0.8:
            self._g = 0.0
            self._row(0)
        else:
            self._g = time.monotonic()

    def action_last(self) -> None:
        self._row(self.row_count - 1)

    def _page(self) -> int:
        return max(1, self.size.height - (1 if self.show_header else 0))

    def action_half(self, sign: int) -> None:
        self._row(self.cursor_row + sign * max(1, self._page() // 2))

    def _top(self) -> int:
        return max(0, int(self.scroll_y))

    def action_scroll_line(self, sign: int) -> None:
        """Scroll the view one line; the cursor moves only if it would leave the screen."""
        top = max(0, min(self.row_count - 1, self._top() + sign))
        self.scroll_to(y=top, animate=False)
        bottom = top + self._page() - 1
        if self.cursor_row < top:
            self._row(top)
        elif self.cursor_row > bottom:
            self._row(bottom)

    def action_screen_row(self, where: str) -> None:
        top = self._top()
        last = min(self.row_count - 1, top + self._page() - 1)
        self._row({"top": top, "bottom": last, "middle": (top + last) // 2}[where])


class NewPaper(ModalScreen):
    """Pull an Overleaf project (type to filter, enter pulls the highlighted one), or create a new one."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel"),
                Binding("down,ctrl+n,ctrl+j", "move(1)", show=False), Binding("up,ctrl+p,ctrl+k", "move(-1)", show=False),
                Binding("ctrl+o", "create", "Create new")]
    CSS = """
    NewPaper { align: center middle; }
    #box { width: 90; height: 80%; border: round $accent; background: $surface; padding: 1 2; }
    #box OptionList { height: 1fr; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("[b]Pull from Overleaf[/]: type to filter, [b]enter[/] pulls the highlighted project (up/down move).\n"
                        "[b]ctrl+o[/] creates a [b]new[/] Overleaf project named like the text you typed (asks first).")
            yield Input(placeholder="Overleaf project name", id="name")
            yield OptionList(id="remote")
            yield Label("esc cancels", id="hint")

    def on_mount(self) -> None:
        self.remote: list[dict] = []
        self.shown: list[dict] = []
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
        elif not self.shown and want:
            lst.add_option(f"[dim]no Overleaf project matches; ctrl+o creates {escape(repr(want))}[/]")
        if self.shown:
            lst.highlighted = 0

    def on_input_changed(self, ev: Input.Changed) -> None:
        if self.remote:
            self.show()

    def action_move(self, step: int) -> None:
        lst = self.query_one("#remote", OptionList)
        if self.shown:
            lst.highlighted = max(0, min(len(self.shown) - 1, (lst.highlighted or 0) + step))

    def _pull(self) -> None:
        lst = self.query_one("#remote", OptionList)
        i = lst.highlighted
        if self.shown and i is not None and i < len(self.shown):
            self.dismiss(("pull", self.shown[i]["name"]))

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        self._pull()

    def on_option_list_option_selected(self, ev: OptionList.OptionSelected) -> None:
        self._pull()

    def action_create(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            return
        exact = [p for p in self.remote if p["name"] == name]
        if exact:
            return self.dismiss(("pull", name))  # it exists: never create a twin

        def go(yes: bool | None) -> None:
            if yes:
                self.dismiss(("new", name))
        self.app.push_screen(Confirm(f"Create a NEW Overleaf project called [b]{escape(name)}[/]?"), go)


class Say(ModalScreen):
    """One line of text for the agent."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
    CSS = "Say { align: center middle; } #sbox { width: 90; border: round $accent; background: $surface; padding: 1 2; }"

    def __init__(self, title: str):
        super().__init__()
        self.title_text = title

    def compose(self) -> ComposeResult:
        with Vertical(id="sbox"):
            yield Label(self.title_text)
            yield Input(id="say")

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value.strip() or None)


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
        Binding("l", "enter", "Changes"),
        Binding("h", "back", "Back"),
        Binding("slash", "search", "Search"),
        Binding("escape", "escape", show=False),
        Binding("n", "key_n", "New / note"),
        Binding("a", "accept", "Accept"),
        Binding("x", "reject", "Reject"),
        Binding("d", "discuss", "Ask AI"),
        Binding("t", "chat", "Talk to AI"),
        Binding("v", "toggle_view", "Page/Text"),
        Binding("u", "undo", "Undo"),
        Binding("p", "pull", "Pull"),
        Binding("P", "push", "Push"),
        Binding("c", "compile", "Compile"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]
    CSS = """
    #papers, #changes { width: 3fr; }
    .side { width: 2fr; border-left: solid $primary; padding: 0 1; }
    #detail { height: 8; }
    #cdetail { height: 12; overflow-y: auto; }
    #log { height: 7; border-top: solid $primary; }
    #search { display: none; dock: bottom; }
    #cscroll { display: none; height: 1fr; }
    #changes-view.textmode #changes { width: 2fr; }
    #changes-view.textmode .side { width: 3fr; }
    """
    PAPER_ONLY = {"open", "pull", "push"}
    CHANGES_ONLY = {"accept", "reject", "discuss", "undo", "toggle_view"}

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, registry.Info] = {}
        self.all_papers: list[registry.Info] = []
        self.changes: list[dict] = []
        self.paper: Path | None = None
        self.qpane = None
        self.level = "papers"
        self.queries = {"papers": "", "changes": ""}
        self.open_root: Path | None = None
        self.qdoc = None
        self.text_view = False        # source context instead of the PDF page (no kitty graphics needed)
        self.seen_mtime = 0.0         # newest .tex mtime the change list was built from
        self.compiled_for = 0.0       # ... and the one the last compile ran on
        self.compiling = False
        self.compile_timer = None

    def compose(self) -> ComposeResult:
        yield Header()
        with ContentSwitcher(initial="papers-view"):
            with Horizontal(id="papers-view"):
                yield VimTable(id="papers", cursor_type="row", zebra_stripes=True)
                with Vertical(classes="side"):
                    yield Static(id="detail")
                    yield PagePreview(id="page")
            with Horizontal(id="changes-view"):
                yield VimTable(id="changes", cursor_type="row", zebra_stripes=True)
                with Vertical(classes="side"):
                    yield Static(id="cdetail")
                    yield PagePreview(id="cpage")
                    with VerticalScroll(id="cscroll"):
                        yield Static(id="cctx")
        yield Input(placeholder="/ search (enter keeps the filter, esc clears)", id="search")
        yield RichLog(id="log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#papers", DataTable).add_columns("Paper", "Pending", "Unrated", "Overleaf", "tmux", "Active")
        self.query_one("#changes", DataTable).add_columns("#", "Kind", "Section", "Line", "Mark", "Text")
        self.query_one("#papers").focus()
        self.text_view = not self.query_one("#cpage", PagePreview).graphics_ok()
        self.apply_view()
        self.set_interval(4.0, self.watch)
        self.action_refresh()

    # ---- levels --------------------------------------------------------------------
    def table(self) -> VimTable:
        return self.query_one("#papers" if self.level == "papers" else "#changes", VimTable)

    def check_action(self, action: str, parameters) -> bool | None:
        if self.level == "papers" and action in self.CHANGES_ONLY:
            return False
        if self.level == "changes" and action in self.PAPER_ONLY - {"open"}:
            return False
        return True

    def action_enter(self) -> None:  # l
        if self.level != "papers":
            return
        it = self.selected()
        if not it or it.error:
            return self.say("this paper cannot be read" + (f": {it.error}" if it and it.error else ""))
        self.paper = it.path
        self.level = "changes"
        self.query_one(ContentSwitcher).current = "changes-view"
        self.set_query("changes", "")
        self.changes = []
        self.fill_changes()
        self.query_one("#changes").focus()
        self.refresh_bindings()
        self.load_changes()
        self.maybe_compile_on_entry()

    @work(thread=True, group="entry")
    def maybe_compile_on_entry(self) -> None:
        from . import quick
        from .state import Project
        try:
            if quick.stale(Project.open(self.paper)) and self.newest_tex() != self.compiled_for:
                self.call_from_thread(self.say, "the PDF is older than the source: rebuilding it")
                self.call_from_thread(self.compile_paper, self.paper)
        except RedlineError:
            pass

    def action_back(self) -> None:  # h
        if self.level == "changes":
            self.level = "papers"
            self.query_one(ContentSwitcher).current = "papers-view"
            self.query_one("#papers").focus()
            self.refresh_bindings()
            self.action_refresh()

    def action_escape(self) -> None:
        search = self.query_one("#search", Input)
        if search.has_focus:
            self.set_query(self.level, "")
            self.table().focus()
        elif self.queries[self.level]:
            self.set_query(self.level, "")
        else:
            self.action_back()

    def action_key_n(self) -> None:
        self.action_new() if self.level == "papers" else self.action_note()

    def action_quit(self) -> None:
        if self.level == "changes":
            return self.action_back()
        self.quit_now()

    def quit_now(self) -> None:
        for w in self.query(PagePreview):
            w.forget()
        self.exit()

    # ---- search ----------------------------------------------------------------------
    def action_search(self) -> None:
        box = self.query_one("#search", Input)
        box.display = True
        box.value = self.queries[self.level]
        box.focus()

    def set_query(self, level: str, text: str) -> None:
        self.queries[level] = text
        box = self.query_one("#search", Input)
        if level == self.level:
            box.value = text
            box.display = bool(text) or box.has_focus
        self.refill(level)

    def on_input_changed(self, ev: Input.Changed) -> None:
        if ev.input.id == "search" and ev.value != self.queries[self.level]:
            self.queries[self.level] = ev.value
            self.refill(self.level)

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        if ev.input.id == "search":
            ev.input.display = bool(ev.value)
            self.table().focus()

    def refill(self, level: str) -> None:
        (self.fill if level == "papers" else self.fill_changes)()

    @staticmethod
    def matches(query: str, text: str) -> bool:
        return all(w in text.lower() for w in query.lower().split())

    # ---- the paper list --------------------------------------------------------------
    def action_refresh(self) -> None:
        if self.level == "changes":
            return self.load_changes()
        self.load()

    @work(thread=True, exclusive=True, group="load")
    def load(self) -> None:
        rows = registry.listing()
        self.call_from_thread(self.set_papers, rows)

    def set_papers(self, rows: list[registry.Info]) -> None:
        self.all_papers = rows
        self.fill()

    def fill(self) -> None:
        t = self.query_one("#papers", VimTable)
        keep = self.current_path()
        t.clear()
        self.rows = {}
        for it in self.all_papers:
            if not self.matches(self.queries["papers"], f"{it.name} {it.sync} {it.path}"):
                continue
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
        if ev.data_table.id == "papers":
            self.show_detail()
        else:
            self.show_change()

    def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:  # enter
        self.action_open()

    def show_detail(self) -> None:
        it = self.selected()
        side = self.query_one("#detail", Static)
        page = self.query_one("#page", PagePreview)
        if not it:
            page.clear()
            side.update("No papers under " + ", ".join(str(r) for r in registry.roots())
                        + "\n\nPress [b]n[/] to make one.")
            return
        lines = [f"[b]{escape(it.name)}[/]", f"[dim]{escape(str(it.path))}[/]", ""]
        if it.error:
            lines.append(f"[red]{escape(it.error)}[/]")
        else:
            lines += [f"pending changes   {it.pending}", f"without a marker  {it.unrated}",
                      f"Overleaf          {it.sync}", f"tmux session      {registry.session_name(it.path)}"
                      + (" (running)" if it.running else ""), f"last edit         {registry.ago(it.active)}"
                      + "   [dim]l: review its changes[/]"]
        side.update("\n".join(lines))
        pdf = None
        if not it.error:
            from .state import Project
            try:
                pdf = thumb.paper_pdf(Project.open(it.path))
            except Exception:
                pdf = None
        page.show_pdf(pdf)

    # ---- the quick menu: one paper's changes ---------------------------------------
    @work(thread=True, exclusive=True, group="changes")
    def load_changes(self) -> None:
        from . import quick
        from .state import Project
        try:
            proj = Project.open(self.paper)
            rows, doc = quick.load(proj)
            self.qdoc = doc
            self.seen_mtime = self.newest_tex()
            if self.qpane is None or self.qpane.proj.cfg.root != self.paper:
                self.qpane = quick.change_pane(proj)
        except RedlineError as e:
            self.call_from_thread(self.say, f"cannot read the paper: {e}")
            return
        self.call_from_thread(self.set_changes, rows)

    def set_changes(self, rows: list[dict]) -> None:
        self.changes = rows
        self.fill_changes()

    def fill_changes(self) -> None:
        t = self.query_one("#changes", VimTable)
        keep = self.current_change_id()
        row_of = {}
        t.clear()
        for d in self.changes:
            text = " ".join((d.get("new") or d.get("old") or d.get("comment") or "").split())
            mark = ("%" + d["marker"]["kind"]) if d["marker"] else ""
            if not self.matches(self.queries["changes"], f"{d['label']} {d['kind']} {d['section']} {mark} {text} "
                                                         f"{d.get('comment') or ''} {d['origin']}"):
                continue
            kind = {"added": "[green]+ added[/]", "deleted": "[red]- deleted[/]", "replaced": "[yellow]~ replaced[/]",
                    "comment": "[cyan]? comment[/]", "highlight": "highlight"}.get(d["kind"], d["kind"])
            t.add_row(d["label"], kind, escape(d["section"][:28]), str(d["lines"][0]),
                      f"[b yellow]{mark}[/]" if mark else "", escape(text[:90]), key=d["id"])
            row_of[d["id"]] = t.row_count - 1
        if keep in row_of:
            t.move_cursor(row=row_of[keep])
        self.show_change()

    def current_change_id(self) -> str | None:
        t = self.query_one("#changes", DataTable)
        if not t.row_count:
            return None
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None

    def change(self) -> dict | None:
        cid = self.current_change_id()
        return next((d for d in self.changes if d["id"] == cid), None)

    def show_change(self) -> None:
        if self.level != "changes" and not self.changes:
            return
        d = self.change()
        side = self.query_one("#cdetail", Static)
        page = self.query_one("#cpage", PagePreview)
        name = escape(self.paper.name) if self.paper else ""
        if not d:
            side.update(f"[b]{name}[/]\n\n" + ("nothing matches" if self.queries["changes"] else
                                                 "No pending changes. [dim]h: back[/]"))
            page.clear()
            return
        head = (f"[b]{d['label']}[/] {d['kind']}  §{escape(d['section'])}  l.{d['lines'][0]}"
                + (f"-{d['lines'][1]}" if d['lines'][1] != d['lines'][0] else "") + f"  [dim]{d['origin']}[/]")
        if d["marker"]:
            head += f"  [b yellow]%{d['marker']['kind']}[/] {escape(d['marker']['note'])}"
        body = [head, ""]
        if d["old"] is not None:
            body.append("[red strike]" + escape(" ".join(d["old"].split())) + "[/]")
        if d["new"] is not None:
            body.append("[green]" + escape(" ".join(d["new"].split())) + "[/]")
        if d["comment"] is not None:
            body.append("[cyan]" + escape(" ".join(d["comment"].split())) + "[/]")
        from . import quick
        if self.text_view:
            if self.qdoc is not None:
                self.query_one("#cctx", Static).update(quick.context(self.qdoc, d["id"]))
            side.update("\n".join(body))
            return
        side.update("\n".join(body))
        pane, pdfm = self.qpane, 0
        if pane is None:
            return
        with contextlib.suppress(OSError):
            pdfm = pane.pdf.stat().st_mtime_ns
        page.show_job(f"chg:{self.paper}:{d['id']}:{pdfm}", lambda w, h, d=d, pane=pane: quick.change_image(pane, d, w, h))

    # ---- the view: PDF page or source text -------------------------------------------
    def apply_view(self) -> None:
        view = self.query_one("#changes-view")
        view.set_class(self.text_view, "textmode")
        self.query_one("#cpage").display = not self.text_view
        self.query_one("#cscroll").display = self.text_view

    def action_toggle_view(self) -> None:
        self.text_view = not self.text_view
        self.apply_view()
        self.say("showing the " + ("source text around the change (colours: green added, red deleted, cyan comment)"
                                   if self.text_view else "PDF page of the change"))
        self.show_change()

    def newest_tex(self) -> float:
        if not self.paper:
            return 0.0
        return max((f.stat().st_mtime for f in self.paper.glob("*.tex")), default=0.0)

    def watch(self) -> None:
        """Edits that arrive from outside (the agent, nvim): reload the list, rebuild the PDF."""
        if self.level != "changes" or not self.paper:
            return
        m = self.newest_tex()
        if m != self.seen_mtime:
            self.seen_mtime = m
            self.load_changes()
            self.schedule_compile()

    def schedule_compile(self, delay: float = 10.0) -> None:
        """Rebuild the PDF once the paper has been quiet for a while (as in the editor)."""
        if self.compile_timer is not None:
            self.compile_timer.stop()
        self.compile_timer = self.set_timer(delay, self.auto_compile)

    def auto_compile(self) -> None:
        if self.level == "changes" and self.paper and not self.compiling \
                and self.newest_tex() != self.compiled_for:
            self.compile_paper(self.paper)

    @work(thread=True, group="compile")
    def compile_paper(self, root: Path) -> None:
        from . import compile as C
        from .state import Project
        self.compiling = True
        say = lambda s: self.call_from_thread(self.say, s)  # noqa: E731
        try:
            proj = Project.open(root)
            self.compiled_for = self.newest_tex()
            say(f"compiling {root.name} ...")
            res = C.run(proj)
            if res.ok:
                say(f"compiled in {res.seconds:.0f}s")
            else:
                first = res.errors[0] if res.errors else None
                say(f"compile FAILED: {len(res.errors)} error(s)"
                    + (f"; first: l.{first.line} {first.message[:80]}" if first else "")
                    + "  (the PDF stays at the last clean build)")
        except RedlineError as e:
            say(f"compile failed: {e}")
        finally:
            self.compiling = False
        self.call_from_thread(self.show_change)

    # ---- deciding ----------------------------------------------------------------
    def _decide(self, verb: str, fn) -> None:
        d = self.change()
        if not d or not self.paper:
            return
        self.decide(verb, self.paper, d, fn)

    @work(thread=True, group="decide")
    def decide(self, verb: str, root: Path, d: dict, fn) -> None:
        from .state import Project
        try:
            fn(Project.open(root), d["id"])
            self.call_from_thread(self.say, f"{verb} {d['label']} ({d['kind']}, l.{d['lines'][0]})   u undoes")
            self.call_from_thread(self.schedule_compile)
        except RedlineError as e:
            self.call_from_thread(self.say, f"{verb} failed: {e}")
        self.call_from_thread(self.load_changes)

    def action_accept(self) -> None:
        from . import quick
        self._decide("accepted", quick.accept)

    def action_reject(self) -> None:
        from . import quick
        self._decide("rejected", quick.reject)

    @work(thread=True, group="decide")
    def action_undo(self) -> None:
        from . import quick
        from .state import Project
        try:
            self.call_from_thread(self.say, quick.undo(Project.open(self.paper)))
            self.call_from_thread(self.schedule_compile)
        except RedlineError as e:
            self.call_from_thread(self.say, f"undo failed: {e}")
        self.call_from_thread(self.load_changes)

    # ---- talking to the agent --------------------------------------------------------
    def action_note(self) -> None:
        self.talk("note")

    def action_discuss(self) -> None:
        self.talk("ask")

    def action_chat(self) -> None:
        self.talk("chat")

    def talk(self, how: str) -> None:
        root = self.paper if self.level == "changes" else (self.selected().path if self.selected() else None)
        d = self.change() if how != "chat" and self.level == "changes" else None
        if not root or (how != "chat" and not d):
            return
        from . import quick
        if not quick.alive(root):
            self.say(f"starting {quick.session(root)} in the background while you write ...")
            self.start_session(root)
        title = {"note": f"Note on {d['label']} (stays pending as %N; the agent revises it):" if d else "",
                 "ask": f"Ask the agent about {d['label']} (nothing is edited):" if d else "",
                 "chat": f"Talk to the agent about {registry.session_name(root)[8:]} (nothing is edited unless you ask):"}[how]

        def done(text) -> None:
            if text:
                self.send(how, root, d, text)
        self.push_screen(Say(title), done)

    @work(thread=True, group="session")
    def start_session(self, root: Path) -> None:
        from . import quick
        try:
            quick.ensure_session(root)
            self.call_from_thread(self.say, "background session is up")
        except RedlineError as e:
            self.call_from_thread(self.say, f"session failed: {e}")

    @work(thread=True, group="send")
    def send(self, how: str, root: Path, d: dict, text: str) -> None:
        from . import quick
        from .state import Project
        say = lambda s: self.call_from_thread(self.say, s)  # noqa: E731
        try:
            if how == "note":
                quick.mark_note(Project.open(root), d["id"], text)
                msg = quick.note_message(d, text)
            elif how == "ask":
                msg = quick.ask_message(d, text)
            else:
                msg = quick.chat_message(Project.open(root).cfg.project_name, text)
            say("waiting for the agent ..." if not quick.alive(root) else "sending ...")
            name = quick.send_to_agent(root, msg)
            say(f"sent to the agent in {name}; read the answer there (enter opens it)")
        except RedlineError as e:
            say(f"not sent: {e}")
        except Exception as e:
            say(f"not sent: {e}")
        self.call_from_thread(self.load_changes)

    # ---- the rest ----------------------------------------------------------------
    def say(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def action_open(self) -> None:
        root = self.paper if self.level == "changes" else (self.selected().path if self.selected() else None)
        if not root:
            return
        line = (self.change() or {}).get("lines", [0])[0] if self.level == "changes" else 0
        if os.environ.get("TMUX"):
            self.say(f"opening {root.name} in tmux...")
            self.attach(root, line)
        else:
            self.open_root = root
            self.open_line = line
            self.quit_now()

    @work(thread=True)
    def attach(self, root: Path, line: int = 0) -> None:
        from .launcher import open_workspace
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                open_workspace(root, attach=False)
            if line:
                from . import quick
                quick.goto(root, line)
            from .launcher import _tmux
            _tmux("switch-client", "-t", f"={registry.session_name(root)}")
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
        root = self.paper if self.level == "changes" else (self.selected().path if self.selected() else None)
        if root:
            self.run_job(label, root, fn)

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
        if self.level == "changes" and self.paper:
            self.compiled_for = 0.0
            return self.compile_paper(self.paper)
        from . import compile as C
        self._job("compile", lambda proj: C.format_report(C.run(proj)))


def run() -> int:
    app = Full()
    app.run()
    if app.open_root:
        from .launcher import open_workspace
        rc = open_workspace(app.open_root, attach=False)
        if getattr(app, "open_line", 0):
            from . import quick
            quick.goto(app.open_root, app.open_line)
        import subprocess
        os.execvp("tmux", ["tmux", "attach-session", "-t", f"={registry.session_name(app.open_root)}"]) if not rc else None
        return rc
    return 0
