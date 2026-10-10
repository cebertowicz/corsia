"""The System section key in one stored JSON document (schema 78 -> 79, item 1).

Pure text functions, no database. ``migration_78_to_79_stored_names`` owns the
tables, the transaction and the refusal; this module answers, for one stored
JSON text, what converts, what refuses, and what the converted text is.

Only two owners hold the key, and only at the top level of their entries:

- ``$._corsia_installed_context_sources[*]``
- ``$.context_usage.context_snapshot.source_inventory[*]``

Inside those entries an exact former-name string at a section key becomes the
new name, and a System source id moves with it. The same name anywhere else is
content (a project name, a path, tool output) and is kept. A section key that
holds the former name outside the two owners is an owner nobody classified, so
it refuses rather than being guessed at.

The rewrite replaces token spans in the stored text; every other byte is kept.
It is then checked by a method that shares nothing with the span walk: the new
text is parsed and compared with the parsed original converted in memory.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# The former product name, spelled here and nowhere else in this module.
OLD_NAME = "maestro"
NEW_NAME = "corsia"

SECTION_KEYS = ("origin_section", "component_id", "layer")
INSTALLED_SOURCES = "_corsia_installed_context_sources"
USAGE_SNAPSHOT_PATH = ("context_usage", "context_snapshot", "source_inventory")

# The tokens a row must carry for anything here to apply. A stored document is
# written by ``json.dumps``, which never escapes an ASCII letter, so the name
# as an exact string value, or leading a source id, appears in these forms.
EXACT_TOKEN = f'"{OLD_NAME}"'
SOURCE_ID_TOKEN = f'"{OLD_NAME}:'
NATIVE_ID_TOKEN = f'"native:system:{OLD_NAME}'

_NATIVE_PREFIX = "native:system:"
_OLD_NATIVE_ID = _NATIVE_PREFIX + OLD_NAME
# The two id forms the System tier wrote under the former name: a packaged file
# (``<name>:v1:<path>``) and the manifest-drift row.
_OLD_VERSIONED_ID = re.compile(rf"{OLD_NAME}:v[0-9]+:.")
_OLD_ERROR_ID = f"{OLD_NAME}:error:manifest-mismatch"
_WHITESPACE = re.compile(r"[ \t\n\r]*")
_DECODER = json.JSONDecoder()
# What stands just before and after the name when it is a whole section value or
# native id, and when it leads a System source id. Matched against the last
# bytes before the name only; a longer run of whitespace is left to the walk.
_SEPARATOR = r'"[ \t\n\r]*:[ \t\n\r]*"'
_BEFORE_WHOLE = re.compile(
    rf'"(?:(?:{"|".join(SECTION_KEYS)}){_SEPARATOR}|source_id{_SEPARATOR}{_NATIVE_PREFIX})\Z'
)
_BEFORE_LEADING = re.compile(rf'"source_id{_SEPARATOR}\Z')
_AFTER_LEADING = re.compile(r':v[0-9]+:[^"]|:error:manifest-mismatch"')
_BEFORE_BYTES = 64


class SectionKeyRefusal(ValueError):
    """One stored document cannot be converted without guessing."""


@dataclass(frozen=True)
class DocumentResult:
    """What one document converts to. ``text`` is ``None`` when nothing changes."""

    text: str | None
    section_values: int
    source_ids: int


def _converted_source_id(value: str) -> str | None:
    """The new form of a System source id, ``None`` when it is not one."""
    if value == _OLD_NATIVE_ID:
        return _NATIVE_PREFIX + NEW_NAME
    if value == _OLD_ERROR_ID or _OLD_VERSIONED_ID.match(value):
        return NEW_NAME + value[len(OLD_NAME):]
    if value.startswith((OLD_NAME + ":", _OLD_NATIVE_ID)):
        raise SectionKeyRefusal("a source id carries the former name in an unknown form")
    return None


class _SectionCensus:
    """Counts every section key that holds the former name, at any depth."""

    def __init__(self) -> None:
        self.exact = 0
        self.not_a_string = 0

    def __call__(self, value: dict[str, Any]) -> dict[str, Any]:
        for key in SECTION_KEYS:
            held = value.get(key)
            if held == OLD_NAME:
                self.exact += 1
            elif isinstance(held, (dict, list)) and EXACT_TOKEN in json.dumps(held):
                self.not_a_string += 1
        return value


def _parse(text: str, census: _SectionCensus | None = None) -> Any:
    # NaN never equals itself, so constants parse to their names: the two
    # parses of one row then compare equal.
    return json.loads(text, object_hook=census, parse_constant=str)


def _checked_parse(text: Any, census: _SectionCensus) -> Any:
    if not isinstance(text, str):
        raise SectionKeyRefusal("the value is not text")
    try:
        return _parse(text, census)
    except (ValueError, RecursionError) as error:
        raise SectionKeyRefusal("the value is not valid JSON") from error


def _owner_entries(document: Any) -> list[dict[str, Any]]:
    """Every entry of the two owners.

    An owner that is absent or null is normal and skipped, and so is a usage
    record with no snapshot to hold one. An owner that is present but is not a
    list of objects refuses.
    """
    if not isinstance(document, dict):
        return []
    owners: list[tuple[str, Any]] = [(INSTALLED_SOURCES, document.get(INSTALLED_SOURCES))]
    node: Any = document
    for key in USAGE_SNAPSHOT_PATH[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
    if isinstance(node, dict):
        owners.append((".".join(USAGE_SNAPSHOT_PATH), node.get(USAGE_SNAPSHOT_PATH[-1])))
    entries: list[dict[str, Any]] = []
    for name, owner in owners:
        if owner is None:
            continue
        if not isinstance(owner, list):
            raise SectionKeyRefusal(f"{name} is not a list")
        for entry in owner:
            if not isinstance(entry, dict):
                raise SectionKeyRefusal(f"{name} holds an entry that is not an object")
            entries.append(entry)
    return entries


def _convert_entries(entries: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Convert the parsed entries in place: (section values, source ids, already new)."""
    sections = source_ids = already_new = 0
    for entry in entries:
        for key in SECTION_KEYS:
            if entry.get(key) == OLD_NAME:
                entry[key] = NEW_NAME
                sections += 1
            elif entry.get(key) == NEW_NAME:
                already_new += 1
        source_id = entry.get("source_id")
        if isinstance(source_id, str) and OLD_NAME in source_id:
            converted = _converted_source_id(source_id)
            if converted is not None:
                entry["source_id"] = converted
                source_ids += 1
    return sections, source_ids, already_new


