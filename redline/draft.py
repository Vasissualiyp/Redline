"""Initial drafting: from a brief (topic, results, literature, earlier papers) to a first draft.

`draft.toml` in the paper holds the brief. Two goals share every step but the last:
  paper   a lit review + outline, then a first-draft paper in the main .tex
  survey  a lit review + a list of claims (source, page, quote), then `survey.tex`: short
          summaries of what the literature claims, for exploring a topic (no paper)

Phases (.redline/draft.json): brief -> reviewed -> approved -> written. The agent works through
the MCP tools draft_* / lit_*; this module is what they call, and what builds the agent's tasks.
The human checkpoint sits between `reviewed` and `approved` (checkpoint = false skips it).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .model import RedlineError
from .state import Project

BRIEF = "draft.toml"
NOTES = "draft"                         # notes directory inside the paper
GOALS = ("paper", "survey")
SOURCES = ("openalex", "arxiv", "ads")
PHASES = ("brief", "reviewed", "approved", "written")
NOTE_NAMES = {"litreview", "outline", "claims"}
MAX_LINES = 600
MAX_BYTES = 3_000_000
OVERWRITE_LIMIT = 1500                  # a main file with more body text than this is not "a new paper"

BRIEF_TEMPLATE = '''\
# What redline should draft. Fill this in, then: redline draft review
topic = ""                  # what the paper / survey is about, as precisely as you can
goal = "paper"              # "paper": lit review + first-draft paper; "survey": lit review + short claim summaries (survey.tex)
title = ""
results_dir = ""            # your results: data, tables, figure scripts, logs (read-only; paper goal)
zotero_collection = ""      # a Zotero collection with the papers you care about (path, e.g. "Pop III/Cooling")
literature = []             # local PDFs, .bib files or folders of them that are not in Zotero
prior_papers = []           # your earlier papers: names from `redline full` or paths to .tex files (style, notation, methods)
template = ""               # a .tex file or folder to start from (journal template, an earlier paper)
notes = """
Anything else: the story you want told, what to stress, what is not to be claimed, the target journal.
"""
search = ["openalex", "arxiv"]   # where the lit review looks beyond what you gave it (openalex, arxiv, ads; ads needs ADS_API_TOKEN)
max_new_papers = 40         # at most this many papers the review adds to Zotero
model = ""                  # a [models] entry (default: draft_model of .redline.toml)
checkpoint = true           # stop after the lit review so you can edit draft/*.md and approve
'''


@dataclass
class Brief:
    topic: str = ""
    goal: str = "paper"
    title: str = ""
    results_dir: str = ""
    zotero_collection: str = ""
    literature: list[str] = field(default_factory=list)
    prior_papers: list[str] = field(default_factory=list)
    template: str = ""
    notes: str = ""
    search: list[str] = field(default_factory=lambda: ["openalex", "arxiv"])
    max_new_papers: int = 40
    model: str = ""
    checkpoint: bool = True


def brief_path(proj: Project) -> Path:
    return proj.cfg.root / BRIEF


def notes_dir(proj: Project) -> Path:
    return proj.cfg.root / NOTES


def load_brief(proj: Project) -> Brief:
    p = brief_path(proj)
    if not p.exists():
        raise RedlineError(f"no {BRIEF} in this paper: run `redline draft init` and fill it in")
    try:
        data = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        raise RedlineError(f"{BRIEF}: {e}")
    b = Brief()
    for k in ("topic", "goal", "title", "results_dir", "zotero_collection", "template", "notes", "model"):
        if k in data:
            setattr(b, k, str(data[k]).strip())
    for k in ("literature", "prior_papers", "search"):
        if k in data:
            v = data[k]
            setattr(b, k, [str(x).strip() for x in ([v] if isinstance(v, str) else v) if str(x).strip()])
    if "max_new_papers" in data:
        b.max_new_papers = max(0, int(data["max_new_papers"]))
    if "checkpoint" in data:
        b.checkpoint = bool(data["checkpoint"])
    if b.goal not in GOALS:
        raise RedlineError(f"{BRIEF}: goal must be one of {', '.join(GOALS)} (not {b.goal!r})")
    bad = [s for s in b.search if s not in SOURCES]
    if bad:
        raise RedlineError(f"{BRIEF}: unknown search source {', '.join(bad)} (choose from {', '.join(SOURCES)})")
    return b


# ---- state --------------------------------------------------------------------------
def _state_path(proj: Project) -> Path:
    return proj.dir / "draft.json"


def state(proj: Project) -> dict:
    try:
        return json.loads(_state_path(proj).read_text())
    except (OSError, ValueError):
        return {"phase": "brief"}


def _set_phase(proj: Project, phase: str, **extra) -> None:
    st = state(proj)
    st.update(extra, phase=phase)
    proj.dir.mkdir(parents=True, exist_ok=True)
    _state_path(proj).write_text(json.dumps(st, indent=1))


def init(proj: Project, topic: str = "", goal: str = "") -> str:
    p = brief_path(proj)
    if p.exists():
        raise RedlineError(f"{BRIEF} already exists; edit it")
    text = BRIEF_TEMPLATE
    if topic:
        text = text.replace('topic = ""', f"topic = {json.dumps(topic)}", 1)
    if goal:
        if goal not in GOALS:
            raise RedlineError(f"goal must be one of {', '.join(GOALS)}")
        text = text.replace('goal = "paper"', f'goal = "{goal}"', 1)
    p.write_text(text)
    _set_phase(proj, "brief")
    return f"wrote {p}: fill it in, then run `redline draft review`"


def target(proj: Project, b: Brief) -> Path:
    return proj.cfg.root / ("survey.tex" if b.goal == "survey" else proj.cfg.main)


def required_notes(b: Brief) -> list[str]:
    return ["litreview", "claims"] if b.goal == "survey" else ["litreview", "outline"]


def status(proj: Project) -> str:
    try:
        b = load_brief(proj)
    except RedlineError as e:
        return str(e)
    st = state(proj)
    have = [n for n in required_notes(b) if (notes_dir(proj) / f"{n}.md").exists()]
    out = [f"goal: {b.goal}   phase: {st['phase']}   topic: {b.topic or '(empty: fill in the brief)'}",
           f"notes: {', '.join(f'{NOTES}/{n}.md' for n in have) or 'none yet'}"]
    nxt = {"brief": "redline draft review", "reviewed": "edit draft/*.md, then redline draft approve",
           "approved": "redline draft write", "written": f"read {target(proj, b).name}; redline verification for the citations"}
    out.append("next: " + nxt[st["phase"]])
    return "\n".join(out)


def approve(proj: Project) -> str:
    st = state(proj)
    if st["phase"] not in ("reviewed", "approved", "written"):
        raise RedlineError("nothing to approve: run `redline draft review` first")
    _set_phase(proj, "approved" if st["phase"] == "reviewed" else st["phase"])
    return "approved: `redline draft write` writes the " + ("survey" if load_brief(proj).goal == "survey" else "draft")


# ---- the materials the human supplied (read-only, sandboxed) ----------------------------
def _expand(p: str, root: Path | None = None) -> Path:
    q = Path(p).expanduser()
    return q if q.is_absolute() or root is None else root / q


def materials(proj: Project, b: Brief) -> list[tuple[str, Path]]:
    """(role, path) of everything in the brief that is a file or folder. Prior papers that are
    names of other papers (not paths) are read with the paper_* tools instead."""
    out = []
    if b.results_dir:
        out.append(("results", _expand(b.results_dir, proj.cfg.root)))
    for x in b.literature:
        out.append(("literature", _expand(x, proj.cfg.root)))
    if b.template:
        out.append(("template", _expand(b.template, proj.cfg.root)))
    for x in b.prior_papers:
        p = _expand(x, proj.cfg.root)
        if "/" in x or x.endswith(".tex") or p.exists():
            out.append(("prior paper", p))
    return out


def overview(proj: Project) -> str:
    b = load_brief(proj)
    rows = [f"topic: {b.topic}", f"goal: {b.goal}", f"title: {b.title or '(none)'}"]
    if b.notes:
        rows.append("notes from the human:\n" + b.notes)
    for role, p in materials(proj, b):
        if p.is_dir():
            n = sum(1 for f in p.rglob("*") if f.is_file() and not f.name.startswith("."))
            rows.append(f"{role}: {p}/  ({n} files; draft_list / draft_read / draft_search)")
        elif p.is_file():
            rows.append(f"{role}: {p}  ({p.stat().st_size} bytes; draft_read)")
        else:
            rows.append(f"{role}: {p}  (MISSING on this machine)")
    names = [x for x in b.prior_papers if not any(str(p) == str(_expand(x, proj.cfg.root)) for _, p in materials(proj, b))]
    if names:
        rows.append("prior papers by name (read with papers() / paper_outline / paper_read): " + ", ".join(names))
    if b.zotero_collection:
        rows.append(f"Zotero collection: {b.zotero_collection} (the zotero tools read it)")
    rows.append(f"search sources: {', '.join(b.search) or 'none'}; room for {b.max_new_papers} new papers in Zotero")
    return "\n".join(rows)


def _resolve(proj: Project, b: Brief, path: str) -> Path:
    """An absolute path (or one relative to results_dir) that lies inside a supplied path."""
    mats = [p.resolve() for _, p in materials(proj, b) if p.exists()]
    raw = _expand(path)
    cands = [raw] if raw.is_absolute() else [(_expand(b.results_dir, proj.cfg.root) / raw) if b.results_dir else raw]
    for c in cands:
        r = c.resolve()
        for m in mats:
            if r == m or m in r.parents:
                return r
    raise RedlineError(f"{path}: not inside anything the brief supplies (draft_list() shows what is readable)")


def _pdf_text(p: Path) -> str:
    if not shutil.which("pdftotext"):
        raise RedlineError("pdftotext is not installed, so this PDF cannot be read here (it may be in Zotero: use the zotero tools)")
    r = subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RedlineError(f"{p.name}: pdftotext failed")
    return r.stdout


def _text(p: Path) -> str:
    if p.suffix.lower() == ".pdf":
        return _pdf_text(p)
    if p.stat().st_size > MAX_BYTES:
        raise RedlineError(f"{p.name}: too large to read here ({p.stat().st_size} bytes)")
    return p.read_text(errors="replace")


def listing(proj: Project, path: str = "", limit: int = 200) -> str:
    b = load_brief(proj)
    if not path:
        return overview(proj)
    p = _resolve(proj, b, path)
    if p.is_file():
        return f"{p}  ({p.stat().st_size} bytes)"
    rows = []
    for f in sorted(p.rglob("*")):
        if f.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in f.relative_to(p).parts):
            rows.append(f"{f.relative_to(p)}  ({f.stat().st_size} bytes)")
            if len(rows) >= limit:
                rows.append(f"... stopped at {limit} files; list a subfolder")
                break
    return "\n".join(rows) or "(empty)"


def read(proj: Project, path: str, first: int | None = None, last: int | None = None) -> str:
    b = load_brief(proj)
    p = _resolve(proj, b, path)
    if not p.is_file():
        raise RedlineError(f"{path}: not a file (draft_list shows what exists)")
    lines = _text(p).splitlines()
    a = max(1, first or 1)
    z = min(len(lines), last or a + MAX_LINES - 1, a + MAX_LINES - 1)
    return f"{p.name}  lines {a}-{z} of {len(lines)}\n" + "\n".join(f"{n:5d}  {lines[n - 1]}" for n in range(a, z + 1))


def search(proj: Project, query: str, path: str = "", regex: bool = False, limit: int = 60) -> str:
    b = load_brief(proj)
    if not query:
        raise RedlineError("give a search text")
    pat = re.compile(query if regex else re.escape(query), re.I)
    roots = [_resolve(proj, b, path)] if path else [p.resolve() for _, p in materials(proj, b) if p.exists()]
    hits = []
    for r in roots:
        files = [r] if r.is_file() else sorted(f for f in r.rglob("*") if f.is_file())
        for f in files:
            if f.suffix.lower() == ".pdf" and r != f:
                continue                      # PDFs only when named: converting a folder of them is slow
            if f.suffix.lower() not in {".pdf", ".tex", ".bib", ".txt", ".md", ".csv", ".tsv", ".json", ".py", ".dat",
                                        ".log", ".out", ".yaml", ".yml", ".toml", ".ipynb", ".rst"}:
                continue
            try:
                lines = _text(f).splitlines()
            except (RedlineError, OSError):
                continue
            for n, ln in enumerate(lines, 1):
                if pat.search(ln):
                    hits.append(f"{f}:{n}: {ln.strip()[:200]}")
                    if len(hits) >= limit:
                        return "\n".join(hits) + f"\n... stopped at {limit} matches; narrow the search"
    return "\n".join(hits) or "no matches"


# ---- open literature search -------------------------------------------------------------
def _get(url: str, headers: dict | None = None, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "redline-draft", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    words = {}
    for w, pos in inv.items():
        for i in pos:
            words[i] = w
    return " ".join(words[i] for i in sorted(words))


def _fmt(n: int, title: str, authors: list[str], year, venue: str, ids: dict, abstract: str, extra: str = "") -> str:
    au = ", ".join(authors[:6]) + (" et al." if len(authors) > 6 else "")
    idtxt = "  ".join(f"{k}={v}" for k, v in ids.items() if v)
    out = f"[{n}] {title} ({au}, {year}{', ' + venue if venue else ''}){extra}\n    {idtxt}"
    if abstract:
        out += "\n    " + abstract[:600].replace("\n", " ") + ("..." if len(abstract) > 600 else "")
    return out


def lit_search(proj: Project, query: str, source: str = "openalex", limit: int = 10) -> str:
    b = load_brief(proj)
    if source not in b.search:
        raise RedlineError(f"search source {source!r} is not enabled in the brief (search = {b.search})")
    limit = max(1, min(25, limit))
    try:
        if source == "openalex":
            q = urllib.parse.urlencode({"search": query, "per-page": limit})
            data = json.loads(_get(f"https://api.openalex.org/works?{q}"))
            rows = []
            for i, w in enumerate(data.get("results", []), 1):
                au = [(a.get("author") or {}).get("display_name", "") for a in w.get("authorships", [])]
                venue = (((w.get("primary_location") or {}).get("source")) or {}).get("display_name", "")
                rows.append(_fmt(i, w.get("title") or "", au, w.get("publication_year"), venue,
                                 {"doi": (w.get("doi") or "").removeprefix("https://doi.org/")},
                                 _abstract(w.get("abstract_inverted_index")),
                                 f"  cited {w.get('cited_by_count', 0)}x"))
        elif source == "arxiv":
            q = urllib.parse.urlencode({"search_query": f"all:{query}", "max_results": limit})
            root = ET.fromstring(_get(f"http://export.arxiv.org/api/query?{q}"))
            ns = {"a": "http://www.w3.org/2005/Atom"}
            rows = []
            for i, e in enumerate(root.findall("a:entry", ns), 1):
                g = lambda t: (e.findtext(f"a:{t}", "", ns) or "").strip()  # noqa: E731
                au = [(x.findtext("a:name", "", ns) or "").strip() for x in e.findall("a:author", ns)]
                rows.append(_fmt(i, " ".join(g("title").split()), au, g("published")[:4], "arXiv",
                                 {"eprint": g("id").rsplit("/abs/", 1)[-1]}, " ".join(g("summary").split())))
        else:
            import os
            tok = os.environ.get("ADS_API_TOKEN")
            if not tok:
                raise RedlineError("ADS_API_TOKEN is not set")
            q = urllib.parse.urlencode({"q": query, "rows": limit, "fl": "title,author,year,bibcode,doi,abstract,pub"})
            data = json.loads(_get(f"https://api.adsabs.harvard.edu/v1/search/query?{q}",
                                   {"Authorization": f"Bearer {tok}"}))
            rows = [_fmt(i, (d.get("title") or [""])[0], d.get("author", []), d.get("year"), d.get("pub", ""),
                         {"doi": (d.get("doi") or [""])[0], "bibcode": d.get("bibcode")}, d.get("abstract", ""))
                    for i, d in enumerate(data.get("response", {}).get("docs", []), 1)]
    except (OSError, ValueError, ET.ParseError) as e:
        raise RedlineError(f"{source} search failed: {e}")
    return "\n".join(rows) or f"{source}: no results for {query!r}"


def bib_file(proj: Project) -> Path:
    return proj.cfg.root / "references.bib"


def lit_add(proj: Project, key: str, title: str, authors: list[str], year: str, venue: str = "", doi: str = "",
            eprint: str = "", zotero: bool = True) -> str:
    """Add a found paper to references.bib and (best effort) to Zotero with its PDF."""
    b = load_brief(proj)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9:_-]*", key):
        raise RedlineError(f"{key!r}: a bib key is letters, digits, : _ - (starting with a letter)")
    if not (doi or eprint):
        raise RedlineError("give a doi or an arXiv eprint: a paper without either cannot be fetched or verified")
    path = bib_file(proj)
    cur = path.read_text() if path.exists() else ""
    if re.search(r"@\w+\{" + re.escape(key) + r",", cur):
        raise RedlineError(f"{key}: already in references.bib")
    added = state(proj).get("added", [])
    if len(added) >= b.max_new_papers:
        raise RedlineError(f"already added {len(added)} papers (max_new_papers = {b.max_new_papers})")
    fields = {"author": " and ".join(authors), "title": "{" + title + "}", "year": str(year)}
    if venue:
        fields["journal"] = venue
    if doi:
        fields["doi"] = doi
    if eprint:
        fields["eprint"] = eprint
        fields["archivePrefix"] = "arXiv"
    entry = f"@article{{{key},\n" + "".join(f"  {k} = {{{v}}},\n" for k, v in fields.items()) + "}\n"
    path.write_text(cur + ("\n" if cur and not cur.endswith("\n\n") else "") + entry)
    _set_phase(proj, state(proj)["phase"], added=added + [key])
    msg = f"{key} added to references.bib"
    if zotero:
        from . import zotero as _z
        try:
            zot = _z.Zotero(path)
            zot.paper = proj.cfg.root.name
            zot.pull(key)
            msg += "; in Zotero with its PDF (read it with the zotero tools)"
        except Exception as e:                                   # noqa: BLE001  best effort, said plainly
            msg += f"; NOT in Zotero ({str(e).splitlines()[0][:200]}): do not cite claims you cannot read in a PDF"
    return msg


# ---- what the agent writes -----------------------------------------------------------------
def write_note(proj: Project, name: str, text: str) -> str:
    if name not in NOTE_NAMES:
        raise RedlineError(f"note name must be one of {', '.join(sorted(NOTE_NAMES))}")
    if not text.strip():
        raise RedlineError("empty note")
    d = notes_dir(proj)
    d.mkdir(exist_ok=True)
    (d / f"{name}.md").write_text(text if text.endswith("\n") else text + "\n")
    return f"{NOTES}/{name}.md written ({len(text.split())} words)"


def _cite_keys(tex: str) -> set[str]:
    code = re.sub(r"(?<!\\)%.*", "", tex)
    return {k.strip() for m in re.finditer(r"\\cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}", code)
            for k in m.group(1).split(",") if k.strip()}


def _bib_keys(proj: Project) -> set[str]:
    keys = set()
    for f in proj.cfg.root.glob("*.bib"):
        keys |= set(re.findall(r"@\w+\{([^,\s]+),", f.read_text(errors="replace")))
    return keys


def check_write(proj: Project, b: Brief, force: bool = False) -> None:
    st = state(proj)
    if st["phase"] not in ("approved", "written"):
        raise RedlineError("the lit review is not approved yet: look at draft/*.md, then `redline draft approve`")
    if b.goal == "paper" and st["phase"] == "approved" and not (force or st.get("force")):
        body = re.sub(r"(?s)^.*?\\begin\{document\}", "", proj.read())
        if len(body.strip()) > OVERWRITE_LIMIT:
            raise RedlineError(f"{proj.cfg.main} already has {len(body.strip())} characters of text: drafting would "
                               "replace it (undoable, but say so with --force)")


def write_tex(proj: Project, text: str, file: str = "") -> str:
    """Write the draft (goal paper: the main file, through the journal so `undo` works; goal
    survey: survey.tex). Reports citations the .bib lacks or the lit review never mentioned."""
    import os
    if os.environ.get("REDLINE_READONLY"):
        raise RedlineError("this session is read-only (a lit-review run): it may only write draft/*.md notes")
    b = load_brief(proj)
    check_write(proj, b)
    tgt = target(proj, b)
    if file and Path(file).name != tgt.name:
        raise RedlineError(f"write {tgt.name} (this brief's goal is {b.goal}), not {file}")
    if "\\begin{document}" not in text or "\\end{document}" not in text:
        raise RedlineError("a complete document is needed: \\documentclass ... \\begin{document} ... \\end{document}")
    if b.goal == "survey":
        tgt.write_text(text)
    else:
        cur = proj.read()
        proj.write(text, expect=cur, reason="first draft")
        from . import preamble
        preamble.ensure(tgt)
    _set_phase(proj, "written", force=False)
    return "\n".join([f"{tgt.name} written ({len(text.split())} words, {len(_cite_keys(text))} cited papers)",
                      *cite_report(proj, text)])


def cite_report(proj: Project, text: str) -> list[str]:
    """Warnings about the citations of a draft: keys no .bib has, keys the lit review notes never mention."""
    notes = "\n".join(p.read_text(errors="replace") for p in notes_dir(proj).glob("*.md")) if notes_dir(proj).exists() else ""
    keys = _cite_keys(text)
    missing = sorted(keys - _bib_keys(proj))
    unread = sorted(k for k in keys - set(missing) if k not in notes)
    out = []
    if missing:
        out.append("CITED BUT NOT IN ANY .bib (add them with lit_add or fix the key): " + ", ".join(missing))
    if unread:
        out.append("cited but never mentioned in the lit review notes (justify or remove): " + ", ".join(unread))
    return out


# ---- tasks for the agent -----------------------------------------------------------------------
def review_task(b: Brief) -> str:
    out = ["You are doing the literature review for a new " + ("paper" if b.goal == "paper" else "topic survey")
           + " with the Redline tools. Do NOT write the paper yet and do not edit any .tex file.",
           "1. Call draft_brief() and read everything it lists: the human's notes, results, literature, template and "
           "earlier papers (draft_list / draft_read / draft_search; papers() / paper_outline / paper_read for named papers; "
           "the zotero tools for the library"
           + (f" and the collection {b.zotero_collection!r}" if b.zotero_collection else "") + ").",
           "2. Search beyond what was given with lit_search(query, source) over the enabled sources: "
           f"{', '.join(b.search)}. Vary the queries (the topic, its methods, competing explanations, the latest results); "
           "follow the reference lists of the key papers. Add only papers that matter with lit_add (it needs a doi or "
           f"arXiv id; at most {b.max_new_papers}) and read each one you will cite in Zotero.",
           "3. Every statement about a paper that you record must be something you read in its PDF: keep the exact quote and "
           "the page. Never cite from memory or from an abstract alone; if a PDF is unavailable, say so and do not rely on it.",
           "4. Write the notes with draft_note(name, text):"]
    if b.goal == "paper":
        out += ["   - 'litreview': the state of the field in themed groups. For each paper: bib key, one-line claim, "
                "quote, page. Then: what is established, what is contested, what is missing, and where the human's "
                "results fit (read the results first).",
                "   - 'outline': the proposed paper, section by section with the points each makes, which results "
                "(files, figures, numbers) and which papers back each point, and the story in 3 sentences."]
    else:
        out += ["   - 'litreview': the state of the field in themed groups; what is established, contested, missing.",
                "   - 'claims': one entry per claim worth knowing: the claim in 1-2 sentences, bib key, page, exact "
                "quote, and whether other papers agree or disagree (name them)."]
    out.append("Be thorough; this is the base the draft stands on. Finish by saying what you could not find or read.")
    return "\n".join(out)


def write_task(proj: Project, b: Brief) -> str:
    notes = [f"{NOTES}/{n}.md" for n in required_notes(b)]
    base = ["Read " + ", ".join(notes) + " first (the human may have edited them: their edits win), then draft_brief()."]
    if b.goal == "survey":
        return "\n".join(base + [
            "Write survey.tex with draft_write(text): a complete LaTeX article (\\documentclass ... \\end{document}) that "
            "is a compact exploratory survey of the topic. Group the claims from draft/claims.md by theme (\\section); "
            "under each, short paragraphs or an itemize of 1-3 sentence summaries, each ending with \\cite{key} (and the "
            "page in the optional argument, \\cite[p.~N]{key}). Mark disagreements and open questions explicitly in their "
            "own subsections. End with a \\section{Open questions} and \\bibliography{references} with a style. "
            "Use only claims in the notes; do not invent any. Then compile() and fix errors."])
    tmpl = (f"Start from the template {b.template!r} (read it with draft_read; keep its preamble and structure). "
            if b.template else "Start from the main file's current preamble. ")
    return "\n".join(base + [
        tmpl + "Match the style, notation and macros of the human's earlier papers where they are given.",
        f"Write the first-draft paper with draft_write(text) as a complete LaTeX document following draft/outline.md: "
        "abstract, introduction (context from the lit review, the gap, what this paper does), methods (from the earlier "
        "papers and the results' scripts/logs), results (only what the results directory shows; quote the real numbers and "
        "reference the real figures and tables with \\ref), discussion (against the literature), conclusions.",
        "Rules: every number comes from the results (say where in a % comment); every literature claim is cited with "
        "\\cite{key} for a key in references.bib that the lit review read; never invent a result, a number or a "
        "reference. Where something is missing, leave a % TODO line and say it in your final message. Keep "
        "\\bibliography{references}. Then compile() and fix errors."])
