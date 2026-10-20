
from __future__ import annotations

import json
from types import SimpleNamespace

from thread.model_state import ModelSwitchOutcome
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from delegation_fixtures import _TOKEN, client, deleg_db



class TestRequireExplicitThreadModel:
    def test_passed_model_wins(self, deleg_db):
        from infra.delegation import require_explicit_thread_model, set_setting
        stale_key = "role" + "_defaults"
        set_setting("delegation", {stale_key: {"subagent": "claude-code/fable"}})
        assert require_explicit_thread_model("anthropic/some-model") == "anthropic/some-model"

    def test_configured_agents_default_is_ignored(self, deleg_db, monkeypatch):
        from infra.delegation import require_explicit_thread_model, set_setting, DelegationModelRequired
        set_setting("agents", {"defaults": {"model": "anthropic/system-default"}})
        with pytest.raises(DelegationModelRequired):
            require_explicit_thread_model(None)

    def test_fail_loud_when_nothing_resolves(self, deleg_db, monkeypatch):
        from infra.delegation import require_explicit_thread_model, DelegationModelRequired
        with pytest.raises(DelegationModelRequired):
            require_explicit_thread_model(None)
        with pytest.raises(DelegationModelRequired):
            require_explicit_thread_model("   ")  # blank is "no model"

class TestDelegationDefaultCleanup:
    def test_migrate_drops_stale_delegation_default_maps(self, deleg_db):
        from infra.delegation import migrate_delisted_models, set_setting
        from infra.settings import get_setting
        stale_key = "role" + "_defaults"
        set_setting("delegation", {stale_key: {
            "conductor": "claude-code/fable",   # delisted
            "coordinator": "claude-code/opus",
            "subagent": "claude-code/sonnet",
        }, "other_setting": True})
        assert migrate_delisted_models() is True
        current = get_setting("delegation") or {}
        assert stale_key not in current
        assert current["other_setting"] is True
        assert migrate_delisted_models() is False

    def test_migrate_clears_delisted_agents_default(self, deleg_db):
        from infra.delegation import migrate_delisted_models
        from infra.settings import get_setting, set_setting
        set_setting("agents", {"defaults": {
            "model": "claude-fable-5",          # delisted (resolved id form)
            "temperature": 0.2,                  # non-model field untouched
        }})
        assert migrate_delisted_models() is True
        defaults = (get_setting("agents") or {}).get("defaults", {})
        assert "model" not in defaults           # removed → resolution fails loud
        assert defaults.get("temperature") == 0.2

    def test_get_setting_returns_none_for_non_object_json(self, deleg_db):
        import json
        from infra.database import get_db
        from infra.settings import get_setting

        db = get_db()
        try:
            db.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                ("corsia_agent_thread_id", json.dumps("cond-workspace"), "t"),
            )
            db.commit()
        finally:
            db.close()

        assert get_setting("corsia_agent_thread_id") is None

