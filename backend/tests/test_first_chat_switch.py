"""Setup's first chat moves to Claude Code or Codex when a new sign-in, from setup or Settings, is ready.

Three guards keep it narrow: only the first chat setup created, only if the person has not
picked a model in it since, and only while no first-chat switch is in flight.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent
from onboarding import first_chat_switch as hook

LOCAL = "omlx@srv/Qwen-Test"
CLOUD = "claude-code/claude-test-default"
CLOUD_READY = "claude-code/claude-test-ready"


def _make_agent(model: str | None) -> str:
    """The CORSIA agent, created with ``model`` (None: it has no model yet), with a local and a Claude Code model listed."""
    from connections.models import upsert_connection
    from infra.database import get_db

    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/test"})
    db = get_db()
    try:
        db.execute("UPDATE models SET enabled=0 WHERE harness='claude-code'")
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,backend,source,"
            "enabled,available,context_window,metadata_json,created_at,updated_at) VALUES "
            "('vmlx-qwen-test','native','omlx','Qwen-Test',?,'Qwen','network','discovered',1,1,200000,'{}','t','t'),"
            "(?,'claude-code','claude-code','claude-test-default',?,'Default','harness','discovered',1,1,200000,"
            "'{\"is_default\": true, \"discovery_order\": 0}','t','t')",
            (LOCAL, CLOUD, CLOUD),
        )
        thread_id = ensure_corsia_agent("u1", model=model, db=db)
        db.commit()
    finally:
        db.close()
    assert get_corsia_agent() == thread_id
    return thread_id


@pytest.fixture
def agent(isolated_db):
    """The CORSIA agent, holding the local model setup gave it."""
    return _make_agent(LOCAL)


@pytest.fixture
def modelless_agent(isolated_db, monkeypatch):
    """The CORSIA agent before anyone has chosen a model for it, with the live switch door's side effects stubbed."""
    import infra.thread_execution as execution

    monkeypatch.setattr(execution, "drive_thread", AsyncMock())
    monkeypatch.setattr("infra.state.broadcast_to_thread", Mock())
    return _make_agent(None)


def _account_id(provider: str = "claude-code") -> str:
    from connections.vendor_accounts import default_account

    return str(default_account(provider)["id"])


def _run_claim(operation: str, provider: str = "claude-code"):
    from infra.database import get_db

    db = get_db()
    try:
        return hook.claim_switch(provider, operation, account_id=_account_id(), db=db)
    finally:
        db.close()


def _thread(thread_id: str, **fields):
    from infra.database import get_db

    db = get_db()
    try:
        for column, value in fields.items():
            db.execute(f"UPDATE threads SET {column}=? WHERE id=?", (value, thread_id))
        db.commit()
    finally:
        db.close()


class TestRecordedFirstModel:
    def test_the_local_model_setup_gave_the_agent_is_recorded(self, agent):
        hook.record_first_model(agent, LOCAL)
        from infra.settings import get_setting

        assert get_setting(hook.SETTINGS_KEY)["model"] == LOCAL

    def test_a_cloud_first_model_needs_no_switch_and_is_not_recorded(self, agent):
        hook.record_first_model(agent, CLOUD)
        from infra.settings import get_setting

        assert get_setting(hook.SETTINGS_KEY) is None

    def test_a_thread_that_is_not_the_corsia_agent_is_not_recorded(self, agent):
        from infra.database import get_db

        db = get_db()
        try:
            db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES ('other','u',?, 't0','t0')", (LOCAL,))
            db.commit()
        finally:
            db.close()
        hook.record_first_model("other", LOCAL)
        from infra.settings import get_setting

        assert get_setting(hook.SETTINGS_KEY) is None


