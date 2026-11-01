"""Evidence library: short result ids, Zotero highlights and notes (Zotero faked)."""
import shutil
from pathlib import Path

import pytest

from redline import citechain, evidence, ops, verify
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


BIB = {  # the paper's .bib: abel02 (the paper a quote mentions) and two candidate keys
    "abel02": {"title": "A source paper", "author": "Abel, Tom", "year": "2002"},
    "fg20": {"title": "A cosmic UV/X-ray background model update", "author": "Faucher-Gigu{\\`e}re, Claude-Andr{\\'e}",
             "year": "2020", "volume": "493", "pages": "1614--1632", "doi": "10.1093/mnras/staa302"},
    "chan19": {"title": "Cosmic ray feedback in the FIRE simulations", "author": "Chan, T K and Kere{\\v s}, D",
               "year": "2019", "volume": "488", "pages": "3716-3744", "doi": "10.1093/mnras/stz1895"},
}
REFS = [citechain.RefEntry("Faucher-Giguère C.-A., 2020, MNRAS, 493, 1614", 9, "Faucher-Giguère", "2020"),
        citechain.RefEntry("Faucher-Giguère C.-A., Quataert E., 2012, MNRAS, 425, 605", 9, "Faucher-Giguère", "2012")]


class FakeZotero:
    def __init__(self):
        self.up = True
        self.highlights, self.notes = {}, {}
        self.pulled = []

    def available(self):
        return self.up

    def resolve(self, bibkey, item_key=""):
        att = {"abel02": "ATT1", "bromm04": "ATT2", "fg20": "ATT3", "chan19": "ATT4"}.get(bibkey)
        if not att:
            raise ZoteroError(f"{bibkey}: not found in Zotero")
        if bibkey in ("fg20", "chan19") and bibkey not in self.pulled and not item_key:
            self.pulled.append(bibkey)  # in neither Zotero nor the library until pulled
        return "ITEM" + att[-1], att

    def pdf_path(self, att):
        return Path("/fake") / (att + ".pdf")

    def _bib_entries(self):
        return BIB

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

    def get(self, path):
        return {"data": {"title": "A paper"}}

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


def test_migrate_can_leave_unmatched_results_inline(svc):
    src = svc.proj.read().replace(
        "\\citep{abel02}.\n",
        "\\citep{abel02}.\n% VERIFIED abel02: \"first stars form in minihaloes\" — §1, p.2\n", 1)
    src = src.replace("at a high rate.\n", "at a high rate.\n% VERIFIED bromm04: \"made-up words\" — p.5\n", 1)
    svc.proj.main.write_text(src)
    out = svc.migrate_verifications(apply=True, skip_unmatched=True)
    t = svc.proj.read()
    assert "% VERIFIED abel02#1\n" in t and '% VERIFIED bromm04: "made-up words" — p.5\n' in t, out
    assert "bromm04#1" not in svc.library().entries and "left inline" in out


def test_block_of_stacked_results(svc):
    from redline.evidence_view import block_ids
    src = svc.proj.read().replace(
        "\\citep{abel02}.\n",
        "\\citep{abel02}.\n% VERIFIED abel02#1\n% VERIFY any: «x»\n% VERIFIED bromm04#2 (+abel02)\n"
        "\n% VERIFIED other#1\n", 1)
    svc.proj.main.write_text(src)
    assert block_ids(svc.proj, "bromm04#2") == ["abel02#1", "bromm04#2"]
    assert block_ids(svc.proj, "other#1") == ["other#1"]


def test_note_is_wrapped_to_the_pane():
    import re
    from redline.evidence_view import rich_text
    e = evidence.Entry("abel02#1", "VERIFIED", claim="word " * 60, sources=[
        evidence.Source(bibkey="abel02", page=2, quote="quote words " * 30, section="§1", highlight="ok")])
    text = rich_text(e, 50, ["abel02#1"])
    visible = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in text.splitlines()]
    assert max(len(ln) for ln in visible) <= 50
    assert "VERIFIED  abel02#1" in visible[0] and any(ln.startswith("  │ quote") for ln in visible)


