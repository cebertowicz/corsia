"""A skill's handler and workflow data is read from ``corsia.yaml`` and nowhere else.

The top-level ``corsia:`` block is read only for lesson data (``lesson``, ``capture``).
A handler or workflow block there, or under any other key, is not read, so it must fail
validation loudly rather than validate cleanly while its data silently disappears.

Every place a skill author copies CORSIA data from must use a form the reader reads.
Tracked skills now keep it in a ``corsia.yaml`` sidecar, so the last test validates
every tracked sidecar with its SKILL.md and fails any tracked SKILL.md that still carries
a handler or workflow block (top level, misfiled key or body example) or a dead pointer.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from core.skill_sidecar import BLOCK_ONLY_KEYS, SIDECAR_FILE, pointer_errors
from core.skill_validation import validate_skill_file, validate_skill_text

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY = "corsia"
# Test data: a near-miss key a skill author could type.
MISFILED = "corsai"
# A column-0 block opener whose first child is handler or workflow: the retired form, wherever it is.
OPENER = re.compile(r"^([\"']?)([^\s:#\"']+)\1:[ \t]*(?:#.*)?\n[ \t]+(?:handler|workflow):", re.MULTILINE)


def _broken_handler(key: str) -> str:
    return f"""---
name: frontmatter-key
description: Proves which block is read
{key}:
  handler: {{kind: not-a-kind}}
  workflow: {{steps: not-a-list}}
---
# Frontmatter key
"""


def test_a_corsia_block_is_read_under_its_own_name_and_is_retired_for_handler_data() -> None:
    result = validate_skill_text(_broken_handler(KEY))

    assert result.ok is False
    assert len(result.errors) == 1
    assert "the top-level corsia block is retired" in result.errors[0]
    assert "corsia.yaml" in result.errors[0] and "corsia-data" in result.errors[0]
    assert result.corsia is None


def test_a_block_filed_under_another_key_fails_loudly_and_is_not_read() -> None:
    result = validate_skill_text(_broken_handler(MISFILED))

    assert result.ok is False
    assert result.errors == [
        "corsai.handler is CORSIA metadata, which no reader reads under corsai; "
        "move it to corsia.yaml beside SKILL.md",
        "corsai.workflow is CORSIA metadata, which no reader reads under corsai; "
        "move it to corsia.yaml beside SKILL.md",
    ]
    assert KEY not in result.metadata


def test_another_tools_metadata_is_left_alone_even_under_the_same_field_names() -> None:
    text = (
        "---\nname: other\ndescription: Another tool's metadata\nmetadata:\n  handler: owner\n"
        "othertool:\n  handler: {command: run}\n  workflow: {stages: [build]}\n---\n"
    )

    result = validate_skill_text(text)

    assert result.ok is True, result.errors


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", pattern],
        capture_output=True, check=True,
    ).stdout.decode("utf-8")
    return [rel for rel in out.split("\0") if rel and (REPO_ROOT / rel).is_file()]


def _skill_problems(root: Path, rels: list[str]) -> list[str]:
    """What keeps a skill from the sidecar form: a retired block, an old example, a dead pointer."""
    problems: list[str] = []
    for rel in rels:
        path = root / rel
        if not path.is_file():
            continue
        result = validate_skill_file(path)
        block = result.metadata.get(KEY)
        if block is not None and not (isinstance(block, dict) and set(BLOCK_ONLY_KEYS) & set(block)):
            problems.append(f"{rel}: a top-level {KEY}: block that is not lesson data")
        problems.extend(f"{rel}: {error}" for error in result.errors if "is CORSIA metadata" in error)
        for match in OPENER.finditer(result.body):
            number = result.body.count("\n", 0, match.start()) + 1
            problems.append(f"{rel}: body example {match.group(2)}: block (body line {number})")
        if pointer_errors(result.metadata.get("metadata"))[0] and not result.ok:
            problems.append(f"{rel}: its {SIDECAR_FILE} pointer does not resolve: {result.errors}")
    return problems


def test_the_scan_names_each_way_a_skill_misses_the_sidecar_form(tmp_path) -> None:
    pointer = "metadata:\n  corsia-data: corsia.yaml\n  corsia-format: '1'\n"
    skills = {
        "good": f"---\nname: good\ndescription: d\n{pointer}---\n# Good\n",
        "lesson": "---\nname: lesson\ndescription: d\ncorsia:\n  handler: {kind: cli}\n  lesson: {}\n---\n",
        "block": "---\nname: block\ndescription: d\ncorsia:\n  handler: {kind: cli}\n---\n",
        "example": "---\nname: example\ndescription: d\n---\n```yaml\ncorsia:\n  workflow: {}\n```\n",
        "dead": f"---\nname: dead\ndescription: d\n{pointer}---\n",
    }
    for name, text in skills.items():
        (tmp_path / "skills" / name).mkdir(parents=True)
        (tmp_path / "skills" / name / "SKILL.md").write_text(text, encoding="utf-8")
    (tmp_path / "skills/good/corsia.yaml").write_text("handler: {kind: cli}\n", encoding="utf-8")

    problems = _skill_problems(tmp_path, [f"skills/{name}/SKILL.md" for name in skills])

    assert [problem.split(":")[0] for problem in problems] == [
        "skills/block/SKILL.md", "skills/example/SKILL.md", "skills/dead/SKILL.md",
    ]
    assert "not lesson data" in problems[0] and "body example corsia:" in problems[1]
    assert "pointer does not resolve" in problems[2]


def test_every_skill_file_is_in_the_sidecar_form() -> None:
    sidecars = _tracked(f"*/{SIDECAR_FILE}")
    assert sidecars, f"no tracked {SIDECAR_FILE} found at all: the check would prove nothing"
    invalid = [
        f"{rel}: {result.errors}"
        for rel in sidecars
        if not (result := validate_skill_file((REPO_ROOT / rel).parent / "SKILL.md")).ok
    ]
    assert not invalid, "tracked sidecars that fail validation:\n  " + "\n  ".join(invalid)

    rels = _tracked("*SKILL.md")
    assert rels, "no SKILL.md is tracked at all: the check would prove nothing"
    problems = _skill_problems(REPO_ROOT, rels)
    assert not problems, (
        f"{len(problems)} tracked skill(s) are not in the sidecar form (handler and workflow "
        f"data in {SIDECAR_FILE}, pointed at from metadata):\n  " + "\n  ".join(problems)
    )