class TestGuards:
    def test_the_first_chat_switches_to_the_tool_that_signed_in(self, agent):
        hook.record_first_model(agent, LOCAL)
        assert _run_claim("account-1:1") == (agent, CLOUD)

    def test_guard_1_no_recorded_first_chat_means_no_switch(self, agent):
        assert _run_claim("account-1:1") is None

    def test_guard_2_a_model_the_person_picked_since_stops_it(self, agent):
        hook.record_first_model(agent, LOCAL)
        _thread(agent, model="opus", execution_epoch=1)
        assert _run_claim("account-1:1") is None

    def test_guard_2_a_new_epoch_on_the_same_model_stops_it(self, agent):
        hook.record_first_model(agent, LOCAL)
        _thread(agent, execution_epoch=1)
        assert _run_claim("account-1:1") is None

    def test_guard_2_a_pending_switch_stops_it(self, agent):
        hook.record_first_model(agent, LOCAL)
        _thread(agent, pending_model="opus")
        assert _run_claim("account-1:1") is None

    def test_guard_3_a_second_claim_is_refused_while_the_first_is_in_flight(self, agent):
        hook.record_first_model(agent, LOCAL)
        assert _run_claim("account-1:1") == (agent, CLOUD)
        assert _run_claim("account-2:1") is None

    def test_no_listed_model_for_the_tool_means_no_switch_and_nothing_claimed(self, agent):
        hook.record_first_model(agent, LOCAL)
        assert _run_claim("account-1:1", "codex-none") is None
        assert _run_claim("account-1:1") == (agent, CLOUD)

    def test_a_usage_limited_default_is_skipped_for_a_ready_sibling(self, agent):
        from connections.account_receipts import capture_receipt
        from connections import harness_usage
        from infra.database import get_db

        db = get_db()
        try:
            db.execute(
                "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,backend,source,"
                "enabled,available,context_window,metadata_json,created_at,updated_at) VALUES "
                "('claude-ready','claude-code','claude-code','claude-test-ready',?,'Ready','harness','discovered',1,1,200000,"
                "'{\"discovery_order\": 1}','t','t')",
                (CLOUD_READY,),
            )
            db.commit()
            harness_usage.record_limited(
                provider="claude-code", scope_model=CLOUD, reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00",
                source="test", receipt=capture_receipt("claude-code", account_id=_account_id(), db=db), db=db,
            )
        finally:
            db.close()

        hook.record_first_model(agent, LOCAL)
        assert _run_claim("account-1:1") == (agent, CLOUD_READY)

    def test_an_account_without_a_ready_model_is_not_claimed(self, agent):
        from connections.account_receipts import capture_receipt
        from connections import harness_usage
        from infra.database import get_db
        from infra.settings import get_setting

        db = get_db()
        try:
            harness_usage.record_limited(
                provider="claude-code", scope_model=CLOUD, reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00",
                source="test", receipt=capture_receipt("claude-code", account_id=_account_id(), db=db), db=db,
            )
            hook.record_first_model(agent, LOCAL)
            assert hook.claim_switch("claude-code", "account-1:1", account_id=_account_id(), db=db) is None
            assert get_setting(hook.SETTINGS_KEY, db=db)["claimed"] is None
        finally:
            db.close()


