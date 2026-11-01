"""MCP server: safe, tracked edits to the paper for any agent."""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from .model import RedlineError
from .service import Service
from .state import Project

INSTRUCTIONS = """\
Redline edits a LaTeX paper with the `changes` package so the human can accept or
reject every suggestion from their editor. Rules:
- Never write \\added/\\deleted/\\replaced/\\chcomment yourself and never edit the main
  .tex with generic file tools: use propose_replace / propose_insert / propose_delete /
  add_comment. Anchors are exact text copied from read() (without the line-number
  gutter); whitespace differences are tolerated. Keep anchors short but unique.
- One proposal per idea; add a short `comment` when the reason is not obvious
  (it becomes a \\chcomment footnote). Prefix comments that answer a coauthor note
  with "Resolved:" / keep "TODO" in ones that need human input.
- Check before you propose: you can read the whole paper (read(section=...) for a full
  section) and the analysis code. Never leave a TODO for a check you can run yourself
  ("check the wording matches §X", "verify this number"): read it, make the text
  consistent, then propose. TODO is only for the author's decisions, or for checks you
  cannot run (say which and why).
- Review markers: the human writes %Y (accept), %N [note] (keep pending; the note is
  feedback for you) and %X (reject) next to changes. Read review_notes() first when
  asked to address feedback; revise_change() a %N suggestion instead of stacking a
  new one on top. revise_change clears the %N (the human re-reviews the change); if
  you disagree with a note, answer it with revise_change(id, comment="Kept: <why>").
- REFCHECK IS MANDATORY. Every new or changed citation is verified against the cited
  paper's PDF before it is written: propose_replace/insert/revise_change refuse text that
  cites a key without evidence=[{bibkey, page, quote, section}] (exact quotes from the
  PDF; the .bib key must also be the paper Zotero holds). Whenever you check anything in
  the paper (a claim, a number, a reference, "make sure the facts are solid") you record
  the outcome with record_verification, so a `% VERIFIED/REFUTED/UNVERIFIED id` line is
  left; a check that leaves no such line did not happen. A paper named by author and year
  in a quote is also checked against the source's reference list (for_key), and any paper
  you cite that Zotero lacks is pulled with its PDF automatically. Only when the human explicitly
  says not to check, pass unchecked_reason (the result is marked % UNVERIFIED). lint()
  flags new citations that have no result line.
- Pending changes are addressed by id (stable hash like a3f9c2) or label (C12;
  labels renumber after every edit, ids do not).
- Run compile() after a batch of edits and fix what it reports. lint(fix=true) fixes
  markup problems mechanically.
- Restructuring stays tracked: new list items/headings go through propose_insert
  (\item and \section stay outside the markup automatically), relocated text through
  propose_move. plain_edit is only for equations/environments and tiny typo fixes;
  it refuses prose changes.
- `% VERIFY <refcheck|code|any>: «claim»` lines are verification requests from the
  human: verification_requests() lists them; answer each with record_verification()
  (structured sources: exact quote + page per bibkey). Results in the paper are short
  ids (% VERIFIED ho25b#1); evidence(id) shows what is behind one. Never write or edit
  % VERIFIED/REFUTED/UNVERIFIED lines by hand.
"""

mcp = MCPServer(name="redline", instructions=INSTRUCTIONS)
_service: Service | None = None


def svc() -> Service:
    global _service
    if _service is None:
        _service = Service(Project.open())
    return _service


def _origin(ctx: Context | None, tag: str | None) -> str:
    if tag:
        return tag
    try:
        info = ctx.session.client_params.client_info  # type: ignore[union-attr]
        return info.name or "agent"
    except Exception:
        return "agent"


def _safe(fn, *a, **kw) -> str:
    try:
        out = fn(*a, **kw)
        return out if isinstance(out, str) else str(out)
    except RedlineError as e:
        return f"ERROR: {e}"


# ---- reading ------------------------------------------------------------------
@mcp.tool()
def outline() -> str:
    """Section tree with line ranges and counts of pending changes/comments/markers."""
    return _safe(svc().outline)


