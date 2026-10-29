"""Overleaf sync through overleaf-sync (`ols`), with a real 3-way merge.

pull:  download the project into .redline/remote, merge every text file with
       `git merge-file local base remote`, then remember remote as the new base.
push:  pull+merge first (so remote edits are never overwritten), lint, compile,
       run the pre_push hook, then `ols -l`.
"""
from __future__ import annotations

import filecmp
import fnmatch
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import compile as _compile
from .lint import lint
from .model import RedlineError
from .state import Project, merge3

TEXT_EXT = {".tex", ".bib", ".cls", ".sty", ".bst", ".txt"}
IGNORE_ENTRIES = [".redline", ".redline/*", ".redline.toml", ".mcp.json", "opencode.json"]


@dataclass
class SyncReport:
    merged: list[str] = field(default_factory=list)       # files changed by the merge
    conflicts: dict[str, list[int]] = field(default_factory=dict)  # file -> conflict lines
    added: list[str] = field(default_factory=list)        # new remote files copied
    skipped: list[str] = field(default_factory=list)      # binary files that differ
    pushed: bool = False
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()

    def text(self) -> str:
        out = []
        if self.merged:
            out.append("merged remote changes into: " + ", ".join(self.merged))
        for f, lines in self.conflicts.items():
            out.append(f"CONFLICTS in {f} at lines {lines[:20]} (<<<<<<< markers left in the file)")
        if self.added:
            out.append("new files from Overleaf: " + ", ".join(self.added[:20]))
        if self.skipped:
            out.append("binary files differing from Overleaf (kept local): " + ", ".join(self.skipped[:10]))
        if not (self.merged or self.conflicts or self.added):
            out.append("no remote changes")
        out.extend(self.messages)
        if self.pushed:
            out.append("pushed to Overleaf")
        return "\n".join(out)


def ensure_ignores(root: Path) -> list[str]:
    """Keep redline's files out of Overleaf and git (olssync.sh copies .gitignore over .olignore)."""
    changed = []
    for name in (".olignore", ".gitignore"):
        p = root / name
        lines = p.read_text().splitlines() if p.exists() else []
        missing = [e for e in IGNORE_ENTRIES if e not in lines]
        if missing:
            p.write_text("\n".join(lines + ["# redline"] + missing) + "\n")
            changed.append(name)
    return changed


def _ols(proj: Project) -> list[str]:
    cmd = shlex.split(proj.cfg.ols)
    if shutil.which(cmd[0]):
        return cmd
    flake = Path.home() / "Software/Overleaf-nix-flake"
    if flake.exists() and shutil.which("nix"):
        return ["nix", "develop", str(flake), "--command", *cmd]
    raise RedlineError(f"{cmd[0]!r} not found; set `ols` in .redline.toml")