class TestSwitch:
    async def test_the_switch_goes_through_the_one_door_with_the_target_writing_the_handoff(self, agent, monkeypatch):
        hook.record_first_model(agent, LOCAL)
        door = AsyncMock()
        monkeypatch.setattr("thread.model.switch_model", door)

        asked = await hook.switch_when_ready("claude-code", "account-1:1", user_id="u1", account_id=_account_id())

        assert asked is True
        door.assert_awaited_once()
        assert door.await_args.args == (agent, CLOUD)
        assert door.await_args.kwargs["actor_kind"] == "system"
        assert door.await_args.kwargs["reason"] == "setup_first_chat_ready"
        assert door.await_args.kwargs["checkpoint_override"] == "target"
        assert door.await_args.kwargs["account_id"] == _account_id()

    async def test_a_settings_sign_in_switches_with_no_user(self, agent, monkeypatch):
        hook.record_first_model(agent, LOCAL)
        door = AsyncMock()
        monkeypatch.setattr("thread.model.switch_model", door)

        asked = await hook.switch_when_ready("claude-code", "account-1:1", user_id=None, account_id=_account_id())

        assert asked is True
        assert door.await_args.kwargs["user_id"] is None

    async def test_a_later_completion_after_success_is_a_no_op(self, agent, monkeypatch):
        hook.record_first_model(agent, LOCAL)
        door = AsyncMock()
        monkeypatch.setattr("thread.model.switch_model", door)

        assert await hook.switch_when_ready("claude-code", "account-1:1", user_id="u1", account_id=_account_id()) is True
        _thread(agent, model=CLOUD, execution_epoch=1)
        again = await hook.switch_when_ready("claude-code", "account-2:1", user_id="u1", account_id=_account_id())

        assert again is False
        door.assert_awaited_once()

    async def test_a_refused_switch_leaves_the_chat_where_it_is(self, agent, monkeypatch):
        hook.record_first_model(agent, LOCAL)
        monkeypatch.setattr("thread.model.switch_model", AsyncMock(side_effect=RuntimeError("refused")))

        assert await hook.switch_when_ready("claude-code", "account-1:1", user_id="u1", account_id=_account_id()) is False

    async def test_a_failed_switch_releases_the_claim_for_a_different_completion(self, agent, monkeypatch):
        hook.record_first_model(agent, LOCAL)
        monkeypatch.setattr("thread.model.switch_model", AsyncMock(side_effect=RuntimeError("refused")))
        assert await hook.switch_when_ready("claude-code", "account-1:1", user_id="u1", account_id=_account_id()) is False

        door = AsyncMock()
        monkeypatch.setattr("thread.model.switch_model", door)
        retried = await hook.switch_when_ready("claude-code", "account-2:1", user_id="u1", account_id=_account_id())

        assert retried is True
        door.assert_awaited_once()


class TestSigninSeam:
    """finish_signin asks for the switch only when a new login's own read came back ready, bound or not."""

    @staticmethod
    def _operation(**fields):
        from types import SimpleNamespace

        from connections.corsia_agent_account import FirstRunBind

        return SimpleNamespace(provider="claude-code", duplicate_of=None, account_id="a1", revision=3,
                               bind=FirstRunBind("thread", "u1"), **fields)

    async def _finish(self, monkeypatch, refresh: str, operation) -> AsyncMock:
        from connections import corsia_agent_account as account

        monkeypatch.setattr(account, "_refresh", AsyncMock(return_value=(refresh, None)))
        monkeypatch.setattr(account, "signin_outcome", lambda *args, **kwargs: ("bound", None))
        monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)
        asked = AsyncMock(return_value=True)
        monkeypatch.setattr(hook, "switch_when_ready", asked)
        await account.finish_signin(operation, object(), "claude")
        return asked

    async def test_a_ready_read_asks_once_for_this_operation(self, agent, monkeypatch):
        asked = await self._finish(monkeypatch, "ready", self._operation())
        asked.assert_awaited_once_with("claude-code", "a1:3", user_id="u1", account_id="a1")

    async def test_a_failed_read_does_not_ask(self, agent, monkeypatch):
        asked = await self._finish(monkeypatch, "failed", self._operation())
        asked.assert_not_awaited()

    async def test_a_settings_sign_in_without_a_bind_asks_with_no_user(self, agent, monkeypatch):
        operation = self._operation()
        operation.bind = None
        asked = await self._finish(monkeypatch, "ready", operation)
        asked.assert_awaited_once_with("claude-code", "a1:3", user_id=None, account_id="a1")

    async def test_a_sign_in_that_reads_nothing_does_not_ask(self, agent, monkeypatch):
        operation = self._operation()
        operation.duplicate_of = "other"
        asked = await self._finish(monkeypatch, "ready", operation)
        asked.assert_not_awaited()


