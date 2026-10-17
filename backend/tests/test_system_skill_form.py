"""Every System skill keeps spec-only frontmatter; CORSIA data lives in a ``corsia.yaml`` sidecar.

The Agent Skills checker rejects unknown top-level keys, non-string ``metadata`` values and
flow collections (strictyaml), and nothing else in the repo stops one coming back.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
POINTER = "corsia-data"


def _tracked_system_skills() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "backend/prompts/system/**/SKILL.md"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8")
    return [rel for rel in out.split("\0") if rel and (REPO_ROOT / rel).is_file()]


def _frontmatter(text: str) -> str:
    assert text.startswith("---\n"), "SKILL.md has no frontmatter"
    return text[4 : text.index("\n---", 4)]


def _flow_nodes(node: yaml.Node) -> list[yaml.Node]:
    found = [node] if getattr(node, "flow_style", False) else []
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            found += _flow_nodes(key) + _flow_nodes(value)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            found += _flow_nodes(item)
    return found


def test_the_check_sees_the_system_skills() -> None:
    assert len(_tracked_system_skills()) >= 20


@pytest.mark.parametrize("rel", _tracked_system_skills())
def test_system_skill_frontmatter_is_spec_only_block_style(rel: str) -> None:
    path = REPO_ROOT / rel
    front = _frontmatter(path.read_text(encoding="utf-8"))
    data = yaml.safe_load(front)

    assert set(data) <= SPEC_KEYS, f"{rel}: non-spec top-level keys {sorted(set(data) - SPEC_KEYS)}"
    metadata = data.get("metadata", {})
    assert isinstance(metadata, dict), f"{rel}: metadata must be a mapping"
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()), (
        f"{rel}: metadata values must be strings"
    )
    assert not _flow_nodes(yaml.compose(front)), f"{rel}: frontmatter uses a flow collection"
    if POINTER in metadata:
        assert (path.parent / metadata[POINTER]).is_file(), (
            f"{rel}: {POINTER} names a missing file {metadata[POINTER]}"
        )