def _skip(text: str, index: int) -> int:
    match = _WHITESPACE.match(text, index)
    assert match is not None
    return match.end()


def _object_spans(text: str, index: int) -> list[tuple[str, int, int]]:
    """(key, value start, value end) for each member of the object at ``index``."""
    if text[index:index + 1] != "{":
        raise SectionKeyRefusal("an owner entry is not an object in the stored text")
    spans: list[tuple[str, int, int]] = []
    cursor = _skip(text, index + 1)
    if text[cursor:cursor + 1] == "}":
        return spans
    while True:
        if text[cursor:cursor + 1] != '"':
            raise SectionKeyRefusal("an object key is not a string in the stored text")
        key, key_end = _DECODER.raw_decode(text, cursor)
        value_start = _skip(text, _skip(text, key_end) + 1)
        _value, value_end = _DECODER.raw_decode(text, value_start)
        spans.append((key, value_start, value_end))
        cursor = _skip(text, value_end)
        if text[cursor:cursor + 1] != ",":
            return spans
        cursor = _skip(text, cursor + 1)


def _array_spans(text: str, index: int) -> list[int]:
    """The start of each element of the array at ``index``."""
    if text[index:index + 1] != "[":
        raise SectionKeyRefusal("an owner is not a list in the stored text")
    starts: list[int] = []
    cursor = _skip(text, index + 1)
    if text[cursor:cursor + 1] == "]":
        return starts
    while True:
        _value, end = _DECODER.raw_decode(text, cursor)
        starts.append(cursor)
        cursor = _skip(text, end)
        if text[cursor:cursor + 1] != ",":
            return starts
        cursor = _skip(text, cursor + 1)


def _owner_starts(text: str) -> list[int]:
    """Where each owner's list starts in the stored text (a repeated key yields each)."""
    starts: list[int] = []
    for key, start, _end in _object_spans(text, _skip(text, 0)):
        if key == INSTALLED_SOURCES and text[start] == "[":
            starts.append(start)
        elif key == USAGE_SNAPSHOT_PATH[0]:
            starts.extend(_nested_starts(text, start, USAGE_SNAPSHOT_PATH[1:]))
    return starts


