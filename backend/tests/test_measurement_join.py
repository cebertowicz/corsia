"""One page load's repeated Measurement reads run once (``api.measurement_join``).

``/where-to-look`` ranks the page's own ``/series`` and ``/agents`` payloads. An identical
read in flight is joined, and where-to-look alone may take one finished less than 30
seconds ago. The rules: one caller leaving never cancels the read for the others, the
last caller leaving cancels it and drops its key, a failure is never kept, results are
bounded, and the key names every input that can change a body.
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from api import measurement as page
from api import measurement_join as join
from api.measurement_cost import cost_read
from api.measurement_where_to_look import SERIES_IDS
from core.measurement_scope import resolve_grain
from test_measurement_agents import POST_BREAK, _freeze, _seed_agent, _seed_period, _seed_project, _seeded
from test_measurement_where_to_look import BEFORE, LATEST

# The page's batched ``/series`` id list: every panel's ids, the measures ranked here among them.
PAGE_IDS = ",".join(["outcomes", "active_hours_per_outcome", "asks_per_outcome", "took_recommendation_pct",
                     "answered_other_pct", "answered_neither_pct", "first_try_acceptance", "rejections_per_outcome",
                     "fresh_tokens_per_outcome", "estimated_human_hours_per_active_hour"])
USER = {"id": "steve"}


@pytest.fixture(autouse=True)
def _no_flights():
    join._flights.clear()
    yield
    join._flights.clear()


def _flights() -> dict:
    return join._flights.get(asyncio.get_running_loop(), {})


class _Read:
    """A read the test releases by hand, counting how often it ran."""

    def __init__(self, result=None, error: Exception | None = None):
        self.release = asyncio.Event()
        self.runs = 0
        self.cancelled = False
        self.result = {"rows": [1, 2, 3]} if result is None else result
        self.error = error

    async def __call__(self):
        self.runs += 1
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error:
            raise self.error
        return self.result


async def _settled():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_identical_reads_in_flight_run_once_and_share_the_result():
    read = _Read()
    callers = [asyncio.ensure_future(join.joined("k", read)) for _ in range(3)]
    await _settled()
    read.release.set()
    assert await asyncio.gather(*callers) == [read.result] * 3
    assert read.runs == 1


async def test_a_page_read_never_takes_a_finished_result_and_where_to_look_takes_one_inside_the_window(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(join.time, "monotonic", lambda: clock[0])
    read = _Read()
    read.release.set()
    await join.joined("k", read)
    await join.joined("k", read)
    assert read.runs == 2
    clock[0] += join.RETENTION_SECONDS - 1
    await join.joined("k", read, reuse_finished=True)
    assert read.runs == 2
    clock[0] += 1
    await join.joined("k", read, reuse_finished=True)
    assert read.runs == 3


async def test_the_first_caller_runs_the_read_in_its_own_request_before_yielding():
    read = _Read()
    first = asyncio.ensure_future(join.joined("k", read))
    await asyncio.sleep(0)
    assert read.runs == 1
    read.release.set()
    await first


async def test_a_waiting_caller_leaving_never_touches_the_read():
    read = _Read()
    first = asyncio.ensure_future(join.joined("k", read))
    second = asyncio.ensure_future(join.joined("k", read))
    await _settled()
    second.cancel()
    await _settled()
    assert second.cancelled() and not read.cancelled
    read.release.set()
    assert await first == read.result
    assert read.runs == 1


async def test_the_first_caller_leaving_hands_the_read_to_a_caller_still_waiting():
    read = _Read()
    first = asyncio.ensure_future(join.joined("k", read))
    second = asyncio.ensure_future(join.joined("k", read))
    third = asyncio.ensure_future(join.joined("k", read))
    await _settled()
    first.cancel()
    await _settled()
    assert first.cancelled() and read.cancelled and not second.done() and not third.done()
    read.release.set()
    assert await asyncio.gather(second, third) == [read.result] * 2
    assert read.runs == 2


async def test_the_last_caller_leaving_cancels_the_read_and_drops_its_key():
    read = _Read()
    callers = [asyncio.ensure_future(join.joined("k", read)) for _ in range(2)]
    await _settled()
    for caller in callers:
        caller.cancel()
    await _settled()
    assert read.cancelled and "k" not in _flights()
    again = _Read()
    again.release.set()
    assert await join.joined("k", again) == again.result and again.runs == 1


async def test_a_failure_reaches_every_caller_and_is_never_kept():
    read = _Read(error=RuntimeError("read failed"))
    callers = [asyncio.ensure_future(join.joined("k", read, reuse_finished=True)) for _ in range(2)]
    await _settled()
    read.release.set()
    results = await asyncio.gather(*callers, return_exceptions=True)
    assert all(result is read.error for result in results)
    assert "k" not in _flights()
    again = _Read()
    again.release.set()
    assert await join.joined("k", again, reuse_finished=True) == again.result


async def test_a_result_that_cannot_be_sized_still_reaches_every_caller_and_is_never_kept():
    read = _Read(result={(1, 2): "a tuple key json cannot write"})
    callers = [asyncio.ensure_future(join.joined("k", read, reuse_finished=True)) for _ in range(2)]
    await _settled()
    read.release.set()
    assert await asyncio.wait_for(asyncio.gather(*callers), timeout=1) == [read.result] * 2
    assert "k" not in _flights()
    assert await asyncio.wait_for(join.joined("k", read, reuse_finished=True), timeout=1) == read.result
    assert read.runs == 2


async def test_finished_results_are_bounded_by_count_and_size_oldest_first(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(join.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(join, "MAX_ENTRIES", 2)
    for key in ("a", "b", "c"):
        clock[0] += 1
        read = _Read()
        read.release.set()
        await join.joined(key, read)
    assert list(_flights()) == ["b", "c"]
    monkeypatch.setattr(join, "MAX_BYTES", 2 * len('{"rows": [1, 2, 3]}') - 1)
    read = _Read()
    read.release.set()
    clock[0] += 1
    await join.joined("d", read)
    assert list(_flights()) == ["d"]


def _varied(read, base: dict, variants: dict) -> None:
    """The key carries every parameter of the read, and changing any one changes it."""
    assert set(variants) == set(base) == set(inspect.signature(read).parameters)
    reference = join.key_of(read, **base)
    assert join.key_of(read, *base.values()) == reference, "a positional call keys differently"
    for name, value in variants.items():
        assert join.key_of(read, **{**base, name: value}) != reference, f"{read.__name__} key ignores {name}"


def test_the_series_key_names_every_input_of_the_series_read():
    days = resolve_grain("days", "UTC", now=POST_BREAK, averaged=True)
    _varied(page.series_payload.__wrapped__,
            {"requested": ["outcomes"], "project": "p1", "agent": "none", "page_grain": days, "user": USER},
            {"requested": ["asks_per_outcome"], "project": "p2", "agent": "a1",
             "page_grain": resolve_grain("days", "America/Los_Angeles", now=POST_BREAK, averaged=True),
             "user": {"id": "other"}})


def test_the_agents_key_names_every_input_of_the_agents_read():
    weeks = resolve_grain("weeks", "UTC", now=POST_BREAK)
    _varied(page.agents_payload.__wrapped__,
            {"project": "p1", "agent": "none", "page_grain": weeks, "sort": "ranked",
             "metric": "fresh_tokens_per_outcome", "dollars": True, "user": USER},
            {"project": "p2", "agent": "a1", "page_grain": resolve_grain("days", "UTC", now=POST_BREAK),
             "sort": "name", "metric": "asks_per_outcome", "dollars": False, "user": {"id": "other"}})


def test_two_reads_with_the_same_arguments_never_share_a_key():
    weeks = resolve_grain("weeks", "UTC", now=POST_BREAK)

    async def other(project, agent, page_grain, sort, metric, dollars, user):
        return {}

    arguments = ("p1", "none", weeks, "ranked", "fresh_tokens_per_outcome", True, USER)
    assert join.key_of(other, *arguments) != join.key_of(page.agents_payload.__wrapped__, *arguments)


def test_the_joined_reads_read_only_the_user_id_the_keys_carry():
    for read in (page.series_payload, page.agents_payload):
        assert set(re.findall(r"user\[[^\]]*\]", inspect.getsource(read))) == {'user["id"]'}


def test_the_page_id_list_carries_every_measure_ranked_here():
    assert set(SERIES_IDS) <= set(PAGE_IDS.split(","))


def _seed(db):
    _seed_project(db, "p1")
    for agent in ("worse", "better"):
        _seed_agent(db, agent, project_id="p1")
    _seed_period(db, "worse", BEFORE, 30, not_first_try=3)
    _seed_period(db, "worse", LATEST, 30, not_first_try=12)
    _seed_period(db, "better", BEFORE, 30, not_first_try=15)
    _seed_period(db, "better", LATEST, 30, not_first_try=3)


@pytest.mark.parametrize("grain", ["days", "weeks"])
async def test_where_to_look_joined_with_the_page_equals_where_to_look_alone(isolated_db, monkeypatch, grain):
    from api.measurement_where_to_look import measurement_where_to_look

    _freeze(monkeypatch, POST_BREAK)
    _seeded(_seed)
    reads = ("series_payload", "agents_payload", "models_read", "cost_read", "ledger_read", "task_chain_costs")
    runs = dict.fromkeys(reads, 0)
    lead = join._lead

    async def counted(flights, key, compute, loop):
        runs[key[1]] += 1  # the read's name: every read that actually ran
        return await lead(flights, key, compute, loop)

    monkeypatch.setattr(join, "_lead", counted)

    for project, agent in (("p1", "none"), ("p1", "worse"), ("all", "none")):
        scope = {"project": project, "agent": agent, "grain": grain, "timezone_name": "UTC", "user": USER}
        alone = await measurement_where_to_look(**scope)
        page_series = await page.measurement_series(ids=PAGE_IDS, **scope)
        join._flights.clear()
        runs.update(dict.fromkeys(reads, 0))
        # The page's Cost and Models panels ask for the one ledger together, and its Agents and Models panels for the
        # one set of task chains' costs: each is read once, not once each.
        cost_grain = page.resolve_grain(grain, "UTC", averaged=True)
        series, _, _, _, joined = await asyncio.gather(
            page.measurement_series(ids=PAGE_IDS, **scope), page.measurement_agents(**scope), page.measurement_models(**scope),
            cost_read(project, agent, cost_grain, USER), measurement_where_to_look(ids=PAGE_IDS, **scope))
        assert joined == alone and series == page_series
        assert runs == dict.fromkeys(reads, 1), (project, agent)
