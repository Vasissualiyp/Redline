"""The paper on disk: reading, journaled writes, undo, origins, nvim hand-off.

Everything that changes the main file goes through `Project.transact()`:
re-read, compute edits on a fresh parse, write, journal pre/post snapshots.
When the reviewer's nvim is running (socket from config.socket_path) it is asked
to save its buffer first and to reload afterwards, so neither side clobbers
the other.
"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import config as _config
from .model import Document, RedlineError, TextEdit
from .ops import apply
from .parse import parse

JOURNAL_KEEP = 200


class Project:
    def __init__(self, cfg: _config.Config):
        self.cfg = cfg
        self.dir = cfg.state_dir
        self.dir.mkdir(exist_ok=True)
        (self.dir / "history").mkdir(exist_ok=True)

    @classmethod
    def open(cls, root: Path | str | None = None) -> "Project":
        return cls(_config.load(Path(root) if root else None))

    # ---- reading ---------------------------------------------------------
    @property
    def main(self) -> Path:
        return self.cfg.main_path

    def read(self) -> str:
        return self.main.read_text()

    def doc(self, text: str | None = None) -> Document:
        return parse(self.read() if text is None else text, self.cfg.legacy)

    # ---- sidecar state -------------------------------------------------------
    def _state_path(self) -> Path:
        return self.dir / "state.json"

    def state(self) -> dict:
        try:
            return json.loads(self._state_path().read_text())
        except (OSError, ValueError):
            return {"origins": {}}

    def save_state(self, st: dict) -> None:
        tmp = self._state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
        tmp.replace(self._state_path())

    def origins(self) -> dict[str, str]:
        return {k: v.get("origin", "manual") for k, v in self.state().get("origins", {}).items()}

    # ---- nvim coordination ---------------------------------------------------
    def _nvim(self, lua: str) -> str | None:
        sock = _config.socket_path(self.cfg.root, "nvim")
        if not sock.exists() or not shutil.which("nvim"):
            return None
        try:
            r = subprocess.run(["nvim", "--server", str(sock), "--remote-expr",
                                f'luaeval("{lua}")'], capture_output=True, text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else None
        except (subprocess.TimeoutExpired, OSError):
            return None

    def flush_editor(self) -> None:
        self._nvim("require'redline'.flush()")

    def reload_editor(self) -> None:
        self._nvim("require'redline'.reload()")

    # ---- writing -----------------------------------------------------------------
    def write(self, new: str, *, expect: str, reason: str, new_origin: str | None = None) -> None:
        cur = self.read()
        if cur != expect:
            raise _Stale()
        if new == cur:
            return
        self._journal(cur, new, reason)
        if new_origin:
            self._record_origins(cur, new, new_origin)
        self.main.write_text(new)  # in place: keeps the inode nvim is watching
        self.reload_editor()

    def transact(self, fn: Callable[[Document], list[TextEdit]], *, reason: str,
                 origin: str | None = None) -> tuple[Document, Document]:
        """Apply fn's edits to a fresh parse. Returns (before, after) documents."""
        self.flush_editor()
        for _ in range(3):
            text = self.read()
            before = self.doc(text)
            edits = fn(before)
            new = apply(text, edits)
            try:
                self.write(new, expect=text, reason=reason, new_origin=origin)
            except _Stale:
                continue
            return before, self.doc(new)
        raise RedlineError("the file kept changing while editing; try again")

    def _record_origins(self, old: str, new: str, origin: str) -> None:
        before = {it.id for it in self.doc(old).items}
        st = self.state()
        ts = _dt.datetime.now().isoformat(timespec="seconds")
        for it in self.doc(new).items:
            if it.id not in before:
                st.setdefault("origins", {})[it.id] = {"origin": origin, "created": ts}
        self.save_state(st)

    # ---- journal / undo --------------------------------------------------------
    def _journal(self, pre: str, post: str, reason: str) -> None:
        h = self.dir / "history"
        n = max([int(p.name.split("-")[0]) for p in h.glob("*-meta.json")] + [0]) + 1
        (h / f"{n:05d}-pre.tex").write_text(pre)
        (h / f"{n:05d}-post.tex").write_text(post)
        (h / f"{n:05d}-meta.json").write_text(json.dumps({
            "reason": reason, "time": _dt.datetime.now().isoformat(timespec="seconds")}))
        metas = sorted(h.glob("*-meta.json"))
        for old in metas[:-JOURNAL_KEEP]:
            k = old.name.split("-")[0]
            for p in h.glob(f"{k}-*"):
                p.unlink()

    def history(self, limit: int = 20) -> list[dict]:
        h = self.dir / "history"
        out = []
        for meta in sorted(h.glob("*-meta.json"), reverse=True)[:limit]:
            d = json.loads(meta.read_text())
            d["n"] = int(meta.name.split("-")[0])
            out.append(d)
        return out

    def undo(self) -> str:
        """Revert the most recent journaled write, keeping later unrelated edits."""
        h = self.dir / "history"
        metas = sorted(h.glob("*-meta.json"))
        if not metas:
            raise RedlineError("nothing to undo")
        k = metas[-1].name.split("-")[0]
        pre = (h / f"{k}-pre.tex").read_text()
        post = (h / f"{k}-post.tex").read_text()
        reason = json.loads(metas[-1].read_text())["reason"]
        self.flush_editor()
        cur = self.read()
        if cur == post:
            result = pre
        else:
            result, conflicts = merge3(cur, post, pre)
            if conflicts:
                raise RedlineError("the file changed since that edit in an overlapping place; "
                                   "undo it by hand")
        self.main.write_text(result)
        for p in h.glob(f"{k}-*"):
            p.unlink()
        self.reload_editor()
        return reason


class _Stale(Exception):
    pass


def merge3(ours: str, base: str, theirs: str, labels=("local", "base", "remote")) -> tuple[str, int]:
    """git merge-file: apply base->theirs onto ours. Returns (text, n_conflicts)."""
    with tempfile.TemporaryDirectory() as td:
        o, b, t = (Path(td) / n for n in ("ours", "base", "theirs"))
        o.write_text(ours)
        b.write_text(base)
        t.write_text(theirs)
        r = subprocess.run(["git", "merge-file", "-p", "-L", labels[0], "-L", labels[1],
                            "-L", labels[2], str(o), str(b), str(t)],
                           capture_output=True, text=True)
        if r.returncode < 0 or r.returncode > 127:
            raise RedlineError(f"git merge-file failed: {r.stderr.strip()}")
        return r.stdout, r.returncode
