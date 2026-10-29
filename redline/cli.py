"""`redline` command line."""
from __future__ import annotations

import argparse
import json
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

    p = sub.add_parser("init", help="create .redline.toml, ignore entries and the Overleaf merge base")
    p.add_argument("--code-dir", default="", help="analysis/plotting code directory")
    p.add_argument("--no-sync", action="store_true", help="skip downloading the Overleaf base")

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
        p.add_argument("--ignore-markers", action="store_true")

    p = sub.add_parser("lint", help="check markup that breaks the build")
    p.add_argument("--fix", action="store_true")
    p = sub.add_parser("compile", help="latexmk build into .redline/build")
    p.add_argument("--isolate", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("sync", help="Overleaf: init | pull | push | check | overleaf | finish")
    p.add_argument("action", choices=["init", "pull", "push", "check", "overleaf", "finish"],
                   help="overleaf = sync: push if Overleaf is unchanged, else merge with an agent first")
    p.add_argument("--force", action="store_true", help="push despite lint/compile errors")
    p.add_argument("--no-pull", action="store_true", help="push without pulling/merging first")
    p.add_argument("--agent", help="agent that resolves the merge (see `redline agents`), or git")
    p.add_argument("--no-push", action="store_true", help="stop after merging")
    p.add_argument("--yes", action="store_true", help="do not ask before pushing")
    p.add_argument("--json", action="store_true")
    p.add_argument("--reuse-download", action="store_true",
                   help="reuse an Overleaf download made in the last 10 minutes")
    p = sub.add_parser("agents", help="configured merge agents")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("verify", help="claim verification: list requests / report / agent message")
    p.add_argument("what", choices=["requests", "report"], nargs="?", default="report")
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
    p = sub.add_parser("open", help="start the tmux workspace")
    p.add_argument("dir", nargs="?")
    p.add_argument("--no-attach", action="store_true")

    a = ap.parse_args(argv)
    if a.root:
        import os
        os.environ["REDLINE_ROOT"] = str(Path(a.root).expanduser().resolve())
    try:
        return _run(a)
    except RedlineError as e:
        print(f"redline: {e}", file=sys.stderr)
        return 1


def _run(a: argparse.Namespace) -> int:
    from . import config

    if a.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return 0
    if a.cmd == "open":
        from .launcher import open_workspace
        return open_workspace(Path(a.dir).expanduser() if a.dir else None, attach=not a.no_attach)
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
    if a.cmd == "init":
        from . import sync
        root = config.find_root()
        path = config.write_default(root, a.code_dir)
        print(f"config: {path}")
        changed = sync.ensure_ignores(root)
        if changed:
            print("added redline entries to " + ", ".join(changed))
        from .state import Project
        proj = Project.open()
        from .launcher import write_mcp_config
        print("mcp config: " + write_mcp_config(proj))
        if not a.no_sync:
            print(sync.init_base(proj).text())
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
        print(s.resolve(a.cmd, sel, a.comments, honor_markers=not a.ignore_markers))
    elif a.cmd == "lint":
        out = s.lint(a.fix)
        print(out)
        return 1 if "ERROR" in out else 0
    elif a.cmd == "compile":
        from . import compile as C
        res = C.run(proj, isolate=a.isolate)
        print(json.dumps(res.as_dict()) if a.json else C.format_report(res))
        return 0 if res.ok else 1
    elif a.cmd == "sync":
        return _sync_cmd(s, a)
    elif a.cmd == "agents":
        names = list(proj.cfg.agents)
        print(json.dumps(names) if a.json else "\n".join(f"{n}: {' '.join(proj.cfg.agents[n])}" for n in names))
    elif a.cmd == "verify":
        print(s.verification_requests() if a.what == "requests" else s.verification_report())
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
        if chk.remote_unchanged:
            if not a.yes and input("Push to Overleaf? [y/N] ").strip().lower() != "y":
                return 0
            return done(s.push_only())
        if chk.both_changed:
            names = list(s.proj.cfg.agents)
            clean = all(chk.conflicts.get(f, 0) == 0 for f in chk.both_changed)
            opts = names + (["git"] if clean else [])
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
    if a.no_push:
        return 0
    if not a.yes and input("Push the merged paper to Overleaf? [y/N] ").strip().lower() != "y":
        return 0
    return done(s.push_only())


if __name__ == "__main__":
    sys.exit(main())
