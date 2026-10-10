"""The first-run wake carries its instructions and setup facts inline.

Turn 1 of a new install reads them from the wake instead of loading a skill
and calling tools. The wake is filled in when the person picks the conductor's
model, only while no Turn has read it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

SECRET_ENOUGH = "long-enough-passphrase"
FACTS = {"your_model": {"display_name": "Sonnet", "local": False}, "setup": {"tasks": []}}


@pytest.fixture
def fresh_install(isolated_db, monkeypatch):
    """A new install whose wake facts are fixed and whose analytics go nowhere."""
    from infra import onboarding_events
    from infra.database import write_transaction

    monkeypatch.setattr("tools.mcp_import.detected_sources", lambda *a, **k: [])
    monkeypatch.setattr("onboarding.project_discovery.session_apps", lambda _roots: [])
    monkeypatch.setattr(onboarding_events, "emit_product_event", Mock(return_value=False))
    monkeypatch.setattr("infra.thread_execution.drive_thread", AsyncMock())
    monkeypatch.setattr("infra.state.broadcast_to_thread", Mock())
    with write_transaction(None, scope="test_fresh_install") as db:
        db.execute("DELETE FROM auth_tokens")
        db.execute("DELETE FROM users")
    return isolated_db


def _wake_row() -> dict:
    from infra.database import get_db

    db = get_db()
    try:
        [row] = db.execute("SELECT * FROM messages WHERE id LIKE 'first-run:%'").fetchall()
        return dict(row)
    finally:
        db.close()


async def _new_install_picks(model: str) -> str:
    from infra import accounts
    from infra.conductor import WORKSPACE_SCOPE, get_conductor
    from thread.model import switch_model

    accounts.create_user("newcomer", SECRET_ENOUGH)
    conductor_id = get_conductor(WORKSPACE_SCOPE)
    await switch_model(
        conductor_id, model, user_id="newcomer", actor_kind="user",
        actor_id="newcomer", reason="harness_model_switch",
    )
    return conductor_id


class TestRender:
    def test_instructions_are_the_served_reference_whole(self):
        from infra.system_package import system_resource_source
        from onboarding import first_run_wake

        text = first_run_wake.instructions()
        reference = system_resource_source(first_run_wake.FIRST_RUN_REFERENCE).read_text()
        assert text == reference.strip()

    def test_the_same_facts_render_the_same_bytes(self):
        from onboarding import first_run_wake

        reordered = dict(reversed(list(FACTS.items())))
        assert first_run_wake.render(FACTS, "steps") == first_run_wake.render(reordered, "steps")

    def test_the_wake_carries_summary_instructions_and_sorted_facts(self):
        import json

        from onboarding import first_run_wake

        wake = first_run_wake.render(FACTS, "Do the steps.")
        assert wake.startswith("[EVENT: ")
        assert first_run_wake.INLINE_SUMMARY in wake
        assert wake.index("Do the steps.") < wake.index("## Setup facts")
        assert wake.endswith(json.dumps(FACTS, sort_keys=True, ensure_ascii=False))


class TestFirstModelChoice:
    @pytest.mark.asyncio
    async def test_the_choice_fills_the_held_wake_for_the_chosen_model(self, fresh_install, monkeypatch):
        from onboarding import first_run_wake

        facts_for = AsyncMock(return_value=FACTS)
        monkeypatch.setattr(first_run_wake, "setup_facts", facts_for)

        await _new_install_picks("claude-code/sonnet")

        facts_for.assert_awaited_once_with("claude-code/sonnet")
        row = _wake_row()
        assert row["content"] == first_run_wake.render(FACTS, first_run_wake.instructions())
        assert (row["delivery_state"], row["target_model"]) == ("queued", "claude-code/sonnet")

    @pytest.mark.asyncio
    async def test_the_served_file_is_embedded_whole(self, fresh_install):
        from infra.system_package import system_resource_source
        from onboarding import first_run_wake

        await _new_install_picks("claude-code/sonnet")

        reference = system_resource_source(first_run_wake.FIRST_RUN_REFERENCE).read_text().strip()
        assert reference in _wake_row()["content"]

    @pytest.mark.asyncio
    async def test_an_escaping_error_fails_the_pick_instead_of_changing_the_wake(
        self, fresh_install, monkeypatch,
    ):
        from onboarding import first_run_wake

        monkeypatch.setattr(first_run_wake, "instructions", Mock(side_effect=OSError("package unreadable")))

        with pytest.raises(OSError):
            await _new_install_picks("claude-code/sonnet")

        assert "Summary:" in _wake_row()["content"] and "Setup facts" not in _wake_row()["content"]

    @pytest.mark.asyncio
    async def test_the_real_facts_render(self, fresh_install):
        from onboarding import first_run_wake

        await _new_install_picks("claude-code/sonnet")

        content = _wake_row()["content"]
        assert first_run_wake.INLINE_SUMMARY in content
        assert '"your_model": {' in content and '"setup": {' in content

    @pytest.mark.asyncio
    async def test_a_failed_fact_renders_unavailable_with_its_read(self, fresh_install, monkeypatch):
        from onboarding import first_run_wake

        monkeypatch.setattr("api.system_reviewer.host_access_reviewer_status", Mock(side_effect=OSError("no db")))

        facts = await first_run_wake.setup_facts("claude-code/sonnet")

        assert set(facts) == set(first_run_wake.UNAVAILABLE_READS)
        assert facts["host_access_reviewer"] == {
            "unavailable": first_run_wake.UNAVAILABLE_READS["host_access_reviewer"],
        }
        assert "unavailable" not in facts["your_model"]

    @pytest.mark.asyncio
    async def test_an_unfinished_fact_is_unavailable_after_the_total_bound(self, fresh_install, monkeypatch):
        import asyncio

        from onboarding import first_run_wake

        async def never(*_args, **_kwargs):
            await asyncio.sleep(60)

        monkeypatch.setattr(first_run_wake, "FACTS_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr("onboarding.setup_decisions.setup_plan", never)

        facts = await asyncio.wait_for(first_run_wake.setup_facts("claude-code/sonnet"), 5)

        assert facts["setup"] == {"unavailable": first_run_wake.UNAVAILABLE_READS["setup"]}
        assert "unavailable" not in facts["your_model"]

    @pytest.mark.asyncio
    async def test_no_search_runs_while_the_facts_are_read(self, fresh_install, monkeypatch):
        from onboarding import first_run_wake

        probe = AsyncMock(return_value=True)
        monkeypatch.setattr("onboarding.setup_decisions.web_search_works", probe)

        facts = await first_run_wake.setup_facts("claude-code/sonnet")

        probe.assert_not_awaited()
        assert facts["web_search_key"] is False

    def test_every_unavailable_read_is_named_in_the_reference(self):
        from infra.system_package import system_resource_source
        from onboarding import first_run_wake

        reference = system_resource_source(first_run_wake.FIRST_RUN_REFERENCE).read_text()
        assert all(f"`{name}`" in reference for name in first_run_wake.UNAVAILABLE_READS)


class TestOnlyAnUnreadWakeChanges:
    def _queue(self, delivery_state: str, delivery_error: str | None) -> str:
        from infra import accounts
        from infra.conductor import WORKSPACE_SCOPE, get_conductor
        from infra.database import write_transaction

        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = get_conductor(WORKSPACE_SCOPE)
        with write_transaction(None, scope="test_wake_state") as db:
            db.execute(
                "UPDATE messages SET delivery_state=?, delivery_error=? WHERE thread_id=?",
                (delivery_state, delivery_error, conductor_id),
            )
        return conductor_id

    @pytest.mark.parametrize(("state", "error"), [
        ("queued", "no_model_selected"), ("queued", None), ("delivered", None),
    ])
    def test_fill_touches_only_the_held_wake(self, fresh_install, state, error):
        from infra.database import write_transaction
        from infra.onboarding_events import fill_held_first_run_wake, has_held_first_run_wake

        conductor_id = self._queue(state, error)
        before = _wake_row()["content"]
        with write_transaction(None, scope="test_fill") as db:
            held = has_held_first_run_wake(conductor_id, db=db)
            filled = fill_held_first_run_wake(conductor_id, "inline", db=db)

        held_now = (state, error) == ("queued", "no_model_selected")
        assert (held, filled) == (held_now, held_now)
        assert _wake_row()["content"] == ("inline" if held_now else before)
