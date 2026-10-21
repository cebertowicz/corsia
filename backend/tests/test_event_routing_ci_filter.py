
from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

from infra import corsia_agent

from unittest.mock import patch

import pytest

from tests.event_routing_helpers import check_suite_payload as _cs_payload
from tests.event_routing_helpers import mk_session as _mk_session
from tests.event_routing_helpers import register_pr as _register_pr
from tests.event_routing_helpers import route as _route


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")



class TestCancelledDrop:

    @pytest.mark.asyncio
    async def test_check_suite_cancelled_dropped_with_no_owner(self, isolated_db):
        conductor_thread = _mk_session("cond-cancel-1")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route(
                "webhook.github.check_suite_completed",
                _cs_payload("o/r", 70, conclusion="cancelled"),
            )

        from events.router import RouteOutcome
        assert (res.outcome, res.reason) == (RouteOutcome.SUPPRESSED, "rule_set:github@1:cancelled")
        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_suite_cancelled_dropped_even_for_registered_owner(
        self, isolated_db
    ):
        worker = _mk_session("wk-cancel")
        _register_pr("o/r", 71, worker)

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route(
                "webhook.github.check_suite_completed",
                _cs_payload("o/r", 71, conclusion="cancelled"),
            )

        from events.router import RouteOutcome
        assert (res.outcome, res.reason) == (RouteOutcome.SUPPRESSED, "rule_set:github@1:cancelled")
        mock_deliver.assert_not_called()



class TestWorkerPrCIIsolation:

    @pytest.mark.asyncio
    async def test_worker_pr_failure_delivered_to_worker_not_conductor(
        self, isolated_db
    ):
        worker = _mk_session("wk-fail-b")
        _register_pr("o/r", 80, worker)
        conductor_thread = _mk_session("cond-fail-b")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _cs_payload("o/r", 80, conclusion="failure")

        with patch("events.router.delivery.deliver_to_thread",
                   return_value=True) as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.ACCEPTED
        assert res.thread_id == worker
        mock_deliver.assert_called_once()
        assert mock_deliver.call_args.args[0] == worker
        assert mock_deliver.call_args.args[0] != conductor_thread

    @pytest.mark.asyncio
    async def test_conductor_owned_pr_failure_reaches_conductor(self, isolated_db):
        conductor_thread = _mk_session("cond-own")
        _register_pr("o/r", 81, conductor_thread)
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _cs_payload("o/r", 81, conclusion="failure")

        with patch("events.router.delivery.deliver_to_thread",
                   return_value=True) as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.ACCEPTED
        assert res.thread_id == conductor_thread
        mock_deliver.assert_called_once()



class TestNoOwnerFailure:

    @pytest.mark.asyncio
    async def test_unregistered_main_pr_failure_dead_letters(self, isolated_db):
        conductor_thread = _mk_session("cond-pf-1")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _cs_payload("o/r", 110, conclusion="failure", base_ref="main")

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.DEAD_LETTERED
        assert res.reason == "no_owner"
        assert res.thread_id is None
        mock_deliver.assert_not_called()
        assert conductor_thread

    @pytest.mark.asyncio
    async def test_unregistered_non_main_pr_failure_still_filtered(self, isolated_db):
        conductor_thread = _mk_session("cond-pf-2")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _cs_payload("o/r", 111, conclusion="failure", base_ref="dev")

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.PARKED
        assert res.reason == "check_suite_noise_suppressed"
        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_unregistered_main_pr_success_still_filtered(self, isolated_db):
        conductor_thread = _mk_session("cond-pf-3")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _cs_payload("o/r", 112, conclusion="success", base_ref="main")

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.PARKED
        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("conclusion", ["timed_out", "action_required"])
    async def test_unregistered_main_pr_other_terminal_failures_dead_letter(
        self, isolated_db, conclusion
    ):
        conductor_thread = _mk_session(f"cond-pf-{conclusion}")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _cs_payload("o/r", 113, conclusion=conclusion, base_ref="main")

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.DEAD_LETTERED
        assert res.reason == "no_owner"
        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_unregistered_no_base_ref_failure_still_filtered(self, isolated_db):
        conductor_thread = _mk_session("cond-pf-4")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _cs_payload("o/r", 114, conclusion="failure")

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.PARKED
        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_registered_conductor_pr_failure_still_delivered(self, isolated_db):
        conductor_thread = _mk_session("cond-pf-5")
        _register_pr("o/r", 115, conductor_thread)
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _cs_payload("o/r", 115, conclusion="failure", base_ref="main")

        with patch("events.router.delivery.deliver_to_thread",
                   return_value=True) as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.ACCEPTED
        assert res.thread_id == conductor_thread
        mock_deliver.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancelled_main_pr_still_dropped_before_failure(self, isolated_db):
        conductor_thread = _mk_session("cond-pf-7")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        payload = _cs_payload("o/r", 117, conclusion="cancelled", base_ref="main")

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.check_suite_completed", payload)

        from events.router import RouteOutcome
        assert (res.outcome, res.reason) == (RouteOutcome.SUPPRESSED, "rule_set:github@1:cancelled")
        mock_deliver.assert_not_called()
