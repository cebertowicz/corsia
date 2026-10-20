"""The first-run wake carries its instructions and setup facts inline.

Turn 1 of a new install reads them from the wake instead of loading a skill
and calling tools. The wake is filled in when the person picks the conductor's
model, only while no Turn has read it.
"""

from __future__ import annotations

import json
from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

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
    from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator
    from thread.model import switch_model

    accounts.create_user("newcomer", SECRET_ENOUGH)
    conductor_id = get_corsia_agent()
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

        facts_for.assert_awaited_once_with("claude-code/sonnet", "script")
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

        assert set(facts) == {"first_run_path", "your_model", "model_sources", "mcp_sources", "setup", "host_access_reviewer", "web_search_key"}
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
        assert all(f"`{name}`" in reference for name in ("your_model", "model_sources", "mcp_sources", "setup", "host_access_reviewer", "web_search_key"))


class TestOnlyAnUnreadWakeChanges:
    def _queue(self, delivery_state: str, delivery_error: str | None) -> str:
        from infra import accounts
        from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator
        from infra.database import write_transaction

        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = get_corsia_agent()
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
        from infra.onboarding_events import fill_held_first_run_wake, held_first_run_path

        conductor_id = self._queue(state, error)
        before = _wake_row()["content"]
        with write_transaction(None, scope="test_fill") as db:
            held = held_first_run_path(conductor_id, db=db)
            filled = fill_held_first_run_wake(conductor_id, "inline", db=db)

        held_now = (state, error) == ("queued", "no_model_selected")
        assert (held, filled) == (("script" if held_now else None), held_now)
        assert _wake_row()["content"] == ("inline" if held_now else before)


