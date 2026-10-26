from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest

from webhook_ingress_fixtures import (  # noqa: F401
    WEBHOOK_SECRET,
    _disable_analytics,
    _isolated_db,
    _issue_comment_on_issue_payload,
    _issue_comment_on_pr_payload,
    _make_signature,
)


class TestHmacVerification:

    def test_valid_signature(self):
        from events.connectors.github_receiver import GITHUB_RECEIVER
        from events.connectors.receiver import verify_signature
        body = b'{"hello":"world"}'
        sig = _make_signature(body)
        assert verify_signature(GITHUB_RECEIVER, body, sig, WEBHOOK_SECRET) is True

    def test_invalid_signature(self):
        from events.connectors.github_receiver import GITHUB_RECEIVER
        from events.connectors.receiver import verify_signature
        body = b'{"hello":"world"}'
        sig = _make_signature(body, secret="wrong-secret")
        assert verify_signature(GITHUB_RECEIVER, body, sig, WEBHOOK_SECRET) is False

    def test_missing_prefix(self):
        from events.connectors.github_receiver import GITHUB_RECEIVER
        from events.connectors.receiver import verify_signature
        body = b'{"hello":"world"}'
        mac = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256)
        sig_no_prefix = mac.hexdigest()
        assert verify_signature(GITHUB_RECEIVER, body, sig_no_prefix, WEBHOOK_SECRET) is False

    def test_empty_signature(self):
        from events.connectors.github_receiver import GITHUB_RECEIVER
        from events.connectors.receiver import verify_signature
        assert verify_signature(GITHUB_RECEIVER, b"body", "", WEBHOOK_SECRET) is False

    def test_tampered_body(self):
        from events.connectors.github_receiver import GITHUB_RECEIVER
        from events.connectors.receiver import verify_signature
        original = b'{"original":"body"}'
        sig = _make_signature(original)
        tampered = b'{"tampered":"body"}'
        assert verify_signature(GITHUB_RECEIVER, tampered, sig, WEBHOOK_SECRET) is False



class TestEventTypeMapping:

    def test_pr_opened(self):
        from events.github_mapper import map_github_event
        result = map_github_event("pull_request", {"action": "opened", "pull_request": {}})
        assert result == "webhook.github.pr_opened"

    def test_pr_synchronize(self):
        from events.github_mapper import map_github_event
        result = map_github_event("pull_request", {"action": "synchronize"})
        assert result == "webhook.github.pr_synchronize"

    def test_pr_ready_for_review(self):
        from events.github_mapper import map_github_event
        result = map_github_event(
            "pull_request", {"action": "ready_for_review"}
        )
        assert result == "webhook.github.pr_ready_for_review"

    def test_pr_merged(self):
        from events.github_mapper import map_github_event
        payload = {"action": "closed", "pull_request": {"merged": True}}
        result = map_github_event("pull_request", payload)
        assert result == "webhook.github.pr_merged"

    def test_pr_closed_not_merged(self):
        from events.github_mapper import map_github_event
        payload = {"action": "closed", "pull_request": {"merged": False}}
        result = map_github_event("pull_request", payload)
        assert result == "webhook.github.pr_closed"

    def test_issue_comment_on_pr(self):
        from events.github_mapper import map_github_event
        result = map_github_event("issue_comment", _issue_comment_on_pr_payload())
        assert result == "webhook.github.pr_comment"

    def test_issue_comment_on_issue(self):
        from events.github_mapper import map_github_event
        result = map_github_event("issue_comment", _issue_comment_on_issue_payload())
        assert result == "webhook.github.issue_comment"

    def test_pr_review_comment_from_human(self):
        from events.github_mapper import map_github_event
        result = map_github_event(
            "pull_request_review_comment",
            {"action": "created",
             "comment": {"body": "LGTM"},
             "sender": {"login": "alice", "type": "User"}},
        )
        assert result == "webhook.github.pr_comment"

    def test_pr_review_comment_no_sender_is_not_noise(self):
        from events.github_mapper import map_github_event
        result = map_github_event("pull_request_review_comment", {"action": "created"})
        assert result == "webhook.github.pr_comment"

    def test_check_suite_completed(self):
        from events.github_mapper import map_github_event
        result = map_github_event("check_suite", {"action": "completed"})
        assert result == "webhook.github.check_suite_completed"

    def test_check_suite_rerequested_returns_none(self):
        from events.github_mapper import map_github_event
        result = map_github_event("check_suite", {"action": "rerequested"})
        assert result is None

    def test_check_run_all_actions_return_none(self):
        from events.github_mapper import map_github_event
        for action in ["completed", "created", "rerequested", "requested_action"]:
            assert map_github_event("check_run", {"action": action}) is None, (
                f"check_run.{action} must return None"
            )

    def test_unknown_event(self):
        from events.github_mapper import map_github_event
        assert map_github_event("deployment", {"action": "created"}) is None

    def test_unhandled_action(self):
        from events.github_mapper import map_github_event
        assert map_github_event("pull_request", {"action": "edited"}) is None



