from __future__ import annotations

import time
from types import SimpleNamespace

import yaml

from registry import models
from tests.event_routing_helpers import mk_session


def _item(*, withdrawn: bool = False) -> dict:
    return {
        "name": "pdf",
        "description": "Create and review PDF documents.",
        "version": "1.0.0",
        "license": "Apache-2.0",
        "source": {"repository": "openai/skills", "commit": "a" * 40, "path": "skills/pdf"},
        "files": [{"path": "SKILL.md", "sha256": "sha256:" + "b" * 64}],
        "withdrawn": withdrawn,
        "withdrawn_reason": "unsafe" if withdrawn else None,
    }


def test_library_tool_is_deferred_memory_and_registered(tmp_path):
    from core.agent import CORSIAAgent, _register_memory_tools
    from tools.catalog import TOOL_CATALOG, tool_pack

    agent = CORSIAAgent(tmp_path)
    _register_memory_tools(agent)

    tool = agent.tools.get("skill_library")
    assert tool.name == "skill_library"
    assert TOOL_CATALOG["skill_library"].category == "memory"
    assert tool_pack("skill_library") == "memory"
    assert tool.parameters["properties"]["action"]["enum"] == ["search", "install", "update", "remove"]
    assert "local_edits_token" in tool.parameters["properties"]
    assert "local_edits_token" in tool.parameters["properties"]["discard_local_edits"]["description"]


def test_search_reports_age_state_and_the_no_match_recovery(monkeypatch):
    from tools import skill_library

    monkeypatch.setattr(skill_library, "_index", lambda: SimpleNamespace(skills=(_item(),), index_age="2 hours ago"))
    monkeypatch.setattr(skill_library, "_installed_library_origins", lambda *_args: {})
    from library import producer

    monkeypatch.setattr(producer, "request_refresh", lambda: None)

    result = skill_library._search("review pdf", "owner", None, object())
    none = skill_library._search("unrelated", "owner", None, object())

    assert result == {
        "index_age": "2 hours ago",
        "results": [{
            "name": "pdf", "description": "Create and review PDF documents.", "version": "1.0.0",
            "license": "Apache-2.0", "source": "openai/skills@" + "a" * 40 + ":skills/pdf",
            "files": 1, "state": "not_installed",
        }],
    }
    assert none["results"] == []
    assert "skill-creator" in none["hint"]


def test_search_stays_under_200ms_with_one_owner_query_for_1000_items_and_30_copies(isolated_db, tmp_path, monkeypatch):
    """The search budget is 30 owner copies, not every routing registration."""
    from infra.database import get_db
    from library import producer
    from tools import skill_library

    owner = mk_session("library-search-owner")
    monkeypatch.setattr(skill_library, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(producer, "request_refresh", lambda: None)
    from library.items import item_manifest

    source = "openai/skills@" + "a" * 40 + ":skills/pdf"
    manifest = item_manifest(_item())
    for number in range(30):
        name = f"copy-{number}"
        folder = tmp_path / ".threads" / owner / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: copy\n---\nbody\n")
        (folder / "corsia.yaml").write_text(yaml.safe_dump({"origin": {"source": source, "version": "1.0.0", "item_manifest": manifest}}))
        models.register_artifact(
            integration="library", type="installed-skill", external_id=f"{owner}/{name}",
            parent_thread_id=owner, path=str(folder / "SKILL.md"),
        )
    index = tuple({**_item(), "name": f"copy-{number}"} for number in range(1000))
    monkeypatch.setattr(skill_library, "_index", lambda: SimpleNamespace(skills=index, index_age="just now"))
    db = get_db()
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        started = time.perf_counter()
        result = skill_library._search("copy", owner, None, db)
        elapsed = time.perf_counter() - started
    finally:
        db.set_trace_callback(None)
        db.close()

    assert elapsed < 0.2
    # Search deliberately caps agent-facing results at ten, while matching the
    # complete synthetic index before it looks up the returned rows' state.
    assert len(result["results"]) == 10
    assert result["results"][0]["state"] == "installed"
    assert len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]) == 1
    assert not [sql for sql in statements if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]


def test_same_source_with_a_changed_declared_manifest_is_an_available_update():
    from library.items import item_manifest, source_ref
    from tools import skill_library

    current = _item()
    old = {**current, "version": "0.9.0"}
    origin = {"source": source_ref(current), "item_manifest": item_manifest(old)}

    assert skill_library._state(current, {"pdf": origin}) == "update_available"
    assert not skill_library._current_library_item(origin, current)


def test_discard_token_is_bound_to_the_exact_local_files(tmp_path):
    from tools import skill_library

    folder = tmp_path / "pdf"
    folder.mkdir()
    (folder / "SKILL.md").write_bytes(b"first")
    token = skill_library._local_edits_token("owner", "pdf", folder)

    assert skill_library._discard_confirmation("owner", "pdf", folder, ["SKILL.md"], token) == {}
    (folder / "SKILL.md").write_bytes(b"changed")
    assert skill_library._discard_confirmation("owner", "pdf", folder, ["SKILL.md"], token)["error"] == "discard_confirmation_required"


def test_update_results_name_files_dropped_upstream():
    from tools import skill_library

    changed = skill_library._changed_paths(
        {"SKILL.md": b"same", "corsia.yaml": b"local", "removed.md": b"old"},
        b"same", {"added.md": b"new"},
    )

    assert changed == ["added.md", "removed.md"]


def test_raw_index_failures_return_actionable_agent_errors(isolated_db, monkeypatch):
    from tools import skill_library

    monkeypatch.setattr(skill_library, "_persistent_owner", lambda *_args: "owner")
    monkeypatch.setattr(
        skill_library, "_index",
        lambda: (_ for _ in ()).throw(skill_library._LibraryIndexError("library_unreachable", "try later")),
    )

    assert skill_library._execute("search", "thread", {}, "pdf", None, False) == {
        "error": "library_unreachable", "message": "try later",
    }


def test_refusals_name_the_missing_argument_and_exact_search_recovery():
    from tools import skill_library

    assert skill_library._error("query_required", "search requires query")["error"] == "query_required"
    assert skill_library._error("not_in_library", "search first; names are exact")["message"] == "search first; names are exact"
