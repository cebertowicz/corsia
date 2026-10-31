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

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login

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

        assert set(facts) == {"first_run_path", "your_model", "model_sources", "mcp_sources", "setup", "host_access_reviewer", "web_search_key", "auto_approve"}
        assert facts["host_access_reviewer"] == {
            "unavailable": first_run_wake.UNAVAILABLE_READS["host_access_reviewer"],
        }
        assert "unavailable" not in facts["your_model"]

    @pytest.mark.asyncio
    async def test_a_failed_selected_model_fact_remains_unavailable(self):
        import asyncio

        from onboarding import first_run_wake

        failed = asyncio.get_running_loop().create_future()
        failed.set_exception(OSError("catalog unavailable"))

        facts = await first_run_wake._read_facts({"your_model": lambda: failed})

        assert facts == {
            "your_model": {"unavailable": first_run_wake.UNAVAILABLE_READS["your_model"]},
        }

    @pytest.mark.asyncio
    async def test_an_unfinished_fact_is_unavailable_after_the_total_bound(self, monkeypatch):
        import asyncio

        from onboarding import first_run_wake

        async def never():
            await asyncio.Event().wait()

        ready = asyncio.get_running_loop().create_future()
        ready.set_result({"display_name": "Sonnet", "local": False})
        # The completed future makes the successful fact independent of event
        # loop and DB-pool timing; only the deliberate never-ending read uses
        # the zero shared budget.
        monkeypatch.setattr(first_run_wake, "FACTS_TIMEOUT_SECONDS", 0)

        facts = await first_run_wake._read_facts({
            "your_model": lambda: ready,
            "setup": never,
        })

        assert facts == {
            "your_model": {"display_name": "Sonnet", "local": False},
            "setup": {"unavailable": first_run_wake.UNAVAILABLE_READS["setup"]},
        }

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
        assert all(f"`{name}`" in reference for name in ("your_model", "model_sources", "mcp_sources", "setup", "host_access_reviewer", "web_search_key", "auto_approve"))


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
    async def test_served_app_path_uses_compact_instructions_and_only_first_step(self, monkeypatch):
        from onboarding import first_run_wake

        app_facts = {
            "first_run_path": "app", "harness_ready": ["codex"], "sources_missing": {},
            "agents_found": ["hermes"], "local_download": {"state": "stopped"}, "local_eligible": False, "auto_approve": True,
            "tool_attempts": {"claude_code": {"attempt": "none"}, "codex": {"attempt": "none"}},
            "pays_for": [],
            "bound_model": None,
            "web_search_ready": False, "web_search_key": False, "github_ready": False,
            "setup": {"setup_decisions": []},
            "user_profile": {"name": "Pat"},
        }
        monkeypatch.setattr(first_run_wake, "app_reference_served", lambda: True)
        monkeypatch.setattr("onboarding.app_first_run.read_app_facts", AsyncMock(return_value=app_facts))
        monkeypatch.setattr(first_run_wake, "instructions", lambda path="script": f"{path} instructions")

        wake = await first_run_wake.render_for("codex/gpt-5.6-terra", "app")

        assert first_run_wake.APP_INLINE_SUMMARY in wake
        assert "app instructions" in wake
        assert "## Your first step" in wake
        assert "3. **Where your work lives" in wake
        assert "4. **Web search" not in wake
        assert "## The opening" not in wake
        assert set(app_facts) == first_run_wake.APP_FACT_KEYS
        assert not any(key in wake for key in ("mcp_sources", "your_model", "model_sources", "host_access_reviewer"))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("source_rows", "expected_missing"), [
        (({"state": "ready"}, {"state": "ready"}), {}),
        (({"state": "not_installed"}, {"state": "ready"}), {"claude_code": "not_installed"}),
        (({"state": "ready"}, {"state": "needs_auth"}), {"codex": "needs_auth"}),
        (({"state": "unverified"}, {"state": "ready"}), {"claude_code": "unusable"}),
        (({"state": "ready"}, {"state": "ready", "limited": True}), {"codex": "limited"}),
    ])
    @pytest.mark.parametrize(("status", "expected_download"), [
        ({"state": "downloading", "model": "qwen", "percent": 50}, {"state": "downloading", "model": "qwen", "percent": 50}),
        ({"state": "ready", "model": "qwen"}, {"state": "ready", "model": "qwen"}),
        ({"state": "failed", "cause": "disk_full", "bytes_to_free": 1024}, {"state": "failed", "cause": "disk_full", "bytes_to_free": 1024}),
        ({"state": "setup_failed", "cause": "other"}, {"state": "setup_failed", "cause": "other"}),
        ({"state": "idle", "stage": None, "result": None}, {"state": "stopped"}),
    ])
    async def test_app_setup_facts_carry_every_source_and_download_state(
        self, monkeypatch, source_rows, expected_missing, status, expected_download,
    ):
        from onboarding import first_run_wake

        monkeypatch.setattr(first_run_wake, "_model_sources", lambda: {
            "sources": [
                {"source": "claude-code", **source_rows[0]},
                {"source": "codex", **source_rows[1]},
                {"source": "omlx", "state": "needs_setup", "eligible": True},
            ],
            "github": {"state": "not_installed"}, "github_agent": {"state": "not_installed"},
        })
        monkeypatch.setattr("llm.local_setup.setup_status", lambda: status)
        monkeypatch.setattr(first_run_wake, "get_host_access_auto_approve", lambda: False)
        monkeypatch.setattr(first_run_wake, "_app_setup_handoff", lambda: {
            "tools": {"claude-code": {"attempt": "none"}, "codex": {"attempt": "install_failed", "cause": "download_unavailable"}},
        })
        monkeypatch.setattr(first_run_wake, "_app_setup_picks", lambda: {"claude": False, "chatgpt": True, "key": False})
        monkeypatch.setattr(first_run_wake, "_app_user_profile", lambda: {})
        monkeypatch.setattr(first_run_wake, "_app_setup", AsyncMock(return_value={"setup_decisions": []}))
        monkeypatch.setattr("onboarding.setup_decisions.web_search_works", AsyncMock(return_value=False))
        monkeypatch.setattr(first_run_wake, "_app_agents_found", lambda: ["openclaw"])

        facts = await first_run_wake.setup_facts("codex/gpt-5.6-terra", "app")

        assert facts["sources_missing"] == expected_missing
        assert facts["local_download"] == expected_download
        assert facts["local_eligible"] is True
        assert facts["auto_approve"] is False
        assert facts["tool_attempts"] == {
            "claude_code": {"attempt": "none"},
            "codex": {"attempt": "install_failed", "cause": "download_unavailable"},
        }
        assert facts["pays_for"] == ["codex"]
        assert facts["bound_model"] is None
        assert facts["agents_found"] == ["openclaw"]
        assert set(facts) == first_run_wake.APP_FACT_KEYS

    @pytest.mark.parametrize(("omlx_row", "expected"), [
        ({"source": "omlx", "eligible": False, "state": "not_installed"}, False),
        ({"source": "omlx", "eligible": False}, False),
        ({"source": "omlx"}, False),
        ({"source": "omlx", "eligible": False, "state": "ready"}, True),
        ({"source": "omlx", "eligible": True, "state": "not_installed"}, True),
        (None, False),
    ])
    def test_a_computer_reads_eligible_when_it_can_run_a_local_model_or_already_runs_one(self, omlx_row, expected):
        from onboarding import first_run_wake

        rows = [omlx_row] if omlx_row else [{"source": "codex", "eligible": True}]
        assert first_run_wake._app_local_eligible({"sources": rows}) is expected

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
        markers = {"openclaw": tmp_path / ".openclaw" / "workspace", "hermes": tmp_path / ".hermes" / "memories"}
        for source in present:
            markers[source].mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        opened: list[str] = []
        for target, name in ((builtins, "open"), (io, "open"), (Path, "open"), (Path, "read_text"), (Path, "read_bytes")):
            original = getattr(target, name)
            monkeypatch.setattr(target, name, lambda *a, _original=original, **k: opened.append(str(a[0])) or _original(*a, **k))

        found = first_run_wake._app_agents_found()

        assert found == expected
        assert not [path for path in opened if str(tmp_path) in path]

    def test_app_user_profile_carries_the_full_name_beside_the_preferred_name(self, monkeypatch, tmp_path):
        from infra import config, user_profile
        from onboarding import first_run_wake

        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        (tmp_path / "context").mkdir()
        (tmp_path / user_profile.PROFILE_RELATIVE_PATH).write_text(
            user_profile.profile_text("Ada Lovelace", ""), encoding="utf-8",
        )

        profile = first_run_wake._app_user_profile()

        assert profile["name"] == "Ada" and profile["full_name"] == "Ada Lovelace"

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
        monkeypatch.setattr(first_run_wake, "_app_setup_handoff", lambda: None)
        monkeypatch.setattr("onboarding.setup_decisions.web_search_works", AsyncMock(return_value=True))
        monkeypatch.setattr(first_run_wake, "app_reference_served", lambda: True)
        monkeypatch.setattr(first_run_wake, "instructions", lambda path="script": "safe instructions")

        facts = await first_run_wake.setup_facts("codex/gpt-5.6-terra", "app")
        wake = first_run_wake.render(facts, "safe instructions")

        assert facts["tool_attempts"] is None
        assert facts["bound_model"] is None
        assert facts["user_profile"] == {"name": "Pat " + "n" * 116, "full_name": None}
        assert "ask you in chat for an email address not already used on GitHub" in wake
        assert not any(
            value in wake
            for value in (profile_email, "person+agents@gmail.com", str(tmp_path), "America/Los_Angeles")
            if value
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failed, expected", [
        ("model_sources", ("harness_ready", "sources_missing", "github_ready", "local_eligible")),
        ("local_download", ("local_download",)),
        ("auto_approve", ("auto_approve",)),
        ("setup_handoff", ("tool_attempts",)),
        ("setup_picks", ("pays_for",)),
        ("web_search_ready", ("web_search_ready",)),
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
        monkeypatch.setattr(first_run_wake, "_app_setup_handoff", lambda: None)
        monkeypatch.setattr(first_run_wake, "_app_setup_picks", lambda: None)
        monkeypatch.setattr(first_run_wake, "_app_user_profile", lambda: {})
        monkeypatch.setattr(first_run_wake, "_app_setup", AsyncMock(return_value={"setup_decisions": []}))
        monkeypatch.setattr("onboarding.setup_decisions.web_search_works", AsyncMock(return_value=False))
        if failed == "model_sources":
            monkeypatch.setattr(first_run_wake, "_model_sources", fail)
        elif failed == "local_download":
            monkeypatch.setattr(first_run_wake, "_app_local_download", fail)
        elif failed == "auto_approve":
            monkeypatch.setattr(first_run_wake, "get_host_access_auto_approve", fail)
        elif failed == "web_search_ready":
            monkeypatch.setattr("onboarding.setup_decisions.web_search_works", AsyncMock(side_effect=OSError("unavailable")))
        elif failed == "setup_handoff":
            monkeypatch.setattr(first_run_wake, "_app_setup_handoff", fail)
        elif failed == "setup_picks":
            monkeypatch.setattr(first_run_wake, "_app_setup_picks", fail)
        elif failed == "setup":
            monkeypatch.setattr(first_run_wake, "_app_setup", AsyncMock(side_effect=OSError("unavailable")))
        elif failed == "agents_found":
            monkeypatch.setattr(first_run_wake, "_app_agents_found", fail)
        else:
            monkeypatch.setattr(first_run_wake, "_app_user_profile", fail)

        facts = await first_run_wake.setup_facts("codex/gpt-5.6-terra", "app")

        for name in expected:
            assert facts[name] == {"unavailable": first_run_wake.UNAVAILABLE_READS[name]}

    @pytest.mark.parametrize(("record", "expected"), [
        ({"step": "models", "picks": {"claude": True, "chatgpt": True, "key": False}}, ["claude_code", "codex"]),
        ({"step": "models", "picks": {"claude": True, "chatgpt": False, "key": False}}, ["claude_code"]),
        ({"step": "models", "picks": {"claude": False, "chatgpt": True, "key": False}}, ["codex"]),
        ({"step": "models", "picks": {"claude": False, "chatgpt": False, "key": False}}, []),
        ({"step": "models", "picks": None}, []),
        ({"step": "models"}, []),
        (None, []),
    ])
    def test_pays_for_comes_from_the_subscriptions_screens_saved_picks(self, monkeypatch, record, expected):
        from infra import setup_progress
        from onboarding import first_run_wake

        monkeypatch.setattr(setup_progress, "read", lambda **_: record)

        assert first_run_wake._pays_for(first_run_wake._app_setup_picks()) == expected

    @pytest.mark.asyncio
    async def test_a_person_who_pays_for_both_with_only_claude_signed_in_is_offered_codex(self, monkeypatch):
        from infra import setup_progress
        from onboarding import first_run_wake

        monkeypatch.setattr(setup_progress, "read", lambda **_: {
            "step": "models", "picks": {"claude": True, "chatgpt": True, "key": False},
            "handoff": {"tools": {"claude-code": {"attempt": "none"}, "codex": {"attempt": "none"}}},
        })
        monkeypatch.setattr(first_run_wake, "_model_sources", lambda: {
            "sources": [{"source": "claude-code", "state": "ready"}, {"source": "codex", "state": "not_installed"}],
            "github": {"state": "not_installed"}, "github_agent": {"state": "not_installed"},
        })
        monkeypatch.setattr("llm.local_setup.setup_status", lambda: {"state": "idle", "stage": None, "result": None})
        monkeypatch.setattr(first_run_wake, "_app_user_profile", lambda: {})
        monkeypatch.setattr(first_run_wake, "_app_setup", AsyncMock(return_value={"setup_decisions": []}))
        monkeypatch.setattr("onboarding.setup_decisions.web_search_works", AsyncMock(return_value=False))
        monkeypatch.setattr(first_run_wake, "_app_agents_found", lambda: [])

        facts = await first_run_wake.setup_facts("codex/gpt-5.6-terra", "app")

        assert facts["sources_missing"] == {"codex": "not_installed"}
        assert facts["pays_for"] == ["claude_code", "codex"]
        offered = [tool for tool in facts["sources_missing"] if tool in facts["pays_for"]]
        assert offered == ["codex"]

    async def test_app_setup_decisions_omit_projects_consent_and_address(self, monkeypatch):
        from onboarding import first_run_wake, setup_decisions

        facts = setup_decisions.Facts(session_apps=("Claude Code",), email="person@gmail.com")
        monkeypatch.setattr(setup_decisions, "read_facts", AsyncMock(return_value=facts))

        decisions = (await first_run_wake._app_setup())["setup_decisions"]

        assert len(decisions) == 3  # the community card and both GitHub cards; no projects consent
        assert {d["task_title"] for d in decisions} == {"Set up GitHub"}
        assert "person@gmail.com" not in json.dumps(decisions)
        assert "Claude Code" not in json.dumps(decisions)

    def test_app_reference_covers_reviewed_source_and_local_failure_edges(self):
        from infra import system_package
        from onboarding import first_run_wake

        reference = system_package.system_resource_source(first_run_wake.APP_REFERENCE).read_text()
        local_offer = system_package.system_resource_source(
            "skills/conductor-bootstrap/references/local-model-offer.md",
        ).read_text(encoding="utf-8")

        assert "if a local model is connected, make no offer" in local_offer
        assert "download lands in the person's oMLX" not in local_offer
        assert "a tool `pays_for` doesn't list is never offered or mentioned" in reference
        assert "You said you pay for [Claude | ChatGPT]." in reference
        assert "Setup couldn't install [Claude Code | Codex]: [what went wrong, in plain words]." in reference
        assert "Setup's sign-in to [Claude Code | Codex] didn't finish." in reference
        assert "[Settings › Providers](/settings/providers), press Sign in on its card" in reference
        assert "Never initiate or cancel the vendor sign-in yourself" in reference
        assert "POST /api/vendor-accounts/{id}/signin" not in reference
        assert "Never read the local download yourself." in reference
        assert "Post the news first, as its own message, word for word as below; it never asks anything." in reference
        assert "when your question is still unanswered, end your turn: that question stays open" in reference
        assert "Never write the opening or create the setup plan" in reference
        assert "mark each done when complete" in reference
        assert "The tasks steps 3 and 6 make are follow-up work, so do add those" in reference
        assert "already shows that same outcome, say nothing" in reference
        assert "Done for now. Anything I skipped, ask me any time and I'll set it up." in reference

    # The reference lists each fact as a top-level key; a parenthesised gloss may name nested keys
    # (`setup.setup_decisions`, the `user_profile` fields), which are not facts of their own.
    def test_served_app_reference_names_the_app_fact_keys(self):
        import re

        from onboarding import first_run_wake

        from infra.system_package import system_resource_source

        reference = system_resource_source(first_run_wake.APP_REFERENCE).read_text()
        paragraph = reference.split("**Facts to read from `## Setup facts`**", 1)[1].split("Never restate", 1)[0]
        top_level = re.sub(r"\([^)]*\)", "", paragraph)
        keys = set(re.findall(r"`([a-z_]+)`", top_level))
        assert keys == first_run_wake.APP_PLAN_FACT_KEYS - {"first_run_path", "setup"}
        assert "`setup.setup_decisions`" in paragraph  # setup is named through its nested decisions
