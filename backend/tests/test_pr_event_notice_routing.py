"""Silent binds learn from the routed event itself (design §7 test 2) and
check-suite deliveries are logged with the head the quiet sweep looks up.

A declaration or rule bind registers the owner without any tool call, so the
first routed event is the owner's first news of the PR.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from automation.continuation.models import StaleContinuation
from events.connectors import github_pr_notice as notices
from infra.settings import get_setting, set_setting
from tests.event_routing_helpers import (
    check_suite_payload,
    mk_session,
    pr_payload,
    register_pr,
    route,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    notices._owned_cache.clear()
    yield
    notices._owned_cache.clear()


def _contents(send: AsyncMock) -> list[str]:
    return [call.args[2] for call in send.await_args_list]


@pytest.mark.asyncio
async def test_notice_rides_the_first_routed_event_and_not_the_second(isolated_db):
    set_setting("webhook", {"github_webhook_secret": "configured"})
    owner = mk_session("silent-bind")
    register_pr("o/r", 41, owner)  # the bind wrote an owner row; no tool result followed

    with patch("events.delivery.send_thread_input", new=AsyncMock()) as send:
        await route("webhook.github.pr_synchronize", pr_payload("o/r", 41, action="synchronize"))
        await route("webhook.github.pr_synchronize", pr_payload("o/r", 41, action="synchronize"))

    first, second = _contents(send)
    assert first.startswith("[EVENT: webhook.github.pr_synchronize]")
    assert first.endswith(notices._notice(notices.OwnedPr("o/r#41", "o/r", 41, True)))
    assert "[CORSIA PR notice]" not in second
    assert get_setting(f"pr_notice:{owner}:o/r#41") is not None


@pytest.mark.asyncio
async def test_a_failed_delivery_leaves_the_notice_for_the_next_event(isolated_db):
    owner = mk_session("failed-delivery")
    register_pr("o/r", 44, owner)

    failed = AsyncMock(return_value=False)
    with patch("events.delivery.deliver_and_resolve_to_thread", new=failed):
        await route("webhook.github.pr_synchronize", pr_payload("o/r", 44, action="synchronize"))
    assert failed.await_args.kwargs["notice"]
    assert get_setting(f"pr_notice:{owner}:o/r#44") is None

    with patch("events.delivery.send_thread_input", new=AsyncMock()) as send:
        await route("webhook.github.pr_synchronize", pr_payload("o/r", 44, action="synchronize"))
    assert "[CORSIA PR notice]" in _contents(send)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("raised", [
    StaleContinuation("source moved"), asyncio.CancelledError(),
], ids=["stale-continuation", "cancelled"])
async def test_a_delivery_that_raises_leaves_the_notice_for_the_next_event(
    isolated_db, raised,
):
    """#2493 E4: the real send path raises at admission; the claim must not orphan."""
    owner = mk_session(f"raised-{type(raised).__name__}")
    register_pr("o/r", 45, owner)

    with patch("infra.send_service.admit_prepared_input", side_effect=raised):
        with pytest.raises(type(raised)):
            await route("webhook.github.pr_synchronize", pr_payload("o/r", 45, action="synchronize"))
    assert get_setting(f"pr_notice:{owner}:o/r#45") is None

    with patch("events.delivery.send_thread_input", new=AsyncMock()) as send:
        await route("webhook.github.pr_synchronize", pr_payload("o/r", 45, action="synchronize"))
    assert "[CORSIA PR notice]" in _contents(send)[0]


@pytest.mark.asyncio
async def test_a_tool_noticed_owner_gets_no_second_notice_on_its_event(isolated_db):
    owner = mk_session("tool-noticed")
    register_pr("o/r", 42, owner)
    assert notices.pr_event_notice(thread_id=owner, tool_name="read_file", arguments={})

    with patch("events.delivery.send_thread_input", new=AsyncMock()) as send:
        await route("webhook.github.pr_synchronize", pr_payload("o/r", 42, action="synchronize"))

    assert "[CORSIA PR notice]" not in _contents(send)[0]


def test_admitted_check_suite_is_logged_with_its_suite_before_any_owner(isolated_db):
    """The quiet sweep reads CI ingress from the delivery log, owned PR or not."""
    from events.connectors.github_admission import record_delivery_for_admission
    from infra.webhook_delivery_log import check_suites_received_at, last_check_suite_without_id

    completed = check_suite_payload("o/r", 43, suite_id=77)
    requested = {**completed, "action": "requested"}
    record_delivery_for_admission("check_suite", "requested", "d-1", "o/r", requested)
    since = "2000-01-01T00:00:00Z"
    assert check_suites_received_at([(77, since)]) == {}, "only a completed suite is a CI result"
    assert last_check_suite_without_id() is None, "an admitted suite always carries its id"

    record_delivery_for_admission("check_suite", "completed", "d-2", "O/R", completed)
    received = check_suites_received_at([(77, since), (78, since)])
    assert list(received) == [77], "only the suite that was delivered"
    assert check_suites_received_at([(77, "2999-01-01T00:00:00Z")]) == {}, (
        "a delivery before the attempt started does not answer it"
    )
