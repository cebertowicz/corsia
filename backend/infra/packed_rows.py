"""Fetch a read's rows through one SQLite step instead of one step per row.

``sqlite3`` releases and re-takes the GIL around every row it steps. While any
other thread is running Python, each re-take waits for that thread to yield,
so a read returning thousands of rows waits thousands of times: a plan
projection that takes half a second alone took 5-20 s beside one to three busy
threads. Here SQLite builds the whole result as one JSON array inside a single
step, and Python decodes it in one call.

The rows are identical to ``execute(...).fetchall()``: the same values, order
and row factory. JSON cannot carry a BLOB, and SQLite's own JSON writes a REAL
with 15 significant digits, so a REAL travels as ``printf('%!.17g')`` text
inside a marker object and is rebuilt with ``float``: 17 significant digits
identify one double, and that rendering was checked to round-trip across the
magnitudes below. Outside them (SQLite misrenders some doubles beyond about
1e50) the read falls back, as do a BLOB, a column name SQLite would have to
rename, and a connection wrapper whose ``execute`` does not return SQLite
cursors (a test's row-counting proxy).

A zero REAL always arrives as ``0.0``, because ``printf`` drops its sign. The
ordinary fetch can return ``-0.0`` from a column with no REAL affinity or from
an expression; the two compare equal. No cost column can hold ``-0.0``, since
REAL affinity stores it as ``0.0``.
"""

import json
import re
import sqlite3
from typing import Any

#: REALs packed as text are 0 or between these magnitudes; others fall back.
_REAL_MIN, _REAL_MAX = "1e-20", "1e20"
#: How a packed REAL appears in the JSON; no text value can contain it unescaped.
_REAL_MARK = '{"r":"'

#: SQLite's suffix for a duplicate column name read through a subquery.
_RENAMED = re.compile(r":\d+$")


def _packed_sql(sql: str, width: int) -> str:
    columns = ",".join(f"c{i}" for i in range(width))
    # A BLOB, or a REAL outside the checked magnitudes, becomes an empty BLOB,
    # which json_array refuses, so the whole read falls back. ``||''`` drops any
    # JSON subtype a text value carries, so json_array embeds it as the string
    # the ordinary fetch returns.
    values = ",".join(
        f"CASE typeof(c{i}) "
        f"WHEN 'real' THEN CASE WHEN c{i}=0 OR (abs(c{i})>={_REAL_MIN} AND abs(c{i})<={_REAL_MAX}) "
        f"THEN json_object('r',printf('%!.17g',c{i})) ELSE x'' END "
        f"WHEN 'blob' THEN x'' WHEN 'text' THEN c{i}||'' ELSE c{i} END"
        for i in range(width)
    )
    return f"WITH q({columns}) AS ({sql}) SELECT json_group_array(json_array({values})) FROM q"


def fetch_all(db, sql: str, params: Any = ()) -> list:
    """Return ``db.execute(sql, params).fetchall()`` for one SELECT, stepped once."""
    sql = sql.strip().rstrip(";")
    # LIMIT 0 prepares the read for its column names without running it.
    shape = db.execute(f"SELECT * FROM ({sql}) LIMIT 0", params)
    if not isinstance(shape, sqlite3.Cursor):
        return db.execute(sql, params).fetchall()
    names = [column[0] for column in shape.description]
    if any(_RENAMED.search(name) for name in names):
        return db.execute(sql, params).fetchall()
    try:
        packed = db.execute(_packed_sql(sql, len(names)), params).fetchone()[0]
    except sqlite3.OperationalError as error:
        if "BLOB" not in str(error):
            raise
        return db.execute(sql, params).fetchall()
    decoded = json.loads(packed)
    if _REAL_MARK in packed:
        decoded = [[_unpack(value) for value in row] for row in decoded]
    factory = shape.row_factory
    if factory is None:
        return [tuple(row) for row in decoded]
    return [factory(shape, tuple(row)) for row in decoded]


def _unpack(value: Any) -> Any:
    return float(value["r"]) if type(value) is dict else value
