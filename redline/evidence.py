"""The evidence library behind `% VERIFIED bibkey#n` lines.

A result line in the paper only names an id. The entry with that id holds the
claim, the verdict and, per source, the bib key, page, section and the exact
sentence(s) that support or contradict the claim, plus the Zotero highlight
made on them. Zotero is the shared copy (zotero.py writes one note per entry
and one highlight per quote); `.redline/evidence.json` is the local cache, and
can be rebuilt from Zotero on another machine.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .model import RedlineError
from .verify import ID_RE

ROLES = ("supports", "contradicts", "context")
COLORS = {"supports": "green", "contradicts": "red", "context": "blue"}  # zotero-refcheck's scheme


@dataclass
class Source:
    bibkey: str = ""
    role: str = "supports"
    page: int | None = None          # 1-based page of the quote in the source PDF
    quote: str = ""                  # exact sentence(s) copied from the PDF
    section: str = ""                # where in the source: "§3.1", "Abstract", "Table 1"
    item_key: str = ""               # Zotero parent item
    att_key: str = ""                # Zotero PDF attachment
    ann_key: str = ""                # Zotero highlight
    page_index: int | None = None    # 0-based page the highlight sits on
    rects: list = field(default_factory=list)  # Zotero rects: [x1, y1, x2, y2], origin bottom-left
    highlight: str = ""              # "", "ok", "pending", or "none: <why>"
    code: dict | None = None         # code checks: {path, line, value}

    @classmethod
    def of(cls, d: dict) -> "Source":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Entry:
    id: str
    status: str
    claim: str = ""
    section: str = ""                # section of the paper the claim is in
    reason: str = ""                 # UNVERIFIED: why; otherwise an optional remark
    sources: list[Source] = field(default_factory=list)
    created: str = ""
    note_key: str = ""
    zotero: str = "pending"          # "synced", "pending", "failed: ..."
    legacy: str = ""                 # the inline result this entry was migrated from

    @classmethod
    def of(cls, d: dict) -> "Entry":
        d = dict(d)
        d["sources"] = [Source.of(s) for s in d.get("sources", [])]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def also(self) -> list[str]:
        keys = []
        for s in self.sources[1:]:
            if s.bibkey and s.bibkey != self.sources[0].bibkey and s.bibkey not in keys:
                keys.append(s.bibkey)
        return keys

    def text(self) -> str:
        """Human-readable form (the note pane, MCP `evidence`)."""
        out = [f"{self.id}  {self.status}", ""]
        if self.claim:
            out += ["Claim:", f"  {self.claim}", ""]
        if self.section:
            out.append(f"In the paper: §{self.section}")
        if self.reason:
            out += [f"Remark: {self.reason}"]
        for i, s in enumerate(self.sources, 1):
            out.append("")
            if s.code:
                out.append(f"[{i}] code {s.code.get('path', '')}:{s.code.get('line', '')}")
                if s.code.get("value"):
                    out.append(f"    {s.code['value']}")
                continue
            where = ", ".join(x for x in (s.section, f"p.{s.page}" if s.page else "") if x)
            out.append(f"[{i}] {s.bibkey} ({s.role})" + (f" — {where}" if where else ""))
            if s.quote:
                out.append(f"    “{s.quote}”")
            if s.highlight and s.highlight != "ok":
                out.append(f"    highlight: {s.highlight}")
        out += ["", f"Zotero: {self.zotero}"]
        return "\n".join(out)


def now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


class Library:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, Entry] = {}
        if path.exists():
            data = json.loads(path.read_text() or "{}")
            self.entries = {k: Entry.of(v) for k, v in data.get("entries", {}).items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": {k: e.as_dict() for k, e in sorted(self.entries.items())}},
                                  indent=1, ensure_ascii=False))
        tmp.replace(self.path)

    def get(self, eid: str) -> Entry:
        if eid in self.entries:
            return self.entries[eid]
        hits = [e for k, e in self.entries.items() if k.lower() == eid.lower()]
        if not hits:
            raise RedlineError(f"no evidence {eid!r} in the library (try `redline evidence pull`)")
        return hits[0]

    def put(self, e: Entry) -> None:
        self.entries[e.id] = e

    def next_id(self, prefix: str, used: set[str] = frozenset()) -> str:
        """`prefix#n` with n above every number taken for that prefix."""
        prefix = re.sub(r"[^A-Za-z0-9_:.\-]", "", prefix) or "nosrc"
        taken = [int(k.rsplit("#", 1)[1]) for k in set(self.entries) | set(used)
                 if k.rsplit("#", 1)[0] == prefix]
        return f"{prefix}#{max(taken, default=0) + 1}"


def id_prefix(sources: list[Source]) -> str:
    for s in sources:
        if s.code:
            return "code"
        if s.bibkey:
            return s.bibkey
    return "nosrc"


# ---- Zotero note form ------------------------------------------------------------
_DATA = re.compile(r"<pre data-redline=\"1\">(.*?)</pre>", re.S)


def note_html(e: Entry, paper: str) -> str:
    """The Zotero note for an entry: readable, plus the entry as JSON for `pull`."""
    esc = html.escape
    parts = [f"<h1>{esc(e.id)} — {esc(e.status)}</h1>",
             f"<p><b>Paper:</b> {esc(paper)}" + (f", §{esc(e.section)}" if e.section else "") + "</p>"]
    if e.claim:
        parts.append(f"<blockquote>{esc(e.claim)}</blockquote>")
    if e.reason:
        parts.append(f"<p><b>Remark:</b> {esc(e.reason)}</p>")
    for s in e.sources:
        if s.code:
            parts.append(f"<p><b>code</b> {esc(str(s.code.get('path', '')))}:{esc(str(s.code.get('line', '')))}"
                         f" — {esc(str(s.code.get('value', '')))}</p>")
            continue
        where = ", ".join(x for x in (s.section, f"p.{s.page}" if s.page else "") if x)
        link = (f' <a href="zotero://open-pdf/library/items/{s.att_key}?annotation={s.ann_key}">open highlight</a>'
                if s.att_key and s.ann_key else "")
        parts.append(f"<p><b>{esc(s.bibkey)}</b> ({esc(s.role)}){' — ' + esc(where) if where else ''}{link}</p>")
        if s.quote:
            parts.append(f"<blockquote>{esc(s.quote)}</blockquote>")
    data = {k: v for k, v in e.as_dict().items() if k not in ("note_key", "zotero")}
    parts.append(f'<pre data-redline="1">{esc(json.dumps(data, ensure_ascii=False))}</pre>')
    return "\n".join(parts)


def entry_from_note(note: str) -> Entry | None:
    m = _DATA.search(note)
    if not m:
        return None
    try:
        return Entry.of(json.loads(html.unescape(m.group(1))))
    except (ValueError, TypeError):
        return None


# ---- migrating inline results ----------------------------------------------------
_QUOTE = re.compile(r"\"([^\"]+)\"|“([^”]+)”")
_KEYREF = re.compile(r"(?<![\w.])([A-Za-z][\w\-]*\d{2}[a-z]?)(?=[\s:,;)/]|$)")
_PAGE = re.compile(r"\bpp?\.\s*(\d+)")


def parse_legacy(status: str, text: str) -> tuple[list[Source], str]:
    """Sources and remark from an old inline result such as
    `ho25b: "quote" — Abstract, p.1` or `a/b: summary — a §2 "q1"; b p.3 "q2"`."""
    head, sep, body = text.partition(":")
    head_keys = []
    if sep and not _QUOTE.search(head):
        first = re.split(r"[\s(\[]", head.strip(), maxsplit=1)[0]
        head_keys = [k for k in re.split(r"[/,;+]", first) if re.fullmatch(r"[\w:.\-]+", k)]
    if not head_keys:
        body = text
    quotes = list(_QUOTE.finditer(body))
    if status == "UNVERIFIED" or (not quotes and not head_keys):
        return [], text.strip()
    if not quotes:
        return [Source(bibkey=k, section=body.strip(" —-;.")) for k in head_keys], ""
    known = set(head_keys)
    sources = []
    for i, q in enumerate(quotes):
        before = body[(quotes[i - 1].end() if i else 0):q.start()]
        after = body[q.end():(quotes[i + 1].start() if i + 1 < len(quotes) else len(body))]
        key = None
        for m in _KEYREF.finditer(before):
            if m.group(1) in known or before[m.end():m.end() + 1] == ":":
                key = m.group(1)
        if key is None:
            key = sources[-1].bibkey if sources else (head_keys[0] if head_keys else "")
        known.add(key)
        # where: what follows the quote up to the next key mention or quote
        where = re.split(r"(?<![\w.])(?:" + "|".join(map(re.escape, sorted(known, key=len, reverse=True)))
                         + r")\b", after)[0] if known else after
        where = where.strip(" —-;.,\t")
        page = _PAGE.search(where)
        pg = int(page.group(1)) if page else (1 if "abstract" in where.lower() else None)
        quote = (q.group(1) or q.group(2)).strip()
        sources.append(Source(bibkey=key, page=pg, quote=quote, section=_section(where)))
    # `"q1"; "q2" — Abstract, p.1`: quotes listed together share the location after them
    for i in range(len(sources) - 2, -1, -1):
        s, nxt = sources[i], sources[i + 1]
        if s.page is None and not s.section and nxt.bibkey == s.bibkey:
            s.page, s.section = nxt.page, nxt.section
    return sources, ""


def _section(where: str) -> str:
    """'§3.1, p.5 (15 haloes)' -> '§3.1'; 'Abstract, p.1' -> 'Abstract'."""
    first = re.split(r"[;,]|\bpp?\.\s*\d", where)[0].strip(" —-;.,")
    return first[:80]


def match_text(quote: str) -> str:
    """The part of a quote to look for in the PDF: its longest run without an ellipsis."""
    parts = [p.strip(" ,;") for p in re.split(r"\.\.\.|…|\[[^\]]*\]", quote)]
    parts = [p for p in parts if len(p.split()) >= 3] or [quote]
    return max(parts, key=len)


def is_id(s: str) -> bool:
    return bool(re.fullmatch(ID_RE, s))
