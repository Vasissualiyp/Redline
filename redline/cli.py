"""`redline` command line."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .model import RedlineError


def _selector(a: argparse.Namespace) -> dict:
    sel: dict = {}
    if a.ids:
        sel["ids"] = a.ids
    if a.section:
        sel["section"] = a.section
    if a.until:
        sel["until_section"] = a.until
    if a.since:
        sel["from_section"] = a.since
    if a.lines:
        lo, _, hi = a.lines.partition("-")
        sel["lines"] = [int(lo), int(hi or lo)]
    if a.kind:
        sel["kinds"] = a.kind
    if a.marker:
        sel["marker"] = a.marker
    if a.contains:
        sel["contains"] = a.contains
    if a.exclude:
        sel["except_ids"] = a.exclude
    if a.all:
        sel["all"] = True
    return sel


def _add_selector(p: argparse.ArgumentParser) -> None:
    p.add_argument("ids", nargs="*", help="change labels (C12) or ids (a3f9c2)")
    p.add_argument("-s", "--section", action="append", help="section name or number (fuzzy)")
    p.add_argument("--until", metavar="SECTION", help="changes before this section")
    p.add_argument("--since", metavar="SECTION", help="changes from this section on")
    p.add_argument("-l", "--lines", help="line range, e.g. 100-250")
    p.add_argument("-k", "--kind", action="append",
                   choices=["added", "deleted", "replaced", "comment", "highlight"])
    p.add_argument("-m", "--marker", choices=["Y", "N", "X", "none", "any"])
    p.add_argument("-c", "--contains", help="text search inside the change")
    p.add_argument("-x", "--exclude", action="append", help="ids to leave alone")
    p.add_argument("-a", "--all", action="store_true", help="every change")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="redline", description=__doc__)
    ap.add_argument("-C", "--root", help="paper directory (default: nearest with .redline.toml, or cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("full", help="all your papers in one place: tui | list | remote | new | open | back | "
                                    "categories | cat-add | cat-rm | cat-color | set")
    p.add_argument("what", nargs="?", default="tui", choices=[
        "tui", "list", "remote", "new", "open", "back", "categories", "cat-add", "cat-rm", "cat-color", "set"])
    p.add_argument("name", nargs="?", help="paper (open, set), Overleaf project (new) or category (cat-*)")
    p.add_argument("extra", nargs="?", help="set: key=value (category, notes, enforce_verification, code_dir); "
                                            "cat-color: the colour")
    p.add_argument("--color", default="", help="cat-add: colour (name or #rrggbb)")
    p.add_argument("--pull", action="store_true", help="new: pull the existing Overleaf project of this name")
    p.add_argument("--dir", help="new: directory for the paper (default: next to your other papers)")
    p.add_argument("--code-dir", default="", help="new: analysis code directory (default: analysis/ inside the paper)")
    p.add_argument("--no-verification", action="store_true", help="new: not a project whose citations are refchecked")
    p.add_argument("--two-sessions", action="store_true", help="open: nvim + agent and the PDF view in two tmux sessions")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-attach", action="store_true", help="open: start the session without attaching")

    p = sub.add_parser("init", help="create .redline.toml, ignore entries and the Overleaf merge base")
    p.add_argument("--code-dir", default="", help="analysis/plotting code directory")
    p.add_argument("--no-sync", action="store_true", help="skip downloading the Overleaf base")
    p.add_argument("--no-verification", action="store_true",
                   help="this project does not cite papers that must be refchecked against Zotero")

    p = sub.add_parser("overview", help="high-level overview: one thought per block (nvim: gO)")
    p.add_argument("action", nargs="?", default="show",
                   choices=["show", "refresh", "flow", "plan-set", "plan-clear", "plan-message", "plan-apply", "edit-message"])
    p.add_argument("--json", action="store_true")
    p.add_argument("--model", help="refresh/flow: a [models] entry (default: outline_model), or pane")
    p.add_argument("--all", action="store_true", help="refresh: drop every thought and rewrite them all")
    p.add_argument("--op", choices=["merge", "split"], help="edit-message")
    p.add_argument("--hashes", help="edit-message: comma separated block hashes")
    p.add_argument("--instruction", default="", help="edit-message: what you want")

    p = sub.add_parser("sessions", help="switch this running paper between one tmux session and two (PDF alone)")
    p.add_argument("to", nargs="?", choices=["one", "two", "toggle", "status"], default="toggle")
    p.add_argument("--save", action="store_true", help="also make it the paper's setting (two_sessions)")

    p = sub.add_parser("verification", help="refcheck enforcement for this paper: status | on | off")
    p.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")

    sub.add_parser("outline", help="sections with pending-change counts")
    sub.add_parser("status", help="counts as JSON")
    p = sub.add_parser("list", help="list pending changes")
    _add_selector(p)
    p.add_argument("--full", action="store_true")
    sub.add_parser("notes", help="changes the human marked with %%Y/%%N/%%X")

    for name, hlp in (("accept", "accept changes"), ("reject", "reject changes"),
                      ("markers", "apply %%Y/%%X markers")):
        p = sub.add_parser(name, help=hlp)
        _add_selector(p)
        p.add_argument("--comments", choices=["auto", "keep", "drop"], default="auto")
        p.add_argument("--ignore-markers", action="store_true", help="decide by the command, not by %%Y/%%N/%%X")
        p.add_argument("--yes", action="store_true", help="with --ignore-markers: do not ask first")

    p = sub.add_parser("lint", help="check markup that breaks the build")
    p.add_argument("--fix", action="store_true")
    p = sub.add_parser("compile", help="latexmk build into .redline/build")
    p.add_argument("--isolate", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("sync", help="Overleaf: init | pull | push | check | overleaf | finish")
    p.add_argument("action", choices=["init", "pull", "push", "check", "overleaf", "finish", "auto"],
                   help="overleaf = sync: push if Overleaf is unchanged, else merge with an agent first")
    p.add_argument("--force", action="store_true", help="push despite lint/compile errors")
    p.add_argument("--no-pull", action="store_true", help="push without pulling/merging first")
    p.add_argument("--agent", help="agent that resolves the merge (see `redline agents`), or git")
    p.add_argument("--no-push", action="store_true", help="stop after merging")
    p.add_argument("--yes", action="store_true", help="do not ask before pushing")
    p.add_argument("--json", action="store_true")
    p.add_argument("--enabled", action="store_true", help="auto: only say whether single_editor auto-sync is on (exit 0/1)")
    p.add_argument("--reuse-download", action="store_true",
                   help="reuse an Overleaf download made in the last 10 minutes")
    p = sub.add_parser("agents", help="configured merge agents")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("verify", help="claim verification: list requests / report / agent message")
    p.add_argument("what", choices=["requests", "report"], nargs="?", default="report")
    p = sub.add_parser("evidence", help="evidence behind %% VERIFIED lines (Zotero-backed library)")
    p.add_argument("what", choices=["list", "get", "show", "view", "note", "sync", "migrate"],
                   help="list | get ID (text) | show ID (tmux window, toggles) | sync (with Zotero) | "
                        "migrate (inline results into the library; dry run without --apply)")
    p.add_argument("id", nargs="?")
    p.add_argument("--apply", action="store_true", help="migrate: change the paper (default: dry run)")
    p.add_argument("--retry", action="store_true", help="sync: retry quotes that were not found")
    p.add_argument("--skip-unmatched", action="store_true",
                   help="migrate: leave results whose quotes are not all found inline")
    sub.add_parser("undo", help="revert the last redline write")
    sub.add_parser("history", help="recent redline writes")

    sub.add_parser("mcp", help="run the MCP server on stdio")
    sub.add_parser("rpc", help="run the nvim JSON-lines RPC on stdio")
    p = sub.add_parser("mcp-config", help="write the agent's MCP config into the paper dir")
    p.add_argument("--print", action="store_true", help="print instead of writing")
    p = sub.add_parser("preview", help="PDF pane (kitty graphics)")
    p = sub.add_parser("layout", help="arrange the paper/agent/PDF panes (presets)")
    p.add_argument("name", nargs="?", help="preset name; omit with --next/--prev/--list")
    p.add_argument("--next", action="store_true")
    p.add_argument("--prev", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--session", help="tmux session (default: the current one)")
    p = sub.add_parser("quit", help="close the whole tmux workspace (asks first)")
    p.add_argument("--session", help="tmux session (default: the current one)")
    p.add_argument("--client", help="tmux client to show the menu on")
    p.add_argument("--menu", action="store_true", help="ask with a tmux menu")
    p.add_argument("--status", action="store_true", help="only report what would be closed")
    p.add_argument("--json", action="store_true")
    p.add_argument("--yes", action="store_true", help="close without asking")
    p = sub.add_parser("agent", help="switch the agent pane between claude and opencode models")
    p.add_argument("name", nargs="?", help="entry of [models] to switch to; omit to list")
    p.add_argument("--list", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--menu", action="store_true", help="choose with a tmux menu")
    p.add_argument("--session", help="tmux session (default: the current one)")
    p.add_argument("--client", help="tmux client to show the menu on")
    p.add_argument("--force", action="store_true", help="switch even if the check fails")
    p.add_argument("--refresh", action="store_true", help="re-read opencode's model list")
    p.add_argument("--for", dest="for_", choices=["agent", "outline"], default="agent",
                   help="outline: choose the model for the overview's thoughts and flow check instead of the pane's agent")
    p = sub.add_parser("save-layout", help="remember the current pane arrangement for this paper")
    p.add_argument("--session", help="tmux session (default: the current one)")
    p = sub.add_parser("open", help="start the tmux workspace")
    p.add_argument("dir", nargs="?")
    p.add_argument("--no-attach", action="store_true")
    p.add_argument("--pdf", action="store_true", help="two_sessions papers: attach the PDF session instead")
    p.add_argument("--two-sessions", action="store_true",
                   help="this time: nvim + agent in one tmux session, the PDF view in a second (overrides the paper's setting)")
    p.add_argument("--one-session", action="store_true", help="this time: everything in one session")

    a = ap.parse_args(argv)
    if a.root:
        import os
        os.environ["REDLINE_ROOT"] = str(Path(a.root).expanduser().resolve())
    try:
        return _run(a)
    except RedlineError as e:
        print(f"redline: {e}", file=sys.stderr)
        return 1


def _categories_cmd(a: argparse.Namespace) -> int:
    from . import categories, config, registry
    if a.what == "categories":
        rows = registry.listing()
        tree = categories.build(rows)
        for kind, obj, depth, node in categories.flatten(tree, set()):
            print("  " * depth + (f"[{obj.path}] ({node.color})" if kind == "cat" else f"- {obj.name}"))
        return 0
    if not a.name:
        print(f"redline full {a.what}: give a name", file=sys.stderr)
        return 2
    if a.what == "cat-add":
        print("added " + categories.add(a.name, a.color))
    elif a.what == "cat-color":
        categories.set_color(categories.clean(a.name), a.extra or "")
    elif a.what == "cat-rm":
        gone = categories.remove(categories.clean(a.name))
        for p in registry.scan():
            if categories.clean(config.load(p).category) in gone:
                config.set_key(p, "category", "")
        print("removed " + ", ".join(gone))
    else:  # set NAME key=value
        key, _, val = (a.extra or "").partition("=")
        if key not in ("category", "notes", "enforce_verification", "cross_paper_access", "single_editor", "two_sessions", "code_dir",
                       "overleaf_project") or not _:
            print("usage: redline full set NAME key=value (category, notes, enforce_verification, code_dir, "
                  "overleaf_project)", file=sys.stderr)
            return 2
        root = registry.find(a.name)
        config.set_key(root, key, val.lower() in ("true", "1", "yes", "on") if key in ("enforce_verification", "cross_paper_access", "single_editor", "two_sessions") else val)
        print(f"{root.name}: {key} = {val}")
    return 0


def _full_cmd(a: argparse.Namespace) -> int:
    from . import registry
    if a.what == "tui":
        from .fulltui import run
        return run()
    if a.what == "back":
        from .launcher import go_full
        return go_full()
    if a.what in ("categories", "cat-add", "cat-rm", "cat-color", "set"):
        return _categories_cmd(a)
    if a.what == "list":
        rows = registry.listing()
        if a.json:
            print(json.dumps([r.as_dict() for r in rows], indent=1))
            return 0
        w = max([len(r.name) for r in rows] + [5])
        print(f"{'PAPER':{w}}  PENDING  UNRATED  OVERLEAF     TMUX     ACTIVE")
        for r in rows:
            n = lambda v: "?" if v is None else str(v)  # noqa: E731
            print(f"{r.name:{w}}  {n(r.pending):>7}  {n(r.unrated):>7}  {r.sync or 'error':11}  "
                  f"{'running' if r.running else '-':7}  {registry.ago(r.active)}" + (f"  [{r.error}]" if r.error else ""))
            print(f"{'':{w}}  {r.path}")
        return 0
    if a.what == "remote":
        from . import overleaf
        local = {p.name for p in registry.scan()}
        ps = overleaf.projects()
        if a.json:
            print(json.dumps(ps, indent=1))
        for p in ([] if a.json else ps):
            print(f"{overleaf.pretty(p['updated'])}  {p['name']}" + ("  (local)" if p["name"] in local else ""))
        return 0
    if a.what == "open":
        from .launcher import open_workspace
        if not a.name:
            print("redline full open: give a paper name (`redline full list`)", file=sys.stderr)
            return 2
        return open_workspace(registry.find(a.name), attach=not a.no_attach, two=True if a.two_sessions else None)
    # new
    if not a.name:
        print("redline full new: give the paper's name", file=sys.stderr)
        return 2
    dest = Path(a.dir) if a.dir else None
    make = registry.pull_existing if a.pull else registry.create_blank
    from .spin import spinning
    with spinning(("pulling " if a.pull else "creating ") + a.name):
        path = make(a.name, dest, a.code_dir, not a.no_verification)
    print(f"{'pulled' if a.pull else 'created'}: {path}\nopen it with: redline full open {path.name!r}")
    return 0


def _run(a: argparse.Namespace) -> int:
    from . import config

    if a.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return 0
    if a.cmd == "open":
        from .launcher import open_workspace
        return open_workspace(Path(a.dir).expanduser() if a.dir else None, attach=not a.no_attach, pdf=a.pdf,
                              two=True if a.two_sessions else (False if a.one_session else None))
    if a.cmd == "agent":
        return _agent_cmd(a)
    if a.cmd == "full":
        return _full_cmd(a)
    if a.cmd == "quit":
        from . import launcher as LA
        session = a.session or LA._session_of_caller()
        if a.status:
            st = LA.quit_status(session)
            print(json.dumps(st) if a.json else
                  f"{session}: {len(st['windows'])} windows; not on Overleaf: {st['unpushed'] or 'nothing'}"
                  + ("; merge agent running" if st["agent_running"] else ""))
        elif a.yes:
            LA.quit_now(session)
        elif a.menu:
            try:
                LA.quit_menu(session, a.client)
            except Exception as e:  # run-shell would only say "returned 1"
                import subprocess
                subprocess.run(["tmux", "display-message", *(["-c", a.client] if a.client else []),
                                f"redline quit: {str(e).splitlines()[-1][:180]}"])
                return 1
        else:
            st = LA.quit_status(session)
            print(f"{len(st['windows'])} windows: {', '.join(st['windows'])}")
            if st["unpushed"]:
                print("not on Overleaf yet: " + ", ".join(st["unpushed"]))
            if input("Close all of them? [y/N] ").strip().lower() == "y":
                LA.quit_now(session)
        return 0
    if a.cmd == "save-layout":
        from . import launcher as LA
        session = a.session or LA._session_of_caller()
        root = LA._session_root(session) or config.find_root()
        LA.save_layout(session, root)
        print(f"saved to {root / '.redline' / 'layout.json'}")
        return 0
    if a.cmd == "layout":
        from . import layout as L
        if a.list or not (a.name or a.next or a.prev):
            if a.json:
                print(json.dumps([{"name": n, "desc": L.PRESETS[n][0]} for n in L.ORDER]))
            else:
                print("\n".join(f"{n:13s} {L.PRESETS[n][0]}" for n in L.ORDER))
            return 0
        win = L.find_window(a.session) if a.session else None
        print(L.cycle(-1 if a.prev else 1, win) if (a.next or a.prev) else L.apply(a.name, win))
        return 0
    if a.cmd == "overview":
        from . import outline as O
        from . import outline_agent as OA
        from .state import Project as P
        proj = P.open()
        if a.action == "show":
            o = O.build(proj)
            if a.json:
                print(json.dumps(o))
                return 0
            for n in o["nodes"]:
                t = n["thought"] or "(no thought)"
                print("  " * n["depth"] + (f"§ {n['number'] + ' ' if n['number'] else ''}{n['title']}" if n["kind"] == "heading" else
                                           f"[{n['kind']}] {t}{' ~' if n['stale'] else ''}  ({n['chars']}c {n['words']}w {n['nlines']}l)"))
            print(f"{o['total']} blocks, {o['missing']} without a thought, {o['stale']} stale, {o['flags']} flow flag(s)")
            return 0
        if a.action == "plan-set":
            print(O.set_plan(proj, json.loads(sys.stdin.read())))
            return 0
        if a.action == "plan-clear":
            print(O.set_plan(proj, None))
            return 0
        if a.action == "plan-message":
            print(O.plan_message(proj))
            return 0
        if a.action == "plan-apply":
            print(O.apply_direct(proj))
            return 0
        if a.action == "edit-message":
            print(O.edit_message(proj, a.op or "merge", [h for h in (a.hashes or "").split(",") if h], a.instruction))
            return 0
        from .spin import spinning
        with spinning("asking the model" if (a.model or proj.cfg.outline_model) != "pane" else "sending to the agent"):
            print(OA.refresh(proj, a.model, everything=a.all) if a.action == "refresh" else OA.flow(proj, a.model))
        return 0
    if a.cmd == "sessions":
        from . import config
        from . import launcher as LA
        root = config.find_root()
        if a.to == "status":
            two = LA._has_session(LA.pdf_session_name(LA.session_name_for(root)))
            print("two sessions" if two else "one session")
            return 0
        print(LA.switch_sessions(root, a.to))
        if a.save and a.to != "status":
            two = LA._has_session(LA.pdf_session_name(LA.session_name_for(root)))
            config.set_key(root, "two_sessions", two)
            print(f"saved: two_sessions = {str(two).lower()}")
        return 0
    if a.cmd == "verification":
        from . import config
        root = config.find_root()
        path = config.write_default(root)
        if a.state != "status":
            path = config.set_verification(root, a.state == "on")
        print("refcheck enforcement: " + ("on" if config.load(root).enforce_verification else "off")
              + f"  ({path})")
        return 0
    if a.cmd == "init":
        from . import sync
        root = config.find_root()
        verify = not a.no_verification
        if verify and not (root / config.CONFIG_NAME).exists() and sys.stdin.isatty():
            verify = input("Check every citation against Zotero (refcheck, the default)? "
                           "Say n only if this project is not about cited literature. [Y/n] "
                           ).strip().lower() not in ("n", "no")
        path = config.write_default(root, a.code_dir, enforce_verification=verify)
        print(f"config: {path}" + ("" if verify else "  (refcheck enforcement OFF; `redline verification on` re-enables)"))
        changed = sync.ensure_ignores(root)
        if changed:
            print("added redline entries to " + ", ".join(changed))
        from .state import Project
        proj = Project.open()
        from . import preamble
        added = preamble.ensure(proj.cfg.main_path)
        if added:
            print(f"added to the preamble of {proj.cfg.main}: " + ", ".join(added))
        from .launcher import write_mcp_config
        print("mcp config: " + write_mcp_config(proj))
        if not a.no_sync:
            from .spin import spinning
            with spinning("downloading the Overleaf project"):
                rep = sync.init_base(proj)
            print(rep.text())
        return 0

    from .state import Project
    proj = Project.open()
    if a.cmd == "rpc":
        from .rpc import serve
        serve(proj)
        return 0
    if a.cmd == "preview":
        from .preview import main as preview_main
        return preview_main(proj)
    if a.cmd == "evidence" and a.what in ("show", "view", "note"):
        if not a.id:
            print(f"redline evidence {a.what}: give the evidence id (e.g. ho25b#1)", file=sys.stderr)
            return 2
        from . import evidence_view
        if a.what == "show":
            print(evidence_view.show(proj, a.id))
            return 0
        return (evidence_view.view if a.what == "view" else evidence_view.note)(proj, a.id)
    if a.cmd == "mcp-config":
        from .launcher import mcp_config, write_mcp_config
        if a.print:
            print(json.dumps(mcp_config(proj), indent=2))
        else:
            print(write_mcp_config(proj))
        return 0

    from .service import Service
    s = Service(proj)
    if a.cmd == "outline":
        print(s.outline())
    elif a.cmd == "status":
        print(json.dumps(s.status(), indent=1))
    elif a.cmd == "list":
        sel = _selector(a) or {"all": True}
        print(s.list_changes(sel, a.full))
    elif a.cmd == "notes":
        print(s.review_notes())
    elif a.cmd in ("accept", "reject", "markers"):
        sel = _selector(a)
        if a.cmd == "markers" and not sel:
            sel = {"all": True}
        if a.ignore_markers and not a.yes:
            from . import select as _sel
            doc = proj.doc()
            items = _sel.select(doc, sel, proj.origins())
            noted = sum(1 for i in items if i.marker and i.marker.kind in "NX")
            ask = (f"{a.cmd} {len(items)} change(s), ignoring {noted} %N/%X marker(s)? [y/N] ")
            if not sys.stdin.isatty() or input(ask).strip().lower() != "y":
                print("not applied (confirm interactively, or pass --yes)")
                return 1
        print(s.resolve(a.cmd, sel, a.comments, honor_markers=not a.ignore_markers))
    elif a.cmd == "lint":
        out = s.lint(a.fix)
        print(out)
        return 1 if "ERROR" in out else 0
    elif a.cmd == "compile":
        from . import compile as C
        from .spin import spinning
        with spinning("compiling"):
            res = C.run(proj, isolate=a.isolate)
        print(json.dumps(res.as_dict()) if a.json else C.format_report(res))
        return 0 if res.ok else 1
    elif a.cmd == "sync":
        return _sync_cmd(s, a)
    elif a.cmd == "agents":
        names = list(proj.cfg.agents)
        print(json.dumps(names) if a.json else "\n".join(
            f"{n}{' (interactive)' if proj.cfg.agents[n]['tui'] else ''}: {' '.join(proj.cfg.agents[n]['cmd'])}"
            for n in names))
    elif a.cmd == "verify":
        print(s.verification_requests() if a.what == "requests" else s.verification_report())
    elif a.cmd == "evidence":
        if a.what == "list":
            print(s.evidence_list())
        elif a.what == "get":
            print(s.evidence(a.id or ""))
        elif a.what == "sync":
            print(s.evidence_sync(a.retry))
        elif a.what == "migrate":
            print(s.migrate_verifications(a.apply, progress=lambda m: print(m, file=sys.stderr, flush=True),
                                          skip_unmatched=a.skip_unmatched))
    elif a.cmd == "undo":
        print(s.undo())
    elif a.cmd == "history":
        for h in proj.history():
            print(f"{h['n']:5d} {h['time']} {h['reason']}")
    return 0


def _sync_cmd(s, a: argparse.Namespace) -> int:
    def done(out: str) -> int:
        print(out)
        return 1 if ("CONFLICT" in out or "not pushing" in out or "nothing applied" in out) else 0

    if a.action == "auto":
        cfg = s.proj.cfg
        if a.enabled:
            print(json.dumps({"enabled": cfg.single_editor, "seconds": cfg.auto_sync_seconds, "poll": cfg.auto_sync_poll}))
            return 0 if cfg.single_editor else 1
        if not cfg.single_editor:
            print("single_editor is off for this paper (redline full set PAPER single_editor=true)")
            return 5
        from . import sync as _s
        state, text = _s.auto(s.proj)
        print(json.dumps({"state": state, "text": text}) if a.json else f"{state}: {text}")
        return {"idle": 0, "pushed": 0, "pulled": 0, "busy": 4}.get(state, 3)
    if a.action == "init":
        return done(s.sync_init())
    if a.action == "pull":
        return done(s.pull())
    if a.action == "push":
        return done(s.push_only() if a.no_pull else s.push(a.force))
    if a.action == "check":
        chk = s.sync_check(a.reuse_download)
        print(json.dumps({**chk.as_dict(), "agents": list(s.proj.cfg.agents)}) if a.json else chk.text())
        return 0
    if a.action == "finish":
        return done(s.sync_finish(push=not a.no_push))
    # a.action == "overleaf"
    agent = a.agent
    if agent is None:
        chk = s.sync_check(a.reuse_download)
        print(chk.text())
        if chk.remote_unchanged:  # Overleaf is behind (or equal): just update it
            return done(s.push_only() if chk.local_edits else "already in sync with Overleaf")
        if chk.both_changed and not chk.clean:
            opts = list(s.proj.cfg.agents)
            for i, n in enumerate(opts, 1):
                print(f"  {i}) {n}")
            pick = input("Who merges? number (empty = cancel): ").strip()
            if not pick.isdigit() or not 1 <= int(pick) <= len(opts):
                return 0
            agent = opts[int(pick) - 1]
    out = s.sync_to_overleaf(agent, push=False, stream=lambda ln: print(ln, end="", flush=True),
                             reuse=True)
    print(out)
    if "CONFLICT" in out or "nothing applied" in out:
        return 1
    if a.no_push or "nothing to push" in out:
        return 0
    if not a.yes and input("Push the merged paper to Overleaf? [y/N] ").strip().lower() != "y":
        return 0
    return done(s.push_only())




def _agent_cmd(a) -> int:
    import os
    import subprocess
    from . import agentpick
    from . import launcher as LA
    from .state import Project
    if a.session and not os.environ.get("REDLINE_ROOT"):
        root = LA._session_root(a.session)
        if root:
            os.environ["REDLINE_ROOT"] = str(root)
    try:
        proj = Project.open()
        if a.menu:
            agentpick.menu(proj, a.session or LA._session_of_caller(), a.client)
        elif a.name:
            msg = (agentpick.set_outline(proj, a.name, a.force) if a.for_ == "outline"
                   else agentpick.switch(proj, a.name, a.session, a.force))
            if a.session and not sys.stdout.isatty():
                subprocess.run(["tmux", "display-message", *(["-c", a.client] if a.client else []), msg])
            print(msg)
        else:
            items = agentpick.listing(proj, a.refresh, a.for_)
            if a.json:
                print(json.dumps(items))
            else:
                for it in items:
                    print(f"{'*' if it['current'] else ' '} {it['name']:12s} {it['label']}"
                          + (f"  [{it['problem']}]" if it["problem"] else ""))
    except (RedlineError, RuntimeError) as e:
        if a.menu or (a.session and not sys.stdout.isatty()):
            subprocess.run(["tmux", "display-message", *(["-c", a.client] if a.client else []),
                            f"redline agent: {str(e).splitlines()[-1][:180]}"])
        raise RedlineError(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
