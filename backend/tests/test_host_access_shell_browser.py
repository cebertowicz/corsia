from __future__ import annotations

import os
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest


@pytest.fixture(autouse=True)
def _fresh_process_admission(monkeypatch) -> None:
    # These tests reuse thread-1/turn-1 and need that Turn's admission open.
    # The process owner is one per process, so state its precondition here
    # rather than rely on every other test file restoring it.
    from tools import shell

    monkeypatch.setattr(shell.PROCESS_OWNER, "_closed_turns", set())
    monkeypatch.setattr(shell.PROCESS_OWNER, "_closed_leases", set())
    monkeypatch.setattr(shell.PROCESS_OWNER, "_closing_threads", set())


class _Process:
    pid = 999_999
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"ok", b""

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_bash_consumes_exact_permit_immediately_before_process_creation(
    tmp_path,
    monkeypatch,
) -> None:
    from core.turn_core import TurnIdentity
    from host_access import processes
    from tools import host_access_gate, shell

    events: list[tuple[str, object]] = []
    cwd_fd = os.open(
        tmp_path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )

    async def consume(**kwargs):
        events.append(("consume", kwargs))
        return SimpleNamespace(
            working_directory=tmp_path.resolve(),
            effective_timeout=23,
            _working_directory_fd=cwd_fd,
        )

    async def spawn(*argv, **kwargs):
        events.append(("spawn", (argv, kwargs)))
        return _Process()

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: events.append(("repo-policy", None)) or [],
    )
    monkeypatch.setattr(
        shell,
        "controlled_fast_forward_policy",
        lambda *_a, **_k: events.append(("controlled-fast-forward", None)),
    )
    monkeypatch.setattr(
        shell.BashTool,
        "_guard_command",
        lambda *_a, **_k: events.append(("protected-guard", None)),
    )
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell, "is_path_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        shell,
        "execution_envelope_profile",
        lambda *_a, **kwargs: events.append(("profile", kwargs.get("leased"))) or "",
    )
    monkeypatch.setattr(
        shell,
        "safehouse_argv",
        lambda command, *_a, **kwargs: (
            events.append(("argv", kwargs.get("leased")))
            or ["safehouse", "--", "bash", "-c", command]
        ),
    )
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        processes,
        "_process_creation_fingerprint",
        lambda _pid: "test-fingerprint",
    )
    monkeypatch.setattr(
        processes.ProcessOwner,
        "_owned_group_exists",
        lambda _self, _record: False,
    )
    monkeypatch.setattr(
        processes.ProcessOwner,
        "_runtime_marker_processes",
        lambda _self, _runtime: (),
    )

    result = await shell.BashTool(
        working_dir=str(tmp_path),
        default_timeout=120,
    ).execute(
        "printf ok",
        timeout=23,
        _thread_id="thread-1",
        _thread_project_id="project-1",
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert result == "ok"
    assert [event[0] for event in events] == ["consume",
        "profile",
        "argv",
        "spawn",
    ]
    assert events[1:3] == [("profile", True), ("argv", True)]
    consume_call = events[0][1]
    assert consume_call["thread_id"] == "thread-1"
    assert consume_call["project_id"] == "project-1"
    assert consume_call["tool_name"] == "bash"
    assert consume_call["params"] == {
        "command": "printf ok",
        "description": None,
        "timeout": 23,
        "working_dir": None,
        "background": False,
        "host": False,
    }
    _argv, spawn_kwargs = events[3][1]
    assert _argv[:3] == (sys.executable, "-I", "-c")
    assert "os.fchdir(fd)" in _argv[3]
    assert _argv[4] == str(cwd_fd)
    assert spawn_kwargs["cwd"] is None
    assert spawn_kwargs["pass_fds"] == (cwd_fd,)
    assert spawn_kwargs["start_new_session"] is True
    with pytest.raises(OSError):
        os.fstat(cwd_fd)


@pytest.mark.asyncio
async def test_bash_closes_consumed_cwd_fd_when_spawn_raises(
    tmp_path,
    monkeypatch,
) -> None:
    from core.turn_core import TurnIdentity
    from tools import host_access_gate, shell

    cwd_fd = os.open(
        tmp_path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )

    async def consume(**_kwargs):
        return SimpleNamespace(
            working_directory=tmp_path.resolve(),
            effective_timeout=23,
            _working_directory_fd=cwd_fd,
        )

    class _Owner:
        async def spawn(self, _argv, **kwargs):
            assert kwargs["cwd_fd"] == cwd_fd
            assert os.fstat(cwd_fd).st_ino == os.stat(tmp_path).st_ino
            raise RuntimeError("spawn failed")

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(shell, "PROCESS_OWNER", _Owner())
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell, "execution_envelope_profile", lambda *_a, **_k: "")
    monkeypatch.setattr(
        shell,
        "safehouse_argv",
        lambda command, *_a, **_k: ["safehouse", "--", "bash", "-c", command],
    )

    result = await shell.BashTool(working_dir=str(tmp_path)).execute(
        "printf ok",
        timeout=23,
        _thread_id="thread-1",
        _thread_project_id=None,
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert result == "Error executing command: spawn failed"
    with pytest.raises(OSError):
        os.fstat(cwd_fd)


@pytest.mark.asyncio
async def test_bash_refuses_foreground_effect_without_authoritative_turn_identity(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes
    from tools import host_access_gate, shell

    effects: list[str] = []

    async def consume(**_kwargs):
        return SimpleNamespace(
            working_directory=tmp_path.resolve(),
            effective_timeout=23,
        )

    class _Owner:
        async def spawn(self, _argv, **_kwargs):
            effects.append("spawn")
            return processes.ForegroundProcessResult(
                record=SimpleNamespace(),
                stdout=b"unexpected",
                stderr=b"",
                returncode=0,
            )

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(shell, "PROCESS_OWNER", _Owner())
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [])
    monkeypatch.setattr(
        shell,
        "controlled_fast_forward_policy",
        lambda *_args, **_kwargs: None,
    )
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell, "is_path_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        shell,
        "execution_envelope_profile",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        shell,
        "safehouse_argv",
        lambda command, *_args, **_kwargs: [
            "safehouse",
            "--",
            "bash",
            "-c",
            command,
        ],
    )

    result = await shell.BashTool(working_dir=str(tmp_path)).execute(
        "printf unexpected",
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert result == (
        "Error: shell execution requires an exact active thread and turn identity"
    )
    assert effects == []


@pytest.mark.asyncio
async def test_controlled_fast_forward_refuses_missing_turn_identity(
    tmp_path,
    monkeypatch,
) -> None:
    from infra.project_repo_policy import RepoPolicy
    from tools import host_access_gate, shell

    repo = tmp_path / "repo"
    repo.mkdir()
    policy = RepoPolicy("project-1", "repo", repo, "main", True)
    effects: list[str] = []

    async def consume(**_kwargs):
        return None

    def fast_forward(*_args, **_kwargs):
        effects.append("fast-forward")
        return SimpleNamespace(
            updated=False,
            branch="main",
            previous_oid="a" * 40,
            current_oid="a" * 40,
        )

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(
        shell,
        "list_registered_repo_policies",
        lambda: [policy],
    )
    monkeypatch.setattr(
        shell,
        "controlled_fast_forward_policy",
        lambda *_args, **_kwargs: policy,
    )
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell, "fast_forward_repo_locked", fast_forward)

    result = await shell.BashTool(working_dir=str(repo)).execute(
        "git pull --ff-only",
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert result == (
        "Error: shell execution requires an exact active thread and turn identity"
    )
    assert effects == []


@pytest.mark.asyncio
async def test_controlled_fast_forward_reports_safehouse_discovery(
    tmp_path,
    monkeypatch,
) -> None:
    from core.turn_core import TurnIdentity
    from infra.project_repo_policy import RepoPolicy
    from tools import host_access_gate, shell

    repo = tmp_path / "repo"
    repo.mkdir()
    policy = RepoPolicy("project-1", "repo", repo, "main", True)

    async def consume(**_kwargs):
        return None

    monkeypatch.setattr(host_access_gate, "consume_current_action_permit", consume)
    monkeypatch.setattr(
        shell,
        "list_registered_repo_policies",
        lambda **_kwargs: [policy],
    )
    monkeypatch.setattr(
        shell,
        "controlled_fast_forward_policy",
        lambda *_args, **_kwargs: policy,
    )
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: False)
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")

    result = await shell.BashTool(working_dir=str(repo)).execute(
        "git pull --ff-only",
        _thread_id="thread-1",
        _thread_project_id="project-1",
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert "backend PATH: /usr/local/bin:/usr/bin:/bin" in result
    assert "install Agent Safehouse" in result
    assert "CORSIA service PATH" in result


@pytest.mark.asyncio
async def test_bash_background_returns_only_an_opaque_managed_handle(
    tmp_path,
    monkeypatch,
) -> None:
    from core.turn_core import TurnIdentity
    from host_access.processes import BackgroundProcessResult
    from tools import host_access_gate, shell

    events: list[tuple[str, object]] = []

    async def consume(**kwargs):
        events.append(("consume", kwargs))
        cwd_fd = os.open(
            tmp_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        return SimpleNamespace(
            working_directory=tmp_path.resolve(),
            effective_timeout=23,
            _working_directory_fd=cwd_fd,
            lease_id="lease-1",
            lease_revision=7,
            permit_id="permit-1",
        )

    class _Owner:
        async def spawn(self, argv, **kwargs):
            events.append(("owner_spawn", (tuple(argv), kwargs)))
            return BackgroundProcessResult(handle="opaque-handle")

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(shell, "PROCESS_OWNER", _Owner(), raising=False)
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [])
    monkeypatch.setattr(
        shell,
        "controlled_fast_forward_policy",
        lambda *_a, **_k: None,
    )
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell, "is_path_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        shell,
        "execution_envelope_profile",
        lambda *_a, **_k: "",
    )
    monkeypatch.setattr(
        shell,
        "safehouse_argv",
        lambda command, *_a, **_k: ["safehouse", "--", "bash", "-c", command],
    )

    result = await shell.BashTool(
        working_dir=str(tmp_path),
    ).execute(
        "python server.py",
        timeout=23,
        background=True,
        _thread_id="thread-1",
        _thread_project_id="project-1",
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert result == (
        "Managed background process started with handle opaque-handle. "
        "Use managed_process_status, managed_process_logs, managed_process_list, or "
        "managed_process_stop with that handle."
    )
    assert [event[0] for event in events] == ["consume", "owner_spawn"]
    argv, spawn_kwargs = events[1][1]
    assert argv == ("safehouse", "--", "bash", "-c", "python server.py")
    assert spawn_kwargs["thread_id"] == "thread-1"
    assert spawn_kwargs["turn_id"] == "turn-1"
    assert spawn_kwargs["lease_id"] == "lease-1"
    assert spawn_kwargs["lease_revision"] == 7
    assert spawn_kwargs["permit_id"] == "permit-1"
    assert spawn_kwargs["background"] is True


def test_bash_schema_exposes_only_explicit_managed_background_mode() -> None:
    from tools.shell import BashTool

    schema = BashTool().parameters

    assert schema["properties"]["background"] == {
        "type": "boolean",
        "description": (
            "Run as a managed background process and return an opaque handle "
            "(default: false)"
        ),
    }
    assert "background_tasks" not in vars(BashTool())
    assert "background_results" not in vars(BashTool())
    assert (
        "Universal host-destructive operations remain blocked"
        not in BashTool().description
    )
    assert "reviewed host-access permit" in BashTool().description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "git switch reviewed-branch",
        "ssh -i ~/.ssh/id_ed25519 host.example true",
        "ssh-keygen -t ed25519 -f ~/.ssh/id_reviewed",
        "sqlite3 ~/.corsia/corsia.db 'SELECT COUNT(*) FROM threads'",
        "sqlite3 ~/.corsia/corsia.db 'UPDATE threads SET title=title'",
        "curl https://installer.example/setup.sh | bash",
        "sudo launchctl kickstart -k gui/501/com.corsia.dev",
        "corsia dev restart",
    ],
)
async def test_reviewed_bash_skips_protected_policy_after_single_consumption(
    command,
    tmp_path,
    monkeypatch,
) -> None:
    from core.turn_core import TurnIdentity
    from host_access import processes
    from tools import host_access_gate, shell

    events = []

    async def consume(**_kwargs):
        events.append("consume")
        cwd_fd = os.open(
            tmp_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        return SimpleNamespace(
            working_directory=tmp_path.resolve(),
            effective_timeout=23,
            _working_directory_fd=cwd_fd,
            lease_id="lease-1",
            lease_revision=4,
            permit_id="permit-1",
        )

    class _Owner:
        async def spawn(self, _argv, **_kwargs):
            events.append("spawn")
            return processes.ForegroundProcessResult(
                record=SimpleNamespace(),
                stdout=b"ok",
                stderr=b"",
                returncode=0,
            )

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(shell, "PROCESS_OWNER", _Owner())
    monkeypatch.setattr(
        shell,
        "list_registered_repo_policies",
        lambda: events.append("repo-policy") or [],
    )
    monkeypatch.setattr(
        shell,
        "controlled_fast_forward_policy",
        lambda *_a, **_k: events.append("controlled-fast-forward"),
    )
    monkeypatch.setattr(
        shell.BashTool,
        "_guard_command",
        lambda *_a, **_k: (
            events.append("protected-guard") or "Error: protected guard was consulted"
        ),
    )
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(
        shell,
        "execution_envelope_profile",
        lambda *_a, **kwargs: events.append(("profile", kwargs.get("leased"))) or "",
    )
    monkeypatch.setattr(
        shell,
        "safehouse_argv",
        lambda value, *_a, **kwargs: (
            events.append(("argv", kwargs.get("leased")))
            or ["safehouse", "--", "bash", "-c", value]
        ),
    )

    result = await shell.BashTool(working_dir=str(tmp_path)).execute(
        command,
        timeout=23,
        _thread_id="thread-1",
        _thread_project_id=None,
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert result == "ok"
    assert events == [
        "consume",
        ("profile", True),
        ("argv", True),
        "spawn",
    ]


@pytest.mark.asyncio
async def test_protected_bash_still_applies_broad_guard_without_permit(
    tmp_path,
    monkeypatch,
) -> None:
    from core.turn_core import TurnIdentity
    from tools import host_access_gate, shell

    async def no_permit(**_kwargs):
        return None

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        no_permit,
    )
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [])
    monkeypatch.setattr(
        shell,
        "controlled_fast_forward_policy",
        lambda *_a, **_k: None,
    )

    result = await shell.BashTool(working_dir=str(tmp_path)).execute(
        "rm -rf /",
        _thread_id="thread-1",
        _thread_project_id=None,
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert result.startswith("Error: Command blocked by protected-mode safety guard")


@pytest.mark.asyncio
async def test_browser_screenshot_consumes_before_creating_outside_destination(
    tmp_path,
    monkeypatch,
) -> None:
    from tools import browser, host_access_gate, sandbox_config

    requested = tmp_path / "requested" / "shot.png"
    reviewed = tmp_path / "reviewed" / "shot.png"
    events: list[tuple[str, object]] = []

    async def consume(**kwargs):
        events.append(("consume", kwargs))
        assert not reviewed.parent.exists()
        return SimpleNamespace(
            resolved_path=reviewed.resolve(strict=False),
            lease_id="lease-1",
        )

    async def run(
        args,
        *,
        thread_id,
        lease_id=None,
        timeout=30,
        show_window=None,
        _reviewed_local_effect=False,
    ):
        events.append(
            ("run", (args, thread_id, lease_id, timeout))
        )
        assert not reviewed.parent.exists()
        Path(args[-1]).write_bytes(b"screenshot")
        return "saved"

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(
        sandbox_config,
        "check_repo_write_path",
        lambda _path: "Protected checkout mutation is blocked.",
    )
    monkeypatch.setattr(browser, "_run", run)

    result = await browser.BrowserScreenshotTool().execute(
        path=str(requested),
        full_page=True,
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert result.startswith(f"Screenshot saved to: {reviewed}")
    assert [event[0] for event in events] == ["consume", "run"]
    screenshot_args = events[1][1][0]
    assert events[1][1][1:] == (
        "thread-1",
        "lease-1",
        30,
    )
    assert screenshot_args[:2] == ["screenshot", "--full"]
    assert screenshot_args[-1] != str(reviewed)
    assert reviewed.read_bytes() == b"screenshot"


@pytest.mark.asyncio
async def test_browser_file_open_refuses_before_permit_or_effect(
    tmp_path,
    monkeypatch,
) -> None:
    from tools import browser, host_access_gate

    requested = tmp_path / "requested.txt"
    reviewed = tmp_path / "reviewed.txt"
    reviewed.write_text("reviewed\n", encoding="utf-8")
    events: list[tuple[str, object]] = []
    opened_content: list[str] = []

    async def consume(**kwargs):
        events.append(("consume", kwargs))
        return SimpleNamespace(
            resolved_path=reviewed.resolve(strict=False),
            lease_id="lease-1",
        )

    async def run(
        args,
        *,
        thread_id,
        lease_id=None,
        timeout=30,
        show_window=None,
        _reviewed_local_effect=False,
    ):
        events.append(
            ("run", (args, thread_id, lease_id, timeout))
        )
        if args[0] == "open":
            opened_content.append(
                Path(unquote(urlsplit(args[1]).path)).read_text(encoding="utf-8")
            )
        return "opened"

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(browser, "_run", run)

    result = await browser.BrowserOpenTool().execute(
        requested.resolve(strict=False).as_uri(),
        wait_for_load=False,
        _thread_id="thread-1",
        _thread_project_id=None,
    )

    assert "HTTP, HTTPS or about:blank" in result
    assert events == []
    assert opened_content == []


@pytest.mark.asyncio
async def test_reviewed_local_browser_effects_run_and_forward_bound_lease(
    tmp_path,
    monkeypatch,
) -> None:
    """Reviewed local browser effects run and forward their bound lease id.

    With the pending-decision freeze removed (RFC §5), there is no longer a
    second decision gate: reviewed effects carry their lease, and an ordinary
    unreviewed action simply runs with no lease binding.
    """
    from tools import browser, browser_actions, host_access_gate

    reviewed = tmp_path / "reviewed.txt"
    reviewed.write_text("reviewed\n", encoding="utf-8")
    screenshot = tmp_path / "shot.png"
    events: list[tuple[str, object]] = []

    async def consume(**kwargs):
        tool_name = kwargs["tool_name"]
        return SimpleNamespace(
            resolved_path=(
                reviewed.resolve(strict=False)
                if tool_name == "browser_open"
                else screenshot.resolve(strict=False)
            ),
            lease_id="lease-1",
        )

    class _Owner:
        async def run_browser(
            self,
            thread_id,
            args,
            *,
            lease_id=None,
            timeout=30,
            show_window=None,
        ):
            events.append(("run", (thread_id, args, lease_id, timeout)))
            if args[0] == "screenshot":
                Path(args[-1]).write_bytes(b"screenshot")
            return "ok"

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    from tools import browser_runtime
    monkeypatch.setattr(browser_runtime, "_check_installed", lambda: None)
    monkeypatch.setattr(browser_runtime, "PROCESS_OWNER", _Owner())

    assert (
        await browser.BrowserOpenTool().execute(
            reviewed.as_uri(),
            wait_for_load=False,
            _thread_id="thread-1",
            _thread_project_id="project-1",
        )
    ).startswith("Error: Browser navigation requires HTTP, HTTPS or about:blank")
    assert (
        await browser.BrowserScreenshotTool().execute(
            path=str(screenshot),
            _thread_id="thread-1",
            _thread_project_id="project-1",
        )
    ).startswith(f"Screenshot saved to: {screenshot}")
    assert (
        await browser_actions.BrowserClickTool().execute(
            "@e1",
            _thread_id="thread-1",
            _thread_project_id="project-1",
        )
        == "ok"
    )

    runs = [event[1] for event in events if event[0] == "run"]
    # Screenshots still forward their bound lease; file navigation is refused.
    assert all(run[2] == "lease-1" for run in runs[:-1])
    # An ordinary click runs normally with no lease binding and no freeze gate.
    assert runs[-1] == ("thread-1", ["click", "@e1"], None, 30)


@pytest.mark.asyncio
async def test_browser_file_open_refuses_even_with_reviewed_path(
    tmp_path,
    monkeypatch,
) -> None:
    from tools import browser, host_access_gate

    home = tmp_path / "home"
    sensitive = home / ".ssh" / "id_ed25519"
    sensitive.parent.mkdir(parents=True)
    sensitive.write_text("reviewed-private-material\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    effects: list[str] = []

    async def consume(**_kwargs):
        return SimpleNamespace(
            resolved_path=sensitive,
            lease_id="lease-1",
        )

    from tools.filesystem_effects import copy_bound_file as real_copy

    def copy(*args, **kwargs):
        effects.append("copy")
        return real_copy(*args, **kwargs)

    async def run(args, **_kwargs):
        effects.append(args[0])
        return "opened"

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(browser, "copy_bound_file", copy)
    monkeypatch.setattr(browser, "_run", run)

    result = await browser.BrowserOpenTool().execute(
        (tmp_path / "requested.txt").as_uri(),
        wait_for_load=False,
        _thread_id="thread-1",
        _thread_project_id=None,
    )

    assert "HTTP, HTTPS or about:blank" in result
    assert effects == []


@pytest.mark.asyncio
async def test_browser_screenshot_ancestor_swap_cannot_redirect_output(
    tmp_path,
    monkeypatch,
) -> None:
    from tools import browser, host_access_gate

    reviewed_root = tmp_path / "reviewed"
    reviewed_root.mkdir()
    reviewed = reviewed_root / "shot.png"
    reviewed.write_bytes(b"reviewed")
    redirected_root = tmp_path / "redirected"
    redirected_root.mkdir()
    redirected = redirected_root / "shot.png"
    redirected.write_bytes(b"must-stay")
    displaced_root = tmp_path / "displaced"

    async def consume(**_kwargs):
        reviewed_root.rename(displaced_root)
        reviewed_root.symlink_to(redirected_root, target_is_directory=True)
        return SimpleNamespace(
            resolved_path=reviewed,
            lease_id="lease-1",
        )

    async def run(
        args,
        *,
        thread_id,
        lease_id=None,
        timeout=30,
        show_window=None,
        _reviewed_local_effect=False,
    ):
        Path(args[-1]).write_bytes(b"new-screenshot")
        return "saved"

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(browser, "_run", run)

    result = await browser.BrowserScreenshotTool().execute(
        path=str(reviewed),
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert result.startswith("Error")
    assert redirected.read_bytes() == b"must-stay"
    assert (displaced_root / "shot.png").read_bytes() == b"reviewed"


@pytest.mark.asyncio
async def test_browser_file_open_ancestor_swap_fails_before_navigation(
    tmp_path,
    monkeypatch,
) -> None:
    from tools import browser, host_access_gate

    reviewed_root = tmp_path / "reviewed"
    reviewed_root.mkdir()
    reviewed = reviewed_root / "notes.txt"
    reviewed.write_text("reviewed\n", encoding="utf-8")
    redirected_root = tmp_path / "redirected"
    redirected_root.mkdir()
    (redirected_root / "notes.txt").write_text("redirected\n", encoding="utf-8")
    displaced_root = tmp_path / "displaced"
    navigations: list[str] = []

    async def consume(**_kwargs):
        reviewed_root.rename(displaced_root)
        reviewed_root.symlink_to(redirected_root, target_is_directory=True)
        return SimpleNamespace(
            resolved_path=reviewed,
            lease_id="lease-1",
        )

    async def run(
        args,
        *,
        thread_id,
        lease_id=None,
        timeout=30,
        show_window=None,
        _reviewed_local_effect=False,
    ):
        if args[0] == "open":
            navigations.append(
                Path(unquote(urlsplit(args[1]).path)).read_text(encoding="utf-8")
            )
        return "opened"

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(browser, "_run", run)

    result = await browser.BrowserOpenTool().execute(
        reviewed.as_uri(),
        wait_for_load=False,
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert result.startswith("Error")
    assert navigations == []


def test_browser_screenshot_description_matches_thread_default() -> None:
    from tools.browser import BrowserScreenshotTool

    description = BrowserScreenshotTool.parameters["properties"]["path"]["description"]

    assert "/tmp" not in description
    assert "thread" in description.lower()
    assert "working/screenshots" in description


@pytest.mark.asyncio
async def test_browser_http_open_does_not_enter_host_path_gate(monkeypatch) -> None:
    from tools import browser, host_access_gate

    calls: list[list[str]] = []
    results = iter(["launched", "navigated"])

    async def unexpected(**_kwargs):
        raise AssertionError("network URL is not a local host-path action")

    async def run(
        args,
        *,
        thread_id,
        lease_id=None,
        timeout=30,
        show_window=None,
        navigation_url=None,
        notice=True,
    ):
        assert notice == (args != ["open", "about:blank"])
        assert thread_id == "thread-1"
        assert lease_id is None
        calls.append((args, show_window))
        return next(results)

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        unexpected,
    )
    monkeypatch.setattr(browser, "_run", run)

    result = await browser.BrowserOpenTool().execute(
        "https://example.com/",
        wait_for_load=False,
        _thread_id="thread-1",
        _thread_project_id=None,
    )

    assert result == "navigated"
    assert calls == [
        (["open", "about:blank"], False),
        ([
            "eval",
            'location.href = "https://example.com/"; \'navigating\'',
        ], None),
    ]


@pytest.mark.asyncio
async def test_browser_http_open_requests_a_visible_new_session(monkeypatch) -> None:
    from tools import browser

    calls: list[tuple[list[str], bool | None]] = []

    async def run(args, *, thread_id, lease_id=None, timeout=30, show_window=None, navigation_url=None, notice=True):
        assert notice == (args != ["open", "about:blank"])
        assert thread_id == "thread-1" and lease_id is None
        calls.append((args, show_window))
        return "opened"

    monkeypatch.setattr(browser, "_run", run)
    result = await browser.BrowserOpenTool().execute(
        "https://example.com/", wait_for_load=False, show_window=True,
        _thread_id="thread-1", _thread_project_id=None,
    )

    assert result == "opened"
    assert calls[0] == (["open", "about:blank"], True)
    assert calls[1][1] is None


@pytest.mark.asyncio
async def test_browser_http_open_stages_navigation_before_waiting(monkeypatch) -> None:
    from tools import browser

    calls: list[tuple[list[str], int]] = []
    results = iter(["launched", "navigating", "loaded"])

    async def run(
        args,
        *,
        thread_id,
        lease_id=None,
        timeout=30,
        show_window=None,
        navigation_url=None,
        notice=True,
    ):
        assert notice == (args != ["open", "about:blank"])
        assert thread_id == "thread-1"
        assert lease_id is None
        calls.append((args, timeout))
        return next(results)

    monkeypatch.setattr(browser, "_run", run)

    result = await browser.BrowserOpenTool().execute(
        "https://example.com/path?query='quoted'",
        _thread_id="thread-1",
        _thread_project_id=None,
    )

    assert result == "navigating\nloaded"
    assert calls == [
        (["open", "about:blank"], 30),
        ([
            "eval",
            'location.href = "https://example.com/path?query=\'quoted\'"; '
            "'navigating'",
        ], 30),
        (["wait", "--load", "networkidle"], 15),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "arguments"),
    [
        (lambda _tmp: __import__(
            "tools.browser",
            fromlist=["BrowserOpenTool"],
        ).BrowserOpenTool(), ("https://example.com/", False),
        ),
        (lambda _tmp: __import__(
            "tools.browser_actions",
            fromlist=["BrowserSnapshotTool"],
        ).BrowserSnapshotTool(), (False,),
        ),
        (lambda _tmp: __import__(
            "tools.browser_actions",
            fromlist=["BrowserClickTool"],
        ).BrowserClickTool(), ("@e1",),
        ),
        (lambda _tmp: __import__(
            "tools.browser_actions",
            fromlist=["BrowserFillTool"],
        ).BrowserFillTool(), ("@e1", "value"),
        ),
        (lambda tmp: __import__(
            "tools.browser",
            fromlist=["BrowserScreenshotTool"],
        ).BrowserScreenshotTool(workspace=tmp), ("shot.png", False),
        ),
        (lambda _tmp: __import__(
            "tools.browser_actions",
            fromlist=["BrowserEvalTool"],
        ).BrowserEvalTool(), ("document.title",),
        ),
        (lambda _tmp: __import__(
            "tools.browser_actions",
            fromlist=["BrowserPersonStepTool"],
        ).BrowserPersonStepTool(), ("Sign in to Google",),
        ),
        (lambda _tmp: __import__(
            "tools.browser_actions",
            fromlist=["BrowserWaitTool"],
        ).BrowserWaitTool(), ("10",),
        ),
        (lambda _tmp: __import__(
            "tools.browser",
            fromlist=["BrowserCloseTool"],
        ).BrowserCloseTool(), (),
        ),
    ],
)
async def test_every_browser_tool_requires_exact_backend_identity(
    tmp_path,
    factory,
    arguments,
) -> None:
    tool = factory(tmp_path)

    with pytest.raises(PermissionError, match="thread identity"):
        await tool.execute(*arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_args"),
    [
        ("BrowserSnapshotTool", (True,), ["snapshot", "-i"]),
        ("BrowserClickTool", ("@e1",), ["click", "@e1"]),
        ("BrowserFillTool", ("@e1", "value"), ["fill", "@e1", "value"]),
        ("BrowserEvalTool", ("document.title",), ["eval", "document.title"]),
        ("BrowserWaitTool", ("10",), ["wait", "10"]),
    ],
)
async def test_browser_commands_forward_exact_backend_identity(
    monkeypatch,
    tool_name,
    arguments,
    expected_args,
) -> None:
    from tools import browser_actions

    calls: list[tuple[object, ...]] = []

    async def run(
        args,
        *,
        thread_id,
        lease_id=None,
        timeout=30,
    ):
        calls.append((args, thread_id, lease_id, timeout))
        return "ok"

    monkeypatch.setattr(browser_actions, "_run", run)
    tool = getattr(browser_actions, tool_name)()

    assert (
        await tool.execute(
            *arguments,
            _thread_id="authoritative-thread",
            _thread_project_id="project-1",
        )
        == "ok"
    )
    assert calls == [
        (
            expected_args,
            "authoritative-thread",
            None,
            15 if tool_name == "BrowserSnapshotTool" else (
                30 if tool_name == "BrowserWaitTool" else 30
            ),
        )
    ]


@pytest.mark.asyncio
async def test_browser_person_step_runs_the_real_declaration_for_the_authoritative_thread(monkeypatch) -> None:
    """Only the browser process and the plan write are stand-ins; the tool returns the declaration's success."""
    from host_access import browser_cdp, browser_step_lifecycle as lifecycle
    from host_access.browser_cdp import PasswordProbe
    from host_access.browser_sessions import BrowserSessionOwner
    from host_access.browser_session_state import BrowserSessionRecord
    from tools import browser_actions, browser_runtime

    owner = BrowserSessionOwner(namespace="corsia-person-step", instance_key="test")
    admitted: list[str] = []
    writes: list[tuple[str, object]] = []

    async def pin() -> None:
        return None

    async def admit(thread_id, **_kwargs):
        admitted.append(thread_id)
        return SimpleNamespace(record=BrowserSessionRecord(thread_id, "session-one"))

    async def page_endpoint(_command):
        return "ws://page.test"

    async def probe(_endpoint, _known):
        return PasswordProbe("visit", "accounts.google.com", frozenset(), "ws://page.test", present_nodes=frozenset())

    monkeypatch.setattr(owner, "_pin_native", pin)
    monkeypatch.setattr(owner, "_admit", admit)
    monkeypatch.setattr(browser_cdp, "page_endpoint", page_endpoint)
    monkeypatch.setattr(browser_cdp, "page_password_probe", probe)

    def plan_write(operation, *_args, **kwargs):
        writes.append((operation, kwargs["title"]))
        return "step"

    monkeypatch.setattr(lifecycle, "_plan_write", plan_write)
    monkeypatch.setattr(lifecycle, "_step_row_state", lambda _node_id: None)
    monkeypatch.setattr(lifecycle, "emit_product_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_runtime, "_check_installed", lambda: None)
    monkeypatch.setattr(browser_runtime, "PROCESS_OWNER", SimpleNamespace(browser_sessions=lambda: owner))
    try:
        result = await browser_actions.BrowserPersonStepTool().execute(
            "Sign in to Google", _thread_id="authoritative-thread", _thread_project_id="project-1",
        )
    finally:
        lifecycle._steps.pop("authoritative-thread", None)
        lifecycle._answers.pop("authoritative-thread", None)
        lifecycle._sessions.pop("authoritative-thread", None)

    assert result == "Person step opened; end your turn."
    assert admitted == ["authoritative-thread"]
    assert writes == [("open_user_step", "Your turn in the shared browser (google.com)")]


@pytest.mark.asyncio
async def test_browser_person_step_failure_logs_its_cause_without_claiming_success(monkeypatch, caplog) -> None:
    import logging
    from tools import browser_actions, browser_runtime
    from plans.public import PlanError

    class FailingOwner:
        async def declare_person_step(self, _thread_id, _instruction):
            raise PlanError("fixture receipt write failed")

    monkeypatch.setattr(browser_runtime, "_check_installed", lambda: None)
    monkeypatch.setattr(browser_runtime, "PROCESS_OWNER", SimpleNamespace(browser_sessions=lambda: FailingOwner()))
    with caplog.at_level(logging.ERROR, logger=browser_runtime.__name__):
        result = await browser_actions.BrowserPersonStepTool().execute(
            "Choose your account", _thread_id="authoritative-thread", _thread_project_id="project-1",
        )
    assert result.startswith("Error: browser person step could not be opened.")
    [record] = caplog.records
    assert "declaration failed for thread authoritative-thread" in record.message
    assert isinstance(record.exc_info[1], PlanError)
    assert "fixture receipt write failed" in caplog.text


@pytest.mark.asyncio
async def test_browser_close_closes_only_authoritative_thread(monkeypatch) -> None:
    from tools import browser, browser_runtime

    calls: list[str] = []

    class _Owner:
        async def close_browser(self, thread_id):
            calls.append(thread_id)
            return "Browser session closed."

    monkeypatch.setattr(browser_runtime, "PROCESS_OWNER", _Owner())

    result = await browser.BrowserCloseTool().execute(
        _thread_id="authoritative-thread",
        _thread_project_id="project-1",
    )

    assert result == "Browser session closed."
    assert calls == ["authoritative-thread"]


@pytest.mark.asyncio
async def test_thread_has_live_lease_uses_authoritative_thread_identity(
    monkeypatch,
) -> None:
    from host_access import supervisor
    from tools.shell_cwd import _thread_has_live_lease

    observed: list[str] = []

    async def resolve(thread_id: str):
        observed.append(thread_id)
        return {"id": "lease-1"} if thread_id == "child-thread" else None

    monkeypatch.setattr(
        supervisor,
        "resolve_active_host_access_lease",
        resolve,
    )

    assert await _thread_has_live_lease("child-thread") is True
    assert await _thread_has_live_lease("other-thread") is False
    assert observed == ["child-thread", "other-thread"]


@pytest.mark.asyncio
async def test_host_true_bash_with_live_lease_but_no_permit_refuses_downgrade(
    tmp_path,
    monkeypatch,
) -> None:
    """A host:true call under a live lease is refused loudly, never de-scoped.

    host:true is the only bash call reviewed under a lease, so reaching
    execution with no permit means the review/permit path was bypassed or lost.
    The command must not quietly run under the narrower protected profile.
    """
    from core.turn_core import TurnIdentity
    from host_access import supervisor
    from tools import host_access_gate, shell

    probes: list[str] = []

    async def consume(**_kwargs):
        return None

    async def resolve(thread_id):
        assert thread_id == "thread-1"
        return {"id": "lease-1", "state": "active"}

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(
        supervisor,
        "resolve_active_host_access_lease",
        resolve,
    )
    monkeypatch.setattr(
        shell,
        "list_registered_repo_policies",
        lambda: probes.append("protected-path") or [],
    )

    result = await shell.BashTool(
        working_dir=str(tmp_path),
        default_timeout=120,
    ).execute(
        "printf ok",
        host=True,
        _thread_id="thread-1",
        _thread_project_id="project-1",
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert result.startswith(
        "Error: this thread has access to an active host-access lease"
    )
    assert "protected scope" in result
    assert probes == []


@pytest.mark.asyncio
async def test_default_bash_under_live_lease_runs_protected_not_refused(
    tmp_path,
    monkeypatch,
) -> None:
    """RFC §9.1 at the effect boundary: a default (host:false) bash call under a
    live lease runs the protected envelope and is never refused as a downgrade.
    """
    from core.turn_core import TurnIdentity
    from host_access import supervisor
    from infra.project_repo_policy import RepoPolicyQueryError
    from tools import host_access_gate, shell

    async def consume(**_kwargs):
        return None

    async def resolve(_thread_id):
        raise AssertionError(
            "a default bash call must not consult live-lease state"
        )

    def probe_protected_path():
        raise RepoPolicyQueryError("probe: reached protected path")

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(
        supervisor,
        "resolve_active_host_access_lease",
        resolve,
    )
    monkeypatch.setattr(
        shell,
        "list_registered_repo_policies",
        probe_protected_path,
    )

    result = await shell.BashTool(
        working_dir=str(tmp_path),
        default_timeout=120,
    ).execute(
        "printf ok",
        _thread_id="thread-1",
        _thread_project_id="project-1",
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    # Reaches the protected policy path rather than the live-lease refusal.
    assert "repository policy enforcement is unavailable" in result
    assert "probe: reached protected path" in result


@pytest.mark.asyncio
async def test_bash_without_any_lease_still_takes_protected_path(
    tmp_path,
    monkeypatch,
) -> None:
    """No live lease means the ordinary protected path is unchanged."""
    from core.turn_core import TurnIdentity
    from host_access import supervisor
    from infra.project_repo_policy import RepoPolicyQueryError
    from tools import host_access_gate, shell

    async def consume(**_kwargs):
        return None

    async def resolve(_thread_id):
        return None

    def probe_protected_path():
        raise RepoPolicyQueryError("probe: reached protected path")

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(
        supervisor,
        "resolve_active_host_access_lease",
        resolve,
    )
    monkeypatch.setattr(
        shell,
        "list_registered_repo_policies",
        probe_protected_path,
    )

    result = await shell.BashTool(
        working_dir=str(tmp_path),
        default_timeout=120,
    ).execute(
        "printf ok",
        _thread_id="thread-1",
        _thread_project_id="project-1",
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert "repository policy enforcement is unavailable" in result
    assert "probe: reached protected path" in result


@pytest.mark.asyncio
async def test_stale_thread_default_cwd_degrades_to_workspace_root(
    tmp_path,
    monkeypatch,
) -> None:
    """RFC §9.8: a stale thread-default cwd (no working_dir passed) that is
    outside protected scope degrades to the workspace root and executes rather
    than raising a false out-of-scope lease error.
    """
    from core.turn_core import TurnIdentity
    from host_access.processes import ForegroundProcessResult
    from infra import config
    from tools import host_access_gate, shell

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    stale_default = tmp_path / "old-pre-migration-workspace"
    stale_default.mkdir()
    allowed_root = workspace_root.resolve(strict=False)
    captured: dict[str, object] = {}

    async def no_permit(**_kwargs):
        return None

    class _Owner:
        async def spawn(self, _argv, **kwargs):
            captured["cwd"] = kwargs["cwd"]
            return ForegroundProcessResult(
                record=None,
                stdout=b"ok",
                stderr=b"",
                returncode=0,
            )

    monkeypatch.setattr(
        host_access_gate, "consume_current_action_permit", no_permit
    )
    monkeypatch.setattr(shell, "PROCESS_OWNER", _Owner())
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [])
    monkeypatch.setattr(
        shell, "controlled_fast_forward_policy", lambda *_a, **_k: None
    )
    monkeypatch.setattr(shell.BashTool, "_guard_command", lambda *_a, **_k: None)
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell, "execution_envelope_profile", lambda *_a, **_k: "")
    monkeypatch.setattr(
        shell,
        "safehouse_argv",
        lambda command, *_a, **_k: ["safehouse", "--", "bash", "-c", command],
    )
    # Only the workspace root is in protected scope; the stale default is not.
    monkeypatch.setattr(
        shell,
        "is_path_allowed",
        lambda path, **_k: Path(path).resolve(strict=False) == allowed_root,
    )
    monkeypatch.setattr(config, "WORKSPACE", workspace_root)

    result = await shell.BashTool(
        working_dir=str(stale_default),
        default_timeout=120,
    ).execute(
        "printf ok",
        _thread_id="thread-1",
        _thread_project_id=None,
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert result == "ok"
    assert captured["cwd"] == allowed_root


@pytest.mark.asyncio
async def test_explicit_out_of_scope_working_dir_denies_with_variant_a(
    tmp_path,
    monkeypatch,
) -> None:
    """RFC §9.8: an explicitly passed out-of-scope working_dir is never degraded
    — it denies with the state-aware §7a guidance (Variant A, no lease).
    """
    from core.turn_core import TurnIdentity
    from host_access import supervisor
    from tools import host_access_gate, shell

    async def no_permit(**_kwargs):
        return None

    async def no_lease(_thread_id):
        return None

    monkeypatch.setattr(
        host_access_gate, "consume_current_action_permit", no_permit
    )
    monkeypatch.setattr(
        supervisor, "resolve_active_host_access_lease", no_lease
    )
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [])
    monkeypatch.setattr(
        shell, "controlled_fast_forward_policy", lambda *_a, **_k: None
    )
    monkeypatch.setattr(shell.BashTool, "_guard_command", lambda *_a, **_k: None)
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell, "is_path_allowed", lambda *_a, **_k: False)

    outside = tmp_path / "outside"
    outside.mkdir()
    result = await shell.BashTool(default_timeout=120).execute(
        "printf ok",
        working_dir=str(outside),
        _thread_id="thread-1",
        _thread_project_id=None,
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert result.startswith("Blocked by protected scope")
    assert str(outside.resolve(strict=False)) in result
    assert "request a host-access lease" in result
    assert "`host: true`" in result


@pytest.mark.asyncio
async def test_registered_repo_cwd_is_never_degraded_when_path_check_lags(
    tmp_path,
    monkeypatch,
) -> None:
    """A cwd within a registered repo is in scope and is never relocated.

    Regression guard for the real worktree-lifecycle path: even when
    is_path_allowed's authoritative policy view lags and reports the repo as
    out-of-scope, a guard-validated repo/worktree command must still execute in
    the repo — never silently degraded to the workspace root.
    """
    from core.turn_core import TurnIdentity
    from host_access.processes import ForegroundProcessResult
    from infra import config
    from infra.project_repo_policy import RepoPolicy
    from tools import host_access_gate, shell

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    captured: dict[str, object] = {}

    async def no_permit(**_kwargs):
        return None

    class _Owner:
        async def spawn(self, _argv, **kwargs):
            captured["cwd"] = kwargs["cwd"]
            return ForegroundProcessResult(
                record=None,
                stdout=b"ok",
                stderr=b"",
                returncode=0,
            )

    monkeypatch.setattr(
        host_access_gate, "consume_current_action_permit", no_permit
    )
    monkeypatch.setattr(shell, "PROCESS_OWNER", _Owner())
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [policy])
    monkeypatch.setattr(
        shell, "controlled_fast_forward_policy", lambda *_a, **_k: None
    )
    monkeypatch.setattr(shell.BashTool, "_guard_command", lambda *_a, **_k: None)
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell, "execution_envelope_profile", lambda *_a, **_k: "")
    monkeypatch.setattr(
        shell,
        "safehouse_argv",
        lambda command, *_a, **_k: ["safehouse", "--", "bash", "-c", command],
    )
    # Force the out-of-scope branch: the registered repo is absent from the
    # path allowlist, mimicking the authoritative-policy lag the real
    # worktree-lifecycle test hits.
    monkeypatch.setattr(shell, "is_path_allowed", lambda *_a, **_k: False)
    monkeypatch.setattr(config, "WORKSPACE", workspace_root)

    result = await shell.BashTool(
        working_dir=str(repo),
        default_timeout=120,
    ).execute(
        "git worktree add .worktrees/feature",
        _thread_id="thread-1",
        _thread_project_id=None,
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert result == "ok"
    # cwd stays the repo — never relocated to the workspace root.
    assert captured["cwd"] == repo.resolve(strict=False)
    assert captured["cwd"] != workspace_root.resolve(strict=False)
