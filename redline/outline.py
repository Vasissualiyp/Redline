"""High-level overview: one "thought" per paragraph, figure, table, list item, equation.

The paper is cut into blocks (paragraphs, figures, tables, lists and their items, equations,
headings). Every block is hashed from its *accepted* text (pending `changes` markup resolved as
proposed; comments, review markers and result lines ignored), so a thought belongs to exactly
this wording: edit the paragraph and its thought goes stale (it stays visible, marked ~, until
refreshed). Thoughts, flow flags and the user's reordering plan live in .redline/outline.json.

Who writes the thoughts is an agent, through the MCP tools `outline_pending` / `record_thoughts`
(a cheap model of your choice, see `outline_model` in the config), never this module.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from dataclasses import dataclass, field

from .model import Change, Document, RedlineError
from .state import Project

LIST_ENVS = {"itemize", "enumerate", "description"}
FLOAT_ENVS = {"figure": "figure", "figure*": "figure", "wrapfigure": "figure", "table": "table", "table*": "table"}
MATH_ENVS = {"equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*", "eqnarray"}
_BEGIN = re.compile(r"^[ \t]*\\begin\{([A-Za-z*]+)\}")
_HEAD = re.compile(r"^[ \t]*\\(section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\s*\{")
_COMMENT_LINE = re.compile(r"^[ \t]*%")
FLOW_KINDS = ("gap", "repeat", "forward-ref", "weak-transition", "orphan-figure", "other")
DEFAULT_WORDS = 10   # a thought is a glance, not a summary
SLACK = 4             # over words + slack is refused so the agent shortens it
MAX_THOUGHT = 140


@dataclass
class Block:
    kind: str                # heading | paragraph | abstract | figure | table | list | item | equation | env
    start: int
    end: int
    line_start: int
    line_end: int
    depth: int = 0
    level: int = 0           # headings: 1 section, 2 subsection, 3 subsubsection
    title: str = ""
    number: str = ""        # headings: the section number as typeset ("3.4.1"; "" for starred)
    text: str = ""           # readable accepted text
    hash: str = ""
    parent: int = -1         # index of the enclosing heading or list block
    list_ref: object = None  # items: the list block they belong to (while cutting)

    def counts(self) -> dict:
        return {"chars": len(self.text), "words": len(self.text.split()),
                "nlines": self.line_end - self.line_start + 1}


# ---- reading a block as the reader would --------------------------------------------
def accepted(doc: Document, a: int, b: int) -> str:
    """doc.src[a:b] with every pending change resolved as proposed."""
    src = doc.src
    out, pos = [], a
    for it in sorted((i for i in doc.items if i.start < b and i.full_end > a and i.nested_in is None
                      and i.attached_to is None), key=lambda i: i.start):
        s, e = max(it.start, a), min(it.full_end, b)
        out.append(src[pos:s])
        if it.kind in ("added", "replaced", "highlight"):
            out.append(it.new(src) or "")
        pos = e
    out.append(src[pos:b])
    return "".join(out)


def _strip_comments(text: str) -> str:
    lines = [ln for ln in text.splitlines() if not _COMMENT_LINE.match(ln)]
    return "\n".join(re.sub(r"(?<!\\)%.*$", "", ln) for ln in lines)


def readable(text: str) -> str:
    """Plain words: citations and refs as [key], simple text commands unwrapped, other commands dropped."""
    t = _strip_comments(text)
    maths: list[str] = []

    def keep_math(m: re.Match) -> str:
        maths.append(m.group(0))
        return f"\u27e6{len(maths) - 1}\u27e7"
    t = re.sub(r"(?<!\\)\$\$.*?(?<!\\)\$\$|(?<!\\)\$.*?(?<!\\)\$", keep_math, t, flags=re.S)  # math stays as written
    t = re.sub(r"\\(?:cite[a-z]*|nocite)\*?(?:\[[^\]]*\])*\{([^}]*)\}", lambda m: f"[{m.group(1)}]", t)
    t = re.sub(r"\\(?:auto|c|C|eq|v)?ref\*?\{[^}]*\}", "[ref]", t)
    t = re.sub(r"\\label\{[^}]*\}", "", t)
    for _ in range(4):
        t = re.sub(r"\\(?:textbf|textit|emph|textsc|texttt|underline|mbox|text)\s*\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\(?:begin|end)\{[^}]*\}(?:\[[^\]]*\])?", " ", t)
    t = re.sub(r"\\(?:centering|noindent|par|clearpage|newpage|small|footnotesize)\b", " ", t)
    t = re.sub(r"\\item\b", "•", t)
    t = re.sub(r"\\(?:includegraphics)(?:\[[^\]]*\])?\{([^}]*)\}", lambda m: f"<{m.group(1).rsplit('/', 1)[-1]}>", t)
    t = re.sub(r"\\caption(?:\[[^\]]*\])?\{", "Caption: {", t)
    t = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?", " ", t)
    t = re.sub(r"[{}]", "", t).replace("~", " ")
    t = re.sub("\u27e6(\\d+)\u27e7", lambda m: " ".join(maths[int(m.group(1))].split()), t)
    return " ".join(t.split())


def _hash(kind: str, text: str) -> str:
    return hashlib.sha1((kind + "\0" + " ".join(text.split())).encode()).hexdigest()[:10]


# ---- cutting the paper into blocks ------------------------------------------------
def _match_end(src: str, name: str, start: int, limit: int) -> int:
    """Offset just after the \\end{name} that closes the \\begin{name} at `start`."""
    depth, pos = 0, start
    pat = re.compile(r"\\(begin|end)\{" + re.escape(name) + r"\}")
    for m in pat.finditer(src, start, limit):
        depth += 1 if m.group(1) == "begin" else -1
        if depth == 0:
            return m.end()
    return limit


def _brace_end(src: str, start: int, limit: int) -> int:
    """Offset after the group whose opening brace is the first { at or after start."""
    i = src.find("{", start, limit)
    if i < 0:
        return start
    depth = 0
    while i < limit:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        depth += c == "{"
        depth -= c == "}"
        i += 1
        if depth == 0:
            return i
    return limit


def _items(doc: Document, a: int, b: int) -> list[tuple[int, int]]:
    """Spans of the \\item entries of the list whose inner text is src[a:b] (top level of that list)."""
    src, depth, marks = doc.src, 0, []
    for m in re.finditer(r"\\(begin|end)\{[A-Za-z*]+\}|\\item\b", src[a:b]):
        tok = m.group(0)
        if tok.startswith("\\begin"):
            depth += 1
        elif tok.startswith("\\end"):
            depth -= 1
        elif depth == 0:
            marks.append(a + m.start())
    return [(s, marks[i + 1] if i + 1 < len(marks) else b) for i, s in enumerate(marks)]


def blocks(doc: Document) -> list[Block]:
    src = doc.src
    lo, hi = doc.body_start, doc.body_end
    starts = doc.line_starts
    heads = {s.start: s for s in doc.sections}
    out: list[Block] = []

    def mk(kind: str, a: int, b: int, **kw) -> Block | None:
        while b > a and src[b - 1] in " \t\r\n":
            b -= 1
        raw = accepted(doc, a, b)
        if kind == "equation":  # an equation reads as the equation
            text = " ".join(re.sub(r"\\(?:begin|end)\{[^}]*\}|\\label\{[^}]*\}", " ", _strip_comments(raw)).split())
        else:
            text = readable(raw)
        if kind != "heading" and len(text.split()) < 3 and kind not in ("figure", "table", "equation"):
            return None
        blk = Block(kind, a, b, doc.line_of(a), doc.line_of(max(a, b - 1)), text=text, **kw)
        blk.hash = _hash(kind, accepted(doc, a, b) if kind != "heading" else kw.get("title", ""))
        out.append(blk)
        return blk

    pos = lo
    para: int | None = None

    def close_para(end: int) -> None:
        nonlocal para
        if para is not None:
            mk("paragraph", para, end)
            para = None

    ls = [x for x in starts if lo <= x < hi]
    i = 0
    while i < len(ls):
        a = ls[i]
        eol = src.find("\n", a, hi)
        eol = hi if eol < 0 else eol
        line = src[a:eol]
        if not line.strip() or _COMMENT_LINE.match(line):
            if not line.strip():
                close_para(a)
            i += 1
            continue
        first = a + (len(line) - len(line.lstrip()))
        sec = heads.get(first)
        m_env = _BEGIN.match(line)
        if sec is not None and _HEAD.match(line):
            close_para(a)
            end = _brace_end(src, first, hi)
            mk("heading", a, end, level=sec.level, title=sec.title, number=sec.number)
            while i < len(ls) and ls[i] < end:
                i += 1
            continue
        if m_env and m_env.group(1) != "document":
            name = m_env.group(1)
            end = _match_end(src, name, first, hi)
            close_para(a)
            if name == "abstract":
                mk("abstract", a, end)
            elif name in FLOAT_ENVS:
                mk(FLOAT_ENVS[name], a, end)
            elif name in MATH_ENVS:
                mk("equation", a, end)
            elif name in LIST_ENVS:
                lst = mk("list", a, end)
                if lst is not None:
                    inner_a = src.find("}", first) + 1
                    inner_b = src.rfind("\\end{", first, end)
                    for s, e in _items(doc, inner_a, inner_b if inner_b > 0 else end):
                        it = mk("item", s, e, list_ref=lst)
            elif name in ("center", "minipage", "algorithm", "algorithmic", "verbatim", "lstlisting", "tikzpicture"):
                mk("env", a, end)
            else:  # an unknown environment inside running text: leave it with its paragraph
                para = a if para is None else para
            while i < len(ls) and ls[i] < end:
                i += 1
            continue
        if re.match(r"^[ \t]*\\(?:end\{document\}|bibliography|bibliographystyle|maketitle|label|clearpage|newpage|appendix)", line):
            close_para(a)
            i += 1
            continue
        if para is None:
            para = a
        i += 1
    close_para(hi)
    first_real = next((k for k, b in enumerate(out) if b.kind in ("abstract", "heading")), None)
    if first_real:  # title, authors, affiliations: not part of the argument
        out = out[first_real:]
    # nesting: blocks belong to the closest heading before them (items keep their list)
    stack: list[int] = []
    idx_of = {id(b): k for k, b in enumerate(out)}
    for k, blk in enumerate(out):
        if blk.kind == "heading":
            while stack and out[stack[-1]].level >= blk.level:
                stack.pop()
            blk.parent = stack[-1] if stack else -1
            blk.depth = len(stack)
            stack.append(k)
        elif blk.kind == "item":
            blk.parent = idx_of[id(blk.list_ref)]
            blk.depth = out[blk.parent].depth + 1
        else:
            blk.parent = stack[-1] if stack else -1
            blk.depth = len(stack)
    return out


# ---- the store ----------------------------------------------------------------------
def _path(proj: Project):
    return proj.dir / "outline.json"


def load_store(proj: Project) -> dict:
    try:
        data = json.loads(_path(proj).read_text())
    except (OSError, ValueError):
        data = {}
    for k, v in (("thoughts", {}), ("last", []), ("flags", []), ("plan", None), ("suggestions", [])):
        data.setdefault(k, v)
    return data


def save_store(proj: Project, data: dict) -> None:
    proj.dir.mkdir(parents=True, exist_ok=True)
    tmp = _path(proj).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(_path(proj))


def _carry(old: list[dict], new: list[Block], thoughts: dict) -> dict[int, dict]:
    """Stale thoughts for edited blocks: new index -> the thought of the block it grew out of."""
    carried: dict[int, dict] = {}
    sm = difflib.SequenceMatcher(None, [o["hash"] for o in old], [b.hash for b in new], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace":
            continue
        used = set()
        for j in range(j1, j2):
            best, score = None, 0.45
            for i in range(i1, i2):
                if i in used or old[i]["kind"] != new[j].kind or old[i]["hash"] not in thoughts:
                    continue
                r = difflib.SequenceMatcher(None, old[i]["head"], new[j].text[:200], autojunk=False).ratio()
                if r > score:
                    best, score = i, r
            if best is not None:
                used.add(best)
                carried[j] = thoughts[old[best]["hash"]]
    return carried


def _span(nodes: list[dict], k: int) -> int:
    """How many nodes follow heading k inside its section (subsections included)."""
    n = 0
    for m in nodes[k + 1:]:
        if m["kind"] == "heading" and m["depth"] <= nodes[k]["depth"]:
            break
        n += 1
    return n


def review_stats(doc: Document, a: int, b: int) -> dict:
    """What in doc.src[a:b] waits for a human: pending edits (by their review marker) and comments."""
    st = {"edits": 0, "unrated": 0, "accepted": 0, "rejected": 0, "noted": 0, "comments": 0}
    for it in doc.items:
        if it.start >= b or it.full_end <= a:
            continue
        if it.kind == "comment":
            st["comments"] += 1
            continue
        st["edits"] += 1
        k = it.marker.kind if it.marker else None
        st["unrated"] += k is None
        st["accepted"] += k == "Y"
        st["rejected"] += k == "X"
        st["noted"] += k == "N"
        st["comments"] += it.comment is not None   # the explanation of a change is a comment to read
    return st


def build(proj: Project, doc: Document | None = None) -> dict:
    """The overview: {nodes: [...flat, in order...], stale, missing, total, flags, plan}."""
    doc = doc or proj.doc()
    bl = blocks(doc)
    store = load_store(proj)
    thoughts = store["thoughts"]
    carried = _carry(store["last"], bl, thoughts)
    hashes = {b.hash for b in bl}
    flags = [f for f in store["flags"] if set(f.get("hashes", [])) <= hashes]
    nodes = []
    for k, b in enumerate(bl):
        t = thoughts.get(b.hash)
        stale = False
        if t is None and k in carried:
            t, stale = carried[k], True
        node = {"index": k, "hash": b.hash, "kind": b.kind, "depth": b.depth, "level": b.level, "title": b.title,
                "number": b.number,
                "lines": [b.line_start, b.line_end], "text": b.text if b.kind != "heading" else b.title,
                "thought": t["thought"] if t else None, "stale": stale, "model": t.get("model", "") if t else "",
                "split": (t.get("split") if t and not stale and not t.get("split_dismissed") else None),
                "suggestions": [x for x in store.get("suggestions", [])
                                if x["hashes"][0] == b.hash and not x.get("dismissed") and set(x["hashes"]) <= hashes],
                "parent": b.parent, "review": review_stats(doc, b.start, b.end), "flags": [f for f in flags if b.hash in f.get("hashes", [])], **b.counts()}
        if b.kind == "heading":
            node["chars"] = node["words"] = 0
        nodes.append(node)
    # a section's own counts: all words below it
    for k in reversed(range(len(nodes))):
        n = nodes[k]
        if n["kind"] == "heading":
            sub = [m for m in nodes[k + 1:] if m["depth"] > n["depth"]] if False else []
    for k, n in enumerate(nodes):
        if n["kind"] == "heading":
            tot_c = tot_w = 0
            for m in nodes[k + 1:]:
                if m["kind"] == "heading" and m["depth"] <= n["depth"]:
                    break
                if m["kind"] not in ("heading", "item"):
                    tot_c += m["chars"]
                    tot_w += m["words"]
            n["chars"], n["words"] = tot_c, tot_w
            tot = {k: 0 for k in n["review"]}
            for m in nodes[k + 1:k + 1 + _span(nodes, k)]:
                if m["kind"] not in ("heading", "item"):
                    for kk, vv in m["review"].items():
                        tot[kk] += vv
            n["review"] = tot
            n["nlines"] = sum(m["nlines"] for m in nodes[k + 1:k + 1 + _span(nodes, k)] if m["kind"] not in ("heading", "item"))
    saved_last = [{"hash": b.hash, "kind": b.kind, "head": b.text[:200]} for b in bl]
    if [x["hash"] for x in store["last"]] != [x["hash"] for x in saved_last] or flags != store["flags"]:
        store["last"], store["flags"] = saved_last, flags
        save_store(proj, store)
    body = [n for n in nodes if n["kind"] != "heading"]
    return {"nodes": nodes, "total": len(body), "stale": sum(1 for n in body if n["stale"]),
            "splits": sum(1 for n in body if n.get("split")),
            "to_review": sum(1 for n in body if n["review"]["edits"] or n["review"]["comments"]),
            "dismissed": sum(1 for t in thoughts.values() if t.get("split_dismissed"))
                         + sum(1 for x in store.get("suggestions", []) if x.get("dismissed")),
            "thoughts": sum(1 for n in body if n["thought"] is not None),
            "model": getattr(proj.cfg, "outline_model", ""), "review_after": bool(getattr(proj.cfg, "outline_review", True)),
            "suggestions": sum(len(n["suggestions"]) for n in body),
            "missing": sum(1 for n in body if n["thought"] is None), "flags": len(flags),
            "plan": store["plan"]}


# ---- what the agent does ----------------------------------------------------------
def split_words(proj: Project) -> int:
    return max(60, int(getattr(proj.cfg, "outline_split_words", 200)))


def words_limit(proj: Project) -> int:
    return max(4, int(getattr(proj.cfg, "outline_words", DEFAULT_WORDS)))


def pending(proj: Project, limit: int = 40) -> str:
    o = build(proj)
    todo = [n for n in o["nodes"] if n["kind"] != "heading" and (n["thought"] is None or n["stale"])]
    if not todo:
        return "every block has an up-to-date thought"
    path = []
    w = words_limit(proj)
    out = [f"{len(todo)} block(s) need a thought (showing {min(limit, len(todo))}). For each: a SHORT phrase, "
           f"{w} words or fewer, no filler, in the paper's terms: the one idea the block adds (a figure: what it shows; "
           "a list: its point; an equation: what it defines). Telegraphic is fine ('Turbulence drives fragmentation', "
           "'Disk Q profile over time'). Then call record_thoughts once with [{hash, thought}, ...].",
           f"SPLIT RECOMMENDED: only when a block of {split_words(proj)}+ words holds two or more SEPARATE ideas that are each "
           "substantial (roughly 80+ words of their own, e.g. a long paragraph that describes two figure panels at length). "
           "Then add \"split\": [\"first idea\", \"second idea\"] (short phrases) to its item. A short block is never "
           "flagged, however many things it mentions: a 100-word paragraph describing two panels is fine, as long as the "
           "panels are related (a reader wants them together); neither is one idea told at length. Unrelated ideas "
           "glued into one paragraph are the case to flag.", ""]
    sec = ""
    for n in todo[:limit]:
        for h in reversed(o["nodes"][:n["index"]]):
            if h["kind"] == "heading":
                sec = h["title"]
                break
        out.append(f"- hash {n['hash']} [{n['kind']}, §{sec}, {n['words']}w, l.{n['lines'][0]}]"
                   + (f" (old thought, now stale: «{n['thought']}»)" if n["stale"] else ""))
        out.append("    " + n["text"][:900])
    return "\n".join(out)


def record(proj: Project, items: list[dict], model: str = "") -> str:
    o = build(proj)
    known = {n["hash"] for n in o["nodes"] if n["kind"] != "heading"}
    store = load_store(proj)
    done, bad, long_, ignored_split = 0, [], [], []
    limit = words_limit(proj)
    for it in items:
        h, t = str(it.get("hash", "")).strip(), " ".join(str(it.get("thought", "")).split())
        if h not in known:
            bad.append(h)
            continue
        if not t:
            raise RedlineError(f"{h}: empty thought")
        if len(t.split()) > limit + SLACK:
            long_.append(f"{h} ({len(t.split())} words)")
            continue
        split = it.get("split")
        if isinstance(split, str):
            split = [x.strip() for x in re.split(r"\s*(?:\||;)\s*", split) if x.strip()]
        split = [" ".join(str(x).split())[:60] for x in (split or []) if str(x).strip()]
        words_of = next((n["words"] for n in o["nodes"] if n["hash"] == h), 0)
        if split and (len(split) < 2 or words_of < split_words(proj)):
            ignored_split.append(h)   # short blocks (or a single idea) are never "split recommended"
            split = []
        store["thoughts"][h] = {"thought": t[:MAX_THOUGHT], "model": model, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                **({"split": split} if split else {})}
        done += 1
    save_store(proj, store)
    msg = f"recorded {done} thought(s)"
    if bad:
        msg += f"; unknown hashes ignored (the block changed meanwhile?): {', '.join(bad)}"
    if ignored_split:
        msg += (f"; split ignored for {len(ignored_split)} block(s) (under {split_words(proj)} words, or one idea)")
    if long_:
        msg += (f"; NOT recorded, too long (max {limit} words, shorter please): {', '.join(long_)}; "
                "send these again, shortened")
    return msg


def overview(proj: Project) -> str:
    """The sequence of thoughts, for a flow check (not the full text)."""
    o = build(proj)
    out = []
    for n in o["nodes"]:
        pad = "  " * n["depth"]
        if n["kind"] == "heading":
            out.append(f"{pad}§ {n['title']}")
        else:
            t = n["thought"] or "(no thought yet)"
            out.append(f"{pad}- {n['hash']} [{n['kind']}, {n['words']}w] {t}" + (" (stale)" if n["stale"] else "")
                       + (f" (split recommended: {' | '.join(n['split'])})" if n.get("split") else ""))
    return "\n".join(out)


def record_flow(proj: Project, items: list[dict]) -> str:
    o = build(proj)
    known = {n["hash"] for n in o["nodes"]}
    store = load_store(proj)
    keep = [f for f in store["flags"] if f.get("source") != "agent"]
    n = 0
    for it in items:
        hs = [h for h in ([it.get("hash")] if it.get("hash") else []) + list(it.get("hashes") or []) if h in known]
        kind = it.get("kind", "other")
        if not hs or not it.get("note"):
            continue
        keep.append({"kind": kind if kind in FLOW_KINDS else "other", "note": " ".join(str(it["note"]).split())[:300],
                     "hashes": list(dict.fromkeys(hs)), "source": "agent", "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        n += 1
    store["flags"] = keep
    save_store(proj, store)
    return f"recorded {n} flow flag(s)"


# ---- the user's plan: reordering as a draft ---------------------------------------------
def set_plan(proj: Project, plan) -> str:
    """plan: None, a list of hashes (the wanted order), or {"order": [...], "deleted": [...]}."""
    store = load_store(proj)
    if isinstance(plan, list):
        plan = {"order": plan, "deleted": []}
    store["plan"] = plan
    save_store(proj, store)
    return "plan cleared" if plan is None else \
        f"plan saved ({len(plan['order'])} nodes, {len(plan.get('deleted', []))} deleted)"


def _plan(store: dict, order) -> tuple[list[str] | None, list[str]]:
    plan = order if order is not None else store.get("plan")
    if isinstance(plan, list):
        return plan, []
    if isinstance(plan, dict):
        return plan.get("order"), list(plan.get("deleted", []))
    return None, []


def _lcs_keep(a: list[str], b: list[str]) -> set[str]:
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    keep = set()
    for blk in sm.get_matching_blocks():
        keep.update(a[blk.a:blk.a + blk.size])
    return keep


def plan_moves(proj: Project, order: list[str] | None = None) -> list[dict]:
    """The moves that turn the paper's order into the plan: nodes outside the longest common
    subsequence, each with the node it should follow."""
    o = build(proj)
    cur = [n["hash"] for n in o["nodes"]]
    by = {n["hash"]: n for n in o["nodes"]}
    order, deleted = _plan(load_store(proj), order)
    if not order:
        return []
    order = [h for h in order if h in by]
    cur_kept = [h for h in cur if h not in set(deleted)]
    if order == cur_kept:
        return []
    keep = _lcs_keep(cur_kept, order)
    moves = []
    for k, h in enumerate(order):
        if h in keep:
            continue
        prev = order[k - 1] if k else None
        moves.append({"hash": h, "kind": by[h]["kind"], "text": (by[h]["thought"] or by[h]["text"])[:160],
                      "lines": by[h]["lines"], "after": prev, "after_text": ((by[prev]["thought"] or by[prev]["text"])[:120]
                                                                             if prev else "(the start of the body)"),
                      "after_lines": by[prev]["lines"] if prev else None})
    return moves


def plan_deletions(proj: Project) -> list[dict]:
    o = build(proj)
    by = {n["hash"]: n for n in o["nodes"]}
    _, deleted = _plan(load_store(proj), None)
    return [{"hash": h, "kind": by[h]["kind"], "text": (by[h]["thought"] or by[h]["text"])[:160], "lines": by[h]["lines"]}
            for h in deleted if h in by and by[h]["kind"] != "heading"]


def plan_message(proj: Project) -> str:
    moves, dels = plan_moves(proj), plan_deletions(proj)
    if not moves and not dels:
        raise RedlineError("the plan changes nothing")
    out = ["[redline] Change the paper to follow my outline plan. Tracked edits only: propose_move for each move (a whole "
           "paragraph, figure, list), and propose_delete for each deletion, keeping everything reviewable; do not rewrite "
           "the text except where a move needs a one-line transition (add it as a tracked insert and tell me). Read the "
           "paper first (outline_plan() gives this plan again with full context). After the edits, compile() once and give "
           "me one line per change.", ""]
    for m in moves:
        out.append(f"- move [{m['kind']}] «{m['text']}» (hash {m['hash']}, now l.{m['lines'][0]}-{m['lines'][1]}) "
                   f"to after «{m['after_text']}» (hash {m['after']}"
                   + (f", l.{m['after_lines'][1]}" if m["after_lines"] else "") + ")")
    for d in dels:
        out.append(f"- DELETE [{d['kind']}] «{d['text']}» (hash {d['hash']}, l.{d['lines'][0]}-{d['lines'][1]}): remove it "
                   "from the paper as a tracked deletion; if other text refers to it, tell me instead of fixing silently")
    return "\n".join(out)


def edit_message(proj: Project, op: str, hashes: list[str], instruction: str = "") -> str:
    """Task text for merging several thoughts into one paragraph, or splitting one into several."""
    o = build(proj)
    by = {n["hash"]: n for n in o["nodes"]}
    chosen = [by[h] for h in hashes if h in by]
    if not chosen:
        raise RedlineError("those blocks no longer exist; refresh the overview")
    if op == "merge" and len(chosen) < 2:
        raise RedlineError("merging needs at least two blocks")
    if op == "split" and len(chosen) != 1:
        raise RedlineError("splitting works on one block")
    head = ("Merge these blocks into ONE paragraph that conveys a single clear thought" if op == "merge" else
            "Split this block into separate paragraphs, one clear thought each")
    out = [f"[redline] {head}. Tracked edits only (propose_replace / propose_insert / propose_delete: the original "
           "wording is reused wherever it still fits, nothing gets lost silently, citations keep their refcheck). "
           "Then compile() once and tell me, in one line each, the thought of every resulting paragraph."]
    if instruction.strip():
        out += ["", f"My instruction: {instruction.strip()}"]
    out.append("")
    for n in chosen:
        out.append(f"- [{n['kind']}] hash {n['hash']}, l.{n['lines'][0]}-{n['lines'][1]}, {n['words']}w"
                   + (f", thought: «{n['thought']}»" if n["thought"] else "")
                   + (f", suggested split into: {' | '.join(n['split'])}" if op == "split" and n.get("split") else ""))
        out.append("    " + n["text"][:1500])
    return "\n".join(out)


def flow_task() -> str:
    return ("Check whether the narrative of this paper flows. Call outline_overview() (the sequence of thoughts, "
            "not the full text; if some blocks have no thought yet, say so and stop) and judge the sequence as a reader "
            "would. Flag only real problems, each with the hash(es) involved: gap (a step the reader needs is missing), "
            "repeat (the same thought twice), forward-ref (something used before it is introduced), weak-transition "
            "(two adjacent thoughts with no link), orphan-figure (a figure/table whose point no text picks up). Call "
            "record_flow once with [{hash or hashes, kind, note}]; notes are one short sentence saying what to fix. "
            "Do not edit the paper.")


def dismiss(proj: Project, hashes: list[str]) -> str:
    """Dismiss the recommendations on these blocks (split, move, merge). They stay away until the
    block is run through the model again (its thought is rewritten)."""
    store = load_store(proj)
    n = 0
    for h in hashes:
        t = store["thoughts"].get(h)
        if t and t.get("split") and not t.get("split_dismissed"):
            t["split_dismissed"] = True
            n += 1
        for x in store["suggestions"]:
            if x["hashes"][0] == h and not x.get("dismissed"):
                x["dismissed"] = True
                n += 1
    save_store(proj, store)
    return f"dismissed {n} recommendation(s)"


def forget_blocks(proj: Project, hashes: list[str]) -> str:
    """Drop the thoughts of these blocks only, so the next refresh runs exactly them through the model."""
    store = load_store(proj)
    n = sum(1 for h in hashes if store["thoughts"].pop(h, None) is not None)
    store["suggestions"] = [x for x in store["suggestions"] if x["hashes"][0] not in hashes]
    save_store(proj, store)
    return f"{n} thought(s) dropped"


SUGGEST_OPS = ("move", "merge")


def record_suggestions(proj: Project, items: list[dict]) -> str:
    """Store the model's rearrangement / merge recommendations (replacing its earlier ones).

    move:  {op: "move", hash, after (a hash, or null for the start of the body), note}
    merge: {op: "merge", hashes: [h1, h2, ...] (in paper order), note}"""
    o = build(proj)
    known = {n["hash"]: n for n in o["nodes"]}
    store = load_store(proj)
    keep = [x for x in store["suggestions"] if x.get("source") != "agent"]
    n, bad = 0, []
    for it in items:
        op = it.get("op")
        note = " ".join(str(it.get("note", "")).split())[:200]
        if op == "move":
            h, after = str(it.get("hash", "")), it.get("after")
            if h not in known or (after and after not in known) or h == after or known[h]["kind"] == "heading":
                bad.append(h or "?")
                continue
            hs = [h]
            entry = {"op": "move", "hashes": hs, "after": after or None}
        elif op == "merge":
            hs = [str(x) for x in (it.get("hashes") or [])]
            if len(hs) < 2 or any(x not in known or known[x]["kind"] == "heading" for x in hs):
                bad.append(",".join(hs) or "?")
                continue
            order = [x["hash"] for x in o["nodes"]]
            hs.sort(key=order.index)
            entry = {"op": "merge", "hashes": hs}
        else:
            bad.append(str(op))
            continue
        keep.append({**entry, "note": note, "source": "agent", "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        n += 1
    store["suggestions"] = keep
    save_store(proj, store)
    return f"recorded {n} suggestion(s)" + (f"; invalid ignored: {', '.join(bad)}" if bad else "")


def review_task(proj: Project) -> str:
    return ("You are reviewing the structure of a paper from its thoughts. Call outline_overview(): the sequence of one-line "
            "thoughts with hashes, word counts, and any '(split recommended: ...)' notes. Recommend only changes that "
            "clearly improve the argument:\n"
            "- MOVE a block elsewhere (op 'move', hash, after = the hash it should follow, or null for the very start; "
            "note = why, 12 words or fewer),\n"
            "- MERGE blocks that say one thing in several places (op 'merge', hashes in paper order, note). Blocks marked "
            "'split recommended' are the opposite case: do not merge them, and consider that a block you want to move "
            "may be better split first,\n"
            "Be selective: a few strong recommendations, not a rewrite; leave sound order alone. Call record_suggestions "
            "once with [{op, ...}] (an empty list is a fine answer). Do not edit the paper.")


def forget(proj: Project) -> str:
    """Full reclassification: drop every thought, and with them the split recommendations, the model's
    move/merge suggestions, the flow flags and the dismissals (all of them describe the old thoughts).
    The draft plan is yours and stays."""
    store = load_store(proj)
    n = len(store["thoughts"])
    store["thoughts"], store["last"], store["suggestions"], store["flags"] = {}, [], [], []
    save_store(proj, store)
    return f"{n} thought(s) dropped"


def thoughts_task(words: int = DEFAULT_WORDS) -> str:
    return (f"Call outline_pending(), write one very short thought ({words} words or fewer) per block it lists, and "
            "call record_thoughts once with all of them. If record_thoughts says some were too long, shorten and "
            "send those again. Repeat until nothing is pending. Do not edit the paper.")


# ---- applying the plan without an agent ------------------------------------------------
def apply_direct(proj: Project, origin: str = "overview plan") -> str:
    """Execute the plan declaratively: every move and deletion becomes tracked markup (a
    \\deleted here, an \\added there, a footnote each), in ONE journaled write (one undo).
    Blocks keep their hash across a tracked move (hashes use the accepted text), so later
    moves find them again. What cannot be done mechanically (a block that holds pending
    changes, a destination inside markup) is skipped and reported."""
    from . import author, ops
    from .service import Service, _diff_edit
    moves, dels = plan_moves(proj), plan_deletions(proj)
    if not moves and not dels:
        raise RedlineError("the plan changes nothing")
    done: list[str] = []
    skipped: list[str] = []

    def fn(doc: Document):
        done.clear()      # _edit evaluates fn more than once (citation check, then the write)
        skipped.clear()
        text = doc.src
        for m in moves:
            d = proj.doc(text)
            bl = {b.hash: b for b in blocks(d)}
            src = bl.get(m["hash"])
            if src is None:
                skipped.append(f"«{m['text'][:40]}»: not found any more")
                continue
            try:
                if m["after"] is None:
                    first = next((b for b in bl.values() if b.start > src.end or b.start < src.start), None)
                    first = min((b for b in bl.values() if b is not src), key=lambda b: b.start, default=None)
                    if first is None:
                        continue
                    edits = author.move_span(d, src.start, src.end, first.start, after=False, comment="outline plan",
                                             trail="\n\n")
                else:
                    dst = bl.get(m["after"])
                    if dst is None:
                        skipped.append(f"«{m['text'][:40]}»: its destination block is gone")
                        continue
                    lead = "\n" if src.kind == "item" and dst.kind == "item" else "\n\n"
                    edits = author.move_span(d, src.start, src.end, dst.end, after=True, comment="outline plan", lead=lead)
                text = ops.apply(text, edits)
                done.append(f"moved «{m['text'][:50]}»")
            except RedlineError as e:
                skipped.append(f"«{m['text'][:40]}»: {str(e).splitlines()[0][:100]}")
        for dl in dels:
            d = proj.doc(text)
            b = next((x for x in blocks(d) if x.hash == dl["hash"]), None)
            if b is None:
                skipped.append(f"«{dl['text'][:40]}»: not found any more")
                continue
            try:
                text = ops.apply(text, author.delete_span(d, b.start, b.end, "outline plan"))
                done.append(f"deleted «{dl['text'][:50]}»")
            except RedlineError as e:
                skipped.append(f"«{dl['text'][:40]}»: {str(e).splitlines()[0][:100]}")
        return [_diff_edit(doc.src, text)] if text != doc.src else []

    Service(proj)._edit(fn, "outline plan (declarative)", origin)
    if done:
        set_plan(proj, None)  # applied: the paper is the plan now
    msg = f"{len(done)} change(s) written as tracked markup (one undo): " + "; ".join(done[:8]) + ("; ..." if len(done) > 8 else "")
    if skipped:
        msg += "\nSKIPPED (do these by hand or with the agent): " + "; ".join(skipped)
    return msg if done else "nothing could be applied mechanically: " + "; ".join(skipped)
