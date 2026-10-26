from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/unit-tests.yml"
RUNTIME_REQUIREMENTS = REPO_ROOT / "backend/requirements.txt"
TEST_REQUIREMENTS = REPO_ROOT / "backend/requirements-test.txt"
LOCKED_REQUIREMENTS = REPO_ROOT / "backend/requirements.lock"
E2E_PORT_HELPER = REPO_ROOT / "scripts/assign-e2e-runner-ports"
RELEVANCE_ACTION = REPO_ROOT / ".github/actions/relevance/action.yml"
RELEVANCE_ACTION_PATH = r"\.github/actions/relevance/action\.yml$"
RELEVANCE_ACTION_PATH_PATTERN = rf"^{RELEVANCE_ACTION_PATH}"
RELEVANCE_CONSUMER_JOBS = {
    "antipatterns.yml": ("antipatterns",),
    "code-health.yml": ("file-sizes",),
    "integration.yml": ("integration", "live-reviewer"),
    "lint.yml": ("ruff", "mypy"),
    "unit-tests.yml": ("backend", "backend-macos", "frontend"),
}
RELEVANCE_PATTERN_ENV = {
    "lint.yml": "LINT_RELEVANCE_PATTERNS",
    "unit-tests.yml": "UNIT_TEST_RELEVANCE_PATTERNS",
}
RELEVANCE_PATTERN_CASES = {
    "antipatterns.yml": {
        "must_run": ("backend/module.py", "frontend/app.ts", "tools/check.py", "scripts/check", "infra/config"),
        "must_skip": ("README.md",),
    },
    "code-health.yml": {
        "must_run": ("root_module.py", "frontend/app.ts", "frontend/App.svelte", "quality/file-size-limits.json"),
        "must_skip": ("README.md",),
    },
    "integration.yml": {
        "must_run": ("backend/module.py", "requirements-test.txt"),
        "must_skip": ("frontend/app.ts",),
    },
    "lint.yml": {
        "must_run": ("backend/module.py", "tools/check.py", "pyproject.toml"),
        "must_skip": ("frontend/app.ts",),
    },
    "unit-tests.yml": {
        "must_run": ("backend/module.py", "frontend/app.ts", "requirements-test.txt", "frontend/package-lock.json"),
        "must_skip": ("README.md",),
    },
}
HISTORICAL_SCENARIO_TESTS = (
    REPO_ROOT / "backend/tests/test_yaml_scenario_loading.py",
    REPO_ROOT / "backend/tests/test_yaml_scenario_solver.py",
)
LINUX_RUNNER = "runs-on: ubuntu-24.04"
MACOS_RUNNER = "runs-on: macos-15"
MACOS_ONLY_TESTS = REPO_ROOT / "backend/tests/macos_only_tests.txt"
# Every job runs on a GitHub-hosted runner. macOS only where a job needs it:
# the L3 contract suite (Seatbelt + Safehouse) and the macOS-only L1 files.
LINUX_WORKFLOW_JOBS = {
    "antipatterns.yml": 2,
    "code-health.yml": 2,
    "docs-drift.yml": 1,
    "lint.yml": 3,
    "large-files.yml": 1,
    "fixed-in-dev.yml": 1,
    "e2e.yml": 2,
    "evals.yml": 1,
}
MACOS_WORKFLOW_JOBS: dict[str, int] = {}
MIXED_RUNNER_WORKFLOW_JOBS = {
    "integration.yml": (1, 2),
}
BLOCKING_CONTEXTS = {
    "antipatterns.yml": {"Anti-pattern detector (error-severity, blocking)"},
    "code-health.yml": {"File size ratchet"},
    "docs-drift.yml": {"Documentation drift check (citations + private-name scan)"},
    "integration.yml": {"Integration tests (L3)"},
    "large-files.yml": {"No file over 500 KB"},
    "lint.yml": {"mypy (regression gate)"},
    "unit-tests.yml": {
        "Backend unit tests",
        "Backend unit tests (macOS-only files)",
        "Frontend unit tests",
    },
}
RELEVANCE_CONSUMER_JOB_COUNTS = {
    "antipatterns.yml": 1,
    "code-health.yml": 1,
    "integration.yml": 2,
    "lint.yml": 2,
    "unit-tests.yml": 3,
}