# ---- every new citation is refchecked ------------------------------------------------
def test_cited_text_without_evidence_is_refused(svc):
    before = svc.proj.read()
    with pytest.raises(RedlineError, match="refchecked.*nothing was written.*abel02"):
        svc.propose_insert(" (see \\citep{abel02})", after="Pop III stars form in minihaloes")
    assert svc.proj.read() == before


def test_cited_text_with_evidence_gets_its_result_line(svc):
    out = svc.propose_insert(" as shown by \\citep{abel02}", after="stars form in minihaloes",
                             evidence=[QUOTE])
    t = svc.proj.read()
    assert "\\added{as shown by \\citep{abel02}}" in t and "% VERIFIED abel02#1" in t, t
    assert "refchecked" in out and "quote found on p.2" in out
    assert svc.library().get("abel02#1").sources[0].ann_key == "ANN1"
    assert not [i for i in __import__("redline.lint", fromlist=["x"]).lint(svc.proj.doc())
                if i.code == "unverified-citation"]


def test_wrong_quote_refuses_the_whole_edit(svc):
    before = svc.proj.read()
    with pytest.raises(RedlineError, match="exact text"):
        svc.propose_insert(" \\citep{abel02}", after="stars form in minihaloes",
                           evidence=[dict(QUOTE, quote="made up words")])
    assert svc.proj.read() == before and not svc.library().entries


def test_zotero_down_refuses_cited_edits(svc):
    svc._zot.up = False
    with pytest.raises(RedlineError, match="Zotero is not reachable"):
        svc.propose_insert(" \\citep{abel02}", after="stars form in minihaloes", evidence=[QUOTE])


def test_unchecked_reason_marks_unverified(svc):
    svc.propose_insert(" \\citep{abel02}", after="stars form in minihaloes", unchecked_reason="author said skip")
    t = svc.proj.read()
    assert "% UNVERIFIED abel02#1" in t
    assert "author said skip" in svc.evidence("abel02#1")


def test_uncited_edits_and_existing_citations_need_no_evidence(svc):
    svc.propose_insert(" really", after="stars form in minihaloes")
    svc.propose_replace("We study", "We examine")  # no citation involved
    assert "\\added{really}" in svc.proj.read()


def test_plain_edit_cannot_add_a_citation(svc):
    with pytest.raises(RedlineError, match="cannot add citations"):
        svc.plain_edit("high rate.", "high rate \\citep{bromm04}.")


def test_rejecting_a_cited_change_removes_its_verified_line(svc):
    from redline import ops
    from redline.parse import parse
    svc.propose_insert(" as shown by \\citep{abel02}", after="stars form in minihaloes", evidence=[QUOTE])
    doc = parse(svc.proj.read())
    it = next(i for i in doc.items if "as shown" in (i.new(doc.src) or ""))
    out = ops.apply(doc.src, ops.resolve(doc, ops.decide(doc, [it], "reject", honor_markers=False)))
    assert "as shown" not in out and "abel02#1" not in out
    acc = ops.apply(doc.src, ops.resolve(doc, ops.decide(doc, [it], "accept", honor_markers=False)))
    assert "% VERIFIED abel02#1" in acc  # accepted: the evidence stays


def test_lint_flags_hand_added_unchecked_citations(svc):
    from redline import lint
    from redline.parse import parse
    src = svc.proj.read().replace("minihaloes", "minihaloes\\added{ \\citep{bromm04}}", 1)
    issues = [i for i in lint.lint(parse(src)) if i.code == "unverified-citation"]
    assert issues and "bromm04" in issues[0].message


