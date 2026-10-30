"""Measurement Limits uses the canonical provider classifier and turn order."""
from __future__ import annotations

from datetime import datetime, timezone
import time

import pytest


def _pin(monkeypatch, year: int, month: int, day: int) -> None:
    """Pin the page's clock to noon UTC on a date."""
    monkeypatch.setattr("core.measurement_scope._now", lambda: datetime(year, month, day, 12, tzinfo=timezone.utc))


def _params(kind: str, year: int, month: int, day: int, **scope) -> dict:
    from core.measurement_scope import resolve_grain

    base = {"user_id": "steve", "scope_project": "project-a", "scope_agent": None}
    return {**base, **scope, "grain": resolve_grain(kind, "UTC", now=datetime(year, month, day, 12, tzinfo=timezone.utc))}


def _thread(db, thread_id: str, project: str, *, agent_enabled: bool = False) -> None:
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING", (project, project, 't', 't'))
    db.execute(
        "INSERT INTO threads (id,user_id,title,project_id,agent_enabled,agent_status,created_at,updated_at) VALUES (?,?,?,?,?,'active',?,?)",
        (thread_id, 'steve', thread_id, project, int(agent_enabled), 't', 't'),
    )


def _turn(db, turn_id: str, thread_id: str, model: str, status: str, started: str, ended: str | None, error: str | None, account: str | None) -> None:
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,ended_at,error,boot_epoch,vendor_account_id) VALUES (?,?,0,?,?,?,?,?,?,?)",
        (turn_id, thread_id, model, status, started, ended, error, 'test', account),
    )


def test_fetch_limit_hits_classifies_each_next_turn_and_calls_canonical_classifier(isolated_db, monkeypatch):
    from api import measurement_limits
    from infra.database import get_db

    db = get_db()
    try:
        _thread(db, 'root', 'project-a')
        accounts = [row['id'] for row in db.execute('SELECT id FROM vendor_accounts LIMIT 2')]
        a, b = accounts
        base = '2026-09-29T12:00:00Z'
        _turn(db, 'retry-limit', 'root', 'model-a', 'error', base, base, 'usage limit', a)
        _turn(db, 'retry-next', 'root', 'model-a', 'completed', '2026-09-29T12:01:00Z', '2026-09-29T12:02:00Z', None, a)
        _turn(db, 'model-limit', 'root', 'model-a', 'error', '2026-09-29T12:03:00Z', '2026-09-29T12:03:00Z', 'quota exhausted', a)
        _turn(db, 'model-next', 'root', 'model-b', 'completed', '2026-09-29T12:04:00Z', '2026-09-29T12:05:00Z', None, a)
        _turn(db, 'account-limit', 'root', 'model-a', 'error', '2026-09-29T12:06:00Z', '2026-09-29T12:06:00Z', 'weekly limit', a)
        _turn(db, 'account-next', 'root', 'model-a', 'completed', '2026-09-29T12:07:00Z', '2026-09-29T12:08:00Z', None, b)
        _turn(db, 'not-a-limit', 'root', 'model-a', 'error', '2026-09-29T12:09:00Z', '2026-09-29T12:09:00Z', 'temporary network error', a)
        _turn(db, 'last-limit', 'root', 'model-a', 'error', '2026-09-29T12:10:00Z', '2026-09-29T12:10:00Z', 'usage cap', a)
        db.commit()
        called: list[object] = []
        original = measurement_limits.is_provider_usage_or_quota_limit
        monkeypatch.setattr(measurement_limits, 'is_provider_usage_or_quota_limit', lambda value: called.append(value) or original(value))
        hits = measurement_limits.fetch_limit_hits(db, _params('weeks', 2026, 10, 6))
    finally:
        db.close()

    assert [hit['next'] for hit in hits] == ['retried', 'moved_model', 'moved_account', 'no_next_turn']
    assert len(called) == 5
    assert all(hit['next'] != 'unknown_next' for hit in hits)
    assert hits[0]['wait_seconds'] == 60