def _macos_only_files() -> list[str]:
    return [
        line.strip()
        for line in MACOS_ONLY_TESTS.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_unit_jobs_use_hosted_runners_and_controlled_shards() -> None:
    workflow = WORKFLOW.read_text()
    relevance_job = _workflow_job(workflow, "relevance")
    backend_job = _workflow_job(workflow, "backend")
    macos_job = _workflow_job(workflow, "backend-macos")
    frontend_job = _workflow_job(workflow, "frontend")

    for job in (relevance_job, backend_job, frontend_job):
        assert LINUX_RUNNER in job
        assert MACOS_RUNNER not in job
    assert MACOS_RUNNER in macos_job
    assert LINUX_RUNNER not in macos_job
    assert "self-hosted" not in workflow
    assert "hdiutil" not in workflow
    # Linux uses uv's managed CPython so its SQLite matches the Mac's, not the distro's.
    assert "uv venv --python 3.11 --managed-python --seed .venv" in backend_job
    assert "actions/setup-python" not in backend_job
    assert "CORSIA_BACKUP_FREE_FLOOR_BYTES:" in backend_job
    assert "python-version: '3.11'" in macos_job
    assert "scripts/install-ci-safehouse" in macos_job
    assert "PYTHONUNBUFFERED: '1'" in workflow
    assert "CORSIA_TEST_MODE: '1'" in workflow
    assert 'local isolated_root="$CI_TEST_ROOT/corsia-ci-backend/${shard}"' in workflow
    assert '--basetemp="$isolated_root/pytest"' in workflow
    assert "-o tmp_path_retention_policy=failed" in workflow
    assert "Use in-memory test root" in backend_job
    assert "/private/tmp/corsia-ci-" not in workflow
    assert "CORSIA_WORKSPACE=$RUNNER_TEMP" not in workflow
    assert "actions/setup-node" not in backend_job
    assert "fail-fast: false" in backend_job
    assert "shard: [core-cdgmr, remainder, core-aehilostuw]" in backend_job
    assert "pytest-xdist" not in workflow
    assert "run_backend_shard()" in workflow
    assert "mapfile -t cdgmr_files < <(linux_files backend/tests/test_[cdgmr]*.py)" in workflow
    assert "mapfile -t aehilostuw_files < <(linux_files backend/tests/test_[aehilostuw]*.py)" in workflow
    assert 'run_backend_shard core-cdgmr "${cdgmr_files[@]}"' in workflow
    assert 'run_backend_shard core-aehilostuw "${aehilostuw_files[@]}"' in workflow
    assert "run_backend_shard remainder backend/tests" in workflow
    assert '"${macos_ignores[@]}"' in workflow
    # The remainder must skip exactly what the named shards run, so every
    # test file runs once.
    assert "'--ignore-glob=backend/tests/test_[cdgmr]*.py'" in workflow
    assert "'--ignore-glob=backend/tests/test_[aehilostuw]*.py'" in workflow
    assert workflow.count("--ignore-glob=") == 2
    assert 'case "${{ matrix.shard }}" in' in backend_job
    assert 'status=$?' in backend_job
    assert 'exit "$status"' in backend_job
    assert "run_with_test_capacity.py" not in workflow
    assert "backend-full-suite" not in workflow
    assert "--maxfail" not in workflow
    assert "--durations=50 --durations-min=0.25" in workflow
    assert '--junitxml="$RUNNER_TEMP/backend-junit-${shard}.xml"' in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "if: always()" in workflow
    assert "backend-junit-*.xml" in workflow
    assert "backend-*.log" in workflow
    assert "Summarize backend test shards" in workflow
    assert "continue-on-error: true" in workflow
    assert "### Backend test shards" in workflow
    assert "Backend aggregate:" in workflow
    assert 'shards = ["core-cdgmr", "remainder", "core-aehilostuw"]' in workflow
    assert "BACKEND_SHARDS" not in workflow
    assert "except (OSError, ET.ParseError)" in workflow
    assert "incomplete_logs = []" in workflow
    assert '2>&1 | tee "$RUNNER_TEMP/backend-${shard}.log"' in backend_job
    assert 'f"::group::Incomplete backend shard {shard} log "' in workflow
    assert 'f"({status}; log follows)"' in workflow
    assert 'name: backend-test-report-${{ github.run_id }}-${{ matrix.shard }}' in backend_job
    assert 'print(log.read_text(errors="replace"), end="")' in workflow
    assert 'print(f"::warning::Unable to read {log.name}: {error}")' in workflow
    assert "{len(complete_rows)}/{len(shards)} reports" in workflow
    assert ".venv/bin/pip install -r backend/requirements.txt" in workflow
    assert ".venv/bin/pip install -r backend/requirements-test.txt" in workflow
    assert "RUNNER_TOOL_CACHE" not in workflow
    aggregate = _workflow_job(workflow, "backend-aggregate")
    assert "needs: [relevance, backend]" in aggregate
    assert "if: ${{ !cancelled() }}" in aggregate
    assert "name: Backend unit tests\n" in aggregate
    assert "${{ needs.backend.result }}" in aggregate
    assert '[ "$BACKEND_RESULT" != success ]' in aggregate
    assert '[ "$RELEVANCE_RESULT" != success ]' in aggregate
    assert '[ "$RELEVANT" = true ]' in aggregate
    assert "pattern: backend-test-report-${{ github.run_id }}-*" in aggregate


def test_backend_aggregate_fails_closed_for_relevant_shards() -> None:
    aggregate = _workflow_job(WORKFLOW.read_text(), "backend-aggregate")
    step = aggregate.split("      - name: Require successful backend shards\n", 1)[1]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    cases = (
        ("success", "true", "success", 0),
        ("success", "true", "failure", 1),
        ("success", "true", "cancelled", 1),
        ("success", "true", "skipped", 1),
        ("success", "false", "skipped", 0),
        ("failure", "", "skipped", 1),
    )
    for relevance, relevant, backend, expected in cases:
        result = subprocess.run(
            ["/bin/bash", "-e", "-c", script],
            env={**os.environ, "RELEVANCE_RESULT": relevance,
                 "RELEVANT": relevant, "BACKEND_RESULT": backend},
            text=True, capture_output=True, check=False,
        )
        assert result.returncode == expected, (relevance, relevant, backend, result.stdout)


def test_l3_enables_contract_suite_for_integration_matrix() -> None:
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/integration.yml"
    ).read_text()

    assert "CORSIA_CONTRACT_SUITE: '1'" in workflow
    assert '-m "integration"' in workflow
    assert "branches: [main, dev]" in workflow


