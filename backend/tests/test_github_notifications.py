"""The notifications poller admits comments exactly as the webhook does."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from github_notifications_fixtures import (
    FLOOR,
    LAST_MODIFIED,
    PR,
    REPO,
    FakeGh,
    comment_object,
    webhook_issue_comment,
    webhook_review_comment,
)
from tests.event_routing_helpers import mk_session, register_pr
from webhook_ingress_fixtures import WEBHOOK_SECRET, _make_signature


@pytest.fixture(autouse=True)
def _poller_env(isolated_db, monkeypatch):
    from events.connectors import github_notifications as poller
    from infra.settings import set_setting

    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: WEBHOOK_SECRET)
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
        lambda _path, remote: REPO if remote == "origin" else None,
    )
    set_setting(poller.CURSOR_SETTING, {"floor": FLOOR, "cursor": FLOOR})
    yield


class _Delivered:
    """Capture what reaches an owning thread through the real router."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, thread_id, identity, internal_event, summary, details, **kwargs):
        self.calls.append((thread_id, internal_event, summary, details, kwargs.get("chip")))
        return True


async def _settle_dispatch() -> None:
    from events.connectors.github_webhook import _BG_DISPATCH

    while _BG_DISPATCH:
        await asyncio.gather(*list(_BG_DISPATCH), return_exceptions=True)


async def _post_webhook(event_type: str, payload: dict, delivery_id: str):
    import httpx
    from main import app

    raw = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event_type, "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": _make_signature(raw),
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/webhooks/github", content=raw, headers=headers)
    await _settle_dispatch()
    return response.json()


async def _poll(gh: FakeGh) -> int:
    from events.connectors.github_notifications import poll_once

    result = await poll_once(gh)
    await _settle_dispatch()
    return result.interval


def _delivery_rows() -> list[dict]:
    from infra.database import get_db

    return [dict(row) for row in get_db().execute(
        "SELECT delivery_id, object_key, status FROM webhook_deliveries ORDER BY created_at"
    )]


@pytest.mark.parametrize("through_broker", [False, True], ids=["runner", "read_broker"])
def test_every_github_call_is_a_read(through_broker):
    from connections.github_broker import GithubReadBroker

    gh = FakeGh()
    gh.issue_comments = [comment_object(501)]
    gh.review_comments = [comment_object(601)]
    # The broker refuses a write or an untracked repository by raising, which fails the poll.
    asyncio.run(_poll(GithubReadBroker(gh, tracked=lambda: frozenset({REPO})) if through_broker else gh))
    assert gh.api_calls(), "the poll made no GitHub reads"
    for call in gh.api_calls():
        assert call[:4] == ["api", "--method", "GET", "-i"]
        assert not {"-f", "-F", "--field", "--raw-field", "--input", "-X"} & set(call[4:])
        assert "notifications/threads" not in call[-1]


def test_polled_comment_routes_exactly_as_the_webhook_payload():
    owner = mk_session("own-1")
    register_pr(REPO, PR, owner)
    comment = comment_object(501)
    review = comment_object(601, body="nit: rename")

    async def _run():
        from events.router import route_event

        webhook = _Delivered()
        with patch("events.router.delivery.deliver_and_resolve_to_thread", webhook):
            await route_event("webhook.github.pr_comment", webhook_issue_comment(comment))
            await route_event("webhook.github.pr_comment", webhook_review_comment(review))
        polled = _Delivered()
        gh = FakeGh()
        gh.issue_comments, gh.review_comments = [comment], [review]
        with patch("events.router.delivery.deliver_and_resolve_to_thread", polled):
            await _poll(gh)
        return webhook.calls, polled.calls

    webhook_calls, polled_calls = asyncio.run(_run())
    assert len(webhook_calls) == 2
    assert polled_calls == webhook_calls
    assert {call[0] for call in polled_calls} == {owner}


