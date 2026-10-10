"""The context sheet's rows add up to its headline (display accounting only)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.effective_context import ContextSourceEntry
from tests.test_context_preview_contracts import (
    _patch_preview_sources,
    _preview_connection,
    _target,
)
from tests.test_skills_registry import _db, _skill, _valid


def _text_source(source_id: str, origin: str, text: str, **metadata) -> ContextSourceEntry:
    return ContextSourceEntry(
        source_id=source_id, origin_section=origin, label=source_id,
        stable_reference=f"file:///{source_id}", freshness="fresh",
        inclusion="included", reason="test", owner_surface="document-viewer",
        prompt_content=text, metadata=metadata,
    )


def _skill_sources(tmp_path):
    from core.skills_registry import skill_context_sources

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _skill(workspace, "skills/review", _valid("review", "Use when reviewing code."))
    _skill(workspace, "skills/ship", _valid("ship", "Use when shipping a change."))
    return skill_context_sources(
        thread_id="sess", project_id=None, workspace=workspace, db=_db(),
    )


def _index(tmp_path) -> ContextSourceEntry:
    return next(
        source for source in _skill_sources(tmp_path)
        if source.source_id == "dynamic:skills-index"
    )


def test_skills_index_lists_each_skill_with_a_share_of_the_block(tmp_path):
    index = _index(tmp_path)
    listed = index.metadata["skills"]
    mine = [row for row in listed if row["scope"] == "workspace"]
    assert [(row["name"], row["lesson_id"]) for row in mine] == [
        ("review", None), ("ship", None),
    ]
    inventory = index.to_inventory_dict(model="gpt-4o")
    shares = [row["prompt_tokens"] for row in inventory["metadata"]["skills"]]
    assert all(share > 0 for share in shares)
    # The header and framing stay in the block's own count, never in a skill.
    assert sum(shares) < inventory["prompt_tokens"]
    # The stored entry is untouched: the estimate lives in the inventory only.
    assert all("prompt_tokens" not in row for row in index.metadata["skills"])


def test_a_skill_missing_from_the_delivered_text_has_no_estimate(tmp_path):
    index = _index(tmp_path)
    index.metadata["skills"].append(
        {"name": "ghost", "scope": "workspace", "scope_id": "w", "role": None, "lesson_id": None}
    )
    rows = index.to_inventory_dict(model="gpt-4o")["metadata"]["skills"]
    assert "prompt_tokens" not in rows[-1]
    assert all("prompt_tokens" in row for row in rows[:-1])


def _accounted(preview) -> tuple[int, ...]:
    """What the sheet lists, in the order it adds up to the headline."""
    inventory = preview["context_snapshot"]["source_inventory"]
    rows = sum(
        row["prompt_tokens"] or 0 for row in inventory
        if row["origin_section"] not in {"preflight", "model", "user", "history"}
    )
    return (
        rows,
        preview["history_tokens"],
        preview["user_tokens"],
        preview["preflight"]["checkpoint_tokens"],
    )


def _scoped_sources(tmp_path):
    return [
        _text_source("system:a", "corsia", "system words " * 40),
        _text_source("workspace:b", "workspace", "workspace words " * 30),
        _text_source("project:c", "project", "project words " * 20),
        *_skill_sources(tmp_path),
    ]


@pytest.mark.parametrize("harness", ["native", "codex"])
def test_preview_rows_equal_the_headline(monkeypatch, tmp_path, harness):
    from core import context_preview

    connection, thread_row = _preview_connection()
    _patch_preview_sources(monkeypatch, _target(harness=harness, model=f"{harness}/model"))
    sources = _scoped_sources(tmp_path)
    monkeypatch.setattr(
        context_preview, "collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            sources=sources, agent_facts={}, installed=None, project_cwd=None,
        ),
    )
    definitions = [{
        "type": "function",
        "function": {"name": "read_exact", "parameters": {"type": "object", "properties": {}}},
    }]

    preview = context_preview.build_next_turn_context_preview(
        "preview-thread", thread_row, db=connection,
        current_user_content="hello", tool_definitions=definitions,
    )

    parts = _accounted(preview)
    assert sum(parts) == preview["total_used"], (parts, preview["total_used"])


def test_preview_with_an_installed_checkpoint_still_adds_up(
    isolated_db, monkeypatch, tmp_path,
):
    from core import context_preview
    from tests.test_context_preview_contracts import _checkpointed_preview_connection

    connection, thread_row = _checkpointed_preview_connection(isolated_db)
    _patch_preview_sources(monkeypatch, _target())
    sources = _scoped_sources(tmp_path)
    monkeypatch.setattr(
        context_preview, "collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            sources=sources, agent_facts={}, installed=None, project_cwd=None,
        ),
    )

    preview = context_preview.build_next_turn_context_preview(
        "preview-checkpoint", thread_row, db=connection,
    )

    assert preview["preflight"]["checkpoint_tokens"] > 0
    parts = _accounted(preview)
    assert sum(parts) == preview["total_used"], (parts, preview["total_used"])
    connection.close()


from tests.test_lesson_index import (  # noqa: E402,F401  (fixtures are imported by name)
    LESSON,
    _activate,
    _index as _lesson_index,
    _plain,
    _sources,
    _write,
    _trial_text,
    db as lesson_db,
    workspace as lesson_workspace,
)


def _listed(db, workspace):
    index = _lesson_index(_sources(db, workspace))
    # System skills share the index; only the thread's own skills matter here.
    return {
        row["name"]: row["lesson_id"] for row in index.metadata["skills"]
        if row["name"] in {"deploy", "cite-the-head"}
    }


def test_an_offered_trial_sets_lesson_id_on_the_skills_list(lesson_db, lesson_workspace):
    _write(lesson_workspace, "owner", "deploy", _plain("deploy"))
    _activate(lesson_db, lesson_workspace)

    assert _listed(lesson_db, lesson_workspace) == {"cite-the-head": LESSON, "deploy": None}


def test_a_held_trial_carries_no_lesson_id_on_the_skills_list(lesson_db, lesson_workspace):
    _write(lesson_workspace, "owner", "deploy", _plain("deploy"))
    # Written without a scheduled review, so the registry holds it back.
    _write(lesson_workspace, "owner", "cite-the-head", _trial_text())

    assert _listed(lesson_db, lesson_workspace) == {"deploy": None}