def test_limit_hits_respect_scope_and_completed_week_boundary(isolated_db):
    from api.measurement_limits import limits_by_period
    from infra.database import get_db

    db = get_db()
    try:
        _thread(db, 'included', 'project-a')
        _thread(db, 'excluded', 'project-b')
        _turn(db, 'before', 'included', 'model', 'error', '2026-09-27T23:59:00Z', '2026-09-27T23:59:00Z', 'usage limit', None)
        _turn(db, 'inside', 'included', 'model', 'error', '2026-09-28T00:00:00Z', '2026-09-28T00:00:00Z', 'usage limit', None)
        _turn(db, 'other-scope', 'excluded', 'model', 'error', '2026-09-29T00:00:00Z', '2026-09-29T00:00:00Z', 'usage limit', None)
        db.commit()
        counts = limits_by_period(db, _params('weeks', 2026, 10, 6))
    finally:
        db.close()

    # The Sunday-night hit stays in its own period; the other project's hit is not counted.
    assert counts == {'2026-09-21': 1, '2026-09-28': 1}


def test_naive_utc_sunday_stays_in_its_completed_week_outside_utc(isolated_db, monkeypatch):
    from api.measurement_limits import fetch_limit_hits, limit_summary, limits_by_period
    from infra.database import get_db

    monkeypatch.setenv('TZ', 'America/Los_Angeles')
    time.tzset()
    db = get_db()
    try:
        _thread(db, 'root', 'project-a')
        _turn(db, 'sunday-limit', 'root', 'model', 'error', '2026-10-04T23:30:00', '2026-10-04T23:30:00', 'usage limit', None)
        db.commit()
        params = _params('weeks', 2026, 10, 6)
        counts = limits_by_period(db, params)
        summary = limit_summary(fetch_limit_hits(db, params))
    finally:
        db.close()
        monkeypatch.undo()
        time.tzset()

    assert counts == {'2026-09-28': 1}
    assert counts['2026-09-28'] == summary['hits']


@pytest.mark.asyncio
async def test_series_serves_limit_hits_from_the_existing_weekly_rows(isolated_db, monkeypatch):
    from api.measurement import measurement_series
    from infra.database import get_db

    _pin(monkeypatch, 2026, 10, 6)
    db = get_db()
    try:
        _thread(db, 'root', 'project-a')
        _turn(db, 'hit', 'root', 'model', 'error', '2026-09-29T00:00:00Z', '2026-09-29T00:00:00Z', 'usage limit', None)
        db.commit()
    finally:
        db.close()

    served = await measurement_series('limit_hits', project='project-a', agent='none', grain="weeks", timezone_name="UTC", user={'id': 'steve'})
    assert [(row['period'], row['value']) for row in served['rows']] == [('2026-09-28', 1)]


@pytest.mark.asyncio
async def test_latest_limits_payload_excludes_a_prior_period_hit_and_wait(isolated_db, monkeypatch):
    from api import measurement_limits
    from infra.database import get_db

    _pin(monkeypatch, 2026, 10, 7)
    db = get_db()
    try:
        _thread(db, 'root', 'project-a')
        _turn(db, 'prior', 'root', 'model', 'error', '2026-09-29T00:00:00Z', '2026-09-29T00:00:00Z', 'usage limit', None)
        _turn(db, 'prior-next', 'root', 'model', 'completed', '2026-09-29T02:00:00Z', '2026-09-29T02:01:00Z', None, None)
        _turn(db, 'latest', 'root', 'model', 'error', '2026-10-06T00:00:00Z', '2026-10-06T00:00:00Z', 'usage limit', None)
        _turn(db, 'latest-next', 'root', 'model', 'completed', '2026-10-06T00:01:00Z', '2026-10-06T00:02:00Z', None, None)
        db.commit()
    finally:
        db.close()

    served = await measurement_limits.measurement_limits(project='project-a', agent='none', grain="days", timezone_name="UTC", user={'id': 'steve'})
    assert served['period']['latest'] == {'from': '2026-09-30', 'to': '2026-10-06'}
    assert served['hits'] == 1
    assert served['previous_hits'] == 1
    assert served['median_wait_seconds'] == 60
    assert served['p90_wait_seconds'] == 60


