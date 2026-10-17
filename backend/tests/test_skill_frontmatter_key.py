"""A skill's handler and workflow data is read from ``corsia.yaml`` and nowhere else.

The top-level ``corsia:`` block is read only for lesson data (``lesson``, ``capture``).
A handler or workflow block there, or under any other key, is not read, so it must fail
validation loudly rather than validate cleanly while its data silently disappears.

Every place a skill author copies CORSIA data from must use a form the reader reads.
Tracked skills now keep it in a ``corsia.yaml`` sidecar, so the last test validates
every tracked sidecar with its SKILL.md, and still fails any column-0 block opener left
in a tracked SKILL.md (frontmatter or a body example) under a key other than ``corsia:``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from core.skill_sidecar import SIDECAR_FILE
from core.skill_validation import validate_skill_file, validate_skill_text

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY = "corsia"
# Test data: a near-miss key a skill author could type.
MISFILED = "corsai"


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


def test_every_skill_file_files_its_data_where_the_reader_reads_it() -> None:
    sidecars = _tracked(f"*/{SIDECAR_FILE}")
    assert sidecars, f"no tracked {SIDECAR_FILE} found at all: the check would prove nothing"
    invalid = [
        f"{rel}: {result.errors}"
        for rel in sidecars
        if not (result := validate_skill_file((REPO_ROOT / rel).parent / "SKILL.md")).ok
    ]
    assert not invalid, "tracked sidecars that fail validation:\n  " + "\n  ".join(invalid)

    opener = re.compile(r"^([\"']?)([^\s:#\"']+)\1:[ \t]*(?:#.*)?\n[ \t]+(?:handler|workflow):", re.MULTILINE)
    misfiled: list[str] = []
    for rel in _tracked("*SKILL.md"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for match in opener.finditer(text):
            if match.group(2) != KEY:
                number = text.count("\n", 0, match.start()) + 1
                misfiled.append(f"{rel}:{number}: {match.group(2)}:")
    assert not misfiled, (
        f"{len(misfiled)} SKILL.md block(s) are filed under a key other than '{KEY}:', so a "
        "skill written from them fails validation:\n  " + "\n  ".join(misfiled)
    )
