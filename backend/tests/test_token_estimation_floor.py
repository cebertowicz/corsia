"""The dense-run floor is a regex now; it must count exactly what the loop did."""

from __future__ import annotations

import random
import sys

import pytest

from core.token_estimation import _LONG_UNBROKEN_RUN_CHARS as N
from core.token_estimation import _long_unbroken_token_floor


def _reference(text: str) -> int:
    """The original per-character implementation, kept as the oracle."""
    total = 0
    run = 0
    for ch in text:
        if ch.isspace():
            if run >= N:
                total += run
            run = 0
        else:
            run += 1
    if run >= N:
        total += run
    return total


# \x1c-\x1f and \x85 are whitespace to str.isspace; U+200B is not.
_EDGE_CHARS = [
    " ", "\t", "\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f", "\x85",
    "\xa0", " ", " ", " ", " ", " ", " ", " ",
    "　", "​", "﻿", "᠎", "a", "é", "漢", "\U0001f600",
]


@pytest.mark.parametrize("sep", _EDGE_CHARS)
def test_each_separator_matches_the_loop(sep):
    for left in (N - 1, N, N + 1):
        for right in (N - 1, N, N + 1):
            text = "x" * left + sep + "y" * right
            assert _long_unbroken_token_floor(text) == _reference(text)


def test_split_boundaries_are_exactly_isspace_for_every_code_point():
    for cp in range(sys.maxunicode + 1):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        assert (ch.split() == []) == ch.isspace(), hex(cp)


@pytest.mark.parametrize("length", [0, 1, N - 1, N, N + 1, 3 * N])
def test_runs_at_below_and_above_the_threshold(length):
    for text in ("x" * length, "x" * length + " ", " " + "x" * length,
                 " " * 3 + "x" * length + " " * 3, ("x" * length + " ") * 3):
        assert _long_unbroken_token_floor(text) == _reference(text)


def test_empty_and_all_whitespace():
    assert _long_unbroken_token_floor("") == 0
    for text in (" " * 500, "\n\t 　" * 100, "\x1c\x1d\x1e\x1f\x85\xa0" * 50):
        assert _long_unbroken_token_floor(text) == _reference(text) == 0


def test_a_run_counts_every_character_once():
    assert _long_unbroken_token_floor("x" * 200) == 200
    assert _long_unbroken_token_floor("x" * 79 + " " + "y" * 80) == 80


def test_seeded_random_fuzz_agrees_with_the_loop():
    rng = random.Random(20261005)
    alphabet = _EDGE_CHARS + ["b"] * 40
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 400)))
        assert _long_unbroken_token_floor(text) == _reference(text), repr(text)
    # Long runs near the threshold, where the boundary lives.
    for _ in range(2000):
        parts = []
        for _ in range(rng.randint(1, 8)):
            parts.append("z" * rng.randint(N - 3, N + 3))
            parts.append(rng.choice(_EDGE_CHARS[:24]))
        text = "".join(parts)
        assert _long_unbroken_token_floor(text) == _reference(text), repr(text)
