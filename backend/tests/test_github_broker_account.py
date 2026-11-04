"""Which GitHub account the pollers' read brokers run ``gh`` as."""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from connections import github_agent
from connections.github_agent import AgentAccountView
from connections.github_agent_home import LOCKED_HOME
from connections.github_broker import _run_gh, default_broker, person_broker


@pytest.fixture
def ran(monkeypatch):
    seen = {}

    def fake(argv, **kwargs):
        seen.update(argv=argv, env=kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake)
    return seen


def never_verified():
    raise AssertionError("only a verifying account waits on verify_now")


def test_the_broker_runs_gh_as_the_agents_account(monkeypatch, ran):
    monkeypatch.setattr(github_agent, "view", lambda: AgentAccountView(state="ready"))
    monkeypatch.setattr(github_agent, "verify_now", never_verified)
    monkeypatch.setattr(github_agent, "backend_gh",
                        lambda _account: ("/agent/gh", {"GH_CONFIG_DIR": "/agent/home"}))
    _run_gh(["api", "user"], timeout=5)
    assert ran["argv"] == ["/agent/gh", "api", "user"]
    assert ran["env"]["GH_CONFIG_DIR"] == "/agent/home" and ran["env"]["GH_HOST"] == "github.com"


def test_an_agents_account_that_is_not_ready_reads_locked_never_as_the_person(monkeypatch, ran):
    monkeypatch.setenv("GH_TOKEN", "person-credential-value")
    monkeypatch.setattr(github_agent, "view", lambda: AgentAccountView(
        state="needs_auth", cause="signed_out", binary="/agent/gh", home="/agent/home"))
    monkeypatch.setattr(github_agent, "verify_now", never_verified)
    _run_gh(["api", "user"], timeout=5)
    assert ran["argv"][0] == "/agent/gh"
    assert ran["env"]["GH_CONFIG_DIR"] == LOCKED_HOME and "GH_TOKEN" not in ran["env"]


def test_every_poller_defaults_to_its_broker(monkeypatch) -> None:
    from events.connectors import github_notifications as notifications
    from events.connectors import github_pr_quiet_watch as quiet
    from events.connectors import github_repo_activity as activity

    seen = {}

    class Stop(Exception):
        pass

    async def stop_poll(runner, repos):
        seen["activity"] = runner
        raise Stop

    async def stop_cycle(runner, failures=0):
        seen["notifications"] = runner
        return None, 0

    monkeypatch.setattr(notifications, "run_cycle", stop_cycle)
    monkeypatch.setattr(quiet, "disabled_reason", lambda runner: seen.setdefault("quiet", runner) and "test_mode")
    asyncio.run(notifications.notifications_poll_loop())
    asyncio.run(quiet.pr_quiet_watch_loop())
    monkeypatch.setattr(activity.feed, "tracked_github_repositories", lambda: frozenset())
    monkeypatch.setattr(activity, "poll_once", stop_poll)
    with pytest.raises(Stop):
        asyncio.run(activity.repo_activity_poll_loop())
    assert seen["quiet"] is seen["activity"] is default_broker()
    # the person's feed reads as the person
    assert seen["notifications"] is person_broker() and seen["notifications"] is not default_broker()


def test_the_notifications_feed_is_read_as_the_person_never_the_agents_account(monkeypatch, ran):
    from events.connectors import github_notifications as notifications

    def never(*_args):
        raise AssertionError("the person's feed never runs as the agents' account")

    async def one_cycle(runner, failures=0):
        runner(["api", "--method", "GET", "-i", "notifications"], timeout=5)
        return None, 0

    monkeypatch.setattr(github_agent, "view", never)
    monkeypatch.setattr(github_agent, "backend_gh", never)
    monkeypatch.setattr(notifications, "run_cycle", one_cycle)
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    asyncio.run(notifications.notifications_poll_loop())
    assert ran["argv"] == ["gh", "api", "--method", "GET", "-i", "notifications"]
    assert ran["env"]["GH_HOST"] == "github.com" and "GH_CONFIG_DIR" not in ran["env"]


# --- each default_broker() reader, when the agents' account exists but is not ready ---------
# A locked gh has no credential, and gh refuses `api` and `auth status` without one before
# any request (exit 4, as real gh 2.x does with an empty config folder): GitHub is never
# called, so no budget learns anything. Each reader reports it as it reports any failed read.

NO_LOGIN = "To get started with GitHub CLI, please run:  gh auth login\n"


