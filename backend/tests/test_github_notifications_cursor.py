"""The poller's cursor only moves past work it fully handled."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from github_notifications_fixtures import (
    FLOOR,
    FakeGh,
    comment_object,
    notification_thread,
)

SERVER_TIME = "2026-09-24T05:31:00Z"


@pytest.fixture(autouse=True)
def _poller_env(isolated_db, monkeypatch):
    from events.connectors import github_notifications as poller
    from infra.settings import set_setting

    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv(poller.DISABLE_ENV, raising=False)
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
    yield


def _cycle(gh: FakeGh) -> tuple[int | None, int]:
    from events.connectors import github_notifications as poller
    from events.connectors.github_webhook import _BG_DISPATCH

    async def _run():
        outcome = await poller.run_cycle(gh)
        while _BG_DISPATCH:
            await asyncio.gather(*list(_BG_DISPATCH), return_exceptions=True)
        return outcome

    return asyncio.run(_run())


def _state() -> tuple[str, str | None]:
    from events.connectors.github_notifications import poller_status

    status = poller_status()
    return status["state"], status["reason"]


def _cursor() -> dict:
    from events.connectors.github_notifications import CURSOR_SETTING
    from infra.settings import get_setting

    return get_setting(CURSOR_SETTING)


def _object_keys() -> list[str]:
    from infra.database import get_db

    return [row[0] for row in get_db().execute(
        "SELECT object_key FROM webhook_deliveries ORDER BY created_at"
    )]


def test_admission_failure_holds_the_cursor_and_the_comment_is_read_again():
    gh = FakeGh()
    gh.issue_comments = [comment_object(501)]
    with patch(
        "events.connectors.github_admission.record_delivery_for_admission",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        _cycle(gh)
    assert _object_keys() == []
    assert _state() == ("degraded", "admission_failed")
    held = _cursor()
    assert held["cursor"] == "2026-09-24T05:30:00Z", "the cursor passed an unhandled comment"
    assert held["last_modified"] is None, "a conditional read would 304 past the retry"

    retry = FakeGh()
    retry.issue_comments = [comment_object(501)]
    _cycle(retry)
    assert not any("If-Modified-Since" in part for part in retry.api_calls()[0])
    assert _object_keys() == ["issue_comment:501"]
    assert _state() == ("enabled", None)
    assert _cursor()["cursor"] == SERVER_TIME


def test_foreign_repository_is_dropped_before_its_comment_is_read(monkeypatch):
    """The registered repository policies scope notifications before payload reads."""
    monkeypatch.setattr(
        "infra.project_repo_policy.list_registered_repo_policies",
        lambda: [SimpleNamespace(source_binding_valid=True, enforcement_error=None, path="/tracked")],
    )
    monkeypatch.setattr(
        "infra.worktree_reaper_probe.github_remote_full_name",
        lambda _path, remote: "tracked/repository" if remote == "origin" else None,
    )
    gh = FakeGh()
    gh.issue_comments = [comment_object(501)]

    _cycle(gh)

    assert _object_keys() == []
    assert [call[-1].split("?")[0] for call in gh.api_calls()] == ["notifications"]
    from events.connectors.github_notifications import poller_status

    assert poller_status()["dropped_notifications"] == 1


def test_upstream_repository_from_a_fork_is_admitted(monkeypatch):
    """Forked source checkouts track notifications from their upstream repository."""
    monkeypatch.setattr(
        "infra.worktree_reaper_probe.github_remote_full_name",
        lambda _path, remote: {"origin": "worker/fork", "upstream": "o/r"}.get(remote),
    )
    gh = FakeGh()
    gh.issue_comments = [comment_object(501)]

    _cycle(gh)

    assert _object_keys() == ["issue_comment:501"]


def test_origin_only_repository_stays_admitted(monkeypatch):
    """A source checkout without upstream preserves origin-only behavior."""
    monkeypatch.setattr(
        "infra.worktree_reaper_probe.github_remote_full_name",
        lambda _path, remote: "o/r" if remote == "origin" else None,
    )
    gh = FakeGh()
    gh.issue_comments = [comment_object(501)]

    _cycle(gh)

    assert _object_keys() == ["issue_comment:501"]


def test_invalid_body_holds_the_cursor_for_a_retry():
    gh = FakeGh()
    gh.issue_comments = [comment_object(501)]
    gh.routes = {"repos/o/r/issues/42": "not an issue object"}

    _cycle(gh)

    assert _state() == ("degraded", "thread_invalid_body")
    assert _cursor()["cursor"] == FLOOR
    assert _cursor()["last_modified"] is None


def test_auth_status_failure_is_not_mislabeled_signed_out(caplog):
    from events.connectors import github_notifications as poller

    def timeout(_args, **_kwargs):
        raise subprocess.TimeoutExpired("gh auth status", 30)

    assert poller.disabled_reason(timeout) == "gh_error"
    assert "TimeoutExpired" in caplog.text


def test_only_the_active_account_decides_signed_in():
    """A failing second stored account must not turn the poller off."""
    from events.connectors import github_notifications as poller
    from events.connectors import github_pr_quiet_watch as watch

    calls: list[list[str]] = []

    def gh(args, **_kwargs):
        calls.append(list(args))
        active = args == ["auth", "status", "--hostname", "github.com", "--active"]
        return subprocess.CompletedProcess(args, 0 if active else 1, "", "")

    assert poller.disabled_reason(gh) is None
    assert watch.disabled_reason(gh) is None
    assert calls == [["auth", "status", "--hostname", "github.com", "--active"]] * 2


@pytest.mark.parametrize("stderr", [
    "You are not logged into any GitHub hosts. To log in, run: gh auth login\n",
    "You are not logged into any accounts on github.com\n",
    'Hostname "github.com" not found among authenticated GitHub hosts\n',
])
def test_no_github_com_account_reads_signed_out(stderr):
    """Signed in to another host only is signed out here, for both pollers."""
    from events.connectors import github_notifications as poller
    from events.connectors import github_pr_quiet_watch as watch

    def gh(args, **_kwargs):
        return subprocess.CompletedProcess(args, 1, "", stderr)

    assert poller.disabled_reason(gh) == "gh_signed_out"
    assert watch.disabled_reason(gh) == "gh_signed_out"


def test_failure_after_the_row_is_written_counts_as_handled():
    gh = FakeGh()
    gh.issue_comments = [comment_object(502)]
    with patch(
        "events.connectors.github_webhook.schedule_github_webhook",
        side_effect=RuntimeError("scheduler down"),
    ):
        _cycle(gh)
    from infra.database import get_db

    status = get_db().execute(
        "SELECT status FROM webhook_deliveries WHERE object_key=?", ("issue_comment:502",),
    ).fetchone()[0]
    assert status == "failed"
    assert _state() == ("enabled", None)
    assert _cursor()["cursor"] == SERVER_TIME


@pytest.mark.parametrize(
    ("status", "state", "cursor", "skipped"),
    [
        (404, ("enabled", None), SERVER_TIME, 1),
        (410, ("enabled", None), SERVER_TIME, 1),
        (403, ("enabled", None), SERVER_TIME, 1),
        (502, ("degraded", "thread_http_502"), FLOOR, 0),
    ],
)
def test_one_bad_thread_never_blocks_the_others(status, state, cursor, skipped):
    from events.connectors.github_notifications import poller_status

    gh = FakeGh()
    gh.threads = [notification_thread(7, kind="issues"), notification_thread()]
    gh.routes = {"repos/o/r/issues/7/comments": status}
    gh.issue_comments = [comment_object(501)]
    _cycle(gh)
    assert _object_keys() == ["issue_comment:501"]
    assert _state() == state
    assert _cursor()["cursor"] == cursor
    assert poller_status()["skipped_threads"] == skipped


def test_truncated_feed_holds_the_cursor(monkeypatch):
    from events.connectors import github_notifications_feed as feed

    monkeypatch.setattr(feed, "MAX_PAGES", 1)
    gh = FakeGh()
    gh.notifications_next = True
    gh.issue_comments = [comment_object(501)]
    _cycle(gh)
    assert _object_keys() == ["issue_comment:501"], "what was read is still admitted"
    assert _state() == ("degraded", "truncated")
    assert _cursor()["cursor"] == FLOOR
    assert _cursor()["last_modified"] is None


@pytest.mark.parametrize("reason", ["subscribed", "team_mention"])
def test_threads_the_account_did_not_take_part_in_are_not_read(reason):
    gh = FakeGh()
    gh.threads = [notification_thread(reason=reason)]
    gh.issue_comments = [comment_object(501)]
    _cycle(gh)
    assert [call[-1].split("?")[0] for call in gh.api_calls()] == ["notifications"]
    assert _object_keys() == []


def test_rate_limited_403_is_transient_not_a_skip():
    from events.connectors.github_notifications_feed import FeedError, gh_get

    def _runner(args, **_kwargs):
        stdout = "HTTP/2.0 403 Forbidden\r\nX-RateLimit-Remaining: 0\r\n\r\n{}"
        return subprocess.CompletedProcess(args, 1, stdout, "")

    with pytest.raises(FeedError) as caught:
        gh_get(_runner, "repos/o/r/issues/1/comments")
    assert caught.value.error_class == "rate_limited"


def test_sqlite_work_runs_off_the_event_loop():
    from events.connectors import github_admission
    from events.connectors import github_notifications as poller

    seen: dict[str, str] = {}

    def _spy(name, fn):
        def wrapped(*args, **kwargs):
            seen[name] = threading.current_thread().name
            return fn(*args, **kwargs)
        return wrapped

    gh = FakeGh()
    gh.issue_comments = [comment_object(501)]
    with patch.object(poller, "load_cursor", _spy("load", poller.load_cursor)), \
            patch.object(poller, "_save_cursor", _spy("save", poller._save_cursor)), \
            patch.object(github_admission, "_record_phase",
                         _spy("record", github_admission._record_phase)), \
            patch.object(github_admission, "_settle_phase",
                         _spy("settle", github_admission._settle_phase)):
        loop_thread = threading.current_thread().name
        _cycle(gh)
    assert set(seen) == {"load", "save", "record", "settle"}
    assert loop_thread not in seen.values()
