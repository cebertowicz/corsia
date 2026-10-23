from __future__ import annotations

from pathlib import Path

from core.lesson_index import LessonPartition
from core.skill_registry_types import SkillResolution, SkillRow


def _row(
    name: str,
    scope: str,
    scope_id: str,
    description: str = "Use this skill.",
    corsia=None,
) -> SkillRow:
    return SkillRow(
        name=name,
        description=description,
        scope=scope,  # type: ignore[arg-type]
        scope_id=scope_id,
        path=Path(f"/{scope}/{scope_id}/{name}/SKILL.md"),
        body_path=Path(f"/{scope}/{scope_id}/{name}/SKILL.md"),
        metadata={},
        status="included",
        corsia=corsia,
    )


def _index(
    monkeypatch,
    rows: list[SkillRow],
    *,
    precedence: tuple[tuple[str, str], ...] = (
        ("system", "v1"), ("workspace", "workspace"), ("project", "project"),
        ("thread", "parent"), ("thread", "current"),
    ),
):
    import core.skills_registry as registry

    monkeypatch.setattr(
        registry,
        "resolve_effective_skills",
        lambda **_kwargs: SkillResolution(rows, [], [], None, precedence),
    )
    monkeypatch.setattr(
        registry,
        "partition",
        lambda included, omitted, **_kwargs: LessonPartition(included, omitted),
    )
    monkeypatch.setattr(registry, "emit_product_event", lambda *_args, **_kwargs: None)
    return registry.skill_context_sources(
        thread_id="current", project_id="project", workspace=Path("/workspace"), db=object(),
    )[0]


def test_non_system_skills_stay_full_through_40_then_overflow_by_name(monkeypatch):
    system = _row("system-skill", "system", "v1", "System stays complete.")
    forty = [
        _row(f"skill-{number:02}", "workspace", "workspace")
        for number in range(40)
    ]
    at_limit = _index(monkeypatch, [system, *forty])
    over_limit = _index(monkeypatch, [system, *forty, _row("skill-40", "workspace", "workspace")])

    assert "Also installed, listed by name only" not in (at_limit.prompt_content or "")
    assert "Also installed, listed by name only" in (over_limit.prompt_content or "")
    listed = over_limit.metadata["skills"]
    assert listed[0]["name"] == "system-skill"
    assert listed[0]["listed"] == "full"
    assert all(skill["listed"] == "full" for skill in listed[1:41])
    assert listed[-1]["name"] == "skill-40"
    assert listed[-1]["listed"] == "name"


def test_index_orders_non_system_skills_current_thread_to_workspace(monkeypatch):
    index = _index(monkeypatch, [
        _row("workspace", "workspace", "workspace"),
        _row("project", "project", "project"),
        _row("parent-z", "thread", "parent"),
        _row("current-z", "thread", "current"),
        _row("current-a", "thread", "current"),
        _row("system", "system", "v1"),
    ])

    assert [skill["name"] for skill in index.metadata["skills"]] == [
        "system", "current-a", "current-z", "parent-z", "project", "workspace",
    ]


def test_index_is_byte_identical_when_resolution_scan_order_changes(monkeypatch):
    rows = [
        _row("workspace", "workspace", "workspace"),
        _row("project", "project", "project"),
        _row("current", "thread", "current"),
        _row("system", "system", "v1"),
    ]

    first = _index(monkeypatch, rows).prompt_content
    second = _index(monkeypatch, list(reversed(rows))).prompt_content

    assert second == first


def test_adopted_copy_origin_is_projected_without_adoption_timestamp(monkeypatch):
    index = _index(monkeypatch, [_row(
        "imported", "workspace", "workspace", corsia={"origin": {
            "source": "library:imported", "version": 1.2, "sha256": "a" * 64,
            "adopted_at": "2026-10-18T12:00:00Z",
        }},
    )])

    assert index.metadata["skills"][0]["origin"] == {
        "source": "library:imported", "version": "1.2", "sha256": "a" * 64,
    }


def test_non_system_descriptions_are_bounded_but_system_descriptions_are_not(monkeypatch):
    long_description = "x" * 501
    index = _index(monkeypatch, [
        _row("system", "system", "v1", long_description),
        _row("workspace", "workspace", "workspace", long_description),
    ])

    content = index.prompt_content or ""
    assert f"- system: {long_description}" in content
    assert f"- workspace: {'x' * 500} (" in content
    assert f"- workspace: {long_description}" not in content


def test_overflow_rows_sum_to_the_index_prompt_tokens(monkeypatch):
    rows = [_row("system", "system", "v1")] + [
        _row(f"skill-{number:02}", "workspace", "workspace")
        for number in range(41)
    ]
    inventory = _index(monkeypatch, rows).to_inventory_dict()
    skills = inventory["metadata"]["skills"]

    assert all("prompt_tokens" in skill for skill in skills)
    assert sum(skill["prompt_tokens"] for skill in skills) <= inventory["prompt_tokens"]
    assert any(skill["listed"] == "name" for skill in skills)
