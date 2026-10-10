from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _selected_commands(*changed: str) -> list[str]:
    result = subprocess.run(
        ["node", "qa/select-tests.mjs", "--json", *changed],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["commands"]


@pytest.mark.parametrize(
    ("changed", "expected_test"),
    [
        ("docs/observability.md", "tests/test_plan_observability_contract.py"),
        ("archive/README.md", "tests/test_retired_architecture_authority.py"),
    ],
)
def test_contract_changes_select_their_backend_tests(
    changed: str, expected_test: str,
) -> None:
    assert any(expected_test in command for command in _selected_commands(changed))


def test_matched_rule_preserves_baseline_local_gates() -> None:
    assert "bash scripts/check-pr" in _selected_commands("docs/observability.md")


def test_changed_file_discovery_includes_deleted_tracked_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    tracked = repo / "deleted.md"
    tracked.write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "deleted.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=Impact Test",
            "-c", "user.email=impact-test@example.invalid",
            "commit", "-qm", "base",
        ],
        check=True,
    )
    tracked.unlink()

    script = (
        "const { getChangedFiles } = await import(process.argv[1]);"
        "console.log(JSON.stringify(getChangedFiles(process.argv[2])));"
    )
    result = subprocess.run(
        [
            "node", "--input-type=module", "--eval", script,
            (ROOT / "qa/core/utils.mjs").as_uri(), str(repo),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == ["deleted.md"]
