"""The quick menu behind `redline full`: review a paper's changes without opening its editor.

Accepting and rejecting go through the same journaled edit as everywhere else (undoable,
an open nvim reloads the file). Talking to the agent needs the paper's tmux session; it
is started in the background the first time it is needed (when you begin writing a
message) and then reused, so `redline full` shows it as running.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import config, registry
from .model import RedlineError
from .service import Service, describe
from .state import Project


# ---- reading and deciding --------------------------------------------------------
def list_changes(proj: Project) -> list[dict]:
    """Every pending change and comment of the paper, in file order."""
    doc = proj.doc()
    org = proj.origins()
    return [describe(doc, it, org, 0) for it in doc.items]


def accept(proj: Project, cid: str) -> str:
    return Service(proj).resolve("accept", {"ids": [cid]})


def reject(proj: Project, cid: str) -> str:
    return Service(proj).resolve("reject", {"ids": [cid]})


def undo(proj: Project) -> str:
    return Service(proj).undo()


def plain(tex: str, n: int = 200) -> str:
    s = re.sub(r"\\[A-Za-z]+\*?", " ", tex or "")
    return " ".join(re.sub(r"[{}$^_\\~]", " ", s).split())[:n]


def excerpt(d: dict) -> str:
    """Words to look for in the PDF: what the change put there (or took away)."""
    return plain(d.get("new") or d.get("old") or d.get("comment") or "")


# ---- messages for the agent ------------------------------------------------------
def _about(d: dict) -> str:
    out = [f"- {d['label']} id {d['id']} ({d['kind']}, §{d['section']}, l.{d['lines'][0]})"]
    if d.get("new"):
        out.append("    new: «" + plain(d["new"], 240) + "»")
    if d.get("old"):
        out.append("    old: «" + plain(d["old"], 160) + "»")
    if d.get("comment") and d["kind"] != "comment":
        out.append("    comment: «" + plain(d["comment"], 200) + "»")
    return "\n".join(out)


def note_message(d: dict, note: str) -> str:
    return ("[redline] Review note: I marked this pending change with %N and a note. Address it with "
            "revise_change(id, new_text?, comment?) (that clears the %N so I re-review it), or answer with "
            "revise_change(id, comment=\"Kept: <why>\") if you disagree. Do any checks yourself, compile() "
            f"once at the end, then give me one line.\n\n{_about(d)}\n    note: «{note}»")


def ask_message(d: dict, question: str) -> str:
    return (f"[redline] About this pending change (do not edit anything unless I ask):\n{_about(d)}\n\n{question}")


# ---- the paper's tmux session ----------------------------------------------------
def session(root: Path) -> str:
    return registry.session_name(root)


def alive(root: Path) -> bool:
    return session(root) in registry.sessions()


def ensure_session(root: Path) -> str:
    """Start the paper's workspace in the background if it is not running; its name."""
    if not alive(root):
        from .launcher import open_workspace
        with contextlib.redirect_stdout(io.StringIO()):
            rc = open_workspace(root, attach=False)
        if rc:
            raise RedlineError("could not start the tmux session")
    return session(root)


def agent_pane(name: str) -> str | None:
    out = subprocess.run(["tmux", "list-panes", "-s", "-t", f"={name}", "-F", "#{pane_id} #{@redline_role}"],
                         capture_output=True, text=True).stdout
    for ln in out.splitlines():
        pid, _, role = ln.partition(" ")
        if role == "agent":
            return pid
    return None


def wait_ready(pane: str, timeout: float = 30.0) -> bool:
    """A freshly started agent takes a while to show its prompt: wait until its screen settles."""
    last, calm, end = None, 0, time.time() + timeout
    while time.time() < end:
        shot = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane], capture_output=True, text=True).stdout
        if shot.strip() and shot == last:
            calm += 1
            if calm >= 2:
                return True
        else:
            calm = 0
        last = shot
        time.sleep(1.0)
    return False


def send_to_agent(root: Path, text: str) -> str:
    """Paste text into the agent pane (bracketed, like the editor does) and submit it."""
    name = ensure_session(root)
    pane = agent_pane(name)
    if not pane:
        raise RedlineError("the session has no agent pane")
    wait_ready(pane)
    subprocess.run(["tmux", "load-buffer", "-b", "redline-quick", "-"], input=text, text=True, check=True)
    subprocess.run(["tmux", "paste-buffer", "-p", "-d", "-b", "redline-quick", "-t", pane], check=True)
    time.sleep(0.2)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
    return name


def mark_note(proj: Project, cid: str, note: str) -> str:
    return Service(proj).set_marker(cid, "N", note)


def goto(root: Path, line: int) -> None:
    """Best effort: put the paper nvim's cursor on the line and show its window."""
    sock = config.socket_path(root, "nvim")
    if not sock.exists() or not shutil.which("nvim"):
        return
    try:
        main = Project.open(root).main
        expr = f"luaeval('require(\"redline\").goto_source(_A[1], _A[2])', [{json.dumps(str(main))}, {int(line)}])"
        subprocess.run(["nvim", "--server", str(sock), "--remote-expr", expr], capture_output=True, timeout=5)
        subprocess.run(["tmux", "select-window", "-t", f"={session(root)}:paper"], capture_output=True)
    except (OSError, subprocess.TimeoutExpired, RedlineError):
        pass


# ---- the PDF page of a change ----------------------------------------------------
def change_pane(proj: Project):
    from .preview import Pane
    pane = Pane(proj, tmp=proj.dir / "quick", draw=False)
    pane.check_pdf()
    return pane


def change_image(pane, d: dict, px_w: int, px_h: int):
    """The page(s) holding the change with it highlighted: a PIL image, or a string saying why not."""
    if not pane.pdf.exists():
        return "no PDF yet: press c to compile"
    pane.check_pdf()
    pane.view.follow = True
    pane.goto_line(d["lines"][0], 0, excerpt(d), d["lines"][1])
    v = pane.view
    if not v.rects:
        return v.status or "not found in the PDF (recompile?)"
    img = pane.compose(v, px_w, px_h)
    return img if img is not None else "cannot render the page"
