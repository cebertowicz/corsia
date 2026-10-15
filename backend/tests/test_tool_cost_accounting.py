"""The sheet's tool cost: per-app index rows, and what an agent looked up.

Two figures, both attribution only. The per-app index rows plus the CORSIA
remainder must equal the single Tool definitions row they replace, and the
looked-up figures must come out of History without changing any total.
"""

from __future__ import annotations

import json
from typing import Any

from core.loaded_tool_usage import loaded_tool_tokens
from core.token_estimation import _estimate_messages_tokens, _text_tokens
from core.tool_definition_sources import tool_definition_entries
from tests.test_tool_surface_disclosure import _ConfiguredTool, _registry

SERVERS = {"mcp_github_search": "github", "mcp_github_pr": "github", "mcp_linear_issue": "linear"}


def _call(call_id: str, **arguments: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": "tools_load", "arguments": json.dumps(arguments)},
    }]}


def _result(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _history() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "check the PR"},
        _call("a", server="github"),
        _result("a", "Tools from github. " + "definition " * 200),
        _call("b", names=["mcp_linear_issue"]),
        _result("b", "[" + '{"name": "mcp_linear_issue"}' * 20 + "]"),
        _call("c", pack="plans"),
        _result("c", "pack definitions " * 50),
        _call("d", server="github"),
        _result("d", "Error: server github is not connected"),
        {"role": "assistant", "content": "done"},
    ]


def test_index_rows_and_corsia_remainder_equal_the_old_tool_definitions_total(tmp_path):
    from core.native_context_prefix import _canonical_tool_definitions
    from tools.catalog import advertised_tool_definitions

    registry = _registry(tmp_path)
    for tool in (
        _ConfiguredTool("mcp_github_search", "github"),
        _ConfiguredTool("mcp_github_pr", "github"),
        _ConfiguredTool("mcp_linear_issue", "linear"),
    ):
        registry.register(tool)
    definitions = tuple(advertised_tool_definitions(registry))
    canonical = _canonical_tool_definitions(definitions)
    total = _text_tokens(canonical, "m") + 4

    entries = tool_definition_entries(
        id_prefix="native", thread_id="t", freshness="fresh", model="m",
        canonical_tool_definitions=canonical, frozen_tool_definitions=definitions,
        total_tokens=total, single_label="Native tool definitions",
    )

    index = [e for e in entries if (e.metadata or {}).get("provider") == "mcp_index"]
    assert sorted(e.label for e in index) == ["github", "linear"]
    assert {(e.metadata or {})["display_name"] for e in index} == {"GitHub", "Linear"}
    assert entries[-1].label == "CORSIA tools"
    assert sum(e.to_inventory_dict(model="m")["prompt_tokens"] for e in entries) == total


def test_no_connected_app_keeps_the_single_row_and_total(tmp_path):
    from core.native_context_prefix import _canonical_tool_definitions
    from tools.catalog import advertised_tool_definitions

    definitions = tuple(advertised_tool_definitions(_registry(tmp_path)))
    canonical = _canonical_tool_definitions(definitions)

    entries = tool_definition_entries(
        id_prefix="native", thread_id="t", freshness="fresh", model="m",
        canonical_tool_definitions=canonical, frozen_tool_definitions=definitions,
        total_tokens=500, single_label="Native tool definitions",
    )

    assert [e.label for e in entries] == ["Native tool definitions"]
    assert entries[0].to_inventory_dict(model="m")["prompt_tokens"] == 500


def test_looked_up_tokens_attribute_each_load_to_the_right_app():
    history = _history()

    loaded = loaded_tool_tokens(history, SERVERS, model="m")

    by_server = {row["server"]: row for row in loaded}
    assert set(by_server) == {"github", "linear"}  # pack load and the error stay History
    assert by_server["github"]["display_name"] == "GitHub"
    assert by_server["github"]["tokens"] > by_server["linear"]["tokens"] > 0
    assert loaded == sorted(loaded, key=lambda row: -row["tokens"])


