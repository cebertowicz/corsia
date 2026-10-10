"""``infra.packed_rows.fetch_all`` returns exactly what ``fetchall`` returns."""

import math
import random
import sqlite3
import struct

import pytest

from infra.packed_rows import fetch_all


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, n INTEGER, doc TEXT)")
    connection.executemany(
        "INSERT INTO t (name, n, doc) VALUES (?, ?, ?)",
        [
            ("b", 2, '{"a":[1,2]}'),
            ("a", None, "[1]"),
            ('quote " back \\ tab \t é 😀', 2**63 - 1, None),
            ("", -(2**63), "plain"),
        ],
    )
    yield connection
    connection.close()


def _same(db, sql, params=()):
    expected = db.execute(sql, params).fetchall()
    actual = fetch_all(db, sql, params)
    assert [tuple(row) for row in actual] == [tuple(row) for row in expected]
    assert [type(value) for row in actual for value in row] == [
        type(value) for row in expected for value in row
    ]
    if expected and isinstance(expected[0], sqlite3.Row):
        assert [row.keys() for row in actual] == [row.keys() for row in expected]
        assert [dict(row) for row in actual] == [dict(row) for row in expected]
    return actual


def test_rows_values_and_order_match_fetchall(db):
    rows = _same(db, "SELECT * FROM t ORDER BY name DESC")
    assert rows[0]["name"] == 'quote " back \\ tab \t é 😀'


def test_json_text_stays_the_string_fetchall_returns(db):
    rows = _same(
        db,
        "SELECT id, json_extract(doc, '$.a') AS a, json_object('k', n) AS o, doc -> '$.a' AS arrow "
        "FROM t WHERE json_valid(doc) ORDER BY id",
    )
    assert rows[0]["a"] == "[1,2]"


def test_parameters_cte_and_empty_results(db):
    _same(db, "WITH x AS (SELECT * FROM t WHERE n > ?) SELECT name FROM x ORDER BY id", (0,))
    _same(db, "SELECT * FROM t WHERE name = :name", {"name": "a"})
    assert fetch_all(db, "SELECT * FROM t WHERE id < 0") == []


def test_tuple_rows_without_a_row_factory(db):
    db.row_factory = None
    assert fetch_all(db, "SELECT id, name FROM t ORDER BY id") == db.execute(
        "SELECT id, name FROM t ORDER BY id"
    ).fetchall()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, CAST(name AS BLOB) AS raw FROM t ORDER BY id",  # JSON has no BLOB
        "SELECT a.id, b.id FROM t a JOIN t b ON b.id = a.id ORDER BY a.id",  # duplicate names
    ],
)
def test_results_json_cannot_carry_fall_back_to_fetchall(db, sql):
    _same(db, sql)


def _bits(rows) -> list:
    return [struct.pack("<d", value) if isinstance(value, float) else value for row in rows for value in row]


def test_reals_come_back_bit_for_bit(db):
    # SQLite's printf('%!.17g') misrenders about one in five random doubles beyond ~1e50
    # (7.761117632285041e+178 reads back as 7.7611176322850402e+178), so a REAL outside
    # 1e-20..1e20 is never rebuilt from text: the read falls back to the ordinary fetch.
    values = [
        7.761117632285041e178,
        0.1 + 0.2,
        1 / 3,
        5e-324,
        -5e-324,
        1.7976931348623157e308,
        0.0,
        5.0,
        1e22,
        123456789.12345679,
    ]
    generator = random.Random(7)
    values += [generator.random() * 10 ** generator.randint(-12, 12) for _ in range(200)]
    values += [struct.unpack("<d", generator.randbytes(8))[0] for _ in range(300)]
    values = [value for value in values if value == value and abs(value) != float("inf")]
    db.execute("CREATE TABLE r (id INTEGER PRIMARY KEY, x REAL, y)")
    db.executemany("INSERT INTO r (x, y) VALUES (?, ?)", [(value, value) for value in values])
    sql = "SELECT id, x, y, x * 3 AS tripled, SUM(x) OVER (ORDER BY id) AS running FROM r ORDER BY id"
    expected = db.execute(sql).fetchall()

    packed = fetch_all(db, sql)

    assert _bits(packed) == _bits(expected)
    assert all(type(row["x"]) is float for row in packed)


def test_a_double_printf_misrenders_is_returned_exactly(db):
    db.execute("CREATE TABLE d (x REAL)")
    db.executemany("INSERT INTO d VALUES (?)", [(7.761117632285041e178,), (0.1 + 0.2,)])
    sql = "SELECT x FROM d ORDER BY rowid"
    assert [row["x"] for row in fetch_all(db, sql)] == [7.761117632285041e178, 0.1 + 0.2]
    assert _bits(fetch_all(db, sql)) == _bits(db.execute(sql).fetchall())


