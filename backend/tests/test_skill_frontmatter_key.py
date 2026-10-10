"""A skill's CORSIA block is read under ``corsia:`` and only ``corsia:``.

A block filed under any other key is not read, so it must fail validation loudly
rather than validate cleanly while its handler and workflow silently disappear.

Every place a skill author copies the block from must file it under the same key,
including the YAML example inside a skill body (skill-creator's template), so the last
test scans every column-0 block opener in every tracked SKILL.md, not only frontmatter.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from core.skill_validation import validate_skill_text

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


def test_the_corsia_block_is_validated_under_its_own_name() -> None:
    result = validate_skill_text(_broken_handler(KEY))

    assert result.ok is False, "the corsia block was not read at all"
    assert (
        "corsia.handler.kind must be one of connection_action, cli, instruction, "
        "native_tool, script"
    ) in result.errors
    assert "corsia.workflow.steps must be a list" in result.errors


def test_a_block_filed_under_another_key_fails_loudly_and_is_not_read() -> None:
    result = validate_skill_text(_broken_handler(MISFILED))

    assert result.ok is False
    assert result.errors == [
        "corsai.handler is CORSIA metadata, which is read only under corsia; move the block there",
        "corsai.workflow is CORSIA metadata, which is read only under corsia; move the block there",
    ]
    assert KEY not in result.metadata


def test_another_tools_metadata_is_left_alone_even_under_the_same_field_names() -> None:
    text = (
        "---\nname: other\ndescription: Another tool's metadata\nmetadata:\n  handler: owner\n"
        "othertool:\n  handler: {command: run}\n  workflow: {stages: [build]}\n---\n"
    )

    result = validate_skill_text(text)

    assert result.ok is True, result.errors


def test_every_skill_file_files_its_block_under_the_key_the_reader_reads() -> None:
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "*SKILL.md"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8").split("\0")
    opener = re.compile(r"^([\"']?)([^\s:#\"']+)\1:[ \t]*(?:#.*)?\n[ \t]+(?:handler|workflow):", re.MULTILINE)
    block_lines = 0
    misfiled: list[str] = []
    for rel in filter(None, tracked):
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for match in opener.finditer(text):
            block_lines += 1
            if match.group(2) != KEY:
                number = text.count("\n", 0, match.start()) + 1
                misfiled.append(f"{rel}:{number}: {match.group(2)}:")
    assert block_lines, "no SKILL.md block opener found at all: the check would prove nothing"
    assert not misfiled, (
        f"{len(misfiled)} SKILL.md block(s) are filed under a key other than '{KEY}:', so a "
        "skill written from them fails validation:\n  " + "\n  ".join(misfiled)
    )