# ---- citation chain: does the source's reference list point at the cited paper? ---------
MENTION = {"bibkey": "abel02", "page": 2, "quote": "first stars form in minihaloes", "section": "§1"}


@pytest.fixture
def chain(svc, monkeypatch):
    PAGE[("ATT1", 2)] = "We update the assumed UVB to the more recent Faucher-Giguere (2020) model."
    PAGE[("ATT1", 9)] = "Faucher-Giguère C.-A., 2020, MNRAS, 493, 1614"
    monkeypatch.setattr(citechain, "reference_entries", lambda pdf: REFS)
    monkeypatch.setattr(svc, "_bibfile", lambda: Path("paper.bib"))
    return svc


def test_mentions_in_a_quote():
    ms = citechain.mentions("we update to the more recent Faucher-Giguere (2020) model; see Hopkins et al. 2018a")
    assert [(m.author, m.year, m.suffix) for m in ms] == [("Faucher-Giguere", "2020", ""), ("Hopkins", "2018", "a")]
    assert citechain.mentions("as in (Springel & Hernquist 2003)")[0].text == "Springel & Hernquist 2003)"
    assert citechain.mentions("In 2020 the model was updated") == []


def test_reference_entries_split_and_compare():
    text = ("Anglés-Alcázar D., Davé R., Faucher-Giguère C.-A., Özel F., Hopkins P. F.,\n2017a, MNRAS, 464, 2840\n"
            "Anglés-Alcázar D., Faucher-Giguère C.-A., Kereš D., Hopkins P. F.,\nQuataert E., 2017b, MNRAS, 470, 4698\n"
            "Faucher-Giguère C.-A., 2020, MNRAS, 493, 1614\nChan T. K., et al., 2019, MNRAS, 488, 3716\n")
    ents = citechain.parse_entries([(9, text)])
    assert [(e.author, e.year, e.suffix) for e in ents] == [("Anglés-Alcázar", "2017", "a"),
                                                              ("Anglés-Alcázar", "2017", "b"),
                                                              ("Faucher-Giguère", "2020", ""), ("Chan", "2019", "")]
    fg = ents[2]
    assert citechain.compare(fg, citechain.meta_of("fg20", BIB["fg20"]))[0] == "match"
    verdict, why = citechain.compare(fg, citechain.meta_of("chan19", BIB["chan19"]))
    assert verdict == "mismatch" and "vol. 493" in why and "488" in why


def test_the_right_paper_is_recorded_with_its_reference_entry(chain):
    out = chain.record_verification("VERIFIED", sources=[dict(MENTION, quote="Faucher-Giguere (2020) model",
                                                              for_key="fg20")], anchor="Pop III stars form")
    e = chain.library().get("fg20#1")
    refs = [s for s in e.sources if s.role == "reference"]
    assert len(refs) == 1 and refs[0].verdict == "match" and refs[0].target == "fg20" and refs[0].page == 9
    assert "MNRAS, 493, 1614" in refs[0].quote and refs[0].ann_key  # highlighted in Zotero too
    assert "reference check" in out and "MATCH" in out.upper()
    assert chain._zot.highlights[refs[0].ann_key]["color"] == "orange"
    assert "% VERIFIED fg20#1" in chain.proj.read()


def test_the_wrong_paper_is_refused(chain):
    before = chain.proj.read()
    with pytest.raises(RedlineError, match="(?s)nothing recorded.*Faucher-Giguère C.-A., 2020, MNRAS, 493, 1614.*chan19"):
        chain.record_verification("VERIFIED", sources=[dict(MENTION, quote="Faucher-Giguere (2020) model",
                                                            for_key="chan19")], anchor="Pop III stars form")
    assert chain.proj.read() == before and not chain.library().entries


def test_for_key_needs_the_quote_to_name_the_paper(chain):
    with pytest.raises(RedlineError, match="does not name fg20"):
        chain.record_verification("VERIFIED", sources=[dict(MENTION, quote="We update the assumed UVB",
                                                            for_key="fg20")], anchor="Pop III stars form")


