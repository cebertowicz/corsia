"""What a document asks of the person who has to read it, counted from its file.

``core.attention_notice`` states these counts to the agent that handed the
document over. Counting is by reading the file as UTF-8 text: its words, the
least time they take to read, and its images. Text written without spaces
(Han, kana) is counted in characters, with no reading time, because the
reading rate below is a words figure.

The file is read a piece at a time. A file that is not text (an image, a PDF,
a video registered as a document) is given up on at its first piece that does
not decode, and a long text file is never held whole, so what a count holds
in memory does not grow with the size of what an agent registers.
"""

from __future__ import annotations

import codecs
import math
import re
from dataclasses import dataclass
from pathlib import Path

#: Average adult silent reading rate for English non-fiction (Brysbaert 2019,
#: "How many words do we read per minute?", Journal of Memory and Language).
#: Reading to judge is slower, so the time stated is a floor.
_WORDS_PER_MINUTE = 238

_WORD = re.compile(r"[^\W_]+")
#: Han and kana, which are written without spaces: a run of them is one
#: ``_WORD`` match however long, so they are counted as characters instead.
_UNSPACED = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")
_IMAGE = re.compile(r"!\[|<img\b", re.IGNORECASE)
_BREAKS = (" ", "\n", "\t", "\r")

#: Bytes read at a time.
_PIECE = 1 << 16
#: Characters of text with no whitespace in it that are held before they are
#: counted as they stand. Text is otherwise counted up to its last whitespace,
#: so no word or image mark is split; past this length a split word can count
#: twice and a split image mark not at all.
_UNBROKEN = 1 << 16


@dataclass(frozen=True)
class Measure:
    """What one document asks of its reader."""

    words: int
    unspaced: int
    images: int

    @property
    def in_words(self) -> bool:
        """Whether word count describes the text (false for unspaced scripts)."""
        return self.words >= self.unspaced


def measure_file(path: Path) -> Measure | None:
    """Count what the file asks of a reader, or None when it cannot be read as text."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    words = unspaced = images = 0
    held = ""
    try:
        with path.open("rb") as handle:
            while True:
                piece = handle.read(_PIECE)
                text = held + decoder.decode(piece, final=not piece)
                held = ""
                if piece:
                    # Count up to the last whitespace; the rest joins the next piece.
                    cut = _last_break(text)
                    text, held = text[:cut], text[cut:]
                unspaced += len(_UNSPACED.findall(text))
                words += len(_WORD.findall(_UNSPACED.sub(" ", text)))
                images += len(_IMAGE.findall(text))
                if not piece:
                    return Measure(words=words, unspaced=unspaced, images=images)
    except (OSError, UnicodeDecodeError):
        return None


def _last_break(text: str) -> int:
    """Where to stop counting this piece: after its last whitespace.

    With none, nothing is counted yet, until the unbroken text is too long to hold.
    """
    cut = max(text.rfind(mark) for mark in _BREAKS) + 1
    return cut or (len(text) if len(text) > _UNBROKEN else 0)


def describe(measure: Measure | None) -> str:
    """One document's length as the notice states it."""
    if measure is None:
        return "its length could not be measured, because the file could not be read as text"
    if measure.in_words:
        length = f"{measure.words:,} words, at least {_duration(_minutes(measure.words))} to read"
    else:
        length = f"{measure.unspaced:,} characters"
    if measure.images:
        length += f", {measure.images} image{'s' if measure.images != 1 else ''}"
    return length


def total_of(measures: list[Measure | None]) -> str:
    """The words and least reading time of several documents, or nothing when none is in words."""
    words = sum(item.words for item in measures if item is not None and item.in_words)
    return f": {words:,} words and at least {_duration(_minutes(words))} in all" if words else ""


def _minutes(words: int) -> int:
    return max(1, math.ceil(words / _WORDS_PER_MINUTE))


def _duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if rest or not hours:
        parts.append(f"{rest} minute{'s' if rest != 1 else ''}")
    return " ".join(parts)
