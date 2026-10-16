"""The first-chat oMLX decision while CORSIA manages the person's oMLX, and raising it again later."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from onboarding import omlx_settings_wake as wake
from onboarding import setup_decisions as setup
from plans import decision_fields

MODEL = "Qwen3.8-27B-oQ4e-mtp"
GLOBALS = (
    {"setting": "max_concurrent_requests", "current": 4, "recommended": 1},
    {"setting": "prefill_priority", "current": "decode", "recommended": "context"},
)
MODEL_ROWS = tuple({**row, "model": MODEL} for row in (
    {"setting": "max_context_window", "current": None, "recommended": 65536},
    {"setting": "max_tokens", "current": 4096, "recommended": 16384},
    {"setting": "mtp_enabled", "current": None, "recommended": True},
    {"setting": "turboquant_kv_bits", "current": None, "recommended": 8},
    {"setting": "turboquant_kv_enabled", "current": False, "recommended": True},
    {"setting": "turboquant_skip_last", "current": None, "recommended": True},
))
CONTEXT = (
    "You let CORSIA manage your oMLX. I'd change these settings, each shown as current → new: "
    "oMLX-wide, Max Concurrent Requests: 4 → 1, Prefill Priority: decode → context; "
    f"for {MODEL}, Ctx Window: oMLX default → 65536, Max Tokens: 4096 → 16384, "
    "Native MTP: oMLX default → on, TurboQuant bits per channel: oMLX default → 8, "
    "TurboQuant KV Cache: off → on, TurboQuant skip last layer: oMLX default → on. "
    "They change for every app that uses this oMLX. Your key, sign-in, network settings, port, "
    "folders, default model and other models stay as they are."
)


def _managed(rows=GLOBALS + MODEL_ROWS) -> setup.Facts:
    return setup.Facts(rows, False, True, (), "", omlx_managed=True)


def test_the_managed_decision_lists_every_setting_as_current_to_new():
    decision = setup.omlx_decision(_managed())

    assert decision == {
        "task_title": "Configuration checks",
        "title": "Change your oMLX's settings for CORSIA?",
        "context": CONTEXT,
        "tradeoff": ("Other apps that use your oMLX get these settings too. Stopping management in "
                     "Settings puts every value back."),
        "recommendation": "A",
        "rationale": "They match how CORSIA sets up an oMLX it installs.",
        "options": [
            {"id": "A", "label": "Change them", "detail": "I'll apply them now and tell you if oMLX needs a restart."},
            {"id": "B", "label": "Leave them as they are", "detail": "CORSIA still works with your current settings."},
        ],
        "other_enabled": True,
    }
    decision_fields.content({key: value for key, value in decision.items() if key != "task_title"})


def test_before_the_model_is_ready_only_the_oMLX_wide_settings_are_listed():
    context = setup.omlx_decision(_managed(GLOBALS))["context"]

    assert "Prefill Priority: decode → context." in context
    assert MODEL not in context


def test_nothing_to_change_raises_no_decision_and_unmanaged_keeps_the_one_row_decision():
    assert setup.omlx_decision(_managed(())) is None
    unmanaged = setup.omlx_decision(setup.Facts(GLOBALS, False))
    assert unmanaged["title"] == "Change one oMLX setting for CORSIA?"


def test_read_facts_takes_management_from_the_source_row(monkeypatch):
    monkeypatch.setattr("onboarding.project_discovery.session_apps", lambda _roots: [])
    monkeypatch.setattr(setup, "_saved_email", lambda: "")
    monkeypatch.setattr("api.provider_sources.list_sources", lambda: [{
        "source": "omlx", "omlx_settings": {"differences": list(GLOBALS), "fingerprint": "f"},
        "omlx_management": {"state": "managed", "cannot_check": None, "restores": [], "forgets_key": True},
    }])

    facts = asyncio.run(setup.read_facts(probe_web_search=False))

    assert facts.omlx_managed is True and facts.omlx_differences == GLOBALS


@pytest.fixture
def delivered(monkeypatch):
    sent: list[tuple] = []

    async def deliver(thread_id, event, summary, details=None, **kwargs):
        sent.append((thread_id, event, summary, kwargs))
        return True

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("events.delivery.deliver_to_thread", deliver)
    monkeypatch.setattr("infra.db_async.run_in_db", inline)
    monkeypatch.setattr("infra.corsia_agent.get_corsia_agent", lambda: "conductor")
    monkeypatch.setattr(wake, "_delivered_first_run", lambda _conductor: True)
    monkeypatch.setattr(wake, "managed_decision", AsyncMock(return_value=setup.omlx_decision(_managed())))
    return sent


def test_the_wake_carries_the_decision_and_how_to_apply_it(delivered):
    assert asyncio.run(wake.rearm()) is True

    [(thread_id, event, summary, kwargs)] = delivered
    assert (thread_id, event) == ("conductor", "onboarding.omlx_settings")
    assert "references/omlx-setting.md" in summary
    assert kwargs["chip"] == "Your local model is ready in your oMLX."
    assert json.loads(kwargs["notice"].split("\n\n", 1)[1]) == setup.omlx_decision(_managed())
    assert kwargs["delivery_id"].startswith("omlx-settings:conductor:")


def test_the_same_decision_gets_the_same_delivery_id(delivered):
    asyncio.run(wake.rearm())
    asyncio.run(wake.rearm())

    assert delivered[0][3]["delivery_id"] == delivered[1][3]["delivery_id"]


@pytest.mark.parametrize("change", ["no_conductor", "first_run_waiting", "nothing_to_ask"])
def test_no_wake_without_a_conductor_a_delivered_first_run_or_a_decision(delivered, monkeypatch, change):
    if change == "no_conductor":
        monkeypatch.setattr("infra.corsia_agent.get_corsia_agent", lambda: None)
    elif change == "first_run_waiting":
        monkeypatch.setattr(wake, "_delivered_first_run", lambda _conductor: False)
    else:
        monkeypatch.setattr(wake, "managed_decision", AsyncMock(return_value=None))

    assert asyncio.run(wake.rearm()) is False
    assert delivered == []


def test_a_failing_wake_never_raises(delivered, monkeypatch):
    monkeypatch.setattr(wake, "managed_decision", AsyncMock(side_effect=RuntimeError("down")))

    assert asyncio.run(wake.rearm()) is False


def test_only_a_first_run_wake_a_model_has_taken_counts_as_delivered(isolated_db, monkeypatch):
    """Against a real database: none, then held for the first model choice, then taken."""
    from infra import accounts
    from infra.corsia_agent import get_corsia_agent
    from infra.database import write_transaction
    from infra.onboarding_events import first_run_message_id

    monkeypatch.setattr("tools.mcp_import.detected_sources", lambda *a, **k: [])
    monkeypatch.setattr("onboarding.project_discovery.session_apps", lambda _roots: [])
    with write_transaction(None, scope="test_own_omlx_fresh") as db:
        db.execute("DELETE FROM auth_tokens")
        db.execute("DELETE FROM users")
    accounts.create_user("newcomer", "long-enough-passphrase")
    conductor_id = get_corsia_agent()

    assert wake._delivered_first_run("no-such-thread") is False
    assert wake._delivered_first_run(conductor_id) is False
    with write_transaction(None, scope="test_own_omlx_taken") as db:
        db.execute("UPDATE messages SET delivery_error=NULL WHERE id=?", (first_run_message_id(conductor_id),))
    assert wake._delivered_first_run(conductor_id) is True
