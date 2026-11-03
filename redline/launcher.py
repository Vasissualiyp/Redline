"""tmux workspace + per-agent MCP configuration.

Window "paper":  nvim on the paper (left) | agent (top right) / PDF preview (bottom right)
Window "code":   nvim in code_dir | shell
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as _config
from . import sync as _sync
from . import agentpick
from .state import Project

REPO = Path(__file__).resolve().parent.parent


def nvim_dir() -> Path:
    """The nvim plugin: in the checkout, or $out/share/nvim/redline when installed."""
    here = Path(__file__).resolve()
    for cand in (REPO / "nvim", here.parents[4] / "share" / "nvim" / "redline"):
        if (cand / "lua" / "redline" / "init.lua").exists():
            return cand
    return REPO / "nvim"


def redline_cmd() -> list[str]:
    """How other processes (agent, nvim, tmux panes) should start redline."""
    exe = shutil.which("redline")
    if exe and "nix/store" in os.path.realpath(exe):
        return [exe]
    if (REPO / "redline" / "__init__.py").exists():
        # running from a checkout: this interpreter has the dependencies
        return [sys.executable, "-m", "redline"]
    return [exe or "redline"]


def _env_prefix() -> str:
    """Shell prefix that starts redline with the right Python path."""
    return "env -u PYTHONPATH " + (f"PYTHONPATH={shlex.quote(str(REPO))} "
                                   if redline_cmd()[0] == sys.executable else "")


def _env() -> dict[str, str]:
    env = {"PYTHONPATH": str(REPO) if redline_cmd()[0] == sys.executable else ""}
    return env


def mcp_servers(proj: Project) -> dict:
    cmd = redline_cmd()
    servers = {"redline": {"command": cmd[0], "args": cmd[1:] + ["mcp"],
                           "env": {**_env(), "REDLINE_ROOT": str(proj.cfg.root)}}}
    for name, c in proj.cfg.mcp_extra.items():
        if not c:
            continue
        entry = {"command": c[0], "args": list(c[1:])}
        if name == "zotero":
            entry["env"] = {"ZOTERO_LOCAL": "true"}
        servers[name] = entry
    return servers


def mcp_config(proj: Project, kind: str | None = None) -> dict:
    servers = mcp_servers(proj)
    if (kind or proj.cfg.agent_kind) == "opencode":
        return {"$schema": "https://opencode.ai/config.json",
                "mcp": {n: {"type": "local", "command": [s["command"], *s["args"]],
                            "environment": s.get("env", {}), "enabled": True}
                        for n, s in servers.items()}}
    return {"mcpServers": servers}


def _merge_json(path: Path, update) -> None:
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except ValueError:
            data = {}
    update(data)
    path.write_text(json.dumps(data, indent=2) + "\n")


def write_mcp_config(proj: Project) -> str:
    """Merge our servers into both agents' project configs (other entries are kept),
    so switching the agent pane between claude and opencode keeps the tools."""
    root = proj.cfg.root
    paths = []
    for kind, name, key in (("claude", ".mcp.json", "mcpServers"), ("opencode", "opencode.json", "mcp")):
        cfg = mcp_config(proj, kind)

        def upd(data, cfg=cfg, key=key):
            data.setdefault(key, {}).update(cfg[key])
            if "$schema" in cfg:
                data.setdefault("$schema", cfg["$schema"])
        _merge_json(root / name, upd)
        paths.append(str(root / name))
    return ", ".join(paths)


def _tmux(*args: str, check: bool = True) -> str:
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def _shell(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def in_login_shell(cmd: str) -> str:
    """Run a pane command through the user's login shell.

    Commands given to tmux directly skip the shell's startup files, so
    variables exported there (API keys for minuet, opencode, ...) would be
    missing in nvim and the agent panes.
    """
    sh = os.environ.get("SHELL") or "/bin/sh"
    return f"{shlex.quote(sh)} -lc {shlex.quote('exec ' + cmd)}"


def pdf_session_name(name: str) -> str:
    return name + "-pdf"


def _has_session(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", f"={name}"], capture_output=True).returncode == 0


def open_workspace(root: Path | None, attach: bool = True, pdf: bool = False, two: bool | None = None) -> int:
    """Start (or reach) the paper's tmux workspace. two: True/False overrides the paper's two_sessions setting."""
    root = _config.find_root(root)
    fresh = not (root / _config.CONFIG_NAME).exists()
    _config.write_default(root)
    _sync.ensure_ignores(root)
    proj = Project.open(root)
    if fresh:
        from . import preamble
        preamble.ensure(proj.cfg.main_path)
    write_mcp_config(proj)
    if not shutil.which("tmux"):
        print("tmux not found", file=sys.stderr)
        return 1

    name = "redline-" + re.sub(r"[^A-Za-z0-9_-]+", "-", root.name).strip("-")
    two = proj.cfg.two_sessions if two is None else two
    pdf_name = pdf_session_name(name)
    exists = _has_session(name)
    if not exists:
        rl = _shell(redline_cmd())
        env_prefix = _env_prefix()
        sock = _config.socket_path(root, "nvim")
        sock.unlink(missing_ok=True)
        nvim = _shell(["nvim", "--listen", str(sock), "--cmd", f"set rtp^={nvim_dir()}",
                       "-c", "lua require('redline').start()", str(proj.main)])
        # session environment so nvim's RPC job finds the same redline
        envs = ["-e", f"REDLINE_ROOT={root}", "-e", f"REDLINE_CMD={env_prefix}{rl}"]
        cols, rows = shutil.get_terminal_size((240, 60))
        editor = _tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name, "-n", "paper",
                       "-x", str(cols), "-y", str(rows), "-c", str(root), *envs, in_login_shell(nvim))
        paper = f"={name}:paper"
        agent = _tmux("split-window", "-h", "-l", "40%", "-t", editor, "-c", str(root),
                      "-P", "-F", "#{pane_id}", in_login_shell(agentpick.command(proj)))
        _tmux("set-option", "-p", "-t", agent, "@redline_role", "agent")
        if not two:  # one session: the PDF is a pane here; two sessions: it lives in its own, below
            prev = _tmux("split-window", "-v", "-l", "50%", "-t", agent, "-c", str(root),
                         "-P", "-F", "#{pane_id}", in_login_shell(f"{env_prefix}{rl} preview"))
            _tmux("set-option", "-p", "-t", prev, "@redline_role", "preview")
        _tmux("select-pane", "-t", editor)
        _tmux("set-option", "-p", "-t", editor, "@redline_role", "paper")
        code = proj.cfg.code_dir if proj.cfg.code_dir and Path(proj.cfg.code_dir).is_dir() else str(root)
        code_pane = _tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={name}:",
                          "-n", "code", "-c", code, in_login_shell("nvim ."))
        _tmux("split-window", "-h", "-l", "35%", "-t", code_pane, "-c", code)
        _tmux("select-window", "-t", paper)
        # the arrangement saved when this paper was last quit, else the preset
        restore_layout(name, root, proj.cfg.layout)
    if two and not _has_session(pdf_name) and not _has_preview_pane(name):
        _start_pdf_session(root, name, pdf_name)
    bind_quit_key(proj.cfg.tmux_quit_key)
    agentpick.bind_key(proj.cfg.tmux_model_key)
    target = pdf_name if (pdf and two) else name
    if not attach:
        print(name + (f" (PDF session: {pdf_name})" if two else ""))
        return 0
    if os.environ.get("TMUX"):
        _tmux("switch-client", "-t", f"={target}")
    else:
        os.execvp("tmux", ["tmux", "attach-session", "-t", f"={target}"])
    return 0


def _has_preview_pane(session: str) -> bool:
    out = subprocess.run(["tmux", "list-panes", "-s", "-t", f"={session}", "-F", "#{@redline_role}"],
                         capture_output=True, text=True).stdout.split()
    return "preview" in out


def _start_pdf_session(root: Path, name: str, pdf_name: str) -> None:
    """The second session of a two-session paper: only the PDF view, same preview process and socket
    as the pane would have, so the editor drives it exactly the same way."""
    rl = _shell(redline_cmd())
    envs = ["-e", f"REDLINE_ROOT={root}", "-e", f"REDLINE_CMD={_env_prefix()}{rl}"]
    cols, rows = shutil.get_terminal_size((240, 60))
    pane = _tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", pdf_name, "-n", "pdf",
                 "-x", str(cols), "-y", str(rows), "-c", str(root), *envs,
                 in_login_shell(f"{_env_prefix()}{rl} preview"))
    _tmux("set-option", "-p", "-t", pane, "@redline_role", "preview")
    # the two sessions know each other: quitting one closes both
    _tmux("set-option", "-t", f"={name}:", "@redline_pair", pdf_name)
    _tmux("set-option", "-t", f"={pdf_name}:", "@redline_pair", name)


# ---- back to the all-papers list ------------------------------------------------
FULL_SESSION = "redline-full"


def record_full_location() -> None:
    """Called by the `redline full` TUI: remember which tmux window it runs in."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return
    loc = subprocess.run(["tmux", "display", "-p", "-t", pane, "#{session_name}:#{window_id}"],
                         capture_output=True, text=True).stdout.strip()
    if loc:
        subprocess.run(["tmux", "set-option", "-g", "@redline_full", loc], capture_output=True)


