"""Evidence library: short result ids, Zotero highlights and notes (Zotero faked)."""
import shutil
from pathlib import Path

import pytest

from redline import evidence, ops, verify
from redline.evidence_view import zotero_rects
from redline.model import RedlineError
from redline.parse import parse
from redline.service import Service
from redline.state import Project
from redline.zotero import ZoteroError

FIX = Path(__file__).parent / "fixtures" / "mini.tex"
PAGE = {  # what the fake PDFs say, per (attachment, page)
    ("ATT1", 2): "We find that first stars form in minihaloes of about 1e6 solar masses.",
    ("ATT2", 5): "Accretion rates of 1e-3 Msun/yr are typical.",
}


class FakeZotero:
    def __init__(self):
        self.up = True
        self.highlights, self.notes = {}, {}

    def available(self):
        return self.up

    def resolve(self, bibkey, item_key=""):
        att = {"abel02": "ATT1", "bromm04": "ATT2"}.get(bibkey)
        if not att:
            raise ZoteroError(f"{bibkey}: not found in Zotero")
        return "ITEM" + att[-1], att

    def find_page(self, item, text):
        return next((p for (a, p), t in PAGE.items() if text in t), None)

    def check(self, att, quotes):
        return [{"ok": True, "page_found": p} if t in PAGE.get((att, p), "")
                else {"ok": False, "error": "Error: Could not find text\nBest match text: \"nearby words\""}
                for p, t in quotes]

    def highlight(self, att, page, text, comment, tags, color):
        key = f"ANN{len(self.highlights) + 1}"
        self.highlights[key] = dict(att=att, page=page, text=text, tags=tags, color=color, comment=comment)
        return {"key": key, "page_index": page - 1, "rects": [[100, 700, 300, 712]]}

    def collection(self, path):
        return "COLL"

    def save_note(self, html, tags, coll, key=""):
        key = key or f"NOTE{len(self.notes) + 1}"
        self.notes[key] = {"data": {"note": html, "tags": [{"tag": t} for t in tags]}, "key": key}
        return key

    def notes_tagged(self, tag):
        return [n for n in self.notes.values() if any(t["tag"] == tag for t in n["data"]["tags"])]


