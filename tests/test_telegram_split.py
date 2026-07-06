"""Message splitting — Telegram rejects >4096-char messages."""

from app.telegram_client import _split_text


def test_short_text_is_single_chunk():
    assert _split_text("hello") == ["hello"]


def test_splits_at_newline_boundary():
    lines = [f"line {i:04d} " + "x" * 90 for i in range(50)]  # ~5000 chars
    text = "\n".join(lines)
    chunks = _split_text(text)
    assert len(chunks) == 2
    assert all(len(c) <= 4000 for c in chunks)
    assert "\n".join(chunks) == text          # nothing lost
    assert not chunks[0].endswith("x" * 91)   # cut fell on a line boundary


def test_no_newline_falls_back_to_hard_cut():
    text = "a" * 9000
    chunks = _split_text(text)
    assert [len(c) for c in chunks] == [4000, 4000, 1000]
    assert "".join(chunks) == text
