from __future__ import annotations

import json

import pytest

from delegation_fixtures import deleg_db  # noqa: F401


def _row(model_id: str, status: str, **overrides) -> dict:
    row = {
        "id": model_id,
        "selector": model_id,
        "status": status,
        "usage": {"windows": [], "source": None, "observed_at": None, "stale": False},
    }
    row.update(overrides)
    return row


def test_scoped_delegation_keeps_the_api_label_after_an_earlier_removal(monkeypatch):
    import connections.vendor_accounts as accounts
    from llm.delegation_model import _account_label

    account = {
        "id": "default", "provider": "codex", "name": "Default", "source_home": "/home/default",
        "state": "signed_in", "is_default": True,
        "identity": {"plan": "Plus"},
    }
    calls = []
    monkeypatch.setattr(accounts, "get_account", lambda _id: account)
    monkeypatch.setattr(accounts, "list_accounts", lambda **kwargs: calls.append(kwargs) or [
        {"id": "removed", "provider": "codex", "state": "removed"}, account,
    ])

    assert _account_label("default") == "ChatGPT Plus"
    assert calls == [{"provider": "codex", "include_removed": True, "db": None}]


def test_scoped_refusal_names_the_canonical_label_and_account_id(deleg_db, monkeypatch):
    import llm.delegation_model as delegation_model

    monkeypatch.setattr(delegation_model, "_account_label", lambda account_id: "Personal")
    monkeypatch.setattr(delegation_model, "_scoped_rows", lambda *args: ([{
        "id": "codex/gpt-5-codex", "connection": "codex", "status": "ready",
        "runs_on": [{"account_id": "another-account", "selected": True, "status": "ready", "status_reason": None}],
    }], None))

    selected, refusal = delegation_model.eligible_delegation_model("codex/gpt-5-codex", account_id="account-42")

    assert selected is None
    assert "on account 'Personal' (account-42)" in refusal


def test_ready_selector_passes_through_as_canonical_id(deleg_db, monkeypatch):
    import llm.model_status as status
    from llm.delegation_model import eligible_delegation_model

    monkeypatch.setattr(status, "get_model_status_rows", lambda: [
        _row("anthropic/claude-3-5-sonnet", "ready"),
    ])

    selected, refusal = eligible_delegation_model("anthropic/claude-3-5-sonnet")

    assert (selected, refusal) == ("anthropic/claude-3-5-sonnet", None)


def test_unknown_usage_stays_eligible_but_limited_usage_refuses(deleg_db, monkeypatch):
    import llm.model_status as status
    from llm.delegation_model import eligible_delegation_model

    monkeypatch.setattr(status, "get_model_status_rows", lambda: [
        _row("anthropic/claude-3-5-sonnet", "ready", usage={"state": "unknown", "usable": True, "windows": []}),
        _row("anthropic/claude-3-5-opus", "unavailable", status_reason="usage_limited",
             usage={"state": "limited", "usable": False, "windows": []}),
    ])

    assert eligible_delegation_model("anthropic/claude-3-5-sonnet") == ("anthropic/claude-3-5-sonnet", None)
    selected, refusal = eligible_delegation_model("anthropic/claude-3-5-opus")
    assert selected is None
    assert "usage_limited" in refusal


def test_alias_selects_its_canonical_catalog_id(deleg_db, monkeypatch):
    import llm.model_status as status
    from llm.delegation_model import eligible_delegation_model

    deleg_db.execute(
        "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,"
        "backend,source,enabled,available,context_window,metadata_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("claude-code/sonnet-alias", "claude-code", "claude-code", "sonnet-alias",
         "claude-code/sonnet-alias", "Sonnet alias", "cc", "test", 1, 1, 200000,
         '{"alias_of":"claude-code/sonnet"}', "t", "t"),
    )
    deleg_db.commit()
    from vendor_account_fixtures import signed_in_default

    signed_in_default("claude-code", db=deleg_db)  # the Default lists the alias
    monkeypatch.setattr(status, "get_model_status_rows", lambda: [
        _row("claude-code/sonnet", "ready"),
    ])

    selected, refusal = eligible_delegation_model("claude-code/sonnet-alias")

    assert (selected, refusal) == ("claude-code/sonnet", None)


@pytest.mark.parametrize("status_name, reason", [
    ("unavailable", "usage_limited"),
    ("available", "available"),
])
def test_non_ready_selector_refuses_with_ready_usage(deleg_db, monkeypatch, status_name, reason):
    import llm.model_status as status
    from llm.delegation_model import eligible_delegation_model

    monkeypatch.setattr(status, "get_model_status_rows", lambda: [
        _row("anthropic/claude-3-5-sonnet", status_name, status_reason=reason),
        _row("anthropic/claude-3-5-opus", "ready", usage={
            "windows": [{"kind": "five_hour", "used_percent": 42}],
            "source": "reported", "observed_at": "now", "stale": False,
        }),
    ])

    selected, refusal = eligible_delegation_model("anthropic/claude-3-5-sonnet")

    assert selected is None
    assert reason in refusal
    assert 'anthropic/claude-3-5-opus (usage {"observed_at": "now", "source": "reported", "stale": false, "windows": [{"kind": "five_hour", "used_percent": 42}]})' in refusal


def test_hidden_and_unknown_selectors_refuse_distinctly(deleg_db, monkeypatch):
    import llm.model_status as status
    from llm.delegation_model import eligible_delegation_model

    monkeypatch.setattr(status, "get_model_status_rows", lambda: [
        _row("anthropic/claude-3-5-opus", "ready"),
    ])
    deleg_db.execute("UPDATE models SET enabled = 0 WHERE id = ?", ("anthropic/claude-3-5-sonnet",))
    deleg_db.commit()

    _, hidden = eligible_delegation_model("anthropic/claude-3-5-sonnet")
    _, unknown = eligible_delegation_model("does-not-exist")

    assert "hidden" in hidden
    assert "do not retry it" in hidden
    assert "unknown" in unknown


@pytest.mark.asyncio
async def test_all_delegation_tools_refuse_limited_selector(deleg_db, monkeypatch):
    import llm.model_status as status
    from tools.delegate_task import DelegateTaskTool
    from tools.delegation import DelegateCreateThreadTool
    from tools.delegation_model import DelegateSetModelTool

    monkeypatch.setattr(status, "get_model_status_rows", lambda: [
        _row("anthropic/claude-3-5-sonnet", "unavailable", status_reason="usage_limited"),
        _row("anthropic/claude-3-5-opus", "ready"),
    ])

    create = await DelegateCreateThreadTool().execute(
        model="anthropic/claude-3-5-sonnet", _thread_id="sender-self",
    )
    task = await DelegateTaskTool().execute(
        task="do not dispatch", model="anthropic/claude-3-5-sonnet",
        plan_node_id="none", _thread_id="sender-self",
    )
    switch = await DelegateSetModelTool().execute(
        "missing-target", "anthropic/claude-3-5-sonnet", _thread_id="sender-self",
    )

    for result in (create, task, switch):
        assert "usage_limited" in result
        assert "anthropic/claude-3-5-opus" in result


@pytest.mark.asyncio
async def test_model_catalog_tool_returns_shared_projection(monkeypatch):
    import llm.model_catalog_projection as projection
    from tools.model_catalog import ModelCatalogTool

    expected = {"models": [_row("m", "ready")], "active_local_threads": 0}
    monkeypatch.setattr(projection, "model_catalog_projection", lambda _threads: expected)

    assert json.loads(await ModelCatalogTool().execute()) == expected
