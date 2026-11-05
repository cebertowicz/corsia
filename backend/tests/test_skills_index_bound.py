from __future__ import annotations

from pathlib import Path

from core.lesson_index import LessonPartition, TRIAL_MARK, index_line, line_tokens
from core.skill_registry_types import SkillResolution, SkillRow
from tests.test_lesson_index import db as lesson_db, workspace as lesson_workspace  # noqa: F401


def _row(
    name: str,
    scope: str,
    scope_id: str,
    description: str = "Use this skill.",
    corsia=None,
    metadata: dict | None = None,
) -> SkillRow:
    return SkillRow(
        name=name,
        description=description,
        scope=scope,  # type: ignore[arg-type]
        scope_id=scope_id,
        path=Path(f"/{scope}/{scope_id}/{name}/SKILL.md"),
        body_path=Path(f"/{scope}/{scope_id}/{name}/SKILL.md"),
        metadata=metadata or {},
        status="included",
        corsia=corsia,
    )


def _index(
    monkeypatch,
    rows: list[SkillRow],
    *,
    lessons: list[dict] | None = None,
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
        lambda included, omitted, **_kwargs: LessonPartition(included, omitted, lessons or []),
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
    assert content.endswith(f"- workspace: {'x' * 500}")
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


def _trial(name: str, description: str = "Use this skill.") -> SkillRow:
    return _row(
        name,
        "thread",
        "current",
        description,
        metadata={"corsia": {"lesson": {"id": "trial-id"}}},
    )


def _trial_record(row: SkillRow) -> dict:
    return {
        "id": "trial-id",
        "name": row.name,
        "scope": row.scope,
        "scope_id": row.scope_id,
        "index_line": index_line(row),
    }


def test_trial_uses_the_same_bounded_line_as_its_lesson_record(monkeypatch, lesson_db, lesson_workspace):
    from core.lesson_delivery import start_trials
    from tests import test_lesson_index as lessons

    original = lessons._trial_text
    monkeypatch.setattr(lessons, "_trial_text", lambda *args, **kwargs: original(*args, **kwargs).replace(
        "Use when reporting a check result on a pull request.", "x" * 501,
    ))
    lessons._activate(lesson_db, lesson_workspace)
    [lesson] = lessons._task_offers(lesson_db, lesson_workspace)
    node = lesson_db.execute("SELECT node_id FROM guidance_loads WHERE variant='with'").fetchone()[0]
    [offer] = start_trials(lesson_db, thread_id="owner", node_id=node)
    delivered_line = offer["guidance"]

    assert delivered_line == lesson["index_line"]
    assert "x" * 500 in delivered_line
    assert "x" * 501 not in delivered_line


def test_trial_after_the_ordinary_limit_stays_full(monkeypatch):
    ordinary = [
        _row(f"skill-{number:02}", "workspace", "workspace")
        for number in range(45)
    ]
    trial = _trial("skill-trial")
    index = _index(monkeypatch, [*ordinary, trial], lessons=[_trial_record(trial)])
    skills = index.metadata["skills"]

    trial_skill = next(skill for skill in skills if skill["name"] == trial.name)
    assert trial_skill["listed"] == "full"
    assert f"- {trial.name}{TRIAL_MARK}:" in (index.prompt_content or "")
    assert all(skill["name"] != trial.name for skill in skills if skill["listed"] == "name")


def test_trial_token_estimate_uses_its_delivered_line(monkeypatch):
    trial = _trial("trial", "x" * 501)
    index = _index(monkeypatch, [trial], lessons=[_trial_record(trial)])
    inventory = index.to_inventory_dict()
    skill = inventory["metadata"]["skills"][0]

    assert skill["prompt_tokens"] == line_tokens(_trial_record(trial)["index_line"])


def _odd_row(scope: str, scope_id: str) -> SkillRow:
    row = _row("odd", scope, scope_id)
    return SkillRow(**{**row.__dict__, "path": Path("/elsewhere/renamed/SKILL.md")})


def test_rows_sharing_a_root_state_it_once_and_an_odd_path_keeps_its_own(monkeypatch):
    rows = [_row("first", "workspace", "workspace"), _row("second", "workspace", "workspace"), _odd_row("system", "v1")]
    lines = (_index(monkeypatch, rows).prompt_content or "").splitlines()

    header = "Skills in /workspace/workspace/ (read /workspace/workspace/<name>/SKILL.md):"
    assert lines.count(header) == 1
    assert "- first: Use this skill." in lines
    assert "- second: Use this skill." in lines
    assert "- odd: Use this skill. (/elsewhere/renamed/SKILL.md)" in lines
    assert not any("/first/SKILL.md" in line for line in lines)


def test_overflow_names_share_a_root_header_and_an_odd_path_keeps_its_own(monkeypatch):
    rows = [_row(f"extra-{number:02}", "project", "project") for number in range(40)]
    rows += [_row("first", "workspace", "workspace"), _row("second", "workspace", "workspace"), _odd_row("workspace", "workspace")]
    overflow = (_index(monkeypatch, rows).prompt_content or "").splitlines()[-1]

    assert overflow.startswith("Also installed, listed by name only")
    assert "Skills in /workspace/workspace/ (read /workspace/workspace/<name>/SKILL.md): " in overflow
    assert "odd (/elsewhere/renamed/SKILL.md)" in overflow
    assert "/first/SKILL.md" not in overflow
