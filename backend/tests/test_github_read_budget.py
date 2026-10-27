"""Both GitHub pollers read through one shared, header-driven budget."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from github_notifications_fixtures import FLOOR, FakeGh, comment_object

NOW = 1_800_000_000.0
RESET = NOW + 1800


@pytest.fixture(autouse=True)
def _budget_env(isolated_db, monkeypatch):
    from events.connectors import github_notifications as poller
    from events.connectors.github_read_budget import budget
    from infra.settings import set_setting

    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.setattr(poller, "_status", {
        "state": "starting", "reason": None, "last_success_at": None, "last_error": None,
        "skipped_threads": 0, "dropped_notifications": 0,
    })
    monkeypatch.setattr(
        "infra.project_repo_policy.list_registered_repo_policies",
        lambda: [SimpleNamespace(source_binding_valid=True, enforcement_error=None, path="/tracked")],
    )
    monkeypatch.setattr(
        "infra.worktree_reaper_probe.github_remote_full_name",
        lambda _path, remote: "o/r" if remote == "origin" else None,
    )
    set_setting(poller.CURSOR_SETTING, {"floor": FLOOR, "cursor": FLOOR})
    clock = {"now": NOW}
    monkeypatch.setattr(budget, "_clock", lambda: clock["now"])
    budget.clear()
    yield clock
    budget.clear()


class Runner:
    """A ``gh`` runner that answers every read with one status and headers."""

    def __init__(self, status: int = 200, body: dict | None = None, **headers: str) -> None:
        self.status, self.headers, self.body = status, headers, body or {}
        self.paths: list[str] = []

    def __call__(self, args, **_kwargs):
        self.paths.append(args[-1])
        head = "\r\n".join([f"HTTP/2.0 {self.status} X", *(f"{k.replace('_', '-')}: {v}" for k, v in self.headers.items())])
        return subprocess.CompletedProcess(args, 0, f"{head}\r\n\r\n{json.dumps(self.body)}", "")


def _notifications_read(runner):
    from events.connectors.github_notifications_feed import gh_get

    return gh_get(runner, "notifications?all=true")


def _sweep_read(runner, repo: str):
    from events.connectors import github_pr_quiet_watch as watch

    watch._etags.clear()
    return asyncio.run(watch._get(runner, f"repos/{repo}/pulls/1"))


def _rate_limited(call):
    from events.connectors.github_notifications_feed import FeedError

    with pytest.raises(FeedError) as caught:
        call()
    assert caught.value.error_class == "rate_limited"


def test_two_pollers_and_two_repos_share_one_budget():
    first = Runner(403, retry_after="120")
    _rate_limited(lambda: _notifications_read(first))
    sweep = Runner()
    _rate_limited(lambda: _sweep_read(sweep, "p/q"))
    _rate_limited(lambda: _sweep_read(sweep, "o/r"))
    assert sweep.paths == [], "GitHub was called while the budget backed off"


def test_retry_after_seen_by_the_sweep_blocks_the_notifications_poller(_budget_env):
    _rate_limited(lambda: _sweep_read(Runner(403, retry_after="30"), "o/r"))
    reader = Runner()
    _rate_limited(lambda: _notifications_read(reader))
    assert reader.paths == []
    _budget_env["now"] += 31
    assert _notifications_read(reader).status == 200


def test_remaining_zero_blocks_every_read_until_reset(_budget_env):
    ok = Runner(200, x_ratelimit_remaining="0", x_ratelimit_reset=str(int(RESET)))
    _notifications_read(ok)
    reader = Runner()
    _rate_limited(lambda: _notifications_read(reader))
    _rate_limited(lambda: _sweep_read(reader, "o/r"))
    assert reader.paths == []
    _budget_env["now"] = RESET + 1
    assert _notifications_read(reader).status == 200


def test_a_429_backs_off_for_a_minute_even_without_headers(_budget_env):
    _rate_limited(lambda: _notifications_read(Runner(429)))
    reader = Runner()
    _rate_limited(lambda: _sweep_read(reader, "o/r"))
    _budget_env["now"] += 61
    assert _sweep_read(reader, "o/r") == {}


def test_a_secondary_limit_403_backs_off_every_read_without_headers(_budget_env):
    secondary = Runner(403, {"message": "You have exceeded a secondary rate limit."}, x_ratelimit_remaining="40")
    _rate_limited(lambda: _notifications_read(secondary))
    reader = Runner()
    _rate_limited(lambda: _sweep_read(reader, "o/r"))
    assert reader.paths == []
    _budget_env["now"] += 61
    assert _notifications_read(reader).status == 200


def test_a_plain_403_is_not_a_rate_limit():
    from events.connectors.github_notifications_feed import FeedError

    with pytest.raises(FeedError) as caught:
        _notifications_read(Runner(403, {"message": "Resource not accessible"}, x_ratelimit_remaining="40"))
    assert caught.value.error_class == "http_403"
    assert _notifications_read(Runner()).status == 200


def test_another_resource_does_not_move_core():
    other = Runner(200, x_ratelimit_remaining="0", x_ratelimit_reset=str(int(RESET)), x_ratelimit_resource="search")
    _notifications_read(other)
    assert _notifications_read(Runner()).status == 200


def test_the_reserve_is_five_percent_of_the_accounts_limit():
    from events.connectors.github_read_budget import budget

    assert budget.reserve() == 250
    _notifications_read(Runner(200, x_ratelimit_limit="15000", x_ratelimit_remaining="600", x_ratelimit_reset=str(int(RESET))))
    assert budget.reserve() == 750
    _rate_limited(lambda: _sweep_read(Runner(), "o/r"))  # 600 left is under 750
    assert _notifications_read(Runner()).status == 200


def test_sweep_skips_under_the_reserve_while_notifications_still_reads():
    from events.connectors.github_read_budget import budget

    low = Runner(200, x_ratelimit_remaining=str(budget.reserve() - 1), x_ratelimit_reset=str(int(RESET)))
    _notifications_read(low)
    reader = Runner()
    _rate_limited(lambda: _sweep_read(reader, "o/r"))
    assert reader.paths == []
    assert _notifications_read(reader).status == 200
    assert reader.paths == ["notifications?all=true"]


def _sweep_once(runner):
    from events.connectors import github_pr_quiet_watch as watch

    asyncio.run(watch.sweep_once(runner))
    return watch.quiet_watch_status()


@pytest.fixture()
def _one_candidate(monkeypatch):
    from events.connectors import github_pr_quiet_watch as watch

    monkeypatch.setattr(watch, "_candidates", lambda: [watch.Candidate("t", "github:o/r#1", "o/r", 1)])
    monkeypatch.setattr(watch, "_status", {
        "state": "starting", "reason": None, "last_run": None, "fired_count": 0, "last_error": None,
    })
    watch._etags.clear()


def test_the_sweep_skips_under_the_reserve_without_degrading(_one_candidate):
    from events.connectors.github_read_budget import budget

    _notifications_read(Runner(200, x_ratelimit_remaining=str(budget.reserve() - 1), x_ratelimit_reset=str(int(RESET))))
    reader = Runner()
    status = _sweep_once(reader)
    assert reader.paths == [], "the sweep read inside the reserve"
    assert status["state"] == "enabled" and status["last_error"] is None


def test_a_real_refusal_still_degrades_the_sweep(_one_candidate):
    status = _sweep_once(Runner(403, retry_after="30"))
    assert status["state"] == "degraded" and status["last_error"] == "rate_limited"


def _cursor() -> dict:
    from events.connectors.github_notifications import CURSOR_SETTING
    from infra.settings import get_setting

    return get_setting(CURSOR_SETTING)


def test_cursor_is_not_advanced_when_the_first_read_backs_off():
    from events.connectors import github_notifications as poller

    _rate_limited(lambda: _notifications_read(Runner(429)))
    before = _cursor()
    gh = FakeGh()
    code = asyncio.run(poller.run_cycle(gh))
    assert code[0] is not None and code[1] == 1
    assert gh.api_calls() == [], "GitHub was read while backing off"
    assert _cursor() == before


def test_cursor_is_held_when_the_budget_runs_out_mid_cycle(monkeypatch):
    from events.connectors import github_notifications as poller

    gh = FakeGh()
    gh.issue_comments = [comment_object(501)]
    real = gh.__call__

    def limited(args, **kwargs):
        if args[-1].startswith("repos/"):
            return Runner(403, retry_after="60")(args)
        return real(args, **kwargs)

    before = _cursor()
    asyncio.run(poller.poll_once(limited))
    held = _cursor()
    assert held["cursor"] == before["cursor"] == FLOOR
    assert held["last_modified"] is None
    assert poller.poller_status()["state"] != "enabled"


def test_status_shows_the_budget(_budget_env):
    from events.connectors.github_notifications import poller_status
    from events.connectors.github_pr_quiet_watch import quiet_watch_status

    assert poller_status()["read_budget"] == {}
    _notifications_read(Runner(200, x_ratelimit_remaining="12", x_ratelimit_reset=str(int(RESET))))
    core = poller_status()["read_budget"]
    assert core == {
        "remaining": 12, "limit": 5000, "reset_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(RESET)),
        "backing_off": False, "backoff_until": None, "sweep_paused": True,
    }
    _notifications_read(Runner(200, x_ratelimit_remaining="0", x_ratelimit_reset=str(int(RESET))))
    core = poller_status()["read_budget"]
    assert core["backing_off"] is True and core["backoff_until"] == core["reset_at"]
    assert {"state", "last_run", "fired_count"} <= set(quiet_watch_status())
