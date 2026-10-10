
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from doceditor.quote_anchor import (  # noqa: E402
    AmbiguousQuote,
    QuoteNotFound,
    anchor_context,
    reanchor,
    resolve_quote,
)

TEXT = (
    "# Spec\n"                                  # line 1
    "\n"                                         # line 2
    "The router decides where an event goes.\n"  # line 3
    "\n"                                         # line 4
    "The router decides where an event goes.\n"  # line 5 (duplicate)
    "Tail line with a marker token here.\n"      # line 6
)


def test_unique_match_returns_exact_offsets():
    quote = "Tail line with a marker"
    frm, to = resolve_quote(TEXT, quote)
    assert TEXT[frm:to] == quote


def test_unique_match_is_the_only_occurrence():
    quote = "marker token"
    frm, to = resolve_quote(TEXT, quote)
    assert TEXT[frm:to] == quote
    assert frm == TEXT.index(quote)


def test_missing_quote_raises_quote_not_found():
    with pytest.raises(QuoteNotFound) as exc:
        resolve_quote(TEXT, "this span does not exist anywhere")
    assert isinstance(exc.value.candidate_lines, list)


def test_ambiguous_quote_without_near_raises_with_candidate_lines():
    quote = "The router decides where an event goes."
    with pytest.raises(AmbiguousQuote) as exc:
        resolve_quote(TEXT, quote)
    assert exc.value.candidate_lines == [3, 5]


def test_near_disambiguates_to_the_closest_occurrence():
    quote = "The router decides where an event goes."
    frm, to = resolve_quote(TEXT, quote, near="marker token")
    assert TEXT[frm:to] == quote
    second = TEXT.index(quote, TEXT.index(quote) + 1)
    assert frm == second


def test_near_disambiguates_to_the_first_occurrence():
    quote = "The router decides where an event goes."
    frm, to = resolve_quote(TEXT, quote, near="# Spec")
    first = TEXT.index(quote)
    assert frm == first


def test_near_equidistant_stays_ambiguous():
    text = "zzxxxNxxxxzz"  # "zz" at 0 and 10; "N" at 5 — equidistant (5 each)
    with pytest.raises(AmbiguousQuote):
        resolve_quote(text, "zz", near="N")


def test_near_not_present_in_doc_stays_ambiguous():
    quote = "The router decides where an event goes."
    with pytest.raises(AmbiguousQuote):
        resolve_quote(TEXT, quote, near="nonexistent hint")


def test_empty_quote_is_a_loud_error():
    with pytest.raises(ValueError):
        resolve_quote(TEXT, "")


def test_offsets_are_utf16_code_units_for_astral_chars():
    text = "😀😀 target span here\n"  # each 😀 is 2 UTF-16 units
    frm, to = resolve_quote(text, "target span")
    assert frm == 5  # 2 emoji × 2 units + 1 space
    assert to == 5 + len("target span")


def test_self_overlapping_span_is_a_single_match_not_ambiguous():
    text = "prefix aaa suffix\n"
    frm, to = resolve_quote(text, "aa")
    assert text[frm:to] == "aa"
    assert frm == text.index("aa")


@pytest.mark.parametrize("text, expected", [
    ("new before target after", (11, 17)),
    ("before tarXget after", None),
    ("before target new after", (7, 13)),
    ("before target after new", (7, 13)),
    ("before NEWtarget after", (10, 16)),
    ("before targetafter", (7, 13)),
])
def test_reanchor_tracks_surviving_quote_or_orphans(text, expected):
    original = "before target after"
    frm, to = resolve_quote(original, "target")
    assert reanchor(
        text, "target", from16=frm, to16=to, prefix="before ", suffix=" after"
    ) == expected


def test_reanchor_repeated_quote_uses_matching_context():
    original = "left target first / right target second"
    frm, to = resolve_quote(original, "target", near="second")
    changed = "new left target first / right target second"
    assert reanchor(
        changed, "target", from16=frm, to16=to, prefix="right ", suffix=" second"
    ) == (30, 36)


def test_reanchor_never_jumps_when_its_repeated_quote_is_deleted():
    original = "left target first / right target second"
    frm, to = resolve_quote(original, "target", near="second")
    changed = "left target first / right missing second"
    assert reanchor(
        changed, "target", from16=frm, to16=to, prefix="right ", suffix=" second"
    ) is None


def test_reanchor_preserves_utf16_offsets_after_astral_text():
    original = "😀 before target after"
    frm, to = resolve_quote(original, "target")
    changed = "😀😀 new before target after"
    assert reanchor(
        changed, "target", from16=frm, to16=to, prefix="😀 before ", suffix=" after"
    ) == (16, 22)


def test_contextless_reanchor_stays_only_at_its_stored_span():
    assert reanchor(
        "target xx target", "target", from16=10, to16=16, prefix=None, suffix=None
    ) == (10, 16)
    # A unique survivor may be a different occurrence: the commented one was
    # deleted and nothing stored can tell them apart (#3101).
    assert reanchor(
        "new target", "target", from16=0, to16=6, prefix=None, suffix=None
    ) is None
    assert reanchor(
        "target xx target", "target", from16=5, to16=11, prefix=None, suffix=None
    ) is None


def test_anchor_context_reads_utf16_spans_and_rejects_invalid_ones():
    text = "😀 before target after"
    assert anchor_context(text, 10, 16, "target") == ("😀 before ", " after")
    assert anchor_context(text, 1, 3, None) is None  # splits the emoji's surrogate pair
    assert anchor_context(text, 10, 99, None) is None  # past the end
    assert anchor_context(text, None, 16, "target") is None  # malformed client input
    assert anchor_context(text, "10", 16, "target") is None
    assert anchor_context(text, 11, 17, "target") is None  # slice is not the quote


def test_read_orphans_a_contextless_comment_whose_quote_was_deleted(tmp_path, monkeypatch):
    from doceditor import comment_anchoring, comments as store

    doc = tmp_path / "doc.md"
    rows = [
        {"id": "kept", "parent_id": None, "review": 0, "anchor_text": "target",
         "from_off": 5, "to_off": 11, "anchor_prefix": None, "anchor_suffix": None},
        {"id": "gone", "parent_id": None, "review": 0, "anchor_text": "target",
         "from_off": 25, "to_off": 31, "anchor_prefix": None, "anchor_suffix": None},
    ]
    persisted: list[dict] = []
    monkeypatch.setattr(store, "list_for_artifact", lambda *_a, **_k: rows)
    monkeypatch.setattr(store, "update_positions", lambda _a, updates, **_k: persisted.extend(updates))
    doc.write_text("keep target here / other there")  # the second occurrence is deleted

    threads, anchored = comment_anchoring.anchored_threads("artifact", str(doc), None)

    assert anchored
    kept, gone = threads
    assert kept["orphaned"] is False and (kept["from_off"], kept["to_off"]) == (5, 11)
    assert gone["orphaned"] is True and (gone["from_off"], gone["to_off"]) == (25, 31)
    assert persisted == [{"id": "kept", "from_off": 5, "to_off": 11,
                          "anchor_prefix": "keep ", "anchor_suffix": " here / other there"}]
