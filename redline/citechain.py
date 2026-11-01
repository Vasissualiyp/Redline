"""Citation chain: which paper does a source mean by "Faucher-Giguère (2020)"?

A quote from a source paper often names another paper by author and year
("we update the assumed UVB to the more recent Faucher-Giguere (2020) model").
That only supports a claim citing `Faucher-Giguere_2020` if the source's own
reference list entry for it is the paper our .bib key points at (a wrong key once
pointed at a cosmic-ray paper). This module finds the mention in a quote, the
matching entries in the source PDF's reference list, and compares each entry
with the .bib entry by DOI, arXiv id, journal volume + first page, or title.
"""
from __future__ import annotations

import re
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

LETTER = r"[^\W\d_]"
SURNAME = rf"(?<![\w-])(?:(?:de|van|von|der|den|la|le|du|di|da|del|dos|ter)\s)?[A-ZÀ-Ý]{LETTER}*(?:[-'’]{LETTER}+)*"
YEAR = r"\b(1[5-9]\d\d|20\d\d)([a-z])?\b"
_START = re.compile(rf"^({SURNAME}),?\s+[A-Z][\.\-]")
_NUMBERED = re.compile(r"^\s*(?:\[\d+\]|\d+\.)\s+")
_HEADING = re.compile(r"^\s*(?:references|bibliography|literature cited|references cited)\s*$", re.I)
_DOI = re.compile(r"10\.\d{4,9}/[^\s,;]+")
_ARXIV = re.compile(r"(?:arXiv:\s*|abs/)(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})", re.I)
STOP = {"in", "the", "and", "of", "see", "from", "by", "since", "with", "for", "at", "as", "following",
        "using", "adopt", "adopts", "update", "updated", "version", "model", "models", "table", "figure"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    return re.sub(r"[^a-z0-9]", "", s)


def plain(tex: str) -> str:
    s = re.sub(r"\\[`'^\"~=.uvHtcdbk]\{?([A-Za-z])\}?", r"\1", tex or "")  # \`e, \'{e}, ...
    s = re.sub(r"\\[A-Za-z]+\*?", " ", s)
    return " ".join(re.sub(r"[{}$^_\\~]", "", s).split())


# ---- mentions in a quote -----------------------------------------------------------
@dataclass
class Mention:
    author: str       # first author's surname as written
    year: str
    suffix: str = ""
    text: str = ""    # as in the quote: "Faucher-Giguère (2020)"


def mentions(quote: str) -> list[Mention]:
    """Author-year mentions: `Smith (2020)`, `Smith et al. 2020a`, `(Smith & Jones, 2020)`."""
    out = []
    for m in re.finditer(YEAR, quote):
        before = quote[max(0, m.start() - 70):m.start()]
        # ... Surname [et al.| and X| & X] [,(] year
        pm = re.search(rf"({SURNAME})(?:\s+et\s+al\.?|\s+(?:and|&)\s+[A-ZÀ-Ý]{LETTER}+(?:[-'’]{LETTER}+)*)?"
                       r"[\s,;(]*\(?$", before)
        if not pm or pm.group(1).lower() in STOP:
            continue
        close = ")" if quote[m.end():m.end() + 1] == ")" else ""
        out.append(Mention(pm.group(1).strip(), m.group(1), m.group(2) or "",
                           (before[pm.start():] + m.group(0)).strip() + close))
    seen, uniq = set(), []
    for x in out:
        if (norm(x.author), x.year, x.suffix) not in seen:
            seen.add((norm(x.author), x.year, x.suffix))
            uniq.append(x)
    return uniq


# ---- the source's reference list ---------------------------------------------------
@dataclass
class RefEntry:
    text: str
    page: int
    author: str = ""
    year: str = ""
    suffix: str = ""
    doi: str = ""
    arxiv: str = ""

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("text", "page", "author", "year", "suffix", "doi", "arxiv")}


def _entry(text: str, page: int) -> RefEntry:
    t = " ".join(text.split())
    a = _START.match(t)
    y = re.search(YEAR, t)
    doi = _DOI.search(t)
    ax = _ARXIV.search(t)
    return RefEntry(t, page, a.group(1) if a else "", y.group(1) if y else "", (y.group(2) or "") if y else "",
                    doi.group(0).rstrip(".)") .lower() if doi else "", ax.group(1).lower() if ax else "")


def parse_entries(pages: list[tuple[int, str]]) -> list[RefEntry]:
    """Entries of a reference list given as (page, text) from the heading's page on.

    A new entry starts at a line that looks like `Surname I.,` (or `[12]`) once the
    entry before it has its year: author lists wrap onto lines that look the same."""
    out: list[RefEntry] = []
    cur, cur_page = "", 0
    for page, text in pages:
        for line in text.splitlines():
            ln = line.strip()
            if not ln or _HEADING.match(ln) or re.fullmatch(r"\d{1,3}", ln):
                continue
            starts = bool(_START.match(ln) or _NUMBERED.match(ln))
            if cur and starts and re.search(YEAR, cur):
                out.append(_entry(cur, cur_page))
                cur = ""
            if not cur:
                cur_page = page
            cur = (cur + " " + ln).strip()
    if cur:
        out.append(_entry(cur, cur_page))
    return [e for e in out if e.year]


