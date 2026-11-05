"""Backend-private launch files live in 0700 state directories no sandbox can reach."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid

import pytest

FULL_UUID = "0b2c4e6a-1d3f-4a5b-8c7d-9e0f1a2b3c4d"


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    from infra import config

    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr(config, "STATE_ROOT", root)
    return root


@pytest.fixture
def short_system_temp():
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        yield Path(directory)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_private_runtime_dir_is_0700_under_the_state_root(tmp_path) -> None:
    from tools.private_runtime_files import private_runtime_dir

    directory = private_runtime_dir("shell-envelopes", tmp_path / "state")
    assert directory == tmp_path / "state" / "runtime" / "shell-envelopes"
    assert _mode(directory) == 0o700

    directory.chmod(0o755)
    assert _mode(private_runtime_dir("shell-envelopes", tmp_path / "state")) == 0o700


def test_every_writer_uses_the_one_private_helper(state_root, monkeypatch) -> None:
    from subagents.cc_runtime_helpers import _write_seed_file
    from subagents.harness_tool_bridge import write_cc_mcp_config
    from tools.safehouse_launch import mcp_envelope_dir
    from tools.shell_envelope import Envelope, _write_envelope

    runtime = state_root / "runtime"
    legacy_temp = state_root / "legacy-temp"
    legacy_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(legacy_temp))

    config = Path(write_cc_mcp_config(thread_id=FULL_UUID, tool_token="synthetic-test-token"))
    seed = Path(_write_seed_file(FULL_UUID, "synthetic seed"))
    profile, _argv = _write_envelope(
        "true", state_root,
        Envelope(lambda *_a, **_k: "(version 1)", lambda command, *_a, **_k: ["safehouse", command]),
    )
    mcp = mcp_envelope_dir(state_root)

    assert config.parent == runtime / "harness-mcp" and _mode(config) == 0o600
    assert seed.parent == runtime / "cc-seeds"
    assert profile.parent.resolve() == (runtime / "shell-envelopes").resolve()
    assert mcp == runtime / "mcp-envelopes"
    assert all(_mode(path) == 0o700 for path in (config.parent, seed.parent, profile.parent, mcp))
    assert list(legacy_temp.iterdir()) == []


def test_startup_sweep_empties_only_the_private_launch_directories(state_root) -> None:
    from tools.private_runtime_files import private_runtime_dir, sweep_private_runtime_files

    stale = [
        private_runtime_dir(name) / filename
        for name, filename in (
            ("shell-envelopes", "corsia-shell-envelope-a.sb"),
            ("harness-mcp", "cc-a.json"),
            ("cc-seeds", "cc-seed-a-b.txt"),
        )
    ]
    live_mcp = private_runtime_dir("mcp-envelopes") / "corsia-mcp-envelope-a.sb"
    elsewhere = state_root / "runtime" / "corsia-restart.signal"
    for path in (*stale, live_mcp, elsewhere):
        path.write_text("x")

    assert sweep_private_runtime_files() == len(stale)
    assert not any(path.exists() for path in stale)
    assert live_mcp.exists() and elsewhere.exists()


def test_legacy_sweep_deletes_only_the_exact_prefixes(tmp_path) -> None:
    from tools.private_runtime_files import sweep_legacy_temp_files

    root = tmp_path / "T"
    harness = root / "corsia-harness-mcp"
    harness.mkdir(parents=True)
    doomed = [
        root / "corsia-shell-envelope-ab12.sb",
        root / "corsia-controlled-git-ab12.sb",
        root / "corsia-mcp-envelope-ab12.sb",
        root / f"cc-seed-{FULL_UUID[:8]}-ab12.txt",
        harness / "cc-ab12.json",
    ]
    kept = [
        root / "corsia-shell-envelope-ab12.txt",
        root / "corsia-other.sb",
        root / "other-corsia-shell-envelope-ab12.sb",
        root / "cc-seed-ab12.json",
        root / "unrelated.sb",
        harness / "notes.txt",
    ]
    for path in (*doomed, *kept):
        path.write_text("x")
    outside = tmp_path / "outside.sb"
    outside.write_text("x")
    linked = root / "corsia-shell-envelope-link.sb"
    linked.symlink_to(outside)

    assert sweep_legacy_temp_files([root]) == len(doomed)
    assert not any(path.exists() for path in doomed)
    assert all(path.exists() for path in kept) and outside.exists() and linked.is_symlink()
    assert harness.is_dir()


def test_legacy_sweep_removes_the_empty_harness_directory(tmp_path) -> None:
    from tools.private_runtime_files import sweep_legacy_temp_files

    harness = tmp_path / "corsia-harness-mcp"
    harness.mkdir()
    (harness / "cc-a.json").write_text("x")

    sweep_legacy_temp_files([tmp_path])

    assert not harness.exists()


def test_sweep_warns_with_path_and_errno_when_an_unlink_fails(state_root, monkeypatch, caplog) -> None:
    import errno

    from tools import private_runtime_files

    stale = private_runtime_files.private_runtime_dir("shell-envelopes") / "corsia-shell-envelope-a.sb"
    stale.write_text("x")

    real_unlink = os.unlink

    def refuse(path, *args, **kwargs):
        if os.fspath(path) != os.fspath(stale):
            return real_unlink(path, *args, **kwargs)
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(private_runtime_files.os, "unlink", refuse)
    with caplog.at_level("WARNING", logger=private_runtime_files.log.name):
        assert private_runtime_files.sweep_private_runtime_files() == 0

    assert stale.exists()
    assert any(
        record.levelname == "WARNING" and str(stale) in record.getMessage()
        and f"errno={errno.EPERM}" in record.getMessage()
        for record in caplog.records
    )


def test_startup_leaves_the_real_temp_roots_alone_under_test(state_root, monkeypatch) -> None:
    from tools import private_runtime_files

    swept: list[bool] = []

    def legacy_sweep() -> int:
        swept.append(True)
        return 0

    monkeypatch.setattr(private_runtime_files, "sweep_legacy_temp_files", legacy_sweep)

    private_runtime_files.sweep_at_startup()
    assert swept == []

    monkeypatch.delenv("CORSIA_TEST_MODE")
    private_runtime_files.sweep_at_startup()
    assert swept == [True]


def test_backend_startup_empties_the_private_launch_directories(state_root, isolated_db, monkeypatch, tmp_path) -> None:
    import app_lifecycle
    import core.project_path_conversion as conversion
    import core.system_projection as system_projection
    import infra.config as config
    import infra.guidance_git as guidance_git
    import infra.startup_prerequisites as prerequisites
    import registry.durable_writes as durable_writes
    import registry.relocate as relocate
    from tools.private_runtime_files import SEED_DIR, private_runtime_dir

    for directory in ("UPLOADS_DIR", "LOGS_DIR", "RUNTIME_DIR"):
        monkeypatch.setattr(config, directory, tmp_path / directory)
    monkeypatch.setattr(prerequisites, "prepare_startup_prerequisites", lambda *_a, **_k: None)
    monkeypatch.setattr(app_lifecycle.app_startup_init, "init_db", lambda: None)
    monkeypatch.setattr(conversion, "convert_legacy_project_paths", lambda *_a, **_k: None)
    monkeypatch.setattr(durable_writes, "recover_write_intents", lambda *_a, **_k: None)
    monkeypatch.setattr(relocate, "recover_interrupted_relocations", lambda *_a, **_k: None)
    monkeypatch.setattr(system_projection, "ensure_system_projection", lambda *_a, **_k: None)
    monkeypatch.setattr(guidance_git, "startup_sweep", lambda *_a, **_k: None)
    stale = private_runtime_dir(SEED_DIR) / "cc-seed-stale.txt"
    stale.write_text("x", encoding="utf-8")

    class QuietLogger:
        def info(self, *_args: object) -> None:
            pass

    app_lifecycle.app_startup_init.initialize_startup(QuietLogger())

    assert not stale.exists()


def test_protected_shell_cannot_write_the_generated_workspace_projections(tmp_path) -> None:
    from tools.host_path_policy import ProtectedHostPathPolicy

    workspace = tmp_path / "workspace"
    policy = ProtectedHostPathPolicy(
        home=tmp_path / "home", state_root=tmp_path / "state", workspace=workspace,
    )
    rules = policy.sandbox_rules().splitlines()

    for name in ("context", "skills"):
        root = (workspace / ".corsia" / name).resolve()
        assert f'(deny file-write* (subpath "{root}"))' in rules
        assert policy.denial(root / "f.md", for_write=True)
        assert policy.denial(root, for_write=True)
        assert policy.denial(root / "f.md", for_write=False) is None
    assert not any(f'(subpath "{(workspace / ".corsia").resolve()}")' in rule for rule in rules)
    assert policy.denial(workspace / ".corsia" / "notes.md", for_write=True) is None
    assert policy.ordinary_denial(workspace / ".corsia" / "context" / "f.md", for_write=True)


def test_lease_leaves_the_projections_to_the_reviewed_route(tmp_path, monkeypatch) -> None:
    from infra import config
    from tools import shell_guard

    workspace = tmp_path / "workspace"
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    profile = tmp_path / "lease.sb"
    safehouse = tmp_path / "safehouse"
    safehouse.write_text("", encoding="utf-8")
    monkeypatch.setattr(shell_guard, "_safehouse_executable", lambda: safehouse)
    monkeypatch.setattr(shell_guard, "pinned_repo_sandbox_profile", lambda *_a, **_k: ";; REPO\n")

    leased = shell_guard.execution_envelope_profile("true", tmp_path, profile, leased=True)
    protected = shell_guard.execution_envelope_profile("true", tmp_path, profile, leased=False)

    assert ".corsia/context" not in leased
    assert f'(deny file-write* (subpath "{(workspace / ".corsia" / "context").resolve()}"))' in protected
    for pinned in (workspace / ".corsia", workspace):
        assert f'(deny file-write* (literal "{pinned.resolve()}"))' not in leased
        assert f'(deny file-write* (literal "{pinned.resolve()}"))' in protected


def test_launch_agent_rule_is_rendered_from_the_python_gate_pattern(tmp_path) -> None:
    from tools.host_path_policy import ProtectedHostPathPolicy
    from tools.launch_agent_plists import is_corsia_plist

    policy = ProtectedHostPathPolicy(home=tmp_path / "home", state_root=tmp_path / "state")
    launch_agents = policy.home / "Library" / "LaunchAgents"
    pinned = (
        f'(deny file-write* (regex #"^{launch_agents}/'
        '[^/]*[Cc][Oo][Rr][Ss][Ii][Aa][^/]*\\.plist$"))'
    )
    pattern = re.compile(re.escape(str(launch_agents)) + "/[^/]*[Cc][Oo][Rr][Ss][Ii][Aa][^/]*\\.plist$")

    assert pinned in policy.sandbox_rules().splitlines()
    assert "[Mm][Aa][Ee][Ss][Tt][Rr][Oo]" not in policy.sandbox_rules()
    for name in ("com.corsia.backend.plist", "corsia.plist", "io.CORSIA.app.plist", "xCorsiay.plist"):
        assert is_corsia_plist(name) and pattern.fullmatch(f"{launch_agents}/{name}"), name
        assert policy.denial(launch_agents / name, for_write=True), name
    for name in ("maestro.plist", "com.apple.plist", "corsia.txt"):
        assert not pattern.fullmatch(f"{launch_agents}/{name}"), name
        assert not is_corsia_plist(name), name


_SAFEHOUSE_REQUIRED_ENV = "CORSIA_REQUIRE_SAFEHOUSE"


def _safehouse_unavailable(detail: str):
    if os.environ.get(_SAFEHOUSE_REQUIRED_ENV) == "1":
        pytest.fail(f"{_SAFEHOUSE_REQUIRED_ENV}=1 forbids skipping this proof: {detail}")
    pytest.skip(detail)


@pytest.mark.integration
def test_safehouse_shell_cannot_read_or_write_private_launch_files(
    tmp_path, monkeypatch, short_system_temp,
) -> None:
    """A protected shell cannot touch any envelope profile or harness config.

    It also cannot write the generated workspace projections, nor rename one away
    and recreate it, and it still reads them.
    """
    from infra import config
    from tools import sandbox_config, shell_guard
    from tools.private_runtime_files import (
        BROWSER_STAGING_DIR,
        CLI_INSTALLER_DIR,
        HARNESS_MCP_DIR,
        SEED_DIR,
        SHELL_ENVELOPE_DIR,
        private_runtime_dir,
    )

    if os.environ.get("CORSIA_THREAD_ID"):
        _safehouse_unavailable("agent shells cannot prove nested Safehouse confinement")
    safehouse = shell_guard.shutil.which("safehouse")
    if safehouse is None:
        _safehouse_unavailable("Agent Safehouse is unavailable: shutil.which('safehouse') is None")

    workspace = short_system_temp / "w"
    home = short_system_temp / "h"
    state = short_system_temp / "s"
    context = workspace / ".corsia" / "context"
    skills = workspace / ".corsia" / "skills"
    for directory in (workspace, home, state, context, skills):
        directory.mkdir(parents=True, exist_ok=True)
    (context / "seed.md").write_text("projection-read-ok", encoding="utf-8")
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "STATE_ROOT", state)
    monkeypatch.setenv("HOME", str(home))

    marker = f"private-launch-file-{uuid.uuid4().hex}"
    unique = uuid.uuid4().hex[:12]
    decoys = [
        private_runtime_dir(SHELL_ENVELOPE_DIR) / f"corsia-shell-envelope-{unique}.sb",
        private_runtime_dir(SHELL_ENVELOPE_DIR) / f"corsia-controlled-git-{unique}.sb",
        private_runtime_dir(HARNESS_MCP_DIR) / f"cc-{unique}.json",
        private_runtime_dir(SEED_DIR) / f"cc-seed-{unique}-x.txt",
        private_runtime_dir(CLI_INSTALLER_DIR) / f"corsia-cli-install-{unique}.sh",
        private_runtime_dir(BROWSER_STAGING_DIR) / f"document-{unique}.png",
    ]
    profile = private_runtime_dir(SHELL_ENVELOPE_DIR) / f"corsia-shell-envelope-own-{unique}.sb"
    try:
        for decoy in decoys:
            decoy.write_text(marker, encoding="utf-8")
        profile.write_text(
            shell_guard.execution_envelope_profile(
                "true", workspace, profile, leased=False, thread_id="test-thread",
            ),
            encoding="utf-8",
        )

        def run(command: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                shell_guard.safehouse_argv(command, workspace, profile, thread_id="test-thread"),
                capture_output=True, cwd=workspace, text=True, timeout=20,
                env=shell_guard.safehouse_subprocess_env("test-thread"),
            )

        control = run(f"/bin/cat {context / 'seed.md'}")
        if control.returncode != 0:
            _safehouse_unavailable(
                f"the benign control could not run under Agent Safehouse: {control.stderr.strip()[:300]}"
            )
        assert control.stdout == "projection-read-ok"
        assert run(f"/usr/bin/touch {workspace / '.corsia' / 'notes.md'}").returncode == 0

        for target in (*decoys, profile):
            read = run(f"/bin/cat {target}")
            assert read.returncode != 0 and marker not in read.stdout + read.stderr, target
            append = run(f"/bin/sh -c 'echo appended >> {target}'")
            assert append.returncode != 0, target
            assert "Operation not permitted" in append.stderr, (target, append.stderr)
        assert all(decoy.read_text(encoding="utf-8") == marker for decoy in decoys)

        for root in (context, skills):
            for command in (
                f"/usr/bin/touch {root / 'must-not-exist'}",
                f"/bin/mv {root} {root}-moved",
                f"/bin/sh -c 'mkdir -p {root}-new && mv {root}-new {root}'",
            ):
                result = run(command)
                assert result.returncode != 0, command
                assert "Operation not permitted" in result.stderr, (command, result.stderr)
            assert root.is_dir() and not (root / "must-not-exist").exists()
            assert not Path(f"{root}-moved").exists()
        recreate = run(
            f"/bin/sh -c 'mv {context} {context}-x; mkdir -p {context}; echo x > {context}/f'"
        )
        assert recreate.returncode != 0 and not (context / "f").exists()
        assert (context / "seed.md").read_text(encoding="utf-8") == "projection-read-ok"
    finally:
        for decoy in decoys:
            decoy.unlink(missing_ok=True)


@pytest.mark.integration
def test_safehouse_shell_cannot_rename_an_ancestor_of_a_protected_path(
    tmp_path, monkeypatch, short_system_temp,
) -> None:
    """Seatbelt checks a rename only at its source and destination.

    Every protected path inside the granted workspace (a generated root, a control
    file, a write root) is still reached by renaming one of its ancestors, so each
    ancestor is denied too, while ordinary children of those ancestors stay usable.
    """
    from infra import config
    from tools import host_path_policy, managed_runtime, sandbox_config, shell_guard
    from tools.private_runtime_files import SHELL_ENVELOPE_DIR, private_runtime_dir

    if os.environ.get("CORSIA_THREAD_ID"):
        _safehouse_unavailable("agent shells cannot prove nested Safehouse confinement")
    if shell_guard.shutil.which("safehouse") is None:
        _safehouse_unavailable("Agent Safehouse is unavailable: shutil.which('safehouse') is None")

    workspace = short_system_temp / "w"
    home = short_system_temp / "h"
    state = short_system_temp / "s"
    context = workspace / ".corsia" / "context"
    control = workspace / "ctl" / "restart"
    write_root = workspace / "cache" / "apps"
    protected = {context / "seed.md": "context", control: "control", write_root / "app.bin": "write-root"}
    for directory in (home, state, context, control.parent, write_root):
        directory.mkdir(parents=True, exist_ok=True)
    for path, text in protected.items():
        path.write_text(text, encoding="utf-8")
    repo = workspace / "repo"
    repo.mkdir()
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "STATE_ROOT", state)
    monkeypatch.setattr(host_path_policy, "_CONTROL_FILES", (control,))
    monkeypatch.setattr(managed_runtime, "application_cache_root", lambda home=None: write_root)
    monkeypatch.setenv("HOME", str(home))

    unique = uuid.uuid4().hex[:12]
    profile = private_runtime_dir(SHELL_ENVELOPE_DIR) / f"corsia-shell-envelope-anc-{unique}.sb"
    try:
        profile.write_text(
            shell_guard.execution_envelope_profile(
                "true", workspace, profile, leased=False, thread_id="test-thread", policies=[],
            ),
            encoding="utf-8",
        )

        def run(command: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                shell_guard.safehouse_argv(command, workspace, profile, thread_id="test-thread", policies=[]),
                capture_output=True, cwd=workspace, text=True, timeout=30,
                env=shell_guard.safehouse_subprocess_env("test-thread"),
            )

        control_run = run(f"/bin/cat {context / 'seed.md'}")
        if control_run.returncode != 0:
            _safehouse_unavailable(
                f"the benign control could not run under Agent Safehouse: {control_run.stderr.strip()[:300]}"
            )

        ancestors = (workspace / ".corsia", control.parent, write_root.parent, workspace)
        for ancestor in ancestors:
            moved = Path(f"{ancestor}-moved")
            for attempt in (
                f"/bin/mv {ancestor} {moved}",
                f"/bin/sh -c 'mv {ancestor} {moved} && mkdir -p {ancestor} && echo evil > {ancestor}/x'",
            ):
                result = run(attempt)
                assert result.returncode != 0, attempt
                assert "Operation not permitted" in result.stderr, (attempt, result.stderr)
            assert ancestor.is_dir() and not moved.exists(), ancestor
            assert not (ancestor / "x").exists(), ancestor
        for path, text in protected.items():
            assert path.read_text(encoding="utf-8") == text, path

        for ancestor in ancestors:
            sibling = ancestor / "sibling.txt"
            made = run(
                f"/bin/sh -c 'echo a > {sibling} && echo b >> {sibling} && mkdir {ancestor}/sd "
                f"&& rmdir {ancestor}/sd && rm {sibling}'"
            )
            assert made.returncode == 0, (ancestor, made.stderr)
            assert not sibling.exists(), ancestor

        added = run(f"/usr/bin/git -C {repo} worktree add -q -b nb {repo / '.worktrees' / 'nb'}")
        assert added.returncode == 0, added.stderr
        assert (repo / ".worktrees" / "nb" / ".git").exists()
    finally:
        profile.unlink(missing_ok=True)
