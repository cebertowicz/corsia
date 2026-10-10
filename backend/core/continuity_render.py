"""Compact deterministic text rendering for installed continuity snapshots.

The stored ``continuity_snapshot_json`` (see ``infra/checkpoints.py`` and
``core/continuity_types.py``) is never touched here. This module only changes
what the resuming model reads.

Five fields the model most needs at a glance -- ``active_constraints``,
``approvals_and_preferences``, ``blockers``, ``open_questions``,
``next_actions`` -- render in full: ``entry_id``, the primary text, every
non-empty secondary key, and ``source_refs``. Every other field renders one
compact index line per entry instead: ``#<entry_id> <primary text>``, cut to
160 characters with a trailing ``…`` when cut, and nothing else -- no
secondary keys, no sources. Its heading is marked ``## <field> (index)`` so
the model can tell at a glance which groups are index lines and which are
full entries -- a full entry with no non-empty secondary keys would
otherwise render identically to an index line. Whenever an index group
appears, a one-line hint near the top tells the model it can fetch any of
those entries' full bodies with the ``continuity_edit`` tool
(``operation: "inspect"``, that ``entry_id``); that tool already resolves
both live and mid-fold state, so the index costs the model nothing it
cannot recover on demand.

Field groups render in the canonical ``ENTRY_FIELDS`` order (matching the
``continuity_edit`` inspect listing in ``infra/continuity_staging.py::_grouped``),
so the layout a model edits against and the layout it resumes into agree.

Structure survives model-authored content because every rendered value
occupies exactly one physical line: a line break inside a body value is
escaped, never emitted -- for both the full secondary-key/sources lines and
an index line's cut primary text. Body text is untrusted here -- the model
writes it -- so each physical line is produced by exactly one of this
module's own prefixes (``## <field>``, ``## <field> (index)``,
``#<entry_id> ``, ``  <key>: ``, ``  sources: ``). Without that rule a
multi-line statement could orphan a line that reads as its own heading,
forges another entry's id, or -- being indistinguishable from a
continuation -- forges ``sources:``, ``rationale:`` or ``evidence:``
provenance the store never recorded.

The snapshot's shape is a contract, not a guess. Every byte in that column is
written by ``infra/continuity_store.py::active_entry_snapshot`` (through
``merge_staged_edits``/``freeze_continuity``), which always emits
``{entry_id: int, field: str, body: dict, revision: int}`` with a body each
write path already passed through ``validate_entry_body``. So this module
renders exactly that shape and asserts it, rather than carrying a branch per
shape the write path cannot produce.

A snapshot that does not match -- unparseable, not a list, or an entry off
the contract -- is a corrupt stored row, not a shape to interpret. It is
returned unchanged behind an explicit unreadable-snapshot marker, the same
reportable-fault posture as the ``continuity_unreadable`` flag the sibling
reader ``memory/checkpoint_search.py::_continuity`` returns to its callers on
the same column. Content is never dropped, and the reader is always told
which of the two it is looking at. ``core/checkpoint_render.py`` renders this
straight into the resume prefix, so degrading is the only safe answer there:
raising would cost the model its whole checkpoint body as well.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from core.continuity_types import ENTRY_FIELDS

# The delimiter the checkpoint renderer emits before these entries; delivery
# accounting finds the block by it, so it has this one home.
CONTINUITY_ENTRIES_HEADER = "[CORSIA continuity entries]\n"

_SOURCE_REFS_KEY = "source_refs"
_PRIMARY_KEYS = ("statement", "result")
_RELEVANT_STATE_KEYS = ("kind", "reference", "state")
_SEARCH_TEXT_KEYS = (
    "statement", "rationale", "state", "result", "evidence", "reference",
)
_UNREADABLE_MARKER = (
    "[CORSIA warning: continuity snapshot could not be read as entries; "
    "showing the raw stored snapshot below.]"
)

# Fields worth showing in full at every resume; everything else collapses to
# an index line the model can expand on demand via continuity_edit inspect.
_FULL_FIELDS = frozenset({
    "active_constraints",
    "approvals_and_preferences",
    "blockers",
    "open_questions",
    "next_actions",
})
_INDEX_TEXT_LIMIT = 160
_INDEX_ELLIPSIS = "…"
_INDEX_HEADING_SUFFIX = " (index)"
_FETCH_HINT = (
    '[Groups marked (index) show one line per entry. To read an entry in '
    'full, call continuity_edit with operation "inspect" and that entry_id.]'
)


class _MalformedSnapshot(ValueError):
    """A stored snapshot that is off the active_entry_snapshot contract."""


class ContinuitySnapshotUnreadable(ValueError):
    """A stored continuity snapshot cannot be trusted as addressable entries."""


@dataclass(frozen=True)
class ContinuitySnapshotEntry:
    """One validated frozen continuity entry shared by readers."""

    entry_id: int
    field: str
    body: dict
    revision: int | None

    @property
    def primary_text(self) -> str | None:
        """Return the canonical human-readable primary text for this entry."""
        text, _ = _primary_text(self.body)
        return text

    @property
    def search_text(self) -> str:
        """Return the entry's human words, with no JSON keys or escapes.

        The one text both the search indexer and its test oracle use. Source
        refs are provenance, not words, so they are deliberately left out.
        """
        return " ".join(
            value.strip()
            for key in _SEARCH_TEXT_KEYS
            if isinstance(value := self.body.get(key), str) and value.strip()
        )


def read_continuity_snapshot(snapshot_json: str | None) -> list[ContinuitySnapshotEntry]:
    """Decode the canonical frozen snapshot or raise a typed unreadable error."""
    try:
        parsed = json.loads(snapshot_json or "[]")
    except (TypeError, ValueError) as error:
        raise ContinuitySnapshotUnreadable("snapshot is not JSON") from error
    try:
        if not isinstance(parsed, list):
            raise _MalformedSnapshot("snapshot is not a list of entries")
        entries = []
        for raw in parsed:
            field, entry_id, body = _validate_entry(raw)
            revision = raw.get("revision")
            if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool)):
                raise _MalformedSnapshot("entry revision is not an integer")
            entries.append(ContinuitySnapshotEntry(entry_id, field, body, revision))
        return entries
    except _MalformedSnapshot as error:
        raise ContinuitySnapshotUnreadable(str(error)) from error


def render_continuity_entries(snapshot_json: str) -> str:
    """Render one installed continuity snapshot as compact grouped text.

    Entries are grouped by ``field``, headings ordered by the canonical
    ``ENTRY_FIELDS`` tuple with any other field after it in first-appearance
    order. The shared reader decides whether a snapshot matches the stored
    contract; otherwise rendering falls back to its raw bytes behind the
    unreadable marker.
    """
    try:
        entries = read_continuity_snapshot(snapshot_json)
    except ContinuitySnapshotUnreadable:
        return f"{_UNREADABLE_MARKER}\n{snapshot_json}"
    return _render_entries(entries)


def _render_entries(entries: list[ContinuitySnapshotEntry]) -> str:
    """Group canonical entries under their field heading in canonical order."""
    groups: dict[str, list[tuple[int, dict]]] = {}
    appearance_order: list[str] = []
    for entry in entries:
        field, entry_id, body = entry.field, entry.entry_id, entry.body
        if field not in groups:
            groups[field] = []
            appearance_order.append(field)
        groups[field].append((entry_id, body))

    known_fields = [field for field in ENTRY_FIELDS if field in groups]
    other_fields = [field for field in appearance_order if field not in ENTRY_FIELDS]
    ordered_fields = (*known_fields, *other_fields)

    rendered: list[str] = []
    if any(field not in _FULL_FIELDS for field in ordered_fields):
        rendered.append(_FETCH_HINT)
    for field in ordered_fields:
        full = field in _FULL_FIELDS
        suffix = "" if full else _INDEX_HEADING_SUFFIX
        rendered.append(f"## {field}{suffix}")
        for entry_id, body in groups[field]:
            if full:
                rendered.extend(_rendered_keys(entry_id, body))
            else:
                rendered.append(_index_line(entry_id, body))
    return "\n".join(rendered)


def _validate_entry(entry: Any) -> tuple[str, int, dict]:
    """Check one entry against the stored contract; return its parts."""
    if not isinstance(entry, dict):
        raise _MalformedSnapshot("entry is not an object")
    field = entry.get("field")
    if not isinstance(field, str) or not field:
        raise _MalformedSnapshot("entry has no field name")
    entry_id = entry.get("entry_id")
    if not isinstance(entry_id, int) or isinstance(entry_id, bool):
        # continuity_edit addresses entries by entry_id; a snapshot whose ids
        # cannot be resolved is not editable, so it is reported, not rendered.
        raise _MalformedSnapshot("entry has no addressable entry_id")
    body = entry.get("body")
    if not isinstance(body, dict):
        raise _MalformedSnapshot("entry has no object body")
    _validate_source_refs(body.get(_SOURCE_REFS_KEY))
    return field, entry_id, body


def _rendered_keys(entry_id: int, body: dict) -> list[str]:
    """Render one full entry's body keys under its ``#entry_id`` header."""
    lines: list[str] = []
    primary, consumed = _primary_text(body)
    lines.append(f"#{entry_id} {primary}" if primary is not None else f"#{entry_id}")
    for key, value in body.items():
        if key in consumed or key == _SOURCE_REFS_KEY or not _nonempty(value):
            continue
        lines.append(f"  {key}: {_scalar(value)}")
    refs_line = _source_refs_line(body.get(_SOURCE_REFS_KEY))
    if refs_line is not None:
        lines.append(refs_line)
    return lines


