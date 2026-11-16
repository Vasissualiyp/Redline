"""Run the drafting phases (lit review, writing) headless with the model the brief / config names.

The review run cannot edit the paper (it only writes draft/*.md notes); the write run can, through
draft_write. Both go through outline_agent.run, so the model choice, the checks and the tool
config handling are the same as for the overview.
"""
from __future__ import annotations

from . import draft, outline_agent
from .model import RedlineError
from .state import Project

TIMEOUT = 3600


def _model(proj: Project, b: draft.Brief, model: str | None) -> str:
    return (model or b.model or proj.cfg.draft_model or "claude").strip()


def review(proj: Project, model: str | None = None, stream=None) -> str:
    b = draft.load_brief(proj)
    if not b.topic:
        raise RedlineError("the brief has no topic: fill in draft.toml first")
    if draft.state(proj)["phase"] in ("approved", "written"):
        raise RedlineError("this draft is already approved or written; reviewing again would replace notes you approved "
                           "(delete .redline/draft.json to start over)")
    res = outline_agent.run(proj, draft.review_task(b), _model(proj, b, model), stream, TIMEOUT, readonly=True)
    missing = [n for n in draft.required_notes(b) if not (draft.notes_dir(proj) / f"{n}.md").exists()]
    if missing:
        raise RedlineError(f"{res}, but it did not write: {', '.join(f'draft/{n}.md' for n in missing)}")
    draft._set_phase(proj, "reviewed")
    return f"{res}; notes in draft/ ({', '.join(draft.required_notes(b))}). Edit them, then `redline draft approve`"


def write(proj: Project, model: str | None = None, stream=None, force: bool = False) -> str:
    b = draft.load_brief(proj)
    draft.check_write(proj, b, force)
    tgt = draft.target(proj, b)
    old = tgt.read_text() if tgt.exists() else ""
    draft._set_phase(proj, draft.state(proj)["phase"], force=force)
    res = outline_agent.run(proj, draft.write_task(proj, b), _model(proj, b, model), stream, TIMEOUT, readonly=False)
    new = tgt.read_text() if tgt.exists() else ""
    if new == old or draft.state(proj)["phase"] != "written":
        raise RedlineError(f"{res}, but {tgt.name} was not written (the agent never called draft_write)")
    warn = draft.cite_report(proj, new)
    return "\n".join([f"{res}; {tgt.name} written. Check it, compile it, and run the citation verification", *warn])


def run_all(proj: Project, model: str | None = None, stream=None, force: bool = False) -> str:
    """review, then (without a checkpoint in the brief) approve and write."""
    out = [review(proj, model, stream)]
    if draft.load_brief(proj).checkpoint:
        return out[0]
    draft.approve(proj)
    out.append(write(proj, model, stream, force))
    return "; ".join(out)
