"""Daily movement report: validation and routing to the scope's conductor."""

from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import copy

import pytest

from connections import binder
from events.movement_report import (
    MalformedMovementReport, MovementReport, deliver_movement_report_sync,
)
from events.platform import DEAD_LETTERED, DIGESTED, DUPLICATE, WOKE
from infra.conductor import WORKSPACE_SCOPE, set_scope_coordinator
from infra.database import get_db
from tests.event_routing_helpers import mk_session

CHANGE = {"kind": "skill", "ref": "skills/attention-watch", "at": "2026-10-14T08:00:00+00:00"}
MOVE = {
    "series_id": "person_attention.agent-a", "owner_agent": "agent-a",
    "value": 9.0, "normal_range": [1.0, 4.0], "preceding_changes": [CHANGE],
}


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


def _report(scope="workspace", day="2026-10-15", breach=False, moves=None):
    return {"scope": scope, "report_date": day, "breach": breach,
            "moves": [copy.deepcopy(MOVE)] if moves is None else moves}


def _count(sql, *args):
    db = get_db()
    try:
        return db.execute(sql, args).fetchone()[0]
    finally:
        db.close()


def _wakes(thread):
    return _count(
        "SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'",
        thread)


def _notes(thread):
    return _count(
        "SELECT COUNT(*) FROM context_change_journal "
        "WHERE object_kind='thread_context' AND object_id=?", thread)


@pytest.fixture
def workspace(isolated_db):
    thread = mk_session("ws")
    assert set_corsia_agent(thread)
    return thread


@pytest.fixture
def project(isolated_db):
    db = get_db()
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) "
               "VALUES ('proj-1','P','t','t')")
    db.commit()
    db.close()
    thread = mk_session("proj-conductor")
    assert set_project_conductor("proj-1", thread)
    return thread


class TestRouting:
    def test_no_move_day_is_a_passive_digest_only(self, workspace):
        out = deliver_movement_report_sync(_report(moves=[]))
        assert [(d.outcome, d.thread_id) for d in out] == [(DIGESTED, workspace)]
        assert (_wakes(workspace), _notes(workspace)) == (0, 1)

    def test_move_without_breach_is_digested(self, workspace):
        out = deliver_movement_report_sync(_report())
        assert [d.outcome for d in out] == [DIGESTED]

    def test_breach_wakes_once(self, workspace):
        out = deliver_movement_report_sync(_report(breach=True))
        assert [(d.outcome, d.thread_id) for d in out] == [(WOKE, workspace)]
        assert _wakes(workspace) == 1

    def test_repeat_is_a_noop(self, workspace):
        first = deliver_movement_report_sync(_report(breach=True))
        again = deliver_movement_report_sync(_report(breach=True))
        assert [d.outcome for d in first] == [WOKE]
        assert [d.outcome for d in again] == [DUPLICATE]
        assert _wakes(workspace) == 1

    def test_digest_repeat_is_a_noop(self, workspace):
        deliver_movement_report_sync(_report())
        again = deliver_movement_report_sync(_report())
        assert [d.outcome for d in again] == [DUPLICATE]
        assert _notes(workspace) == 1

    def test_designation_move_reaches_new_owner_next_day(self, workspace):
        deliver_movement_report_sync(_report(day="2026-10-15", breach=True))
        other = mk_session("ws2")
        assert set_corsia_agent(other)
        out = deliver_movement_report_sync(_report(day="2026-10-16", breach=True))
        assert [(d.outcome, d.thread_id) for d in out] == [(WOKE, other)]

    def test_project_report_reaches_its_conductor(self, workspace, project):
        out = deliver_movement_report_sync(_report(scope="proj-1", breach=True))
        assert [(d.outcome, d.thread_id) for d in out] == [(WOKE, project)]
        assert _wakes(workspace) == 0

    def test_project_designation_follows_the_conductor(self, workspace, project):
        deliver_movement_report_sync(_report(scope="proj-1", day="2026-10-15"))
        other = mk_session("proj-conductor-2")
        assert set_project_conductor("proj-1", other)
        out = deliver_movement_report_sync(
            _report(scope="proj-1", day="2026-10-16", breach=True))
        assert [(d.outcome, d.thread_id) for d in out] == [(WOKE, other)]

    def test_no_conductor_dead_letters_once(self, isolated_db):
        first = deliver_movement_report_sync(_report(breach=True))
        again = deliver_movement_report_sync(_report(breach=True))
        assert [d.outcome for d in first] == [DEAD_LETTERED]
        assert [d.outcome for d in again] == [DUPLICATE]
        assert _count("SELECT COUNT(*) FROM dead_letter_events") == 1

    def test_designation_rules_are_one_per_scope(self, workspace, project):
        deliver_movement_report_sync(_report(day="2026-10-15"))
        deliver_movement_report_sync(_report(day="2026-10-16"))
        deliver_movement_report_sync(_report(scope="proj-1"))
        assert len(binder.list_rules("measurement")) == 2


class TestValidation:
    @pytest.mark.parametrize("mutate", [
        lambda r: r.pop("scope"),
        lambda r: r.update(scope=""),
        lambda r: r.update(report_date="15/10/2026"),
        lambda r: r.update(breach="yes"),
        lambda r: r.update(breach=True, moves=[]),
        lambda r: r.update(extra=1),
        lambda r: r["moves"][0].update(value="9"),
        lambda r: r["moves"][0].update(normal_range=[4.0, 1.0]),
        lambda r: r["moves"][0].update(normal_range=[1.0]),
        lambda r: r["moves"][0].pop("owner_agent"),
        lambda r: r["moves"][0]["preceding_changes"][0].update(kind="cause"),
        lambda r: r["moves"][0]["preceding_changes"][0].update(at="yesterday"),
        lambda r: r["moves"][0]["preceding_changes"][0].pop("ref"),
    ])
    def test_malformed_raises_and_delivers_nothing(self, workspace, mutate):
        raw = _report()
        mutate(raw)
        with pytest.raises(MalformedMovementReport):
            deliver_movement_report_sync(raw)
        assert _notes(workspace) == 0 and _wakes(workspace) == 0

    def test_typed_report_round_trips(self):
        report = MovementReport.parse(_report(breach=True))
        assert MovementReport.parse(report.as_contract()) == report
