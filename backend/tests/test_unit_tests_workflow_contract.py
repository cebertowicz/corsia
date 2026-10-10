from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/unit-tests.yml"
RUNTIME_REQUIREMENTS = REPO_ROOT / "backend/requirements.txt"
TEST_REQUIREMENTS = REPO_ROOT / "backend/requirements-test.txt"
LOCKED_REQUIREMENTS = REPO_ROOT / "backend/requirements.lock"
E2E_PORT_HELPER = REPO_ROOT / "scripts/assign-e2e-runner-ports"
HISTORICAL_SCENARIO_TESTS = (
    REPO_ROOT / "backend/tests/test_yaml_scenario_loading.py",
    REPO_ROOT / "backend/tests/test_yaml_scenario_solver.py",
)
BACKEND_RUNNER = "runs-on: [self-hosted, macOS, ARM64, mac-studio, backend-l1]"
HOSTED_RUNNER = "runs-on: ubuntu-latest"
# Light checks run on any Mac runner, without taking a backend-l1 or
# ci-integration lane label. GitHub-hosted runners bill paid Actions minutes;
# moving a job there needs the repository owner's explicit approval.
LIGHT_RUNNER = "runs-on: [self-hosted, macOS, ARM64, mac-studio]"
INTEGRATION_RUNNER = "runs-on: [self-hosted, macOS, ARM64, mac-studio, ci-integration]"
LIGHT_WORKFLOW_JOBS = {
    "antipatterns.yml": 1,
    "code-health.yml": 1,
    "docs-drift.yml": 1,
    "lint.yml": 2,
    "large-files.yml": 1,
    "close-on-dev-merge.yml": 1,
}
INTEGRATION_WORKFLOW_JOBS = {
    "e2e.yml": 2,
    "evals.yml": 1,
    "integration.yml": 1,
}


def test_unit_jobs_use_self_hosted_capacity_and_controlled_shards() -> None:
    workflow = WORKFLOW.read_text()
    backend_job = workflow.split("  frontend:", 1)[0]

    assert workflow.count(BACKEND_RUNNER) == 1
    assert workflow.count(LIGHT_RUNNER) == 1
    assert LIGHT_RUNNER in workflow.split("  frontend:", 1)[1]
    assert HOSTED_RUNNER not in workflow
    assert "runs-on: macos-14" not in workflow
    assert "runs-on: self-hosted" not in workflow
    assert "PYTHONUNBUFFERED: '1'" in workflow
    assert "CORSIA_TEST_MODE: '1'" in workflow
    assert 'local isolated_root="$CI_TEST_ROOT/corsia-ci-backend/${shard}"' in workflow
    assert '--basetemp="$isolated_root/pytest"' in workflow
    assert "-o tmp_path_retention_policy=failed" in workflow
    assert 'diskutil mount -mountPoint "$mount_point"' in backend_job
    assert "Mount test RAM disk" in backend_job
    assert "if: always() && env.CI_RAMDISK_DEVICE != ''" in backend_job
    assert "/private/tmp/corsia-ci-" not in workflow
    assert "CORSIA_WORKSPACE=$RUNNER_TEMP" not in workflow
    assert "actions/setup-python" not in backend_job
    assert "actions/setup-node" not in backend_job
    assert "matrix:" not in workflow
    assert "pytest-xdist" not in workflow
    assert "run_backend_shard()" in workflow
    assert "run_backend_shard core-cgr backend/tests/test_[cgr]*.py" in workflow
    assert "run_backend_shard core-aehilsuw backend/tests/test_[aehilsuw]*.py" in workflow
    assert "run_backend_shard remainder backend/tests" in workflow
    # The remainder must skip exactly what the named shards run, so every
    # test file runs once.
    assert "'--ignore-glob=backend/tests/test_[cgr]*.py'" in workflow
    assert "'--ignore-glob=backend/tests/test_[aehilsuw]*.py'" in workflow
    assert workflow.count("--ignore-glob=") == 2
    assert 'pids+=("$!")' in workflow
    assert 'wait "${pids[$index]}"' in workflow
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
    assert 'glob("backend-*.log")' in workflow
    assert "BACKEND_SHARDS" not in workflow
    assert "except (OSError, ET.ParseError)" in workflow
    assert "incomplete_logs = []" in workflow
    assert 'touch "$RUNNER_TEMP/backend-${names[$index]}.log.printed"' in workflow
    assert 'f"::group::Incomplete backend shard {shard} log "' in workflow
    assert 'f"({status}; log follows)"' in workflow
    assert 'if log.with_suffix(".log.printed").exists():' in workflow
    assert 'print(log.read_text(errors="replace"), end="")' in workflow
    assert 'print(f"::warning::Unable to read {log.name}: {error}")' in workflow
    assert "{len(complete_rows)}/{len(shards)} reports" in workflow
    assert ".venv/bin/pip install -r backend/requirements.txt" in workflow
    assert ".venv/bin/pip install -r backend/requirements-test.txt" in workflow
    assert "RUNNER_TOOL_CACHE" not in workflow


def test_l3_enables_contract_suite_for_integration_matrix() -> None:
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/integration.yml"
    ).read_text()

    assert "CORSIA_CONTRACT_SUITE: '1'" in workflow
    assert '-m "integration"' in workflow
    assert "branches: [main, dev]" in workflow


