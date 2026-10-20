"""PR lifecycle regressions using only fake responses and persisted state copies."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from events.connectors import github_repo_activity as poller
from events.connectors.github_notifications_feed import GhResponse


def pull(number, *, head="h", state="open", updated_at="t1"):
    return {"number": number, "head": {"sha": head}, "state": state,
            "updated_at": updated_at, "merged": False}


@pytest.fixture
def lifecycle(monkeypatch):
    saved = SimpleNamespace(state={}, emitted=[])
    monkeypatch.setattr(poller, "_locks", {})
    monkeypatch.setattr(poller, "_recent_webhook", lambda *_a: False)
    monkeypatch.setattr(poller, "_repo_state", lambda _repo: deepcopy(saved.state))
    monkeypatch.setattr(poller, "_save_state", lambda _repo, state: setattr(saved, "state", deepcopy(state)))

    async def admit(event, payload, key):
        saved.emitted.append((event, deepcopy(payload), key))

    async def poll(open_pulls, closed_pulls):
        responses = [GhResponse(304 if saved.state.get("issues_cursor") else 200, {}, []), GhResponse(200, {}, open_pulls),
                     GhResponse(200, {}, closed_pulls),
                     GhResponse(200, {}, {"workflow_runs": []}), GhResponse(200, {}, {"workflow_runs": []})]
        monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=responses.pop(0)))
        await poller._poll_repo(lambda *_a: None, "o/r", datetime(2026, 1, 4, tzinfo=UTC))
        assert responses == []

    monkeypatch.setattr(poller, "_admit", admit)
    saved.poll = poll
    return saved


@pytest.mark.asyncio
async def test_close_reopen_is_persisted_and_emits_once_per_closure(lifecycle):
    lifecycle.state = {"issues_cursor": "x", "heads": {"7": "h"}, "closed": {}, "pending": []}
    await lifecycle.poll([], [pull(7, state="closed")])
    assert lifecycle.state["heads"] == {} and lifecycle.state["closed"] == {"7": "t1"}
    closed_snapshot = deepcopy(lifecycle.state)
    await lifecycle.poll([pull(7)], [])
    assert [event[1]["action"] for event in lifecycle.emitted] == ["closed", "reopened"]
    assert lifecycle.emitted[-1] == ("pull_request", {"action": "reopened", "repository": {"full_name": "o/r"},
                                                   "pull_request": pull(7)}, "reopened:o/r#7:t1")
    await lifecycle.poll([pull(7)], [])
    assert len(lifecycle.emitted) == 2  # unchanged poll never re-admits reopening
    lifecycle.state = closed_snapshot  # a retry from the same persisted closure uses the same delivery key
    await lifecycle.poll([pull(7)], [])
    assert lifecycle.emitted[-1][2] == lifecycle.emitted[-2][2]
    await lifecycle.poll([], [pull(7, state="closed", updated_at="t2")])
    await lifecycle.poll([pull(7, updated_at="t3")], [])
    assert lifecycle.emitted[-1][1]["action"] == "reopened"
    assert lifecycle.emitted[-1][2] == "reopened:o/r#7:t2"  # another closure gives a distinct delivery


@pytest.mark.asyncio
async def test_seeding_then_reopen_open_and_synchronize_keep_their_actions(lifecycle):
    await lifecycle.poll([pull(7)], [pull(8, state="closed")])
    assert lifecycle.emitted == []  # historical open and closed PRs only seed state
    await lifecycle.poll([pull(7), pull(8), pull(9)], [])
    assert [event[1]["action"] for event in lifecycle.emitted] == ["reopened", "opened"]
    assert [event[2] for event in lifecycle.emitted] == ["reopened:o/r#8:t1", "opened:o/r#9"]
    await lifecycle.poll([pull(7, head="new"), pull(8), pull(9)], [])
    assert lifecycle.emitted[-1][1]["action"] == "synchronize"
    assert lifecycle.emitted[-1][1]["before"] == "h"
    assert lifecycle.emitted[-1][2] == "synchronize:o/r#7:new"
    await lifecycle.poll([pull(7, head="new"), pull(8), pull(9)], [])
    assert len(lifecycle.emitted) == 3
