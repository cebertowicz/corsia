"""Focused behavior tests for the local correction-classifier recorder."""

from __future__ import annotations

import json

import pytest


def _db():
    from infra.database import get_db

    return get_db()


def _seed(db, *, message_id="owner-message", created_at="2026-10-01T00:01:00Z"):
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','project','t','t')")
    db.execute(
        "INSERT INTO threads (id,user_id,title,project_id,created_at,updated_at) VALUES ('thread','owner','thread','project','t','t')"
    )
    db.execute("INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES ('prior','thread','assistant','Prior answer.','{}','2026-10-01T00:00:00Z')")
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?,?,?,?,?,?)",
        (message_id, "thread", "user", "That result is wrong.", "{}", created_at),
    )
    db.commit()


def _version():
    from core.corrections_recording import CheckerVersion

    return CheckerVersion("corrections-classifier", "v1", "correction", 0.595681, "modernbert-owner-context-v2-2048")


def test_owner_message_filter_excludes_agents_and_system_wrappers():
    from core.corrections_recording import is_owner_authored_message

    assert not is_owner_authored_message(role="user", content="x", metadata=json.dumps({"sender_kind": "agent"}))
    assert not is_owner_authored_message(role="user", content="[EVENT: tick]", metadata="{}")
    assert is_owner_authored_message(role="user", content="That is wrong.", metadata="{}")


def test_arrival_trigger_only_notifies_new_owner_messages(monkeypatch):
    from jobs import corrections_realtime

    seen = []
    monkeypatch.setattr(corrections_realtime, "notify", seen.append)

    corrections_realtime.notify_if_owner("message", role="user", content="A correction.", metadata="{}", duplicate=False)
    corrections_realtime.notify_if_owner("duplicate", role="user", content="A correction.", metadata="{}", duplicate=True)
    corrections_realtime.notify_if_owner("agent", role="user", content="A correction.", metadata={"sender_kind": "agent"}, duplicate=False)

    assert seen == ["message"]


@pytest.mark.asyncio
async def test_scan_hydrates_recent_context_and_uses_versioned_dedupe(isolated_db):
    from jobs.corrections_scanning import hydrate_candidates, pending_candidate_keys

    db = _db()
    try:
        _seed(db)
    finally:
        db.close()
    keys = pending_candidate_keys(_version(), 20, "owner")
    candidates = hydrate_candidates(keys)

    assert [candidate.message_id for candidate in candidates] == ["owner-message"]
    assert candidates[0].context == (("assistant", "Prior answer."),)


@pytest.mark.asyncio
async def test_fact_carries_the_verified_asset_result_without_message_text(isolated_db):
    from jobs.corrections_recording import _record_fact
    from jobs.corrections_scanning import hydrate_candidates, pending_candidate_keys

    db = _db()
    try:
        _seed(db)
    finally:
        db.close()
    candidate = hydrate_candidates(pending_candidate_keys(_version(), 20, "owner"))[0]
    assert _record_fact(candidate, {"label": "correction", "truncated": False, "original_tokens": 9, "failure_reason": "none"}, _version())

    db = _db()
    try:
        attributes = json.loads(db.execute("SELECT attributes FROM outcome_events").fetchone()[0])
    finally:
        db.close()
    assert attributes == {
        "message_id": "owner-message", "thread_id": "thread", "label": "correction",
        "asset_id": "corrections-classifier", "asset_version": "v1", "head": "correction",
        "threshold": "0.595681", "input_policy_id": "modernbert-owner-context-v2-2048",
        "truncated": "false", "original_tokens": 9, "failure_reason": "none",
        "classified_at": attributes["classified_at"],
    }


@pytest.mark.asyncio
async def test_same_message_and_version_is_recorded_once_when_scan_and_notify_race(isolated_db):
    from jobs.corrections_recording import _record_fact
    from jobs.corrections_scanning import hydrate_candidates, pending_candidate_keys

    db = _db()
    try:
        _seed(db)
    finally:
        db.close()
    candidate = hydrate_candidates(pending_candidate_keys(_version(), 20, "owner"))[0]
    payload = {"label": "not_correction", "truncated": False, "original_tokens": 9, "failure_reason": "none"}
    assert _record_fact(candidate, payload, _version())
    assert not _record_fact(candidate, payload, _version())
