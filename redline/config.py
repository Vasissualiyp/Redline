"""Per-paper configuration (`.redline.toml` in the paper directory)."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .model import RedlineError
from .parse import DEFAULT_LEGACY

CONFIG_NAME = ".redline.toml"

DEFAULT_TOML = """\
# Redline settings for this paper. Not synced to Overleaf.
main = "{main}"
# Directory with the plotting/analysis code, opened in the "code" tmux window.
code_dir = "{code_dir}"
# Command started in the agent pane, and how to register MCP servers for it
# (agent_kind: "claude" writes .mcp.json, "opencode" writes opencode.json).
agent = "claude"
agent_kind = "claude"
# Pane arrangement at start: auto (pdf-tall on landscape screens, paper-top on
# portrait ones), default, columns, rows, pdf-tall, agent-bottom, pdf-left,
# paper-top. Switch later with W in review mode or `redline layout`.
layout = "auto"
# tmux key (after the prefix) that asks to close the whole session; "" = none.
tmux_quit_key = "Q"
# Overleaf sync (overleaf-sync's `ols`); project defaults to the directory name.
ols = "ols"
overleaf_project = ""
# Comments matching this regex survive bulk accepts.
keep_comment_regex = "TODO"
# Shell command run before pushing (e.g. rsync fresh plots into light/).
pre_push = ""
compile_timeout = 600
merge_timeout = 1800
# Extra MCP servers registered for the agent (name -> command list).
[mcp_extra]
zotero = ["zotero-mcp"]

# Agents offered when Overleaf and the local copy conflict ("Sync to Overleaf";
# clean merges need no agent). {{prompt}} is replaced by the task; the command
# runs in .redline/merge/. A list runs headless; a table with tui = true opens
# an interactive session in a tmux window, closed once the merge is resolved.
# Defaults: claude (interactive), claude-headless (claude -p), opencode (run),
# opencode-tui.
# Define more here or, for all papers, in ~/.config/redline/config.toml.
[agents]
# "claude-opus" = {{ cmd = ["claude", "--model", "opus", "--permission-mode", "acceptEdits", "{{prompt}}"], tui = true }}
# "opencode-kimi" = ["opencode", "run", "-m", "kimi/kimi-k2", "{{prompt}}"]

# Hand-written coauthor macros, macro -> added|deleted|comment|highlight.
[legacy]
"""

GLOBAL_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "redline" / "config.toml"

DEFAULT_AGENTS: dict[str, dict] = {
    # interactive Claude Code in a tmux window: you can watch and steer it
    "claude": {"cmd": ["claude", "--permission-mode", "acceptEdits", "{prompt}"], "tui": True},
    "claude-headless": {"cmd": ["claude", "-p", "--permission-mode", "acceptEdits", "{prompt}"],
                        "tui": False},
    "opencode": {"cmd": ["opencode", "run", "{prompt}"], "tui": False},
    "opencode-tui": {"cmd": ["opencode", "--prompt", "{prompt}"], "tui": True},
}


def agent_spec(value) -> dict:
    """An [agents] entry: a command list (headless) or {cmd = [...], tui = bool}."""
    if isinstance(value, dict):
        cmd = list(value.get("cmd") or [])
        return {"cmd": cmd, "tui": bool(value.get("tui", False))}
    return {"cmd": list(value or []), "tui": False}


@dataclass
class Config:
    root: Path
    main: str
    code_dir: str = ""
    agent: str = "claude"
    agent_kind: str = "claude"
    layout: str = "auto"
    tmux_quit_key: str = "Q"
    ols: str = "ols"
    overleaf_project: str = ""
    keep_comment_regex: str = "TODO"
    pre_push: str = ""
    compile_timeout: int = 600
    merge_timeout: int = 1800
    agents: dict[str, dict] = field(default_factory=lambda: dict(DEFAULT_AGENTS))
    mcp_extra: dict[str, list[str]] = field(default_factory=lambda: {"zotero": ["zotero-mcp"]})
    legacy: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LEGACY))

    @property
    def main_path(self) -> Path:
        return self.root / self.main

    @property
    def state_dir(self) -> Path:
        return self.root / ".redline"

    @property
    def project_name(self) -> str:
        return self.overleaf_project or self.root.name


def socket_path(root: Path, name: str) -> Path:
    """Unix sockets live outside the paper dir (paths there can exceed the 108-byte limit)."""
    import hashlib
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    h = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:12]
    return base / f"redline-{h}-{name}.sock"


def detect_main(root: Path) -> str:
    cands = []
    for p in sorted(root.glob("*.tex")):
        try:
            head = p.read_text(errors="replace")
        except OSError:
            continue
        if re.search(r"^\s*\\documentclass", head, re.M) and "\\begin{document}" in head:
            cands.append((len(head), p.name))
    if not cands:
        raise RedlineError(f"no main .tex file (with \\documentclass) in {root}")
    return max(cands)[1]


def find_root(start: Path | None = None) -> Path:
    """The paper directory: $REDLINE_ROOT, or the nearest dir with .redline.toml, or cwd."""
    env = os.environ.get("REDLINE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    p = (start or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        if (d / CONFIG_NAME).exists():
            return d
    return p


def load(root: Path | None = None) -> Config:
    root = find_root(root)
    path = root / CONFIG_NAME
    glob: dict = {}
    if GLOBAL_CONFIG.exists():
        with open(GLOBAL_CONFIG, "rb") as f:
            glob = tomllib.load(f)
    data: dict = {}
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
    agents = dict(DEFAULT_AGENTS)
    agents.update({k: agent_spec(v) for k, v in glob.get("agents", {}).items()})
    agents.update({k: agent_spec(v) for k, v in data.get("agents", {}).items()})
    data = {**{k: v for k, v in glob.items() if k != "agents"}, **data}
    main = data.get("main") or detect_main(root)
    legacy = dict(DEFAULT_LEGACY)
    legacy.update(data.get("legacy", {}))
    cfg = Config(root=root, main=main)
    for k in ("code_dir", "agent", "agent_kind", "layout", "tmux_quit_key", "ols", "overleaf_project",
              "keep_comment_regex", "pre_push"):
        if k in data:
            setattr(cfg, k, str(data[k]))
    if "compile_timeout" in data:
        cfg.compile_timeout = int(data["compile_timeout"])
    if "merge_timeout" in data:
        cfg.merge_timeout = int(data["merge_timeout"])
    cfg.agents = {k: v for k, v in agents.items() if v["cmd"]}
    if "mcp_extra" in data:
        cfg.mcp_extra = {k: list(v) for k, v in data["mcp_extra"].items()}
    cfg.legacy = legacy
    cfg.code_dir = os.path.expanduser(cfg.code_dir) if cfg.code_dir else ""
    return cfg


def write_default(root: Path, code_dir: str = "") -> Path:
    path = root / CONFIG_NAME
    if not path.exists():
        path.write_text(DEFAULT_TOML.format(main=detect_main(root), code_dir=code_dir))
    return path
