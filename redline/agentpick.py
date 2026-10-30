"""Switch the agent pane between claude and opencode models.

The choices are the [models] entries of the config (config.DEFAULT_MODELS plus
your own). Choosing one writes opencode's model (and any opencode config the
entry carries) into the paper's opencode.json, keeps both agents' MCP configs
current, and restarts the agent pane with the entry's command. The choice is
remembered in .redline/agent.json, so `redline open` starts it next time.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from .model import RedlineError
from .state import Project

MODELS_CACHE_TTL = 6 * 3600


def kind(entry: dict) -> str:
    return "opencode" if Path(entry["cmd"].split()[0]).name == "opencode" else "claude"


def current(proj: Project) -> str | None:
    p = proj.dir / "agent.json"
    try:
        name = json.loads(p.read_text()).get("preset")
    except (OSError, ValueError):
        return None
    return name if name in proj.cfg.models else None


def command(proj: Project) -> str:
    """What the agent pane runs: the chosen entry, else the `agent` setting."""
    name = current(proj)
    return proj.cfg.models[name]["cmd"] if name else proj.cfg.agent


def _opencode_models(proj: Project, refresh: bool = False) -> set[str]:
    cache = proj.dir / "opencode-models.txt"
    if not refresh and cache.exists() and time.time() - cache.stat().st_mtime < MODELS_CACHE_TTL:
        return set(cache.read_text().split())
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    try:
        out = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=60,
                             env=env, cwd=proj.cfg.root).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names = {ln.strip() for ln in out.splitlines() if "/" in ln and " " not in ln.strip()}
    if names:
        cache.write_text("\n".join(sorted(names)) + "\n")
    return names


def _local_endpoint(provider: str) -> str | None:
    """An http:// (LAN/tailnet) baseURL for the provider in the global opencode config."""
    p = Path.home() / ".config" / "opencode" / "opencode.json"
    try:
        url = json.loads(p.read_text()).get("provider", {}).get(provider, {}).get("options", {}).get("baseURL", "")
    except (OSError, ValueError):
        return None
    return url if url.startswith("http://") else None


def check(proj: Project, entry: dict, refresh: bool = False) -> str:
    """"" when the entry can start, else why not."""
    exe = entry["cmd"].split()[0]
    if not shutil.which(exe):
        return f"{exe} is not installed"
    if kind(entry) != "opencode" or not entry.get("model"):
        return ""
    model = entry["model"].split("#")[0]
    provider = model.split("/")[0]
    known = _opencode_models(proj, refresh)
    if known and model not in known:
        return f"{model} is not set up in opencode (opencode auth login, or add the provider)"
    url = _local_endpoint(provider)
    if url:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=3):
                pass
        except OSError:
            return f"{provider} at {url} is not reachable (machine off?)"
    return ""


def listing(proj: Project, refresh: bool = False) -> list[dict]:
    cur = current(proj)
    return [{"name": n, "label": e.get("label", n), "kind": kind(e), "model": e.get("model", ""),
             "current": n == cur, "problem": check(proj, e, refresh)}
            for n, e in proj.cfg.models.items()]


def _merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


def _agent_pane(session: str) -> str | None:
    out = subprocess.run(["tmux", "list-panes", "-s", "-t", session, "-F", "#{pane_id} #{@redline_role}"],
                         capture_output=True, text=True).stdout
    for ln in out.splitlines():
        pid, _, role = ln.partition(" ")
        if role == "agent":
            return pid
    return None


def _session() -> str | None:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    r = subprocess.run(["tmux", "display", "-p", "-t", pane, "#{session_name}"], capture_output=True, text=True)
    return r.stdout.strip() or None


def switch(proj: Project, name: str, session: str | None = None, force: bool = False) -> str:
    from .launcher import in_login_shell, write_mcp_config
    if name not in proj.cfg.models:
        raise RedlineError(f"unknown agent {name!r}; choices: {', '.join(proj.cfg.models)}")
    entry = proj.cfg.models[name]
    problem = check(proj, entry)
    if problem and not force:
        raise RedlineError(f"{entry.get('label', name)}: {problem}")
    write_mcp_config(proj)
    if kind(entry) == "opencode":
        path = proj.cfg.root / "opencode.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        _merge(data, entry.get("opencode") or {})
        if entry.get("model"):
            data["model"] = entry["model"]
        path.write_text(json.dumps(data, indent=2) + "\n")
    (proj.dir / "agent.json").write_text(json.dumps({"preset": name}) + "\n")
    session = session or _session()
    pane = _agent_pane(session) if session else None
    if not pane:
        return f"{entry.get('label', name)} chosen; it starts with the next `redline open`"
    subprocess.run(["tmux", "respawn-pane", "-k", "-t", pane, "-c", str(proj.cfg.root),
                    in_login_shell(entry["cmd"])], capture_output=True, check=False)
    return f"agent pane: {entry.get('label', name)}"


def menu(proj: Project, session: str, client: str | None = None) -> None:
    """tmux display-menu listing the agents; unavailable ones are greyed out with the reason."""
    from .launcher import _env_assign, _tmux, _tmux_arg, redline_cmd
    args = ["display-menu", "-T", "#[align=centre] Agent pane ", "-x", "C", "-y", "C"]
    if client:
        args += ["-c", client]
    args.append("--")
    keys = iter("123456789abcdefghijklmnopqrstuvwxyz")
    for it in listing(proj):
        mark = "* " if it["current"] else "  "
        if it["problem"]:
            args += [f"-{mark}{it['label']}  ({it['problem'][:60]})", "", ""]
            continue
        cmd = "run-shell " + _tmux_arg(["env", "-u", "PYTHONPATH", *_env_assign(), *redline_cmd(), "-C",
                                        str(proj.cfg.root), "agent", it["name"], "--session", session])
        args += [f"{mark}{it['label']}", next(keys), cmd]
    args += ["", "Cancel", "q", ""]
    _tmux(*args)


def bind_key(key: str) -> None:
    """prefix <key> in a redline session opens the agent menu."""
    from .launcher import _env_assign, redline_cmd
    if not key:
        return
    cmd = ["env", "-u", "PYTHONPATH", *_env_assign(), *redline_cmd(), "agent", "--menu",
           "--session", "#{session_name}", "--client", "#{client_name}"]
    subprocess.run(["tmux", "bind-key", key, "run-shell", " ".join(cmd)], capture_output=True)
