"""PR-body ownership declarations claim only authenticated local PRs."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from tests.event_routing_helpers import mk_session, register_pr

OWN_LOGIN = "cebertowicz"
ACTIVE_THREAD = "01234567-89ab-4cde-8fab-0123456789ab"
OTHER_THREAD = "12345678-89ab-4cde-8fab-0123456789ab"
SHORT_THREAD = "f11109c1"


class _SubprocessProxy:
    def __init__(self, run):
        self.run = run

    def __getattr__(self, name):
        return getattr(subprocess, name)


@pytest.fixture(autouse=True)
def _reset_own_login_refresh(monkeypatch):
    """Keep routing tests independent of local GitHub CLI authentication."""
    from events.connectors import github_ownership

    refresh_done = threading.Event()
    refresh_done.set()
    monkeypatch.setattr(github_ownership, "_OWN_LOGIN", None)
    monkeypatch.setattr(github_ownership, "_LAST_REFRESH_MONOTONIC", float("-inf"))
    monkeypatch.setattr(github_ownership, "_REFRESH_LOCK", threading.Lock())
    monkeypatch.setattr(github_ownership, "_REFRESH_DONE", refresh_done)
    monkeypatch.setattr(
        github_ownership,
        "subprocess",
        _SubprocessProxy(
            lambda args, **_kwargs: CompletedProcess(args, 1, "", "gh unavailable"),
        ),
    )


def _payload(*, body: str, author: str | None = OWN_LOGIN) -> dict:
    return {
        "action": "opened",
        "repository": {"full_name": "cebertowicz/corsia"},
        "pull_request": {
            "number": 1187,
            "body": body,
            "user": {"login": author},
            "head": {"sha": "claim-head"},
            "base": {"ref": "dev"},
        },
        "sender": {"login": "github-webhook", "type": "User"},
    }


def _comment_payload(*, body: str, author: str = OWN_LOGIN) -> dict:
    payload = _payload(body=body, author=author)
    payload.pop("pull_request")
    payload["issue"] = {
        "number": 1187,
        "body": body,
        "user": {"login": author},
        "pull_request": {"url": "https://api.github.test/repos/o/r/pulls/1187"},
    }
    payload["comment"] = {
        "id": 5,
        "body": "please review",
        "user": {"login": "alice"},
    }
    return payload


async def _route(payload: dict):
    from events.router import route_event

    return await route_event("webhook.github.pr_opened", payload)


@pytest.mark.asyncio
async def test_own_author_single_active_declaration_claims_and_records_provenance(isolated_db):
    mk_session(SHORT_THREAD)
    with patch("events.connectors.github_ownership.cached_own_login", return_value=OWN_LOGIN), patch(
        "events.router.delivery.deliver_to_thread", return_value=True,
    ) as deliver:
        result = await _route(
            _payload(
                body=(
                    f"Owner thread ID: `{SHORT_THREAD}`\n"
                    f"Owner thread ID: `{SHORT_THREAD}`"
                )
            )
        )

    assert result.thread_id == SHORT_THREAD
    assert deliver.call_args.args[0] == SHORT_THREAD
    connection = sqlite3.connect(str(isolated_db))
    metadata = connection.execute(
        "SELECT metadata FROM artifact_identities WHERE external_id=?",
        ("cebertowicz/corsia#1187",),
    ).fetchone()[0]
    connection.close()
    assert json.loads(metadata)["ownership_provenance"] == "declared_in_pr_body"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("author", "body", "cached_login"),
    [
        ("dealcoach", f"Owner thread ID: {ACTIVE_THREAD}", OWN_LOGIN),
        ("dealcoach", "Owner thread ID: f73454e1", OWN_LOGIN),
        (OWN_LOGIN, "", OWN_LOGIN),
        (
            OWN_LOGIN,
            f"Owner thread ID: {ACTIVE_THREAD}\nOwner thread ID: {OTHER_THREAD}",
            OWN_LOGIN,
        ),
        (OWN_LOGIN, f"Owner thread ID: {ACTIVE_THREAD}", None),
        (None, f"Owner thread ID: {ACTIVE_THREAD}", None),
    ],
)
async def test_invalid_declarations_fall_through(isolated_db, author, body, cached_login):
    mk_session(ACTIVE_THREAD)
    with patch("events.connectors.github_ownership.cached_own_login", return_value=cached_login):
        result = await _route(_payload(body=body, author=author))

    from events.router import RouteOutcome

    assert result.outcome == RouteOutcome.DEAD_LETTERED


@pytest.mark.asyncio
async def test_unavailable_gh_does_not_route_a_nonexistent_declaration(isolated_db):
    mk_session(ACTIVE_THREAD)

    result = await _route(_payload(body=f"Owner thread ID: {OTHER_THREAD}"))

    from events.router import RouteOutcome

    assert result.outcome == RouteOutcome.DEAD_LETTERED


@pytest.mark.asyncio
async def test_unknown_or_inactive_declared_thread_falls_through(isolated_db):
    mk_session(ACTIVE_THREAD)
    connection = sqlite3.connect(str(isolated_db))
    connection.execute(
        "UPDATE threads SET lifecycle_state='archived', archived_reason='user' WHERE id=?",
        (ACTIVE_THREAD,),
    )
    connection.commit()
    connection.close()
    with patch("events.connectors.github_ownership.cached_own_login", return_value=OWN_LOGIN):
        inactive = await _route(_payload(body=f"Owner thread ID: {ACTIVE_THREAD}"))
        unknown = await _route(_payload(body=f"Owner thread ID: {OTHER_THREAD}"))

    from events.router import RouteOutcome

    assert inactive.outcome == RouteOutcome.DEAD_LETTERED
    assert unknown.outcome == RouteOutcome.DEAD_LETTERED


@pytest.mark.asyncio
async def test_existing_owner_is_not_overridden_by_a_declaration(isolated_db):
    existing_owner = mk_session(OTHER_THREAD)
    mk_session(ACTIVE_THREAD)
    register_pr("cebertowicz/corsia", 1187, existing_owner)
    with patch("events.connectors.github_ownership.cached_own_login", return_value=OWN_LOGIN), patch(
        "events.router.delivery.deliver_to_thread", return_value=True,
    ) as deliver:
        result = await _route(_payload(body=f"Owner thread ID: {ACTIVE_THREAD}"))

    assert result.thread_id == existing_owner
    assert deliver.call_args.args[0] == existing_owner


@pytest.mark.asyncio
async def test_pr_comment_uses_its_issue_pr_body_declaration(isolated_db, monkeypatch):
    monkeypatch.setattr("events.connectors.github_content_facts.is_own", lambda login: login == "alice")
    mk_session(ACTIVE_THREAD)
    with patch("events.connectors.github_ownership.cached_own_login", return_value=OWN_LOGIN), patch(
        "events.router.delivery.deliver_to_thread", return_value=True,
    ) as deliver:
        from events.router import route_event

        result = await route_event(
            "webhook.github.pr_comment",
            _comment_payload(body=f"  owner THREAD id: `{ACTIVE_THREAD}`  "),
        )

    assert result.thread_id == ACTIVE_THREAD
    assert deliver.call_args.args[0] == ACTIVE_THREAD


@pytest.mark.asyncio
async def test_startup_refresh_caches_the_authenticated_gh_login(monkeypatch):
    from app_startup_runtime import _start_github_ownership_refresh
    from events.connectors import github_ownership
    from infra import state

    monkeypatch.setattr(github_ownership, "_OWN_LOGIN", None)
    monkeypatch.setattr(state, "_github_ownership_login_task", None, raising=False)
    monkeypatch.setattr(
        github_ownership.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args, 0, f"{OWN_LOGIN}\n", ""),
    )

    await _start_github_ownership_refresh()
    refresh_task = state._github_ownership_login_task

    assert refresh_task is not None and refresh_task.done()
    assert github_ownership.cached_own_login() == OWN_LOGIN


@pytest.mark.asyncio
async def test_startup_refresh_wait_is_bounded(monkeypatch):
    import app_startup_runtime
    from events.connectors import github_ownership
    from infra import state

    release = threading.Event()

    def wedged_refresh():
        release.wait(5)
        return None

    monkeypatch.setattr(app_startup_runtime, "_OWN_LOGIN_STARTUP_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(github_ownership, "refresh_own_login", wedged_refresh)
    monkeypatch.setattr(state, "_github_ownership_login_task", None, raising=False)

    await app_startup_runtime._start_github_ownership_refresh()
    refresh_task = state._github_ownership_login_task

    assert refresh_task is not None and not refresh_task.done()
    release.set()
    await refresh_task


@pytest.mark.parametrize(
    "result",
    [
        CompletedProcess(["gh"], 1, "", "denied"),
        CompletedProcess(["gh"], 0, "\n", ""),
    ],
)
def test_refresh_own_login_rejects_unsuccessful_or_empty_output(monkeypatch, result):
    from events.connectors import github_ownership

    monkeypatch.setattr(github_ownership, "_OWN_LOGIN", "stale")
    monkeypatch.setattr(github_ownership.subprocess, "run", lambda *_args, **_kwargs: result)

    assert github_ownership.refresh_own_login() is None
    assert github_ownership.cached_own_login() is None


@pytest.mark.parametrize("failure", [OSError(), subprocess.TimeoutExpired(["gh"], 1)])
def test_refresh_own_login_handles_process_failures(monkeypatch, failure):
    from events.connectors import github_ownership

    def raise_failure(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(github_ownership.subprocess, "run", raise_failure)

    assert github_ownership.refresh_own_login() is None


def test_unknown_login_refreshes_off_the_caller_then_validates(monkeypatch):
    from events.connectors import github_ownership

    caller = threading.get_ident()
    gh_threads: list[int] = []

    def fake_run(args, **_kwargs):
        gh_threads.append(threading.get_ident())
        return CompletedProcess(args, 0, f"{OWN_LOGIN}\n", "")

    monkeypatch.setattr(github_ownership, "_OWN_LOGIN", None)
    monkeypatch.setattr(github_ownership, "_LAST_REFRESH_MONOTONIC", float("-inf"))
    monkeypatch.setattr(github_ownership.subprocess, "run", fake_run)
    payload = _payload(body=f"Owner thread ID: {ACTIVE_THREAD}")

    assert github_ownership.declaration_facts(payload) == {}
    for thread in threading.enumerate():
        if thread.name == "github-own-login-refresh":
            thread.join(5)

    assert gh_threads and caller not in gh_threads
    assert github_ownership.declaration_facts(payload) == {
        "declared_owner_thread": ACTIVE_THREAD,
    }


def test_unknown_login_retry_is_rate_limited(monkeypatch):
    from events.connectors import github_ownership

    calls: list[object] = []
    monkeypatch.setattr(github_ownership, "_OWN_LOGIN", None)
    monkeypatch.setattr(
        github_ownership, "_LAST_REFRESH_MONOTONIC", github_ownership.time.monotonic(),
    )
    monkeypatch.setattr(
        github_ownership.subprocess, "run", lambda *args, **_kwargs: calls.append(args),
    )

    assert github_ownership._ensure_own_login() is None
    assert not any(
        thread.name == "github-own-login-refresh" for thread in threading.enumerate()
    )
    assert calls == []
