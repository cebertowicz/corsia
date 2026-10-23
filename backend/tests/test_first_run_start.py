"""The app's first generated turn works one server-seeded setup step."""
from unittest.mock import AsyncMock

import pytest

from onboarding import first_run_wake


def facts(**updates):
    return {
        "sources_missing": {}, "pays_for": [], "agents_found": [],
        "setup": {"setup_decisions": []}, "connected_app_count": 0,
        "email_calendar_ready": False, "web_search_ready": False,
        **updates,
    }


def test_first_step_explains_unfinished_signin_and_points_to_actual_card_action():
    text = first_run_wake.first_app_step(facts(
        sources_missing={"codex": "needs_auth"}, pays_for=["codex"],
    ))
    assert "1. **Add your second subscription" in text
    assert "Setup's sign-in to [Claude Code | Codex] didn't finish." in text
    assert "[Settings › Providers](/settings/providers), press Sign in on its card" in text
    assert "POST /api/vendor-accounts/{id}/signin" not in text
    assert "2. **Bring over" not in text


def test_import_with_consent_is_first_but_missing_consent_skips_it():
    base = facts(agents_found=["hermes"])
    assert "3. **Where your work lives" in first_run_wake.first_app_step(base)
    base["setup"]["setup_decisions"] = [{"title": "Bring over what your other tools know"}]
    text = first_run_wake.first_app_step(base)
    assert "2. **Bring over what your other tools know" in text
    assert "3. **Where your work lives" not in text


def test_connected_apps_start_web_search_with_the_question_inline():
    text = first_run_wake.first_app_step(facts(connected_app_count=2))
    assert "4. **Web search" in text
    assert "Do you already have a Brave Search API account?" in text
    assert "## 2. Open the right page" not in text


def test_search_already_ready_asks_lookup_consent_as_first_question():
    text = first_run_wake.first_app_step(facts(connected_app_count=2, web_search_ready=True))
    assert "5. **About you" in text
    assert "Your name and anything I search for are sent to web search engines. Want me to?" in text
    assert "## B. Search" not in text


@pytest.mark.asyncio
async def test_completed_setup_silently_skips_import_when_no_eligible_tool_remains(monkeypatch):
    from onboarding import app_first_run, setup_decisions

    snapshot = facts(agents_found=["hermes"], harness_ready=[])
    monkeypatch.setattr(app_first_run, "read_cached_app_facts", lambda _thread: snapshot)
    monkeypatch.setattr(first_run_wake, "_bound_model", lambda model: {"model": model, "source": "omlx"})
    monkeypatch.setattr(setup_decisions, "import_card", lambda model, ready: None)

    wake = await first_run_wake.render_for("test-local", "app", thread_id="test-conductor")

    assert "3. **Where your work lives" in wake
    assert "2. **Bring over what your other tools know" not in wake
    assert '"setup_decisions": []' in wake
    assert "skip that step silently" in wake


@pytest.mark.asyncio
async def test_completed_setup_reuses_snapshot_but_builds_provider_specific_import_consent(monkeypatch):
    from onboarding import app_first_run, setup_decisions

    snapshot = facts(agents_found=["hermes"], harness_ready=[])
    monkeypatch.setattr(app_first_run, "read_cached_app_facts", lambda _thread: snapshot)
    read = AsyncMock(side_effect=AssertionError("setup must not run again after the pick"))
    monkeypatch.setattr(app_first_run, "read_app_facts", read)
    monkeypatch.setattr(first_run_wake, "_bound_model", lambda model: {"model": model, "source": "omlx"})
    monkeypatch.setattr(setup_decisions, "import_card", lambda model, ready: setup_decisions.ImportCard(("Hermes Agent",), (), ""))

    wake = await first_run_wake.render_for("test-local", "app", thread_id="test-conductor")

    read.assert_not_awaited()
    assert "2. **Bring over what your other tools know" in wake
    assert "Bring over what your other tools know" in wake
    assert snapshot["setup"]["setup_decisions"] == []  # keep committed snapshot immutable
    assert "Never write the opening or create the setup plan" in wake