@pytest.fixture
def locked(monkeypatch):
    from connections import github_broker
    from events.connectors.github_read_budget import budget

    runs = []

    def fake(argv, **kwargs):
        runs.append(argv)
        assert kwargs["env"]["GH_CONFIG_DIR"] == LOCKED_HOME and "GH_TOKEN" not in kwargs["env"]
        return subprocess.CompletedProcess(argv, 4, "", NO_LOGIN)

    monkeypatch.setenv("GH_TOKEN", "person-credential-value")
    monkeypatch.setattr(github_agent, "view", lambda: AgentAccountView(
        state="needs_auth", cause="signed_out", binary="/agent/gh", home="/agent/home"))
    monkeypatch.setattr(github_agent, "verify_now", never_verified)
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(github_broker, "_default", None)
    budget.clear()
    yield runs
    budget.clear()


@pytest.mark.asyncio
async def test_the_repo_activity_poller_is_refused_locked_and_reports_degraded(monkeypatch, locked):
    from datetime import UTC, datetime

    from events.connectors import github_repo_activity as poller

    monkeypatch.setattr(poller, "_status", {"state": "enabled", "reason": None, "counted_reads": 0,
                                            "degraded": None})
    state = {"reads": [], "issues_cursor": "held"}
    assert await poller._get(default_broker(), "repos/o/r/pulls", state, datetime.now(UTC)) is None
    assert state["issues_cursor"] == "held" and poller.poller_status()["degraded"] == "gh_error"
    assert locked and all(argv[0] == "/agent/gh" for argv in locked)
    assert budget_status() == ({}, {})


@pytest.mark.asyncio
async def test_the_pr_quiet_watch_probes_locked_and_reports_signed_out(monkeypatch, locked):
    from unittest.mock import AsyncMock

    from events.connectors import github_pr_quiet_watch as watch

    monkeypatch.setenv("CORSIA_TEST_MODE", "0")
    monkeypatch.delenv(watch.DISABLE_ENV, raising=False)
    monkeypatch.setattr(watch.rows, "prune_rows", lambda: None)
    sweep = AsyncMock()
    monkeypatch.setattr(watch, "sweep_once", sweep)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(watch.pr_quiet_watch_loop(), 1)
    assert watch.quiet_watch_status()["reason"] == "gh_signed_out" and sweep.await_count == 0
    assert locked and all(argv[0] == "/agent/gh" for argv in locked)


def test_the_script_github_channel_is_refused_locked_and_hands_the_client_the_failure(locked):
    import json

    from automation import script_github_channel as channel

    got = channel._respond(json.dumps(["repos/o/r/pulls/1"]).encode(), repo="o/r", context="run=x")
    assert got["code"] == 4 and got["stdout"] == "" and got["stderr"] == NO_LOGIN
    assert [argv[0] for argv in locked] == ["/agent/gh"]


@pytest.mark.parametrize("verified, home", [
    (AgentAccountView(state="ready", binary="/agent/gh", home="/agent/home"), "/agent/home"),
    (AgentAccountView(state="needs_auth", cause="signed_out", binary="/agent/gh", home="/agent/home"), LOCKED_HOME),
])
def test_a_verifying_account_waits_once_for_its_answer(monkeypatch, ran, verified, home):
    calls = []
    monkeypatch.setattr(github_agent, "view", lambda: AgentAccountView(
        state="unverified", cause="verifying", binary="/agent/gh", home="/agent/home"))
    monkeypatch.setattr(github_agent, "verify_now", lambda: calls.append(1) or verified)
    _run_gh(["api", "user"], timeout=5)
    assert calls == [1]
    assert ran["argv"][0] == "/agent/gh" and ran["env"]["GH_CONFIG_DIR"] == home


def budget_status():
    from events.connectors.github_read_budget import budget, person_budget

    return budget.status(), person_budget.status()


@pytest.mark.parametrize("account, agents", [
    (AgentAccountView(state="ready", binary="/agent/gh", home="/agent/home"), True),
    (AgentAccountView(state="needs_auth", cause="signed_out", binary="/agent/gh", home="/agent/home"), True),
    (AgentAccountView(state="not_installed", absent=True), False),
])
def test_each_broker_names_the_budget_of_the_account_it_reads_as(monkeypatch, account, agents):
    from events.connectors.github_read_budget import budget, person_budget

    monkeypatch.setattr(github_agent, "view", lambda: account)
    assert account.absent is not agents
    assert default_broker().read_budget is (budget if agents else person_budget)
    assert person_broker().read_budget is person_budget