def test_blocking_contexts_are_created_for_every_pull_request() -> None:
    workflow_root = REPO_ROOT / ".github/workflows"

    for filename, contexts in BLOCKING_CONTEXTS.items():
        workflow = (workflow_root / filename).read_text()
        for context in contexts:
            assert f"\n    name: {context}\n" in workflow, (filename, context)

    for filename, expected_consumer_jobs in RELEVANCE_CONSUMER_JOB_COUNTS.items():
        workflow = (workflow_root / filename).read_text()
        pull_request = workflow.split("  pull_request:\n", 1)[1].split(
            "  workflow_dispatch:", 1
        )[0]
        assert "branches: [main, dev]" in pull_request, filename
        assert "paths:" not in pull_request, filename
        assert "paths-ignore:" not in pull_request, filename
        assert workflow.count("uses: ./.github/actions/relevance") == 1, filename
        assert workflow.count("\n    needs: relevance\n") == expected_consumer_jobs, filename


def test_live_reviewer_proof_is_a_separate_static_workflow_dispatch_job() -> None:
    workflow = (REPO_ROOT / ".github/workflows/integration.yml").read_text()
    integration = _workflow_job(workflow, "integration")
    live_reviewer = _workflow_job(workflow, "live-reviewer")

    assert "name: Integration tests (L3)" in integration
    assert "timeout-minutes: 15" in integration
    assert "Run integration tests" in integration
    assert "Run bounded live Haiku reviewer proof" not in integration
    assert "!(github.event_name == 'workflow_dispatch' && inputs.live_reviewer)" in integration

    assert "name: Live host reviewer proof (Haiku)" in live_reviewer
    assert "github.event_name == 'workflow_dispatch'" in live_reviewer
    assert "inputs.live_reviewer" in live_reviewer
    assert "timeout-minutes: 30" in live_reviewer
    assert "Run bounded live Haiku reviewer proof" in live_reviewer


def _workflow_job(workflow: str, job_name: str) -> str:
    start = workflow.index(f"  {job_name}:\n")
    next_job = re.search(r"^  [A-Za-z][A-Za-z0-9_-]*:\n", workflow[start + 1 :], re.MULTILINE)
    end = start + 1 + next_job.start() if next_job else len(workflow)
    return workflow[start:end]