@pytest.mark.parametrize("first", ["webhook", "poller"])
def test_one_comment_is_admitted_once_whichever_source_sees_it_first(first):
    comment = comment_object(777)
    routed: list[str] = []

    async def _count_route(internal_event, payload, delivery_id=None):
        routed.append(delivery_id)

    async def _run():
        from events.connectors.github_admission import admit_github_delivery_async
        from events.connectors.github_notifications_feed import comment_payloads
        from datetime import datetime, UTC

        gh = FakeGh()
        gh.issue_comments = [comment]
        webhook_payload = webhook_issue_comment(comment)
        with patch("events.ingress.route_leg", side_effect=_count_route):
            if first == "webhook":
                first_response = await _post_webhook("issue_comment", webhook_payload, "gh-guid-1")
                assert first_response["status"] == "ok"
                await _poll(gh)
                # Bypass the poller's pre-check: the log's own atomic dedupe holds too.
                polled = comment_payloads(
                    gh, gh.threads[0], datetime(2026, 9, 24, 5, tzinfo=UTC), lambda _key: False,
                )
                second = await admit_github_delivery_async(
                    polled[0][0], polled[0][1], "gh-notifications:x", source="notifications",
                )
                assert second.content["status"] == "duplicate"
            else:
                await _poll(gh)
                second = await _post_webhook("issue_comment", webhook_payload, "gh-guid-1")
                assert second["status"] == "duplicate"
            await _settle_dispatch()

    asyncio.run(_run())
    assert len(routed) == 1
    rows = _delivery_rows()
    assert [row["object_key"] for row in rows] == ["issue_comment:777"]


def test_cursor_resumes_after_restart_and_first_run_never_replays(monkeypatch):
    from datetime import UTC, datetime

    from events.connectors import github_notifications as poller
    from infra.database import get_db
    from infra.settings import get_setting

    monkeypatch.setattr(poller, "_now", lambda: datetime(2026, 9, 24, 5, 20, tzinfo=UTC))
    get_db().execute("DELETE FROM settings WHERE key=?", (poller.CURSOR_SETTING,))
    get_db().commit()
    gh = FakeGh()
    gh.issue_comments = [comment_object(10, created_at="2026-01-01T00:00:00Z")]
    asyncio.run(_poll(gh))
    assert _delivery_rows() == [], "a first run replayed history"

    stored = get_setting(poller.CURSOR_SETTING)
    assert stored["floor"] == "2026-09-24T05:20:00Z"
    assert stored["last_modified"] == LAST_MODIFIED
    assert stored["cursor"] == "2026-09-24T05:31:00Z"

    # A restarted process reads only what the database holds.
    restarted = FakeGh()
    restarted.notifications_status = 304
    monkeypatch.setattr(poller, "_status", dict(poller._status))
    asyncio.run(_poll(restarted))
    request = restarted.api_calls()[0]
    assert f"If-Modified-Since: {LAST_MODIFIED}" in request
    assert "since=2026-09-24T05:26:00Z" in request[-1]


def test_not_modified_makes_no_further_calls_and_keeps_the_cursor():
    from events.connectors import github_notifications as poller
    from infra.settings import get_setting

    before = get_setting(poller.CURSOR_SETTING)
    gh = FakeGh()
    gh.notifications_status = 304
    gh.issue_comments = [comment_object(1)]
    assert asyncio.run(_poll(gh)) == 60
    assert len(gh.api_calls()) == 1
    assert get_setting(poller.CURSOR_SETTING) == before


