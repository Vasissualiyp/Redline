"""Read-only access to the paper's analysis / plotting code (`code_dir`), for the agent.

`code_dir` used to be only the directory of the "code" tmux window: agents were never told
where it is, so "where is the plotting code?" had no answer, and a path outside the paper is
often outside the agent's sandbox too. These functions (the MCP tools code_dir / code_list /
code_read / code_search) show it, whatever the sandbox says. Nothing here can write, and
nothing can leave the directory (paths are resolved first, symlinks included).
"""
from __future__ import annotations

import re
from pathlib import Path

from .model import RedlineError
from .state import Project

TEXT_EXT = {".py", ".ipynb", ".sh", ".bash", ".txt", ".md", ".rst", ".yaml", ".yml", ".toml", ".json", ".cfg", ".ini",
            ".csv", ".tsv", ".c", ".h", ".cpp", ".hpp", ".f90", ".f", ".jl", ".m", ".r", ".tex", ".slurm", ".sbatch",
            ".params", ".mplstyle", ".dat", ".in", ".out", ".log"}
SKIP_DIRS = {"__pycache__", ".git", ".ipynb_checkpoints", "node_modules", ".venv", "venv", ".mypy_cache"}
MAX_BYTES = 1_500_000
MAX_LINES = 600


def is_dir_within(p: Path, seconds: float) -> bool | None:
    """Path.is_dir(), but None when it does not answer in time: a stale sshfs/NFS mount (cluster
    scratch) blocks any access forever, and must not freeze `redline open` or an agent call."""
    import threading
    out: list[bool] = []
    t = threading.Thread(target=lambda: out.append(p.is_dir()), daemon=True)  # daemon: never blocks exit
    t.start()
    t.join(seconds)
    return out[0] if out else None


def root(proj: Project) -> Path:
    d = (proj.cfg.code_dir or "").strip()
    if not d:
        raise RedlineError("this paper has no code_dir (set it: redline full set PAPER code_dir=/path/to/analysis, "
                           "or `e` in the redline full list)")
    p = Path(d).expanduser()
    ok = is_dir_within(p, 6)
    if ok is None:
        raise RedlineError(f"code_dir {p} does not answer within 6 s (a network mount that is down or stale? "
                           "`ls` on it would hang too; remount it)")
    if not ok:
        raise RedlineError(f"code_dir {p} does not exist (or is not mounted on this machine)")
    return p.resolve()


def _inside(base: Path, rel: str) -> Path:
    p = (base / rel).resolve()
    if p != base and base not in p.parents:
        raise RedlineError(f"{rel}: outside the code directory")
    return p


def _files(base: Path, glob: str = "**/*"):
    for f in sorted(base.glob(glob)):
        if not f.is_file() or any(part in SKIP_DIRS or part.startswith(".") for part in f.relative_to(base).parts):
            continue
        if f.suffix.lower() in TEXT_EXT and f.stat().st_size <= MAX_BYTES:
            yield f


def overview(proj: Project) -> str:
    """Where the code is, and a first look: top-level entries and the plotting scripts."""
    base = root(proj)
    top = sorted(base.iterdir())
    dirs = [p.name + "/" for p in top if p.is_dir() and p.name not in SKIP_DIRS and not p.name.startswith(".")]
    files = [p.name for p in top if p.is_file() and not p.name.startswith(".")]
    plots = [str(f.relative_to(base)) for f in _files(base) if f.suffix == ".py" and re.search(r"plot|fig", f.name, re.I)]
    out = [f"code_dir: {base}", f"top level: {', '.join(dirs + files[:40]) or '(empty)'}"]
    if plots:
        out.append(f"plotting scripts ({len(plots)}): " + ", ".join(plots[:40]) + (" ..." if len(plots) > 40 else ""))
    out.append("read with code_list(glob) / code_read(path, first_line, last_line) / code_search(query)")
    return "\n".join(out)


def listing(proj: Project, glob: str = "**/*", limit: int = 200) -> str:
    base = root(proj)
    if ".." in glob or glob.startswith("/"):
        raise RedlineError("glob must be relative to the code directory")
    rows = []
    for f in _files(base, glob):
        rows.append(f"{f.relative_to(base)}  ({f.stat().st_size} bytes)")
        if len(rows) >= limit:
            rows.append(f"... stopped at {limit} files; narrow the glob (e.g. '**/plot*.py')")
            break
    return "\n".join(rows) or "no matching text files"


def read(proj: Project, path: str, first: int | None = None, last: int | None = None) -> str:
    base = root(proj)
    f = _inside(base, path)
    if not f.is_file():
        raise RedlineError(f"{path}: not a file (code_list shows what exists)")
    if f.stat().st_size > MAX_BYTES * 2:
        raise RedlineError(f"{path}: too large to read here")
    lines = f.read_text(errors="replace").splitlines()
    a = max(1, first or 1)
    b = min(len(lines), last or a + MAX_LINES - 1)
    if b - a + 1 > MAX_LINES:
        b = a + MAX_LINES - 1
    head = f"{f.relative_to(base)}  lines {a}-{b} of {len(lines)}"
    return head + "\n" + "\n".join(f"{n:5d}  {lines[n - 1]}" for n in range(a, b + 1))


def search(proj: Project, query: str, glob: str = "**/*", regex: bool = False, limit: int = 60) -> str:
    base = root(proj)
    if not query:
        raise RedlineError("give a search text")
    pat = re.compile(query if regex else re.escape(query), re.I)
    hits = []
    for f in _files(base, glob):
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for n, ln in enumerate(lines, 1):
            if pat.search(ln):
                hits.append(f"{f.relative_to(base)}:{n}: {ln.strip()[:200]}")
                if len(hits) >= limit:
                    return "\n".join(hits) + f"\n... stopped at {limit} matches; narrow the search"
    return "\n".join(hits) or "no matches"
