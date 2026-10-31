"""PDF pane: shows the page for a source line, highlighted, via kitty graphics.

nvim sends {"line": L, "col": C, "text": excerpt} (or {"page": P}) to the
preview socket. synctex maps the line to the page; the excerpt's words are found
on it (pdfmatch) and marked like a highlighter pen (multiply blend, so the ink
stays black). MuPDF (or pdftoppm) renders; kittygfx sends the page to kitty once
as a small PNG and shows it with unicode placeholders, so tmux redraws keep it
and showing a page again costs nothing. Neighbouring pages are sent while idle.
Re-renders when the PDF changes.

keys: j/k or space/b next/prev page, +/- zoom, 0 fit, z crop to the highlight,
      e or Enter: the LaTeX source of what is shown (cursor there in nvim),
      f toggle follow, r redraw, q quit
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import termios
import time
import tty
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from . import compile as _compile
from . import pdfmatch
from .pdfmatch import Rect
from .state import Project

HIGHLIGHTER = (255, 226, 70)  # marker yellow; multiplied into the page


@dataclass
class View:
    page: int = 1
    zoom: float = 1.0
    crop: bool = False
    follow: bool = True
    rects: list[Rect] = field(default_factory=list)
    status: str = ""
    line: int = 0
    end: int = 0
    text: str = ""
    shown: tuple = ()  # two consecutive pages shown stitched, or () for v.page alone


def synctex_boxes(tex: Path, pdf: Path, line: int, col: int = 0) -> list[Rect]:
    """Line boxes for a source line. synctex's v is the TOP of the box."""
    if not shutil.which("synctex") or not pdf.exists():
        return []
    try:
        r = subprocess.run(["synctex", "view", "-i", f"{line}:{col}:{tex}", "-o", str(pdf)],
                           capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return []
    boxes, cur = [], {}
    for ln in r.stdout.splitlines():
        k, _, v = ln.partition(":")
        if k == "Output" and cur:
            cur = {}
        if k in ("Page", "h", "v", "W", "H"):
            cur[k] = float(v)
            if all(x in cur for x in ("Page", "h", "v", "W", "H")):
                if cur["W"] > 0:
                    boxes.append(Rect(int(cur["Page"]), cur["h"], cur["v"],
                                      cur["h"] + cur["W"], cur["v"] + cur["H"]))
                cur = {}
    return boxes


def synctex_edit(pdf: Path, page: int, x: float, y: float) -> tuple[Path, int] | None:
    """The source file and line typeset at (x, y) on page (pt, origin top-left)."""
    if not shutil.which("synctex") or not pdf.exists():
        return None
    try:
        out = subprocess.run(["synctex", "edit", "-o", f"{page}:{x:.1f}:{y:.1f}:{pdf}"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    inp = re.search(r"^Input:(.+)$", out, re.M)
    line = re.search(r"^Line:(\d+)$", out, re.M)
    if not (inp and line) or int(line.group(1)) < 1:
        return None
    return Path(inp.group(1).strip()), int(line.group(1))


def pdf_info(pdf: Path) -> tuple[int, float]:
    """(page count, page width in pt)."""
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, 612.0
    m = re.search(r"^Pages:\s+(\d+)", out, re.M)
    w = re.search(r"^Page size:\s+([\d.]+) x", out, re.M)
    return (int(m.group(1)) if m else 1), (float(w.group(1)) if w else 612.0)


def cell_size() -> tuple[int, int]:
    try:
        out = subprocess.run(["tmux", "display", "-p", "#{client_cell_width} #{client_cell_height}"],
                             capture_output=True, text=True, timeout=3).stdout.split()
        w, h = int(out[0]), int(out[1])
        if w > 0 and h > 0:
            return w, h
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    return 10, 20


def page_height(pdf: Path, page: int) -> float:
    """Height of one page in pt (pages of a paper can differ)."""
    try:
        out = subprocess.run(["pdfinfo", "-f", str(page), "-l", str(page), str(pdf)],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 792.0
    m = re.search(rf"^Page\s+{page} size:\s+[\d.]+ x ([\d.]+)", out, re.M) or \
        re.search(r"^Page size:\s+[\d.]+ x ([\d.]+)", out, re.M)
    return float(m.group(1)) if m else 792.0


class Pane:
    def __init__(self, proj: Project, pdf: Path | None = None, tmp: Path | None = None):
        self.proj = proj
        self.pdf = pdf or _compile.view_pdf(proj)
        self.tex = proj.main
        self.view = View()
        self.tmp = tmp or proj.dir / "preview"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.mtime = 0.0
        self.pages = 1
        self.page_w = 612.0
        self.drawn_size = (0, 0)
        self.prefetch: list[View] = []
        self.windows: dict[int, tuple[float, float]] = {}  # page -> visible (top, bottom) in pt
        self.source_jump = True  # e/Enter: show the LaTeX source of what is on screen
        from .kittygfx import Kitty
        self.gfx = Kitty()

    # ---- rendering --------------------------------------------------------------
    def render(self) -> None:
        cols, rows = _size()
        self.drawn_size = (cols, rows)
        # stay 2 columns clear of the right edge: a placeholder in the last
        # column makes the terminal wrap the line
        cols = max(10, cols - 2)
        rows -= 1  # status line
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
        if not self.pdf.exists():
            self._status(f"no PDF yet ({self.pdf.name}); compile from nvim with c in review mode")
            return
        v = self.view
        v.page = max(1, min(v.page, self.pages))
        err = self._display(v, cols, rows)
        flags = " ".join(x for x in ("follow" if v.follow else "", "crop" if v.crop else "",
                                     f"zoom {v.zoom:.1f}" if v.zoom != 1 else "") if x)
        pages = v.shown if len(v.shown) == 2 else (v.page,)
        where = f"p.{pages[0]}–{pages[1]}" if len(pages) == 2 else f"p.{v.page}"
        self._status(err or f"{where}/{self.pages}  {flags}  {v.status}")
        # what is likely shown next: the neighbouring pages, sent while you read
        self.prefetch = [View(page=pg, zoom=v.zoom, crop=v.crop) for pg in (v.page + 1, v.page - 1)
                         if 1 <= pg <= self.pages and not v.shown]

    def _key(self, v: View, cols: int, rows: int) -> tuple:
        pages = v.shown if len(v.shown) == 2 else (v.page,)
        marked = tuple((r.page, round(r.x0), round(r.y0), round(r.x1), round(r.y1))
                       for r in v.rects if r.page in pages)
        return (str(self.pdf), self.mtime, pages, marked, round(v.zoom, 3), v.crop, cols, rows, cell_size())

    def _display(self, v: View, cols: int, rows: int, show: bool = True) -> str:
        """Send (unless kitty has it already) and show the image for view v. Returns an error or ""."""
        if os.environ.get("REDLINE_ICAT"):
            return self._icat_display(v, cols, rows) if show else ""
        key = self._key(v, cols, rows)
        if not self.gfx.has(key):
            cw, ch = cell_size()
            img = self.compose(v, cols * cw, rows * ch)
            if img is None:
                return "rendering the page failed"
            c = max(1, min(cols, -(-img.width // cw)))
            r = max(1, min(rows, -(-img.height // ch)))
            from .kittygfx import encode_png
            self.gfx.send(key, encode_png(img), c, r)
        if show:
            self.gfx.show(key)
        return ""

    def _icat_display(self, v: View, cols: int, rows: int) -> str:
        """Fallback (REDLINE_ICAT=1): kitten icat, re-sending the whole image every time."""
        cw, ch = cell_size()
        img = self.compose(v, cols * cw, rows * ch)
        if img is None:
            return "rendering the page failed"
        png = self.tmp / "page.png"
        img.save(png)
        cmd = ["kitten", "icat", "--stdin=no", "--clear", "--transfer-mode=stream", "--passthrough=tmux",
               "--place", f"{cols}x{rows}@0x0", "--scale-up", "--align=left", str(png)]
        try:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"icat failed: {e}"
        return "" if r.returncode == 0 else f"icat failed: {(r.stderr.strip() or r.returncode)}"

    def prefetch_one(self) -> bool:
        """Send one likely-next page to kitty ahead of time. False when there is nothing left."""
        while getattr(self, "prefetch", None):
            v = self.prefetch.pop(0)
            cols, rows = self.drawn_size
            if self._key(v, max(10, cols - 2), rows - 1) in self.gfx.images:
                continue
            self._display(v, max(10, cols - 2), rows - 1, show=False)
            return True
        return False

    def compose(self, v: View, px_w: int, px_h: int):
        """The image for view v (pages, highlights, zoom, crop), at most px_w x px_h."""
        pages = v.shown if len(v.shown) == 2 else (v.page,)
        marked = [r for r in v.rects if r.page in pages]
        # resolution: fit the page width to the pane, times zoom (crop zooms in 2x)
        zoom = v.zoom * (2.0 if v.crop and marked else 1.0)
        dpi = max(30, min(600, int(px_w / (self.page_w / 72.0) * zoom)))
        s = dpi / 72.0
        imgs = []
        for pg in pages:
            im = self._page_image(pg, dpi, [r for r in marked if r.page == pg])
            if im is None:
                return None
            imgs.append(im)
        # highlight rectangles in image pixels (for scrolling to them)
        hl = [(r.x0 * s, r.y0 * s, r.x1 * s, r.y1 * s) for r in marked]
        if len(imgs) == 2:
            # trim the empty page margins at the break (below the last line of
            # the first page, above the first line of the next)
            wa, wb = self._words(pages[0]), self._words(pages[1])
            bounds = (max((w.y1 for w in wa), default=None), min((w.y0 for w in wb), default=None))
            img, hl = self._stitch(imgs, marked, s, bounds)
        else:
            img = imgs[0]
        if len(imgs) == 1:  # visible part of the page, in pt (for jumping to the source)
            self.windows[pages[0]] = (0.0, img.height / s)
        if (v.crop or v.zoom > 1.0) and img.width > px_w:
            # window of pane size centred on the highlight (or the page top)
            if hl:
                cx = sum((a + c) / 2 for a, _, c, _ in hl) / len(hl)
                cy = sum((b + d) / 2 for _, b, _, d in hl) / len(hl)
            else:
                cx, cy = img.width / 2, px_h / 2
            x0 = int(max(0, min(img.width - px_w, cx - px_w / 2)))
            y0 = int(max(0, min(img.height - px_h, cy - px_h / 2)))
            img = img.crop((x0, y0, x0 + px_w, y0 + px_h))
            if len(imgs) == 1:
                self.windows[pages[0]] = (y0 / s, (y0 + px_h) / s)
        elif hl and img.height > px_h:
            # fit-width page taller than the pane: scroll so the highlight is
            # visible; if it does not fit (e.g. it runs from the bottom of one
            # column to the top of the next) show everything, scaled down
            top, bottom = min(b for _, b, _, _ in hl), max(d for _, _, _, d in hl)
            if bottom - top <= px_h * 0.9:
                y0 = top - px_h / 3 if bottom - top < px_h * 2 / 3 else top - px_h / 12
                y0 = int(max(0, min(img.height - px_h, y0)))
                img = img.crop((0, y0, img.width, y0 + px_h))
                if len(imgs) == 1:
                    self.windows[pages[0]] = (y0 / s, (y0 + px_h) / s)
            else:
                img = img.resize((max(1, int(img.width * px_h / img.height)), px_h))
        elif img.height > px_h:
            img = img.crop((0, 0, img.width, px_h))
            if len(imgs) == 1:
                self.windows[pages[0]] = (0.0, px_h / s)
        if img.width > px_w:
            img = img.resize((px_w, max(1, int(img.height * px_w / img.width))))
        return img

    def _raster(self, page: int, dpi: int):
        """The page at dpi, uncoloured; the last few are kept (paging back is instant)."""
        from PIL import Image
        cache = self.__dict__.setdefault("_rcache", OrderedDict())
        key = (str(self.pdf), self.mtime, page, dpi)
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        out = self.tmp / f"page-{page}"
        mutool = shutil.which("mutool")
        if mutool:  # MuPDF: 10-20x faster than poppler on figure-heavy pages
            target = out.with_suffix(".ppm")
            subprocess.run([mutool, "draw", "-q", "-r", str(dpi), "-o", str(target), str(self.pdf), str(page)],
                           capture_output=True, timeout=60)
        else:
            target = out.with_suffix(".png")
            subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi),
                            "-png", "-singlefile", str(self.pdf), str(out)], capture_output=True, timeout=60)
        if not target.exists():
            return None
        img = Image.open(target).convert("RGB")
        img.load()
        target.unlink()
        cache[key] = img
        while len(cache) > 8:
            cache.popitem(last=False)
        return img

    def _page_image(self, page: int, dpi: int, rects: list[Rect]):
        from PIL import Image, ImageChops, ImageDraw
        img = self._raster(page, dpi)
        if img is None:
            return None
        if rects:
            # highlighter pen: a yellow layer multiplied into the page, so the
            # paper turns yellow behind the words and the ink stays black
            s = dpi / 72.0
            pen = Image.new("RGB", img.size, (255, 255, 255))
            d = ImageDraw.Draw(pen)
            pad = max(1.0, 0.8 * s)
            for b in rects:
                d.rounded_rectangle((b.x0 * s - pad, b.y0 * s - pad, b.x1 * s + pad, b.y1 * s + pad),
                                    radius=max(1, int(1.5 * s)), fill=HIGHLIGHTER)
            img = ImageChops.multiply(img, pen)
        return img

    @staticmethod
    def _stitch(imgs, rects: list[Rect], s: float, text_bounds=(None, None)):
        """Bottom half of the first page over the top half of the next one.

        The cuts move outwards if the highlight starts above the middle of the
        first page or ends below the middle of the second, so it is never cut.
        text_bounds (last text line bottom on page 1, first text top on page 2,
        in pt) trim the blank margins at the break.
        """
        from PIL import Image
        a, b = imgs
        end_a, start_b = text_bounds
        if end_a is not None:
            a = a.crop((0, 0, a.width, min(a.height, int((end_a + 8) * s))))
        top_b = 0
        if start_b is not None:
            top_b = max(0, int((start_b - 8) * s))
            b = b.crop((0, top_b, b.width, b.height))
        pa, pb = rects[0].page if rects else 0, rects[-1].page if rects else 0
        margin = 12 * s
        cut_a = a.height // 2
        cut_b = b.height // 2
        on_a = [r for r in rects if r.page == pa]
        on_b = [r for r in rects if r.page == pb and pb != pa]
        if on_a:
            cut_a = int(max(0, min(cut_a, min(r.y0 for r in on_a) * s - margin)))
        if on_b:
            cut_b = int(min(b.height, max(cut_b, max(r.y1 for r in on_b) * s - top_b + margin)))
        gap = max(4, int(2 * s))
        top, bottom = a.crop((0, cut_a, a.width, a.height)), b.crop((0, 0, b.width, cut_b))
        img = Image.new("RGB", (max(a.width, b.width), top.height + gap + bottom.height), (150, 150, 150))
        img.paste(top, (0, 0))
        img.paste(bottom, (0, top.height + gap))
        first = rects[0].page if rects else 0
        hl = []
        for r in rects:
            dy = -cut_a if r.page == first else top.height + gap - top_b
            hl.append((r.x0 * s, r.y0 * s + dy, r.x1 * s, r.y1 * s + dy))
        return img, hl

    def _status(self, text: str) -> None:
        cols, rows = _size()
        text = f" {text} "[: max(1, cols - 1)]
        sys.stdout.write(f"\x1b[{rows};1H\x1b[2K\x1b[7m{text}\x1b[0m")
        sys.stdout.flush()

    # ---- events -------------------------------------------------------------------
    def goto_line(self, line: int, col: int = 0, text: str = "", end: int = 0) -> None:
        """Show the page(s) of source lines line..end, highlighting `text` (the excerpt).

        A change that runs over a page break is shown as the bottom of one page
        stitched onto the top of the next.
        """
        v = self.view
        v.line, v.end, v.text = line, end, text
        boxes = []
        for ln in range(line, max(line, min(end, line + 25)) + 1):
            boxes += synctex_boxes(self.tex, self.pdf, ln, col if ln == line else 0)
        if not boxes:
            v.rects, v.shown, v.status = [], (), f"l.{line}: no synctex match (recompile?)"
            return
        counts = [b.page for b in boxes]
        main = max(set(counts), key=counts.count)
        # synctex files a typeset line under the source line it starts in, so a
        # change can begin on the previous page or end on the next one without
        # synctex saying so: search the neighbours too
        cand = tuple(pg for pg in (main - 1, main, main + 1) if 1 <= pg <= self.pages)
        near = [b for b in boxes if b.page in cand]
        rects = []
        if text:
            words = [w for pg in cand for w in self._words(pg)]
            rects = pdfmatch.locate(words, main, text, near)
        approximate = not rects
        if approximate:
            # excerpt not found as text (math, figure...): mark the source lines' lines
            rects = [b for b in boxes if b.page == main]
        on = sorted({r.page for r in rects})
        v.rects = rects
        v.shown = (on[0], on[1]) if len(on) >= 2 and on[1] == on[0] + 1 else ()
        v.page = on[0] if v.shown else (main if main in on else on[0])
        v.status = f"l.{line}" + (" (approximate)" if approximate else "")

    def _words(self, page: int):
        cache = getattr(self, "_wcache", {})
        if cache.get("mtime") != self.mtime:
            cache = {"mtime": self.mtime}
        if page not in cache:
            cache[page] = pdfmatch.page_words(self.pdf, page)
        self._wcache = cache
        return cache[page]

    def source_point(self) -> tuple[int, float, float]:
        """Where on screen to ask synctex: the highlight, else the first text line in view."""
        v = self.view
        pages = v.shown if len(v.shown) == 2 else (v.page,)
        marked = [r for r in v.rects if r.page in pages]
        if marked:
            r = marked[0]
            return r.page, (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        top, bottom = self.windows.get(v.page, (0.0, float("inf")))
        words = [w for w in self._words(v.page) if w.y0 >= top - 1 and w.y1 <= bottom + 1]
        if words:
            w = min(words, key=lambda w: (round(w.y0 / 3), w.x0))
            return v.page, (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2
        return v.page, self.page_w / 2, top + 72

    def goto_source(self) -> None:
        page, x, y = self.source_point()
        hit = synctex_edit(self.pdf, page, x, y)
        if not hit:
            self.view.status = f"p.{page}: no synctex match (recompile?)"
            return
        path, line = hit
        err = _nvim_goto(self.proj, path, line)
        self.view.status = err or f"source: {path.name}:{line}"

    def handle_msg(self, msg: dict) -> bool:
        if "line" in msg:
            if not self.view.follow and not msg.get("force"):
                return False
            self.goto_line(int(msg["line"]), int(msg.get("col", 0)), str(msg.get("text") or ""),
                           int(msg.get("end") or 0))
            return True
        if "page" in msg:
            self.view.page = int(msg["page"])
            self.view.rects, self.view.shown = [], ()
            return True
        return False

    def handle_key(self, k: str) -> bool:
        v = self.view
        if k in ("j", " ", "\x1b[B", "l", "k", "b", "\x1b[A", "h", "g", "G") and v.shown:
            v.page = v.shown[0]
            v.shown = ()  # paging: one page at a time again
        if k in ("j", " ", "\x1b[B", "l"):
            v.page += 1
        elif k in ("k", "b", "\x1b[A", "h"):
            v.page -= 1
        elif k in ("+", "="):
            v.zoom = min(4.0, v.zoom * 1.25)
        elif k == "-":
            v.zoom = max(1.0, v.zoom / 1.25)
        elif k == "0":
            v.zoom, v.crop = 1.0, False
        elif k == "z":
            v.crop = not v.crop
        elif k == "f":
            v.follow = not v.follow
        elif k == "g":
            v.page = 1
        elif k == "G":
            v.page = self.pages
        elif k == "r":
            pass
        elif k in ("e", "\r", "\n") and self.source_jump:
            self.goto_source()
        elif k in ("q", "\x03"):
            raise KeyboardInterrupt
        else:
            return False
        return True

    def check_pdf(self) -> bool:
        try:
            m = self.pdf.stat().st_mtime
        except OSError:
            return False
        if m != self.mtime:
            self.mtime = m
            self.pages, self.page_w = pdf_info(self.pdf)
            return True
        return False


def _nvim_goto(proj: Project, path: Path, line: int) -> str:
    """Put the paper nvim's cursor on path:line and focus its tmux pane. Returns an error or ""."""
    from .config import socket_path
    sock = socket_path(proj.cfg.root, "nvim")
    if not sock.exists() or not shutil.which("nvim"):
        return "the paper nvim is not running (no socket)"
    expr = f"luaeval('require(\"redline\").goto_source(_A[1], _A[2])', [{json.dumps(str(path))}, {int(line)}])"
    try:
        r = subprocess.run(["nvim", "--server", str(sock), "--remote-expr", expr],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"nvim did not answer: {e}"
    if r.returncode != 0 or r.stdout.strip() not in ("", "ok"):
        return f"nvim: {(r.stdout + r.stderr).strip()[:100]}"
    here = ["-t", os.environ["TMUX_PANE"]] if os.environ.get("TMUX_PANE") else []
    out = subprocess.run(["tmux", "list-panes", "-s", *here, "-F", "#{pane_id} #{window_id} #{@redline_role}"],
                         capture_output=True, text=True).stdout
    for ln in out.splitlines():
        pid, wid, *role = ln.split()
        if role == ["paper"]:
            subprocess.run(["tmux", "select-window", "-t", wid], capture_output=True)
            subprocess.run(["tmux", "select-pane", "-t", pid], capture_output=True)
            break
    return ""


def _size() -> tuple[int, int]:
    """The pane's size from the tty itself (not $COLUMNS/$LINES, which go stale)."""
    try:
        sz = os.get_terminal_size(sys.stdout.fileno())
        return sz.columns, sz.lines
    except OSError:
        return tuple(shutil.get_terminal_size())


def main(proj: Project) -> int:
    if not _compile.view_pdf(proj).exists():
        _compile.publish(proj)  # a build from before the view copy existed
    pane = Pane(proj)
    from .config import socket_path
    sock_path = socket_path(proj.cfg.root, "preview")
    sock_path.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(8)
    try:
        return run(pane, srv)
    finally:
        srv.close()
        sock_path.unlink(missing_ok=True)


def run(pane: Pane, srv: socket.socket | None = None, settle: tuple[float, ...] = (1.5, 4.0)) -> int:
    """The pane's event loop: keys, socket messages, PDF rebuilds and resizes.

    settle: extra redraws (seconds after start) for panes that are still being
    arranged when they start (the paper preview under a layout preset)."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    if old:
        tty.setcbreak(fd)
    sys.stdout.write("\x1b[?25l")
    pane.check_pdf()
    pane.render()
    # The pane is still settling when we start (layout preset, client attach),
    # so draw again shortly after; later resizes redraw once the size is stable.
    t0 = time.monotonic()
    pending = [t0 + d for d in settle]
    resized_at = None
    try:
        while True:
            r, _, _ = select.select([x for x in (srv, sys.stdin) if x is not None], [], [], 0.25)
            dirty = False
            if srv is not None and srv in r:
                conn, _ = srv.accept()
                conn.settimeout(1)
                data = b""
                try:
                    while chunk := conn.recv(4096):
                        data += chunk
                except socket.timeout:
                    pass
                conn.close()
                for line in data.decode(errors="replace").splitlines():
                    try:
                        dirty |= pane.handle_msg(json.loads(line))
                    except (ValueError, KeyError):
                        pass
            if sys.stdin in r:
                k = os.read(fd, 8).decode(errors="replace")
                dirty |= pane.handle_key(k)
            if pane.check_pdf():
                # rebuilt: find the same excerpt again
                if pane.view.line:
                    pane.goto_line(pane.view.line, 0, pane.view.text, pane.view.end)
                dirty = True
            now = time.monotonic()
            if _size() != pane.drawn_size:
                resized_at = resized_at or now
                if now - resized_at >= 0.4:  # size stopped changing
                    dirty, resized_at = True, None
            else:
                resized_at = None
            if pending and now >= pending[0]:
                pending.pop(0)
                dirty = True
            if dirty:
                pane.render()
            elif not r and pane.prefetch and not os.environ.get("REDLINE_ICAT"):
                pane.prefetch_one()  # idle: send a neighbouring page ahead of time
    except KeyboardInterrupt:
        pass
    finally:
        pane.gfx.forget()
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?25h\x1b[2J\x1b[H")
    return 0