class TestDeliveryDedup:

    def test_normal_delivery_does_not_retain_payload(self, _isolated_db):
        from infra.database import get_db
        from infra.webhook_delivery_log import record_delivery

        did = str(uuid.uuid4())
        record_delivery(
            event_type="pull_request",
            action="opened",
            delivery_id=did,
            repo="owner/repo",
            payload={"large": "body"},
        )

        db = get_db()
        try:
            row = db.execute(
                "SELECT payload_json FROM webhook_deliveries WHERE delivery_id=?",
                (did,),
            ).fetchone()
        finally:
            db.close()
        assert row["payload_json"] is None

    def test_restart_held_delivery_retains_payload(self, _isolated_db):
        from infra.database import get_db
        from infra.webhook_delivery_log import record_delivery

        did = str(uuid.uuid4())
        record_delivery(
            event_type="pull_request",
            action="opened",
            delivery_id=did,
            repo="owner/repo",
            payload={"needed": "for replay"},
            status="held_restart",
        )

        db = get_db()
        try:
            row = db.execute(
                "SELECT payload_json FROM webhook_deliveries WHERE delivery_id=?",
                (did,),
            ).fetchone()
        finally:
            db.close()
        assert json.loads(row["payload_json"]) == {"needed": "for replay"}

    @pytest.mark.parametrize(
        ("status", "payload_retained"),
        (("dispatched", False), ("retryable_restart", True)),
    )
    def test_restart_replay_retains_payload_only_while_retryable(
        self,
        _isolated_db,
        status,
        payload_retained,
    ):
        from automation.restart_webhooks import _claim, _settle
        from infra.database import get_db
        from infra.webhook_delivery_log import record_delivery

        did = str(uuid.uuid4())
        record_delivery(
            event_type="pull_request",
            action="opened",
            delivery_id=did,
            repo="owner/repo",
            payload={"needed": "for replay"},
            status="held_restart",
        )
        claim = _claim(did, "held_restart")
        assert claim is not None
        assert _settle(did, claim, status=status)

        db = get_db()
        try:
            row = db.execute(
                "SELECT payload_json FROM webhook_deliveries WHERE delivery_id=?",
                (did,),
            ).fetchone()
        finally:
            db.close()
        assert (row["payload_json"] is not None) is payload_retained

    def test_second_delivery_returns_the_canonical_duplicate(self, _isolated_db):
        from infra.webhook_delivery_log import record_delivery

        did = str(uuid.uuid4())
        first = record_delivery(
            event_type="pull_request",
            action="opened",
            delivery_id=did,
            repo="owner/repo",
            payload={"action": "opened"},
        )
        duplicate = record_delivery(
            event_type="pull_request",
            action="opened",
            delivery_id=did,
            repo="owner/repo",
            payload={"action": "opened"},
        )

        assert first.created is True
        assert duplicate.row_id == first.row_id
        assert duplicate.delivery_id == did
        assert duplicate.status == "received"
        assert duplicate.created is False

    def test_status_update(self, _isolated_db):
        from infra.webhook_delivery_log import record_delivery, update_delivery_status
        from infra.database import get_db
        did = str(uuid.uuid4())
        record_delivery(
            event_type="pull_request",
            action="opened",
            delivery_id=did,
            repo="owner/repo",
            payload={"test": True},
        )
        update_delivery_status(did, "dispatched")
        db = get_db()
        row = db.execute(
            "SELECT status FROM webhook_deliveries WHERE delivery_id = ?", (did,)
        ).fetchone()
        db.close()
        assert row["status"] == "dispatched"
