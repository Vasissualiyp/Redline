"""PDF pane: shows the page for a source line, highlighted, via kitty graphics.

nvim sends {"line": L, "col": C, "text": excerpt} (or {"page": P}) to the
preview socket. synctex maps the line to the page; the excerpt's words are found
on it (pdfmatch) and marked like a highlighter pen (multiply blend, so the ink
stays black). pdftoppm renders, `kitten icat` shows it in this tmux pane
(unicode placeholders, so tmux redraws keep it). Re-renders when the PDF changes.

keys: j/k or space/b next/prev page, +/- zoom, 0 fit, z crop to the highlight,
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
import tty
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


class Pane:
    def __init__(self, proj: Project):
        self.proj = proj
        self.pdf = _compile.pdf_path(proj)
        self.tex = proj.main
        self.view = View()
        self.tmp = proj.dir / "preview"
        self.tmp.mkdir(exist_ok=True)
        self.mtime = 0.0
        self.pages = 1
        self.page_w = 612.0

    # ---- rendering --------------------------------------------------------------
    def render(self) -> None:
        cols, rows = shutil.get_terminal_size()
        # stay 2 columns clear of the right edge: a placeholder in the last
        # column makes the terminal wrap the line
        cols = max(10, cols - 2)
        rows -= 1  # status line
        # flush now: icat writes to the terminal directly, and a clear still
        # sitting in Python's buffer would erase the image right after it
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
        if not self.pdf.exists():
            self._status(f"no PDF yet ({self.pdf.name}); compile from nvim with c in review mode")
            return
        from PIL import Image, ImageChops, ImageDraw

        cw, ch = cell_size()
        px_w, px_h = cols * cw, rows * ch
        v = self.view
        v.page = max(1, min(v.page, self.pages))
        boxes = [b for b in v.rects if b.page == v.page]
        # resolution: fit the page width to the pane, times zoom (crop zooms in 2x)
        zoom = v.zoom * (2.0 if v.crop and boxes else 1.0)
        dpi = max(30, min(600, int(px_w / (self.page_w / 72.0) * zoom)))
        out = self.tmp / "page"
        subprocess.run(["pdftoppm", "-f", str(v.page), "-l", str(v.page), "-r", str(dpi),
                        "-png", "-singlefile", str(self.pdf), str(out)],
                       capture_output=True, timeout=60)
        png = out.with_suffix(".png")
        if not png.exists():
            self._status("pdftoppm failed")
            return
        img = Image.open(png).convert("RGB")
        s = dpi / 72.0
        if boxes:
            # highlighter pen: a yellow layer multiplied into the page, so the
            # paper turns yellow behind the words and the ink stays black
            pen = Image.new("RGB", img.size, (255, 255, 255))
            d = ImageDraw.Draw(pen)
            pad = max(1.0, 0.8 * s)
            for b in boxes:
                d.rounded_rectangle((b.x0 * s - pad, b.y0 * s - pad, b.x1 * s + pad, b.y1 * s + pad),
                                    radius=max(1, int(1.5 * s)), fill=HIGHLIGHTER)
            img = ImageChops.multiply(img, pen)
        if (v.crop or v.zoom > 1.0) and img.width > px_w:
            # window of pane size centred on the highlight (or the page top)
            if boxes:
                cx = sum((b.x0 + b.x1) / 2 for b in boxes) / len(boxes) * s
                cy = sum((b.y0 + b.y1) / 2 for b in boxes) / len(boxes) * s
            else:
                cx, cy = img.width / 2, px_h / 2
            x0 = int(max(0, min(img.width - px_w, cx - px_w / 2)))
            y0 = int(max(0, min(img.height - px_h, cy - px_h / 2)))
            img = img.crop((x0, y0, x0 + px_w, y0 + px_h))
        elif boxes and img.height > px_h:
            # fit-width page taller than the pane: scroll so the highlight is visible
            cy = min(b.y0 for b in boxes) * s
            y0 = int(max(0, min(img.height - px_h, cy - px_h / 3)))
            img = img.crop((0, y0, img.width, y0 + px_h))
        elif img.height > px_h:
            img = img.crop((0, 0, img.width, px_h))
        img.save(png)
        err = self._icat(png, cols, rows)
        flags = " ".join(x for x in ("follow" if v.follow else "", "crop" if v.crop else "",
                                     f"zoom {v.zoom:.1f}" if v.zoom != 1 else "") if x)
        self._status(f"icat failed: {err}" if err else f"p.{v.page}/{self.pages}  {flags}  {v.status}")

    def _icat(self, png: Path, cols: int, rows: int) -> str:
        """Draw png in the pane. Returns an error message, or "" on success.

        --stdin=no matters: stdin is not a terminal here, and icat's default
        (--stdin=detect) then reads it as a second image and refuses --place.
        """
        cmd = ["kitten", "icat", "--stdin=no", "--clear", "--transfer-mode=stream",
               "--passthrough=tmux", "--place", f"{cols}x{rows}@0x0", "--scale-up",
               "--align=left", str(png)]
        try:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                               timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            msg = str(e)
        else:
            msg = "" if r.returncode == 0 else (r.stderr.strip() or f"exit {r.returncode}")
        if msg:
            with open(self.tmp / "log.txt", "a") as f:
                f.write(f"{' '.join(cmd)}\n  -> {msg}\n")
        return msg.splitlines()[0] if msg else ""

    def _status(self, text: str) -> None:
        cols, rows = shutil.get_terminal_size()
        text = f" {text} "[: max(1, cols - 1)]
        sys.stdout.write(f"\x1b[{rows};1H\x1b[2K\x1b[7m{text}\x1b[0m")
        sys.stdout.flush()

    # ---- events -------------------------------------------------------------------
    def goto_line(self, line: int, col: int = 0, text: str = "", end: int = 0) -> None:
        """Show the page of source lines line..end, highlighting `text` (the excerpt)."""
        v = self.view
        v.line, v.end, v.text = line, end, text
        boxes = []
        for ln in range(line, max(line, min(end, line + 25)) + 1):
            boxes += synctex_boxes(self.tex, self.pdf, ln, col if ln == line else 0)
        if not boxes:
            v.rects, v.status = [], f"l.{line}: no synctex match (recompile?)"
            return
        # the page most of the line's boxes are on
        pages = [b.page for b in boxes]
        v.page = max(set(pages), key=pages.count)
        near = [b for b in boxes if b.page == v.page]
        rects = []
        if text:
            words = self._words(v.page)
            rects = pdfmatch.locate(words, v.page, text, near)
        if rects:
            v.rects, v.status = rects, f"l.{line}"
        else:
            # excerpt not found as text (math, figure...): mark the source line's lines
            v.rects, v.status = near, f"l.{line} (approximate)"

    def _words(self, page: int):
        key = (page, self.mtime)
        if getattr(self, "_wcache", (None,))[0] != key:
            self._wcache = (key, pdfmatch.page_words(self.pdf, page))
        return self._wcache[1]

    def handle_msg(self, msg: dict) -> bool:
        if "line" in msg:
            if not self.view.follow and not msg.get("force"):
                return False
            self.goto_line(int(msg["line"]), int(msg.get("col", 0)), str(msg.get("text") or ""),
                           int(msg.get("end") or 0))
            return True
        if "page" in msg:
            self.view.page = int(msg["page"])
            self.view.rects = []
            return True
        return False

    def handle_key(self, k: str) -> bool:
        v = self.view
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


def main(proj: Project) -> int:
    pane = Pane(proj)
    from .config import socket_path
    sock_path = socket_path(proj.cfg.root, "preview")
    sock_path.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(8)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    if old:
        tty.setcbreak(fd)
    sys.stdout.write("\x1b[?25l")
    pane.check_pdf()
    pane.render()
    last_size = shutil.get_terminal_size()
    try:
        while True:
            r, _, _ = select.select([srv, sys.stdin], [], [], 0.7)
            dirty = False
            if srv in r:
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
            size = shutil.get_terminal_size()
            if size != last_size:
                last_size, dirty = size, True
            if dirty:
                pane.render()
    except KeyboardInterrupt:
        pass
    finally:
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?25h\x1b[2J\x1b[H")
        srv.close()
        sock_path.unlink(missing_ok=True)
    return 0
