"""AC41 wake sources, proven by driving the writers that produce them.

The claims here are about `continuation_triggers` and `llm_usage`: what counts
as an admission, what a delivery outcome is, where cost actually comes from, and
why dedupe cannot be counted at all.

These tests used to `INSERT` the very rows they then counted. That passes
against a lifecycle no production writer can produce — which is precisely the
failure the AC41 correction was supposed to close, committed one layer down. So
every row below is written by `triggers.enqueue`, settled by
`execution.deliver_one`, or recorded by `infra.llm_usage.record_llm_usage`.

Ablation, measured: with `triggers.claim` no longer incrementing `attempt`, the
seeded version of this file stayed green at 22 passed and the version below
fails on admission.
"""

from __future__ import annotations

import collections

import pytest

from tests.test_plan_observability_contract import (  # noqa: F401 - shared
    BACKEND,
    _sql_blocks,
    plans_section,
    seeded_db,
)


class _KeepOpen:
    """Hold the fixture connection open when a real writer closes its handle."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        return None


class _NoSourceEffects:
    """The settlement-source protocol with no effects of its own, so the durable
    trigger row is the only thing the AC41 tests below observe."""

    def settle(self, trigger, outcome, reason, *, db):
        return None


def _request(subject: str, thread_id: str = "owner-1"):
    """One continuation request shaped as a production source builds it."""
    from automation.continuation.models import TriggerRequest

    return TriggerRequest(
        kind="t0_liveness", thread_id=thread_id, scope_id="s1",
        subject_id=subject, transition="progress_advanced",
        reason="progress_advanced",
    )


async def _wake(db, subject: str, *, lands: bool):
    """Enqueue and drive one wake through the real `deliver_one`."""
    from automation.continuation import execution, triggers

    request = _request(subject)
    trigger, _created = triggers.enqueue(request, db=db, now="t")
    db.commit()

    async def deliver(*_args, **_kwargs):
        return lands

    await execution.deliver_one(
        trigger, request, _NoSourceEffects(), deliver,
        db=db, now="t", results=collections.defaultdict(list),
    )
    db.commit()
    return trigger


# --- what an admission is, and what it is not ------------------------------


@pytest.mark.asyncio
async def test_admission_counts_attempts_including_a_failed_delivery(
    plans_section, seeded_db,
):
    """Both wakes are driven through the real `deliver_one`, not seeded.

    `deliver_one` claims the trigger — which is what increments `attempt` — and
    delivers afterwards, so a wake that was admitted and then failed to land is
    still an admission. `state='delivered'` silently drops it; `SUM(attempt)`
    is the measure that does not. A hand-written `failed` row would satisfy this
    query against a lifecycle no production writer can actually produce.
    """
    await _wake(seeded_db, "lands", lands=True)
    await _wake(seeded_db, "does-not-land", lands=False)
    block = next(
        block for block in _sql_blocks(plans_section)
        if "continuation_triggers" in block
    )

    assert "SUM(attempt)" in block
    assert "cost_estimate_usd" not in block
    counted = {
        row["state"]: (row["reason"], row["admissions"])
        for row in seeded_db.execute(block).fetchall()
    }

    assert counted["delivered"] == ("progress_advanced", 1)
    assert counted["failed"] == ("delivery_failed", 1)
    assert sum(admissions for _reason, admissions in counted.values()) == 2


@pytest.mark.asyncio
async def test_the_trigger_cost_column_is_a_structural_zero(
    plans_section, seeded_db,
):
    """The document's *reason* has to be true, not just its remedy.

    `cost_estimate_usd` is not an estimate taken at the wrong moment — nothing
    writes it at all. Every `TriggerRequest` in the source directory omits the
    field, the dataclass default is `0.0`, and no path updates it after the
    insert, so it sums to zero over wakes written by the real enqueue path.
    """
    await _wake(seeded_db, "lands", lands=True)
    await _wake(seeded_db, "does-not-land", lands=False)
    sources = BACKEND / "automation" / "continuation" / "sources"
    setters = sorted(
        path.name for path in sources.glob("*.py")
        if "cost_estimate_usd" in path.read_text(encoding="utf-8")
    )

    total = seeded_db.execute(
        "SELECT SUM(cost_estimate_usd) AS total FROM continuation_triggers"
    ).fetchone()["total"]

    assert setters == []
    assert total == 0.0
    assert "structural zero" in plans_section.casefold()


def test_the_cost_query_reads_llm_usage_not_the_trigger_estimate(
    plans_section, seeded_db, monkeypatch,
):
    """Written by `infra.llm_usage.record_llm_usage`, the only production writer
    of the table, so the documented query is proven against the shape that
    writer actually produces."""
    from infra.llm_usage import record_llm_usage

    block = next(
        block for block in _sql_blocks(plans_section) if "llm_usage" in block
    )
    monkeypatch.setattr("infra.database.get_db", lambda: _KeepOpen(seeded_db))
    record_llm_usage(
        "owner-1", "test-model",
        {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
         "cost_usd": 1.25},
        _emit_product_event=lambda *args, **kwargs: None,
    )

    rows = seeded_db.execute(block).fetchall()

    assert [row["cost_usd"] for row in rows] == [1.25]
    assert [row["thread_id"] for row in rows] == ["owner-1"]


def test_dedupe_is_documented_as_unobservable(plans_section, seeded_db):
    """Driven through the real `enqueue`, twice, with one dedupe key.

    Suppression returns the existing row and inserts nothing, so one row and
    zero `suppressed` rows is exactly what a deduped wake looks like — and also
    what a never-duplicated wake looks like. The document must say that rather
    than count a state no writer produces.
    """
    from automation.continuation import triggers

    request = _request("duplicated")
    first, created_first = triggers.enqueue(request, db=seeded_db, now="t")
    second, created_second = triggers.enqueue(request, db=seeded_db, now="t")
    seeded_db.commit()
    lowered = plans_section.casefold()

    assert (created_first, created_second) == (True, False)
    assert first.id == second.id
    rows = seeded_db.execute(
        "SELECT COUNT(*) AS n FROM continuation_triggers WHERE dedupe_key=?",
        (request.dedupe_key,),
    ).fetchone()["n"]
    suppressed = seeded_db.execute(
        "SELECT COUNT(*) AS n FROM continuation_triggers WHERE state='suppressed'"
    ).fetchone()["n"]

    assert (rows, suppressed) == (1, 0)
    assert "not observable" in lowered or "unobservable" in lowered
    assert "suppressed" in plans_section
