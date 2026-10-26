"""PR lifecycle regressions using only fake responses and persisted state copies."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from events.connectors import github_repo_activity as poller
from events.connectors.github_notifications_feed import GhResponse


def pull(number, *, head="h", state="open", updated_at="t1", closed_at="c1"):
    return {"number": number, "head": {"sha": head}, "state": state,
            "updated_at": updated_at, "closed_at": closed_at, "merged": False}


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
                     GhResponse(200, {}, closed_pulls), GhResponse(200, {}, []),
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
    assert lifecycle.state["heads"] == {} and lifecycle.state["closed"] == {"7": "c1"}
    closed_snapshot = deepcopy(lifecycle.state)
    await lifecycle.poll([pull(7)], [])
    assert [event[1]["action"] for event in lifecycle.emitted] == ["closed", "reopened"]
    assert lifecycle.emitted[-1] == ("pull_request", {"action": "reopened", "repository": {"full_name": "o/r"},
                                                   "pull_request": pull(7)}, "reopened:o/r#7:c1")
    await lifecycle.poll([pull(7)], [])
    assert len(lifecycle.emitted) == 2  # unchanged poll never re-admits reopening
    lifecycle.state = closed_snapshot  # a retry from the same persisted closure uses the same delivery key
    await lifecycle.poll([pull(7)], [])
    assert lifecycle.emitted[-1][2] == lifecycle.emitted[-2][2]
    await lifecycle.poll([], [pull(7, state="closed", updated_at="t2", closed_at="c2")])
    await lifecycle.poll([pull(7, updated_at="t3", closed_at="c2")], [])
    assert lifecycle.emitted[-1][1]["action"] == "reopened"
    assert lifecycle.emitted[-1][2] == "reopened:o/r#7:c2"  # another closure gives a distinct delivery


@pytest.mark.asyncio
async def test_seeding_then_reopen_open_and_synchronize_keep_their_actions(lifecycle):
    await lifecycle.poll([pull(7)], [pull(8, state="closed")])
    assert lifecycle.emitted == []  # historical open and closed PRs only seed state
    await lifecycle.poll([pull(7), pull(8), pull(9)], [])
    assert [event[1]["action"] for event in lifecycle.emitted] == ["reopened", "opened"]
    assert [event[2] for event in lifecycle.emitted] == ["reopened:o/r#8:c1", "opened:o/r#9"]
    await lifecycle.poll([pull(7, head="new"), pull(8), pull(9)], [])
    assert lifecycle.emitted[-1][1]["action"] == "synchronize"
    assert lifecycle.emitted[-1][1]["before"] == "h"
    assert lifecycle.emitted[-1][2] == "synchronize:o/r#7:new"
    await lifecycle.poll([pull(7, head="new"), pull(8), pull(9)], [])
    assert len(lifecycle.emitted) == 3


@pytest.mark.asyncio
async def test_closed_pr_comment_does_not_change_its_closure_identity(lifecycle):
    lifecycle.state = {"issues_cursor": "x", "heads": {}, "closed": {}, "pending": []}
    await lifecycle.poll([], [pull(7, state="closed", updated_at="t1", closed_at="c1")])
    await lifecycle.poll([], [pull(7, state="closed", updated_at="t2", closed_at="c1")])
    assert [event[1]["action"] for event in lifecycle.emitted] == ["closed"]


def test_synthetic_pollers_do_not_count_as_live_webhooks():
    from infra.webhook_delivery_log import record_delivery

    repo = "o/synthetic-health"
    base = {"action": "opened", "repository": {"full_name": repo}}
    record_delivery(event_type="pull_request", action="opened",
                    delivery_id="gh-poll:pull_request:opened:o/synthetic-health#1",
                    repo=repo, payload=base)
    record_delivery(event_type="issue_comment", action="created",
                    delivery_id="gh-notifications:issue_comment:9", repo=repo, payload=base)
    assert poller._last_delivery(repo) is None
    assert not poller._recent_webhook(poller._last_delivery(repo), datetime.now(UTC))
    assert poller._startup_boundary(poller._last_delivery(repo), {}) is None


# One fallback rule for every read: an item at or before the last real delivery is skipped, saved state still moves.
DELIVERED = datetime(2026, 1, 2, 0, 0, 30, tzinfo=UTC)
BEFORE, AFTER = "2026-01-02T00:00:10Z", "2026-01-02T00:01:00Z"


class Seen(list):
    """The admitted delivery keys; ``answer`` queues the fake reads."""

    def __init__(self, monkeypatch):
        super().__init__()
        self.monkeypatch = monkeypatch

    def answer(self, *responses):
        queue = list(responses)
        self.monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=queue.pop(0)))


@pytest.fixture
def seen(monkeypatch):
    emitted = Seen(monkeypatch)
    monkeypatch.setattr(poller, "_admit", lambda event, payload, key: emitted.append(key) or asyncio.sleep(0))
    return emitted


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered,at,admitted", [(DELIVERED, BEFORE, False), (DELIVERED, AFTER, True), (None, BEFORE, True)])
async def test_open_pulls_synchronize_and_opened_follow_the_rule(seen, delivered, at, admitted):
    state = {"heads": {"3": "old"}}
    seen.answer(GhResponse(200, {}, [pull(3, head="new", updated_at=at), pull(4, updated_at=at)]))
    await poller._open_pulls(lambda *_a: None, "o/r", state, datetime(2026, 1, 4, tzinfo=UTC), delivered)
    assert seen == (["synchronize:o/r#3:new", "opened:o/r#4"] if admitted else [])
    assert state["heads"] == {"3": "new", "4": "h"}  # saved heads move past a skipped item


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered,closed_at,admitted", [(DELIVERED, BEFORE, False), (DELIVERED, AFTER, True), (None, BEFORE, True)])
async def test_closed_pulls_follow_the_rule(seen, delivered, closed_at, admitted):
    state = {"closed": {}}
    seen.answer(GhResponse(200, {}, [pull(7, state="closed", closed_at=closed_at)]))
    await poller._closed_pulls(lambda *_a: None, "o/r", state, datetime(2026, 1, 4, tzinfo=UTC), delivered)
    assert seen == ([f"closed:o/r#7:{closed_at}"] if admitted else [])
    assert state["closed"] == {"7": closed_at}  # the marker moves past a skipped item


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered,created_at,admitted", [(DELIVERED, BEFORE, False), (DELIVERED, AFTER, True), (None, BEFORE, True)])
async def test_new_issues_follow_the_rule(seen, delivered, created_at, admitted):
    state = {"issues_opened_at": "2026-01-01T00:00:00Z"}
    seen.answer(GhResponse(200, {}, [{"number": 9, "created_at": created_at, "user": {"login": "u"}}]))
    await poller._new_issues(lambda *_a: None, "o/r", state, datetime(2026, 1, 4, tzinfo=UTC), delivered)
    assert seen == (["opened:o/r#9"] if admitted else []) and state["issues_opened_at"] == created_at


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered,updated_at,admitted", [(DELIVERED, BEFORE, False), (DELIVERED, AFTER, True), (None, BEFORE, True)])
async def test_settled_check_suites_follow_the_rule(seen, delivered, updated_at, admitted):
    state = {"pending": ["a"]}
    suite = {"id": 5, "status": "completed", "updated_at": updated_at}
    seen.answer(GhResponse(200, {}, {"workflow_runs": []}), GhResponse(200, {}, {"workflow_runs": []}),
                GhResponse(200, {}, {"check_suites": [suite]}))
    await poller._pending_heads(lambda *_a: None, "o/r", state, datetime(2026, 1, 4, tzinfo=UTC), delivered)
    assert seen == ([f"check_suite:o/r:5:{updated_at}"] if admitted else []) and state["pending"] == []


@pytest.mark.asyncio
async def test_a_poll_and_the_startup_catchup_read_the_delivery_log_once(lifecycle, monkeypatch):
    from infra import webhook_delivery_log
    reads = []
    monkeypatch.setattr(webhook_delivery_log, "repo_last_delivered_at", lambda repo: reads.append(repo) or None)
    lifecycle.state = {"issues_cursor": "x", "heads": {}, "closed": {}, "pending": []}
    await lifecycle.poll([], [])
    assert reads == ["o/r"]
    reads.clear()
    monkeypatch.setattr(poller, "_get", lambda *_a, **_k: asyncio.sleep(0, result=GhResponse(304, {}, None)))
    await poller._startup_catchup(lambda *_a: None, "o/r", datetime(2026, 1, 4, tzinfo=UTC))
    assert reads == ["o/r"]
