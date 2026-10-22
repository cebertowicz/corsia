from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from auth_fixtures import OWNER_TOKEN

_TOKEN = OWNER_TOKEN


def test_thread_mutations_private_owned_thread_naming_surface() -> None:
    import api.thread_mutations as thread_mutations

    assert hasattr(thread_mutations, "_owned_thread_exists")
    assert not hasattr(thread_mutations, "_owned_session_exists")


@pytest.mark.integration
class TestSessionContextBoundary:
    def _create_session(self, integration_client) -> str:
        resp = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": f"context boundary {uuid.uuid4().hex[:8]}"},
        )
        assert resp.status_code == 200
        return resp.json()["thread_id"]

    def test_project_scope_patch_retires_harness_context(
        self, integration_client, monkeypatch
    ):
        import api.thread_mutations as sessions_mod

        sid = self._create_session(integration_client)
        project_resp = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": f"context-boundary-{uuid.uuid4().hex[:8]}", "status": "active"},
        )
        assert project_resp.status_code == 200
        project_id = project_resp.json()["project"]["id"]
        calls: list[tuple[str, str | None, str]] = []

        async def _fake_boundary(thread_id: str, *, user_id: str | None, reason: str):
            calls.append((thread_id, user_id, reason))

        monkeypatch.setattr(
            sessions_mod.runtime_boundary,
            "post_context_boundary_effects",
            _fake_boundary,
        )

        resp = integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}",
            json={"project_id": project_id},
        )

        assert resp.status_code == 200
        assert calls == [(sid, "steve", "session_context_changed")]

    def test_project_scope_patch_rejects_unknown_project_id(
        self, integration_client, monkeypatch
    ):
        import api.thread_mutations as sessions_mod

        sid = self._create_session(integration_client)
        calls: list[tuple[str, str | None, str]] = []

        async def _fake_boundary(thread_id: str, *, user_id: str | None, reason: str):
            calls.append((thread_id, user_id, reason))

        monkeypatch.setattr(
            sessions_mod.runtime_boundary,
            "post_context_boundary_effects",
            _fake_boundary,
        )

        resp = integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}",
            json={"project_id": "missing-project"},
        )

        assert resp.status_code == 400
        assert "Project not found" in resp.text
        assert calls == []

    def test_model_patch_uses_the_canonical_prepared_switch(
        self, integration_client, monkeypatch
    ):
        import api.thread_mutations as sessions_mod

        sid = self._create_session(integration_client)
        from thread.model_state import ModelSwitchOutcome

        # A REAL outcome, because the endpoint distinguishes a committed switch
        # from an accepted background operation by type. A stand-in that merely
        # looks like one would pass this cell while the endpoint took the other
        # branch in production.
        switch = AsyncMock(
            return_value=ModelSwitchOutcome(
                changed=True,
                old_model="anthropic/large",
                new_model="anthropic/claude-3-5-sonnet",
                preparation=None,
                breadcrumb=None,
                committed_at="2026-07-17T08:00:00.000000",
                post_effects_error="retirement failed",
            )
        )
        monkeypatch.setattr(sessions_mod, "switch_model", switch, raising=False)
        monkeypatch.setattr(
            sessions_mod,
            "is_dispatchable_model",
            lambda model: model == "anthropic/claude-3-5-sonnet",
            raising=False,
        )

        resp = integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}",
            json={"model": "anthropic/claude-3-5-sonnet"},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["model"] == "anthropic/claude-3-5-sonnet"
        assert resp.json()["model_updated_at"] == "2026-07-17T08:00:00.000000"
        assert "switch committed" in resp.json()["warning"]
        assert "retirement failed" in resp.json()["warning"]
        switch.assert_awaited_once_with(
            sid,
            "anthropic/claude-3-5-sonnet",
            user_id="steve",
            actor_kind="user",
            actor_id="steve",
            reason="harness_model_switch",
            checkpoint_override=None,
        )

    def test_model_patch_rejects_mixed_mutation_before_any_write(
        self, integration_client, monkeypatch
    ):
        import api.thread_mutations as sessions_mod
        from infra.database import get_db

        sid = self._create_session(integration_client)
        db = get_db()
        try:
            before = dict(
                db.execute(
                    "SELECT title, model FROM threads WHERE id = ?",
                    (sid,),
                ).fetchone()
            )
        finally:
            db.close()
        switch = AsyncMock()
        monkeypatch.setattr(sessions_mod, "switch_model", switch, raising=False)

        resp = integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}",
            json={
                "model": "anthropic/claude-3-5-sonnet",
                "title": "must not be written",
            },
        )

        assert resp.status_code == 400, resp.text
        assert "model" in resp.json()["detail"].lower()
        switch.assert_not_awaited()
        db = get_db()
        try:
            after = dict(
                db.execute(
                    "SELECT title, model FROM threads WHERE id = ?",
                    (sid,),
                ).fetchone()
            )
        finally:
            db.close()
        assert after == before

    def test_model_patch_reports_preparation_conflict_without_mutating(
        self, integration_client, monkeypatch
    ):
        import api.thread_mutations as sessions_mod
        from thread.model_state import ModelSwitchConflict

        sid = self._create_session(integration_client)
        switch = AsyncMock(side_effect=ModelSwitchConflict("history changed"))
        monkeypatch.setattr(sessions_mod, "switch_model", switch, raising=False)
        monkeypatch.setattr(
            sessions_mod,
            "is_dispatchable_model",
            lambda _model: True,
            raising=False,
        )

        resp = integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}",
            json={"model": "anthropic/claude-3-5-sonnet"},
        )

        assert resp.status_code == 409, resp.text
        assert "history changed" in resp.json()["detail"]

    def test_legacy_context_compaction_write_endpoint_is_removed(
        self,
        integration_client,
    ):
        sid = self._create_session(integration_client)

        resp = integration_client.post(
            f"/api/threads/{sid}/context-compaction?token={_TOKEN}",
            json={
                "trigger_reason": "plan_topic_boundary",
                "detail": "plan completed; start next topic",
            },
        )

        assert resp.status_code == 405, resp.text
