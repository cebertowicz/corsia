from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import yaml

from registry import models
from tests.event_routing_helpers import mk_session


@pytest.fixture
def community_workspace(tmp_path, monkeypatch):
    from infra.user_profile import PROFILE_RELATIVE_PATH, USE_COMMUNITY_LABEL
    from tools import skill_library

    profile = tmp_path / PROFILE_RELATIVE_PATH
    profile.parent.mkdir()
    profile.write_text(f"{USE_COMMUNITY_LABEL} Yes\n", encoding="utf-8")
    monkeypatch.setattr(skill_library, "_workspace", lambda: tmp_path)
    return tmp_path


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
    assert tool.parameters["properties"]["action"]["enum"] == ["search", "install", "update", "remove", "fetch_pack"]
    assert "local_edits_token" in tool.parameters["properties"]
    assert "local_edits_token" in tool.parameters["properties"]["discard_local_edits"]["description"]


def test_search_reports_age_state_and_the_no_match_recovery(monkeypatch):
    from tools import skill_library

    monkeypatch.setattr(skill_library, "_index", lambda: SimpleNamespace(items=(_item(),), index_age="2 hours ago", refreshed=False))
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


def test_index_refreshes_notices_only_after_a_raw_index_refresh(monkeypatch):
    from library import producer
    from tools import skill_library_common

    refreshes = []
    monkeypatch.setattr(producer, "request_refresh", lambda: refreshes.append(True))
    monkeypatch.setattr(
        "library.load_index", lambda: SimpleNamespace(items=(_item(),), index_age="just now", refreshed=False),
    )
    skill_library_common.index()
    monkeypatch.setattr(
        "library.load_index", lambda: SimpleNamespace(items=(_item(),), index_age="just now", refreshed=True),
    )
    skill_library_common.index()

    assert refreshes == [True]


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
    monkeypatch.setattr(skill_library, "_index", lambda: SimpleNamespace(items=index, index_age="just now", refreshed=False))
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


def test_install_repairs_registration_for_an_existing_local_library_copy(monkeypatch):
    from library.items import source_ref
    from tools import skill_library

    item = _item()
    copy = SimpleNamespace(
        name="pdf", path="/tmp/pdf/SKILL.md", scope="thread",
        corsia={"origin": {"source": source_ref(item)}},
    )
    repaired = []
    db = object()
    monkeypatch.setattr(skill_library, "_index", lambda: SimpleNamespace(items=(item,)))
    monkeypatch.setattr(skill_library, "_effective", lambda *_args: ([copy], [copy]))
    monkeypatch.setattr(skill_library, "_register", lambda *args: repaired.append(args))

    result = skill_library._install("pdf", "owner", None, db)

    assert result["error"] == "already_installed"
    assert repaired == [("owner", "pdf", None, db)]


@pytest.mark.parametrize("held_as", ["pdf", "PDF"])
def test_install_refuses_a_name_another_skill_holds_and_writes_nothing(monkeypatch, held_as):
    from library import items
    from tools import skill_library

    holder = SimpleNamespace(name=held_as, path=f"/tmp/{held_as}/SKILL.md", scope="project", corsia={})
    monkeypatch.setattr(skill_library, "_index", lambda: SimpleNamespace(items=(_item(),)))
    monkeypatch.setattr(skill_library, "_effective", lambda *_args: ([holder], []))
    monkeypatch.setattr(items, "fetch_bundle", lambda _item: pytest.fail("a held name must not fetch or write"))

    result = skill_library._install("pdf", "owner", None, object())

    assert result["error"] == "name_taken"
    assert holder.path in result["message"]


def test_own_skill_changed_bytes_refuse_install_with_retry_message(monkeypatch):
    from library import index, items
    from tools import skill_library

    row = _item()
    row.update(source="library", url="skills/pdf/SKILL.md")
    item = index._validate_item(row)
    monkeypatch.setattr(skill_library, "_index", lambda: SimpleNamespace(items=(item,)))
    monkeypatch.setattr(skill_library, "_effective", lambda *_args: ([], []))
    monkeypatch.setattr(items, "_fetch_raw", lambda _url: b"changed")
    result = skill_library._install("pdf", "owner", None, object())
    assert result["error"] == "integrity"
    assert "changed moments ago" in result["message"]
    assert "try once more in a few minutes" in result["message"]
    assert "report it to the conductor" in result["message"]


def test_own_skill_file_and_version_changes_are_updates():
    from library import index
    from library.items import item_manifest, source_ref
    from tools import skill_library

    row = _item()
    row.update(source="library", url="skills/pdf/SKILL.md")
    item = index._validate_item(row)
    origin = {"source": source_ref(item), "item_manifest": item_manifest(item)}
    for change in ({"version": "1.1.0"}, {"files": [{"path": "SKILL.md", "sha256": "c" * 64}]}):
        assert skill_library._state({**item, **change}, {"pdf": origin}) == "update_available"


