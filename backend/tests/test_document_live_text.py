"""Real CRDT text operations preserve item identities and Unicode positions."""
from __future__ import annotations

from pycrdt import Doc, Text


def document(text=""):
    doc = Doc()
    doc["text"] = Text(text)
    return doc


def test_agent_diff_preserves_concurrent_user_text():
    from doceditor.live_text import apply_text, clone

    base = document("hello world")
    browser = clone(base.get_update())
    browser["text"].insert(0, "USER ")
    agent = clone(base.get_update())
    before = agent.get_state()
    apply_text(agent, "hello there")
    browser.apply_update(agent.get_update(before))
    assert str(browser["text"]) == "USER hello there"


def test_unicode_diff_uses_pycrdt_byte_offsets():
    from doceditor.live_text import apply_text

    doc = document("aé🙂z")
    apply_text(doc, "aé🙃Z")
    assert str(doc["text"]) == "aé🙃Z"
    apply_text(doc, "é🙃")
    assert str(doc["text"]) == "é🙃"


def test_utf16_range_maps_remote_unicode_delta():
    from doceditor.live_text import clone, map_offset

    doc = document("é hello 🙂 world")
    before = str(doc["text"])
    deltas = []
    text = doc["text"]
    subscription = text.observe(lambda event: deltas.append(event.delta))
    other = clone(doc.get_update())
    other["text"].insert(0, "🙂 ")
    doc.apply_update(other.get_update(doc.get_state()))
    assert map_offset(2, before, deltas[0], right=False) == 5
    assert map_offset(7, before, deltas[0], right=True) == 10
    text.unobserve(subscription)


def test_deletion_delta_collapses_without_quote_guess():
    from doceditor.live_text import apply_text, map_offset

    doc = document("one quote two")
    deltas = []
    text = doc["text"]
    subscription = text.observe(lambda event: deltas.append(event.delta))
    apply_text(doc, "one  two")
    assert map_offset(4, "one quote two", deltas[0], right=False) == 4
    assert map_offset(9, "one quote two", deltas[0], right=True) == 4
    text.unobserve(subscription)


def test_exclusive_boundaries_and_single_point_association():
    from doceditor.live_text import map_offset

    assert map_offset(1, "abc", [{"retain": 1}, {"insert": "🙂"}], right=False) == 3
    assert map_offset(2, "abc", [{"retain": 2}, {"insert": "🙂"}], right=True) == 2
    assert map_offset(1, "abc", [{"retain": 1}, {"insert": "🙂"}], right=True) == 1


def test_utf16_boundary_conversion_and_digest_format():
    from doceditor.live_text import byte_offset, text_digest
    import hashlib
    import pytest

    assert byte_offset("é🙂z", 1) == 2
    assert byte_offset("é🙂z", 3) == 6
    with pytest.raises(ValueError):
        byte_offset("é🙂z", 2)
    assert text_digest("é🙂z") == hashlib.sha256("é🙂z".encode()).hexdigest()[:16]


def test_multi_operation_delta_maps_skin_tone_and_combining_marks():
    from doceditor.live_text import map_offset

    before = "é👍🏽a\u0301QUOTE中"
    delta = [{"retain": 2}, {"delete": 8}, {"insert": "🙂 "}, {"retain": 3}, {"insert": "X"}]
    assert map_offset(7, before, delta, right=False) == 7
    assert map_offset(12, before, delta, right=True) == 12
