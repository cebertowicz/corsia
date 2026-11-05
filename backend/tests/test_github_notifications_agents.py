"""Independent agents' notifications feed and shared admission."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from github_notifications_fixtures import FLOOR, FakeGh, comment_object


@pytest.fixture(autouse=True)
def setup_feeds(isolated_db, monkeypatch):
    from connections import github_agent
    from events.connectors import github_notifications as poller
    from infra.settings import set_setting

    monkeypatch.setenv("CORSIA_GITHUB_NOTIFICATIONS_TEST_ENABLE", "1")
    monkeypatch.delenv(poller.DISABLE_ENV, raising=False)
    monkeypatch.setattr(github_agent, "view", lambda: SimpleNamespace(absent=False))
    monkeypatch.setattr(
        "infra.project_repo_policy.list_registered_repo_policies",
        lambda: [SimpleNamespace(source_binding_valid=True, enforcement_error=None, path="/tracked")],
    )
    monkeypatch.setattr(
        "infra.worktree_reaper_probe.github_remote_full_name",
        lambda _path, remote: "o/r" if remote == "origin" else None,
    )
    for name in ("_status", "_agents_status"):
        monkeypatch.setattr(poller, name, {
            "state": "starting", "reason": None, "last_success_at": None, "last_error": None,
            "skipped_threads": 0, "dropped_notifications": 0,
        })
    for key in (poller.CURSOR_SETTING, poller.AGENTS_CURSOR_SETTING):
        set_setting(key, {"floor": FLOOR, "cursor": FLOOR})


def test_agents_comment_is_admitted_once_across_both_feeds():
    from events.connectors import github_notifications as poller
    from infra.database import get_db

    agent, person = FakeGh(), FakeGh()
    agent.issue_comments = person.issue_comments = [comment_object(777)]
    routed = []

    async def route(*args, **kwargs):
        routed.append(args)

    async def run():
        from events.connectors.github_webhook import _BG_DISPATCH
        with patch("events.ingress.route_leg", side_effect=route):
            assert (await poller.run_cycle(agent, agents=True))[1] == 0
            assert (await poller.run_cycle(person))[1] == 0
            if _BG_DISPATCH:
                await asyncio.gather(*list(_BG_DISPATCH))

    asyncio.run(run())
    rows = get_db().execute("SELECT object_key FROM webhook_deliveries").fetchall()
    assert [row[0] for row in rows] == ["issue_comment:777"]
    assert len(routed) == 1


def test_absent_account_does_not_read_and_rechecks(monkeypatch):
    from connections import github_agent
    from events.connectors import github_notifications as poller
    from events.connectors.poller_status import live_poller_statuses

    monkeypatch.setattr(github_agent, "view", lambda: SimpleNamespace(absent=True))
    gh = FakeGh()
    assert asyncio.run(poller.run_cycle(gh, agents=True)) == (poller.RECHECK_S, 0)
    assert gh.calls == []
    status = live_poller_statuses()["github_notifications_agents"]
    assert (status["state"], status["reason"]) == ("disabled", "no_agents_account")
    assert status.keys() == live_poller_statuses()["github_notifications"].keys()
    monkeypatch.setattr(github_agent, "view", lambda: SimpleNamespace(absent=False))
    assert asyncio.run(poller.run_cycle(gh, agents=True))[1] == 0
    assert gh.api_calls()
    assert poller.poller_status(agents=True)["state"] == "enabled"


def test_loop_uses_agents_broker(monkeypatch):
    from connections.github_broker import agents_broker
    from events.connectors import github_notifications as poller

    seen = []

    async def stop(runner, failures=0, *, agents=False):
        seen.append((runner, agents))
        return None, 0

    monkeypatch.setattr(poller, "run_cycle", stop)
    asyncio.run(poller.notifications_poll_loop(agents=True))
    assert seen == [(agents_broker(), True)]


def test_agents_broker_never_reads_as_the_host_login(monkeypatch):
    """The account removed after the cycle's check fails the read; the person's feed is never read."""
    import subprocess

    from connections import github_agent
    from connections.github_agent import AgentAccountView
    from connections.github_broker import GithubReadBroker, GithubReadRefused

    def never(*_args, **_kwargs):
        raise AssertionError("gh must not run as the host login")

    monkeypatch.setattr(github_agent, "backend_gh_now", lambda: ("gh", None, AgentAccountView("not_installed", absent=True)))
    monkeypatch.setattr(subprocess, "run", never)
    with pytest.raises(GithubReadRefused, match="no_agents_account"):
        GithubReadBroker(agents_only=True)(["api", "--method", "GET", "-i", "notifications"])


def test_failure_does_not_affect_other_feed():
    from events.connectors import github_notifications as poller
    from infra.settings import get_setting

    broken, healthy = FakeGh(), FakeGh()
    broken.notifications_status = 500
    assert asyncio.run(poller.run_cycle(broken, agents=True))[1] == 1
    assert asyncio.run(poller.run_cycle(healthy))[1] == 0
    assert poller.poller_status(agents=True)["state"] == "failing"
    assert poller.poller_status()["state"] == "enabled"
    assert get_setting(poller.CURSOR_SETTING)["cursor"] == "2026-09-24T05:31:00Z"
    assert get_setting(poller.AGENTS_CURSOR_SETTING)["cursor"] == FLOOR


def test_first_agents_read_sets_floor(monkeypatch):
    from events.connectors import github_notifications as poller
    from infra.database import get_db
    from infra.settings import get_setting

    monkeypatch.setattr(poller, "_now", lambda: datetime(2026, 9, 24, 5, 20, tzinfo=UTC))
    db = get_db()
    db.execute("DELETE FROM settings WHERE key=?", (poller.AGENTS_CURSOR_SETTING,))
    db.commit()
    gh = FakeGh()
    gh.issue_comments = [comment_object(11, created_at="2026-01-01T00:00:00Z")]
    asyncio.run(poller.run_cycle(gh, agents=True))
    assert get_setting(poller.AGENTS_CURSOR_SETTING)["floor"] == "2026-09-24T05:20:00Z"
    assert db.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0] == 0
