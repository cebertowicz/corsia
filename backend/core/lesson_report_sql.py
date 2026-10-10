"""SQL helpers that keep lesson-report scans from stepping SQLite one Python row at a time.

Each SQLite row handed to Python costs a GIL handoff, which convoys behind any other
busy thread. The helpers aggregate ordered rows into JSON chunks inside SQLite, so
Python receives a few rows and decodes each chunk in one call.

Both helpers and the metadata predicate need SQLite 3.45.0: an ordered JSON aggregate keeps
an element's JSON subtype from that release, and ``json_valid(X, 2)`` exists from it.
``scripts/install.sh`` refuses an older SQLite, so nothing here probes the library.
"""
from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

from tools.catalog import CC_BUILTIN_ALIASES, MCP_CORSIA_PREFIX
from tools.tool_surface import TOOL_CALL

CHUNK_ROWS = 2000


def _chunk_query(inner_sql: str, fields: Sequence[str | tuple[str, str]], order_by: str, size: int) -> str:
    pairs = ",".join(
        f"'{field}',{field}" if isinstance(field, str) else f"'{field[0]}',{field[1]}" for field in fields
    )
    return (
        f"SELECT json_group_array(json_object({pairs}) ORDER BY rn) AS chunk FROM ("
        f"SELECT *, (ROW_NUMBER() OVER (ORDER BY {order_by}) - 1) AS rn FROM ({inner_sql})"
        f") GROUP BY rn / {int(size)} ORDER BY rn / {int(size)}"
    )


def json_row_chunk_lists(
    db, inner_sql: str, params: Sequence[Any], fields: Sequence[str | tuple[str, str]], order_by: str,
    *, size: int = CHUNK_ROWS,
) -> Iterator[list[dict[str, Any]]]:
    """Yield the rows of ``inner_sql`` as lists of dicts in ``order_by`` order, ``size`` rows per SQLite row.

    ``fields`` are inner column names, or ``(key, SQL expression)`` pairs over them, kept in
    that key order (an expression such as ``json('true')`` yields a JSON boolean). Values are
    text, integers, JSON literals or NULL.
    """
    for chunk in db.execute(_chunk_query(inner_sql, fields, order_by, size), tuple(params)):
        yield json.loads(chunk["chunk"])


def json_text_chunks(
    db, inner_sql: str, params: Sequence[Any], fields: Sequence[str | tuple[str, str]], order_by: str,
    *, size: int = CHUNK_ROWS,
) -> Iterator[str]:
    """Yield each chunk as SQLite's compact JSON array text, never decoded into Python objects."""
    for chunk in db.execute(_chunk_query(inner_sql, fields, order_by, size), tuple(params)):
        yield chunk["chunk"]


def json_row_chunks(db, *args: Any, **kwargs: Any) -> Iterator[dict[str, Any]]:
    """The rows of :func:`json_row_chunk_lists`, one dict at a time."""
    for rows in json_row_chunk_lists(db, *args, **kwargs):
        yield from rows


def _normalized_in(expr: str, names: Sequence[str]) -> str:
    """SQL for ``normalize_tool_name(expr) IN names``: strip the MCP prefix, then map aliases."""
    prefix = MCP_CORSIA_PREFIX
    bare = f"CASE WHEN substr({expr},1,{len(prefix)})='{prefix}' THEN substr({expr},{len(prefix) + 1}) ELSE {expr} END"
    spellings = set(names) | {alias for alias, target in CC_BUILTIN_ALIASES.items() if target in names}
    return f"({bare}) IN ({','.join(repr(spelling) for spelling in sorted(spellings))})"


def read_file_metadata_predicate(column: str) -> str:
    """JSON-aware superset of the rows whose metadata ``_read_call`` can resolve to read_file.

    Mirrors ``resolve_dispatch``: the recorded ``name`` normalizes to read_file, or it
    normalizes to ``tool_call`` and ``arguments`` is an object whose ``name`` normalizes to
    read_file and whose own ``arguments`` is an object. SQLite decodes JSON string escapes,
    so escaped spellings match; ``json_each`` visits duplicate keys, so a duplicate that
    Python's last-key-wins parse would use is seen too.

    The walk is guarded by the JSON5-aware ``json_valid(X,2)``, which never raises and stays a
    superset of what ``json.loads`` accepts (canonical JSON plus ``NaN`` and ``Infinity``).
    ``_read_call`` validates every surviving row.
    """
    walk = (
        f"json_type({column})='object' AND EXISTS (SELECT 1 FROM json_each({column}) n "
        f"WHERE n.key='name' AND n.type='text' AND ({_normalized_in('n.value', ('read_file',))} OR "
        f"({_normalized_in('n.value', (TOOL_CALL,))} AND EXISTS ("
        f"SELECT 1 FROM json_each({column}) a, json_each(a.value) t "
        f"WHERE a.key='arguments' AND a.type='object' AND t.key='name' AND t.type='text' AND "
        f"{_normalized_in('t.value', ('read_file',))} "
        f"AND EXISTS (SELECT 1 FROM json_each(a.value) u WHERE u.key='arguments' AND u.type='object')))))"
    )
    return f"(json_valid({column},2) AND {walk})"
