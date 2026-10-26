from __future__ import annotations

import asyncio
import json
import uuid
from threading import Event
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from webhook_ingress_fixtures import (  # noqa: F401
    WEBHOOK_SECRET,
    _check_suite_payload,
    _isolated_db,
    _issue_comment_on_pr_payload,
    _make_signature,
    _pr_opened_payload,
    _pr_ready_for_review_payload,
)


class _DirectWebhookRequest:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


def _direct_webhook_request(
    payload: dict,
    *,
    event_type: str = "pull_request",
    delivery_id: str | None = None,
) -> _DirectWebhookRequest:
    body = json.dumps(payload).encode()
    return _DirectWebhookRequest(
        body,
        {
            "X-GitHub-Event": event_type,
            "X-GitHub-Delivery": delivery_id or str(uuid.uuid4()),
            "X-Hub-Signature-256": _make_signature(body),
            "Content-Type": "application/json",
        },
    )


class TestWebhookEndpoint:

    @pytest.fixture(autouse=True)
    def _setup(self, _isolated_db, monkeypatch):
        monkeypatch.setattr(
            "api.webhook_receivers.receiver_secret",
            lambda connector: WEBHOOK_SECRET,
        )

        from main import app
        self.client = TestClient(app, raise_server_exceptions=False)
        yield

    def _post_webhook(
        self,
        payload: dict,
        event_type: str = "pull_request",
        delivery_id: str | None = None,
        secret: str = WEBHOOK_SECRET,
        include_sig: bool = True,
    ):
        body = json.dumps(payload).encode()
        headers = {
            "X-GitHub-Event": event_type,
            "X-GitHub-Delivery": delivery_id or str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        if include_sig:
            headers["X-Hub-Signature-256"] = _make_signature(body, secret)
        return self.client.post("/webhooks/github", content=body, headers=headers)

    def test_valid_pr_opened(self):
        resp = self._post_webhook(_pr_opened_payload())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "ignored")  # ok if handlers, ignored if none

    def test_missing_signature_returns_401(self):
        payload = _pr_opened_payload()
        body = json.dumps(payload).encode()
        headers = {
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()),
        }
        resp = self.client.post("/webhooks/github", content=body, headers=headers)
        assert resp.status_code == 401

    def test_invalid_signature_returns_401(self):
        resp = self._post_webhook(
            _pr_opened_payload(),
            secret="wrong-secret",
        )
        assert resp.status_code == 401

    def test_duplicate_delivery_returns_duplicate(self):
        did = str(uuid.uuid4())
        resp1 = self._post_webhook(_pr_opened_payload(), delivery_id=did)
        assert resp1.status_code == 200

        resp2 = self._post_webhook(_pr_opened_payload(), delivery_id=did)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "duplicate"

    def test_unhandled_event_returns_ignored(self):
        resp = self._post_webhook(
            {"action": "created"},
            event_type="deployment",
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_pr_opened_routes_and_reviews_off_loop(self):
        review_log: list[str] = []
        completed = Event()

        async def _fake_route(internal_event, payload, delivery_id=None):
            return None

        def _fake_review(internal_event, payload):
            review_log.append(internal_event)
            completed.set()
            return True

        with patch("events.ingress.review_leg", side_effect=_fake_review), \
             patch("events.ingress.route_leg", side_effect=_fake_route):
            resp = self._post_webhook(_pr_opened_payload())
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["routing"] == "async"
            assert completed.wait(5.0)
        assert review_log == ["webhook.github.pr_opened"]

    def test_check_suite_completed_routes_and_runs_ci_gate_off_loop(self):
        gate_log: list[str] = []
        gate_loop_running: list[bool] = []
        route_delivery_ids: list[str | None] = []
        completed = Event()

        async def _fake_route(internal_event, payload, delivery_id=None):
            route_delivery_ids.append(delivery_id)
            return None

        def _fake_gate(internal_event, payload, loop):
            gate_log.append(internal_event)
            gate_loop_running.append(loop.is_running())
            completed.set()
            return True

        with patch("events.ingress.gate_trigger_leg", side_effect=_fake_gate), \
             patch("events.ingress.route_leg", side_effect=_fake_route), \
             patch("events.ingress.review_leg") as mock_review:
            resp = self._post_webhook(
                _check_suite_payload(),
                event_type="check_suite",
                delivery_id="check-suite-delivery-1",
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["routing"] == "async"
            assert completed.wait(5.0)

        assert gate_log == ["webhook.github.check_suite_completed"]
        assert gate_loop_running == [True]
        assert route_delivery_ids == ["check-suite-delivery-1"]
        mock_review.assert_not_called()

    def test_ready_for_review_routes_and_runs_gate_off_loop(self):
        gate_log: list[str] = []
        completed = Event()

        async def _fake_route(internal_event, payload, delivery_id=None):
            return None

        def _fake_gate(internal_event, payload, loop):
            gate_log.append(internal_event)
            assert loop.is_running()
            completed.set()
            return True

        with patch("events.ingress.gate_trigger_leg", side_effect=_fake_gate), \
             patch("events.ingress.route_leg", side_effect=_fake_route):
            response = self._post_webhook(
                _pr_ready_for_review_payload(),
                event_type="pull_request",
                delivery_id="pr-ready-delivery-1",
            )
            assert response.status_code == 200
            assert completed.wait(5.0)

        assert gate_log == ["webhook.github.pr_ready_for_review"]

    @pytest.mark.asyncio
    async def test_replay_ci_gate_receives_the_running_backend_loop(self):
        from events.connectors.github_webhook import _run_webhook_auxiliary_leg
        from events.reviewer_route import GATE_TRIGGER_EVENTS, REVIEWER_EVENTS

        replay_loop = asyncio.get_running_loop()
        gate_loops: list[asyncio.AbstractEventLoop] = []

        def gate_trigger_leg(internal_event, payload, loop):
            gate_loops.append(loop)
            return True

        await _run_webhook_auxiliary_leg(
            "webhook.github.check_suite_completed",
            _check_suite_payload(),
            review_leg=lambda *_: False,
            gate_trigger_leg=gate_trigger_leg,
            reviewer_events=REVIEWER_EVENTS,
            gate_trigger_events=GATE_TRIGGER_EVENTS,
        )

        assert gate_loops == [replay_loop]

    def test_issue_comment_on_pr_routes_as_pr_comment(self):
        route_log: list[str] = []
        completed = Event()

        async def _fake_route(internal_event, payload, delivery_id=None):
            route_log.append(internal_event)
            completed.set()
            return None

        with patch("events.ingress.route_leg", side_effect=_fake_route), \
             patch("events.ingress.review_leg") as mock_review:
            resp = self._post_webhook(
                _issue_comment_on_pr_payload(),
                event_type="issue_comment",
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
            assert completed.wait(5.0)
        assert route_log == ["webhook.github.pr_comment"]
        mock_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_drain_rejects_before_delivery_dedup_or_dispatch(self):
        from api.webhooks import github_webhook
        from infra import state

        state._drain = {
            "reason": "release",
            "started_at": 0,
        }
        try:
            with (
                patch("automation.self_restart.restart_holds_new_work", return_value=True),
                patch(
                    "infra.webhook_delivery_log.update_delivery_status"
                ) as status_update,
                patch("events.ingress.route_leg") as route_leg,
                patch("events.ingress.review_leg") as review_leg,
            ):
                response = await github_webhook(
                    _direct_webhook_request(_pr_opened_payload())
                )
        finally:
            state._drain = None

        assert response.status_code == 202
        response_payload = json.loads(response.body)
        assert response_payload["status"] == "held"
        assert "this delivery was accepted" in response_payload["message"]
        status_update.assert_not_called()
        route_leg.assert_not_called()
        review_leg.assert_not_called()

    @pytest.mark.asyncio
    async def test_pre_drain_webhook_owns_one_admission_across_all_legs(
        self,
    ):
        from api.webhooks import github_webhook
        from infra import drain, state
        from infra.send_service import SendRejected, send_thread_input

        route_started = Event()
        review_started = Event()
        release_route = Event()
        release_review = Event()
        route_finished = Event()
        review_finished = Event()
        observed_route_status: list[int] = []
        observed_review_status: list[int] = []

        async def _probe_inherited_send() -> int:
            try:
                await send_thread_input(
                    "thread-a",
                    {"id": "thread-a"},
                    "",
                    native_mode="event",
                )
            except SendRejected as exc:
                return exc.status_code
            raise AssertionError("empty send unexpectedly succeeded")

        async def _fake_route(internal_event, payload, delivery_id=None):
            route_started.set()
            await asyncio.to_thread(release_route.wait, 5.0)
            observed_route_status.append(await _probe_inherited_send())
            route_finished.set()
            return None

        def _fake_review(internal_event, payload):
            review_started.set()
            release_review.wait(5.0)
            observed_review_status.append(
                asyncio.run(_probe_inherited_send())
            )
            review_finished.set()
            return True

        state._drain = None
        try:
            with (
                patch(
                    "automation.self_restart.restart_holds_new_work",
                    side_effect=[False, True],
                ),
                patch(
                    "events.ingress.review_leg",
                    side_effect=_fake_review,
                ),
                patch(
                    "events.ingress.route_leg",
                    side_effect=_fake_route,
                ),
            ):
                response = await github_webhook(
                    _direct_webhook_request(_pr_opened_payload())
                )
                assert response["status"] == "ok"
                assert await asyncio.to_thread(route_started.wait, 5.0)
                assert await asyncio.to_thread(review_started.wait, 5.0)
                assert drain._active_work_counts()["turnAdmissions"] == 1

                state._drain = {
                    "reason": "release",
                    "started_at": 0,
                }
                release_route.set()
                assert await asyncio.to_thread(route_finished.wait, 5.0)
                assert observed_route_status == [400]
                assert drain._active_work_counts()["turnAdmissions"] == 1

                later = await github_webhook(
                    _direct_webhook_request(_pr_opened_payload())
                )
                assert later.status_code == 202
                later_payload = json.loads(later.body)
                assert later_payload["status"] == "held"

                release_review.set()
                assert await asyncio.to_thread(review_finished.wait, 5.0)
                for _attempt in range(500):
                    if drain._active_work_counts()["turnAdmissions"] == 0:
                        break
                    await asyncio.sleep(0.01)
                assert observed_review_status == [400]
                assert drain._active_work_counts()["turnAdmissions"] == 0
        finally:
            release_route.set()
            release_review.set()
            state._drain = None

    @pytest.mark.asyncio
    async def test_webhook_admission_releases_after_every_leg_settles_on_error(
        self,
    ):
        from api.webhooks import github_webhook
        from infra import drain, state

        route_started = Event()
        review_started = Event()
        release_review = Event()

        async def _failed_route(internal_event, payload, delivery_id=None):
            route_started.set()
            raise RuntimeError("route failed")

        def _failed_review(internal_event, payload):
            review_started.set()
            release_review.wait(5.0)
            raise RuntimeError("review failed")

        state._drain = None
        try:
            with (
                patch(
                    "events.ingress.review_leg",
                    side_effect=_failed_review,
                ),
                patch(
                    "events.ingress.route_leg",
                    side_effect=_failed_route,
                ),
            ):
                response = await github_webhook(
                    _direct_webhook_request(_pr_opened_payload())
                )
                assert response["status"] == "ok"
                assert await asyncio.to_thread(route_started.wait, 5.0)
                assert await asyncio.to_thread(review_started.wait, 5.0)
                assert drain._active_work_counts()["turnAdmissions"] == 1
                release_review.set()
                for _attempt in range(500):
                    if drain._active_work_counts()["turnAdmissions"] == 0:
                        break
                    await asyncio.sleep(0.01)
                assert drain._active_work_counts()["turnAdmissions"] == 0
        finally:
            release_review.set()
            state._drain = None

    def test_secret_not_configured_returns_401(self, monkeypatch):
        monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: None)
        payload = _pr_opened_payload()
        body = json.dumps(payload).encode()
        headers = {
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": _make_signature(body),
        }
        resp = self.client.post("/webhooks/github", content=body, headers=headers)
        assert resp.status_code == 401

    # --- ping / health-check event tests (#526) ---

    def _ping_payload(self) -> dict:
        return {"zen": "Design for failure.", "hook_id": 1, "hook": {"type": "Repository"}}

    def _post_ping(self, include_sig: bool = False, secret: str = WEBHOOK_SECRET) -> object:
        payload = self._ping_payload()
        body = json.dumps(payload).encode()
        headers = {
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        if include_sig:
            headers["X-Hub-Signature-256"] = _make_signature(body, secret)
        return self.client.post("/webhooks/github", content=body, headers=headers)

    def test_unsigned_ping_without_configured_secret_accepted(self, monkeypatch):
        """Unsigned ping (no secret yet configured) must return 200 ok."""
        monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: None)
        resp = self._post_ping(include_sig=False)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["event"] == "ping"

    def test_unsigned_ping_with_configured_secret_rejected(self):
        """Unsigned ping with a configured secret is anomalous — reject it."""
        resp = self._post_ping(include_sig=False)
        assert resp.status_code == 401

    def test_signed_ping_accepted(self):
        """Signed ping (secret already configured) must also be accepted."""
        resp = self._post_ping(include_sig=True)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_ping_with_wrong_signature_rejected(self):
        """A ping with a bad signature signals misconfiguration — reject it."""
        resp = self._post_ping(include_sig=True, secret="wrong-secret")
        assert resp.status_code == 401

    def test_unsigned_non_ping_still_rejected(self):
        """Non-ping events without a signature must still be rejected."""
        payload = _pr_opened_payload()
        body = json.dumps(payload).encode()
        headers = {
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()),
        }
        resp = self.client.post("/webhooks/github", content=body, headers=headers)
        assert resp.status_code == 401

    def test_invalid_sig_non_ping_rejected(self):
        """Non-ping events with an invalid signature must still be rejected."""
        resp = self._post_webhook(_pr_opened_payload(), secret="wrong-secret")
        assert resp.status_code == 401