_cache: dict = {}


def reference_entries(pdf: Path) -> list[RefEntry]:
    """Entries of the PDF's reference list ([] when no References heading is found)."""
    key = (str(pdf), pdf.stat().st_mtime if pdf.exists() else 0)
    if key in _cache:
        return _cache[key]
    try:
        n = int(re.search(r"^Pages:\s+(\d+)", subprocess.run(["pdfinfo", str(pdf)], capture_output=True,
                                                               text=True, timeout=20).stdout, re.M).group(1))
    except (OSError, AttributeError, subprocess.TimeoutExpired):
        return []
    pages = []
    for p in range(1, n + 1):
        try:
            txt = subprocess.run(["pdftotext", "-f", str(p), "-l", str(p), str(pdf), "-"],
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            txt = ""
        pages.append((p, txt))
    head = [p for p, t in pages if any(_HEADING.match(l) for l in t.splitlines())]
    if not head:
        _cache[key] = []
        return []
    first = head[-1]
    body = []
    for p, t in pages[first - 1:]:
        if p == first:  # only what follows the heading
            lines = t.splitlines()
            idx = max(i for i, l in enumerate(lines) if _HEADING.match(l))
            t = "\n".join(lines[idx + 1:])
        body.append((p, t))
    _cache[key] = parse_entries(body)
    return _cache[key]


def candidates(entries: list[RefEntry], m: Mention) -> list[RefEntry]:
    """Reference entries whose first author and year are the mention's."""
    want = norm(m.author)
    return [e for e in entries if e.year == m.year and (not m.suffix or e.suffix == m.suffix)
            and want and (norm(e.author) == want or norm(e.author).endswith(want))]


# ---- comparing with the .bib entry -------------------------------------------------
@dataclass
class Meta:
    key: str = ""
    title: str = ""
    doi: str = ""
    eprint: str = ""
    volume: str = ""
    page: str = ""
    year: str = ""
    author: str = ""   # first author's surname
    extra: dict = field(default_factory=dict)


def meta_of(key: str, fields: dict[str, str]) -> Meta:
    authors = plain(fields.get("author", ""))
    first = re.split(r"\s+and\s+", authors)[0] if authors else ""
    surname = first.split(",")[0].strip() if "," in first else (first.split() or [""])[-1]
    pages = fields.get("pages", "") or fields.get("eid", "")
    page = re.split(r"[-–—]+", pages)[0].strip() if pages else ""
    return Meta(key, plain(fields.get("title", "")), fields.get("doi", "").lower().strip(),
                re.sub(r"^arxiv:\s*", "", fields.get("eprint", ""), flags=re.I).lower().strip(),
                fields.get("volume", "").strip(), page, fields.get("year", "").strip()[:4], surname)


def names(meta: Meta, m: Mention) -> bool:
    """Does the mention (author, year) describe this .bib entry?"""
    return bool(meta.author) and norm(meta.author) == norm(m.author) and meta.year == m.year


def compare(e: RefEntry, meta: Meta) -> tuple[str, str]:
    """(verdict, why): match | mismatch | unclear."""
    text = e.text
    nt = norm(text)
    if meta.doi and e.doi:
        return ("match", f"same DOI {e.doi}") if meta.doi == e.doi else \
            ("mismatch", f"DOI {e.doi} in the reference list, {meta.doi} in the .bib")
    if meta.eprint and e.arxiv and meta.eprint.split("v")[0] == e.arxiv.split("v")[0]:
        return "match", f"same arXiv id {e.arxiv}"
    if meta.title:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", meta.title) if len(w) > 3]
        if words:
            hit = sum(1 for w in words if norm(w) in nt) / len(words)
            if hit >= 0.8:
                return "match", "the .bib title appears in the entry"
            if hit < 0.3 and re.search(r"\b[A-Za-z]{4,}\b[^,]*\b[a-z]{4,}\b[^,]*,\s*(?:19|20)\d\d\b", text) is None \
                    and len(text) > 120 and not (meta.volume and meta.page):
                return "mismatch", "the .bib title is not in the entry"
    if meta.volume and meta.page:
        vol = re.search(rf"[,\s]{re.escape(meta.volume)}[,\s]+{re.escape(meta.page)}\b", text)
        if vol:
            return "match", f"same volume and page ({meta.volume}, {meta.page})"
        found = re.search(r",\s*(\d{1,4}),\s*([A-Za-z]?\d{1,6}[+]?)\s*(?:\.|,|$|\s)", text)
        if found:
            return "mismatch", (f"the reference list says vol. {found.group(1)}, p. {found.group(2)}; "
                                f"the .bib says vol. {meta.volume}, p. {meta.page}")
    if meta.year and e.year and meta.year != e.year:
        return "mismatch", f"year {e.year} vs {meta.year}"
    return "unclear", "the entry has no DOI, title or volume/page to compare with the .bib"
