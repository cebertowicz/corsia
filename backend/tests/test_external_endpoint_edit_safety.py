"""An edited External endpoint never sends its saved key elsewhere, and its
discovery status never reports a snapshot the edit has already replaced."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from infra.key_store_servers import resolve_server, set_server_key
from key_store_fixtures import fail_the_commit_after_a_key_write, values_after_a_sweep
from omlx_provider_fixtures import _patch_httpx, _resp, _Router

_OLD = "https://old.example.com/v1"
_CAPS = {"context_window": None, "supports_vision": 0,
         "supports_tools": 1, "supports_reasoning": 0}


@pytest.fixture
def edit(isolated_db, monkeypatch):
    from api import network_server_helpers as helpers
    from infra import product_events
    from infra.key_store_servers import set_server_key
    from llm import network_discovery as nd

    sid = nd.upsert_server(
        provider="external", instance_id="old.example.com:443/v1", instance_name="External",
        host="old.example.com", port=443, status="online", kind="external", dialect="openai",
    )
    nd.set_server_metadata(sid, base_url=_OLD)
    set_server_key(sid, "stored-key")
    real_probe = helpers._probe_external
    probe = AsyncMock(return_value=[{"id": "m"}])
    events: list = []
    monkeypatch.setattr(helpers, "_probe_external", probe)
    monkeypatch.setattr(nd, "discover_and_notify", AsyncMock())
    monkeypatch.setattr(
        product_events, "emit_product_event",
        lambda feature, action, **kw: events.append((action, kw.get("properties"))),
    )
    return SimpleNamespace(sid=sid, helpers=helpers, nd=nd, probe=probe,
                           real_probe=real_probe, events=events)


def _update(ctx, base: str, key: str | None = None, dialect: str = "openai") -> dict:
    from api.network_servers import UpdateExternalServerBody

    body = UpdateExternalServerBody(base_url=base, dialect=dialect, api_key=key)
    return asyncio.run(ctx.helpers.update_external_server(ctx.sid, body))


def _unchanged(ctx) -> None:
    server = ctx.nd.get_server(ctx.sid)
    assert server["instance_id"] == "old.example.com:443/v1"
    assert ctx.nd.get_server_metadata(ctx.sid) == {"base_url": _OLD}
    assert resolve_server(ctx.sid) == "stored-key"


# ── the saved key stays with the origin it was saved for ────────────────────

@pytest.mark.parametrize("base", [
    "https://elsewhere.example.net/v1",   # another host
    "http://old.example.com/v1",          # same host, plaintext
    "https://old.example.com:8443/v1",    # same host, another port
])
@pytest.mark.parametrize("key", [None, "   "])
def test_moving_with_a_blank_key_is_refused_before_any_request(edit, base, key) -> None:
    with pytest.raises(HTTPException) as exc:
        _update(edit, base, key)

    assert exc.value.status_code == 400
    assert "Enter the API key for the new host" in exc.value.detail
    edit.probe.assert_not_called()
    _unchanged(edit)
    assert edit.events == [
        ("edit.failed", {"dialect": "openai", "reason": "key_required_for_new_host"}),
    ]


def test_moving_with_a_new_key_sends_and_saves_only_the_new_key(edit) -> None:
    result = _update(edit, "https://elsewhere.example.net/v1", "new-key")

    assert result["models"] == 1
    assert edit.probe.call_args.args[2] == {"Authorization": "Bearer new-key"}
    assert resolve_server(edit.sid) == "new-key"
    assert edit.nd.get_server(edit.sid)["instance_id"] == "elsewhere.example.net:443/v1"
    assert edit.events == [("edited", {
        "dialect": "openai", "models": 1, "moved": True, "key_replaced": True,
    })]


def test_a_failed_base_write_puts_the_old_key_back_and_keeps_the_old_base(edit, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("metadata write failed")

    with monkeypatch.context() as patched:
        patched.setattr(edit.nd, "set_server_metadata", broken)
        with pytest.raises(RuntimeError, match="metadata write failed"):
            _update(edit, "https://elsewhere.example.net/v1", "new-key")

    _unchanged(edit)


def test_a_failed_commit_puts_the_old_key_back(edit, monkeypatch) -> None:
    real = edit.helpers.write_transaction

    @contextlib.contextmanager
    def failing_commit(*args, **kwargs):
        with real(*args, **kwargs) as db:
            yield db
            raise sqlite3.OperationalError("commit failed")

    with monkeypatch.context() as patched:
        patched.setattr(edit.helpers, "write_transaction", failing_commit)
        with pytest.raises(sqlite3.OperationalError):
            _update(edit, "https://elsewhere.example.net/v1", "new-key")

    assert resolve_server(edit.sid) == "stored-key"


def test_a_refused_commit_keeps_the_old_key_and_base_and_a_sweep_removes_the_new_one(edit, monkeypatch) -> None:
    with monkeypatch.context() as patched:
        fail_the_commit_after_a_key_write(patched)
        with pytest.raises(RuntimeError, match="commit failed"):
            _update(edit, "https://elsewhere.example.net/v1", "new-key")

    _unchanged(edit)
    assert list(values_after_a_sweep().values()) == ["stored-key"]


def test_a_same_origin_edit_keeps_the_saved_key_and_is_measured(edit) -> None:
    _update(edit, "https://old.example.com/v2")

    assert edit.probe.call_args.args[2] == {"Authorization": "Bearer stored-key"}
    assert resolve_server(edit.sid) == "stored-key"
    assert edit.events == [("edited", {
        "dialect": "openai", "models": 1, "moved": False, "key_replaced": False,
    })]


def test_a_failed_edit_probe_counts_as_an_edit_not_a_registration(edit, monkeypatch) -> None:
    monkeypatch.setattr(edit.helpers, "_probe_external", edit.real_probe)
    _patch_httpx(monkeypatch, _Router({("GET", "/v2/models"): _resp(401)}))

    with pytest.raises(HTTPException, match="401"):
        _update(edit, "https://old.example.com/v2")

    _unchanged(edit)
    assert edit.events == [("edit.failed", {"dialect": "openai", "reason": "http_401"})]


def test_an_edit_to_a_url_another_endpoint_uses_is_refused_unchanged(edit) -> None:
    edit.nd.upsert_server(
        provider="external", instance_id="other.example.com:443/v1", instance_name="Other",
        host="other.example.com", port=443, status="online", kind="external", dialect="openai",
    )

    with pytest.raises(HTTPException, match="already uses this URL"):
        _update(edit, "https://other.example.com/v1", "new-key")

    _unchanged(edit)


def test_only_an_existing_external_server_can_be_edited_here(edit) -> None:
    omlx = edit.nd.upsert_server(
        provider="omlx", instance_id="lan:8000", instance_name="oMLX",
        host="lan", port=8000, status="online",
    )
    for sid, status in ((omlx, 400), ("ns-missing", 404)):
        edit.sid = sid
        with pytest.raises(HTTPException) as exc:
            _update(edit, _OLD)
        assert exc.value.status_code == status
    edit.probe.assert_not_called()


# ── discovery reports what actually happened ────────────────────────────────

def _discover(ctx, monkeypatch, *, during_fetch=None, fail=None):
    from llm import external_provider as ep

    server = ctx.nd.get_server(ctx.sid)
    monkeypatch.setattr(ep, "_external_discovery_config",
                        lambda _sid: (server, _OLD, "stored-key", _CAPS))

    async def fetch(*_args):
        if during_fetch:
            during_fetch()
        if fail:
            raise fail
        return ["model-a"]

    monkeypatch.setattr(ep, "fetch_external_models", fetch)
    return asyncio.run(ep.discover_external_models(ctx.sid))


def _catalog(ctx) -> list:
    from infra.database import get_db
    from llm.external_provider import served_model_id

    return [served_model_id(r[0]) for r in get_db().execute(
        "SELECT litellm_model FROM models WHERE litellm_model LIKE ?",
        (f"external@{ctx.sid}/%",),
    ).fetchall()]


def test_an_unchanged_endpoint_reconciles_and_records_its_models(edit, monkeypatch) -> None:
    result = _discover(edit, monkeypatch)

    assert result["ok"] is True and result["discovered"] == 1
    assert _catalog(edit) == ["model-a"]
    assert edit.nd.get_server_metadata(edit.sid)["discovery"]["models"] == 1


@pytest.mark.parametrize("change", [
    lambda ctx: ctx.nd.set_server_metadata(ctx.sid, base_url="https://old.example.com/v2"),
    lambda ctx: set_server_key(ctx.sid, "new-key"),
])
def test_a_discovery_that_read_the_old_endpoint_cannot_overwrite_an_edit(
    edit, monkeypatch, change,
) -> None:
    result = _discover(edit, monkeypatch, during_fetch=lambda: change(edit))

    assert result["ok"] is False
    assert result["error"] == "Superseded by an endpoint edit"
    assert _catalog(edit) == []
    assert "discovery" not in edit.nd.get_server_metadata(edit.sid)


def test_a_discovery_db_failure_still_records_its_error(edit, monkeypatch) -> None:
    from llm import external_provider as ep

    def broken(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ep, "_reconcile_external_models", broken)
    result = _discover(edit, monkeypatch)

    assert result["ok"] is False
    recorded = edit.nd.get_server_metadata(edit.sid)["discovery"]
    assert recorded["error"] == "database is locked"
    assert recorded["models"] == 0


# ── a failure from before an edit cannot overwrite the edit's status ────────

def _move(ctx):
    return lambda: ctx.nd.set_server_metadata(ctx.sid, base_url="https://old.example.com/v2")


def test_a_failed_fetch_from_before_an_edit_cannot_overwrite_its_status(
    edit, monkeypatch,
) -> None:
    result = _discover(edit, monkeypatch, during_fetch=_move(edit),
                       fail=ConnectionError("connection refused"))

    assert result["ok"] is False
    assert result["error"] == "Superseded by an endpoint edit"
    assert "discovery" not in edit.nd.get_server_metadata(edit.sid)


def test_a_failed_fetch_on_an_unchanged_endpoint_records_its_error(edit, monkeypatch) -> None:
    result = _discover(edit, monkeypatch, fail=ConnectionError("connection refused"))

    assert result["ok"] is False
    recorded = edit.nd.get_server_metadata(edit.sid)["discovery"]
    assert recorded["error"] == "Connection failed: connection refused"


def test_a_db_failure_from_before_an_edit_cannot_overwrite_its_status(
    edit, monkeypatch,
) -> None:
    from llm import external_provider as ep

    def broken(*_args):
        _move(edit)()
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ep, "_reconcile_external_models", broken)
    result = _discover(edit, monkeypatch)

    assert result["error"] == "Superseded by an endpoint edit"
    assert "discovery" not in edit.nd.get_server_metadata(edit.sid)


# ── the stale check reads the stored base the way dispatch does ─────────────

def test_a_legacy_host_port_row_matches_the_base_it_resolves_to(isolated_db) -> None:
    from infra.database import get_db
    from llm import network_discovery as nd
    from llm.external_discovery_result import superseded_by_edit

    sid = nd.upsert_server(
        provider="external", instance_id="192.168.1.20:8000", instance_name="LAN",
        host="192.168.1.20", port=8000, status="online", kind="external", dialect="openai",
    )
    base = "http://192.168.1.20:8000/v1"
    result: dict = {}

    assert superseded_by_edit(get_db(), sid, ("openai", base, None), result) is False
    assert superseded_by_edit(get_db(), sid, ("openai", "http://192.168.1.20:9000/v1", None),
                              result) is True


def test_an_unreadable_stored_base_supersedes_rather_than_overwrites(edit) -> None:
    from infra.database import get_db
    from llm.external_discovery_result import superseded_by_edit

    db = get_db()
    db.execute("UPDATE network_servers SET metadata_json = '{not json' WHERE id = ?", (edit.sid,))
    db.commit()

    assert superseded_by_edit(db, edit.sid, ("openai", _OLD, "stored-key"), {}) is True