def test_completed_skill_library_action_emits_a_privacy_safe_outcome(isolated_db, monkeypatch, community_workspace):
    from infra import product_events
    from tools import skill_library

    emitted = []
    monkeypatch.setattr(skill_library, "_persistent_owner", lambda *_args: "owner")
    monkeypatch.setattr(skill_library, "_search", lambda *_args, **_kwargs: {"results": []})
    monkeypatch.setattr(product_events, "emit_product_event", lambda *args, **kwargs: emitted.append((args, kwargs)))

    assert skill_library._execute("search", "thread", {}, "pdf", None, False) == {"results": []}
    assert emitted == [(("skill_library", "search"), {"thread_id": "thread", "properties": {"outcome": "empty"}})]


@pytest.mark.parametrize(("action", "query", "name"), [
    ("search", "pdf", None), ("install", None, "pdf"), ("update", None, "pdf"),
])
def test_community_library_actions_stop_before_loading_the_index(
    isolated_db, monkeypatch, community_workspace, action, query, name,
):
    from infra.user_profile import PROFILE_RELATIVE_PATH, USE_COMMUNITY_LABEL
    from tools import skill_library

    profile = community_workspace / PROFILE_RELATIVE_PATH
    profile.write_text(f"{USE_COMMUNITY_LABEL} No\n", encoding="utf-8")
    emitted = []
    monkeypatch.setattr(skill_library, "_index", lambda: pytest.fail("loaded library index"))
    monkeypatch.setattr(
        skill_library, "_emit_action", lambda *args: emitted.append(args),
    )

    result = skill_library._execute(action, "thread", {}, query, name, False)

    assert result == {
        "error": "community_off",
        "message": "The person hasn't agreed to use the community's improvements (the 'Use community improvements' line in their profile), so the library is off and nothing was searched, installed or updated. Continue without it, and write your own skill with skill skill-creator if you need one. Only the person changes this answer; don't ask them for it just for this task. During first-run setup, that answer comes from the decision titled 'Use and share improvements with the CORSIA community?' in setup.setup_decisions; raise it unchanged there, and nowhere else. remove still works on a copy you already hold.",
    }
    assert emitted == [(action, "thread", result)]


def test_invalid_library_action_precedes_community_consent(isolated_db, monkeypatch, community_workspace):
    from infra.user_profile import PROFILE_RELATIVE_PATH, USE_COMMUNITY_LABEL
    from tools import skill_library

    profile = community_workspace / PROFILE_RELATIVE_PATH
    profile.write_text(f"{USE_COMMUNITY_LABEL} No\n", encoding="utf-8")
    monkeypatch.setattr(skill_library, "_emit_action", lambda *_args: None)

    assert skill_library._execute("replace", "thread", {}, None, None, False) == {
        "error": "invalid_action", "message": "action must be search, install, update, remove, or fetch_pack",
    }


def test_removing_a_library_copy_does_not_require_community_consent(isolated_db, monkeypatch, community_workspace):
    from infra.user_profile import PROFILE_RELATIVE_PATH, USE_COMMUNITY_LABEL
    from tools import skill_library

    profile = community_workspace / PROFILE_RELATIVE_PATH
    profile.write_text(f"{USE_COMMUNITY_LABEL} No\n", encoding="utf-8")
    removed = []
    monkeypatch.setattr(skill_library, "_persistent_owner", lambda *_args: "owner")
    monkeypatch.setattr(skill_library, "_remove", lambda *args: removed.append(args) or {"outcome": "removed"})
    monkeypatch.setattr(skill_library, "_emit_action", lambda *_args: None)

    assert skill_library._execute("remove", "thread", {}, None, "pdf", False) == {"outcome": "removed"}
    assert len(removed) == 1


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
    from infra.skill_install import installed_skill_text
    from tools import skill_library

    upstream = b"---\nname: pdf\ndescription: PDF work.\n---\nUse it.\n"
    installed = installed_skill_text("pdf", upstream, "PDF work.")
    assert installed is not None
    changed = skill_library._changed_paths(
        {"SKILL.md": installed.encode(), "corsia.yaml": b"local", "removed.md": b"old"},
        "pdf", upstream, "PDF work.", {"added.md": b"new"},
    )

    assert changed == ["added.md", "removed.md"]


def test_update_results_omit_unchanged_installed_files():
    from infra.skill_install import installed_skill_text
    from tools import skill_library

    upstream = b"---\nname: pdf\ndescription: PDF work.\n---\nUse it.\n"
    installed = installed_skill_text("pdf", upstream, "PDF work.")
    assert installed is not None

    changed = skill_library._changed_paths(
        {"SKILL.md": installed.encode(), "corsia.yaml": b"local", "guide.md": b"same"},
        "pdf", upstream, "PDF work.", {"guide.md": b"same"},
    )

    assert changed == []


def test_raw_index_failures_return_actionable_agent_errors(isolated_db, monkeypatch, community_workspace):
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
