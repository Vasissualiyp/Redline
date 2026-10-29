"""Overleaf sync through overleaf-sync (`ols`), with a real 3-way merge.

pull:  download the project into .redline/remote, merge every text file with
       `git merge-file local base remote`, then remember remote as the new base.
push:  pull+merge first (so remote edits are never overwritten), lint, compile,
       run the pre_push hook, then `ols -l`.
"""
from __future__ import annotations

import filecmp
import fnmatch
import json
import re
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
    # dotfiles (.git, .redline, .olignore, .olauth, ...) never live on Overleaf
    return sorted(str(p.relative_to(d)) for p in d.rglob("*")
                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(d).parts))


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
        if not _ignored(rel, olignore):
            _copy_if_different(root / rel, base / rel)
    return rep


# ---- "Sync to Overleaf": push if Overleaf is unchanged, otherwise merge ------
MERGE_MARKERS = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.M)

MERGE_PROMPT = """\
You are resolving a merge between two versions of a LaTeX paper.
Work ONLY inside this directory ({dir}); do not run ols, git or latexmk.

For each file listed below there are four versions:
  <file>         the merge result to fix: it contains git conflict blocks
                 (<<<<<<< local ... ======= ... >>>>>>> overleaf)
  <file>.LOCAL   our version (it may contain pending `changes`-package
                 suggestions: \\added \\deleted \\replaced \\chcomment)
  <file>.BASE    the version both sides started from (last sync)
  <file>.REMOTE  the Overleaf version (coauthors edit there, and the author may
                 have accepted/rejected suggestions in the Overleaf editor)

Files: {files}

Rules:
1. Edit <file> so that it contains no conflict markers and keeps the intent of
   BOTH sides. Compare each conflict against .BASE to see who changed what.
2. If Overleaf accepted or rejected a suggestion (markup gone there) that is still
   pending locally, follow Overleaf. If Overleaf edited text that is inside a
   pending local suggestion, keep the suggestion but use Overleaf's wording.
3. Never drop a \\chcomment, a `% VERIFIED`/`% VERIFY` line or a %Y/%N/%X marker
   that only one side has.
4. Keep braces balanced; do not reformat or rewrap unrelated text.
5. As your very last step, write a short list of what you decided per conflict
   to RESOLUTION.md (in an interactive session the window closes after that).
"""


@dataclass
class SyncCheck:
    # files where Overleaf has something the local copy does not
    remote_changed: list[str] = field(default_factory=list)
    local_changed: list[str] = field(default_factory=list)
    # text files changed on both sides (need a merge; see conflicts)
    both_changed: list[str] = field(default_factory=list)
    conflicts: dict[str, int] = field(default_factory=dict)    # trial merge: file -> conflict blocks
    # binary files changed on both sides: the local version is kept
    binary_kept: list[str] = field(default_factory=list)
    # Overleaf differed from the snapshot but holds nothing new (e.g. pushed
    # with plain `ols -l`); the snapshot was refreshed
    caught_up: list[str] = field(default_factory=list)

    @property
    def remote_unchanged(self) -> bool:
        return not self.remote_changed

    @property
    def local_edits(self) -> bool:
        """The local copy has something Overleaf does not."""
        return bool(self.local_changed or self.both_changed or self.binary_kept)

    @property
    def clean(self) -> bool:
        return not any(self.conflicts.get(f, 0) for f in self.both_changed)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["remote_unchanged"] = self.remote_unchanged
        d["clean"] = self.clean
        d["local_edits"] = self.local_edits
        return d

    def text(self) -> str:
        if self.remote_unchanged:
            s = "Overleaf has nothing new since the last sync"
            if self.local_changed:
                s += f"; local changes in {', '.join(self.local_changed)}"
        else:
            s = "Overleaf has new edits in: " + ", ".join(self.remote_changed)
            if self.both_changed:
                s += "\nchanged on both sides: " + ", ".join(
                    f"{f} ({self.conflicts.get(f, 0)} conflict block(s))" for f in self.both_changed)
        if self.binary_kept:
            s += "\nbinary files changed on both sides (local kept): " + ", ".join(self.binary_kept[:10])
        return s


def _same(a: Path, b: Path) -> bool:
    return a.exists() and b.exists() and filecmp.cmp(a, b, shallow=False)


