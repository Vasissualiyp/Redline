"""Find where a LaTeX excerpt is typeset on a PDF page.

synctex only knows lines: it returns column-wide boxes for every line the source
line touches. To highlight just the excerpt, the excerpt is reduced to plain
words and aligned with the page's words (`pdftotext -bbox`) near those boxes;
the matched run of words becomes one highlight rectangle per text line.
"""
from __future__ import annotations

import difflib
import html
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Rect:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float  # PDF points, origin top-left


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


_WORD = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>')


def page_words(pdf: Path, page: int) -> list[Word]:
    try:
        out = subprocess.run(["pdftotext", "-bbox", "-f", str(page), "-l", str(page), str(pdf), "-"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [Word(float(a), float(b), float(c), float(d), html.unescape(t))
            for a, b, c, d, t in _WORD.findall(out)]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def tex_tokens(tex: str, limit: int = 80) -> list[str]:
    """Plain words of a LaTeX snippet (citations, math and commands dropped)."""
    s = re.sub(r"(?<!\\)%[^\n]*", " ", tex)
    s = re.sub(r"\\(?:cite[a-zA-Z]*|[aA]uto[rR]ef|[cC]ref|ref|eqref|label|pageref)\*?"
               r"(?:\[[^\]]*\])*\{[^{}]*\}", " ", s)
    s = re.sub(r"\$[^$]*\$", " ", s)
    s = re.sub(r"\\[A-Za-z@]+\*?", " ", s)
    s = s.replace("~", " ").replace("--", " ")
    toks = [norm(t) for t in re.split(r"[\s{}\[\]()]+", s)]
    return [t for t in toks if t][:limit]


def _tokens(words: list[Word]) -> tuple[list[str], list[list[int]]]:
    """Page tokens with the word indexes behind each (hyphenated words are joined)."""
    toks, owners = [], []
    i = 0
    while i < len(words):
        t, own = words[i].text, [i]
        while t.endswith("-") and i + 1 < len(words) and words[i + 1].y0 > words[i].y0 + 2:
            i += 1
            t = t[:-1] + words[i].text
            own.append(i)
        n = norm(t)
        if n:
            toks.append(n)
            owners.append(own)
        i += 1
    return toks, owners


def _best_run(ex: list[str], toks: list[str]) -> tuple[int, int, int] | None:
    """(first token, last token, matched count) of the densest aligned run."""
    sm = difflib.SequenceMatcher(None, ex, toks, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    if not blocks:
        return None
    # long blocks anchor the match; single common words only count inside a run
    strong = [b for b in blocks if b.size >= 2 or len(ex) <= 2]
    if not strong:
        return None
    best, cur = None, None
    for b in strong:
        if cur and b.b - cur[1] <= 8:
            cur = (cur[0], b.b + b.size - 1, cur[2] + b.size)
        else:
            cur = (b.b, b.b + b.size - 1, b.size)
        if best is None or cur[2] > best[2]:
            best = cur
    return best


def line_rects(page: int, words: list[Word]) -> list[Rect]:
    rects: list[Rect] = []
    for w in words:
        r = rects[-1] if rects else None
        if r and abs(w.y0 - r.y0) < 2.5 and w.x0 >= r.x0 - 1:
            r.x1, r.y0, r.y1 = max(r.x1, w.x1), min(r.y0, w.y0), max(r.y1, w.y1)
        else:
            rects.append(Rect(page, w.x0, w.y0, w.x1, w.y1))
    return rects


def locate(words: list[Word], page: int, excerpt: str,
           near: list[Rect] | None = None) -> list[Rect]:
    """Highlight rectangles for `excerpt` on the page, or [] if it is not found."""
    ex = tex_tokens(excerpt)
    if not ex or not words:
        return []
    pools = []
    if near:
        # synctex attributes a typeset line to the source line it starts in, so
        # the excerpt can begin a few lines above the boxes it returns
        y0 = min(r.y0 for r in near) - 70
        y1 = max(r.y1 for r in near) + 70
        xs = [(r.x0 - 5, r.x1 + 5) for r in near]
        pools.append([i for i, w in enumerate(words)
                      if y0 <= w.y0 <= y1 and any(a <= w.x0 <= b for a, b in xs)])
    pools.append(list(range(len(words))))  # footnotes, captions far from the line
    need = max(1, min(3, len(ex)), int(0.35 * min(len(ex), 30)))
    best = None  # (matched, rank, idx); the near pool wins ties
    for rank, pool in enumerate(pools):
        toks, owners = _tokens([words[i] for i in pool])
        run = _best_run(ex, toks)
        if run and run[2] >= need and (best is None or run[2] > best[0]):
            idx = sorted({pool[j] for t in range(run[0], run[1] + 1) for j in owners[t]})
            best = (run[2], rank, idx)
    return line_rects(page, [words[i] for i in best[2]]) if best else []
