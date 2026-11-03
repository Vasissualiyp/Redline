"""All your papers in one place: `redline full`.

A paper is any directory directly under one of the roots that has a
.redline.toml. Roots are listed in ~/.config/redline/config.toml:

    roots = ["~/Software/Overleaf-nix-flake"]   # default
    papers_dir = "~/Software/Overleaf-nix-flake"  # where new papers go (default: the first root)
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import config, overleaf, preamble, sync
from .model import RedlineError
from .state import Project

DEFAULT_ROOT = "~/Software/Overleaf-nix-flake"

BLANK_PAPER = r"""\documentclass{article}
\usepackage[markup=default,commentmarkup=footnote,authormarkup=none,commandnameprefix=ifneeded]{changes}

\title{%(title)s}
\author{}
\date{\today}

\begin{document}
\maketitle

\begin{abstract}
\end{abstract}

\section{Introduction}

\end{document}
"""


def _global() -> dict:
    try:
        return tomllib.loads(config.GLOBAL_CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def roots() -> list[Path]:
    rs = _global().get("roots") or [DEFAULT_ROOT]
    return [Path(r).expanduser() for r in rs]


def papers_dir() -> Path:
    p = _global().get("papers_dir")
    return Path(p).expanduser() if p else roots()[0]


def scan() -> list[Path]:
    seen, out = set(), []
    for r in roots():
        if not r.is_dir():
            continue
        for d in sorted(r.iterdir()):
            if d.is_dir() and (d / config.CONFIG_NAME).exists() and d.resolve() not in seen:
                seen.add(d.resolve())
                out.append(d)
    return out


def session_name(root: Path) -> str:
    return "redline-" + re.sub(r"[^A-Za-z0-9_-]+", "-", root.name).strip("-")


def sessions() -> set[str]:
    r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True)
    return set(r.stdout.split()) if r.returncode == 0 else set()


@dataclass
class Info:
    name: str
    path: Path
    pending: int | None = None   # tracked changes waiting for a decision
    unrated: int | None = None   # of those, without a %Y/%N/%X marker
    sync: str = ""               # "in sync" | "local edits" | "no base"
    running: bool = False
    active: float = 0.0          # unix time of the last edit
    error: str = ""
    category: str = ""
    notes: str = ""
    strict: bool = True          # refcheck enforcement
    pdf_session: bool = False    # a two-session paper whose PDF session is running

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["path"] = str(self.path)
        return d


def ago(t: float) -> str:
    if not t:
        return "-"
    s = max(0, time.time() - t)
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago" if unit != "min" else f"{int(s // n)} min ago"
    return "just now"


def info(root: Path, running: set[str] | None = None) -> Info:
    it = Info(root.name, root)
    live = sessions() if running is None else running
    it.running = session_name(root) in live
    it.pdf_session = session_name(root) + "-pdf" in live
    try:
        proj = Project.open(root)
        it.name = proj.cfg.project_name  # the Overleaf project's name, as written there
        it.category, it.notes, it.strict = proj.cfg.category, proj.cfg.notes, proj.cfg.enforce_verification
        main = proj.cfg.main_path
        items = [i for i in proj.doc().items if i.kind != "comment"]
        it.pending = len(items)
        it.unrated = sum(1 for i in items if i.marker is None)
        base = proj.dir / "base" / proj.cfg.main
        if not base.exists():
            it.sync = "no base"
        else:
            it.sync = "in sync" if base.read_bytes() == main.read_bytes() else "local edits"
        stamps = [main.stat().st_mtime]
        hist = proj.dir / "history"
        if hist.exists():
            stamps.append(hist.stat().st_mtime)
        it.active = max(stamps)
    except (RedlineError, OSError) as e:
        it.error = str(e).splitlines()[0][:80]
    return it


def listing() -> list[Info]:
    running = sessions()
    return sorted((info(p, running) for p in scan()), key=lambda i: i.active, reverse=True)


def find(name: str) -> Path:
    paths = scan()
    def title(p: Path) -> str:
        try:
            return config.load(p).project_name
        except Exception:
            return p.name
    exact = [p for p in paths if p.name == name or title(p) == name or session_name(p) == name]
    if len(exact) == 1:
        return exact[0]
    low = name.lower()
    fuzzy = [p for p in paths if low in p.name.lower() or low in title(p).lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if not fuzzy and not exact:
        raise RedlineError(f"no paper matches {name!r}; `redline full list` shows them")
    raise RedlineError(f"{name!r} is ambiguous: " + ", ".join(p.name for p in (exact or fuzzy)))


# ---- new papers --------------------------------------------------------------------
def _dest(name: str, dest: Path | None) -> Path:
    # the directory carries the Overleaf name as is ("/" cannot be part of a directory name)
    dest = Path(dest).expanduser() if dest else papers_dir() / name.replace("/", "∕")
    if dest.exists() and any(dest.iterdir()):
        raise RedlineError(f"{dest} exists and is not empty")
    return dest


def _adopt(dest: Path, project: str, code_dir: str = "", verify: bool = True) -> Project:
    """.redline.toml, ignores and the MCP config for a downloaded or new paper."""
    config.write_default(dest, code_dir, project if project != dest.name else "", verify)
    sync.ensure_ignores(dest)
    proj = Project.open(dest)
    from .launcher import write_mcp_config
    write_mcp_config(proj)
    return proj


def pull_existing(name: str, dest: Path | None = None, code_dir: str = "", verify: bool = True) -> Path:
    """Download the Overleaf project `name` into a new paper directory."""
    dest = _dest(name, dest)
    dest.mkdir(parents=True, exist_ok=True)
    sync.fetch_project(name, dest)
    proj = _adopt(dest, name, code_dir, verify)
    sync.snapshot_base(proj)  # Overleaf's text is the base; the packages below are a local edit
    added = preamble.ensure(proj.cfg.main_path)
    if added:
        print(f"added to the preamble of {proj.cfg.main}: " + ", ".join(added))
    return dest


def create_blank(name: str, dest: Path | None = None, code_dir: str = "", verify: bool = True) -> Path:
    """Create an Overleaf project and a local paper with a starter main.tex, synced both ways."""
    dest = _dest(name, dest)
    overleaf.create(name)
    dest.mkdir(parents=True, exist_ok=True)
    sync.fetch_project(name, dest)  # the new project's own files: the merge base
    stage = dest / ".redline-base"
    stage.mkdir()
    for f in dest.iterdir():
        if f.name != stage.name and not f.name.startswith("."):
            (shutil.copytree if f.is_dir() else shutil.copy)(f, stage / f.name)
    (dest / "main.tex").write_text(BLANK_PAPER % {"title": name})
    proj = _adopt(dest, name, code_dir, verify)
    preamble.ensure(proj.cfg.main_path)
    base = proj.dir / "base"
    if base.exists():
        shutil.rmtree(base)
    shutil.move(str(stage), str(base))
    rep = sync.push(proj, force=True, skip_pull=True)
    if not rep.pushed:
        raise RedlineError(f"{dest} created, but the first push failed:\n{rep.text()}")
    return dest