def test_limit_hits_leave_missing_next_identity_unclassified(isolated_db):
    from api.measurement_limits import fetch_limit_hits, limit_summary
    from infra.database import get_db

    db = get_db()
    try:
        for thread_id in ('both-missing', 'account-missing', 'next-account-missing'):
            _thread(db, thread_id, 'project-a')
        _turn(db, 'both-missing-limit', 'both-missing', 'model', 'error', '2026-09-29T00:00:00Z', '2026-09-29T00:00:00Z', 'usage limit', None)
        _turn(db, 'both-missing-next', 'both-missing', 'model', 'completed', '2026-09-29T00:01:00Z', '2026-09-29T00:02:00Z', None, None)
        _turn(db, 'account-missing-limit', 'account-missing', 'model', 'error', '2026-09-29T00:00:00Z', '2026-09-29T00:00:00Z', 'usage limit', None)
        _turn(db, 'account-missing-next', 'account-missing', 'model', 'completed', '2026-09-29T00:01:00Z', '2026-09-29T00:02:00Z', None, 'known')
        _turn(db, 'next-account-missing-limit', 'next-account-missing', 'model', 'error', '2026-09-29T00:00:00Z', '2026-09-29T00:00:00Z', 'usage limit', 'known')
        _turn(db, 'next-account-missing-next', 'next-account-missing', 'model', 'completed', '2026-09-29T00:01:00Z', '2026-09-29T00:02:00Z', None, None)
        db.commit()
        summary = limit_summary(fetch_limit_hits(db, _params('weeks', 2026, 10, 6)))
    finally:
        db.close()

    assert summary['hits'] == 3
    assert summary['next'] == {'retried': 0, 'moved_model': 0, 'moved_account': 0, 'no_next_turn': 0, 'unknown_next': 3}


@pytest.mark.asyncio
async def test_series_starts_limit_zeroes_at_the_first_scoped_turn_not_the_first_hit(isolated_db, monkeypatch):
    from api import measurement
    from infra.database import get_db

    _pin(monkeypatch, 2026, 10, 5)
    db = get_db()
    try:
        _thread(db, 'root', 'project-a')
        _turn(db, 'recorded', 'root', 'model', 'completed', '2026-09-08T00:00:00Z', '2026-09-08T00:01:00Z', None, 'account')
        _turn(db, 'limit', 'root', 'model', 'error', '2026-09-22T00:00:00Z', '2026-09-22T00:00:00Z', 'usage limit', 'account')
        db.commit()
    finally:
        db.close()

    served = await measurement.measurement_series('limit_hits', project='project-a', agent='none', grain="weeks", timezone_name="UTC", user={'id': 'steve'})
    assert [(row['period'], row['value']) for row in served['rows']] == [
        ('2026-09-07', 0), ('2026-09-14', 0), ('2026-09-21', 1), ('2026-09-28', 0),
    ]


@pytest.mark.asyncio
async def test_series_keeps_the_requested_window_after_prior_scoped_activity_without_hits(isolated_db, monkeypatch):
    from api import measurement
    from infra.database import get_db

    _pin(monkeypatch, 2027, 1, 4)
    db = get_db()
    try:
        _thread(db, 'root', 'project-a')
        _turn(db, 'recorded', 'root', 'model', 'completed', '2026-09-07T00:00:00Z', '2026-09-07T00:01:00Z', None, 'account')
        db.commit()
    finally:
        db.close()

    served = await measurement.measurement_series('limit_hits', project='project-a', agent='none', grain="weeks", timezone_name="UTC", user={'id': 'steve'})
    assert len(served['rows']) == 12
    assert {(row['period'], row['value']) for row in served['rows']} == {
        ('2026-10-12', 0), ('2026-10-19', 0), ('2026-10-26', 0), ('2026-11-02', 0),
        ('2026-11-09', 0), ('2026-11-16', 0), ('2026-11-23', 0), ('2026-11-30', 0),
        ('2026-12-07', 0), ('2026-12-14', 0), ('2026-12-21', 0), ('2026-12-28', 0),
    }