class TestCreateThread:
    def test_auth_required(self, client):
        r = client.post("/api/delegation/threads", params={"token": "bad"}, json={})
        assert r.status_code == 401

    def test_passed_model_persists(self, client, deleg_db, monkeypatch):
        monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *a, **k: None)
        deleg_db.execute(
            "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
            "VALUES ('par-model', 'u-test', 'Parent', '2026-01-01', '2026-01-01')"
        )
        deleg_db.commit()
        r = client.post("/api/delegation/threads", params={"token": _TOKEN},
                        json={"title": "T", "model": "anthropic/claude-3-5-sonnet",
                              "thread_kind": "subagent", "spawned_by": "par-model"})
        assert r.status_code == 200
        body = r.json()
        assert body["model"] == "anthropic/claude-3-5-sonnet"
        sid = body["thread_id"]
        row = deleg_db.execute("SELECT model, title FROM threads WHERE id = ?",
                               (sid,)).fetchone()
        assert row["model"] == "anthropic/claude-3-5-sonnet"
        assert row["title"] == "T"

    def test_explicit_local_model_ignores_stale_delegation_default(self, client, deleg_db, monkeypatch):
        from infra.settings import set_setting

        monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *a, **k: None)
        deleg_db.execute(
            "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
            "VALUES ('par-local', 'u-test', 'Parent', '2026-01-01', '2026-01-01')"
        )
        deleg_db.commit()
        stale_key = "role" + "_defaults"
        set_setting("delegation", {stale_key: {"subagent": "claude-code/fable"}})

        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={"title": "Local", "model": "omlx@local/qwen",
                  "thread_kind": "subagent", "spawned_by": "par-local"},
        )

        assert r.status_code == 200
        assert r.json()["model"] == "omlx@local/qwen"
        sid = r.json()["thread_id"]
        row = deleg_db.execute("SELECT model FROM threads WHERE id = ?", (sid,)).fetchone()
        assert row["model"] == "omlx@local/qwen"

    def test_create_thread_emits_visible_session_payload(self, client, deleg_db, monkeypatch):
        monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *a, **k: None)
        deleg_db.execute(
            "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
            "VALUES ('par-emit', 'u-test', 'Parent', '2026-01-01', '2026-01-01')"
        )
        deleg_db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) "
            "VALUES ('corsia-project', 'CORSIA', '2026-01-01', '2026-01-01')"
        )
        deleg_db.commit()

        emitted: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "infra.state.broadcast_to_user",
            lambda user_id, payload: emitted.append((user_id, payload)),
        )

        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={
                "title": "Fresh Spawn Visibility",
                "model": "codex/gpt-5-codex",
                "thread_kind": "subagent",
                "project_id": "corsia-project",
                "spawned_by": "par-emit",
            },
        )

        assert r.status_code == 200
        sid = r.json()["thread_id"]
        assert emitted == [
            (
                "u-test",
                {
                    "type": "session_created",
                    "session": {
                        "id": sid,
                        "user_id": "u-test",
                        "profile": "personal",
                        "title": "Fresh Spawn Visibility",
                        "model": "codex/gpt-5-codex",
                        "project_id": "corsia-project",
                        "parent_session_id": None,
                        "spawned_by": "par-emit",
                        "local_only": 0,
                        "local_only_inherited_from_thread_id": None,
                        "reasoning_effort": None,
                        "created_at": emitted[0][1]["session"]["created_at"],
                        "updated_at": emitted[0][1]["session"]["updated_at"],
                    },
                },
            )
        ]

    @pytest.mark.parametrize("body", [{"thread_kind": "chat"}, {"title": "T", "model": "  "}, {"model": None}])
    def test_missing_or_blank_model_is_422_and_creates_nothing(self, client, deleg_db, monkeypatch, body):
        from infra.settings import set_setting
        set_setting("agents", {"defaults": {"model": "claude-code/opus"}})
        before = deleg_db.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        r = client.post("/api/delegation/threads", params={"token": _TOKEN}, json=body)
        assert r.status_code == 422
        assert "model is required" in r.json()["detail"].lower()
        assert deleg_db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == before

    def test_rejects_unroutable_selection_id_at_create(self, client, deleg_db):
        # An unregistered selection cannot produce a canonical execution target.
        # Persisting it would create a thread that silently no-ops at native
        # dispatch, so create must fail loud and NOT persist a doomed thread.
        r = client.post(
            "/api/delegation/threads", params={"token": _TOKEN},
            json={"title": "Doomed", "model": "omlx/abc123"},
        )
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "canonical execution target" in detail.lower()
        assert "register or discover the exact model" in detail.lower()
        # The doomed thread must not be persisted. (The fixture seeds a
        # 'sender-self' thread for attributed sends, so assert on the doomed
        # title specifically rather than a global session count.)
        row = deleg_db.execute(
            "SELECT COUNT(*) AS c FROM threads WHERE title = 'Doomed'"
        ).fetchone()
        assert row["c"] == 0

    def test_local_only_model_refusal_is_400(self, client, monkeypatch):
        import api.delegation as delegation_api

        async def refuse(**_kwargs):
            raise ValueError("local_only_requires_local_model: choose local")

        monkeypatch.setattr(delegation_api, "create_visible_fresh_thread", refuse)

        response = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={"model": "claude-code/sonnet"},
        )

        assert response.status_code == 400
        assert "local_only_requires_local_model" in response.json()["detail"]