@mcp.tool()
def read(section: str | None = None, first_line: int | None = None, last_line: int | None = None) -> str:
    """Read the paper with line numbers: a section (name or number, fuzzy) or a line range."""
    lines = [first_line, last_line] if first_line else None
    return _safe(svc().read, section, lines)


@mcp.tool()
def list_changes(selector: dict[str, Any] | None = None, full: bool = False) -> str:
    """List pending changes. selector keys (ANDed): ids, section, until_section,
    from_section, lines [a,b], kinds [added|deleted|replaced|comment|highlight],
    marker (Y|N|X|none|any), origin, contains (text search), except_ids, all.
    Default: all. full=true shows untruncated text."""
    return _safe(svc().list_changes, selector, full)


@mcp.tool()
def review_notes() -> str:
    """Every change the human marked with %Y/%N/%X, including their notes."""
    return _safe(svc().review_notes)


@mcp.tool()
def status() -> str:
    """Counts of pending items, markers and lint errors."""
    return _safe(lambda: str(svc().status()))


# ---- proposing -------------------------------------------------------------------
@mcp.tool()
def propose_replace(old_text: str, new_text: str, comment: str | None = None,
                    section: str | None = None, occurrence: int | None = None,
                    tag: str | None = None, evidence: list[dict[str, Any]] | None = None,
                    unchecked_reason: str | None = None, ctx: Context | None = None) -> str:
    """Suggest replacing old_text (exact, unique) with new_text as \\replaced{new}{old}.
    Paragraph breaks and citations are handled automatically. tag overrides the
    recorded origin (e.g. "refcheck").

    Citations: any \\cite in the new text is refchecked in the same call. Pass
    evidence=[{bibkey, page, quote, section}] for every new key (exact sentence(s) from
    that paper's PDF that support the text); the quotes are matched on the page, the .bib
    key is compared with the Zotero item, and Redline adds the `% VERIFIED key#n` line
    itself. Without evidence the call is refused and nothing is written. A cited paper that
    is not in Zotero is pulled in automatically with its PDF (never report it as missing).
    When the quote is from another paper that only NAMES the cited one ("... the more recent
    Faucher-Giguere (2020) model"), give that source for_key="<the cited bibkey>": Redline
    finds the named paper in the source's own reference list, highlights that entry too, and
    refuses unless it is the same paper as the .bib entry (DOI / arXiv id / volume and page /
    title), so a key pointing at the wrong paper cannot pass. unchecked_reason
    is only for when the human explicitly said not to check (the result is then marked
    % UNVERIFIED)."""
    return _safe(svc().propose_replace, old_text, new_text, comment, section, occurrence,
                 _origin(ctx, tag), evidence, unchecked_reason)


@mcp.tool()
def propose_insert(text: str, after: str | None = None, before: str | None = None,
                   comment: str | None = None, section: str | None = None,
                   occurrence: int | None = None, tag: str | None = None,
                   evidence: list[dict[str, Any]] | None = None, unchecked_reason: str | None = None,
                   ctx: Context | None = None) -> str:
    """Suggest inserting text right after (or before) an exact anchor, as \\added{text}.
    Include the leading space/punctuation you need.

    Citations: any \\cite in the new text is refchecked in the same call. Pass
    evidence=[{bibkey, page, quote, section}] for every new key (exact sentence(s) from
    that paper's PDF that support the text); the quotes are matched on the page, the .bib
    key is compared with the Zotero item, and Redline adds the `% VERIFIED key#n` line
    itself. Without evidence the call is refused and nothing is written. unchecked_reason
    is only for when the human explicitly said not to check (the result is then marked
    % UNVERIFIED)."""
    return _safe(svc().propose_insert, text, after, before, comment, section, occurrence,
                 _origin(ctx, tag), evidence, unchecked_reason)


@mcp.tool()
def propose_delete(old_text: str, comment: str | None = None, section: str | None = None,
                   occurrence: int | None = None, tag: str | None = None, ctx: Context | None = None) -> str:
    """Suggest deleting old_text (exact, unique) as \\deleted{...}."""
    return _safe(svc().propose_delete, old_text, comment, section, occurrence, _origin(ctx, tag))


