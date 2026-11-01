"""The evidence window: the source page with the quote highlighted, and the note.

`redline evidence show ID` opens a tmux window "evidence" next to the paper:
left the source PDF (the PDF pane's renderer, with the Zotero highlight drawn
as a highlighter), right the entry as text. q in either pane, or the nvim key
again, closes the window.

PDF pane keys: n/p next/previous source, ]/[ next/previous claim of the same
block of result lines (the note follows), o open the highlight in Zotero,
j/k pages, +/- zoom, z crop, q close.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import pdfmatch
from .evidence import Entry, Library, Source
from .model import RedlineError
from .pdfmatch import Rect
from .preview import Pane, page_height, run
from .state import Project
from .zotero import Zotero, ZoteroError

WINDOW = "evidence"


def zotero_rects(s: Source, pdf: Path) -> list[Rect]:
    """Zotero rects (origin bottom-left) as top-left Rects on 1-based pages."""
    if not s.rects:
        return []
    page = (s.page_index + 1) if s.page_index is not None else (s.page or 1)
    h = page_height(pdf, page)
    return [Rect(page, x1, h - y2, x2, h - y1) for x1, y1, x2, y2 in s.rects]


def block_ids(proj: Project, eid: str) -> list[str]:
    """Ids of the result lines stacked with `eid` (one block of % comment lines)."""
    from .parse import parse
    from .verify import results
    src = proj.read()
    doc = parse(src)
    by_line = {r.line: r for r in results(doc)}
    me = next((r for r in by_line.values() if r.id == eid), None)
    if me is None:
        return [eid]
    lines = src.splitlines()

    def comment(n: int) -> bool:
        return 1 <= n <= len(lines) and lines[n - 1].lstrip().startswith("%")
    a = me.line
    while comment(a - 1):
        a -= 1
    b = me.line
    while comment(b + 1):
        b += 1
    return [by_line[n].id for n in range(a, b + 1) if n in by_line and by_line[n].id]


class EvidencePane(Pane):
    def __init__(self, proj: Project, entry: Entry, zot: Zotero | None = None,
                 block: list[Entry] | None = None):
        super().__init__(proj, pdf=Path("/nonexistent.pdf"), tmp=proj.dir / "evidence-view")
        self.block = block or [entry]
        self.k = next((j for j, e in enumerate(self.block) if e.id == entry.id), 0)
        self.entry = entry
        self.zot = zot or Zotero()
        self.view.follow = False  # nothing to follow: no paper cursor here
        self.source_jump = False  # a source paper, not ours
        self.i = 0
        self.select(0)

    def claim(self, step: int) -> None:
        """Next/previous claim of the block; the note pane follows."""
        if len(self.block) < 2:
            self.view.status = "only one claim in this block"
            return
        self.k = (self.k + step) % len(self.block)
        self.entry = self.block[self.k]
        self.select(0)
        follow_note(self.proj, self.entry.id)

    def _keys(self) -> str:
        return ("]/[ claim  " if len(self.block) > 1 else "") + "n/p source  o Zotero  q close"

    def select(self, i: int) -> None:
        n = len(self.entry.sources)
        v = self.view
        v.rects, v.shown, v.zoom, v.crop = [], (), 1.0, False
        claim = f"claim {self.k + 1}/{len(self.block)} {self.entry.id}  " if len(self.block) > 1 else ""
        if not n:
            self.pdf = Path("/nonexistent.pdf")
            v.status = f"{claim}{self.entry.id}: no sources  {self._keys()}"
            return
        self.i = i % n
        s = self.entry.sources[self.i]
        head = f"{claim}[{self.i + 1}/{n}] {s.bibkey or 'code'}"
        if s.code:
            self.pdf = Path("/nonexistent.pdf")
            v.status = f"{head}: a code check (see the note)  {self._keys()}"
            return
        try:
            pdf = Path(self.zot.pdf_path(s.att_key)) if s.att_key else None
        except ZoteroError as e:
            pdf, v.status = None, f"{head}: {e}"
        if pdf is None:
            self.pdf = Path("/nonexistent.pdf")
            v.status = v.status or f"{head}: no PDF in Zotero for this source"
            return
        self.pdf, self.mtime = pdf, 0.0
        self.check_pdf()
        rects = zotero_rects(s, pdf)
        how = ""
        if not rects and s.quote and s.page:
            # no highlight in Zotero (yet): find the quote on the page ourselves
            words = [w for pg in (s.page, s.page + 1) if pg <= self.pages for w in self._words(pg)]
            rects = pdfmatch.locate(words, s.page, s.quote)
            how = " (located, no Zotero highlight)" if rects else " (quote not found on the page)"
        on = sorted({r.page for r in rects})
        v.rects = rects
        v.shown = (on[0], on[1]) if len(on) >= 2 and on[1] == on[0] + 1 else ()
        v.page = on[0] if on else (s.page or 1)
        v.status = f"{head} p.{s.page or v.page}{how}  {self._keys()}"

    def render(self) -> None:
        if self.pdf.exists():
            return super().render()
        sys.stdout.write("\x1b[2J\x1b[H")
        s = self.entry.sources[self.i] if self.entry.sources else None
        if s and s.code:
            sys.stdout.write(_code_snippet(self.proj, s.code))
        sys.stdout.flush()
        self._status(self.view.status)

    def handle_key(self, k: str) -> bool:
        if k in ("n", "\t"):
            self.select(self.i + 1)
        elif k in ("p", "N"):
            self.select(self.i - 1)
        elif k in ("]", "J"):
            self.claim(1)
        elif k in ("[", "K"):
            self.claim(-1)
        elif k == "o":
            s = self.entry.sources[self.i] if self.entry.sources else None
            if s and s.ann_key:
                try:
                    self.zot.open_annotation(s.ann_key)
                    self.view.status = "opened in Zotero"
                except ZoteroError as e:
                    self.view.status = str(e)[:120]
            else:
                self.view.status = "this source has no Zotero highlight"
        elif k in ("q", "\x1b", "\x03"):
            close_window()
            raise KeyboardInterrupt
        else:
            return super().handle_key(k)
        return True


def _code_snippet(proj: Project, code: dict, context: int = 12) -> str:
    path = Path(str(code.get("path", "")))
    for base in (Path(proj.cfg.code_dir or "."), proj.cfg.root):
        if not path.is_absolute() and (base / path).exists():
            path = base / path
            break
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return f"\r\n  {code.get('path')}: not found on this machine\r\n"
    ln = int(code.get("line") or 1)
    a, b = max(1, ln - context), min(len(lines), ln + context)
    out = [f"  {path}\r\n"]
    for i in range(a, b + 1):
        mark = ">" if i == ln else " "
        out.append(f"{mark}{i:5d}  {lines[i - 1]}\r\n")
    return "".join(out)


# ---- tmux window ---------------------------------------------------------------------
def _tmux(*args: str) -> str:
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RedlineError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def _session() -> str:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        raise RedlineError("not inside tmux: the evidence window opens in the redline session")
    return _tmux("display", "-p", "-t", pane, "#{session_id}")


def find_window(session: str) -> str | None:
    for ln in _tmux("list-windows", "-t", session, "-F", "#{window_id} #{window_name}").splitlines():
        wid, _, name = ln.partition(" ")
        if name == WINDOW:
            return wid
    return None


def close_window() -> None:
    pane = os.environ.get("TMUX_PANE")
    if pane:
        subprocess.run(["tmux", "kill-window", "-t", pane], capture_output=True)


def show(proj: Project, eid: str, toggle: bool = True) -> str:
    """Open the evidence window for `eid` (or close it, if one is open)."""
    Library(proj.dir / "evidence.json").get(eid)  # fail early on an unknown id
    session = _session()
    open_ = find_window(session)
    if open_:
        shown = _tmux("show-options", "-wqv", "-t", open_, "@redline_evidence")
        _tmux("kill-window", "-t", open_)
        if toggle and shown == eid:
            return "closed the evidence window"
    root = str(proj.cfg.root)
    # the note pane first, then the PDF pane split off to its left at its final
    # size: the page is drawn once, not again after a resize
    note_pane = _tmux("new-window", "-P", "-F", "#{pane_id}", "-t", f"{session}:", "-n", WINDOW, "-c", root,
                      _pane_cmd(proj, "note", eid))
    wid = _tmux("display", "-p", "-t", note_pane, "#{window_id}")
    _tmux("set-option", "-w", "-t", wid, "@redline_evidence", eid)
    _tmux("set-option", "-w", "-t", wid, "@redline_note_pane", note_pane)
    _tmux("split-window", "-h", "-b", "-l", "62%", "-t", note_pane, "-c", root,
          _pane_cmd(proj, "view", eid))
    return f"evidence {eid}: window opened (q closes it)"


def _pane_cmd(proj: Project, what: str, eid: str) -> str:
    from .launcher import _env_prefix, in_login_shell, redline_cmd
    rl = _env_prefix() + " ".join(shlex.quote(c) for c in redline_cmd())
    env = f"REDLINE_ROOT={shlex.quote(str(proj.cfg.root))} "
    return in_login_shell(f"env {env}{rl} evidence {what} {shlex.quote(eid)}")


def follow_note(proj: Project, eid: str) -> None:
    """Show `eid` in the note pane of this evidence window."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return
    r = subprocess.run(["tmux", "show-options", "-wqv", "-t", pane, "@redline_note_pane"],
                       capture_output=True, text=True)
    note_pane = r.stdout.strip()
    subprocess.run(["tmux", "set-option", "-w", "-t", pane, "@redline_evidence", eid], capture_output=True)
    if note_pane:
        subprocess.run(["tmux", "respawn-pane", "-k", "-t", note_pane, _pane_cmd(proj, "note", eid)],
                       capture_output=True)


