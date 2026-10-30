"""The evidence window: the source page with the quote highlighted, and the note.

`redline evidence show ID` opens a tmux window "evidence" next to the paper:
left the source PDF (the PDF pane's renderer, with the Zotero highlight drawn
as a highlighter), right the entry as text. q in either pane, or the nvim key
again, closes the window.

PDF pane keys: n/p next/previous source, o open the highlight in Zotero,
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


class EvidencePane(Pane):
    def __init__(self, proj: Project, entry: Entry, zot: Zotero | None = None):
        super().__init__(proj, pdf=Path("/nonexistent.pdf"), tmp=proj.dir / "evidence-view")
        self.entry = entry
        self.zot = zot or Zotero()
        self.view.follow = False  # nothing to follow: no paper cursor here
        self.i = 0
        self.select(0)

    def select(self, i: int) -> None:
        n = len(self.entry.sources)
        v = self.view
        v.rects, v.shown, v.zoom, v.crop = [], (), 1.0, False
        if not n:
            v.status = f"{self.entry.id}: no sources (q closes)"
            return
        self.i = i % n
        s = self.entry.sources[self.i]
        head = f"[{self.i + 1}/{n}] {s.bibkey or 'code'}"
        if s.code:
            self.pdf = Path("/nonexistent.pdf")
            v.status = f"{head}: a code check (see the note)  n/p source  q close"
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
        v.status = f"{head} p.{s.page or v.page}{how}  n/p source  o Zotero  q close"

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
    from .launcher import _env_prefix, in_login_shell, redline_cmd
    rl = _env_prefix() + " ".join(shlex.quote(c) for c in redline_cmd())
    root = str(proj.cfg.root)
    env = f"REDLINE_ROOT={shlex.quote(root)} "
    left = _tmux("new-window", "-P", "-F", "#{pane_id}", "-t", f"{session}:", "-n", WINDOW, "-c", root,
                 in_login_shell(f"env {env}{rl} evidence view {shlex.quote(eid)}"))
    wid = _tmux("display", "-p", "-t", left, "#{window_id}")
    _tmux("set-option", "-w", "-t", wid, "@redline_evidence", eid)
    _tmux("split-window", "-h", "-l", "38%", "-t", left, "-c", root,
          in_login_shell(f"env {env}{rl} evidence note {shlex.quote(eid)}"))
    _tmux("select-pane", "-t", left)
    return f"evidence {eid}: window opened (q closes it)"


def view(proj: Project, eid: str) -> int:
    entry = Library(proj.dir / "evidence.json").get(eid)
    return run(EvidencePane(proj, entry))


def note(proj: Project, eid: str) -> int:
    """The entry as text in `less`; leaving it (q) closes the whole window."""
    entry = Library(proj.dir / "evidence.json").get(eid)
    text = entry.text() + "\n\n(q closes the evidence window; in the PDF pane: n/p sources, o Zotero)\n"
    try:
        if shutil.which("less"):
            subprocess.run(["less", "-R", "-S", "-K", "--prompt=q closes the evidence window"],
                           input=text.encode(), check=False)
        else:
            sys.stdout.write(text)
            sys.stdout.flush()
            sys.stdin.read(1)
    except KeyboardInterrupt:
        pass
    close_window()
    return 0
