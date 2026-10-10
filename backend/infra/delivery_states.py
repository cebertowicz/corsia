"""The one place a message row's durable state vocabularies are enumerated.

I4 says every lifecycle must be countable. That claim is only true if there is
exactly ONE list per lifecycle: the storage CHECK, the insert validator, the
migration backfill, the SQL that selects by state, and the contract scan must
all read the same names. Restating a vocabulary in a second place is how the
"countable" claim decays into a docstring and storage can diverge from runtime.

Nothing here imports anything, so ``infra.schema`` can generate its CHECK
constraints from these tuples without an import cycle and every transition
writer can validate against the same names.

**State carries the state; the reason columns carry the reason.** An input is
queued, delivered, ambiguous, cancelled, or failed. Details stay in the
reason/evidence columns instead of becoming more lifecycle states.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# messages.delivery_state — the input delivery lifecycle
# ---------------------------------------------------------------------------
#
# ``queued``       admitted and durable, not yet in front of the model. NULL
#                  seq: transcript-ineligible until an explicit transition.
#                  A queued row that is mid-attempt, paced, or blocked is still
#                  ``queued`` — ``delivering_reason`` names which.
# ``delivered``    reached the model and holds a sequence in the transcript.
# ``ambiguous``    provider acceptance could not be proved either way.
# ``cancelled``    proved not written and stopped: by the user, or as a plan
#                  wake whose work moved on before a Turn could claim it.
# ``failed``       proved not written and ended with ``delivery_error``.
#
# Canonical order: waiting, succeeded, uncertain, stopped, failed.
INPUT_DELIVERY_STATES: tuple[str, ...] = (
    "queued",
    "delivered",
    "ambiguous",
    "cancelled",
    "failed",
)
INPUT_DELIVERY_STATE_SET = frozenset(INPUT_DELIVERY_STATES)

#: The terminal failure state used by generic failed-delivery insertion.
INPUT_DELIVERY_ENDED: str = "failed"

# ---------------------------------------------------------------------------
# messages.metadata.$.tool_result_binding_state — the tool-result lifecycle
# ---------------------------------------------------------------------------
#
# ``provisional``         the canonical result row is durable BEFORE the
#                         provider echoes its call id. This is the birth state
#                         and the whole point of the durable bridge: a crash
#                         between execution and echo must not lose the result.
# ``bound``               the provider echo arrived and matched; the row owns
#                         ``tool_call_id`` and is replay-eligible.
# ``quarantined_unbound`` the owning Turn ended while still provisional, so no
#                         echo can ever arrive. Strict replay excludes the row
#                         and the declaring call keeps its interruption stub.
#
# Canonical order: birth, bound, ended-unbound.
TOOL_RESULT_BINDING_STATES: tuple[str, ...] = (
    "provisional",
    "bound",
    "quarantined_unbound",
)

#: Birth state: persisted, awaiting the provider echo.
TOOL_RESULT_PROVISIONAL: str = "provisional"
#: The provider echo arrived and matched the declared call.
TOOL_RESULT_BOUND: str = "bound"
#: The owning Turn ended first; no echo can arrive, so the row is excluded.
TOOL_RESULT_QUARANTINED_UNBOUND: str = "quarantined_unbound"


def sql_state_literals(states: tuple[str, ...]) -> str:
    """Render a vocabulary as the body of a SQL ``IN`` list.

    The storage CHECK is GENERATED from the tuple rather than restated beside
    it, so a vocabulary change cannot land in Python while SQL keeps the old
    domain.
    """
    return ", ".join(f"'{state}'" for state in states)