def _nested_starts(text: str, index: int, path: tuple[str, ...]) -> list[int]:
    if text[index:index + 1] != ("{" if path else "["):
        return []
    if not path:
        return [index]
    return [
        found
        for key, start, _end in _object_spans(text, index)
        if key == path[0]
        for found in _nested_starts(text, start, path[1:])
    ]


def _entry_edits(text: str, index: int) -> list[tuple[int, int, str]]:
    """Token replacements for one owner entry: (start, end, new text)."""
    edits: list[tuple[int, int, str]] = []
    for key, start, end in _object_spans(text, index):
        if text[start] != '"':
            continue
        value, _end = _DECODER.raw_decode(text, start)
        if key in SECTION_KEYS and value == OLD_NAME:
            edits.append((start, end, json.dumps(NEW_NAME)))
        elif key == "source_id" and _converted_source_id(value) is not None:
            # Only the name moves; the rest of the id keeps its stored bytes.
            at = start + 1 + value.index(OLD_NAME)
            if text[at:at + len(OLD_NAME)] != OLD_NAME:
                raise SectionKeyRefusal("a source id stores the former name escaped")
            edits.append((at, at + len(OLD_NAME), NEW_NAME))
    return edits


def _walked(text: str) -> str:
    """The converted text, found by walking to each owner entry's own members."""
    edits = [
        edit
        for owner in _owner_starts(text)
        for entry in _array_spans(text, owner)
        for edit in _entry_edits(text, entry)
    ]
    parts: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(edits):
        parts.extend((text[cursor:start], replacement))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _replaced_in_place(text: str) -> str:
    """The text with the name replaced wherever it stands as one of the tokens."""
    pieces = text.split(OLD_NAME)
    parts = [pieces[0]]
    for before, after in zip(pieces, pieces[1:]):
        tail = before[-_BEFORE_BYTES:]
        if after.startswith('"'):
            is_token = _BEFORE_WHOLE.search(tail) is not None
        else:
            is_token = (
                _AFTER_LEADING.match(after) is not None
                and _BEFORE_LEADING.search(tail) is not None
            )
        parts.extend((NEW_NAME if is_token else OLD_NAME, after))
    return "".join(parts)


def _rewritten(text: str, converted: Any) -> str:
    """The stored text with only the name tokens replaced, checked by a parse.

    Replacing the tokens where they stand is right for a row whose only such
    tokens are in the owners, which is nearly every row, and takes a fraction
    of the walk's time. A row that also carries them as content (a tool call
    that quotes a source id) parses to something else, and is walked instead.
    """
    in_place = _replaced_in_place(text)
    if _parse(in_place) == converted:
        return in_place
    walked = _walked(text)
    if _parse(walked) != converted:
        raise SectionKeyRefusal("the rewritten text does not parse to the converted document")
    return walked


def convert_document(text: Any) -> DocumentResult:
    """Convert one ``messages.metadata`` text, or raise ``SectionKeyRefusal``."""
    census = _SectionCensus()
    document = _checked_parse(text, census)
    if census.not_a_string:
        raise SectionKeyRefusal("a section key holds the former name inside a non-string value")
    entries = _owner_entries(document)
    in_owners = sum(
        1 for entry in entries for key in SECTION_KEYS if entry.get(key) == OLD_NAME
    )
    if census.exact != in_owners:
        raise SectionKeyRefusal(
            f"{census.exact - in_owners} section key(s) hold the former name outside the known owners"
        )
    sections, source_ids, already_new = _convert_entries(entries)
    if sections and already_new:
        raise SectionKeyRefusal("one row holds both the former and the new section key")
    if not sections and not source_ids:
        return DocumentResult(None, 0, 0)
    return DocumentResult(_rewritten(text, document), sections, source_ids)


def refuse_section_key(text: Any) -> None:
    """Refuse a document in a column that has no owner of the section key."""
    census = _SectionCensus()
    _checked_parse(text, census)
    if census.exact or census.not_a_string:
        raise SectionKeyRefusal("a section key holds the former name; this column has no known owner")
