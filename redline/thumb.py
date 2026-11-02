"""First page of a paper's PDF as a picture, for the `redline full` list."""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import struct
import subprocess
import tempfile
import termios
from pathlib import Path

from . import compile as _compile
from .state import Project


def paper_pdf(proj: Project) -> Path | None:
    """The last clean build, else the last build at all."""
    for p in (_compile.view_pdf(proj), _compile.pdf_path(proj)):
        if p.exists():
            return p
    return None


def cell_px() -> tuple[int, int]:
    """Pixels per terminal cell (from the tty, else tmux, else 10x20)."""
    try:
        rows, cols, xp, yp = struct.unpack("HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
        if xp and yp and cols and rows:
            return max(1, xp // cols), max(1, yp // rows)
    except OSError:
        pass
    from .preview import cell_size
    return cell_size()


def first_page(pdf: Path, width_px: int):
    """Page 1 as a PIL image about width_px wide (None when it cannot be rendered)."""
    from PIL import Image
    tmp = Path(tempfile.mkdtemp(prefix="redline-thumb-"))
    try:
        out = tmp / "p.ppm"
        dpi = str(max(30, min(200, width_px * 72 // 612 + 1)))
        if shutil.which("mutool"):
            subprocess.run(["mutool", "draw", "-q", "-r", dpi, "-o", str(out), str(pdf), "1"],
                           capture_output=True, timeout=60)
        else:
            out = tmp / "p.png"
            subprocess.run(["pdftoppm", "-f", "1", "-l", "1", "-r", dpi, "-png", "-singlefile", str(pdf),
                            str(out.with_suffix(""))], capture_output=True, timeout=60)
        if not out.exists():
            return None
        img = Image.open(out).convert("RGB")
        img.load()
        return img
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def fit(aspect: float, cols: int, rows: int, cw: int, ch: int) -> tuple[int, int]:
    """Largest cols x rows (cells) of the page's aspect (width/height) inside the box."""
    r = min(rows, int(cols * cw / (aspect * ch)))
    c = min(cols, max(1, round(r * ch * aspect / cw)))
    return max(1, c), max(1, r)
