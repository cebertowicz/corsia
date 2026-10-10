"""Generic, schema-driven row generation for tables the fixture does not model by hand.

The fixture is built through the real schema, so a table added or widened by a
later change must not break it. Columns the generator does not know get a
value its own definition accepts (an allowed enumeration member, valid JSON, a
unique non-empty string, a counter); a table whose constraints still reject
the generated rows is reported as unseeded instead of failing the build.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .sqlite_pin import sqlite3
from . import clock

_ITEM = r"(?:'[^']*'|-?\d+)"
_IN_LIST = re.compile(rf"(\w+)\s+IN\s*\(\s*({_ITEM}(?:\s*,\s*{_ITEM})*)\s*\)", re.IGNORECASE)
_IN_ITEM = re.compile(r"'([^']*)'|(-?\d+)")
_JSON_OBJECT = re.compile(r"json_type\((\w+)\)\s*=\s*'object'", re.IGNORECASE)
_JSON_ARRAY = re.compile(r"json_type\((\w+)\)\s*=\s*'array'", re.IGNORECASE)
_JSON_ANY = re.compile(r"json_valid\((\w+)\)", re.IGNORECASE)
_LENGTH_64 = re.compile(r"length\((\w+)\)\s*=\s*64", re.IGNORECASE)

WINDOW_DAYS = 90
END = clock.FROZEN_NOW.replace(tzinfo=None)
START = END - timedelta(days=WINDOW_DAYS)


def timestamp(rng: random.Random, after: datetime | None = None) -> str:
    """A naive ISO timestamp inside the fixture window, never after the frozen now."""
    low = after or START
    span = max(1, int((END - low).total_seconds()))
    return (low + timedelta(seconds=rng.randrange(span))).isoformat()


@dataclass
class Pools:
    """Identifier pools generic columns draw their references from."""

    rng: random.Random
    ids: dict[str, list[str]] = field(default_factory=dict)
    cumulative: dict[str, list[float]] = field(default_factory=dict)

    def add(self, name: str, values: list[str], weights: list[float] | None = None) -> None:
        self.ids[name] = values
        if weights:
            total, running = 0.0, []
            for weight in weights:
                total += weight
                running.append(total)
            self.cumulative[name] = running

    def draw(self, name: str) -> str:
        values = self.ids[name]
        running = self.cumulative.get(name)
        if not running:
            return values[self.rng.randrange(len(values))]
        point = self.rng.random() * running[-1]
        low, high = 0, len(values) - 1
        while low < high:
            mid = (low + high) // 2
            if running[mid] < point:
                low = mid + 1
            else:
                high = mid
        return values[low]


#: Column name (or "_suffix") -> pool its values reference.
_POOL_BY_COLUMN = (
    ("thread_id", "thread"), ("_thread_id", "thread"), ("plan_node_id", "node"),
    ("node_id", "node"), ("plan_id", "plan"), ("artifact_id", "artifact"),
    ("attempt_id", "attempt"), ("project_id", "project"),
    ("vendor_account_id", "vendor_account"), ("account_id", "vendor_account"),
    ("_account_id", "vendor_account"),
)


def pool_for(column: str, pools: Pools) -> str | None:
    for key, pool in _POOL_BY_COLUMN:
        matches = column.endswith(key) if key.startswith("_") else column == key
        if matches and pool in pools.ids:
            return pool
    return None


@dataclass
class Column:
    name: str
    sql_type: str
    required: bool
    default: str | None
    rowid_alias: bool
    allowed: list
    json_kind: str | None
    length_64: bool


def _allowed(ddl: str) -> dict[str, list]:
    found = {}
    for match in _IN_LIST.finditer(ddl):
        found[match.group(1)] = [text if num == "" else int(num)
                                 for text, num in _IN_ITEM.findall(match.group(2))]
    return found


def read_columns(db: sqlite3.Connection, table: str) -> list[Column]:
    """Insertable columns of ``table`` with the constraints readable from its DDL."""
    ddl = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    allowed = _allowed(ddl)
    json_kinds = {m.group(1): "any" for m in _JSON_ANY.finditer(ddl)}
    json_kinds.update({m.group(1): "object" for m in _JSON_OBJECT.finditer(ddl)})
    json_kinds.update({m.group(1): "array" for m in _JSON_ARRAY.finditer(ddl)})
    long_text = {m.group(1) for m in _LENGTH_64.finditer(ddl)}
    info = [row for row in db.execute(f"PRAGMA table_xinfo({table})") if not row[6]]
    keys = [row for row in info if row[5]]
    columns = []
    for _cid, name, sql_type, notnull, default, pk, _hidden in info:
        kind = (sql_type or "").upper()
        alias = pk == 1 and len(keys) == 1 and kind == "INTEGER"
        # SQLite lets a TEXT primary key be NULL; generate one regardless.
        required = bool(notnull) or (bool(pk) and not alias)
        columns.append(Column(name, kind, required, default, alias, allowed.get(name, []),
                              json_kinds.get(name), name in long_text))
    return columns


def _default_value(column: Column, table: str, index: int, rng: random.Random, pools: Pools):
    has_default = column.default is not None and column.default != ""
    if column.allowed:
        if not column.required and not has_default:
            return None  # an optional enumeration stays unset
        default = (column.default or "").strip("'")
        return default if default in column.allowed else column.allowed[0]
    if column.json_kind:
        return "[]" if column.json_kind == "array" else "{}"
    if column.length_64:
        return f"{index:064x}"
    pool = pool_for(column.name, pools)
    if pool:
        return pools.draw(pool) if column.required else None
    if column.name == "user_id":
        return "steve"
    if column.rowid_alias or has_default or not column.required:
        return None  # the database assigns it, defaults it, or it stays NULL
    if column.name.endswith("_at"):
        return timestamp(rng)
    if any(kind in column.sql_type for kind in ("INT", "REAL", "NUM")):
        return index + 1
    return f"{table}.{column.name}-{index:07d}"


Override = Callable[[int, random.Random, Pools], object]


def _unique_domains(db: sqlite3.Connection, table: str, columns: list[Column],
                    pools: Pools) -> list[list[tuple[str, list]]]:
    """For each full unique index, its columns and their finite value domains.

    Rows take the combinations of those domains in order, so a unique index on
    pool or enumeration columns is never violated. An index containing a
    column that is unique per row anyway (free text, a counter) needs nothing.
    """
    by_name = {c.name: c for c in columns}
    found = []
    for _seq, name, unique, _origin, partial in db.execute(f"PRAGMA index_list({table})"):
        if not unique or partial:
            continue
        parts: list[tuple[str, list]] = []
        for column_name in (r[2] for r in db.execute(f"PRAGMA index_info({name})")):
            column = by_name.get(column_name)
            pool = pool_for(column_name, pools) if column else None
            if column_name == "user_id":
                parts.append((column_name, ["steve"]))
            elif column and column.required and pool:
                parts.append((column_name, pools.ids[pool]))
            elif column and column.required and column.allowed:
                parts.append((column_name, column.allowed))
            else:
                parts = []
                break
        if parts:
            found.append(parts)
    return found


def _combination(domains: list[tuple[str, list]], index: int) -> dict[str, object] | None:
    chosen = {}
    for name, domain in reversed(domains):
        index, offset = divmod(index, len(domain))
        chosen[name] = domain[offset]
    return chosen if index == 0 else None


def generate_rows(
    db: sqlite3.Connection, table: str, count: int, rng: random.Random, pools: Pools,
    overrides: dict[str, Override] | None = None,
) -> Iterator[tuple[list[str], tuple]]:
    """Yield ``(column names, values)`` for up to ``count`` rows of ``table``.

    Fewer rows are produced when a unique index on pool or enumeration columns
    has fewer combinations than ``count``. A None value is dropped from the
    statement so the column default (or NULL) applies.
    """
    columns = read_columns(db, table)
    overrides = overrides or {}
    domains = _unique_domains(db, table, columns, pools)
    for index in range(count):
        forced: dict[str, object] = {}
        for parts in domains:
            combination = _combination(parts, index)
            if combination is None:
                return
            forced.update(combination)
        names, values = [], []
        for column in columns:
            if column.name in forced:
                value = forced[column.name]
            elif column.name in overrides:
                value = overrides[column.name](index, rng, pools)
            else:
                value = _default_value(column, table, index, rng, pools)
            if value is None:
                continue
            names.append(column.name)
            values.append(value if not isinstance(value, (dict, list)) else json.dumps(value))
        yield names, tuple(values)


def insert_batched(db: sqlite3.Connection, table: str,
                   rows: Iterator[tuple[list[str], tuple]]) -> int:
    """Insert rows, grouping consecutive rows with the same column list. Returns rows stored."""
    stored = 0
    batch: list[tuple] = []
    current: list[str] | None = None

    def flush() -> None:
        nonlocal stored, batch
        if batch and current is not None:
            marks = ",".join("?" * len(current))
            db.executemany(f"INSERT INTO {table} ({','.join(current)}) VALUES ({marks})", batch)
            stored += len(batch)
        batch = []

    for names, values in rows:
        if names != current or len(batch) >= 5_000:
            flush()
            current = names
        batch.append(values)
    flush()
    return stored
