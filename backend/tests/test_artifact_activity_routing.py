
from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

from infra import corsia_agent

import sqlite3
from unittest.mock import patch

import pytest

from events.identity import GITHUB_INTEGRATION, PR_TYPE, pr_external_id
from tests.event_routing_helpers import mk_session as _mk_session
from tests.event_routing_helpers import register_pr as _register_pr
from tests.event_routing_helpers import route as _route


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


def _comment_payload(
    repo: str, pr_number: int, *, body: str = "please review"
) -> dict:
    return {
        "action": "created",
        "repository": {"full_name": repo},
        "issue": {"number": pr_number, "pull_request": {"url": "https://api/pr"}},
        "comment": {
            "body": body,
            "html_url": f"https://example.test/{repo}/pull/{pr_number}#comment",
            "user": {"login": "alice", "type": "User"},
        },
        "sender": {"login": "alice", "type": "User"},
    }


def _add_subscriber(repo: str, pr_number: int, subscriber_thread: str) -> None:
    from registry import models

    models.register_artifact(
        integration=GITHUB_INTEGRATION,
        type=PR_TYPE,
        external_id=pr_external_id(repo, pr_number),
        parent_thread_id=subscriber_thread,
        title=f"PR #{pr_number}",
    )


def _passive_change_rows(db_path, target_thread_id: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT change.*,change.change_kind AS internal_event,"
        "json_extract(change.details_json,'$.external_id') AS external_id "
        "FROM context_change_journal AS change "
        "JOIN artifact_registrations AS registration "
        "ON registration.artifact_id=change.object_id "
        "WHERE change.object_kind='artifact' "
        "AND registration.parent_thread_id=? "
        "AND (change.actor_thread_id IS NULL OR change.actor_thread_id<>?) "
        "ORDER BY change.seq",
        (target_thread_id, target_thread_id),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _registrations_for_thread(db_path, thread_id: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT relationship_state FROM artifact_registrations "
        "WHERE parent_thread_id=? ORDER BY relationship_state",
        (thread_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_owner_wake_records_passive_activity_for_subscribers(isolated_db):
    owner = _mk_session("a2-owner")
    subscriber = _mk_session("a2-subscriber")
    _register_pr("o/r", 301, owner)
    _add_subscriber("o/r", 301, subscriber)

    with patch(
        "events.router.delivery.deliver_to_thread", return_value=True
    ) as mock_deliver:
            res = await _route(
                "webhook.github.pr_comment",
                _comment_payload(
                    "o/r", 301, body="subscriber should digest this later"
                ),
            )

    from events.router import RouteOutcome

    assert res.outcome == RouteOutcome.ACCEPTED
    assert res.thread_id == owner
    mock_deliver.assert_called_once()
    assert mock_deliver.call_args.args[0] == owner

    rows = _passive_change_rows(isolated_db, subscriber)
    assert len(rows) == 1
    assert rows[0]["internal_event"] == "webhook.github.pr_comment"
    assert rows[0]["external_id"] == "o/r#301"
    assert "subscriber should digest this later" in rows[0]["summary"]


@pytest.mark.asyncio
async def test_structured_event_tag_wakes_tagged_thread_without_owner_mutation(
    isolated_db,
):
    owner = _mk_session("a2-owner-tag")
    tagged = _mk_session("a2-tagged-thread")
    _register_pr("o/r", 302, owner)

    payload = _comment_payload("o/r", 302, body="@a2-tagged-thread take this one")
    payload["corsia_tags"] = [
        {
            "tag": "@a2-tagged-thread",
            "thread_id": tagged,
            "source": "structured-event-metadata",
        }
    ]

    with patch(
        "events.router.delivery.deliver_to_thread", return_value=True
    ) as mock_deliver:
        res = await _route("webhook.github.pr_comment", payload)

    from events.router import RouteOutcome

    assert res.outcome == RouteOutcome.ACCEPTED
    assert res.thread_id == tagged
    mock_deliver.assert_called_once()
    assert mock_deliver.call_args.args[0] == tagged
    assert mock_deliver.call_args.args[0] != owner
    details = mock_deliver.call_args.args[3]
    assert details["tag_provenance"] == {
        "tag": "@a2-tagged-thread",
        "thread_id": tagged,
        "source": "structured-event-metadata",
    }
    assert _registrations_for_thread(isolated_db, tagged) == []


@pytest.mark.asyncio
async def test_self_suppressed_event_does_not_record_passive_activity(isolated_db):
    owner = _mk_session("a2-owner-self")
    subscriber = _mk_session("a2-subscriber-self")
    _register_pr("o/r", 303, owner)
    _add_subscriber("o/r", 303, subscriber)

    with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
        res = await _route(
            "webhook.github.pr_comment",
            {
                **_comment_payload("o/r", 303),
                "comment": {
                    **_comment_payload("o/r", 303)["comment"],
                    "user": {"login": "corsia[bot]", "type": "Bot"},
                },
                "sender": {"login": "corsia[bot]", "type": "Bot"},
            },
        )

    from events.router import RouteOutcome

    assert res.outcome == RouteOutcome.SUPPRESSED
    mock_deliver.assert_not_called()
    assert _passive_change_rows(isolated_db, subscriber) == []


@pytest.mark.asyncio
async def test_no_owner_dead_letters_even_when_workspace_conductor_exists(
    isolated_db,
):
    conductor_thread = _mk_session("a2-workspace-conductor")
    from infra import conductor

    corsia_agent.set_corsia_agent(conductor_thread)

    with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
        res = await _route(
            "webhook.github.pr_comment",
            _comment_payload("o/r", 304, body="no owner should not guess workspace"),
        )

    from events.router import RouteOutcome

    assert res.outcome == RouteOutcome.DEAD_LETTERED
    assert res.reason == "no_owner"
    mock_deliver.assert_not_called()


@pytest.mark.asyncio
async def test_rehome_routes_future_events_to_new_owner_once_and_old_owner_passive(
    isolated_db,
):
    old_owner = _mk_session("a25-old-owner")
    new_owner = _mk_session("a25-new-owner")
    _register_pr("o/r", 305, old_owner)

    from registry import models

    models.rehome_artifact(
        integration=GITHUB_INTEGRATION,
        type=PR_TYPE,
        external_id=pr_external_id("o/r", 305),
        new_owner_thread_id=new_owner,
        actor_thread_id=old_owner,
        authority="current-owner-handoff",
        reason="rescue takeover",
        source_path="test",
    )

    with patch(
        "events.router.delivery.deliver_to_thread", return_value=True
    ) as mock_deliver:
        res = await _route(
            "webhook.github.pr_comment",
            _comment_payload("o/r", 305, body="future event follows rescue owner"),
        )

    from events.router import RouteOutcome

    assert res.outcome == RouteOutcome.ACCEPTED
    assert res.thread_id == new_owner
    mock_deliver.assert_called_once()
    assert mock_deliver.call_args.args[0] == new_owner

    old_rows = _passive_change_rows(isolated_db, old_owner)
    assert len(old_rows) == 1
    assert old_rows[0]["internal_event"] == "webhook.github.pr_comment"
    assert "future event follows rescue owner" in old_rows[0]["summary"]
    assert _passive_change_rows(isolated_db, new_owner) == []


@pytest.mark.asyncio
async def test_rehome_routes_future_check_suite_to_latest_owner(isolated_db):
    from tests.event_routing_helpers import check_suite_payload

    old_owner = _mk_session("a25-ci-old-owner")
    new_owner = _mk_session("a25-ci-new-owner")
    _register_pr("o/r", 308, old_owner)

    from registry import models

    models.rehome_artifact(
        integration=GITHUB_INTEGRATION,
        type=PR_TYPE,
        external_id=pr_external_id("o/r", 308),
        new_owner_thread_id=new_owner,
        actor_thread_id=old_owner,
        authority="current-owner-handoff",
        reason="rescue takeover before CI",
        source_path="test",
    )

    with patch(
        "events.router.delivery.deliver_to_thread", return_value=True
    ) as mock_deliver:
        res = await _route(
            "webhook.github.check_suite_completed",
            check_suite_payload("o/r", 308, conclusion="success"),
        )

    from events.router import RouteOutcome

    assert res.outcome == RouteOutcome.ACCEPTED
    assert res.thread_id == new_owner
    mock_deliver.assert_called_once()
    assert mock_deliver.call_args.args[0] == new_owner


@pytest.mark.asyncio
async def test_release_ownerless_pr_dead_letters_without_waking_old_owner(
    isolated_db,
):
    old_owner = _mk_session("a25-release-owner")
    _register_pr("o/r", 306, old_owner)

    from registry import models

    models.release_artifact(
        integration=GITHUB_INTEGRATION,
        type=PR_TYPE,
        external_id=pr_external_id("o/r", 306),
        actor_thread_id=old_owner,
        authority="current-owner-handoff",
        reason="owner released",
        source_path="test",
    )

    with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
        res = await _route(
            "webhook.github.pr_comment",
            _comment_payload("o/r", 306, body="ownerless should fail loud"),
        )

    from events.router import RouteOutcome

    assert res.outcome == RouteOutcome.DEAD_LETTERED
    assert res.reason == "no_owner"
    mock_deliver.assert_not_called()
    assert _passive_change_rows(isolated_db, old_owner) == []


@pytest.mark.asyncio
async def test_released_ownerless_pr_dead_letters_even_with_workspace_conductor(
    isolated_db,
):
    old_owner = _mk_session("a25-release-owner-with-conductor")
    conductor_thread = _mk_session("a25-release-workspace-conductor")
    _register_pr("o/r", 309, old_owner)

    from infra import conductor
    from registry import models

    corsia_agent.set_corsia_agent(conductor_thread)
    models.release_artifact(
        integration=GITHUB_INTEGRATION,
        type=PR_TYPE,
        external_id=pr_external_id("o/r", 309),
        actor_thread_id=old_owner,
        authority="current-owner-handoff",
        reason="owner released",
        source_path="test",
    )

    with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
        res = await _route(
            "webhook.github.pr_comment",
            _comment_payload("o/r", 309, body="ownerless should not fallback"),
        )

    from events.router import RouteOutcome

    assert res.outcome == RouteOutcome.DEAD_LETTERED
    assert res.reason == "no_owner"
    mock_deliver.assert_not_called()
    assert _passive_change_rows(isolated_db, old_owner) == []
    assert _passive_change_rows(isolated_db, conductor_thread) == []


@pytest.mark.asyncio
async def test_release_with_explicit_triage_routes_future_events_to_triage(
    isolated_db,
):
    old_owner = _mk_session("a25-triage-owner")
    triage = _mk_session("a25-triage-target")
    _register_pr("o/r", 307, old_owner)

    from registry import models

    models.release_artifact(
        integration=GITHUB_INTEGRATION,
        type=PR_TYPE,
        external_id=pr_external_id("o/r", 307),
        actor_thread_id=old_owner,
        authority="current-owner-handoff",
        reason="owner released to triage",
        source_path="test",
        triage_thread_id=triage,
    )

    with patch(
        "events.router.delivery.deliver_to_thread", return_value=True
    ) as mock_deliver:
        res = await _route(
            "webhook.github.pr_comment",
            _comment_payload("o/r", 307, body="triage should repair ownership"),
        )

    from events.router import RouteOutcome

    assert res.outcome == RouteOutcome.ACCEPTED
    assert res.thread_id == triage
    mock_deliver.assert_called_once()
    assert mock_deliver.call_args.args[0] == triage
    old_rows = _passive_change_rows(isolated_db, old_owner)
    assert len(old_rows) == 1
    assert "triage should repair ownership" in old_rows[0]["summary"]
