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