class TestAppPath:
    def test_app_reference_predicate_handles_a_missing_and_present_resource(self, monkeypatch, tmp_path):
        from infra import system_package
        from onboarding import first_run_wake

        monkeypatch.setattr(system_package, "system_resource_source", lambda _path: tmp_path / "missing.md")
        assert first_run_wake.app_reference_served() is False
        present = tmp_path / "present.md"
        present.write_text("app path", encoding="utf-8")
        monkeypatch.setattr(system_package, "system_resource_source", lambda _path: present)
        assert first_run_wake.app_reference_served() is True

    @pytest.mark.asyncio
    async def test_served_app_path_uses_only_app_facts_and_both_references(self, monkeypatch):
        from onboarding import first_run_wake

        app_facts = {
            "first_run_path": "app", "harness_ready": ["codex"], "sources_missing": {},
            "agents_found": ["hermes"], "local_download": {"state": "none"}, "gmail_eligible": True,
            "web_search_ready": False, "github_ready": False, "own_omlx_source": False,
            "setup": {"setup_decisions": []},
            "user_profile": {"name": "Pat", "work": None, "place": None, "check_first": None, "answer_style": None},
        }
        monkeypatch.setattr(first_run_wake, "app_reference_served", lambda: True)
        monkeypatch.setattr(first_run_wake, "setup_facts", AsyncMock(return_value=app_facts))
        monkeypatch.setattr(first_run_wake, "instructions", lambda path="script": f"{path} instructions")

        wake = await first_run_wake.render_for("codex/gpt-5.6-terra", "app")

        assert first_run_wake.APP_INLINE_SUMMARY in wake
        assert "app instructions" in wake
        assert set(app_facts) == first_run_wake.APP_FACT_KEYS
        assert not any(key in wake for key in ("mcp_sources", "your_model", "model_sources", "host_access_reviewer"))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("source_states", "expected_missing"), [
        (("ready", "ready"), {}),
        (("not_installed", "ready"), {"claude_code": "not_installed"}),
        (("ready", "needs_auth"), {"codex": "needs_auth"}),
    ])
    @pytest.mark.parametrize(("status", "expected_download"), [
        ({"state": "downloading", "stage": "Downloading 50%", "result": None}, {"state": "downloading", "progress": "Downloading 50%"}),
        ({"state": "succeeded", "stage": None, "result": {"ok": True}}, {"state": "done"}),
        ({"state": "failed", "stage": None, "result": {"ok": False, "message": "Free disk space"}}, {"state": "failed", "message": "Free disk space"}),
        ({"state": "idle", "stage": None, "result": None}, {"state": "none"}),
    ])
    async def test_app_setup_facts_carry_every_f8_source_and_download_state(
        self, monkeypatch, source_states, expected_missing, status, expected_download,
    ):
        from onboarding import first_run_wake

        monkeypatch.setattr(first_run_wake, "_model_sources", lambda: {
            "sources": [
                {"source": "claude-code", "state": source_states[0]},
                {"source": "codex", "state": source_states[1]},
            ],
            "github": {"state": "not_installed"}, "github_agent": {"state": "not_installed"},
        })
        monkeypatch.setattr("llm.local_setup.setup_status", lambda: status)
        monkeypatch.setattr(first_run_wake, "_own_omlx_source", lambda: False)
        monkeypatch.setattr(first_run_wake, "_app_user_profile", lambda: {})
        monkeypatch.setattr(first_run_wake, "_app_setup", AsyncMock(return_value={"setup_decisions": []}))
        monkeypatch.setattr("onboarding.setup_decisions.web_search_works", AsyncMock(return_value=False))
        monkeypatch.setattr(first_run_wake, "_app_agents_found", lambda: ["openclaw"])

        facts = await first_run_wake.setup_facts("codex/gpt-5.6-terra", "app")

        assert facts["sources_missing"] == expected_missing
        assert facts["local_download"] == expected_download
        assert facts["agents_found"] == ["openclaw"]
        assert set(facts) == first_run_wake.APP_FACT_KEYS

    @pytest.mark.parametrize("present, expected", [
        ((), []),
        (("openclaw",), ["openclaw"]),
        (("openclaw", "hermes"), ["openclaw", "hermes"]),
    ])
    def test_agents_found_checks_presence_without_opening_settings(self, monkeypatch, tmp_path, present, expected):
        import builtins
        import io
        from pathlib import Path

        from onboarding import first_run_wake

        for name in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR", "OPENCLAW_HOME", "HERMES_HOME"):
            monkeypatch.delenv(name, raising=False)
        configs = {"openclaw": tmp_path / ".openclaw" / "openclaw.json", "hermes": tmp_path / ".hermes" / "config.yaml"}
        for source in present:
            configs[source].parent.mkdir()
            configs[source].write_text("mcp_servers: {}\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        opened: list[str] = []
        for target, name in ((builtins, "open"), (io, "open"), (Path, "open"), (Path, "read_text"), (Path, "read_bytes")):
            original = getattr(target, name)
            monkeypatch.setattr(target, name, lambda *a, _original=original, **k: opened.append(str(a[0])) or _original(*a, **k))

        found = first_run_wake._app_agents_found()

        assert found == expected
        assert not [path for path in opened if str(tmp_path) in path]

    def test_f8_instructed_routes_remain_open_in_conductor_policy(self):
        from tools.conductor_api_policy import off_limits

        for method, path in (
            ("GET", "/api/providers/local/setup"),
            ("POST", "/api/providers/cli/codex/install"),
            ("GET", "/api/providers/cli/codex/install"),
            ("GET", "/api/vendor-accounts"),
            ("POST", "/api/vendor-accounts/account-id/signin/check"),
        ):
            assert not off_limits(path, method), (method, path)

    @pytest.mark.asyncio
    async def test_unserved_app_path_falls_back_to_script_facts_and_warns_once(self, monkeypatch, caplog):
        from onboarding import first_run_wake

        script_facts = {"first_run_path": "app", **FACTS}
        monkeypatch.setattr(first_run_wake, "app_reference_served", lambda: False)
        monkeypatch.setattr(first_run_wake, "setup_facts", AsyncMock(return_value=script_facts))
        monkeypatch.setattr(first_run_wake, "instructions", lambda path="script": "script instructions")

        wake = await first_run_wake.render_for("codex/gpt-5.6-terra", "app")

        assert "script instructions" in wake and "first_run-app.md" not in wake
        assert script_facts["first_run_path"] == "app"
        warnings = [record for record in caplog.records if record.name == first_run_wake.__name__]
        assert len(warnings) == 1 and first_run_wake.APP_REFERENCE in warnings[0].message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("profile_email", ["person@gmail.com", "person@workspace.example", ""])
    async def test_app_facts_offer_google_setup_without_leaking_profile_email(
        self, monkeypatch, tmp_path, profile_email,
    ):
        from infra import config
        from onboarding import first_run_wake

        profile = tmp_path / "context" / "50-USER-PROFILE.md"
        profile.parent.mkdir()
        profile.write_text(
            "- **Preferred name:** Pat\x01 " + "n" * 130
            + (f"\n- **Email:** {profile_email}" if profile_email else "")
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        monkeypatch.setattr(first_run_wake, "_model_sources", lambda: {
            "sources": [{"source": "claude-code", "state": "ready"}],
            "github": {"state": "not_installed"}, "github_agent": {"state": "not_installed"},
        })
        monkeypatch.setattr(first_run_wake, "_own_omlx_source", lambda: False)
        monkeypatch.setattr("onboarding.setup_decisions.web_search_works", AsyncMock(return_value=True))
        monkeypatch.setattr(first_run_wake, "app_reference_served", lambda: True)
        monkeypatch.setattr(first_run_wake, "instructions", lambda path="script": "safe instructions")

        facts = await first_run_wake.setup_facts("codex/gpt-5.6-terra", "app")
        wake = first_run_wake.render(facts, "safe instructions")

        assert facts["gmail_eligible"] is True
        assert facts["user_profile"] == {"name": "Pat " + "n" * 116, "work": None, "place": None, "check_first": None, "answer_style": None}
        assert "the address you give me in chat" in wake
        assert not any(
            value in wake
            for value in (profile_email, "person+agents@gmail.com", str(tmp_path), "America/Los_Angeles")
            if value
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failed, expected", [
        ("model_sources", ("harness_ready", "sources_missing", "github_ready")),
        ("local_download", ("local_download",)),
        ("web_search_ready", ("web_search_ready",)),
        ("own_omlx_source", ("own_omlx_source",)),
        ("setup", ("setup",)),
        ("user_profile", ("user_profile",)),
        ("agents_found", ("agents_found",)),
    ])
    async def test_each_app_fact_read_names_its_unavailable_recovery(self, monkeypatch, failed, expected):
        from onboarding import first_run_wake

        def fail():
            raise OSError("unavailable")

        monkeypatch.setattr(first_run_wake, "_model_sources", lambda: {
            "sources": [], "github": {"state": "not_installed"}, "github_agent": {"state": "not_installed"},
        })
        monkeypatch.setattr(first_run_wake, "_own_omlx_source", lambda: False)
        monkeypatch.setattr(first_run_wake, "_app_user_profile", lambda: {})
        monkeypatch.setattr(first_run_wake, "_app_setup", AsyncMock(return_value={"setup_decisions": []}))
        monkeypatch.setattr("onboarding.setup_decisions.web_search_works", AsyncMock(return_value=False))
        if failed == "model_sources":
            monkeypatch.setattr(first_run_wake, "_model_sources", fail)
        elif failed == "local_download":
            monkeypatch.setattr(first_run_wake, "_app_local_download", fail)
        elif failed == "web_search_ready":
            monkeypatch.setattr("onboarding.setup_decisions.web_search_works", AsyncMock(side_effect=OSError("unavailable")))
        elif failed == "own_omlx_source":
            monkeypatch.setattr(first_run_wake, "_own_omlx_source", fail)
        elif failed == "setup":
            monkeypatch.setattr(first_run_wake, "_app_setup", AsyncMock(side_effect=OSError("unavailable")))
        elif failed == "agents_found":
            monkeypatch.setattr(first_run_wake, "_app_agents_found", fail)
        else:
            monkeypatch.setattr(first_run_wake, "_app_user_profile", fail)

        facts = await first_run_wake.setup_facts("codex/gpt-5.6-terra", "app")

        for name in expected:
            assert facts[name] == {"unavailable": first_run_wake.UNAVAILABLE_READS[name]}
        assert facts["gmail_eligible"] is True

    async def test_app_setup_decisions_omit_projects_consent_and_address(self, monkeypatch):
        from onboarding import first_run_wake, setup_decisions

        facts = setup_decisions.Facts(session_apps=("Claude Code",), email="person@gmail.com")
        monkeypatch.setattr(setup_decisions, "read_facts", AsyncMock(return_value=facts))

        decisions = (await first_run_wake._app_setup())["setup_decisions"]

        assert len(decisions) == 1  # GitHub only; no projects consent
        assert "person@gmail.com" not in json.dumps(decisions)
        assert "Claude Code" not in json.dumps(decisions)

    def test_app_reference_covers_reviewed_source_and_local_failure_edges(self):
        from infra import system_package
        from onboarding import first_run_wake

        reference = first_run_wake.instructions("app")
        local_offer = system_package.system_resource_source(
            "skills/conductor-bootstrap/references/local-model-offer.md",
        ).read_text(encoding="utf-8")

        assert "setup adds no models to it: don't make the offer" in local_offer
        assert "download lands in the person's oMLX" not in local_offer
        assert "When `harness_ready` is empty, omit the first sentence" in reference
        assert "if you have a [Claude | ChatGPT] subscription" in reference
        assert "Sign in to [Claude Code | Codex] in Settings › Accounts, then tell me when it's done." in reference
        assert "If it shows the source is ready, carry on." in reference
        assert "If it is still unsigned, once post" in reference
        assert "already `failed` in these initial facts was shown by the screens: say nothing about it" in reference
        assert "first time a later read shows it done or failed" in reference
        assert "handle that reply first, then post its `message` and the fix it names" in reference

    # The reference lists each fact as a top-level key; a parenthesised gloss may name nested keys
    # (`setup.setup_decisions`, the `user_profile` fields), which are not facts of their own.
    def test_served_app_reference_names_the_app_fact_keys(self):
        import re

        from onboarding import first_run_wake

        paragraph = first_run_wake.instructions("app").split("**Facts to read from `## Setup facts`**", 1)[1].split("Never restate", 1)[0]
        top_level = re.sub(r"\([^)]*\)", "", paragraph)
        keys = set(re.findall(r"`([a-z_]+)`", top_level))
        assert keys == first_run_wake.APP_FACT_KEYS - {"first_run_path", "setup"}
        assert "`setup.setup_decisions`" in paragraph  # setup is named through its nested decisions
