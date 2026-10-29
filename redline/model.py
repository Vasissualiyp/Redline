"""Plain data types shared by the parser, the ops and the front ends."""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field


class RedlineError(Exception):
    """A user-facing error (bad anchor, ambiguous selector, parse failure...)."""


@dataclass
class Span:
    start: int  # offset of first char
    end: int    # offset one past the last char

    def text(self, src: str) -> str:
        return src[self.start:self.end]


@dataclass
class Marker:
    """A `%Y` / `%N` / `%X` review marker left by the human."""
    kind: str          # "Y" | "N" | "X"
    note: str          # free text after the marker
    start: int         # offset of the '%'
    end: int           # end of the comment (exclusive, before newline)
    line: int          # 1-based
    item: "Change | None" = None


@dataclass
class Section:
    level: int         # 1 section, 2 subsection, 3 subsubsection
    title: str
    number: str        # "3.2", "A", "" for starred
    start: int         # offset of the heading command
    end: int = 0       # end of the section body (filled by the parser)
    line: int = 0
    parent: "Section | None" = None

    @property
    def label(self) -> str:
        return f"{self.number} {self.title}".strip()


@dataclass
class Change:
    kind: str                  # added | deleted | replaced | comment | highlight
    macro: str                 # macro name as written, e.g. "added", "skadd"
    start: int                 # offset of the backslash
    end: int                   # one past the final closing brace
    args: list[Span]           # inner spans: added [new], deleted [old], replaced [new, old], comment [text]
    opt: Span | None = None    # inner span of the optional [..] argument
    comment: "Change | None" = None      # attached \chcomment following the change
    attached_to: "Change | None" = None  # set on an attached comment
    nested_in: "Change | None" = None
    section: Section | None = None
    marker: Marker | None = None
    ordinal: int = 0
    id: str = ""
    line_start: int = 0
    line_end: int = 0
    legacy: bool = False

    # -- text accessors --------------------------------------------------
    def new(self, src: str) -> str | None:
        if self.kind in ("added", "replaced", "highlight"):
            return self.args[0].text(src)
        return None

    def old(self, src: str) -> str | None:
        if self.kind == "deleted":
            return self.args[0].text(src)
        if self.kind == "replaced":
            return self.args[1].text(src)
        return None

    def body(self, src: str) -> str | None:
        """Comment text for comments."""
        if self.kind == "comment":
            return self.args[0].text(src)
        return None

    @property
    def full_end(self) -> int:
        return self.comment.end if self.comment else self.end

    @property
    def label(self) -> str:
        return f"C{self.ordinal}"


@dataclass
class TextEdit:
    start: int
    end: int
    text: str


@dataclass
class Document:
    src: str
    items: list[Change] = field(default_factory=list)      # top-level changes + standalone comments
    nested: list[Change] = field(default_factory=list)     # markup found inside another change
    sections: list[Section] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    body_start: int = 0
    body_end: int = 0
    line_starts: list[int] = field(default_factory=list)
    floats: list[Span] = field(default_factory=list)       # figure/table environments
    problems: list[str] = field(default_factory=list)      # parse-level problems

    def line_of(self, offset: int) -> int:
        """1-based line number of an offset."""
        return bisect.bisect_right(self.line_starts, offset)

    def line_col(self, offset: int) -> tuple[int, int]:
        """0-based (line, byte column) as nvim expects."""
        ln = bisect.bisect_right(self.line_starts, offset) - 1
        col = len(self.src[self.line_starts[ln]:offset].encode("utf-8"))
        return ln, col

    def section_at(self, offset: int) -> Section | None:
        best = None
        for s in self.sections:
            if s.start <= offset < s.end:
                if best is None or s.level > best.level:
                    best = s
        return best

    def find(self, key: str) -> Change:
        """Look an item up by ordinal (C12) or hash id (prefix)."""
        key = key.strip()
        if key[:1] in "Cc" and key[1:].isdigit():
            n = int(key[1:])
            for it in self.items:
                if it.ordinal == n:
                    return it
            raise RedlineError(f"no change {key} (there are {len(self.items)})")
        hits = [it for it in self.items if it.id == key]
        if not hits:
            hits = [it for it in self.items if len(key) >= 4 and it.id.startswith(key)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise RedlineError(f"no change with id {key!r}; it may have been resolved or revised")
        raise RedlineError(f"id {key!r} is ambiguous: {', '.join(h.id for h in hits)}")
