"""Counting what a document asks of its reader, a piece of the file at a time."""

from __future__ import annotations

import io

import pytest

import core.attention_measure as mod


class _Counted:
    """Stands in for a path: hands out the bytes and counts the reads made of them."""

    def __init__(self, data: bytes):
        self._data = data
        self.sizes: list[int] = []

    def open(self, _mode: str) -> io.BytesIO:
        sizes = self.sizes

        class _Stream(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                sizes.append(-1 if size is None else size)
                return super().read(size)

        return _Stream(self._data)


def _whole(text: str) -> mod.Measure:
    """The count the same text gets when it fits in one piece."""
    return mod.Measure(
        words=len(mod._WORD.findall(mod._UNSPACED.sub(" ", text))),
        unspaced=len(mod._UNSPACED.findall(text)),
        images=len(mod._IMAGE.findall(text)),
    )


def test_a_file_that_is_not_text_is_given_up_on_at_its_first_piece(monkeypatch):
    """A large image or PDF registered as a document costs one read, not its size."""
    monkeypatch.setattr(mod, "_PIECE", 64)
    source = _Counted(b"\x89PNG\r\n\x1a\n" + b"\xff\xfe\x00" * 10_000)

    assert mod.measure_file(source) is None  # type: ignore[arg-type]
    assert source.sizes == [64]


def test_a_file_is_never_read_whole(monkeypatch):
    monkeypatch.setattr(mod, "_PIECE", 64)
    source = _Counted(("word " * 1_000).encode())

    assert mod.measure_file(source) == mod.Measure(words=1_000, unspaced=0, images=0)  # type: ignore[arg-type]
    assert set(source.sizes) == {64}


@pytest.mark.parametrize("piece", [7, 16, 64, 1 << 16])
@pytest.mark.parametrize("text", [
    "The lease runs to 30 June.\nRent is 2,400 a month, due on the first.\n" * 40,
    "naïve café déjà vu — señor Über straße\n" * 60,
    "intro ![frame](a.png) middle <img src='b.png'> end\n" * 30,
    "計画の概要です。" * 100 + " plan and 12 more words here to finish the page off now",
    "",
    "one",
])
def test_the_count_does_not_depend_on_where_a_piece_ends(monkeypatch, piece, text):
    """Words, characters cut mid-byte and image marks all survive a piece boundary."""
    monkeypatch.setattr(mod, "_PIECE", piece)

    assert mod.measure_file(_Counted(text.encode())) == _whole(text)  # type: ignore[arg-type]


def test_text_with_no_break_in_it_is_still_counted_a_piece_at_a_time(monkeypatch):
    """Unbroken text is not held until its end; a word split there may count twice."""
    monkeypatch.setattr(mod, "_PIECE", 64)
    monkeypatch.setattr(mod, "_UNBROKEN", 256)
    source = _Counted(b"a" * 10_000)

    measure = mod.measure_file(source)  # type: ignore[arg-type]

    # Counted once per 256 or so held characters, not once at the end of the file.
    assert measure is not None and 30 <= measure.words <= 40
    assert set(source.sizes) == {64}


def test_a_missing_file_is_unmeasured(tmp_path):
    assert mod.measure_file(tmp_path / "gone.md") is None


def test_a_file_cut_off_inside_a_character_is_unmeasured():
    assert mod.measure_file(_Counted("café".encode()[:-1])) is None  # type: ignore[arg-type]
