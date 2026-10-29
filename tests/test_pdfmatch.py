from redline.pdfmatch import Rect, Word, line_rects, locate, tex_tokens


def words_from(lines, y=100.0):
    out = []
    for row, line in enumerate(lines):
        x = 50.0
        for t in line.split():
            out.append(Word(x, y + 12 * row, x + 6 * len(t), y + 12 * row + 9, t))
            x += 6 * len(t) + 4
    return out


def test_tex_tokens_drops_markup():
    assert tex_tokens(r"so \textit{each} molecule~radiates $\propto n$ \citep{bromm02}, at") == \
        ["so", "each", "molecule", "radiates", "at"]


def test_locate_excerpt_across_lines_and_hyphen():
    page = words_from(["The gas cools and each mole-", "cule radiates at a rate that no", "longer depends on density."])
    rects = locate(page, 1, r"each molecule radiates at a rate that no longer depends", None)
    assert len(rects) == 3
    assert rects[0].x0 > 50 and rects[0].y0 == 100.0   # starts at "each", not at the line start
    assert rects[2].y0 == 124.0


def test_locate_prefers_near_boxes():
    page = words_from(["foo bar baz qux", "x", "x", "x", "x", "x", "x", "x", "foo bar baz qux"])
    near = [Rect(1, 40, 196, 300, 206)]
    rects = locate(page, 1, "foo bar baz qux", near)
    assert rects and rects[0].y0 == 196.0


def test_no_match_returns_nothing():
    assert locate(words_from(["completely different words here"]), 1, "accretion luminosity protostar", None) == []


def test_locate_across_a_page_break():
    a = words_from(["unrelated top text here", "the infall continues and the gas"], y=700.0)
    b = words_from(["warms more slowly and stays warm", "Next paragraph starts"], y=60.0)
    for w in a:
        w.page = 16
    for w in b:
        w.page = 17
    rects = locate(a + b, 16, "the infall continues and the gas warms more slowly and stays warm", None)
    assert {r.page for r in rects} == {16, 17}
    assert [r.page for r in rects] == [16, 17]


def test_stitch_keeps_both_highlights_in_view():
    from PIL import Image
    from redline.preview import Pane
    s = 2.0
    pa, pb = Image.new("RGB", (1224, 1584), "white"), Image.new("RGB", (1224, 1584), "white")
    rects = [Rect(16, 300, 700, 560, 712), Rect(17, 50, 66, 300, 76)]
    img, hl = Pane._stitch([pa, pb], rects, s, (740.0, 60.0))
    # first page cut at its middle, trimmed below its last text line; second
    # trimmed above its first line and cut at its middle
    assert img.height < 1584
    (x0, y0, x1, y1), (u0, v0, u1, v1) = hl
    assert 0 <= y0 < y1 <= img.height and 0 <= v0 < v1 <= img.height and y1 < v0


def test_caption_words_are_not_glued_on():
    # "...formation in the middle." (caption) precedes the continuation in reading order
    page = words_from(["Fig. 11 profiles preventing star formation in the middle.",
                       "but through a numerical coupling rather than through starlight"])
    rects = locate(page, 1, "that in the code follows the radiation of the new protostar but through "
                            "a numerical coupling rather than through starlight", None)
    assert len(rects) == 1 and rects[0].y0 == 112.0  # only the real continuation


def test_jump_over_a_figure_skips_it():
    page = words_from(["the gas follows the radiation of the new protostar",
                       "Fig 3 axis label caption words that are not part of it",
                       "but through a numerical coupling rather than starlight"])
    rects = locate(page, 1, "the gas follows the radiation of the new protostar but through "
                            "a numerical coupling rather than starlight", None)
    assert [r.y0 for r in rects] == [100.0, 124.0]  # the caption line in between is not marked