def _copy_if_different(src: Path, dst: Path) -> None:
    if not _same(src, dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _ignore_patterns(root: Path) -> list[str]:
    pats: list[str] = []
    for name in (".olignore", ".gitignore"):
        p = root / name
        if p.exists():
            pats += [ln.strip() for ln in p.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    return pats


def check(proj: Project, reuse: bool = False) -> SyncCheck:
    """What Overleaf has that we don't. reuse: keep a download < 10 min old.

    A file only counts as changed on Overleaf if merging it would change the
    local copy; anything Overleaf merely caught up with (plain `ols -l`, plots
    uploaded by sync.sh) just refreshes the snapshot.
    """
    base = proj.dir / "base"
    if not base.exists():
        raise RedlineError("no merge base yet: run `redline sync init` once")
    remote = proj.dir / "remote"
    import time
    if not (reuse and remote.exists() and time.time() - remote.stat().st_mtime < 600):
        remote = download(proj)
    root = proj.cfg.root
    ignore = _ignore_patterns(root)
    chk = SyncCheck()
    proj.flush_editor()
    for rel in sorted(set(_files(remote)) | set(_files(base))):
        if _ignored(rel, ignore):
            continue
        r, b, l = remote / rel, base / rel, root / rel
        if not r.exists():
            continue  # deleted on Overleaf: never deletes locally
        if _same(r, b):
            if l.exists() and not _same(l, b):
                chk.local_changed.append(rel)
            continue
        # Overleaf differs from the snapshot
        if _same(r, l):
            chk.caught_up.append(rel)
            _copy_if_different(r, b)
            continue
        local_edited = l.exists() and b.exists() and not _same(l, b)
        if not local_edited:
            chk.remote_changed.append(rel)  # only Overleaf changed it: take it
            continue
        if Path(rel).suffix not in TEXT_EXT:
            chk.binary_kept.append(rel)
            continue
        local_text = l.read_text()
        merged, n = merge3(local_text, b.read_text(), r.read_text())
        if not n and merged == local_text:
            chk.caught_up.append(rel)  # Overleaf's edits are already here
            _copy_if_different(r, b)
            chk.local_changed.append(rel)
            continue
        chk.remote_changed.append(rel)
        chk.both_changed.append(rel)
        chk.conflicts[rel] = _count_blocks(merged) if n else 0
    for rel in _files(root):  # new local files (text or plots) are local edits too
        if not _ignored(rel, ignore) and not (base / rel).exists() and not (remote / rel).exists() \
                and rel not in chk.local_changed:
            chk.local_changed.append(rel)
    return chk


def _count_blocks(text: str) -> int:
    return len(re.findall(r"^<{7}( |$)", text, re.M))


def merge_dir(proj: Project) -> Path:
    return proj.dir / "merge"


def prepare_merge(proj: Project, chk: SyncCheck) -> list[str]:
    """Write the merge workspace for files changed on both sides. Returns their names."""
    d = merge_dir(proj)
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    root, base, remote = proj.cfg.root, proj.dir / "base", proj.dir / "remote"
    names = []
    for rel in chk.both_changed:
        local_text = (root / rel).read_text()
        base_text, remote_text = (base / rel).read_text(), (remote / rel).read_text()
        merged, _ = merge3(local_text, base_text, remote_text, labels=("local", "base", "overleaf"))
        name = rel.replace("/", "__")
        for suffix, text in (("", merged), (".LOCAL", local_text), (".BASE", base_text),
                             (".REMOTE", remote_text)):
            (d / (name + suffix)).write_text(text)
        names.append(name)
    (d / "files.json").write_text(json.dumps({n: rel for n, rel in zip(names, chk.both_changed)}))
    (d / "check.json").write_text(json.dumps(chk.__dict__))
    (d / "INSTRUCTIONS.md").write_text(MERGE_PROMPT.format(dir=d, files=", ".join(names)))
    return names


def run_agent(proj: Project, agent: str, stream=None) -> int:
    """Run a configured agent in the merge workspace; returns its exit code.

    Headless agents stream their output. Interactive ones (tui = true) get a
    tmux window "merge" in the current session; it is closed as soon as the
    workspace is resolved (no conflict markers left and RESOLUTION.md written).
    """
    from .config import agent_spec
    spec = proj.cfg.agents.get(agent)
    if not spec:
        raise RedlineError(f"unknown agent {agent!r}; configured: {', '.join(proj.cfg.agents)}")
    spec = agent_spec(spec)
    d = merge_dir(proj)
    names = json.loads((d / "files.json").read_text())
    prompt = MERGE_PROMPT.format(dir=d, files=", ".join(names))
    argv = [a.replace("{prompt}", prompt).replace("{dir}", str(d)) for a in spec["cmd"]]
    if not shutil.which(argv[0]):
        raise RedlineError(f"agent command {argv[0]!r} not found on PATH")
    if spec["tui"]:
        return _run_tui(proj, agent, argv, d, list(names), stream)
    proc = subprocess.Popen(argv, cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, env=_clean_env())
    try:
        for line in proc.stdout:
            if stream:
                stream(line)
        return proc.wait(timeout=proj.cfg.merge_timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RedlineError(f"agent {agent} timed out after {proj.cfg.merge_timeout}s")


def _resolved(d: Path, names: list[str]) -> bool:
    return (d / "RESOLUTION.md").exists() and all(
        not MERGE_MARKERS.search((d / n).read_text()) for n in names)


def _run_tui(proj: Project, agent: str, argv: list[str], d: Path, names: list[str], stream) -> int:
    import os
    import time
    say = stream or (lambda s: None)
    script = d / "run-agent.sh"
    script.write_text("#!/bin/sh\ncd " + shlex.quote(str(d)) + "\nexec " + shlex.join(argv) + "\n")
    if not os.environ.get("TMUX"):
        # no tmux: run in this terminal
        rc = subprocess.call(["sh", str(script)], env=_clean_env())
        return 0 if _resolved(d, names) else (rc or 1)
    pane = os.environ.get("TMUX_PANE", "")
    session = subprocess.run(["tmux", "display", "-p", "-t", pane, "#{session_id}"],
                             capture_output=True, text=True).stdout.strip()
    win = subprocess.run(["tmux", "new-window", "-P", "-F", "#{window_id}", "-t", f"{session}:",
                          "-n", "merge", "-c", str(d),
                          # login shell: the agent gets the user's exported API keys
                          f"{shlex.quote(os.environ.get('SHELL') or '/bin/sh')} -lc "
                          + shlex.quote("exec sh " + shlex.quote(str(script)))],
                         capture_output=True, text=True, env=_clean_env())
    wid = win.stdout.strip()
    if win.returncode != 0 or not wid:
        raise RedlineError(f"could not open the tmux window for {agent}: {win.stderr.strip()}")
    say(f"{agent} is merging interactively in tmux window 'merge' ({wid}); it closes by itself "
        "once the conflicts are resolved and RESOLUTION.md is written.\n")
    t0 = time.time()
    while time.time() - t0 < proj.cfg.merge_timeout:
        time.sleep(2)
        alive = subprocess.run(["tmux", "list-windows", "-a", "-F", "#{window_id}"],
                               capture_output=True, text=True).stdout.split()
        if _resolved(d, names):
            time.sleep(3)  # let the agent finish its last message
            if wid in alive:
                subprocess.run(["tmux", "kill-window", "-t", wid], capture_output=True)
            say("merge resolved\n")
            return 0
        if wid not in alive:
            say(f"the {agent} window was closed before the merge was resolved\n")
            return 1
    raise RedlineError(f"agent {agent} did not finish within {proj.cfg.merge_timeout}s "
                       "(its tmux window is still open)")


def finish_merge(proj: Project, chk: SyncCheck | None = None, use_git: bool = False) -> SyncReport:
    """Apply the resolved workspace (or a clean git merge) and adopt Overleaf as the base."""
    root, base, remote = proj.cfg.root, proj.dir / "base", proj.dir / "remote"
    d = merge_dir(proj)
    rep = SyncReport()
    files = json.loads((d / "files.json").read_text()) if (d / "files.json").exists() else {}
    if chk is not None:
        both = set(chk.both_changed)
        # files only Overleaf changed: take them as they are
        for rel in chk.remote_changed:
            if rel in both:
                continue
            r, l = remote / rel, root / rel
            if not r.exists():
                rep.messages.append(f"{rel}: deleted on Overleaf (kept locally)")
                continue
            l.parent.mkdir(parents=True, exist_ok=True)
            if l.resolve() == proj.main.resolve():
                cur = proj.read()
                proj.write(r.read_text(), expect=cur, reason="take Overleaf version")
            else:
                shutil.copy2(r, l)
            rep.merged.append(rel)
    resolved: dict[str, str] = {}
    for name, rel in files.items():
        if use_git:
            text, n = merge3((root / rel).read_text(), (base / rel).read_text(), (remote / rel).read_text())
            if n:
                raise RedlineError(f"{rel}: git cannot merge it cleanly ({n} conflicts); pick an agent")
        else:
            text = (d / name).read_text()
            if MERGE_MARKERS.search(text):
                raise RedlineError(f"{rel}: conflict markers are still in {d / name}; nothing applied")
        from .lint import balanced
        if Path(rel).suffix == ".tex" and not balanced(text):
            raise RedlineError(f"{rel}: the resolved file has unbalanced braces; nothing applied")
        resolved[rel] = text
    for rel, text in resolved.items():
        l = root / rel
        snapshot = (d / (rel.replace("/", "__") + ".LOCAL"))
        cur = l.read_text()
        if snapshot.exists() and cur != snapshot.read_text():
            # edited while the agent worked: replay those edits on top
            text, n = merge3(cur, snapshot.read_text(), text)
            if n:
                rep.conflicts[rel] = [i + 1 for i, ln in enumerate(text.splitlines()) if ln.startswith("<<<<<<<")]
        if l.resolve() == proj.main.resolve():
            proj.write(text, expect=cur, reason="merge with Overleaf")
        else:
            l.write_text(text)
        rep.merged.append(rel)
    if not rep.conflicts:
        for rel in _files(remote):
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(remote / rel, base / rel)
    res = d / "RESOLUTION.md"
    if res.exists():
        rep.messages.append("agent notes:\n" + res.read_text().strip())
    return rep


def load_check(proj: Project) -> SyncCheck:
    p = merge_dir(proj) / "check.json"
    if not p.exists():
        raise RedlineError("no prepared merge; run the sync check first")
    return SyncCheck(**json.loads(p.read_text()))
