"""I4 — lifecycle states are explicit, single-sourced, and storage-enforced."""

from __future__ import annotations

import re
import sqlite3
from typing import get_args

import pytest

from core.checkpoint_types import CheckpointStatus
from infra.checkpoint_schema import CHECKPOINT_SCHEMA_SQL
from infra.delivery_states import (
    INPUT_DELIVERY_STATES,
    TOOL_RESULT_BINDING_STATES,
)
from tests.contract.antivacuity import require_evidence
from tests.contract.conftest import iter_backend_sources

pytestmark = pytest.mark.contract

CONTRACT_INPUT_STATES = frozenset(INPUT_DELIVERY_STATES)
CONTRACT_CHECKPOINT_STATES = frozenset(get_args(CheckpointStatus))
CONTRACT_TOOL_BINDING_STATES = frozenset(TOOL_RESULT_BINDING_STATES)

_DELIVERY_STATE_RE = re.compile(r"""delivery_state\s*==?\s*["']([a-z_]+)["']""")
_BINDING_STATE_RE = re.compile(
    r"""tool_result_binding_state['"]?\s*[)\]]*\s*(?:==?|:)\s*\(?\s*["']([a-z_]+)["']"""
)
_VOCABULARY_IMPORT_RE = re.compile(
    r"from infra\.delivery_states import\s*\(?([^)\n]*(?:\n[^)]*)?)\)?"
)
_CHECKPOINT_STORAGE_RE = re.compile(
    r"status\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*"
    r"status\s+IN\s*\(([^)]*)\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)
_QUOTED_STATE_RE = re.compile(r"['\"]([a-z_]+)['\"]")


def _vocabulary_consumers(*symbols: str) -> list[str]:
    consumers: list[str] = []
    for path, source in iter_backend_sources():
        if path.name == "delivery_states.py":
            continue
        for match in _VOCABULARY_IMPORT_RE.finditer(source):
            if any(symbol in match.group(1) for symbol in symbols):
                consumers.append(path.name)
                break
    return consumers


def _collect(pattern: re.Pattern[str]) -> set[str]:
    found: set[str] = set()
    for _path, source in iter_backend_sources():
        found.update(match.group(1) for match in pattern.finditer(source))
    return found


def test_input_delivery_states_are_the_contract_set() -> None:
    require_evidence(
        _vocabulary_consumers("INPUT_DELIVERY"),
        scan="input delivery-state scan",
        expectation="production consumers of infra.delivery_states",
        replacement_hint="point the scan at the canonical vocabulary",
    )
    imposters = _collect(_DELIVERY_STATE_RE) - CONTRACT_INPUT_STATES
    assert not imposters, f"undeclared input delivery states: {sorted(imposters)}"


def test_tool_binding_states_are_the_contract_set() -> None:
    require_evidence(
        _vocabulary_consumers("TOOL_RESULT"),
        scan="tool-result binding-state scan",
        expectation="production consumers of infra.delivery_states",
        replacement_hint="point the scan at the canonical vocabulary",
    )
    imposters = _collect(_BINDING_STATE_RE) - CONTRACT_TOOL_BINDING_STATES
    assert not imposters, f"undeclared tool binding states: {sorted(imposters)}"


def test_checkpoint_storage_uses_the_declared_statuses() -> None:
    match = _CHECKPOINT_STORAGE_RE.search(CHECKPOINT_SCHEMA_SQL)
    assert match is not None, "checkpoint storage has no enforced status vocabulary"
    stored = frozenset(_QUOTED_STATE_RE.findall(match.group(1)))
    assert stored == CONTRACT_CHECKPOINT_STATES, (
        "checkpoint type and storage status vocabularies differ: "
        f"type={sorted(CONTRACT_CHECKPOINT_STATES)} storage={sorted(stored)}"
    )


@pytest.mark.parametrize(
    "vocabulary,name",
    [
        (INPUT_DELIVERY_STATES, "messages.delivery_state"),
        (TOOL_RESULT_BINDING_STATES, "tool_result_binding_state"),
        (tuple(get_args(CheckpointStatus)), "checkpoints.status"),
    ],
)
def test_every_vocabulary_is_nonempty_and_unique(vocabulary, name) -> None:
    assert vocabulary, f"{name} declares no states"
    assert len(set(vocabulary)) == len(vocabulary), (
        f"{name} declares a duplicate state: {vocabulary}"
    )


def test_storage_enforces_the_declared_input_vocabulary() -> None:
    from infra.schema import SCHEMA_SQL

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES ('t','u','now','now')"
        )

        def insert(state: str, row_id: str) -> None:
            connection.execute(
                "INSERT INTO messages "
                "(id,thread_id,role,content,created_at,delivery_state) "
                "VALUES (?,'t','user','c','now',?)",
                (row_id, state),
            )

        for index, state in enumerate(INPUT_DELIVERY_STATES):
            insert(state, f"declared-{index}")
        for rejected in ("delivering", "paused", "undelivered", "persisted", ""):
            with pytest.raises(sqlite3.IntegrityError):
                insert(rejected, f"imposter-{rejected}")
    finally:
        connection.close()
