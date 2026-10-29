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


def mcp_config(proj: Project) -> dict:
    servers = mcp_servers(proj)
    if proj.cfg.agent_kind == "opencode":
        return {"$schema": "https://opencode.ai/config.json",
                "mcp": {n: {"type": "local", "command": [s["command"], *s["args"]],
                            "environment": s.get("env", {}), "enabled": True}
                        for n, s in servers.items()}}
    return {"mcpServers": servers}


def write_mcp_config(proj: Project) -> str:
    """Merge our servers into the agent's project config (other entries are kept)."""
    root = proj.cfg.root
    cfg = mcp_config(proj)
    if proj.cfg.agent_kind == "opencode":
        path, key = root / "opencode.json", "mcp"
    else:
        path, key = root / ".mcp.json", "mcpServers"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except ValueError:
            data = {}
    data.setdefault(key, {}).update(cfg[key])
    if "$schema" in cfg:
        data.setdefault("$schema", cfg["$schema"])
    path.write_text(json.dumps(data, indent=2) + "\n")
    return str(path)


def _tmux(*args: str, check: bool = True) -> str:
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def _shell(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def open_workspace(root: Path | None, attach: bool = True) -> int:
    root = _config.find_root(root)
    _config.write_default(root)
    _sync.ensure_ignores(root)
    proj = Project.open(root)
    write_mcp_config(proj)
    if not shutil.which("tmux"):
        print("tmux not found", file=sys.stderr)
        return 1

    name = "redline-" + re.sub(r"[^A-Za-z0-9_-]+", "-", root.name).strip("-")
    exists = subprocess.run(["tmux", "has-session", "-t", f"={name}"], capture_output=True).returncode == 0
    if not exists:
        rl = _shell(redline_cmd())
        env_prefix = "env -u PYTHONPATH " + (f"PYTHONPATH={shlex.quote(str(REPO))} "
                                             if redline_cmd()[0] == sys.executable else "")
        sock = _config.socket_path(root, "nvim")
        sock.unlink(missing_ok=True)
        nvim = _shell(["nvim", "--listen", str(sock), "--cmd", f"set rtp^={nvim_dir()}",
                       "-c", "lua require('redline').start()", str(proj.main)])
        # session environment so nvim's RPC job finds the same redline
        envs = ["-e", f"REDLINE_ROOT={root}", "-e", f"REDLINE_CMD={env_prefix}{rl}"]
        cols, rows = shutil.get_terminal_size((240, 60))
        editor = _tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name, "-n", "paper",
                       "-x", str(cols), "-y", str(rows), "-c", str(root), *envs, nvim)
        paper = f"={name}:paper"
        agent = _tmux("split-window", "-h", "-l", "40%", "-t", editor, "-c", str(root),
                      "-P", "-F", "#{pane_id}", proj.cfg.agent)
        _tmux("set-option", "-p", "-t", agent, "@redline_role", "agent")
        prev = _tmux("split-window", "-v", "-l", "50%", "-t", agent, "-c", str(root),
                     "-P", "-F", "#{pane_id}", f"{env_prefix}{rl} preview")
        _tmux("set-option", "-p", "-t", prev, "@redline_role", "preview")
        _tmux("select-pane", "-t", editor)
        _tmux("set-option", "-p", "-t", editor, "@redline_role", "paper")
        code = proj.cfg.code_dir if proj.cfg.code_dir and Path(proj.cfg.code_dir).is_dir() else str(root)
        code_pane = _tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={name}:",
                          "-n", "code", "-c", code, "nvim .")
        _tmux("split-window", "-h", "-l", "35%", "-t", code_pane, "-c", code)
        _tmux("select-window", "-t", paper)
        # the arrangement saved when this paper was last quit, else the preset
        restore_layout(name, root, proj.cfg.layout)
    bind_quit_key(proj.cfg.tmux_quit_key)
    if not attach:
        print(name)
        return 0
    if os.environ.get("TMUX"):
        _tmux("switch-client", "-t", f"={name}")
    else:
        os.execvp("tmux", ["tmux", "attach-session", "-t", f"={name}"])
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


def quit_status(session: str) -> dict:
    """What would be lost or interrupted by closing the session."""
    root = _session_root(session)
    windows = _tmux("list-windows", "-t", f"={session}", "-F", "#{window_name}").splitlines()
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
    root = _session_root(session)
    if root is not None:
        try:
            save_layout(session, root)
        except Exception as e:  # never block quitting
            print(f"layout not saved: {e}", file=sys.stderr)
        Project.open(root).flush_editor()  # nvim writes its buffer first
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