@pytest.mark.asyncio
async def test_series_omits_periods_before_the_limit_instrument_existed(isolated_db, monkeypatch):
    from api import measurement
    from infra.database import get_db

    _pin(monkeypatch, 2026, 8, 24)
    db = get_db()
    try:
        _thread(db, 'root', 'project-a')
        _turn(db, 'recorded', 'root', 'model', 'completed', '2026-08-10T00:00:00Z', '2026-08-10T00:01:00Z', None, 'account')
        db.commit()
    finally:
        db.close()

    served = await measurement.measurement_series('limit_hits', project='project-a', agent='none', grain="weeks", timezone_name="UTC", user={'id': 'steve'})
    assert [(row['period'], row['value']) for row in served['rows']] == [('2026-08-10', 0), ('2026-08-17', 0)]


@pytest.mark.asyncio
async def test_series_does_not_extend_limit_history_from_another_project_or_agent(isolated_db, monkeypatch):
    from api import measurement
    from infra.database import get_db

    _pin(monkeypatch, 2026, 10, 5)
    db = get_db()
    try:
        _thread(db, 'selected', 'project-a', agent_enabled=True)
        _thread(db, 'other-agent', 'project-a', agent_enabled=True)
        _thread(db, 'other-project', 'project-b')
        _turn(db, 'other-agent-recorded', 'other-agent', 'model', 'completed', '2026-08-10T00:00:00Z', '2026-08-10T00:01:00Z', None, 'account')
        _turn(db, 'other-project-recorded', 'other-project', 'model', 'completed', '2026-08-10T00:00:00Z', '2026-08-10T00:01:00Z', None, 'account')
        _turn(db, 'selected-recorded', 'selected', 'model', 'completed', '2026-09-22T00:00:00Z', '2026-09-22T00:01:00Z', None, 'account')
        db.commit()
    finally:
        db.close()

    served = await measurement.measurement_series('limit_hits', project='project-a', agent='selected', grain="weeks", timezone_name="UTC", user={'id': 'steve'})
    assert [(row['period'], row['value']) for row in served['rows']] == [('2026-09-21', 0), ('2026-09-28', 0)]


@pytest.mark.asyncio
async def test_series_returns_no_limit_baseline_without_a_retained_scoped_turn(isolated_db, monkeypatch):
    from api import measurement
    from infra.database import get_db

    _pin(monkeypatch, 2026, 10, 5)
    db = get_db()
    try:
        _thread(db, 'root', 'project-empty')
        db.commit()
    finally:
        db.close()

    served = await measurement.measurement_series('limit_hits', project='project-empty', agent='none', grain="weeks", timezone_name="UTC", user={'id': 'steve'})
    assert served['rows'] == []


def test_limits_http_scope_failures_keep_the_canonical_status_codes(isolated_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import measurement
    from infra.database import get_db

    db = get_db()
    try:
        _thread(db, 'root', 'project-a')
        _thread(db, 'agent-a', 'project-a', agent_enabled=True)
        _thread(db, 'agent-b', 'project-b', agent_enabled=True)
        db.commit()
    finally:
        db.close()

    app = FastAPI()
    app.include_router(measurement.router)
    app.dependency_overrides[measurement.require_user] = lambda: {'id': 'steve'}
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get('/api/measurement/limits?grain=weeks&timezone=UTC&project=missing&agent=none').status_code == 404
    assert client.get('/api/measurement/limits?grain=weeks&timezone=UTC&project=project-a&agent=missing').status_code == 404
    assert client.get('/api/measurement/limits?grain=weeks&timezone=UTC&project=project-a&agent=agent-b').status_code == 400
    assert client.get('/api/measurement/limits?grain=weeks&timezone=UTC&project=all&agent=').status_code == 400