@mcp.tool()
def add_comment(anchor: str, text: str, section: str | None = None, occurrence: int | None = None,
                tag: str | None = None, ctx: Context | None = None) -> str:
    """Add a \\chcomment footnote after the anchor text (moved after the float if the
    anchor is inside a figure). Anchoring on a pending change attaches it to that change."""
    return _safe(svc().add_comment, anchor, text, section, occurrence, _origin(ctx, tag))


@mcp.tool()
def propose_move(text: str, after: str | None = None, before: str | None = None,
                 comment: str | None = None, section: str | None = None,
                 to_section: str | None = None, occurrence: int | None = None,
                 tag: str | None = None, ctx: Context | None = None) -> str:
    """Move text (sentences, a whole \item...) to a new place, tracked: \deleted where it
    was and \added after/before the destination anchor, each with a "Moved" comment.
    Use this for restructuring (splitting or reordering list items and paragraphs)."""
    return _safe(svc().propose_move, text, after, before, comment, section, to_section,
                 occurrence, _origin(ctx, tag))


@mcp.tool()
def plain_edit(old_text: str, new_text: str, section: str | None = None,
               occurrence: int | None = None, reason: str | None = None, force: bool = False,
               ctx: Context | None = None) -> str:
    """UNTRACKED edit, the human does not see it as a change. Only for equations and
    environments, a tiny typo fix the human asked for, or repairing broken markup.
    Changing more than a few words of prose is refused: use propose_replace /
    propose_insert / propose_delete / propose_move (they handle \item and headings).
    force=true with reason= makes an exception and leaves a \chcomment explaining it."""
    return _safe(svc().plain_edit, old_text, new_text, section, occurrence, _origin(ctx, None),
                 reason, force)


@mcp.tool()
def revise_change(id: str, new_text: str | None = None, comment: str | None = None,
                  keep_note: bool = False, evidence: list[dict[str, Any]] | None = None,
                  unchecked_reason: str | None = None, ctx: Context | None = None) -> str:
    """Rewrite a pending suggestion in place: new_text replaces its new text (a deletion
    becomes a replacement); comment sets/replaces its explaining \\chcomment.
    This answers the human's %N note on the change, so the %N marker is removed and the
    change becomes unrated for re-review (keep_note=true leaves it). To answer a note
    without changing the text, pass only comment (e.g. "Kept: <why>").
    New citations in new_text need evidence=[...] exactly as in propose_insert."""
    return _safe(svc().revise_change, id, new_text, comment, _origin(ctx, None), keep_note,
                 evidence, unchecked_reason)


@mcp.tool()
def withdraw_change(id: str) -> str:
    """Retract one of your suggestions (rejects it, restoring the original text)."""
    return _safe(svc().resolve, "reject", {"ids": [id]})


# ---- resolving --------------------------------------------------------------------
@mcp.tool()
def accept(selector: dict[str, Any], comments: str = "auto", honor_markers: bool = True) -> str:
    """Accept changes (make them plain text). selector as in list_changes, e.g.
    {"until_section": "Simulation and Methods", "except_ids": ["a3f9c2"]} or {"all": true}.
    With honor_markers, %N changes are skipped and %X ones rejected.
    comments: auto (drop \\chcomments except those matching the keep regex, default TODO),
    keep, or drop."""
    return _safe(svc().resolve, "accept", selector, comments, honor_markers)


@mcp.tool()
def reject(selector: dict[str, Any], comments: str = "auto", honor_markers: bool = True) -> str:
    """Reject changes (restore the original text). Same selector/comment rules as accept;
    with honor_markers, %Y changes are accepted instead and %N skipped."""
    return _safe(svc().resolve, "reject", selector, comments, honor_markers)


@mcp.tool()
def apply_markers(selector: dict[str, Any] | None = None, comments: str = "auto") -> str:
    """Apply the human's %Y/%X markers (accept/reject) and leave %N and unmarked changes."""
    return _safe(svc().resolve, "markers", selector or {"all": True}, comments)


# ---- build & sync -------------------------------------------------------------------
@mcp.tool()
def lint(fix: bool = False) -> str:
    """Check the changes markup for constructs that break the build; fix=true repairs
    what can be repaired mechanically."""
    return _safe(svc().lint, fix)