def go_full() -> int:
    """Switch to the `redline full` list: its window if it is running, else a new `redline-full` session."""
    if not shutil.which("tmux"):
        print("tmux not found", file=sys.stderr)
        return 1
    loc = subprocess.run(["tmux", "show-option", "-gqv", "@redline_full"], capture_output=True, text=True).stdout.strip()
    windows = subprocess.run(["tmux", "list-windows", "-a", "-F", "#{session_name}:#{window_id}"],
                             capture_output=True, text=True).stdout.split()
    target = loc if loc and loc in windows else None
    if not target:
        if subprocess.run(["tmux", "has-session", "-t", f"={FULL_SESSION}"], capture_output=True).returncode != 0:
            cols, rows = shutil.get_terminal_size((200, 50))
            _tmux("new-session", "-d", "-s", FULL_SESSION, "-n", "full", "-x", str(cols), "-y", str(rows),
                  in_login_shell(f"{_env_prefix()}{_shell(redline_cmd())} full"))
        target = f"={FULL_SESSION}"
    if os.environ.get("TMUX"):
        _tmux("switch-client", "-t", target)
    else:
        os.execvp("tmux", ["tmux", "attach-session", "-t", target])
    return 0


# ---- quitting the whole workspace ------------------------------------------------
def _session_of_caller() -> str:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        raise RuntimeError("not inside tmux; pass --session")
    return _tmux("display", "-p", "-t", pane, "#{session_name}")