def view(proj: Project, eid: str) -> int:
    lib = Library(proj.dir / "evidence.json")
    entry = lib.get(eid)
    block = [lib.entries[i] for i in block_ids(proj, entry.id) if i in lib.entries] or [entry]
    return run(EvidencePane(proj, entry, block=block), settle=())


# ---- the note pane ---------------------------------------------------------------------
B, I, D, R = "\x1b[1m", "\x1b[3m", "\x1b[2m", "\x1b[0m"
FG = {"green": "\x1b[32m", "red": "\x1b[31m", "yellow": "\x1b[33m", "blue": "\x1b[34m",
      "magenta": "\x1b[35m", "cyan": "\x1b[36m"}
STATUS_COLOR = {"VERIFIED": "green", "REFUTED": "red", "UNVERIFIED": "yellow"}
ROLE_COLOR = {"supports": "green", "contradicts": "red", "context": "blue", "reference": "yellow"}


def _wrap(text: str, width: int, first: str = "", rest: str | None = None, style: str = "") -> list[str]:
    """Word-wrapped lines; `first`/`rest` are plain prefixes (their width counts)."""
    import textwrap
    rest = first if rest is None else rest
    lines = textwrap.wrap(" ".join(text.split()), width=max(20, width), initial_indent="\0" * len(first),
                          subsequent_indent="\0" * len(rest), break_long_words=True) or [""]
    out = []
    for i, ln in enumerate(lines):
        pre = first if i == 0 else rest
        out.append(pre + style + ln.lstrip("\0") + (R if style else ""))
    return out