@mcp.tool()
def compile(isolate: bool = False) -> str:
    """Build the PDF with latexmk. Errors are mapped to the change they sit in;
    isolate=true recompiles with each suspect change rejected to confirm the culprit."""
    return _safe(svc().compile, isolate)


@mcp.tool()
def sync_init() -> str:
    """First-time setup: snapshot the current Overleaf state as the merge base."""
    return _safe(svc().sync_init)


@mcp.tool()
def pull() -> str:
    """Download the Overleaf project and 3-way merge remote edits into the local files."""
    return _safe(svc().pull)


@mcp.tool()
def push(force: bool = False) -> str:
    """Pull+merge, lint, compile, then upload to Overleaf. Refuses on merge conflicts,
    lint errors or a failed compile unless force=true. Only push when the human asks."""
    return _safe(svc().push, force)


@mcp.tool()
def sync_to_overleaf(agent: str | None = None) -> str:
    """"Sync to Overleaf": if Overleaf is unchanged since the last sync, push; if it
    changed, merge first. With agent=None and a real merge needed, this prepares a
    workspace and lists the options; ask the human which agent should merge (or
    resolve the files yourself, then call sync_finish). agent="git" accepts a clean
    automatic merge. Only when the human asks to sync."""
    return _safe(svc().sync_to_overleaf, agent)


@mcp.tool()
def sync_finish(push: bool = True) -> str:
    """Apply a merge workspace you resolved yourself (no conflict markers left),
    adopt Overleaf as the new base and push (push=false to stop before pushing)."""
    return _safe(svc().sync_finish, push)


# ---- claim verification --------------------------------------------------------------
@mcp.tool()
def verification_requests() -> str:
    """Open `% VERIFY <method>: «claim»` requests the human left, with context and
    instructions. Work through them and answer each with record_verification."""
    return _safe(svc().verification_requests)


@mcp.tool()
def record_verification(status: str, sources: list[dict[str, Any]] | None = None,
                        code: dict[str, Any] | None = None, reason: str = "",
                        request_id: str | None = None, anchor: str | None = None,
                        section: str | None = None) -> str:
    """Record the outcome of checking a claim. Redline writes the short line
    `% VERIFIED|REFUTED|UNVERIFIED <bibkey>#<n>` after the claim (in place of the
    request when request_id is given, else after the line where `anchor` ends), puts
    the evidence in the library, highlights each quote in its Zotero PDF and files a
    note in Zotero.
    sources: [{bibkey, page, quote, section?, role?, for_key?}]: quote = exact sentence(s) copied
    from the PDF, page = 1-based PDF page, section = "§3.1"/"Abstract"/"Table 2",
    role = supports (default) | contradicts | context. Quotes are matched on the page
    first; a quote that is not found is refused (with the closest page text) and
    nothing is written, so fix it and call again.
    code: {path, line, value} for checks against the analysis code.
    reason: why, for UNVERIFIED (what was looked at); optional otherwise."""
    return _safe(svc().record_verification, status, sources, code, reason, request_id, anchor, section)


@mcp.tool()
def evidence(id: str) -> str:
    """The evidence behind a result line (e.g. ho25b#1): claim, verdict, sources,
    pages, quotes and the Zotero state."""
    return _safe(svc().evidence, id)


@mcp.tool()
def evidence_sync(retry: bool = False) -> str:
    """Push evidence Zotero does not have yet (made while it was down), and pull
    entries made on other machines. retry=true also retries quotes that were not found."""
    return _safe(svc().evidence_sync, retry)


@mcp.tool()
def migrate_verifications(apply: bool = False, skip_unmatched: bool = False) -> str:
    """Move old inline `% VERIFIED key: "quote" — p.N` results into the evidence library
    (short ids in the paper, Zotero highlights and notes). Without apply: a dry run that
    reports which quotes are found in their PDFs."""
    return _safe(svc().migrate_verifications, apply, None, skip_unmatched)


@mcp.tool()
def verification_report() -> str:
    """Counts of verified/refuted/unverified claims and the open requests."""
    return _safe(svc().verification_report)


@mcp.tool()
def undo() -> str:
    """Revert the most recent write made through redline (keeps later unrelated edits)."""
    return _safe(svc().undo)


def main() -> None:
    mcp.run("stdio")