class TestOnlySetupsOwnFirstModelIsRecorded:
    """Guard 2 starts here: a model the person chose is theirs, so CF7 never moves them off it."""

    @staticmethod
    def _marker():
        from infra.database import get_db
        from infra.settings import get_setting

        db = get_db()
        try:
            return get_setting(hook.SETTINGS_KEY, db=db)
        finally:
            db.close()

    @staticmethod
    async def _pick(thread_id: str, **kwargs):
        from thread.model import switch_model

        return await switch_model(
            thread_id, LOCAL, user_id="u1", actor_kind="user", actor_id="u1", reason="harness_model_switch", **kwargs,
        )

    async def test_a_person_picking_a_local_model_first_in_the_chat_is_not_moved_when_sign_in_is_ready(
        self, modelless_agent, monkeypatch,
    ):
        outcome = await self._pick(modelless_agent)
        assert outcome.changed is True
        assert self._marker() is None
        door = AsyncMock()
        monkeypatch.setattr("thread.model.switch_model", door)

        asked = await hook.switch_when_ready("claude-code", "account-1:1", user_id="u1", account_id=_account_id())

        assert asked is False
        door.assert_not_awaited()

    async def test_setups_own_first_model_write_is_recorded_and_then_moves(self, modelless_agent, monkeypatch):
        await self._pick(modelless_agent, setup_selection=True)
        assert self._marker()["model"] == LOCAL
        door = AsyncMock()
        monkeypatch.setattr("thread.model.switch_model", door)

        asked = await hook.switch_when_ready("claude-code", "account-1:1", user_id="u1", account_id=_account_id())

        assert asked is True
        assert door.await_args.args == (modelless_agent, CLOUD)

    async def test_a_chat_pick_after_setups_write_does_not_refresh_the_marker(self, modelless_agent):
        await self._pick(modelless_agent, setup_selection=True)
        before = self._marker()
        # The thread already has a model, so this is an ordinary switch, not the first selection.
        await self._pick(modelless_agent, checkpoint_override="target")
        assert self._marker() == before


class TestThreadPatchSource:
    """The model PATCH carries setup's source; nothing else may claim it."""

    @staticmethod
    def _patched(monkeypatch):
        from api import thread_mutations
        from thread.model import ModelSwitchOutcome

        monkeypatch.setattr("connections.harness_refresh.prepare_selection", AsyncMock())
        monkeypatch.setattr(thread_mutations, "is_dispatchable_model", lambda _model: True)
        monkeypatch.setattr(thread_mutations, "_owned_thread_exists", lambda *_args: True)
        door = AsyncMock(return_value=ModelSwitchOutcome(
            changed=True, old_model=None, new_model=LOCAL, preparation=None, breadcrumb=None, committed_at="t"))
        monkeypatch.setattr(thread_mutations, "switch_model", door)
        return thread_mutations, door

    async def test_setups_source_reaches_the_switch_door(self, monkeypatch):
        mutations, door = self._patched(monkeypatch)
        await mutations._patch_model("t1", "u1", {"model": LOCAL, "source": "setup"})
        assert door.await_args.kwargs["setup_selection"] is True

    async def test_a_patch_without_a_source_is_the_persons_own_pick(self, monkeypatch):
        mutations, door = self._patched(monkeypatch)
        await mutations._patch_model("t1", "u1", {"model": LOCAL})
        assert door.await_args.kwargs["setup_selection"] is False

    async def test_any_other_source_is_refused(self, monkeypatch):
        from fastapi import HTTPException

        mutations, door = self._patched(monkeypatch)
        with pytest.raises(HTTPException) as refused:
            await mutations._patch_model("t1", "u1", {"model": LOCAL, "source": "chat"})
        assert refused.value.status_code == 422
        door.assert_not_awaited()
