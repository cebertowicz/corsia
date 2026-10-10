"""A streamed tool input decodes the same however the provider splits it."""
import json

import pytest

from doceditor.live_preview_args import ArgScanner


def scan(raw: str, size: int) -> ArgScanner:
    scanner = ArgScanner()
    for start in range(0, len(raw), size):
        scanner.feed(raw[start:start + size])
    return scanner


@pytest.mark.parametrize("size", [1, 2, 3, 5, 64])
def test_escapes_split_anywhere_decode_exactly(size):
    text = 'line "one"\n\ttab \\ slash / café \U0001F600 end'
    raw = json.dumps({"path": "a.md", "old_string": "x", "new_string": text})  # ASCII-escaped, with a surrogate pair
    scanner = scan(raw, size)
    assert not scanner.failed
    assert scanner.fields["new_string"].text() == text
    assert scanner.fields["new_string"].complete


def test_a_surrogate_pair_split_between_fragments_is_one_character():
    scanner = ArgScanner()
    scanner.feed('{"content": "a\\ud83d')
    assert scanner.fields["content"].text() == "a"
    scanner.feed('\\ude00b"}')
    assert scanner.fields["content"].text() == "a\U0001F600b"


def test_a_value_is_incomplete_until_its_closing_quote():
    scanner = ArgScanner()
    scanner.feed('{"old_string": "hel')
    assert scanner.fields["old_string"].text() == "hel"
    assert not scanner.fields["old_string"].complete
    scanner.feed('lo", ')
    assert scanner.fields["old_string"].complete


def test_multi_edit_entries_and_flags_decode_in_any_key_order():
    raw = json.dumps({"edits": [{"new_string": "B", "old_string": "a"}, {"old_string": "c", "new_string": "D"}],
                      "path": "x.md", "replace_all": False})
    scanner = scan(raw, 4)
    assert [(e["old_string"].text(), e["new_string"].text()) for e in scanner.edits] == [("a", "B"), ("c", "D")]
    assert scanner.flags == {"replace_all": False}
    assert scanner.fields["path"].text() == "x.md"


def test_unbalanced_input_fails_instead_of_guessing():
    scanner = ArgScanner()
    scanner.feed('{"path": "a"}}')
    assert scanner.failed


def test_a_malformed_unicode_escape_fails_the_scan_instead_of_raising():
    scanner = ArgScanner()
    scanner.feed('{"new_string": "a\\uZZ')
    scanner.feed('ZZ b"}')
    assert scanner.failed
