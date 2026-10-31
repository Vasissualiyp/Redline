"""kitty graphics protocol output: chunks, tmux wrapping, placeholders, re-use."""
import io

from redline.kittygfx import DIACRITICS, PLACEHOLDER, Kitty


def test_send_is_chunked_wrapped_for_tmux_and_quiet():
    out = io.BytesIO()
    k = Kitty(out=out, tmux=True)
    k.next_id = 0x123456
    iid = k.send("p1", b"x" * 10000, 30, 12)
    s = out.getvalue().decode()
    chunks = s.split("\x1bPtmux;")[1:]
    assert iid == 0x123456 and len(chunks) == 4  # 13336 base64 chars in 4 kB chunks
    assert chunks[0].startswith("\x1b\x1b_Ga=T,U=1,f=100,t=d,i=1193046,c=30,r=12,q=2,m=1;")
    assert all(c.endswith("\x1b\x1b\\\x1b\\") for c in chunks)
    assert chunks[-1].startswith("\x1b\x1b_Gm=0,q=2;")


def test_show_prints_placeholders_and_reuses_the_image():
    out = io.BytesIO()
    k = Kitty(out=out, tmux=False)
    k.next_id = 0x010203
    k.send("p1", b"png", 3, 2)
    sent = len(out.getvalue())
    k.show("p1")
    s = out.getvalue()[sent:].decode()
    assert "\x1b[38;2;1;2;3m" in s
    assert f"\x1b[1;1H{PLACEHOLDER}{chr(DIACRITICS[0])}{chr(DIACRITICS[0])}{PLACEHOLDER * 2}" in s
    assert f"\x1b[2;1H{PLACEHOLDER}{chr(DIACRITICS[1])}{chr(DIACRITICS[0])}" in s
    assert k.has("p1") and len(DIACRITICS) == 297


def test_old_images_are_deleted_from_kitty():
    out = io.BytesIO()
    k = Kitty(out=out, tmux=False, keep=2)
    first = k.send("a", b"1", 1, 1)
    k.send("b", b"2", 1, 1)
    k.send("c", b"3", 1, 1)
    assert not k.has("a") and f"a=d,d=I,i={first},q=2" in out.getvalue().decode()
