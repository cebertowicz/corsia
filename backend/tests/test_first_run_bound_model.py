"""The app handoff isolates an unavailable model fact from the other setup facts."""

import threading
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_app_bound_model_resolution_runs_off_loop_and_failure_keeps_other_facts(monkeypatch):
    from onboarding import first_run_wake as wake

    loop_thread = threading.get_ident()
    observed = []

    def broken(_model):
        observed.append(threading.get_ident())
        raise ValueError("unreadable model")

    monkeypatch.setattr("llm.resolution.resolve_execution_target", broken)
    monkeypatch.setattr(wake, "_model_sources", lambda: {
        "sources": [], "github": {"state": "not_installed"}, "github_agent": {"state": "not_installed"},
    })
    monkeypatch.setattr(wake, "_app_agents_found", lambda: [])
    monkeypatch.setattr(wake, "_app_local_download", lambda: {"state": "stopped"})
    monkeypatch.setattr(wake, "_app_setup_handoff", lambda: None)
    monkeypatch.setattr(wake, "_app_user_profile", lambda: {})
    monkeypatch.setattr(wake, "_app_setup", AsyncMock(return_value={"setup_decisions": []}))
    monkeypatch.setattr("onboarding.setup_decisions.web_search_works", AsyncMock(return_value=False))
    facts = await wake.setup_facts("bad-model", "app")
    assert observed and all(thread != loop_thread for thread in observed)
    assert facts["bound_model"] == {"unavailable": wake.UNAVAILABLE_READS["bound_model"]}
    assert facts["local_download"] == {"state": "stopped"}
    assert set(facts) == wake.APP_FACT_KEYS