@pytest.fixture
def svc(tmp_path, monkeypatch):
    shutil.copy(FIX, tmp_path / "paper.tex")
    monkeypatch.setenv("REDLINE_ROOT", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    s = Service(Project.open())
    s._zot = FakeZotero()
    return s


QUOTE = {"bibkey": "abel02", "page": 2, "quote": "first stars form in minihaloes", "section": "§1"}


def test_record_writes_a_short_id_and_files_the_evidence(svc):
    out = svc.record_verification("VERIFIED", sources=[QUOTE], anchor="Pop III stars form")
    assert "abel02#1" in out and "Zotero: synced" in out
    t = svc.proj.read()
    assert "% VERIFIED abel02#1\n" in t and "first stars form" not in t
    e = svc.library().get("abel02#1")
    s = e.sources[0]
    assert (s.att_key, s.ann_key, s.page, s.highlight) == ("ATT1", "ANN1", 2, "ok")
    assert e.claim and e.note_key == "NOTE1"
    h = svc._zot.highlights["ANN1"]
    assert "rl:abel02#1" in h["tags"] and h["color"] == "green" and h["comment"].startswith("abel02#1 supports")
    # the Zotero note carries the whole entry: another machine can rebuild it
    again = evidence.entry_from_note(svc._zot.notes["NOTE1"]["data"]["note"])
    assert again.id == "abel02#1" and again.sources[0].quote == QUOTE["quote"]


def test_ids_count_up_per_source_and_list_other_sources(svc):
    svc.record_verification("VERIFIED", sources=[QUOTE], anchor="Pop III stars form")
    svc.record_verification("VERIFIED", sources=[
        QUOTE, {"bibkey": "bromm04", "page": 5, "quote": "Accretion rates of 1e-3"}], anchor="at a high rate.")
    t = svc.proj.read()
    assert "% VERIFIED abel02#2 (+bromm04)\n" in t
    [a, b] = [r for r in verify.results(parse(t))]
    assert (a.id, b.id, b.also) == ("abel02#1", "abel02#2", ("bromm04",))


def test_a_quote_not_in_the_pdf_is_refused_and_nothing_written(svc):
    before = svc.proj.read()
    with pytest.raises(RedlineError, match="(?s)nothing recorded.*closest text"):
        svc.record_verification("VERIFIED", sources=[dict(QUOTE, quote="stars are made of cheese")],
                                anchor="Pop III stars form")
    assert svc.proj.read() == before and not svc.library().entries


def test_zotero_down_records_now_and_syncs_later(svc):
    svc._zot.up = False
    out = svc.record_verification("VERIFIED", sources=[QUOTE], anchor="Pop III stars form")
    assert "pending" in out and "% VERIFIED abel02#1" in svc.proj.read()
    svc._zot.up = True
    assert "pushed 1" in svc.evidence_sync()
    e = svc.library().get("abel02#1")
    assert e.zotero == "synced" and e.sources[0].ann_key


def test_pull_rebuilds_the_library_from_zotero(svc):
    svc.record_verification("VERIFIED", sources=[QUOTE], anchor="Pop III stars form")
    (svc.proj.dir / "evidence.json").unlink()
    assert "pulled 1" in svc.evidence_sync()
    assert svc.library().get("abel02#1").sources[0].ann_key == "ANN1"


def test_unverified_and_code_results(svc):
    svc.record_verification("UNVERIFIED", reason="no paper backs this", anchor="Pop III stars form")
    svc.record_verification("VERIFIED", code={"path": "a.py", "line": 3, "value": "2e-4"}, anchor="at a high rate.")
    t = svc.proj.read()
    assert "% UNVERIFIED nosrc#1\n" in t and "% VERIFIED code#1\n" in t
    assert "no paper backs this" in svc.evidence("nosrc#1")


LEGACY = [
    ('ho25b: "turbulence is predominantly supersonic" — Abstract, p.1 (15 haloes)',
     [("ho25b", 1, "Abstract", "turbulence is predominantly supersonic")]),
    ('Springel_2005: "Gravitational forces are computed" — Abstract, p.1; §3.1.3, p.8',
     [("Springel_2005", 1, "Abstract", "Gravitational forces are computed")]),
    ('yoshida03: "critical halo mass" — Abstract; "the minimum collapse mass" — §3, p.4. '
     'kulkarni21: "Mz=20 increases" — §3.1, p.5',
     [("yoshida03", 1, "Abstract", "critical halo mass"), ("yoshida03", 4, "§3", "the minimum collapse mass"),
      ("kulkarni21", 5, "§3.1", "Mz=20 increases")]),
    ('meziani26b (draft): "top-heavy slope"; "alpha = 1.9" — Abstract, p.1',
     [("meziani26b", 1, "Abstract", "top-heavy slope"), ("meziani26b", 1, "Abstract", "alpha = 1.9")]),
]


@pytest.mark.parametrize("text,want", LEGACY)
def test_parse_legacy(text, want):
    srcs, reason = evidence.parse_legacy("VERIFIED", text)
    assert [(s.bibkey, s.page, s.section, s.quote) for s in srcs] == want and not reason


def test_match_text_skips_ellipses():
    assert evidence.match_text("short ... a much longer stretch of words here") == "a much longer stretch of words here"


def test_migrate_dry_run_then_apply(svc):
    src = svc.proj.read().replace(
        "\\citep{abel02}.\n",
        "\\citep{abel02}.\n% VERIFIED abel02: \"first stars form in minihaloes\" — §1, p.2\n", 1)
    src = src.replace("at a high rate.\n", "at a high rate.\n% VERIFIED bromm04: \"made-up words\" — p.5\n", 1)
    svc.proj.main.write_text(src)
    dry = svc.migrate_verifications()
    assert "2 quote(s): 1 found" in dry and "made-up words" in dry and "dry run" in dry
    assert svc.proj.read() == src
    out = svc.migrate_verifications(apply=True)
    t = svc.proj.read()
    assert "% VERIFIED abel02#1\n" in t and "% VERIFIED bromm04#1\n" in t and "made-up" not in t, out
    lib = svc.library()
    assert lib.get("abel02#1").sources[0].highlight == "ok"
    assert lib.get("bromm04#1").sources[0].highlight.startswith("none")  # kept, without a highlight
    assert lib.get("bromm04#1").sources[0].quote == "made-up words"
    assert "nothing to migrate" in svc.migrate_verifications()


def test_zotero_rects_are_flipped_to_top_left(tmp_path, monkeypatch):
    monkeypatch.setattr("redline.evidence_view.page_height", lambda pdf, page: 800.0)
    s = evidence.Source(page=3, page_index=2, rects=[[100, 700, 300, 712]])
    [r] = zotero_rects(s, tmp_path / "x.pdf")
    assert (r.page, r.x0, r.y0, r.x1, r.y1) == (3, 100, 88, 300, 100)


def test_bib_fields_and_plain_titles():
    from redline.zotero import parse_bib, plain
    bib = parse_bib('@article{oh02,\n  title = {Halos with Virial Temperatures above $10^4$ K},\n'
                    '  doi = {10.1086/339393}, eprint = {astro-ph/0108071}}\n'
                    '@ARTICLE{shang10,\n    title = "{keeping gas H$_2$ free, T$_{vir}$ > 10$^4$ K}",\n}\n')
    assert bib["oh02"]["doi"] == "10.1086/339393" and bib["oh02"]["eprint"] == "astro-ph/0108071"
    assert plain(bib["shang10"]["title"]) == "keeping gas H2 free, Tvir > 104 K"