class TestDelegationSendConfirmation:
    def test_local_only_sender_requires_confirmation_before_rest_delivery(
        self,
        client,
        deleg_db,
        monkeypatch,
    ):
        import api.delegation as delegation_api
        import infra.local_only as local_only

        deleg_db.execute(
            "UPDATE threads SET local_only = 1 WHERE id = 'sender-self'"
        )
        deleg_db.execute(
            "INSERT INTO threads (id, user_id, model, title, created_at, updated_at) "
            "VALUES ('cloud-target', 'u-test', 'anthropic/claude-3-5-sonnet', "
            "'Cloud Target', 't', 't')"
        )
        deleg_db.commit()
        delivered: list[str] = []

        async def deliver(*_args, **_kwargs):
            delivered.append("delivered")
            return SimpleNamespace(driver="turn_engine", duplicate=False)

        monkeypatch.setattr(
            local_only,
            "local_only_model_refusal",
            lambda model, **_kwargs: "cloud" if model == "anthropic/claude-3-5-sonnet" else None,
        )
        monkeypatch.setattr(delegation_api, "send_thread_input", deliver)

        refused = client.post(
            "/api/delegation/threads/cloud-target/send",
            params={"token": _TOKEN},
            json={"message": "private material", "from_thread_id": "sender-self"},
        )
        assert refused.status_code == 409
        assert "local_only_send_needs_confirm" in refused.json()["detail"]
        assert delivered == []

        confirmed = client.post(
            "/api/delegation/threads/cloud-target/send",
            params={"token": _TOKEN},
            json={
                "message": "safe summary",
                "from_thread_id": "sender-self",
                "no_sensitive_content": True,
            },
        )

        assert confirmed.status_code == 200
        assert delivered == ["delivered"]


