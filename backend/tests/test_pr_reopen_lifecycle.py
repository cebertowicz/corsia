"""A reopened pull request is no longer ended (event rules R27).

A close records the pull request as ended, and an ended artifact's later activity
fires no rule. GitHub's reopen is the connector's declared way back: it clears the
recorded close, shows the owner its row again and reaches the owner like any other
pull request event. A merge stays an end.

Every delivery here goes through the webhook mapper and the router, the path a
real delivery takes, so a reopen the mapper cannot name fails the test.
"""

from __future__ import annotations

import json

import pytest

from events.connectors import github_repo_activity
from events.github_mapper import map_github_event
from events.identity import pr_external_id
from tests.event_routing_helpers import pr_payload, register_pr, route
from tests.test_event_rules_2a import (  # noqa: F401 (fixtures)
    P, REPO, _agent, _decisions, _merged, _no_analytics, _project, _rows, _sync, _tool,
    resolved, sent,
)

CLOSED_AT = "2026-11-11T21:39:46Z"


async def _deliver(payload: dict):
    """One GitHub ``pull_request`` delivery, mapped and routed as the webhook does."""
    event = map_github_event("pull_request", payload)
    assert event is not None, f"GitHub's {payload['action']!r} maps to no event"
    return await route(event, payload)


def _closed(number: int) -> dict:
    payload = pr_payload(REPO, number, action="closed")
    payload["pull_request"].update({"merged": False, "closed_at": CLOSED_AT})
    return payload


def _reopened(number: int, at: str = "2026-11-11T21:39:57Z", **kwargs) -> dict:
    payload = pr_payload(REPO, number, action="reopened", **kwargs)
    payload["pull_request"]["updated_at"] = at
    return payload


def _identity_metadata(number: int) -> dict:
    row = _rows("SELECT metadata FROM artifact_identities WHERE external_id=?",
                pr_external_id(REPO, number))[0]
    return json.loads(row["metadata"] or "{}")


def _owner_row(number: int, thread_id: str) -> dict:
    return _rows(
        "SELECT r.retired_at, r.surfacing FROM artifact_registrations AS r "
        "JOIN artifact_identities AS i ON i.id=r.artifact_id "
        "WHERE i.external_id=? AND r.parent_thread_id=?",
        pr_external_id(REPO, number), thread_id)[0]


def _outcomes(rule: dict) -> list[str]:
    return [decision["outcome"] for decision in _decisions(rule_id=rule["id"])]


async def _watch(event_type: str) -> dict:
    return await _tool("watcher", operation="create", connector="github", event_type=event_type)


def _setup(number: int) -> None:
    _project(P)
    _agent("owner")
    _agent("watcher")
    register_pr(REPO, number, "owner")


@pytest.mark.asyncio
async def test_a_push_after_a_reopen_fires_its_rule(isolated_db, sent):
    _setup(7)
    pushed = await _watch("pr.synchronize")

    await _deliver(_closed(7))
    await _deliver(_sync(7, 2))
    assert _outcomes(pushed) == ["ended"]

    await _deliver(_reopened(7))
    await _deliver(_sync(7, 3))

    assert _outcomes(pushed) == ["ended", "woke"]
    assert "pr_state" not in _identity_metadata(7)


@pytest.mark.asyncio
async def test_a_reopen_gives_the_pull_request_back_to_its_owner(isolated_db, sent):
    _setup(7)

    await _deliver(_closed(7))
    assert _owner_row(7, "owner")["retired_at"] is not None

    await _deliver(_reopened(7))

    assert _owner_row(7, "owner") == {"retired_at": None, "surfacing": "visible"}
    thread, content = sent[-1]
    assert thread == "owner" and "reopened" in content


@pytest.mark.asyncio
async def test_each_reopen_is_its_own_event(isolated_db, sent):
    _setup(7)
    reopened = await _watch("pr.reopened")

    await _deliver(_closed(7))
    await _deliver(_reopened(7, "2026-11-11T21:39:57Z"))
    await _deliver(_closed(7))
    await _deliver(_reopened(7, "2026-11-12T08:00:00Z"))

    assert _outcomes(reopened) == ["woke", "woke"]


@pytest.mark.asyncio
async def test_a_reopen_by_the_agents_own_account_still_clears_the_close(isolated_db, sent):
    _setup(7)
    pushed = await _watch("pr.synchronize")

    await _deliver(_closed(7))
    await _deliver(_reopened(7, author="ci-bot", author_type="Bot"))
    await _deliver(_sync(7, 2))

    assert _outcomes(pushed) == ["woke"]
    assert all("reopened" not in content for _, content in sent)


@pytest.mark.asyncio
async def test_a_reopen_the_repository_poller_reads_clears_the_close(isolated_db, sent):
    """The poller's delivery names nobody who did it; the pull request is all it has."""
    _setup(7)
    pushed = await _watch("pr.synchronize")
    pull = {**_reopened(7)["pull_request"], "state": "open", "user": {"login": "alice", "type": "User"}}

    await _deliver(_closed(7))
    await _deliver(github_repo_activity._payload(REPO, "reopened", pull))
    await _deliver(_sync(7, 2))

    assert _outcomes(pushed) == ["woke"]
    assert _owner_row(7, "owner") == {"retired_at": None, "surfacing": "visible"}


@pytest.mark.asyncio
async def test_a_merge_stays_an_end(isolated_db, sent):
    _setup(7)
    pushed = await _watch("pr.synchronize")

    await _deliver(_merged(7))
    await _deliver(_reopened(7))
    await _deliver(_sync(7, 2))

    assert _outcomes(pushed) == ["ended"]
    assert _identity_metadata(7)["pr_state"] == "merged"
    assert _owner_row(7, "owner")["retired_at"] is not None


@pytest.mark.asyncio
async def test_a_reopen_of_a_pull_request_never_recorded_closed_changes_nothing(isolated_db, sent):
    _setup(7)
    before = _owner_row(7, "owner")

    await _deliver(_reopened(7))

    assert _owner_row(7, "owner") == before
    assert "pr_state" not in _identity_metadata(7)
