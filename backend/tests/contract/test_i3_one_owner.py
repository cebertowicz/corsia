"""I3 — Turn and input state has one explicit transition layer.

The clean runtime owns each durable transition in a small, named ``infra``
module. This scan prevents an HTTP route, provider adapter, or maintenance job
from becoming a second writer while keeping birth, provider-write settlement,
Turn settlement, model retargeting, claims, and startup recovery separate and readable.
"""

from __future__ import annotations

import re

import pytest

from tests.contract.antivacuity import require_evidence
from tests.contract.conftest import BACKEND_ROOT, iter_backend_sources

pytestmark = pytest.mark.contract

TRANSITION_WRITERS = {
    "infra/input_admission.py",
    "infra/input_writes.py",
    "infra/model_switches.py",
    # The model-switch writer's held-suffix SQL is extracted for the file-size
    # boundary; it requires that existing transaction and cannot open its own.
    "infra/model_switch_inputs.py",
    "infra/thread_driver.py",
    "infra/thread_inputs.py",
    # Shared assignment requires the existing idle or safe-boundary claim writer.
    "infra/input_turn_assignment.py",
    # Claim-associated login stamping and detach expressions, never another dispatch.
    "infra/vendor_login.py",
    # One owner for the Turn's write-once plan-attempt binding. Two Plans
    # callers need this exact transition (selection opened by a plan mutation,
    # review verification), so its column list lives in one place; it is
    # accounting attribution, never execution authority.
    "infra/turn_plan_binding.py",
    # One owner for the Turn's durable provider-usage handoff. Every harness
    # records through this module before the independently failing ledger
    # projection consumes the receipt.
    "infra/turn_usage_receipts.py",
    # One owner for the Turn's guidance-list binding (``context_manifest_id``
    # and its recording status). ``core.guidance_manifest`` decides the list;
    # this writes it. Attribution only, never execution authority.
    "infra/turn_manifest_binding.py",
    # One owner for ending a queued input in place. A user's cancellation and a
    # plan wake whose work moved on leave the same durable row, each named by
    # its own delivery_error, so the FIFO keeps a single cancellation writer.
    "infra/queued_input_settlement.py",
    # The input writer's checkpoint-refusal classification is extracted for the
    # file-size boundary; it requires that existing transaction and cannot open
    # its own, and it settles only rows that writer already claimed.
    "infra/checkpoint_refusal_settlement.py",
}

_WRITE_RE = re.compile(
    r"(?:UPDATE|INSERT(?:\s+OR\s+\w+)?\s+INTO|DELETE\s+FROM|REPLACE\s+INTO)\s+"
    r"(active_turns|messages)\b",
    re.IGNORECASE,
)
_INTERPOLATED_WRITE_RE = re.compile(
    r"(?:UPDATE|INSERT(?:\s+OR\s+\w+)?\s+INTO|DELETE\s+FROM|REPLACE\s+INTO)\s+"
    r"\{\s*\w+\s*\}",
    re.IGNORECASE,
)
_OWNED_TABLE_NAME = re.compile(r"\b(active_turns|messages)\b")
_MESSAGE_DELIVERY_COLUMNS = re.compile(
    r"delivery_state|delivery_error|delivery_revision|delivering_reason|"
    r"delivery_blocked_since|next_attempt_at|input_order|delivery_intent|"
    r"active_turn_id|delivery_attempt_id|write_started_at|write_outcome",
)
_INTERPOLATION_CONTEXT_CHARS = 600


def _owned_write(statement_window: str, table: str) -> bool:
    """Exclude ordinary transcript inserts that do not own input delivery."""
    return table.lower() != "messages" or bool(
        _MESSAGE_DELIVERY_COLUMNS.search(statement_window)
    )


def test_owned_state_has_only_enrolled_transition_writers() -> None:
    violations: list[str] = []
    found: set[str] = set()
    scanned = 0
    for path, source in iter_backend_sources():
        scanned += 1
        rel = str(path.relative_to(BACKEND_ROOT))
        for match in _WRITE_RE.finditer(source):
            window = source[match.start():match.start() + 500]
            if not _owned_write(window, match.group(1)):
                continue
            found.add(rel)
            if rel not in TRANSITION_WRITERS:
                line = source.count("\n", 0, match.start()) + 1
                violations.append(f"{rel}:{line} writes {match.group(1)}")
        for match in _INTERPOLATED_WRITE_RE.finditer(source):
            back = source[
                max(0, match.start() - _INTERPOLATION_CONTEXT_CHARS):match.start()
            ]
            if not _OWNED_TABLE_NAME.search(back):
                continue
            found.add(rel)
            if rel not in TRANSITION_WRITERS:
                line = source.count("\n", 0, match.start()) + 1
                violations.append(f"{rel}:{line} writes an owned interpolated table")
    require_evidence(
        range(scanned),
        scan="Turn/input transition writer scan",
        expectation="backend source files to read",
        replacement_hint="check iter_backend_sources' exclusion list",
    )
    missing = sorted(TRANSITION_WRITERS - found)
    assert not missing, f"enrolled transition writers with no detected write: {missing}"
    assert not violations, (
        "Turn/input state writes outside the explicit transition layer:\n"
        + "\n".join(sorted(violations))
    )


def test_turn_plan_binding_knows_every_role_the_schema_allows() -> None:
    """The guard must not be a second, narrower vocabulary.

    It first shipped with only {execution, verification} and rejected the
    'delegation' role the schema has always allowed, so a child's Turn on a
    parent commitment raised instead of binding.
    """
    import re as _re

    from infra.schema import SCHEMA_SQL
    from infra.turn_plan_binding import USAGE_ROLES

    checks = _re.findall(
        r"plan_usage_role IN \(([^)]*)\)", SCHEMA_SQL, _re.IGNORECASE,
    )
    assert checks, "plan_usage_role CHECK not found in the schema"
    for clause in checks:
        allowed = {value.strip().strip("'") for value in clause.split(",")}
        assert allowed == set(USAGE_ROLES), (
            f"schema allows {sorted(allowed)} but the binding guard accepts "
            f"{sorted(USAGE_ROLES)}"
        )