def _session_root(session: str) -> Path | None:
    r = subprocess.run(["tmux", "show-environment", "-t", f"={session}", "REDLINE_ROOT"],
                       capture_output=True, text=True)
    line = r.stdout.strip()
    return Path(line.split("=", 1)[1]) if r.returncode == 0 and "=" in line else None


def _pair(session: str) -> list[str]:
    """The other session of a two-session paper (empty for a normal one)."""
    r = subprocess.run(["tmux", "show-option", "-qv", "-t", f"={session}:", "@redline_pair"],
                       capture_output=True, text=True)
    other = r.stdout.strip()
    return [other] if other and _has_session(other) else []


def quit_status(session: str) -> dict:
    """What would be lost or interrupted by closing the session."""
    root = _session_root(session)
    windows = _tmux("list-windows", "-t", f"={session}", "-F", "#{window_name}").splitlines()
    for other in _pair(session):
        windows += [f"{w} (session {other})" for w in
                    _tmux("list-windows", "-t", f"={other}", "-F", "#{window_name}").splitlines()]
    st = {"session": session, "redline": root is not None, "windows": windows,
          "agent_running": "merge" in windows, "unpushed": [], "root": str(root or "")}
    if root is None:
        return st
    proj = Project.open(root)
    proj.flush_editor()
    base = proj.dir / "base"
    if base.exists():
        ignore = _sync._ignore_patterns(root)
        st["unpushed"] = [rel for rel in _sync._files(root)
                          if not _sync._ignored(rel, ignore) and not _sync._same(root / rel, base / rel)]
    return st


def _windows(session: str) -> dict[str, str]:
    """name -> window id; the window holding the paper pane is always "paper"."""
    from . import layout as _layout
    out = {}
    for line in _tmux("list-windows", "-t", f"={session}", "-F", "#{window_id} #{window_name}").splitlines():
        wid, _, name = line.partition(" ")
        out.setdefault(name, wid)
    try:
        out["paper"] = _layout.find_window(f"={session}")
    except Exception:
        pass
    return out