def _workflow_jobs(workflow: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^  ([A-Za-z][A-Za-z0-9_-]*):\n", workflow, re.MULTILINE))
    jobs = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(workflow)
        jobs.append((match.group(1), workflow[match.start() : end]))
    return jobs


def _workflow_pattern_env_value(workflow: str, env_name: str) -> str:
    match = re.search(rf"^env:\n  {env_name}: '([^']+)'$", workflow, re.MULTILINE)
    assert match, env_name
    return match.group(1)


def _relevance_pattern(workflow: str) -> str:
    preflight = _workflow_job(workflow, "relevance")
    match = re.search(r"^          patterns: (.+)$", preflight, re.MULTILINE)
    assert match, "relevance"
    pattern_reference = match.group(1)
    env_reference = re.fullmatch(r"\$\{\{ env\.([A-Z_]+) \}\}", pattern_reference)
    return _workflow_pattern_env_value(workflow, env_reference.group(1)) if env_reference else pattern_reference.strip("'")


def _grep_matches(pattern: str, changed_path: str) -> int:
    return subprocess.run(
        ["grep", "-Eq", pattern], input=f"{changed_path}\n", text=True, check=False
    ).returncode


def test_relevance_action_is_the_shared_fail_closed_producer_for_every_gated_job() -> None:
    action = RELEVANCE_ACTION.read_text()
    assert "event_name:" in action
    assert "base_ref:" in action
    assert "patterns:" in action
    assert "value: ${{ steps.detect.outputs.relevant }}" in action
    assert "git fetch --no-tags origin" in action
    assert "git merge-base" in action
    assert "git diff --name-only --no-renames -z" in action
    assert 'echo "relevant=false"' in action

    workflow_root = REPO_ROOT / ".github/workflows"
    actual_consumers = {
        path.name: tuple(
            name for name, job in _workflow_jobs(path.read_text()) if "needs: relevance" in job
        )
        for path in sorted(workflow_root.glob("*.yml"))
    }
    actual_consumers = {filename: jobs for filename, jobs in actual_consumers.items() if jobs}
    assert actual_consumers == RELEVANCE_CONSUMER_JOBS

    for filename, jobs in actual_consumers.items():
        workflow = (workflow_root / filename).read_text()
        preflight = _workflow_job(workflow, "relevance")
        assert "uses: ./.github/actions/relevance" in preflight, filename
        assert workflow.count("uses: ./.github/actions/relevance") == 1, filename
        assert "runs-on: ubuntu-24.04" in preflight, filename
        assert "fetch-depth: 0" in preflight, filename
        assert "id: detect" in preflight, filename
        assert "event_name: ${{ github.event_name }}" in preflight, filename
        assert "base_ref: ${{ github.base_ref }}" in preflight, filename
        assert "relevant: ${{ steps.detect.outputs.relevant }}" in preflight, filename
        assert RELEVANCE_ACTION_PATH in _relevance_pattern(workflow), filename
        for job_name in jobs:
            job = _workflow_job(workflow, job_name)
            assert "needs: relevance" in job, (filename, job_name)
            assert "if: ${{ !cancelled() && (" in job, (filename, job_name)
            assert "needs.relevance.result != 'success'" in job, (filename, job_name)
            assert "needs.relevance.outputs.relevant == 'true'" in job, (filename, job_name)
            steps = job.split("    steps:\n", 1)[1]
            assert steps.startswith(
                "      - name: Fail when relevance preflight did not succeed\n"
                "        if: ${{ needs.relevance.result != 'success' }}\n"
                "        run: |\n"
            ), (filename, job_name)
            assert "exit 1" in steps.split("\n      - ", 1)[0], (filename, job_name)
            assert "fetch-depth: 1" in job, (filename, job_name)
            assert "fetch-depth: 0" not in job, (filename, job_name)


def test_relevance_patterns_have_one_workflow_local_source() -> None:
    workflow_root = REPO_ROOT / ".github/workflows"
    for filename, env_name in RELEVANCE_PATTERN_ENV.items():
        workflow = (workflow_root / filename).read_text()
        pattern = _workflow_pattern_env_value(workflow, env_name)
        assert workflow.count(pattern) == 1, filename
        expected_reference = f"patterns: ${{{{ env.{env_name} }}}}"
        assert expected_reference in _workflow_job(workflow, "relevance"), filename
        for job_name in RELEVANCE_CONSUMER_JOBS[filename]:
            assert expected_reference not in _workflow_job(workflow, job_name), (filename, job_name)


def test_every_relevance_consumer_pattern_matches_its_required_paths() -> None:
    workflow_root = REPO_ROOT / ".github/workflows"
    for filename, jobs in RELEVANCE_CONSUMER_JOBS.items():
        workflow = (workflow_root / filename).read_text()
        cases = RELEVANCE_PATTERN_CASES[filename]
        pattern = _relevance_pattern(workflow)
        for job_name in jobs:
            for changed_path in (*cases["must_run"], f".github/workflows/{filename}", ".github/actions/relevance/action.yml"):
                assert _grep_matches(pattern, changed_path) == 0, (filename, job_name, changed_path, pattern)
            for changed_path in cases["must_skip"]:
                assert _grep_matches(pattern, changed_path) == 1, (filename, job_name, changed_path, pattern)


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _scratch_relevance_repo(tmp_path: Path, changed_path: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "ci@example.test")
    _run_git(repo, "config", "user.name", "CI test")
    (repo / "README.md").write_text("base\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "base")
    _run_git(repo, "branch", "-M", "main")
    _run_git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    changed = repo / changed_path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("change\n")
    _run_git(repo, "add", changed_path)
    _run_git(repo, "commit", "-m", "change")
    return repo


def _run_relevance_action(
    repo: Path, tmp_path: Path, *, base_ref: str, patterns: str, event_name: str = "pull_request"
) -> tuple[subprocess.CompletedProcess[str], Path]:
    action = RELEVANCE_ACTION.read_text()
    run = textwrap.dedent(action.split("      run: |\n", 1)[1])
    github_output = tmp_path / "github-output"
    github_output.parent.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "EVENT_NAME": event_name,
        "BASE_REF": base_ref,
        "PATTERNS": patterns,
        "GITHUB_OUTPUT": str(github_output),
    }
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", run],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, github_output


def test_relevance_action_fails_closed_and_emits_only_successful_results(tmp_path: Path) -> None:
    bogus_repo = _scratch_relevance_repo(tmp_path / "bogus", "README.md")
    bogus, bogus_output = _run_relevance_action(
        bogus_repo, tmp_path / "bogus", base_ref="missing", patterns="^backend/"
    )
    assert bogus.returncode != 0
    assert "::error::unable to fetch pull_request base ref origin/missing" in bogus.stderr
    assert not bogus_output.exists() or not bogus_output.read_text()

    unrelated_repo = _scratch_relevance_repo(tmp_path / "unrelated", "README.md")
    unrelated, unrelated_output = _run_relevance_action(
        unrelated_repo, tmp_path / "unrelated", base_ref="main", patterns="^backend/"
    )
    assert unrelated.returncode == 0, unrelated.stderr
    assert unrelated_output.read_text() == "relevant=false\n"

    matching_repo = _scratch_relevance_repo(tmp_path / "matching", "backend/example.py")
    matching, matching_output = _run_relevance_action(
        matching_repo, tmp_path / "matching", base_ref="main", patterns="^backend/"
    )
    assert matching.returncode == 0, matching.stderr
    assert matching_output.read_text() == "relevant=true\n"

    # A matching path comes first and is followed by more than a typical pipe
    # buffer of unrelated names. A producer-side pipe can receive SIGPIPE when
    # grep exits after this early match under `set -o pipefail`.
    large_repo = _scratch_relevance_repo(tmp_path / "large", "backend/match.py")
    bulk_dir = large_repo / "docs"
    bulk_dir.mkdir()
    for index in range(2_048):
        (bulk_dir / f"unrelated-change-{index:05d}-abcdefghijklmnopqrstuvwxyz.md").write_text("x\n")
    _run_git(large_repo, "add", "docs")
    _run_git(large_repo, "commit", "-m", "large unrelated change set")
    changed_paths = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        cwd=large_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert changed_paths.splitlines()[0] == "backend/match.py"
    assert len(changed_paths) > 65_536
    large_match, large_match_output = _run_relevance_action(
        large_repo, tmp_path / "large", base_ref="main", patterns="^backend/"
    )
    assert large_match.returncode == 0, large_match.stderr
    assert large_match_output.read_text() == "relevant=true\n"

    action_repo = _scratch_relevance_repo(
        tmp_path / "action", ".github/actions/relevance/action.yml"
    )
    action_change, action_change_output = _run_relevance_action(
        action_repo,
        tmp_path / "action",
        base_ref="main",
        patterns=RELEVANCE_ACTION_PATH_PATTERN,
    )
    assert action_change.returncode == 0, action_change.stderr
    assert action_change_output.read_text() == "relevant=true\n"

    renamed_repo = _scratch_relevance_repo(tmp_path / "renamed", "backend/example.py")
    _run_git(renamed_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (renamed_repo / "docs").mkdir()
    _run_git(renamed_repo, "mv", "backend/example.py", "docs/example.py")
    _run_git(renamed_repo, "commit", "-m", "rename backend example")
    renamed, renamed_output = _run_relevance_action(
        renamed_repo, tmp_path / "renamed", base_ref="main", patterns="^backend/"
    )
    assert renamed.returncode == 0, renamed.stderr
    assert renamed_output.read_text() == "relevant=true\n"

    renamed_to_backend_repo = _scratch_relevance_repo(tmp_path / "renamed-to-backend", "docs/example.py")
    _run_git(renamed_to_backend_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (renamed_to_backend_repo / "backend").mkdir()
    _run_git(renamed_to_backend_repo, "mv", "docs/example.py", "backend/example.py")
    _run_git(renamed_to_backend_repo, "commit", "-m", "rename docs example")
    renamed_to_backend, renamed_to_backend_output = _run_relevance_action(
        renamed_to_backend_repo, tmp_path / "renamed-to-backend", base_ref="main", patterns="^backend/"
    )
    assert renamed_to_backend.returncode == 0, renamed_to_backend.stderr
    assert renamed_to_backend_output.read_text() == "relevant=true\n"

    unicode_path = "backend/résumé notes.py"
    unicode_repo = _scratch_relevance_repo(tmp_path / "unicode", unicode_path)
    _run_git(unicode_repo, "config", "core.quotePath", "true")
    unicode_change, unicode_output = _run_relevance_action(
        unicode_repo,
        tmp_path / "unicode",
        base_ref="main",
        patterns=r"^backend/résumé notes\.py$",
    )
    assert unicode_change.returncode == 0, unicode_change.stderr
    assert unicode_output.read_text() == "relevant=true\n"

    dispatched, dispatched_output = _run_relevance_action(
        matching_repo,
        tmp_path / "dispatched",
        base_ref="",
        patterns="[",
        event_name="workflow_dispatch",
    )
    assert dispatched.returncode == 0, dispatched.stderr
    assert dispatched_output.read_text() == "relevant=true\n"


def test_relevance_action_fails_closed_when_diff_fails(tmp_path: Path) -> None:
    repo = _scratch_relevance_repo(tmp_path, "backend/example.py")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"diff\" ]]; then exit 42; fi\n"
        f"exec {real_git} \"$@\"\n"
    )
    fake_git.chmod(0o755)

    action = RELEVANCE_ACTION.read_text()
    run = textwrap.dedent(action.split("      run: |\n", 1)[1])
    github_output = tmp_path / "github-output"
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", run],
        cwd=repo,
        env={
            **os.environ,
            "EVENT_NAME": "pull_request",
            "BASE_REF": "main",
            "PATTERNS": "^backend/",
            "GITHUB_OUTPUT": str(github_output),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert not github_output.exists() or not github_output.read_text()


def test_every_workflow_uses_its_hosted_runner() -> None:
    workflow_root = REPO_ROOT / ".github/workflows"

    for path in sorted(workflow_root.glob("*.yml")):
        workflow = path.read_text()
        assert "self-hosted" not in workflow, path.name
        assert "mac-python" not in workflow, path.name
        assert "/opt/homebrew" not in workflow, path.name

    for filename, expected_jobs in LINUX_WORKFLOW_JOBS.items():
        workflow = (workflow_root / filename).read_text()
        assert workflow.count(LINUX_RUNNER) == expected_jobs, filename
        assert MACOS_RUNNER not in workflow, filename

    for filename, expected_jobs in MACOS_WORKFLOW_JOBS.items():
        workflow = (workflow_root / filename).read_text()
        assert workflow.count(MACOS_RUNNER) == expected_jobs, filename
        assert LINUX_RUNNER not in workflow, filename

    for filename, (linux_jobs, macos_jobs) in MIXED_RUNNER_WORKFLOW_JOBS.items():
        workflow = (workflow_root / filename).read_text()
        assert workflow.count(LINUX_RUNNER) == linux_jobs, filename
        assert workflow.count(MACOS_RUNNER) == macos_jobs, filename

    integration = (workflow_root / "integration.yml").read_text()
    assert "scripts/install-ci-safehouse" in integration
    assert "CORSIA_REQUIRE_SAFEHOUSE: '1'" in integration


def test_macos_only_files_run_exactly_once() -> None:
    files = _macos_only_files()
    assert files, "macos_only_tests.txt lists no files"
    assert len(files) == len(set(files))
    for path in files:
        assert (REPO_ROOT / path).is_file(), path
        assert path.startswith("backend/tests/test_") and path.endswith(".py"), path

    workflow = WORKFLOW.read_text()
    backend_job = workflow.split("  backend-macos:", 1)[0]
    macos_job = workflow.split("  backend-macos:", 1)[1].split("  frontend:", 1)[0]
    # Linux skips the listed files (explicit globs filtered, remainder ignored);
    # the macOS job runs exactly the listed files.
    assert "backend/tests/macos_only_tests.txt" in backend_job
    assert 'macos_ignores+=("--ignore=$path")' in backend_job
    assert "backend/tests/macos_only_tests.txt" in macos_job
    assert '"${files[@]}"' in macos_job
    assert '-m "not integration and not cloud_required"' in macos_job


def test_linux_shard_file_filter_drops_only_listed_files(tmp_path: Path) -> None:
    workflow = WORKFLOW.read_text()
    start = workflow.index("          linux_files() {\n")
    end = workflow.index("          }\n", start) + len("          }\n")
    function = textwrap.dedent(workflow[start:end])
    script = (
        "macos_only=(backend/tests/test_a.py backend/tests/test_c.py)\n"
        + function
        + "linux_files backend/tests/test_a.py backend/tests/test_b.py backend/tests/test_c.py\n"
    )
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", script], text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["backend/tests/test_b.py"]

def test_backend_ci_runs_the_contract_suite() -> None:
    workflow = WORKFLOW.read_text()
    backend_job = workflow.split("  frontend:", 1)[0].split("  backend:", 1)[1]

    assert "CORSIA_CONTRACT_SUITE: '1'" in backend_job


def test_runtime_image_dependency_is_declared() -> None:
    runtime_requirements = RUNTIME_REQUIREMENTS.read_text().casefold().splitlines()
    locked_requirements = LOCKED_REQUIREMENTS.read_text().casefold().splitlines()

    assert "pillow==12.2.0" in runtime_requirements
    assert "pillow==12.2.0" in locked_requirements


def _requirement_names(path: Path) -> set[str]:
    """Distribution names declared in a requirements file, without versions.

    Parsed rather than substring-matched: package names also appear in these
    files' explanatory comments, so a whole-file ``in`` check passes even when
    the declarations themselves are gone.
    """
    names = set()
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        names.add(re.split(r"[=<>~!\[;]", stripped, maxsplit=1)[0].strip().casefold())
    return names


def test_the_test_dependency_set_has_exactly_one_owner() -> None:
    """Every workflow that runs or type-checks the tests installs one file.

    mypy type-checks ``backend/tests/``, so ``lint.yml`` needs these too: without
    them pytest resolves to ``Any`` under ``ignore_missing_imports``, mypy cannot
    see that ``pytest.fail`` is ``NoReturn``, and it reports missing returns in
    helpers that never return. Those errors appear only in CI, so they land on
    whichever PR happens to be open rather than on the change that caused them.

    A literal list repeated per workflow is what let the environments drift in
    the first place, so the list living in exactly one place is the property
    under test -- not merely that some list exists. The expected packages are
    therefore never restated here: both sets are read from their own files, so
    this test cannot itself become the second owner it exists to forbid.

    Scoped to the shared ``.venv``, which is the environment that runs and
    type-checks ``backend/tests/``, because that is the environment whose
    dependency set can drift. A purpose-built throwaway venv for a different
    test tree is a separate environment, not a second owner of this one:
    ``code-health.yml`` deliberately builds one to run ``scripts/tests/``,
    which ``backend/pytest.ini`` never collects. Widening this scan to every
    ``pip`` invocation would fail that legitimate line.
    """
    declared = _requirement_names(TEST_REQUIREMENTS)
    assert declared, f"{TEST_REQUIREMENTS.name} declares no packages"

    # requirements.txt is installed before this file in every workflow that
    # uses it, so anything declared in both is a second owner that can only
    # drift from the pinned runtime version.
    also_runtime = declared & _requirement_names(RUNTIME_REQUIREMENTS)
    assert not also_runtime, (
        f"{sorted(also_runtime)} are declared in both {TEST_REQUIREMENTS.name} "
        f"and {RUNTIME_REQUIREMENTS.name}. The runtime file is installed first "
        "everywhere, so remove the test-file declaration."
    )

    workflow_root = REPO_ROOT / ".github/workflows"
    for path in sorted(workflow_root.glob("*.yml")):
        for line in path.read_text().splitlines():
            lowered = line.casefold()
            if ".venv/bin/pip install" not in lowered or " -r " in lowered:
                continue
            for package in declared:
                assert package not in lowered, (
                    f"{path.name} installs '{package}' into the shared .venv "
                    f"inline: {line.strip()!r}. Add it to "
                    "backend/requirements-test.txt and install that file "
                    "instead, so one owner cannot drift from another."
                )


def test_historical_scenario_tests_require_explicit_opt_in() -> None:
    for path in HISTORICAL_SCENARIO_TESTS:
        source = path.read_text()
        assert "CORSIA_SPRINT1_SCENARIOS_DIR" in source
        assert "Path.home()" not in source


def _test_root_step() -> str:
    workflow = WORKFLOW.read_text()
    step = "      - name: Use in-memory test root\n"
    start = workflow.index(step)
    run_start = workflow.index("        run: |\n", start) + len("        run: |\n")
    run_end = workflow.index("\n      - name:", run_start)
    return textwrap.dedent(workflow[run_start:run_end])


def test_backend_test_root_is_tmpfs_or_runner_temp(tmp_path: Path) -> None:
    github_env = tmp_path / "github-env"
    env = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
        "GITHUB_ENV": str(github_env),
    }
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", _test_root_step()], text=True, capture_output=True, check=False, env=env,
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    if Path("/dev/shm").is_dir() and os.access("/dev/shm", os.W_OK):
        assert values == {"CI_TEST_ROOT": "/dev/shm/corsia-ci-l1"}
    else:
        assert values == {"CI_TEST_ROOT": str(tmp_path / "runner-temp")}
        assert "/dev/shm unavailable" in result.stdout


def _run_e2e_port_assignment(
    tmp_path: Path,
    runner_name: str,
    occupied_port: int | None = None,
    lsof_exit: int | None = 1,
    lsof_diagnostic: str = "",
    runner_environment: str = "self-hosted",
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    lsof_log = tmp_path / "lsof.log"
    if lsof_exit is not None:
        (fake_bin / "lsof").write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$FAKE_LSOF_LOG\"\n"
            "if [ -n \"${FAKE_LSOF_DIAGNOSTIC:-}\" ]; then printf '%s\\n' \"$FAKE_LSOF_DIAGNOSTIC\" >&2; fi\n"
            "if [ -n \"${FAKE_OCCUPIED_PORT:-}\" ] && [[ \"$*\" == *\":${FAKE_OCCUPIED_PORT}\"* ]]; then printf '%s\\n' 999; exit 0; fi\n"
            "exit \"$FAKE_LSOF_EXIT\"\n",
        )
        (fake_bin / "lsof").chmod(0o755)
    github_env = tmp_path / "github-env"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}" if lsof_exit is not None else str(fake_bin),
        "RUNNER_NAME": runner_name,
        "RUNNER_ENVIRONMENT": runner_environment,
        "GITHUB_ENV": str(github_env),
        "FAKE_LSOF_LOG": str(lsof_log),
        "FAKE_LSOF_DIAGNOSTIC": lsof_diagnostic,
    }
    if lsof_exit is not None:
        env["FAKE_LSOF_EXIT"] = str(lsof_exit)
    if occupied_port is not None:
        env["FAKE_OCCUPIED_PORT"] = str(occupied_port)
    result = subprocess.run(
        [str(E2E_PORT_HELPER)], text=True, capture_output=True, check=False, env=env,
    )
    lines = github_env.read_text().splitlines() if github_env.exists() else []
    return result, lines, lsof_log.read_text() if lsof_log.exists() else ""


