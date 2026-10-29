"""PDF pane: shows the page for a source line, highlighted, via kitty graphics.

nvim sends {"line": L, "col": C} (or {"page": P}) to the preview socket;
synctex maps the line to page boxes, pdftoppm renders the page, Pillow draws
the highlight and `kitten icat` puts it in this tmux pane (unicode placeholders,
so tmux redraws keep it). Re-renders when the PDF is rebuilt.

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
from .state import Project


@dataclass
class Box:
    page: int
    x: float
    y: float
    w: float
    h: float


@dataclass
class View:
    page: int = 1
    zoom: float = 1.0
    crop: bool = False
    follow: bool = True
    boxes: list[Box] = field(default_factory=list)
    status: str = ""


def synctex_boxes(tex: Path, pdf: Path, line: int, col: int = 0) -> list[Box]:
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
                boxes.append(Box(int(cur["Page"]), cur["h"], cur["v"] - cur["H"], cur["W"], cur["H"]))
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
        rows -= 1  # status line
        sys.stdout.write("\x1b[2J\x1b[H")
        if not self.pdf.exists():
            self._status(f"no PDF yet ({self.pdf.name}); compile from nvim with c in review mode")
            return
        from PIL import Image, ImageDraw

        cw, ch = cell_size()
        px_w, px_h = cols * cw, rows * ch
        v = self.view
        v.page = max(1, min(v.page, self.pages))
        boxes = [b for b in v.boxes if b.page == v.page]
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
            over = Image.new("RGBA", img.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(over)
            for b in boxes:
                r = (b.x * s - 3, b.y * s - 3, (b.x + b.w) * s + 3, (b.y + b.h * 1.35) * s + 3)
                d.rectangle(r, fill=(255, 220, 0, 70), outline=(230, 60, 0, 255), width=2)
            img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")
        if (v.crop or v.zoom > 1.0) and img.width > px_w:
            # window of pane size centred on the highlight (or the page top)
            if boxes:
                cx = sum(b.x + b.w / 2 for b in boxes) / len(boxes) * s
                cy = sum(b.y + b.h / 2 for b in boxes) / len(boxes) * s
            else:
                cx, cy = img.width / 2, px_h / 2
            x0 = int(max(0, min(img.width - px_w, cx - px_w / 2)))
            y0 = int(max(0, min(img.height - px_h, cy - px_h / 2)))
            img = img.crop((x0, y0, x0 + px_w, y0 + px_h))
        elif boxes and img.height > px_h:
            # fit-width page taller than the pane: scroll so the highlight is visible
            cy = sum(b.y for b in boxes) / len(boxes) * s
            y0 = int(max(0, min(img.height - px_h, cy - px_h / 3)))
            img = img.crop((0, y0, img.width, y0 + px_h))
        elif img.height > px_h:
            img = img.crop((0, 0, img.width, px_h))
        img.save(png)
        subprocess.run(["kitten", "icat", "--clear", "--transfer-mode=stream", "--passthrough=tmux",
                        "--detection-timeout=2", "--place", f"{cols}x{rows}@0x0", "--scale-up",
                        "--align=left", str(png)], stdin=subprocess.DEVNULL)
        flags = " ".join(x for x in ("follow" if v.follow else "", "crop" if v.crop else "",
                                     f"zoom {v.zoom:.1f}" if v.zoom != 1 else "") if x)
        self._status(f"p.{v.page}/{self.pages}  {flags}  {v.status}")

    def _status(self, text: str) -> None:
        _, rows = shutil.get_terminal_size()
        sys.stdout.write(f"\x1b[{rows};1H\x1b[2K\x1b[7m {text[:200]} \x1b[0m")
        sys.stdout.flush()

    # ---- events -------------------------------------------------------------------
    def goto_line(self, line: int, col: int = 0) -> None:
        boxes = synctex_boxes(self.tex, self.pdf, line, col)
        self.view.boxes = boxes
        if boxes:
            self.view.page = boxes[0].page
            self.view.status = f"l.{line}"
        else:
            self.view.status = f"l.{line}: no synctex match (recompile?)"

    def handle_msg(self, msg: dict) -> bool:
        if "line" in msg:
            if not self.view.follow and not msg.get("force"):
                return False
            self.goto_line(int(msg["line"]), int(msg.get("col", 0)))
            return True
        if "page" in msg:
            self.view.page = int(msg["page"])
            self.view.boxes = []
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
                # rebuilt: refresh synctex boxes for the same line
                if pane.view.status.startswith("l."):
                    ln = int(re.match(r"l\.(\d+)", pane.view.status).group(1))
                    pane.goto_line(ln)
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
