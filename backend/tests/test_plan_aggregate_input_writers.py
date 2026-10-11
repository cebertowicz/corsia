"""The W gate: every runtime write of an aggregate input goes through one of three sites.

The plan-aggregates design (``shared/planning/plans-reads-scale-with-open-work``,
r3 section 4) keeps per-node subtree counts exact by one hook called from
``service.touch_node`` and the creation path in ``node_creation``. That holds
only while no other code writes the columns the counts are derived from. This
test pins it statically, so a new writer fails here before it can drift a count.

Aggregate inputs: INSERT, DELETE and REPLACE of a ``plan_nodes`` row, and an
UPDATE of ``state``, ``archived_at``, ``spawned_thread_id``, ``parent_id`` or
``plan_id`` (``sent_back`` joins them when that column exists).

The scan reads every string literal and f-string in backend modules (tests and
``infra/migration_*`` excluded) and matches write verbs against ``plan_nodes``.
A write whose target is not a literal identifier (``DELETE FROM {table}``) or
whose SQL is assembled around a dangling verb is flagged as indirect: it is a
violation unless it is listed in ``KNOWN_INDIRECT`` with the reason it cannot
reach ``plan_nodes``.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
AGGREGATE_COLUMNS = frozenset(
    {"state", "archived_at", "spawned_thread_id", "parent_id", "plan_id", "sent_back"}
)
_SKIPPED_DIRS = {"tests", ".venv", "node_modules", "__pycache__", "eval_runs"}

_VERB = (
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM"
)
# A literal ``plan_nodes`` target matches in any case. An indirect target (a
# placeholder where the table name goes) or a dangling verb matches only in
# upper case, as SQL is written here; prose that says "update" is not a write.
_WRITE = re.compile(rf"\b(?P<verb>{_VERB})\s+plan_nodes\b", re.IGNORECASE)
_INDIRECT = re.compile(rf"\b(?P<verb>{_VERB})\s+(?P<target>[{{%$:?][^\s(]*)")
_DANGLING = re.compile(rf"\b(?:{_VERB})\s*$")
_SET_CLAUSE = re.compile(r"\bSET\b(?P<set>.*?)(?:\bWHERE\b|\bFROM\b|$)", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Write:
    """One SQL write found in source."""

    path: str
    line: int
    function: str
    verb: str
    target: str
    columns: tuple[str, ...]

    @property
    def site(self) -> str:
        return f"{self.path}::{self.function}"

    @property
    def aggregate_input(self) -> bool:
        if self.verb in {"INSERT", "DELETE", "REPLACE"}:
            return True
        return "<dynamic>" in self.columns or bool(AGGREGATE_COLUMNS & set(self.columns))


def _verb(raw: str) -> str:
    word = raw.split()[0].upper()
    return word


def _columns(sql: str, verb: str) -> tuple[str, ...]:
    if verb != "UPDATE":
        return ()
    match = _SET_CLAUSE.search(sql)
    if match is None:
        return ("<dynamic>",)
    names: list[str] = []
    for assignment in match.group("set").split(","):
        name = assignment.split("=")[0].strip()
        if not name:
            continue
        # ``SET {assignments}revision=...``: the column list is built at runtime.
        names.append("<dynamic>" if "{" in name else name)
    return tuple(dict.fromkeys(names))


def _literal_text(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(part.value) if isinstance(part, ast.Constant) else "{}" for part in node.values
        )
    return None


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.stack: list[str] = []
        self.writes: list[Write] = []
        self.executescript: list[tuple[str, int, str]] = []
        self._nested_in_fstring: set[int] = set()

    def _scope(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope  # noqa: N815

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if isinstance(node.func, ast.Attribute) and node.func.attr == "executescript":
            self.executescript.append((self.path, node.lineno, ".".join(self.stack) or "<module>"))
        self.generic_visit(node)

    def _record(self, node: ast.Constant | ast.JoinedStr) -> None:
        text = _literal_text(node)
        if text is None:
            return
        sql = " ".join(text.split())
        function = ".".join(self.stack) or "<module>"
        for match in _WRITE.finditer(sql):
            verb = _verb(match.group("verb"))
            self.writes.append(Write(
                self.path, node.lineno, function, verb, "plan_nodes",
                _columns(sql[match.start():], verb),
            ))
        for match in _INDIRECT.finditer(sql):
            verb = _verb(match.group("verb"))
            self.writes.append(Write(
                self.path, node.lineno, function, verb,
                f"<indirect:{match.group('target')}>", (),
            ))
        if _DANGLING.search(sql):
            verb = _verb(_DANGLING.search(sql).group(0))  # type: ignore[union-attr]
            self.writes.append(Write(
                self.path, node.lineno, function, verb, "<indirect:dangling>", ("<dynamic>",),
            ))

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        if id(node) not in self._nested_in_fstring:
            self._record(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:  # noqa: N802
        self._record(node)
        for part in node.values:
            self._nested_in_fstring.add(id(part))
        self.generic_visit(node)


def _runtime_files() -> list[Path]:
    files = []
    for path in sorted(BACKEND.rglob("*.py")):
        relative = path.relative_to(BACKEND)
        if _SKIPPED_DIRS & set(relative.parts):
            continue
        if relative.parts[0] == "infra" and path.name.startswith("migration_"):
            continue
        files.append(path)
    return files


def _scan() -> tuple[list[Write], list[tuple[str, int, str]]]:
    writes: list[Write] = []
    scripts: list[tuple[str, int, str]] = []
    for path in _runtime_files():
        relative = str(path.relative_to(BACKEND))
        scanner = _Scanner(relative)
        scanner.visit(ast.parse(path.read_text(), filename=str(path)))
        writes.extend(scanner.writes)
        scripts.extend(scanner.executescript)
    return writes, scripts


#: The three sanctioned sites. ``hook`` sites will call ``aggregates.apply``;
#: ``exempt`` sites write ``order_key`` only, which is not an aggregate input.
ALLOWED: dict[str, tuple[str, str]] = {
    "plans/service.py::touch_node": (
        "hook", "UPDATE with caller-named columns from _TOUCH_COLUMNS; the single revision writer",
    ),
    "plans/node_creation.py::_insert_node_row": (
        "hook", "the one creation path: two INSERTs, current shape and pre-v51 shape",
    ),
    "plans/node_creation.py::_open_order_slot": (
        "exempt", "slot shift: order_key only, deliberately raw so siblings keep their revision",
    ),
    "plans/mutations.py::reorder_node": (
        "exempt", "sibling renumber: order_key only, deliberately raw (mutations.py:79)",
    ),
    "plans/aggregates.py::<module>": (
        "aggregates", "the hook's per-level UPDATE (_NODE_UPDATE): aggregate columns only",
    ),
    "plans/aggregates.py::note_event": (
        "aggregates", "own_last_changed on the node, settled_events on its plan, set when service._event appends an event",
    ),
}

#: Writes whose target is not a literal ``plan_nodes``. Each is a violation unless
#: it is listed here with the reason it cannot reach the table.
KNOWN_INDIRECT: dict[str, str] = {
    "eval/host_access_owner_replay.py::<module>": (
        'a replay case label ("user-directed UPDATE"), not SQL; the statement is against threads'
    ),
    "api/measurement_prepared.py::_restore": (
        "table comes from DDL/_ROOT_TABLES/_SCOPE_TABLES: measurement snapshot tables"
    ),
    "api/measurement_recent.py::measurement_key_figures.query": (
        "table is PREPARED_SCOPE, the measurement scope temp table"
    ),
}

#: Files whose indirect writes are accepted must never mention the plan table.
_INDIRECT_FILES_MUST_NOT_MENTION = "plan_nodes"

#: Every runtime ``executescript`` call. A new one needs a look: a script can
#: write any table. None of these scripts writes ``plan_nodes`` (the scan above
#: reads their literals too); they create schema.
KNOWN_EXECUTESCRIPT = {
    "infra/database.py::initialize_schema",
    "infra/db_connection.py::PooledConnection.executescript",
    "infra/otel_logs/store.py::LogStore._connect_with_schema",
    "doceditor/live_store.py::LiveStore.__init__",
}


def test_aggregate_input_writes_only_in_sanctioned_sites() -> None:
    writes, _scripts = _scan()
    violations = []
    for write in writes:
        if write.target == "plan_nodes":
            if write.aggregate_input and write.site not in ALLOWED:
                violations.append(write)
            elif not write.aggregate_input and write.site not in ALLOWED:
                # order_key-only writes are exempt, but only the two listed ones.
                violations.append(write)
        elif write.site not in KNOWN_INDIRECT:
            violations.append(write)
    assert not violations, (
        "unsanctioned plan_nodes write (route it through service.touch_node or "
        "node_creation, or extend the W gate deliberately):\n"
        + "\n".join(
            f"  {w.path}:{w.line} {w.function} {w.verb} {w.target} {list(w.columns)}"
            for w in violations
        )
    )


def test_allowed_sites_are_all_still_present_and_typed() -> None:
    """A stale allowance would hide a moved writer: every entry must still match."""
    writes, _scripts = _scan()
    seen: dict[str, list[Write]] = {}
    for write in writes:
        seen.setdefault(write.site, []).append(write)
    missing = [site for site in (*ALLOWED, *KNOWN_INDIRECT) if site not in seen]
    assert not missing, f"allowed writer no longer found: {missing}"
    for site, (kind, _why) in ALLOWED.items():
        for write in seen[site]:
            assert write.target == "plan_nodes"
            if kind == "exempt":
                assert write.verb == "UPDATE" and write.columns == ("order_key",), write
            if kind == "aggregates":
                assert write.verb == "UPDATE", write
    from infra.plan_aggregates_schema import NODE_AGGREGATE_NAMES
    from plans import aggregates

    # The third writer writes aggregate columns only, never an input, revision or updated_at.
    assert set(aggregates._NODE_WRITES) <= NODE_AGGREGATE_NAMES
    assert not {w.columns for w in seen["plans/aggregates.py::note_event"]} - {
        ("own_last_changed",), ("settled_events",),
    }
    # The creation path is INSERT only; touch_node is the one dynamic UPDATE.
    assert {w.verb for w in seen["plans/node_creation.py::_insert_node_row"]} == {"INSERT"}
    assert {w.verb for w in seen["plans/service.py::touch_node"]} == {"UPDATE"}


def test_no_runtime_delete_replace_or_parent_rewrite() -> None:
    """N3: nothing deletes or replaces a node row, and nothing rewrites its parent or plan."""
    writes, _scripts = _scan()
    plan_writes = [w for w in writes if w.target == "plan_nodes"]
    assert not [w for w in plan_writes if w.verb in {"DELETE", "REPLACE"}]
    assert not [w for w in plan_writes if {"parent_id", "plan_id"} & set(w.columns)]
    from plans import service

    assert not {"parent_id", "plan_id"} & set(service._TOUCH_COLUMNS)
    # touch_node's reachable aggregate-input columns, as r3 section 4 lists them.
    assert AGGREGATE_COLUMNS & set(service._TOUCH_COLUMNS) == {
        "state", "archived_at", "spawned_thread_id", "sent_back",
    }
    from plans import aggregates

    # The hook runs on exactly the touch_node columns that are aggregate inputs.
    assert aggregates.INPUT_COLUMNS == AGGREGATE_COLUMNS & set(service._TOUCH_COLUMNS)


def test_indirect_writers_never_name_the_plan_table() -> None:
    for site in KNOWN_INDIRECT:
        path = BACKEND / site.split("::")[0]
        assert _INDIRECT_FILES_MUST_NOT_MENTION not in path.read_text(), site


def test_executescript_sites_are_known() -> None:
    _writes, scripts = _scan()
    found = {f"{path}::{function}" for path, _line, function in scripts}
    assert found == KNOWN_EXECUTESCRIPT


def test_scanner_catches_each_bypass_shape() -> None:
    """The gate itself: every write shape the design lists must be recognised."""
    shapes = {
        "INSERT INTO plan_nodes (id) VALUES (?)": "INSERT",
        "INSERT OR REPLACE INTO plan_nodes (id) VALUES (?)": "INSERT",
        "REPLACE INTO plan_nodes (id) VALUES (?)": "REPLACE",
        "DELETE FROM plan_nodes WHERE id=?": "DELETE",
        "UPDATE plan_nodes SET state=?, archived_at=? WHERE id=?": "UPDATE",
        "UPDATE OR IGNORE plan_nodes SET parent_id=? WHERE id=?": "UPDATE",
    }
    for sql, verb in shapes.items():
        scanner = _Scanner("x.py")
        scanner.visit(ast.parse(f"db.execute({sql!r})"))
        assert [(w.verb, w.target) for w in scanner.writes] == [(verb, "plan_nodes")], sql
        assert scanner.writes[0].aggregate_input
    for source in (
        'db.execute(f"DELETE FROM {table}")',
        'db.execute("UPDATE " + table + " SET x=1")',
        'db.execute("DELETE FROM %s" % table)',
        'db.executescript("DELETE FROM plan_nodes;")',
    ):
        scanner = _Scanner("x.py")
        scanner.visit(ast.parse(source))
        assert scanner.writes, source
    order_only = _Scanner("x.py")
    order_only.visit(ast.parse('db.execute("UPDATE plan_nodes SET order_key=? WHERE id=?")'))
    assert order_only.writes[0].columns == ("order_key",)
    assert not order_only.writes[0].aggregate_input


def test_runtime_connections_run_with_foreign_keys_off(tmp_path) -> None:
    """The ``ON DELETE CASCADE`` on plan_nodes is dormant, and the hook relies on it.

    A cascade deletes plan_nodes rows below the hook. Turning foreign keys on needs
    those cascades routed through ``aggregates.apply`` first (design r5, section 4).
    """
    from infra.database import _make_connection

    connection = _make_connection(str(tmp_path / "pin.db"))
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    finally:
        connection.close()
    switched_on = [
        str(path.relative_to(BACKEND))
        for path in BACKEND.rglob("*.py")
        if not {"tests", ".venv"} & set(path.relative_to(BACKEND).parts)
        and re.search(r"foreign_keys\s*=\s*(ON|1)", path.read_text(), re.IGNORECASE)
    ]
    # The migration runner restores a setting it found on; nothing else enables it.
    assert switched_on == ["infra/database_migrations.py"]