def test_e2e_port_helper_uses_disjoint_slot_ports_and_refuses_before_e2e_local(
    tmp_path: Path,
) -> None:
    e2e_workflow = (REPO_ROOT / ".github/workflows/e2e.yml").read_text()
    assert "run: scripts/assign-e2e-runner-ports" in e2e_workflow
    assert "group: e2e-${{ github.ref }}" in e2e_workflow
    assert "cancel-in-progress: true" in e2e_workflow
    assigned: set[int] = set()

    for slot in range(1, 5):
        result, lines, lsof_log = _run_e2e_port_assignment(
            tmp_path / str(slot), f"mac-studio-corsia-runner-{slot}",
        )
        assert result.returncode == 0, result.stderr
        values = dict(line.split("=", 1) for line in lines)
        pair = {int(values["CORSIA_E2E_BACKEND_PORT"]), int(values["CORSIA_E2E_FRONTEND_PORT"])}
        assert pair == {32000 + 2 * (slot - 1), 32001 + 2 * (slot - 1)}
        assert not pair & {3110, 5178}
        assert not pair & assigned
        assigned |= pair
        assert lsof_log.count("-iTCP:") == 2

    hosted, lines, lsof_log = _run_e2e_port_assignment(
        tmp_path / "hosted", "GitHub Actions 7", runner_environment="github-hosted",
    )
    assert hosted.returncode == 0, hosted.stderr
    assert dict(line.split("=", 1) for line in lines) == {
        "CORSIA_E2E_BACKEND_PORT": "32000", "CORSIA_E2E_FRONTEND_PORT": "32001",
    }
    assert lsof_log.count("-iTCP:") == 2

    unknown, lines, lsof_log = _run_e2e_port_assignment(tmp_path / "unknown", "unverified")
    assert unknown.returncode != 0
    assert "Unexpected ci-integration runner" in unknown.stderr
    assert lines == []
    assert lsof_log == ""

    occupied, lines, lsof_log = _run_e2e_port_assignment(
        tmp_path / "occupied", "mac-studio-corsia-runner-2", occupied_port=32002,
    )
    assert occupied.returncode != 0
    assert "already listening (PID(s): 999)" in occupied.stderr
    assert lines == []
    assert "-t -nP -iTCP:32002" in lsof_log

    frontend_occupied, lines, lsof_log = _run_e2e_port_assignment(
        tmp_path / "frontend-occupied", "mac-studio-corsia-runner-2", occupied_port=32003,
    )
    assert frontend_occupied.returncode != 0
    assert "already listening (PID(s): 999)" in frontend_occupied.stderr
    assert lines == []
    assert "-t -nP -iTCP:32003" in lsof_log

    unexpected, lines, lsof_log = _run_e2e_port_assignment(
        tmp_path / "unexpected", "mac-studio-corsia-runner-1", lsof_exit=2,
        lsof_diagnostic="lsof: unsupported probe",
    )
    assert unexpected.returncode != 0
    assert "Could not inspect assigned E2E port :32000 (lsof exit 2)" in unexpected.stderr
    assert lines == []
    assert "-iTCP:32000" in lsof_log

    denied, lines, lsof_log = _run_e2e_port_assignment(
        tmp_path / "denied", "mac-studio-corsia-runner-1",
        lsof_diagnostic="lsof: can't get PID byte count: Operation not permitted",
    )
    assert denied.returncode != 0
    assert "Could not inspect assigned E2E port :32000" in denied.stderr
    assert lines == []
    assert "-iTCP:32000" in lsof_log

    missing, lines, lsof_log = _run_e2e_port_assignment(
        tmp_path / "missing", "mac-studio-corsia-runner-1", lsof_exit=None,
    )
    assert missing.returncode != 0
    assert "Could not inspect assigned E2E port :32000" in missing.stderr
    assert lines == []
    assert lsof_log == ""
