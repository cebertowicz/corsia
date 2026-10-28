"""Fixed outside-PR screening rules over staged diffs."""

import pytest

from events.connectors.github_screen.floor import floor_hits, workflow_paths


def diff(path, added="+ordinary text", metadata=""):
    return f"diff --git a/{path} b/{path}\n{metadata}--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{added}\n"


@pytest.mark.parametrize(("patch", "expected"), [
    (diff(".github/workflows/ci.yml"), "floor.github_dir"),
    (diff("scripts/test.sh"), "floor.ci_file"),
    (diff("skills/review/SKILL.md"), "floor.agent_instructions"),
    (diff("image.png", metadata="Binary files a/image.png and b/image.png differ\n"), "floor.binary"),
    (diff("blob.txt", "+" + "A" * 256), "floor.encoded_blob"),
    (diff("link", metadata="new file mode 120000\n"), "floor.symlink"),
    (diff("file.txt", "+a\u202eb"), "floor.hidden_chars"),
])
def test_fixed_floor_rules(patch, expected):
    assert expected in floor_hits(patch, ci_paths={"scripts/test.sh"})


def test_workflow_literal_tokens_match_changed_paths():
    patch = diff("scripts/test.sh") + diff("scripts/other.sh")
    assert workflow_paths(patch, ['run: "./scripts/test.sh"\n']) == {"scripts/test.sh"}
    assert workflow_paths(patch, ["run: scripts/test.sh.extra\n"]) == set()
    assert workflow_paths(patch, ['run: "$SCRIPT"\n']) == set()


def test_wrapped_encoded_blob_and_short_integrity():
    wrapped = "\n".join("+" + "A" * size for size in (76, 76, 76, 72))
    assert "floor.encoded_blob" in floor_hits(diff("blob.txt", wrapped))
    integrity = diff("package-lock.json", '+"integrity": "sha512-' + "A" * 86 + '=="')
    hits = floor_hits(integrity)
    assert "floor.encoded_blob" not in hits
    assert "floor.ci_file" in hits


@pytest.mark.parametrize("path", ["package.json", "setup.py", "setup.cfg", "pyproject.toml",
                                  "sub/Makefile", "requirements-dev.txt", "yarn.lock"])
def test_build_files_are_ci_files(path):
    assert "floor.ci_file" in floor_hits(diff(path))
