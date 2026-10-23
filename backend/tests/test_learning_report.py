"""Learning-status report validation, delivery, and scope routing."""

from __future__ import annotations

import copy

import pytest

from events.envelope import normalize
from events.learning_report import MalformedLearningReport, deliver_learning_report_sync
from events.platform import DIGESTED, DUPLICATE, WOKE
from infra.conductor import set_project_conductor
from infra.corsia_agent import set_corsia_agent
from infra.database import get_db
from tests.event_routing_helpers import mk_session


def _count(value=1, refs=None):
    return {"count": value, "refs": refs or ["record:one"]}


def _report(scope="workspace", day="2026-10-23", suffix="daily", wake=True):
    return {
        "scope": scope,
        "report_date": day,
        "report_key": f"{scope}:{day}:{suffix}",
        "wake": wake,
        "items": {
            "corrections": {"person": _count(2)},
            "lessons": {name: _count() for name in (
                "recorded", "confirmed", "consolidated", "repeated")},
            "experiments": {name: _count() for name in ("started", "decided")},
            "wins": None,
        },
    }


def _count_rows(sql, *args):
    db = get_db()
    try:
        return db.execute(sql, args).fetchone()[0]
    finally:
        db.close()


def _wakes(thread):
    return _count_rows(
        "SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'",
        thread)


def _notes(thread):
    return _count_rows(
        "SELECT COUNT(*) FROM context_change_journal "
        "WHERE object_kind='thread_context' AND object_id=?", thread)


@pytest.fixture
def workspace(isolated_db):
    thread = mk_session("corsia-agent")
    assert set_corsia_agent(thread)
    return thread


@pytest.fixture
def project(isolated_db):
    db = get_db()
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) "
               "VALUES ('proj-1','P','t','t')")
    db.commit()
    db.close()
    thread = mk_session("project-conductor")
    assert set_project_conductor("proj-1", thread)
    return thread


class TestDelivery:
    def test_daily_and_early_reports_have_distinct_artifacts(self, workspace):
        daily = _report(suffix="daily")
        early = _report(suffix="early-1")
        first = deliver_learning_report_sync(daily)
        second = deliver_learning_report_sync(early)

        assert [delivery.outcome for delivery in first] == [WOKE]
        assert [delivery.outcome for delivery in second] == [WOKE]
        assert _wakes(workspace) == 2
        daily_event = normalize("measurement", daily, "measurement.learning_report")
        early_event = normalize("measurement", early, "measurement.learning_report")
        assert daily_event.facts["wake"] is True and "breach" not in daily_event.facts
        assert daily_event.artifact_ref == {
            "integration": "measurement", "type": "learning-report",
            "external_id": daily["report_key"],
        }
        assert early_event.artifact_ref == {
            "integration": "measurement", "type": "learning-report",
            "external_id": early["report_key"],
        }

    def test_repeat_key_is_a_noop(self, workspace):
        report = _report()
        assert [delivery.outcome for delivery in deliver_learning_report_sync(report)] == [WOKE]
        assert [delivery.outcome for delivery in deliver_learning_report_sync(report)] == [DUPLICATE]
        assert _wakes(workspace) == 1

    def test_wake_selects_actionable_or_passive_delivery(self, workspace):
        assert [delivery.outcome for delivery in deliver_learning_report_sync(
            _report(wake=True))] == [WOKE]
        assert [delivery.outcome for delivery in deliver_learning_report_sync(
            _report(day="2026-10-24", wake=False))] == [DIGESTED]
        assert (_wakes(workspace), _notes(workspace)) == (1, 1)

    def test_passive_note_line_carries_the_items(self, workspace):
        deliver_learning_report_sync(_report(wake=False))
        db = get_db()
        try:
            (line,) = db.execute(
                "SELECT summary FROM context_change_journal "
                "WHERE object_kind='thread_context' AND object_id=?", (workspace,)).fetchone()
        finally:
            db.close()
        assert 'items={"corrections":{"person":{"count":2,' in line
        assert '"wins":null' in line and "wake=" not in line

    def test_project_scope_reaches_its_conductor(self, workspace, project):
        assert [(delivery.outcome, delivery.thread_id) for delivery in deliver_learning_report_sync(
            _report(scope="proj-1"))] == [(WOKE, project)]
        assert _wakes(workspace) == 0


class TestValidation:
    @pytest.mark.parametrize("mutate", [
        lambda report: report.update(report_date="20261023"),
        lambda report: report.update(report_key="workspace:2026-10-23:early-3"),
        lambda report: report.update(report_key="other:2026-10-23:daily"),
        lambda report: report.update(report_key="workspace:2026-10-24:daily"),
        lambda report: report.pop("wake"),
        lambda report: report.update(extra=True),
        lambda report: report["items"]["lessons"].update(recorded=_count(-1)),
        lambda report: report["items"]["lessons"].update(recorded=_count(True)),
    ])
    def test_malformed_report_delivers_nothing(self, workspace, mutate):
        report = copy.deepcopy(_report())
        mutate(report)

        with pytest.raises(MalformedLearningReport):
            deliver_learning_report_sync(report)
        assert (_wakes(workspace), _notes(workspace)) == (0, 0)

    def test_refs_are_bounded_and_non_empty_strings(self, workspace):
        report = _report()
        report["items"]["wins"] = _count(1, ["x"] * 51)
        with pytest.raises(MalformedLearningReport):
            deliver_learning_report_sync(report)
        report["items"]["wins"] = _count(1, [""])
        with pytest.raises(MalformedLearningReport):
            deliver_learning_report_sync(report)
        assert (_wakes(workspace), _notes(workspace)) == (0, 0)