def test_non_backend_workflows_use_their_dedicated_runner_lane() -> None:
    workflow_root = REPO_ROOT / ".github/workflows"

    for filename, expected_jobs in LIGHT_WORKFLOW_JOBS.items():
        workflow = (workflow_root / filename).read_text()
        assert workflow.count(LIGHT_RUNNER) == expected_jobs
        assert HOSTED_RUNNER not in workflow
        assert BACKEND_RUNNER not in workflow
        assert INTEGRATION_RUNNER not in workflow
        assert "actions/setup-python" not in workflow
        assert "/tmp/" not in workflow

    for filename, expected_jobs in INTEGRATION_WORKFLOW_JOBS.items():
        workflow = (workflow_root / filename).read_text()
        assert workflow.count(INTEGRATION_RUNNER) == expected_jobs
        assert "runs-on: self-hosted" not in workflow
        assert BACKEND_RUNNER not in workflow
        assert HOSTED_RUNNER not in workflow


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


def _ramdisk_mount_step() -> str:
    workflow = WORKFLOW.read_text()
    step = "      - name: Mount test RAM disk\n"
    start = workflow.index(step)
    run_start = workflow.index("        run: |\n", start) + len("        run: |\n")
    run_end = workflow.index("\n      - name:", run_start)
    return textwrap.dedent(workflow[run_start:run_end])


def _run_ramdisk_mount(
    tmp_path: Path, hdiutil: str, diskutil: str, *, mount: str = "exit 0\n", tmpdir: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    df = 'echo "Filesystem 512-blocks"; echo "/dev/disk99s1 67108864"\n'
    for name, body in (("hdiutil", hdiutil), ("diskutil", diskutil), ("df", df), ("mount", mount)):
        (fake_bin / name).write_text("#!/usr/bin/env bash\n" + body)
        (fake_bin / name).chmod(0o755)
    github_env = tmp_path / "github-env"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_NAME": "mac-studio-backend-l1-1",
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
        "TMPDIR": f"{tmpdir or tmp_path / 'user-tmp'}/",
        "GITHUB_ENV": str(github_env),
    }
    result = subprocess.run(
        ["/bin/bash", "-c", _ramdisk_mount_step()], text=True, capture_output=True, check=False, env=env,
    )
    lines = github_env.read_text().splitlines() if github_env.exists() else []
    return result, dict(line.split("=", 1) for line in lines)


def test_backend_test_state_uses_a_ram_disk_when_one_mounts(tmp_path: Path) -> None:
    result, values = _run_ramdisk_mount(
        tmp_path,
        hdiutil='[ "$1" = attach ] && echo "/dev/disk99"\nexit 0\n',
        diskutil="exit 0\n",
    )

    assert result.returncode == 0, result.stderr
    assert values == {
        "CI_RAMDISK_DEVICE": "/dev/disk99",
        # Not under /Volumes, which sandboxed git cannot resolve.
        "CI_TEST_ROOT": str(tmp_path / "user-tmp" / "ci-l1-mac-studio-backend-l1-1"),
    }


def test_a_leftover_ram_disk_is_found_at_its_resolved_mount_point(tmp_path: Path) -> None:
    # mount(8) lists /private/var/..., never the /var/... that TMPDIR names.
    real_tmp = tmp_path / "real-tmp"
    real_tmp.mkdir()
    linked_tmp = tmp_path / "linked-tmp"
    linked_tmp.symlink_to(real_tmp)
    leftover = real_tmp / "ci-l1-mac-studio-backend-l1-1"
    detached = tmp_path / "detached"
    result, values = _run_ramdisk_mount(
        tmp_path,
        hdiutil=(
            f'[ "$1" = detach ] && echo "$2" >> "{detached}"\n'
            '[ "$1" = attach ] && echo "/dev/disk99"\nexit 0\n'
        ),
        diskutil="exit 0\n",
        mount=f'echo "/dev/disk98s1 on {leftover} (apfs, local, nodev, nosuid)"\n',
        tmpdir=linked_tmp,
    )

    assert result.returncode == 0, result.stderr
    assert detached.read_text().splitlines() == [str(leftover)]
    assert values["CI_TEST_ROOT"] == str(leftover)


def test_backend_test_state_falls_back_to_runner_temp_without_a_ram_disk(tmp_path: Path) -> None:
    for hdiutil, diskutil in (
        ("exit 1\n", "exit 0\n"),
        ('[ "$1" = attach ] && echo "/dev/disk99"\nexit 0\n', "exit 1\n"),
        ('[ "$1" = attach ] && echo "/dev/disk99"\nexit 0\n', '[ "$1" = mount ] && exit 1\nexit 0\n'),
    ):
        case = tmp_path / str(len(list(tmp_path.iterdir())))
        result, values = _run_ramdisk_mount(case, hdiutil=hdiutil, diskutil=diskutil)

        assert result.returncode == 0, result.stderr
        assert values == {"CI_TEST_ROOT": str(case / "runner-temp")}
        assert "RAM disk unavailable" in result.stdout


def _run_e2e_port_assignment(
    tmp_path: Path,
    runner_name: str,
    occupied_port: int | None = None,
    lsof_exit: int | None = 1,
    lsof_diagnostic: str = "",
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
