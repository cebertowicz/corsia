"""Tests for scrub-rule normalization (check_docs_drift.scrub)."""

from __future__ import annotations

from check_docs_drift import scrub

_PERSON_RE = scrub.build_person_pattern(["Alice"])


def test_real_name_token_is_case_insensitive_and_matches_inside_identifiers():
    """`ask_alice` (kit) maps from a workspace `ask_alice`-shaped mention, no boundary."""
    text = "ask_alice approved this; ALICE said so too."

    assert scrub.normalize_text(text, _PERSON_RE) == (
        "ask_<PERSON> approved this; <PERSON> said so too."
    )


def test_home_path_is_replaced():
    """Only the segment after `/Users/` is scrubbed; the regex is bounded at the next `/`."""
    path_text = "See `/Users/alice/corsia/projects/corsia/README.md` for details."

    assert scrub.normalize_text(path_text, _PERSON_RE) == (
        "See `<HOME>/corsia/projects/corsia/README.md` for details."
    )


def test_generic_kit_phrases_all_map_to_the_same_token():
    """`the owner`, `the maintainers`, and `alice` are the kit's three scrub forms."""
    text = "the owner approved; the maintainers agreed; alice merged it."

    assert scrub.normalize_text(text, _PERSON_RE) == (
        "<PERSON> approved; <PERSON> agreed; <PERSON> merged it."
    )


def test_a_generic_phrase_present_on_both_sides_normalizes_on_both_sides():
    """The earlier asymmetric-regex bug: a phrase both sides share must not read as drift."""
    workspace_text = "Bring this to the owner before merging."
    kit_text = "Bring this to the owner before merging."

    assert scrub.normalize_text(workspace_text, _PERSON_RE) == scrub.normalize_text(
        kit_text, _PERSON_RE
    )


def test_kit_and_workspace_normalize_to_the_same_text_when_only_scrubbed():
    """A file that differs only by scrub substitution reports as clean, not drift."""
    workspace_text = "Alice owns this repo. See `/Users/alice/corsia/x.md`."
    kit_text = "the owner owns this repo. See `/Users/alice/corsia/x.md`."

    assert scrub.normalize_text(workspace_text, _PERSON_RE) == scrub.normalize_text(
        kit_text, _PERSON_RE
    )


def test_real_content_rewrite_still_differs_after_normalization():
    """Genuine prose drift must survive scrubbing so it is still reported."""
    workspace_text = "Alice reviews every PR before merge."
    kit_text = "the owner reviews every PR before it ships."

    assert scrub.normalize_text(workspace_text, _PERSON_RE) != scrub.normalize_text(
        kit_text, _PERSON_RE
    )


def test_build_person_pattern_with_no_real_name_tokens_is_still_generic_only():
    person_re = scrub.build_person_pattern([])

    assert scrub.normalize_text("the owner said so", person_re) == "<PERSON> said so"


def test_build_person_pattern_covers_multiple_name_tokens():
    """A multi-word configured name scrubs every token, e.g. first and last name."""
    person_re = scrub.build_person_pattern(["Alice", "Anderson"])

    assert (
        scrub.normalize_text("Alice Anderson wrote this.", person_re)
        == "<PERSON> <PERSON> wrote this."
    )
