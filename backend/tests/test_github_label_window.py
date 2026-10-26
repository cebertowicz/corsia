"""A label or reopen change seen by both the webhook and the poller is admitted once.

Both copies go through ``admit_github_delivery_async``: the webhook directly, the poller
from its real ``issues/events`` read (a fake runner supplies the recorded row). The webhook's
time is the item's ``updated_at``, which can sit a second off the row's time, so copies within
five seconds are one change. Scheduling is replaced by a recorder: the count of scheduled
deliveries is what routing would have received.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from events.connectors import github_admission as admission
from events.connectors import github_repo_activity as poller
from events.connectors.github_notifications_feed import GhResponse
from test_github_label_events import REPO, fixture, pr_webhook

BASE = datetime(2026, 10, 24, 6, 25, 11, tzinfo=UTC)
KINDS = {"pr_labeled": ("row_pr_labeled", "github.pr.labeled"),
         "issue_labeled": ("row_issue_labeled", "github.issue.labeled"),
         "issue_reopened": ("row_issue_labeled", "github.issue.reopened")}


def stamp(seconds: int) -> str:
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def row_for(kind: str, seconds: int, row_id: int) -> dict:
    row = {**fixture(KINDS[kind][0]), "id": row_id, "created_at": stamp(seconds)}
    return {**row, "event": "reopened", "label": None} if kind == "issue_reopened" else row


def webhook_for(kind: str, seconds: int) -> tuple[str, dict]:
    if kind == "pr_labeled":
        return "pull_request", pr_webhook(at=stamp(seconds))
    row = fixture("row_issue_labeled")
    payload = {"action": "reopened" if kind == "issue_reopened" else "labeled", "repository": {"full_name": REPO},
               "sender": {"login": row["actor"]["login"]}, "issue": {**row["issue"], "updated_at": stamp(seconds)}}
    return "issues", payload if kind == "issue_reopened" else {**payload, "label": row["label"]}


@pytest.fixture
def world(main_db, monkeypatch):
    scheduled = []
    monkeypatch.setattr(admission, "_schedule", lambda event_type, payload, delivery_id: scheduled.append(delivery_id) or "routed")
    monkeypatch.setattr(poller, "_locks", {})
    monkeypatch.setattr(poller, "_status", {"state": "starting", "reason": None, "counted_reads": 0,
                                            "backoff": False, "backoff_until": None, "degraded": None})
    counter = iter(range(2000, 3000))

    class World:
        async def webhook(self, kind: str, seconds: int) -> None:
            event, payload = webhook_for(kind, seconds)
            await admission.admit_github_delivery_async(event, payload, f"guid-{next(counter)}")

        async def poll(self, kind: str, seconds: int) -> None:
            response = GhResponse(200, {}, [row_for(kind, seconds, next(counter)), {"id": 1, "created_at": "2020-01-01T00:00:00Z"}])
            monkeypatch.setattr(poller.feed, "gh_get", lambda *_args: response)
            state = {"issues_cursor": "1", "heads": {"1421": "9f2d6c1"}}
            await poller._issue_events(lambda *_a: None, REPO, state, datetime.now(UTC))

        def recorded(self, kind: str) -> int:
            return main_db.execute("SELECT COUNT(*) FROM outcome_events WHERE event_name=?", (KINDS[kind][1],)).fetchone()[0]

        def copies(self) -> int:
            return main_db.execute("SELECT COUNT(*) FROM webhook_deliveries WHERE status=?", (
                "duplicate_copy",)).fetchone()[0]

    world = World()
    world.scheduled = scheduled
    return world


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("skew", [0, 1, -1, 5])
async def test_webhook_then_poller_within_the_window_is_one_change(world, kind, skew):
    await world.webhook(kind, 0)
    await world.poll(kind, skew)
    assert len(world.scheduled) == 1 and world.recorded(kind) == 1 and world.copies() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(KINDS))
async def test_poller_then_webhook_one_second_later_is_one_change(world, kind):
    await world.poll(kind, 0)
    await world.webhook(kind, 1)
    assert len(world.scheduled) == 1 and world.recorded(kind) == 1 and world.copies() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("apart", [6, 180])
async def test_copies_six_seconds_or_minutes_apart_are_two_changes(world, kind, apart):
    await world.webhook(kind, 0)
    await world.poll(kind, apart)
    assert len(world.scheduled) == 2 and world.recorded(kind) == 2 and world.copies() == 0