def rich_text(e: Entry, width: int, block: list[str] | None = None) -> str:
    """The entry for a terminal: colours, bold and italics, wrapped to `width`."""
    w = max(30, width - 1)
    col = FG[STATUS_COLOR.get(e.status, "yellow")]
    out = [f"{B}{col}{e.status}{R}  {B}{e.id}{R}"]
    if block and len(block) > 1:
        k = block.index(e.id) + 1 if e.id in block else 1
        out.append(f"{D}claim {k} of {len(block)} in this block: ]/[ in the PDF pane{R}")
    out.append("")
    if e.claim:
        out.append(f"{B}{FG['cyan']}Claim{R}" + (f"  {D}§{e.section}{R}" if e.section else ""))
        out += _wrap(e.claim, w, "  ", style=I)
        out.append("")
    if e.reason:
        out.append(f"{B}{FG['cyan']}Remark{R}")
        out += _wrap(e.reason, w, "  ")
        out.append("")
    for i, s in enumerate(e.sources, 1):
        if s.code:
            out.append(f"{B}[{i}] code{R} {FG['magenta']}{s.code.get('path', '')}:{s.code.get('line', '')}{R}")
            if s.code.get("value"):
                out += _wrap(str(s.code["value"]), w, "    ")
            out.append("")
            continue
        where = ", ".join(x for x in (s.section, f"p.{s.page}" if s.page else "") if x)
        role = FG[ROLE_COLOR.get(s.role, "yellow")]
        if s.role == "reference":
            vcol = FG["green"] if s.verdict == "match" else FG["red"] if s.verdict == "mismatch" else FG["yellow"]
            out.append(f"{B}[{i}] {FG['magenta']}{s.bibkey}{R} {D}reference list{R}  {D}{where}{R}")
            out += _wrap(f"names «{s.mention}»; compared with {s.target}:", w, "    ")
            out += _wrap(f"{s.verdict.upper()}: {s.why}", w, "    ", style=B + vcol)
        else:
            out.append(f"{B}[{i}] {FG['magenta']}{s.bibkey}{R}  {role}{s.role}{R}"
                       + (f" {D}for {s.for_key}{R}" if s.for_key else "") + (f"  {D}{where}{R}" if where else ""))
        if s.quote:
            bar = f"  {role}│{R} "
            for ln in _wrap(s.quote, w - 4, "", style=I):
                out.append(bar + ln)
        if s.highlight and s.highlight != "ok":
            out += _wrap(f"no Zotero highlight: {s.highlight.removeprefix('none: ')}", w, "    ",
                         style=FG["yellow"])
        out.append("")
    out.append(f"{D}Zotero: {e.zotero}{R}")
    out.append(f"{D}q closes · PDF pane: n/p source"
               + (", ]/[ claim" if block and len(block) > 1 else "") + f", o open in Zotero{R}")
    return "\n".join(out) + "\n"


def note(proj: Project, eid: str) -> int:
    """The entry, formatted, in `less`; leaving it (q) closes the whole window."""
    entry = Library(proj.dir / "evidence.json").get(eid)
    block = block_ids(proj, entry.id)
    try:
        width = os.get_terminal_size(sys.stdout.fileno()).columns
    except OSError:
        width = 80
    text = rich_text(entry, width, block)
    try:
        if shutil.which("less"):
            subprocess.run(["less", "-R", "-K", "--prompt=q closes the evidence window"],
                           input=text.encode(), check=False)
        else:
            sys.stdout.write(text)
            sys.stdout.flush()
            sys.stdin.read(1)
    except KeyboardInterrupt:
        pass
    close_window()
    return 0