def _outside_the_shape_probe_and_wrapper(db, fn) -> list[str]:
    """Statements fn ran that are neither a shape probe nor the packed wrapper."""
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        fn()
    finally:
        db.set_trace_callback(None)
    return [
        statement for statement in statements
        if not statement.rstrip().endswith("LIMIT 0") and not statement.startswith("WITH q(")
    ]


def test_reals_in_range_travel_packed_and_exact(db):
    generator = random.Random(11)
    values = [0.1 + 0.2, 1 / 3, 0.0, 5.0, 2.5e-7, 123456789.12345679]
    values += [(0.5 + generator.random() / 2) * 10 ** generator.randint(-19, 15) for _ in range(5000)]
    values += [-(0.5 + generator.random() / 2) * 10 ** generator.randint(-19, 15) for _ in range(500)]
    db.execute("CREATE TABLE r (id INTEGER PRIMARY KEY, x REAL, g INTEGER)")
    db.executemany(
        "INSERT INTO r (x, g) VALUES (?, ?)", [(value, index % 7) for index, value in enumerate(values)],
    )
    db.execute("INSERT INTO r (x, g) VALUES (NULL, 99)")
    sql = (
        "SELECT g, SUM(x) AS total, MIN(x) AS low, MAX(x) AS high, COUNT(x) AS n "
        "FROM r GROUP BY g ORDER BY g"
    )
    expected = db.execute(sql).fetchall()

    packed = fetch_all(db, sql)
    ran = _outside_the_shape_probe_and_wrapper(db, lambda: fetch_all(db, sql))

    assert _bits(packed) == _bits(expected)
    assert expected[-1]["total"] is None and packed[-1]["total"] is None
    assert ran == []  # the read ran only inside the packed wrapper, never as the plain fetch

    db.execute("CREATE TABLE edge (x REAL)")
    db.executemany("INSERT INTO edge VALUES (?)", [(1e-20,), (1e20,), (-1e20,), (0.0,)])
    edge = "SELECT x FROM edge ORDER BY rowid"
    assert _bits(fetch_all(db, edge)) == _bits(db.execute(edge).fetchall())
    assert _outside_the_shape_probe_and_wrapper(db, lambda: fetch_all(db, edge)) == []


def test_a_text_value_that_looks_like_a_packed_real_stays_text(db):
    db.execute("CREATE TABLE m (v, w REAL)")
    db.executemany("INSERT INTO m VALUES (?, ?)", [('{"r":"1.5"}', 0.5), ('x{"r":"2"}', None)])
    rows = _same(db, "SELECT v, w FROM m ORDER BY rowid")
    assert [row["v"] for row in rows] == ['{"r":"1.5"}', 'x{"r":"2"}']
    assert [row["w"] for row in rows] == [0.5, None]


def test_an_integer_aggregate_packs_in_one_step(db):
    sql = "SELECT name, SUM(n) AS total, COUNT(*) AS rows FROM t GROUP BY name ORDER BY name"
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        rows = fetch_all(db, sql)
    finally:
        db.set_trace_callback(None)

    assert [tuple(row) for row in rows] == [tuple(row) for row in db.execute(sql).fetchall()]
    # Packed: the read ran only inside the wrapper, never as the plain fallback.
    assert not [statement for statement in statements if statement.startswith("SELECT name, SUM")]


def test_other_sql_errors_still_raise(db):
    with pytest.raises(sqlite3.OperationalError):
        fetch_all(db, "SELECT missing FROM t")


def test_a_schema_change_under_a_cached_select_star_reprobes_the_columns():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    sql = "SELECT * FROM t"
    assert fetch_all(db, sql) == [(1,)]
    db.execute("ALTER TABLE t ADD COLUMN b TEXT DEFAULT 'x'")
    assert fetch_all(db, sql) == [(1, "x")]


def test_a_zero_real_arrives_unsigned_where_the_plain_fetch_keeps_a_negative_zero():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE z (y)")
    db.execute("INSERT INTO z VALUES (-0.0)")
    plain = db.execute("SELECT y FROM z").fetchall()[0][0]
    packed = fetch_all(db, "SELECT y FROM z")[0][0]
    assert math.copysign(1.0, plain) == -1.0
    assert math.copysign(1.0, packed) == 1.0
    assert plain == packed == 0.0
    # REAL affinity stores a negative zero as 0.0, so a cost column never holds one.
    db.execute("CREATE TABLE c (y REAL)")
    db.execute("INSERT INTO c VALUES (-0.0)")
    assert math.copysign(1.0, db.execute("SELECT y FROM c").fetchall()[0][0]) == 1.0
    assert math.copysign(1.0, fetch_all(db, "SELECT y FROM c")[0][0]) == 1.0
