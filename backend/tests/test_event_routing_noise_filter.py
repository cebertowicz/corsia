
from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

from infra import corsia_agent

from unittest.mock import patch

import pytest

from events.github_mapper import map_github_event
from tests.event_routing_helpers import check_suite_payload as _check_suite_payload
from tests.event_routing_helpers import mk_session as _mk_session
from tests.event_routing_helpers import pr_payload as _pr_payload
from tests.event_routing_helpers import register_pr as _register_pr
from tests.event_routing_helpers import route as _route


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")



class TestCheckSuiteFilter:

    def test_check_run_events_dropped_at_dispatcher(self):
        for action in ["completed", "created", "rerequested", "requested_action"]:
            assert map_github_event("check_run", {"action": action}) is None, (
                f"check_run.{action} must be dropped at dispatcher, not routed"
            )

    def test_check_suite_completed_maps_to_internal_event(self):
        assert (
            map_github_event("check_suite", {"action": "completed"})
            == "webhook.github.check_suite_completed"
        )

    def test_check_suite_other_actions_return_none(self):
        for action in ["rerequested", "requested"]:
            assert map_github_event("check_suite", {"action": action}) is None

    @pytest.mark.asyncio
    async def test_check_suite_completed_routes_to_registered_owner(self, isolated_db):
        worker = _mk_session("wk-csf-1")
        _register_pr("o/r", 200, worker)

        payload = _check_suite_payload("o/r", 200, conclusion="success")

        with patch("events.router.delivery.deliver_to_thread",
                   return_value=True) as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.ACCEPTED
        assert res.thread_id == worker
        mock_deliver.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_suite_failure_unregistered_main_dead_letters(self, isolated_db):
        conductor_thread = _mk_session("cond-csf-main")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _check_suite_payload("o/r", 201, conclusion="failure",
                                       base_ref="main")

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.DEAD_LETTERED
        assert res.thread_id is None
        assert res.reason == "no_owner"
        mock_deliver.assert_not_called()
        assert conductor_thread

    @pytest.mark.asyncio
    async def test_check_suite_success_unregistered_non_main_parked(self, isolated_db):
        conductor_thread = _mk_session("cond-csf-dev")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _check_suite_payload("o/r", 202, conclusion="success",
                                       base_ref="dev")

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.PARKED
        assert res.thread_id is None
        assert res.reason == "check_suite_noise_suppressed"
        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_suite_cancelled_dropped(self, isolated_db):
        conductor_thread = _mk_session("cond-csf-cancel")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _check_suite_payload("o/r", 203, conclusion="cancelled")

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.PARKED
        assert res.reason == "check_suite_cancelled_dropped"
        mock_deliver.assert_not_called()



class TestConductorNoiseFilter:

    @pytest.mark.asyncio
    async def test_check_suite_completed_with_no_owner_is_parked(self, isolated_db):
        conductor_thread = _mk_session("cond-noise-1")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route(
                "webhook.github.check_suite_completed",
                _check_suite_payload("o/r", 55),
            )

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.PARKED
        assert res.thread_id is None
        assert res.reason == "check_suite_noise_suppressed"
        mock_deliver.assert_not_called()

        from events import dead_letter
        parked = dead_letter.list_parked()
        assert any(
            r["internal_event"] == "webhook.github.check_suite_completed"
            and r["reason"] == "check_suite_noise_parked"
            for r in parked
        )

    @pytest.mark.asyncio
    async def test_check_suite_failure_with_no_owner_is_parked(self, isolated_db):
        conductor_thread = _mk_session("cond-fail-1")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _check_suite_payload("o/r", 61, conclusion="failure")

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.PARKED
        assert res.reason == "check_suite_noise_suppressed"
        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("conclusion", ["timed_out", "action_required"])
    async def test_check_suite_other_conclusions_also_parked_without_owner(
        self, isolated_db, conclusion
    ):
        conductor_thread = _mk_session(f"cond-{conclusion}")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _check_suite_payload("o/r", 62, conclusion=conclusion)

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.PARKED
        assert res.reason == "check_suite_noise_suppressed"
        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_suite_archived_owner_dead_letters(self, isolated_db):
        worker = _mk_session("wk-arch-cs")
        _register_pr("o/r", 60, worker)
        conductor_thread = _mk_session("cond-noise-arch")
        from infra import conductor as cond_mod
        corsia_agent.set_corsia_agent(conductor_thread)

        from infra.database import get_db
        db = get_db()
        db.execute(
            "UPDATE threads SET lifecycle_state='archived', "
            "archived_reason='user' WHERE id=?",
            (worker,),
        )
        db.commit()
        db.close()

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route(
                "webhook.github.check_suite_completed",
                _check_suite_payload("o/r", 60),
            )

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.DEAD_LETTERED
        assert res.reason == "owner_inactive"
        mock_deliver.assert_not_called()
        assert conductor_thread != worker

    @pytest.mark.asyncio
    async def test_check_suite_with_registered_owner_is_delivered_to_worker(
        self, isolated_db
    ):
        worker = _mk_session("wk-noise")
        _register_pr("o/r", 57, worker)
        conductor_thread = _mk_session("cond-noise-3")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        with patch("events.router.delivery.deliver_to_thread",
                   return_value=True) as mock_deliver:
            res = await _route(
                "webhook.github.check_suite_completed",
                _check_suite_payload("o/r", 57),
            )

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.ACCEPTED
        assert res.thread_id == worker
        mock_deliver.assert_called_once()
        assert mock_deliver.call_args.args[0] == worker

    @pytest.mark.asyncio
    async def test_pr_opened_without_owner_dead_letters_even_with_conductor(
        self, isolated_db
    ):
        conductor_thread = _mk_session("cond-noise-4")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route(
                "webhook.github.pr_opened",
                _pr_payload("o/r", 58),
            )

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.DEAD_LETTERED
        assert res.reason == "no_owner"
        mock_deliver.assert_not_called()
        assert conductor_thread

    @pytest.mark.asyncio
    async def test_check_suite_no_owner_still_parks_noise(
        self, isolated_db
    ):
        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route(
                "webhook.github.check_suite_completed",
                _check_suite_payload("o/r", 59),
            )

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.PARKED
        assert res.reason == "check_suite_noise_suppressed"
        mock_deliver.assert_not_called()