def _index_line(entry_id: int, body: dict) -> str:
    """Render one entry as a single addressable index line, no secondaries."""
    primary, _consumed = _primary_text(body)
    if primary is None:
        return f"#{entry_id}"
    return f"#{entry_id} {_cut(primary)}"


def _cut(text: str) -> str:
    """Cut index text at the character limit, marking truncation."""
    if len(text) <= _INDEX_TEXT_LIMIT:
        return text
    return text[:_INDEX_TEXT_LIMIT] + _INDEX_ELLIPSIS


def _primary_text(body: dict) -> tuple[str | None, set[str]]:
    """Pick the one-line summary: statement, result, or kind/reference/state."""
    for key in _PRIMARY_KEYS:
        if key in body:
            return _scalar(body[key]), {key}
    if all(key in body for key in _RELEVANT_STATE_KEYS):
        kind, reference, state = (body[key] for key in _RELEVANT_STATE_KEYS)
        text = f"{_scalar(kind)} {_scalar(reference)}: {_scalar(state)}"
        return text, set(_RELEVANT_STATE_KEYS)
    return None, set()


def _validate_source_refs(refs: Any) -> None:
    """Check source provenance once for every reader of a frozen entry."""
    if refs is None or (isinstance(refs, list) and not refs):
        return
    if not isinstance(refs, list):
        raise _MalformedSnapshot("source_refs is not a list")
    if any(not isinstance(ref, dict) for ref in refs):
        raise _MalformedSnapshot("source_ref is not an object")


def _source_refs_line(refs: Any) -> str | None:
    """Render already-validated source_refs on one line, or omit it when empty."""
    if refs is None or not refs:
        return None
    return "  sources: " + "; ".join(_source_ref_text(ref) for ref in refs)


def _source_ref_text(ref: dict) -> str:
    """Render one already-validated source reference as ``kind:reference``."""
    return f"{_scalar(ref.get('kind', ''))}:{_scalar(ref.get('reference', ''))}"


def _nonempty(value: Any) -> bool:
    """Whether a secondary body value is worth rendering its own line."""
    if value is None:
        return False
    if isinstance(value, (str, list, dict, tuple)):
        return len(value) > 0
    return True


def _scalar(value: Any) -> str:
    """Render one body value as exactly one physical line of text.

    Every line break inside a model-authored value is escaped: a value that
    can start its own line can forge this module's structure, from a
    ``## field`` heading down to a ``  sources: `` provenance line.
    """
    text = value if isinstance(value, str) else _compact_json(value)
    return text.replace("\n", "\\n").replace("\r", "\\r")


def _compact_json(value: Any) -> str:
    """Deterministic compact JSON for values with no dedicated rendering."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