class TestThreadStatus:
    def test_status_keeps_removed_model_selector_readable(
            self, client, deleg_db):
        removed_model = "omlx@local/qwen-flash-next"
        deleg_db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, spawned_by, title, created_at, updated_at) "
            "VALUES ('stale-model', 'u-test', ?, 'parent', 'Stale worker', "
            "'now', 'now')",
            (removed_model,),
        )
        deleg_db.commit()

        with TestClient(client.app, raise_server_exceptions=False) as readable_client:
            response = readable_client.get(
                "/api/delegation/threads/stale-model/status",
                params={"token": _TOKEN},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["model"] == removed_model
        assert body["driver"] is None
        assert body["execution_class"] is None
        assert body["addressable"] is False
        assert body["addressability_error"] == (
            f"Model '{removed_model}' not found in catalog. Choose another model."
        )

    def test_status_reports_runtime_truth_for_reconstructable_codex_conductor(
            self, client, deleg_db, monkeypatch):
        deleg_db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, title, created_at, updated_at) "
            "VALUES ('cx-lost-main', 'u-test', 'codex/gpt-5-codex', "
            "'Project conductor', 'now', 'now')")
        deleg_db.execute(
            "INSERT INTO projects (id, name, conductor_thread_id, created_at, updated_at) "
            "VALUES ('proj-a', 'Proj A', 'cx-lost-main', 'now', 'now')")
        deleg_db.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at) "
            "VALUES ('m-prior-main', 'cx-lost-main', 'assistant', 'prior output', 'now')")
        deleg_db.commit()

        from subagents import cc_runtime, codex_runtime
        monkeypatch.setattr(cc_runtime, "is_live", lambda sid: False)
        monkeypatch.setattr(codex_runtime, "is_live", lambda sid: False)
        monkeypatch.setattr(codex_runtime, "has_thread_id", AsyncMock(return_value=False))
        monkeypatch.setattr(codex_runtime, "has_prior_messages", AsyncMock(return_value=True))

        r = client.get("/api/delegation/threads/cx-lost-main/status",
                       params={"token": _TOKEN})
        assert r.status_code == 200
        body = r.json()
        assert body["model"] == "codex/gpt-5-codex"
        assert body["driver"] == "codex"
        assert body["execution_class"] == "codex_main"
        assert body["addressable"] is True
        assert body["addressability_error"] is None
        assert body["codex_has_thread"] is False
        assert body["codex_has_prior_messages"] is True
        assert body["conductor_scopes"] == [
            {"scope": "proj-a", "scope_type": "project", "project_name": "Proj A"}
        ]

    def test_status_keeps_durable_codex_handle_when_local_context_untrusted(
            self, client, deleg_db, monkeypatch):
        deleg_db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, spawned_by, title, created_at, updated_at) "
            "VALUES ('cx-compact-status', 'u-test', 'codex/gpt-5-codex', "
            "'parent', 'Codex', 'now', 'now')")
        deleg_db.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at) "
            "VALUES ('m-prior-compact', 'cx-compact-status', 'assistant', "
            "'prior output', 'now')")
        deleg_db.commit()

        from registry import models
        from subagents import cc_runtime, codex_runtime
        models.register_artifact(
            integration="codex",
            type="thread",
            external_id="thread_provider_compacted",
            parent_thread_id="cx-compact-status",
            db=deleg_db,
        )
        codex_runtime._PROVIDER_CONTEXT_UNTRUSTED.add("cx-compact-status")
        monkeypatch.setattr(cc_runtime, "is_live", lambda sid: False)
        monkeypatch.setattr(codex_runtime, "is_live", lambda sid: False)

        r = client.get("/api/delegation/threads/cx-compact-status/status",
                       params={"token": _TOKEN})

        assert r.status_code == 200
        body = r.json()
        assert body["driver"] == "codex"
        assert body["addressable"] is True
        assert body["codex_provider_context_untrusted"] is True
        assert body["codex_has_thread"] is True
        assert body["codex_has_prior_messages"] is False
        assert codex_runtime._CODEX_THREADS["cx-compact-status"] == (
            "thread_provider_compacted"
        )

    def test_status_reports_codex_live_as_addressable_not_busy(
        self, client, deleg_db, monkeypatch,
    ):
        deleg_db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, spawned_by, title, created_at, updated_at) "
            "VALUES ('cx-live', 'u-test', 'codex/gpt-5-codex', 'parent', "
            "'Codex', 'now', 'now')")
        deleg_db.commit()
        from subagents import cc_runtime, codex_runtime
        monkeypatch.setattr(cc_runtime, "is_live", lambda sid: False)
        monkeypatch.setattr(codex_runtime, "is_live", lambda sid: True)

        r = client.get("/api/delegation/threads/cx-live/status",
                       params={"token": _TOKEN})
        assert r.status_code == 200
        body = r.json()
        assert body["driver"] == "codex"
        assert body["turn_active"] is False
        assert body["cc_live"] is False
        assert body["codex_live"] is True
        assert body["busy"] is False
        assert body["busy_reason"] is None

    def test_status_reports_turn_and_cc_live(self, client, deleg_db, monkeypatch):
        deleg_db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, spawned_by, title, created_at, updated_at) "
            "VALUES ('t1', 'u-test', 'claude-code/sonnet', 'parent', 'Title', "
            "'now', 'now')")
        deleg_db.execute(
            "INSERT INTO active_turns "
            "(id, thread_id, execution_epoch, model, status, started_at, boot_epoch) "
            "VALUES ('turn1', 't1', 0, 'claude-code/sonnet', 'running', "
            "'2026-06-11T00:00:00', 'test')")
        deleg_db.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at) "
            "VALUES ('m1', 't1', 'assistant', 'done', '2026-06-11T00:01:00')")
        deleg_db.commit()
        from subagents import cc_runtime
        monkeypatch.setattr(cc_runtime, "is_live", lambda sid: True)

        r = client.get("/api/delegation/threads/t1/status", params={"token": _TOKEN})
        assert r.status_code == 200
        body = r.json()
        assert body["turn_active"] is True
        assert body["busy_reason"] == "active_turn"
        assert body["cc_live"] is True
        assert body["busy"] is True
        assert body["driver"] == "cc"
        assert body["recent_messages"][-1]["content"] == "done"

    def test_status_404_for_unknown(self, client, deleg_db):
        r = client.get("/api/delegation/threads/nope/status", params={"token": _TOKEN})
        assert r.status_code == 404

    def test_status_reply_appears_exactly_once(self, client, deleg_db, monkeypatch):
        import json as _json
        deleg_db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, spawned_by, created_at, updated_at) "
            "VALUES ('dup-s', 'u-test', 'claude-code/sonnet', 'parent', "
            "'now', 'now')")
        deleg_db.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at) "
            "VALUES ('dup-a', 'dup-s', 'assistant', 'the-reply', '2026-06-11T00:01:00')")
        deleg_db.execute(
            "INSERT INTO messages (id, thread_id, role, content, metadata, created_at) "
            "VALUES ('dup-r', 'dup-s', 'system', '', ?, '2026-06-11T00:01:01')",
            (_json.dumps({"cc": True, "type": "result", "is_error": False,
                          "stop_reason": "end_turn"}),))
        deleg_db.commit()
        from subagents import cc_runtime
        monkeypatch.setattr(cc_runtime, "is_live", lambda sid: False)

        r = client.get("/api/delegation/threads/dup-s/status", params={"token": _TOKEN})
        assert r.status_code == 200
        recent = r.json()["recent_messages"]

        matches = [m for m in recent if m.get("content") == "the-reply"]
        assert len(matches) == 1, (
            f"reply text must appear in exactly 1 message; got {len(matches)}: "
            + str([(m['role'], m['content'][:60]) for m in matches]))


