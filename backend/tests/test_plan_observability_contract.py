"""The plans domain's evidence contract: named emit points, no content, never state.

Product events are fail-silent operational evidence. Three things go wrong when
that is left implicit: an emitter appears outside the named emit points and the
taxonomy forks, an emit starts carrying plan text, or the documentation names a
source that does not observe the fact it claims to answer.

The last one is the reason these tests execute the documented queries and
resolve the documented emitters rather than checking that a phrase is present.
A metric row that names a fabricated table, or an emitter with no producer, must
fail here — string presence cannot catch either.
"""

from __future__ import annotations

from collections.abc import Iterator

import ast
import re
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
OBSERVABILITY = REPO / "docs" / "observability.md"
ARCHITECTURE = REPO / "ARCHITECTURE.md"

#: Property names that would put plan content into the analytics store.
_FORBIDDEN_PROPERTIES = {
    "title", "description", "acceptance_criteria", "dod", "body", "text",
    "feedback", "option_label", "option_detail", "answer", "other_text",
    "rationale", "recommendation", "tradeoff", "context",
}


def _emit_calls(path: Path) -> list[ast.Call]:
    """Return every `emit_product_event(...)` call site in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None))
        == "emit_product_event"
    ]


def _plan_domain_modules() -> list[Path]:
    return sorted(
        path for path in (BACKEND / "plans").rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _backend_modules() -> list[Path]:
    return sorted(
        path for path in BACKEND.rglob("*.py")
        if "__pycache__" not in path.parts and "tests" not in path.parts
    )


def _dict_keys(value: ast.Dict) -> set[str]:
    return {
        key.value for key in value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _returned_dict_keys(function_name: str) -> set[str]:
    """Return the keys of the dict literals a plans-domain function returns."""
    keys: set[str] = set()
    for path in _plan_domain_modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef) and node.name == function_name:
                keys |= {
                    key
                    for returned in ast.walk(node)
                    if isinstance(returned, ast.Return)
                    and isinstance(returned.value, ast.Dict)
                    for key in _dict_keys(returned.value)
                }
    return keys


def _emit_property_keys(call: ast.Call) -> set[str]:
    """Return the property keys an emit sends: an inline dict, or the dict a
    plans-domain builder it calls returns. Empty means the gate cannot see them."""
    for keyword in call.keywords:
        if keyword.arg != "properties":
            continue
        if isinstance(keyword.value, ast.Dict):
            return _dict_keys(keyword.value)
        if isinstance(keyword.value, ast.Call):
            func = keyword.value.func
            return _returned_dict_keys(getattr(func, "attr", getattr(func, "id", "")))
    return set()


def _emit_identity(call: ast.Call) -> tuple[str, str] | None:
    if len(call.args) < 2:
        return None
    category, action = call.args[0], call.args[1]
    if isinstance(category, ast.Constant) and isinstance(action, ast.Constant):
        return category.value, action.value
    return None


def _resolve_action_literals(call: ast.Call, tree: ast.AST) -> set[str]:
    """Resolve a non-literal `action` to the exact values it can carry.

    A dynamic action must not become a wildcard: treating `continuation.<name>`
    as "anything" makes the gate blind, and an invented action would pass. The
    only dynamic emitter in the tree assigns its action from a conditional
    expression, so the literal outcome set is recoverable from the assignment.
    """
    name = getattr(call.args[1], "id", None)
    if name is None:
        return set()
    literals: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = {getattr(target, "id", None) for target in node.targets}
        if name not in targets:
            continue
        for value in ast.walk(node.value):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                literals.add(value.value)
    return literals


def _produced_event_names() -> set[str]:
    """Return every `category.action` any production module actually emits.

    Dynamic actions are resolved to their literal set rather than wildcarded,
    so a name no emitter can produce fails the gate.
    """
    produced: set[str] = set()
    for path in _backend_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _emit_calls(path):
            if len(call.args) < 2:
                continue
            category, action = call.args[0], call.args[1]
            if not isinstance(category, ast.Constant):
                continue
            if isinstance(action, ast.Constant):
                produced.add(f"{category.value}.{action.value}")
                continue
            for literal in _resolve_action_literals(call, tree):
                produced.add(f"{category.value}.{literal}")
    return produced


def test_the_emitter_gate_resolves_dynamic_actions_to_literals():
    """The gate must have power over the one dynamic emitter in the tree:
    `deliver_one` can pass exactly `delivered` or `failed`, so an invented
    action on that category must not resolve."""
    produced = _produced_event_names()

    assert "continuation.delivered" in produced
    assert "continuation.failed" in produced
    assert "continuation.teleported" not in produced
    assert not any(name.endswith(".*") for name in produced)


@pytest.fixture(scope="module")
def plans_section() -> str:
    text = OBSERVABILITY.read_text(encoding="utf-8")
    return text[text.index("## Structured plans"):]


@pytest.fixture
def seeded_db() -> Iterator[sqlite3.Connection]:
    """A real schema with one row in every table the AC41 queries read."""
    from infra.schema import SCHEMA_SQL

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('p1','P','t','t')"
    )
    connection.execute(
        "INSERT INTO threads (id,user_id,project_id,created_at,updated_at) "
        "VALUES ('owner-1','u1','p1','t','t')"
    )
    connection.execute(
        "INSERT INTO plans (id,owner_kind,owner_id,state,revision,"
        "created_at,updated_at) VALUES ('plan-1','thread','owner-1','active',3,"
        "'2026-01-01T00:00:00+00:00','2026-01-01T02:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,"
        "acceptance_criteria,commitment_id,state,created_at,updated_at) "
        "VALUES ('node-1','plan-1',0,'task','T','DOD','node-1','accepted',"
        "'2026-01-01T00:00:00+00:00','2026-01-01T02:00:00+00:00')"
    )
    for index, (event, prior, new, result, at) in enumerate((
        ("node_created", None, None, None, "2026-01-01T00:00:00+00:00"),
        ("state_transition", "pending", "active", None, "2026-01-01T01:00:00+00:00"),
        ("state_transition", "active", "review_requested", None,
         "2026-01-01T01:30:00+00:00"),
        ("review_rejected", None, None, None, "2026-01-01T01:40:00+00:00"),
        ("state_transition", "review_requested", "accepted", "passed",
         "2026-01-01T02:00:00+00:00"),
    )):
        connection.execute(
            "INSERT INTO plan_node_events (id,node_id,provenance,event,"
            "prior_state,new_state,verification_result,created_at) "
            "VALUES (?,?,'test',?,?,?,?,?)",
            (f"ev-{index}", "node-1", event, prior, new, result, at),
        )
    connection.execute(
        "INSERT INTO plan_liveness_wakes (id,thread_id,status,deadline,cycle,"
        "last_open_count,armed_at,fired_at,updated_at) "
        "VALUES ('w1','owner-1','stalled','t',1,1,'2026-01-01T00:00:00+00:00',"
        "'2026-01-01T01:00:00+00:00','t')"
    )
    # No `continuation_triggers` row is seeded. Every wake these tests count is
    # written by `enqueue` and settled by `deliver_one`, because a hand-written
    # lifecycle can satisfy a query no production writer could ever produce —
    # and the seeded non-zero `cost_estimate_usd` was exactly such a fiction.
    connection.commit()
    yield connection
    connection.close()


def _sql_blocks(section: str) -> list[str]:
    return re.findall(r"```sql\n(.*?)```", section, re.DOTALL)


# --- ownership -------------------------------------------------------------


def test_the_plans_domain_emits_only_from_its_named_emit_points():
    """Named owners, so the taxonomy cannot fork and a reviewer knows where to
    look: the closing mutation's result. Any other
    emitter in this domain is the failure this pins."""
    emitters = sorted(
        str(path.relative_to(REPO))
        for path in _plan_domain_modules()
        if _emit_calls(path)
    )

    assert emitters == ["backend/plans/close_next_event.py"]


def test_no_plans_emit_carries_plan_content():
    """Counts, states, durations and content-free ids only. The analytics store
    is evidence about the work, never a second copy of the work. Every emit's
    keys must be visible to this gate, or it would pass without looking."""
    offending: list[tuple[str, str]] = []
    for path in _plan_domain_modules():
        for call in _emit_calls(path):
            keys = _emit_property_keys(call)
            assert keys, f"{path.relative_to(REPO)}: emit properties are not inspectable"
            offending.extend(
                (str(path.relative_to(REPO)), key)
                for key in sorted(keys & _FORBIDDEN_PROPERTIES)
            )

    assert offending == []


def test_the_plans_emit_taxonomy_is_stable_and_documented():
    """A category/action pair is a name other people query by."""
    identities = {
        _emit_identity(call)
        for path in _plan_domain_modules()
        for call in _emit_calls(path)
    }

    assert identities == {("plans", "close_next")}
    documented = OBSERVABILITY.read_text(encoding="utf-8")
    assert "plans.close_next" in documented


def test_the_documentation_separates_plan_state_from_operational_evidence(
    plans_section,
):
    """The canonical ownership sentence, in both places a reader looks."""
    for document in (OBSERVABILITY, ARCHITECTURE):
        text = document.read_text(encoding="utf-8").casefold()
        assert "plan_nodes" in text
        assert "fail-silent" in text or "fail silent" in text

    lowered = plans_section.casefold()
    assert "never state" in lowered
    assert "authorization" in lowered


# --- AC41 sources must actually observe the fact ---------------------------


def test_every_documented_emitter_has_a_real_producer(plans_section):
    """An emitter named in the taxonomy must be one some production module
    emits. This is what a fabricated event name fails."""
    produced = _produced_event_names()
    named = set(re.findall(r"`([a-z_]+)\.([a-z_*]+)` product event", plans_section))

    assert named
    missing = sorted(
        f"{category}.{action}" for category, action in named
        if f"{category}.{action}" not in produced
        and f"{category}.*" not in produced
    )
    assert missing == []


def test_every_documented_table_exists_in_the_schema(plans_section, seeded_db):
    """A table named as a source must be one a reader can query."""
    tables = {
        row[0] for row in seeded_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    named = set(re.findall(r"`(plan[a-z_]*|continuation_triggers)`", plans_section))

    assert named
    assert sorted(name for name in named if name not in tables) == []


def test_every_documented_query_executes_against_the_real_schema(
    plans_section, seeded_db,
):
    """Each AC41 query is executed, not read. A fabricated table or column
    fails here with the sqlite error naming it."""
    blocks = _sql_blocks(plans_section)

    assert len(blocks) >= 4
    for block in blocks:
        seeded_db.execute(block).fetchall()


def test_the_transition_latency_query_measures_the_pending_dwell(
    plans_section, seeded_db,
):
    """The first transition is the one that shows how long a node sat pending,
    so a query that starts at the first `state_transition` measures nothing for
    it. `node_created` must anchor the series."""
    block = next(
        block for block in _sql_blocks(plans_section)
        if "plan_node_events" in block and "node_created" in block
    )

    rows = seeded_db.execute(block).fetchall()
    measured = [row for row in rows if row["prior_state_seconds"] is not None]

    assert len(measured) == 4
    assert measured[0]["new_state"] == "active"
    assert measured[0]["prior_state_seconds"] == pytest.approx(3600, abs=1)


def test_the_stalled_duration_query_uses_the_stall_onset(
    plans_section, seeded_db,
):
    """`window_started_at` is the rate-limit window and is NULL for a wake that
    never delivered. `fired_at` is written at the stall itself."""
    block = next(
        block for block in _sql_blocks(plans_section)
        if "plan_liveness_wakes" in block
    )

    assert "fired_at" in block
    assert "window_started_at" not in block
    rows = seeded_db.execute(block).fetchall()
    assert len(rows) == 1



@pytest.mark.parametrize(
    "metric",
    [
        "node transition latency",
        "stalled duration",
        "wake admission",
        "wake deferral",
        "verification result",
        "rollup freshness",
        "decision age",
        "user corrections",
    ],
)
def test_every_ac41_metric_names_where_it_is_read_from(plans_section, metric):
    """AC41 is closed by pointing at real sources. Every listed metric,
    including the deferral row, must be present."""
    assert metric in plans_section.casefold()