@pytest.mark.parametrize(
    ("env", "gh_state", "reason", "delay"),
    [
        ({"CORSIA_TEST_MODE": "1"}, None, "test_mode", None),
        ({"CORSIA_GITHUB_NOTIFICATIONS_DISABLE": "1"}, None, "config_off", None),
        ({}, "missing", "gh_missing", 300),
        ({}, "signed_out", "gh_signed_out", 300),
        # The opt-in lifts test mode only; the auth check still applies.
        (
            {"CORSIA_TEST_MODE": "1", "CORSIA_GITHUB_NOTIFICATIONS_TEST_ENABLE": "1"},
            "signed_out", "gh_signed_out", 300,
        ),
        # The disable switch still wins, in and out of test mode.
        (
            {
                "CORSIA_TEST_MODE": "1",
                "CORSIA_GITHUB_NOTIFICATIONS_TEST_ENABLE": "1",
                "CORSIA_GITHUB_NOTIFICATIONS_DISABLE": "1",
            },
            None, "config_off", None,
        ),
        (
            {
                "CORSIA_GITHUB_NOTIFICATIONS_TEST_ENABLE": "1",
                "CORSIA_GITHUB_NOTIFICATIONS_DISABLE": "1",
            },
            None, "config_off", None,
        ),
    ],
)
def test_each_disabled_state_is_visible_and_reads_nothing(monkeypatch, env, gh_state, reason, delay):
    from events.connectors import github_notifications as poller

    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv(poller.DISABLE_ENV, raising=False)
    monkeypatch.delenv(poller.TEST_ENABLE_ENV, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    gh = FakeGh()
    gh.missing = gh_state == "missing"
    gh.auth_rc = 1 if gh_state == "signed_out" else 0
    assert asyncio.run(poller.run_cycle(gh)) == (delay, 0)
    assert gh.api_calls() == []
    status = poller.poller_status()
    assert (status["state"], status["reason"]) == ("disabled", reason)


@pytest.mark.parametrize("test_mode", ["1", None])
def test_the_opt_in_lets_a_signed_in_gh_poll_in_and_out_of_test_mode(monkeypatch, test_mode):
    from events.connectors import github_notifications as poller

    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv(poller.DISABLE_ENV, raising=False)
    monkeypatch.setenv(poller.TEST_ENABLE_ENV, "1")
    if test_mode is not None:
        monkeypatch.setenv("CORSIA_TEST_MODE", test_mode)
    gh = FakeGh()
    assert poller.disabled_reason(gh) is None
    assert asyncio.run(poller.run_cycle(gh)) == (60, 0)
    status = poller.poller_status()
    assert (status["state"], status["reason"]) == ("enabled", None)
    assert gh.api_calls(), "the poller actually polled"


def test_failures_are_visible_then_clear_on_success(monkeypatch):
    from events.connectors import github_notifications as poller

    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    gh = FakeGh()
    gh.notifications_status = 500
    delay, failures = asyncio.run(poller.run_cycle(gh))
    assert (delay, failures) == (120, 1)
    status = poller.poller_status()
    assert (status["state"], status["reason"]) == ("failing", "http_500")
    assert status["last_error"]["class"] == "http_500"

    gh.notifications_status = 401
    assert asyncio.run(poller.run_cycle(gh, failures))[0] == poller.RECHECK_S
    assert poller.poller_status()["reason"] == "gh_signed_out"

    gh.notifications_status = 200
    assert asyncio.run(poller.run_cycle(gh)) == (60, 0)
    status = poller.poller_status()
    assert status["state"] == "enabled" and status["last_success_at"]
    assert status["last_error"]["class"] == "http_500"


def test_system_status_read_exposes_poller_state(monkeypatch):
    import time

    from api.system import system_status

    monkeypatch.setattr(
        "api.system._system_cache", {"data": {"memory": {}}, "ts": time.monotonic()},
    )
    result = asyncio.run(system_status(user={"id": "user-1"}))
    assert result["github_notifications"]["state"] == "starting"


def test_reviewer_marker_and_self_authored_comments_stay_suppressed():
    from infra.settings import set_setting

    owner = mk_session("own-1")
    register_pr(REPO, PR, owner)
    set_setting("event_routing", {"self_authors": ["corsia-automation"]})
    gh = FakeGh()
    gh.issue_comments = [
        comment_object(901, body="Review done.\n\n<!-- corsia-review-summary -->"),
        comment_object(902, body="automation note", login="corsia-automation"),
        comment_object(903, body="x\n<!-- corsia-review-fp: deadbeef -->",
                       login="reviewer[bot]", user_type="Bot"),
    ]
    delivered = _Delivered()
    with patch("events.router.delivery.deliver_and_resolve_to_thread", delivered):
        asyncio.run(_poll(gh))
    assert delivered.calls == []
    statuses = {row["object_key"]: row["status"] for row in _delivery_rows()}
    assert statuses == {
        "issue_comment:901": "dispatched",
        "issue_comment:902": "dispatched",
        "issue_comment:903": "ignored",
    }


def test_object_lookup_reuses_the_thread_pooled_connection():
    """The poller's per-comment dedupe check opens no connection per call."""
    from concurrent.futures import ThreadPoolExecutor

    import infra.database as database
    from infra import webhook_delivery_log

    webhook_delivery_log.object_recorded("issue_comment:0")
    baseline = len(database._all_connections)
    for n in range(50):
        assert not webhook_delivery_log.object_recorded(f"issue_comment:{n}")
    with ThreadPoolExecutor(max_workers=1) as worker:
        for n in range(50):
            worker.submit(webhook_delivery_log.object_recorded, f"issue_comment:{n}").result()
        # The worker thread holds exactly one pooled connection, however many lookups it ran.
        assert len(database._all_connections) == baseline + 1
    assert len(database._all_connections) <= baseline + 1