class TestSetModelReaddress:
    """#447 — supported, explicit re-addressability action for a thread wedged
    by the Codex resume guard. Mechanism only; live thread mutation is a
    conductor/operator action and out of scope here."""

    def _stub_switch_effects(self, monkeypatch):
        """Stub the post-switch side effects + capture the model.switched event."""
        events: list = []
        monkeypatch.setattr("subagents.cc_runtime.is_live", lambda sid: False)
        monkeypatch.setattr("infra.state.broadcast_to_thread",
                            lambda *a, **k: None)
        monkeypatch.setattr(
            "thread.model_runtime.emit_product_event",
            lambda category, action, **k: events.append((category, action, k)),
        )
        return events

    def _wedged_codex(self, db, sid="cx-wedged"):
        """A Codex-selected thread with transcript history and no resume handle."""
        db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, title, created_at, updated_at, next_message_seq) "
            "VALUES (?, 'u-test', 'codex/gpt-5-codex', 'Documents Design', "
            "'now', 'now', 2)",
            (sid,),
        )
        db.execute(
            "INSERT INTO messages "
            "(id, thread_id, role, content, metadata, created_at, seq) "
            "VALUES (?, ?, 'assistant', 'prior design notes', ?, 'now', 1)",
            (
                f"m-{sid}",
                sid,
                json.dumps({"turn_unbound_reason": "legacy_import"}),
            ),
        )
        db.commit()
        return sid

    def test_missing_model_is_422(self, client, deleg_db):
        self._wedged_codex(deleg_db)
        r = client.post("/api/delegation/threads/cx-wedged/set-model",
                        params={"token": _TOKEN},
                        json={"from_thread_id": "sender-self"})
        assert r.status_code == 422
        assert "no silent default" in r.json()["detail"].lower()

    def test_non_string_model_is_422(self, client, deleg_db):
        self._wedged_codex(deleg_db)
        r = client.post(
            "/api/delegation/threads/cx-wedged/set-model",
            params={"token": _TOKEN},
            json={"model": 42, "from_thread_id": "sender-self"},
        )
        assert r.status_code == 422
        assert "no silent default" in r.json()["detail"].lower()

    def test_non_dispatchable_model_is_422(self, client, deleg_db):
        self._wedged_codex(deleg_db)
        r = client.post("/api/delegation/threads/cx-wedged/set-model",
                        params={"token": _TOKEN},
                        json={"model": "omlx/abc123",
                              "from_thread_id": "sender-self"})
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "canonical execution target" in detail.lower()
        assert "register or discover the exact model" in detail.lower()
        # Model must NOT have changed on a rejected switch.
        row = deleg_db.execute(
            "SELECT model FROM threads WHERE id = 'cx-wedged'").fetchone()
        assert row["model"] == "codex/gpt-5-codex"

    def test_missing_attribution_is_400(self, client, deleg_db):
        self._wedged_codex(deleg_db)
        r = client.post("/api/delegation/threads/cx-wedged/set-model",
                        params={"token": _TOKEN},
                        json={"model": "anthropic/claude-3-5-sonnet"})
        assert r.status_code == 400
        assert "from_thread_id is required" in r.json()["detail"]

    def test_unowned_sender_is_403(self, client, deleg_db):
        self._wedged_codex(deleg_db)
        r = client.post("/api/delegation/threads/cx-wedged/set-model",
                        params={"token": _TOKEN},
                        json={"model": "anthropic/claude-3-5-sonnet",
                              "from_thread_id": "ghost-not-owned"})
        assert r.status_code == 403

    def test_unknown_target_is_404(self, client, deleg_db):
        r = client.post("/api/delegation/threads/nope/set-model",
                        params={"token": _TOKEN},
                        json={"model": "anthropic/claude-3-5-sonnet",
                              "from_thread_id": "sender-self"})
        assert r.status_code == 404

    def test_switch_routes_through_canonical_preparation(
            self, client, deleg_db, monkeypatch):
        import api.delegation_model as model_api

        self._wedged_codex(deleg_db)
        outcome = ModelSwitchOutcome(
            changed=True,
            old_model="codex/gpt-5-codex",
            new_model="anthropic/claude-3-5-sonnet",
            preparation=SimpleNamespace(
                projection_mode="exact",
            ),
            breadcrumb=None,
            committed_at="2026-07-17T08:00:00.000000",
            post_effects_error="retirement failed",
        )
        switch = AsyncMock(return_value=outcome)
        monkeypatch.setattr(model_api, "switch_model", switch)

        r = client.post(
            "/api/delegation/threads/cx-wedged/set-model",
            params={"token": _TOKEN},
            json={
                "model": "anthropic/claude-3-5-sonnet",
                "from_thread_id": "sender-self",
                "reason": "repair",
            },
        )

        assert r.status_code == 200, r.text
        assert r.json()["changed"] is True
        assert "full continuity" in r.json()["continuity"]
        assert "switch committed" in r.json()["warning"]
        assert "retirement failed" in r.json()["warning"]
        switch.assert_awaited_once_with(
            "cx-wedged",
            "anthropic/claude-3-5-sonnet",
            user_id="u-test",
            actor_kind="agent",
            actor_id="sender-self",
            reason="repair",
            checkpoint_override=None,
        )

    @pytest.mark.parametrize(
        ("override", "choice"),
        [(None, "current"), ("target", "target")],
    )
    def test_switch_required_returns_pending_checkpoint_receipt(
            self, client, deleg_db, monkeypatch, override, choice):
        import api.delegation_model as model_api
        from infra.model_switch_types import SwitchRequest

        self._wedged_codex(deleg_db)
        switch = AsyncMock(return_value=SwitchRequest(
            operation_id="operation-1",
            thread_id="cx-wedged",
            source_model="codex/gpt-5-codex",
            destination_model="anthropic/claude-3-5-sonnet",
            source_execution_epoch=0,
            destination_execution_epoch=1,
            switch_after_input_order=0,
            actor_kind="agent",
            actor_id="sender-self",
            reason="repair",
            checkpoint_choice=choice,
            requested_at="t",
            observed_input_order=0,
            waiting_for_source=True,
        ))
        monkeypatch.setattr(model_api, "switch_model", switch)
        payload = {
            "model": "anthropic/claude-3-5-sonnet",
            "from_thread_id": "sender-self",
            "reason": "repair",
        }
        if override is not None:
            payload["checkpoint_model"] = override

        response = client.post(
            "/api/delegation/threads/cx-wedged/set-model",
            params={"token": _TOKEN},
            json=payload,
        )

        assert response.status_code == 202
        assert response.json() == {
            "accepted": True,
            "checkpoint_model": choice,
            "instruction": (
                "Call delegate_status with operation_id=operation-1 to observe "
                "completion or failure."
            ),
            "operation_id": "operation-1",
            "pending_model": "anthropic/claude-3-5-sonnet",
            "checkpoint_required": False,
            "thread_id": "cx-wedged",
            "status": "waiting_for_turn",
            "switched": False,
            "target_model": "anthropic/claude-3-5-sonnet",
        }
        switch.assert_awaited_once_with(
            "cx-wedged",
            "anthropic/claude-3-5-sonnet",
            user_id="u-test",
            actor_kind="agent",
            actor_id="sender-self",
            reason="repair",
            checkpoint_override=override,
        )

    def test_switch_accepts_only_target_checkpoint_override(
            self, client, deleg_db):
        self._wedged_codex(deleg_db)

        response = client.post(
            "/api/delegation/threads/cx-wedged/set-model",
            params={"token": _TOKEN},
            json={
                "model": "anthropic/claude-3-5-sonnet",
                "from_thread_id": "sender-self",
                "checkpoint_model": "current",
            },
        )

        assert response.status_code == 422
        assert "omitted" in response.json()["detail"]

    def test_switch_conflict_is_409(
            self, client, deleg_db, monkeypatch):
        import api.delegation_model as model_api
        from thread.model_state import ModelSwitchConflict

        self._wedged_codex(deleg_db)
        switch = AsyncMock(
            side_effect=ModelSwitchConflict("history changed during preparation")
        )
        monkeypatch.setattr(model_api, "switch_model", switch)

        r = client.post(
            "/api/delegation/threads/cx-wedged/set-model",
            params={"token": _TOKEN},
            json={
                "model": "anthropic/claude-3-5-sonnet",
                "from_thread_id": "sender-self",
            },
        )

        assert r.status_code == 409, r.text
        assert "history changed" in r.json()["detail"]

    def test_switch_to_native_unwedges_and_audits(
            self, client, deleg_db, monkeypatch):
        self._wedged_codex(deleg_db)
        events = self._stub_switch_effects(monkeypatch)

        # Pre-switch: Codex is still addressable through RC-0 reconstruction.
        from subagents import cc_runtime, codex_runtime
        monkeypatch.setattr(codex_runtime, "is_live", lambda sid: False)
        monkeypatch.setattr(codex_runtime, "has_thread_id",
                            AsyncMock(return_value=False))
        monkeypatch.setattr(codex_runtime, "has_prior_messages",
                            AsyncMock(return_value=True))
        pre = client.get("/api/delegation/threads/cx-wedged/status",
                         params={"token": _TOKEN}).json()
        assert pre["addressable"] is True

        # Explicit switch to the native engine (continuity-preserving target).
        r = client.post("/api/delegation/threads/cx-wedged/set-model",
                        params={"token": _TOKEN},
                        json={"model": "anthropic/claude-3-5-sonnet",
                              "from_thread_id": "sender-self",
                              "reason": "resume-recovery"})
        assert r.status_code == 200
        body = r.json()
        assert body["changed"] is True
        assert body["old_model"] == "codex/gpt-5-codex"
        assert body["new_model"] == "anthropic/claude-3-5-sonnet"
        assert "full continuity" in body["continuity"]
        persisted = deleg_db.execute(
            "SELECT model FROM threads WHERE id = 'cx-wedged'").fetchone()
        assert persisted["model"] == "anthropic/claude-3-5-sonnet"

        # model.switched emitted exactly once for the change.
        assert [(c, a) for (c, a, _k) in events] == [("model", "switched")]

        # In-transcript audit breadcrumb names the actor + the switch.
        crumb = deleg_db.execute(
            "SELECT role, content, metadata FROM messages "
            "WHERE thread_id = 'cx-wedged' AND role = 'system'").fetchone()
        assert crumb is not None
        meta = json.loads(crumb["metadata"])
        assert meta["type"] == "activity_receipt"
        assert meta["event_type"] == "model.switched"
        assert meta["receipt_type"] == "model_switch"
        assert meta["old_model"] == "codex/gpt-5-codex"
        assert meta["new_model"] == "anthropic/claude-3-5-sonnet"
        assert meta["reason"] == "resume-recovery"
        assert meta["actor_thread"] == "sender-self"

        # Post-switch: now addressable on the native runtime.
        post = client.get("/api/delegation/threads/cx-wedged/status",
                          params={"token": _TOKEN}).json()
        assert post["model"] == "anthropic/claude-3-5-sonnet"
        assert post["driver"] == "engine"
        assert post["execution_class"] == "native"
        assert post["addressable"] is True

    def test_switch_from_removed_source_model_still_works(
            self, client, deleg_db, monkeypatch):
        removed_model = "omlx@local/qwen-flash-next"
        self._wedged_codex(deleg_db, "removed-source")
        deleg_db.execute(
            "UPDATE threads SET model = ? WHERE id = 'removed-source'",
            (removed_model,),
        )
        deleg_db.commit()
        self._stub_switch_effects(monkeypatch)

        response = client.post(
            "/api/delegation/threads/removed-source/set-model",
            params={"token": _TOKEN},
            json={
                "model": "anthropic/claude-3-5-sonnet",
                "from_thread_id": "sender-self",
                "reason": "replace removed selection",
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["old_model"] == removed_model
        assert response.json()["new_model"] == "anthropic/claude-3-5-sonnet"
        persisted = deleg_db.execute(
            "SELECT model FROM threads WHERE id = 'removed-source'"
        ).fetchone()
        assert persisted["model"] == "anthropic/claude-3-5-sonnet"

    def test_switch_then_send_routes_native(
            self, client, deleg_db, monkeypatch):
        self._wedged_codex(deleg_db, "cx-route")
        self._stub_switch_effects(monkeypatch)

        r = client.post("/api/delegation/threads/cx-route/set-model",
                        params={"token": _TOKEN},
                        json={"model": "anthropic/claude-3-5-sonnet",
                              "from_thread_id": "sender-self"})
        assert r.status_code == 200

        # A subsequent /send now routes through the native turn engine.
        import asyncio
        import ws.turn as ws_turn
        async def acknowledge_start(
            *_args,
            start_ack=None,
            preadmitted_turn=None,
            persist_input_once=None,
            **_kwargs,
        ):
            if persist_input_once is not None:
                await persist_input_once()
            if start_ack is not None and not start_ack.done():
                start_ack.set_result(preadmitted_turn)

        monkeypatch.setattr(
            ws_turn,
            "execute_turn",
            AsyncMock(side_effect=acknowledge_start),
        )
        original_create_task = asyncio.create_task
        monkeypatch.setattr(
            asyncio, "create_task",
            lambda coro, **k: original_create_task(coro, **k))
        s = client.post("/api/delegation/threads/cx-route/send",
                        params={"token": _TOKEN},
                        json={"message": "continue the design",
                              "from_thread_id": "sender-self"})
        assert s.status_code == 200
        assert s.json()["driver"] == "turn_engine"

    @pytest.mark.parametrize(
        "old_model,new_model",
        [
            ("anthropic/claude-3-5-sonnet", "claude-code/opus"),
            ("anthropic/claude-3-5-sonnet", "codex/gpt-5-codex"),
            ("claude-code/opus", "anthropic/claude-3-5-sonnet"),
            ("claude-code/opus", "codex/gpt-5-codex"),
            ("codex/gpt-5-codex", "anthropic/claude-3-5-sonnet"),
            ("codex/gpt-5-codex", "claude-code/opus"),
        ],
    )
    def test_supported_switch_paths_preserve_exact_history(
            self, client, deleg_db, monkeypatch,
            old_model, new_model):
        sid = f"switch-{abs(hash((old_model, new_model))) % 1_000_000}"
        deleg_db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, title, created_at, updated_at) "
            "VALUES (?, 'u-test', ?, 'Switch Matrix', 'now', 'now')",
            (sid, old_model),
        )
        deleg_db.commit()
        self._stub_switch_effects(monkeypatch)

        r = client.post(
            f"/api/delegation/threads/{sid}/set-model",
            params={"token": _TOKEN},
            json={
                "model": new_model,
                "from_thread_id": "sender-self",
                "reason": "ctx-switch-matrix",
            },
        )

        assert r.status_code == 200
        assert r.json()["changed"] is True
        assert r.json()["old_model"] == old_model
        assert r.json()["new_model"] == new_model
        assert "full continuity" in r.json()["continuity"]
        crumb = deleg_db.execute(
            "SELECT metadata FROM messages WHERE thread_id = ? AND role = 'system'",
            (sid,),
        ).fetchone()
        meta = json.loads(crumb["metadata"])
        assert meta["type"] == "activity_receipt"
        assert meta["event_type"] == "model.switched"
        assert meta["receipt_type"] == "model_switch"
        assert meta["old_model"] == old_model
        assert meta["new_model"] == new_model
        assert meta["continuity_mode"] == "exact"
        assert "full continuity" in meta["continuity"]

    def test_cc_target_reports_exact_portable_history(
            self, client, deleg_db, monkeypatch):
        self._wedged_codex(deleg_db, "cx-cc")
        self._stub_switch_effects(monkeypatch)
        r = client.post("/api/delegation/threads/cx-cc/set-model",
                        params={"token": _TOKEN},
                        json={"model": "claude-code/opus",
                              "from_thread_id": "sender-self"})
        assert r.status_code == 200
        assert "full continuity" in r.json()["continuity"]
        crumb = deleg_db.execute(
            "SELECT metadata FROM messages "
            "WHERE thread_id = 'cx-cc' AND role = 'system'").fetchone()
        metadata = json.loads(crumb["metadata"])
        assert metadata["continuity_mode"] == "exact"
        assert "full continuity" in metadata["continuity"]

    def test_idempotent_same_model_is_noop(self, client, deleg_db, monkeypatch):
        deleg_db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, title, created_at, updated_at) "
            "VALUES ('cx-same', 'u-test', 'anthropic/claude-3-5-sonnet', "
            "'T', 'now', 'now')")
        deleg_db.commit()
        events = self._stub_switch_effects(monkeypatch)
        r = client.post("/api/delegation/threads/cx-same/set-model",
                        params={"token": _TOKEN},
                        json={"model": "anthropic/claude-3-5-sonnet",
                              "from_thread_id": "sender-self"})
        assert r.status_code == 200
        assert r.json()["changed"] is False
        # No breadcrumb, no event on a no-op.
        assert events == []
        crumb = deleg_db.execute(
            "SELECT COUNT(*) AS c FROM messages "
            "WHERE thread_id = 'cx-same'").fetchone()
        assert crumb["c"] == 0