def save_layout(session: str, root: Path) -> None:
    """Remember the pane arrangement of the paper and code windows for this paper."""
    from . import layout as _layout
    saved = {}
    for name, wid in _windows(session).items():
        if name in ("paper", "code"):
            try:
                saved[name] = _layout.capture(wid)
            except Exception:
                continue
    if saved:
        (root / ".redline").mkdir(exist_ok=True)
        (root / ".redline" / "layout.json").write_text(json.dumps(saved, indent=1))


def restore_layout(session: str, root: Path, fallback: str) -> None:
    from . import layout as _layout
    path = root / ".redline" / "layout.json"
    saved = {}
    if path.exists():
        try:
            saved = json.loads(path.read_text())
        except ValueError:
            saved = {}
    wins = _windows(session)
    for name, wid in wins.items():
        if name in saved:
            try:
                _layout.restore(wid, saved[name])
                continue
            except Exception as e:
                print(f"saved {name} layout not restored: {e}", file=sys.stderr)
        if name == "paper" and fallback and fallback != "default":  # panes start as "default"
            try:
                _layout.apply(fallback, wid)
            except Exception as e:
                print(f"layout {fallback!r}: {e}", file=sys.stderr)


def quit_now(session: str) -> None:
    """Save, then close the session and, for a two-session paper, its partner too."""
    root = _session_root(session)
    pair = _pair(session)
    if root is not None:
        main = session
        if pair and session.endswith("-pdf"):  # asked from the PDF session: the editor session is the other one
            main = pair[0]
        try:
            save_layout(main, root)
        except Exception as e:  # never block quitting
            print(f"layout not saved: {e}", file=sys.stderr)
        Project.open(root).flush_editor()  # nvim writes its buffer first
    for other in pair:
        subprocess.run(["tmux", "kill-session", "-t", f"={other}"], capture_output=True)
    _tmux("kill-session", "-t", f"={session}")


def _tmux_arg(cmd: list[str]) -> str:
    """A shell command as one tmux argument (paths here never contain quotes)."""
    return "'" + " ".join(shlex.quote(c) for c in cmd).replace("'", "'\\''") + "'"


def quit_menu(session: str, client: str | None) -> None:
    """tmux menu that asks before closing every window of the session."""
    st = quit_status(session)
    if not st["redline"]:
        _tmux("display-message", *(["-c", client] if client else []),
              "prefix Q closes redline sessions only")
        return
    info = [f"-{len(st['windows'])} windows: {', '.join(st['windows'])}"]
    if st["unpushed"]:
        files = ", ".join(st["unpushed"][:3]) + (" …" if len(st["unpushed"]) > 3 else "")
        info.append(f"-#[fg=yellow]Not on Overleaf yet: {files}")
    else:
        info.append("-Everything is on Overleaf")
    if st["agent_running"]:
        info.append("-#[fg=red]A merge agent is still running")
    quit_cmd = "run-shell " + _tmux_arg(["env", "-u", "PYTHONPATH", *_env_assign(), *redline_cmd(),
                                         "quit", "--yes", "--session", session])
    args = ["display-menu", "-T", "#[align=centre] Close redline? ", "-x", "C", "-y", "C"]
    if client:
        args += ["-c", client]
    args.append("--")  # disabled items start with "-"; don't let tmux read them as flags
    for line in info:
        args += [line, "", ""]
    args += ["", "Quit: save and close everything", "y", quit_cmd, "Cancel", "n", ""]
    _tmux(*args)


def _env_assign() -> list[str]:
    return [f"PYTHONPATH={REPO}"] if redline_cmd()[0] == sys.executable else []


def bind_quit_key(key: str) -> None:
    """prefix <key> anywhere in a redline session opens the quit menu."""
    if not key:
        return
    cmd = ["env", "-u", "PYTHONPATH", *_env_assign(), *redline_cmd(), "quit", "--menu",
           "--session", "#{session_name}", "--client", "#{client_name}"]
    subprocess.run(["tmux", "bind-key", key, "run-shell", " ".join(cmd)], capture_output=True)