def test_cited_edit_with_a_secondary_source(chain):
    out = chain.propose_insert(" (UVB: \\citep{fg20})", after="stars form in minihaloes",
                               evidence=[dict(MENTION, quote="Faucher-Giguere (2020) model", for_key="fg20")])
    t = chain.proj.read()
    assert "\\citep{fg20}" in t and "% VERIFIED fg20#1" in t
    assert "reference check" in out and "MATCH" in out.upper()
    # the same text pointing at the wrong key never gets in
    with pytest.raises(RedlineError, match="not fg20|not chan19"):
        chain.propose_insert(" (also \\citep{chan19})", after="stars form in minihaloes",
                             evidence=[dict(MENTION, quote="Faucher-Giguere (2020) model", for_key="chan19")])


def test_a_cited_paper_missing_from_zotero_is_pulled(chain):
    out = chain.propose_insert(" (UVB: \\citep{fg20})", after="stars form in minihaloes",
                               evidence=[dict(MENTION, quote="Faucher-Giguere (2020) model", for_key="fg20")])
    assert "pulled into Zotero (with PDF): fg20" in out


# ---- Zotero.pull: DOI, then arXiv, then an open-access copy ----------------------------
def _zot_with(monkeypatch, bib, fail=(), found_after=None):
    from redline import zotero
    z = zotero.Zotero()
    z._bib = {"k": bib}
    z.paper = "P"
    calls = []

    def cli(*args, stdin=None):
        calls.append(args)
        if args[0] == "add" and (args[1], args[2]) in fail:
            raise ZoteroError("no PDF could be attached")
        return {}
    added = []
    monkeypatch.setattr(z, "_cli", cli)
    monkeypatch.setattr(z, "_search_item", lambda key: "ITEM1" if calls and any(c[0] == "add" for c in calls) else "")
    monkeypatch.setattr(z, "_pdf_attachment", lambda item: "ATT")
    monkeypatch.setattr(z, "_openalex", lambda title, year: [("10.1/oa", "https://oa.example/p.pdf")])
    monkeypatch.setattr("time.sleep", lambda s: None)
    return z, calls


def test_pull_uses_the_doi_first(monkeypatch):
    z, calls = _zot_with(monkeypatch, {"doi": "10.1093/mnras/staa302", "title": "A title", "year": "2020"})
    assert z.pull("k") == "ITEM1" and z.pulled == ["k"]
    adds = [c for c in calls if c[0] == "add"]
    assert adds[0][:3] == ("add", "doi", "10.1093/mnras/staa302") and "required" in adds[0]
    assert "redline-pulled,redline:P" in adds[0] and len(adds) == 1


def test_pull_falls_back_to_arxiv_then_open_access(monkeypatch):
    z, calls = _zot_with(monkeypatch, {"doi": "10.1/paywalled", "eprint": "2001.01234", "title": "T", "year": "2020"},
                         fail={("doi", "10.1/paywalled"), ("url", "https://arxiv.org/abs/2001.01234")})
    assert z.pull("k") == "ITEM1"
    assert [c[1:3] for c in calls if c[0] == "add"] == [("doi", "10.1/paywalled"),
                                                         ("url", "https://arxiv.org/abs/2001.01234"),
                                                         ("doi", "10.1/oa")]


def test_pull_failure_names_every_attempt(monkeypatch):
    z, _ = _zot_with(monkeypatch, {"doi": "10.1/x", "title": "T", "year": "2020"},
                     fail={("doi", "10.1/x"), ("doi", "10.1/oa"), ("url", "https://oa.example/p.pdf")})
    with pytest.raises(ZoteroError, match="(?s)could not pull.*10.1/x.*oa.example.*paywalled"):
        z.pull("k")
    with pytest.raises(ZoteroError, match="not in the .bib"):
        z.pull("unknown")