def _run_ols(proj: Project, args: list[str], cwd: Path, timeout: int = 600) -> str:
    cookie = None
    for c in (proj.cfg.root / ".olauth", Path.home() / ".olauth"):
        if c.exists():
            cookie = c
            break
    if cookie and cwd != proj.cfg.root and not (cwd / ".olauth").exists():
        shutil.copy(cookie, cwd / ".olauth")
    cmd = _ols(proj) + ["-n", proj.cfg.project_name] + args
    env = _clean_env()
    try:
        r = subprocess.run(cmd, cwd=cwd, input="y\n" * 50, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RedlineError(f"`{' '.join(cmd)}` timed out")
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise RedlineError(f"`{' '.join(cmd)}` failed ({r.returncode}):\n{out[-2000:]}")
    return out


def _clean_env() -> dict:
    import os
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # never leak our interpreter's paths into ols
    return env


def _ignored(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(Path(rel).name, p) for p in patterns)


def download(proj: Project) -> Path:
    remote = proj.dir / "remote"
    if remote.exists():
        shutil.rmtree(remote)
    remote.mkdir(parents=True)
    _run_ols(proj, ["-r", "-p", "."], cwd=remote)
    (remote / ".olauth").unlink(missing_ok=True)
    return remote


def _files(d: Path) -> list[str]:
    skip = {".redline", ".git"}
    return sorted(str(p.relative_to(d)) for p in d.rglob("*")
                  if p.is_file() and not skip & set(p.relative_to(d).parts)
                  and p.name != ".olauth")


def init_base(proj: Project) -> SyncReport:
    """Take a fresh Overleaf snapshot as the merge base (first run)."""
    remote = download(proj)
    base = proj.dir / "base"
    if base.exists():
        shutil.rmtree(base)
    shutil.copytree(remote, base)
    rep = SyncReport()
    root = proj.cfg.root
    for rel in _files(remote):
        if Path(rel).suffix in TEXT_EXT and (root / rel).exists():
            if not filecmp.cmp(root / rel, remote / rel, shallow=False):
                rep.messages.append(f"{rel}: local differs from Overleaf (kept local; "
                                    "the next pull merges only newer remote edits)")
    rep.messages.append("base snapshot initialised from Overleaf")
    return rep


def pull(proj: Project) -> SyncReport:
    base = proj.dir / "base"
    if not base.exists():
        raise RedlineError("no merge base yet: run `redline sync init` once (or the MCP tool "
                           "sync_init) right after a plain `ols` pull, so redline knows what "
                           "Overleaf looked like")
    remote = download(proj)
    root = proj.cfg.root
    rep = SyncReport()
    olignore = (root / ".olignore").read_text().splitlines() if (root / ".olignore").exists() else []
    for rel in _files(remote):
        if _ignored(rel, olignore):
            continue
        r, l, b = remote / rel, root / rel, base / rel
        if not l.exists():
            if b.exists():
                rep.messages.append(f"{rel}: deleted locally, changed or kept on Overleaf (left deleted)")
                continue
            l.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(r, l)
            rep.added.append(rel)
            continue
        if filecmp.cmp(l, r, shallow=False):
            continue
        if Path(rel).suffix not in TEXT_EXT:
            if b.exists() and filecmp.cmp(l, b, shallow=False):
                shutil.copy2(r, l)  # only Overleaf changed it
                rep.merged.append(rel)
            else:
                rep.skipped.append(rel)
            continue
        if b.exists() and filecmp.cmp(r, b, shallow=False):
            continue  # Overleaf unchanged since base; local edits win
        base_text = b.read_text() if b.exists() else r.read_text()
        merged, n = merge3(l.read_text(), base_text, r.read_text())
        if l.resolve() == proj.main.resolve():
            proj.flush_editor()
            cur = proj.read()
            merged, n = merge3(cur, base_text, r.read_text())
            proj.write(merged, expect=cur, reason="merge from Overleaf")
        else:
            l.write_text(merged)
        rep.merged.append(rel)
        if n:
            rep.conflicts[rel] = [i + 1 for i, ln in enumerate(merged.splitlines())
                                  if ln.startswith("<<<<<<<")]
    # remote is the new base (conflicted files keep the old base so they merge again)
    for rel in _files(remote):
        if rel in rep.conflicts or _ignored(rel, olignore):
            continue
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(remote / rel, base / rel)
    return rep


def push(proj: Project, force: bool = False, skip_pull: bool = False) -> SyncReport:
    ensure_ignores(proj.cfg.root)
    rep = SyncReport() if skip_pull else pull(proj)
    if rep.conflicts:
        rep.messages.append("not pushing: resolve the merge conflicts first")
        return rep
    errors = [i for i in lint(proj.doc()) if i.severity == "error"]
    if errors and not force:
        rep.messages.append("not pushing: lint errors (run `redline lint --fix`):\n"
                            + "\n".join(f"  l.{i.line} {i.code}: {i.message}" for i in errors[:10]))
        return rep
    res = _compile.run(proj)
    if not res.ok and not force:
        rep.messages.append("not pushing: compile failed\n" + _compile.format_report(res))
        return rep
    if proj.cfg.pre_push:
        r = subprocess.run(proj.cfg.pre_push, shell=True, cwd=proj.cfg.root,
                           capture_output=True, text=True, env=_clean_env())
        if r.returncode != 0:
            rep.messages.append(f"not pushing: pre_push failed:\n{(r.stdout + r.stderr)[-1500:]}")
            return rep
    proj.flush_editor()
    out = _run_ols(proj, ["-l"], cwd=proj.cfg.root)
    rep.pushed = True
    rep.messages.append(out[-500:] if out else "")
    # what we pushed is what Overleaf now has
    base = proj.dir / "base"
    root = proj.cfg.root
    olignore = (root / ".olignore").read_text().splitlines() if (root / ".olignore").exists() else []
    for rel in _files(root):
        if Path(rel).suffix in TEXT_EXT and not _ignored(rel, olignore):
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, base / rel)
    return rep
