"""Run the outline's agent tasks (thoughts, flow check) with the model you chose for them.

`outline_model` in the config names an entry of [models] (default "deepseek": a cheap model is
plenty for one-line summaries); "pane" sends the task to the paper's agent pane instead. The
model works through the Redline MCP tools (outline_pending / record_thoughts ...), headless.
"""
from __future__ import annotations

import os
import subprocess

from . import agentpick, outline
from .model import RedlineError
from .state import Project


def run(proj: Project, task: str, model: str | None = None, stream=None, timeout: int = 900) -> str:
    name = (model or proj.cfg.outline_model or "pane").strip()
    if name == "pane":
        from . import quick
        session = quick.send_to_agent(proj.cfg.root, task)
        return f"sent to the agent in {session}"
    entry = proj.cfg.models.get(name)
    if entry is None:
        raise RedlineError(f"outline_model {name!r} is not in [models] ({', '.join(proj.cfg.models)}, or pane)")
    problem = agentpick.check(proj, entry)
    if problem:
        raise RedlineError(f"{entry.get('label', name)}: {problem}")
    agentpick.prepare(proj, entry)
    cmd = agentpick.headless_command(entry, task)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    try:
        p = subprocess.Popen(cmd, cwd=proj.cfg.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)
        out = []
        assert p.stdout is not None
        for line in p.stdout:
            out.append(line)
            if stream:
                stream(line)
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        raise RedlineError(f"{name} timed out after {timeout}s")
    if p.returncode != 0:
        raise RedlineError(f"{name} failed ({p.returncode}):\n{''.join(out)[-1500:]}")
    return f"{entry.get('label', name)}: done"


def refresh(proj: Project, model: str | None = None, stream=None) -> str:
    before = outline.build(proj)
    todo = before["missing"] + before["stale"]
    if not todo:
        return "every thought is up to date"
    res = run(proj, outline.thoughts_task(), model, stream)
    after = outline.build(proj)
    return f"{res}; {todo - after['missing'] - after['stale']} of {todo} thought(s) written, " \
           f"{after['missing'] + after['stale']} still open"


def flow(proj: Project, model: str | None = None, stream=None) -> str:
    o = outline.build(proj)
    if o["missing"] or o["stale"]:
        raise RedlineError(f"{o['missing'] + o['stale']} block(s) have no up-to-date thought: refresh the thoughts "
                           "first (a flow check on stale thoughts would judge old wording)")
    res = run(proj, outline.flow_task(), model, stream)
    return f"{res}; {outline.build(proj)['flags']} flow flag(s)"