def test_names_load_maps_each_name_to_its_server_and_splits_one_result():
    history = [
        _call("x", names=["mcp_github_search", "mcp_linear_issue", "mcp_unknown"]),
        _result("x", "definitions " * 100),
    ]
    paired = _estimate_messages_tokens([history[1]], model="m")

    loaded = loaded_tool_tokens(history, SERVERS, model="m")

    assert {row["server"] for row in loaded} == {"github", "linear"}
    assert sum(row["tokens"] for row in loaded) == paired


def test_looked_up_rows_plus_reduced_history_equal_history_tokens():
    history = _history()
    history_tokens = _estimate_messages_tokens(history, model="m")

    loaded = loaded_tool_tokens(history, SERVERS, model="m")

    reduced = history_tokens - sum(row["tokens"] for row in loaded)
    assert 0 < reduced < history_tokens
    assert reduced + sum(row["tokens"] for row in loaded) == history_tokens


def test_no_connected_app_or_history_attributes_nothing():
    assert loaded_tool_tokens(_history(), {}, model="m") == []
    assert loaded_tool_tokens([], SERVERS, model="m") == []
    assert loaded_tool_tokens(None, SERVERS, model="m") == []


def _seed_rows(isolated_db, thread_id: str) -> None:
    from infra.database import write_transaction
    from infra.queries import insert_message

    with write_transaction(None, scope="seed_tool_cost") as db:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?,?,?,?,?)", (thread_id, "user", "test/model", "t", "t"),
        )
        insert_message(thread_id, "user", "check the PR", {"turn_id": "turn"}, db=db)
        insert_message(thread_id, "tool", "Tools from github. " + "definition " * 200, {
            "turn_id": "turn", "name": "tools_load", "arguments": {"server": "github"},
        }, db=db)
        insert_message(thread_id, "tool", "pack definitions " * 50, {
            "turn_id": "turn", "name": "tools_load", "arguments": {"pack": "plans"},
        }, db=db)
        insert_message(thread_id, "tool", "Error: server linear is not connected", {
            "turn_id": "turn", "name": "tools_load", "arguments": {"server": "linear"},
            "is_error": True,
        }, db=db)


def test_estimate_without_a_checkpoint_reports_lookups_and_leaves_totals_alone(
    isolated_db, monkeypatch,
):
    from core.context_preview_estimate import estimated_preflight
    from infra.database import get_db

    import core.context_preview_estimate as estimate_module

    _seed_rows(isolated_db, "cost-thread")
    monkeypatch.setattr(estimate_module, "_installed_checkpoint_history", lambda *_a, **_k: None)
    monkeypatch.setattr("tools.mcp_runtime.tool_server_index", lambda: dict(SERVERS))
    db = get_db()
    try:
        estimate = estimated_preflight(
            "cost-thread", model="m", current_user_content="hi", db=db,
            live_window=258_000, tool_definition_tokens=0,
            source_collection=type("C", (), {"sources": []})(),
        )
    finally:
        db.close()

    loaded = estimate["loaded_tool_tokens"]
    assert [row["server"] for row in loaded] == ["github"]
    assert 0 < loaded[0]["tokens"] < estimate["history_tokens"]
    assert estimate["total_tokens"] >= estimate["history_tokens"]  # nothing added or subtracted
    assert estimate["continuity_mode"] == "unknown"


def test_provider_resume_estimate_reports_no_lookups(monkeypatch):
    from core.context_preview_estimate import estimated_preflight

    class Result:
        def to_dict(self):
            return {
                "status": "allowed", "continuity_mode": "unknown", "model_context_window": 1000,
                "block_reason": None, "total_tokens": 10, "current_user_tokens": 0,
                "history_tokens": 0, "source_tokens": 10, "source_section_tokens": {},
            }

    import core.context_admission as admission

    monkeypatch.setattr(admission, "check_context_preflight", lambda **_k: Result())
    estimate = estimated_preflight(
        "t", model="m", current_user_content="", db=None, live_window=1000,
        tool_definition_tokens=0, retained_provider_tokens=500,
    )

    assert estimate["loaded_tool_tokens"] == []
