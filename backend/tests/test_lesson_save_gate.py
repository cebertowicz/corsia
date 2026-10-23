"""HG-1: an agent's lesson reaches a guidance file only through ``place``.

The gate sits in ``refused_save``, the one refusal hook every guidance save
passes. It judges a write against the file on disk, for agent authors only.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import cast

import pytest

from infra.skill_save_validation import REFUSED, INVALID_SKILL_REASON, invalid_skill_write, refused_save

AGENT = "Lane <thread-1@agents.corsia.local>"
PERSON = "Person <person@users.corsia.local>"
CORSIA = "CORSIA <corsia@localhost>"
REASON = "lesson-outside-placement"

BOOTLOADER = (
    "# Role\n\n## Lessons\n\n- 2026-10-01: Keep each commit small.\n"
    "- 2026-10-02: Run the smaller tests first.\n\n## Other\n\nPlain body.\n"
)
LESSON_SKILL = (
    "---\nname: gate\ndescription: Use when merging.\ncorsia:\n  lesson:\n"
    "    id: 9ece0e89-3e4d-5e77-b235-d678598db53b\n    state: trial\n"
    "    owner: thread-1\n    review_after: '2026-10-22T00:00:00+00:00'\n"
    "    source_refs:\n    - kind: turn\n      reference: turn-1\n"
    "---\n\n# Trial\n\n- Read the real thing first.\n"
)
PLAIN_SKILL = "---\nname: gate\ndescription: Use when merging.\n---\n\n# Gate\n\n- Run the gates.\n"


def _on_disk(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _refuse(writes: dict[Path, str], author: str | None = AGENT, **kwargs):
    return refused_save({path: text.encode() for path, text in writes.items()}, author=author, **kwargs)


def _reason(result) -> str | None:
    return None if result is None else result.reason


def test_an_agent_cannot_add_a_lesson_line_by_editing(tmp_path) -> None:
    path = _on_disk(tmp_path, "RESPONSIBILITY.md", BOOTLOADER)
    added = BOOTLOADER.replace("\n## Other", "- 2026-10-23: Read the stamp on this frame.\n\n## Other")

    result = _refuse({path: added})

    assert result is not None
    assert (result.outcome, result.reason) == (REFUSED, REASON)
    assert "continuity_edit" in result.line and "place" in result.line
    assert "RESPONSIBILITY.md" in result.line


def test_an_agent_cannot_rewrite_a_lesson_line(tmp_path) -> None:
    path = _on_disk(tmp_path, "RESPONSIBILITY.md", BOOTLOADER)

    result = _refuse({path: BOOTLOADER.replace("Keep each commit small.", "Keep each commit tiny.")})

    assert _reason(result) == REASON


def test_a_new_file_with_a_lessons_section_is_refused(tmp_path) -> None:
    result = _refuse({tmp_path / "LESSONS.md": "# Lessons\n\n## Lessons\n\n- 2026-10-23: new line.\n"})

    assert _reason(result) == REASON


def test_a_new_skill_with_a_lesson_block_is_refused(tmp_path) -> None:
    path = tmp_path / "skills" / "gate" / "SKILL.md"
    assert invalid_skill_write({path: LESSON_SKILL.encode()}) is None

    assert _reason(_refuse({path: LESSON_SKILL})) == REASON


def test_a_changed_lesson_block_is_refused_and_a_body_edit_passes(tmp_path) -> None:
    path = _on_disk(tmp_path, "skills/gate/SKILL.md", LESSON_SKILL)

    changed = _refuse({path: LESSON_SKILL.replace("state: trial", "state: watch")})
    body_edit = _refuse({path: LESSON_SKILL + "- Open the file at the current revision.\n"})

    assert _reason(changed) == REASON
    assert body_edit is None


def test_the_gate_is_on_by_default_and_a_corsia_author_is_an_agent(tmp_path) -> None:
    path = _on_disk(tmp_path, "RESPONSIBILITY.md", BOOTLOADER)
    added = {path: BOOTLOADER + "\n## Lessons\n\n- 2026-10-23: another.\n"}

    assert _reason(_refuse(added, author=CORSIA)) == REASON
    assert _reason(_refuse(added, author=None)) == REASON
    assert _reason(refused_save({p: t.encode() for p, t in added.items()}, author=AGENT)) == REASON


def test_an_invalid_skill_is_reported_before_the_lesson_rule(tmp_path) -> None:
    path = tmp_path / "skills" / "gate" / "SKILL.md"

    result = _refuse({path: LESSON_SKILL.replace("description: Use when merging.\n", "")})

    assert _reason(result) == INVALID_SKILL_REASON


# -- Not gated (HG-1) -------------------------------------------------------------


def test_not_gated_a_persons_own_edit(tmp_path) -> None:
    path = _on_disk(tmp_path, "RESPONSIBILITY.md", BOOTLOADER)
    added = BOOTLOADER.replace("\n## Other", "- 2026-10-23: The person's own words.\n\n## Other")

    assert _refuse({path: added}, author=PERSON) is None
    assert _refuse({tmp_path / "skills" / "gate" / "SKILL.md": LESSON_SKILL}, author=PERSON) is None


def test_not_gated_deletions_and_reorders(tmp_path) -> None:
    path = _on_disk(tmp_path, "RESPONSIBILITY.md", BOOTLOADER)
    dropped = BOOTLOADER.replace("- 2026-10-02: Run the smaller tests first.\n", "")
    first, second = "- 2026-10-01: Keep each commit small.\n", "- 2026-10-02: Run the smaller tests first.\n"
    reordered = BOOTLOADER.replace(first + second, second + first)

    assert _refuse({path: dropped}) is None
    assert _refuse({path: reordered}) is None


def test_not_gated_moving_a_lesson_out_to_a_decision_log(tmp_path) -> None:
    path = _on_disk(tmp_path, "RESPONSIBILITY.md", BOOTLOADER)
    log = _on_disk(tmp_path, "shared/decisions.md", "# Decisions\n")
    moved = BOOTLOADER.replace("- 2026-10-01: Keep each commit small.\n", "")
    logged = "# Decisions\n\n- Keep each commit small.\n"

    assert _refuse({path: moved, log: logged}) is None


def test_not_gated_a_procedure_skill_without_a_lesson_block(tmp_path) -> None:
    path = tmp_path / "skills" / "gate" / "SKILL.md"

    assert _refuse({path: PLAIN_SKILL}) is None
    on_disk = _on_disk(tmp_path, "skills/other/SKILL.md", PLAIN_SKILL.replace("gate", "other"))
    assert _refuse({on_disk: on_disk.read_text() + "- Another step.\n"}) is None


def test_not_gated_a_path_fix_outside_the_lessons_section(tmp_path) -> None:
    path = _on_disk(tmp_path, "RESPONSIBILITY.md", BOOTLOADER + "\nSee old/path.md for the map.\n")

    fixed = _refuse({path: BOOTLOADER.replace("Plain body.", "Plain body.") + "\nSee new/path.md for the map.\n"})

    assert fixed is None


def test_a_path_fix_inside_a_lesson_line_goes_through_place(tmp_path) -> None:
    """The gate has no token-diff rule: a changed lesson line is a changed line."""
    path = _on_disk(tmp_path, "RESPONSIBILITY.md", BOOTLOADER.replace("small.", "small (see old/path.md)."))

    result = _refuse({path: BOOTLOADER.replace("small.", "small (see new/path.md).")})

    assert _reason(result) == REASON


def test_not_gated_the_named_call_sites_that_switch_it_off(tmp_path) -> None:
    path = tmp_path / "skills" / "gate" / "SKILL.md"

    assert _refuse({path: LESSON_SKILL}, lesson_gate=False) is None


_SRC = Path(__file__).resolve().parents[1]
#: skill_install is reviewed library content; placement and lesson_outcomes are the writers behind ``place``.
_GATE_OFF = {"core/placement_save.py": 1, "core/lesson_outcomes.py": 1, "infra/skill_install.py": 3}


def _save_guidance_calls() -> dict[str, list[ast.Call]]:
    found: dict[str, list[ast.Call]] = {}
    for source in _SRC.rglob("*.py"):
        relative = source.relative_to(_SRC).as_posix()
        if relative.startswith(("tests/", ".venv")) or "/tests/" in relative:
            continue
        for node in ast.walk(ast.parse(source.read_text())):
            name = getattr(node.func, "id", getattr(node.func, "attr", None)) if isinstance(node, ast.Call) else None
            if name == "save_guidance":
                found.setdefault(relative, []).append(cast(ast.Call, node))
    return found


def _passes_false(call: ast.Call) -> bool:
    return any(
        keyword.arg == "lesson_gate" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        for keyword in call.keywords
    )


@pytest.mark.parametrize("relative", sorted(_save_guidance_calls()))
def test_only_the_named_call_sites_turn_the_gate_off(relative: str) -> None:
    calls = _save_guidance_calls()[relative]
    switched_off = sum(_passes_false(call) for call in calls)

    assert switched_off == _GATE_OFF.get(relative, 0), relative


def test_every_named_call_site_still_exists() -> None:
    assert set(_GATE_OFF) <= set(_save_guidance_calls())
