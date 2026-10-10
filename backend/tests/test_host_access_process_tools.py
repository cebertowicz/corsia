from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest


def test_process_action_descriptors_are_typed_and_fresh_permit_gated(
    monkeypatch,
) -> None:
    from host_access import processes
    from tools import managed_processes
    from tools import host_access_gate

    target = processes.ProcessMetadata(
        pid=42,
        parent_pid=7,
        name="python",
        executable="/usr/bin/python3",
        arguments=("python3", "--token", "[REDACTED]"),
        create_time=123.5,
        executable_fingerprint="a" * 64,
    )
    monkeypatch.setattr(
        processes,
        "describe_bound_process",
        lambda **_kwargs: target,
    )

    discover = host_access_gate._prepare_action(
        "process_discover",
        {"pid": 42, "query": None, "limit": 5},
        tool=SimpleNamespace(),
    )
    signal = host_access_gate._prepare_action(
        "process_signal",
        {
            "pid": 42,
            "create_time": 123.5,
            "executable_fingerprint": "a" * 64,
            "signal": "terminate",
        },
        tool=SimpleNamespace(),
    )

    assert discover is not None and discover.requires_elevation is True
    assert discover.review_descriptor() == {
        "version": 1,
        "tool": "process_discover",
        "pid": 42,
        "query": None,
        "limit": 5,
    }
    assert signal is not None and signal.requires_elevation is True
    assert signal.review_descriptor() == {
        "version": 1,
        "tool": "process_signal",
        "pid": 42,
        "create_time": 123.5,
        "executable_fingerprint": "a" * 64,
        "signal": "terminate",
        "target": {
            "pid": 42,
            "parent_pid": 7,
            "name": "python",
            "executable": "/usr/bin/python3",
            "arguments": ["python3", "--token", "[REDACTED]"],
            "create_time": 123.5,
            "executable_fingerprint": "a" * 64,
        },
    }


@pytest.mark.asyncio
async def test_process_discover_consumes_permit_before_psutil_read(
    monkeypatch,
) -> None:
    from host_access import processes
    from tools import managed_processes
    from tools import host_access_gate

    events: list[str] = []

    async def consume(**kwargs):
        events.append("consume")
        assert kwargs["tool_name"] == "process_discover"
        return SimpleNamespace()

    def discover(**kwargs):
        events.append("discover")
        assert kwargs == {"pid": 42, "query": None, "limit": 5}
        return (
            processes.ProcessMetadata(
                pid=42,
                parent_pid=2,
                name="python",
                executable="/usr/bin/python",
                arguments=("python", "[REDACTED]"),
                create_time=123.5,
                executable_fingerprint="a" * 64,
            ),
        )

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(processes, "discover_processes", discover)

    result = await managed_processes.ProcessDiscoverTool().execute(
        pid=42,
        query=None,
        limit=5,
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert events == ["consume", "discover"]
    assert json.loads(result) == {
        "processes": [
            {
                "pid": 42,
                "parent_pid": 2,
                "name": "python",
                "executable": "/usr/bin/python",
                "arguments": ["python", "[REDACTED]"],
                "create_time": 123.5,
                "executable_fingerprint": "a" * 64,
            }
        ]
    }


@pytest.mark.asyncio
async def test_process_signal_consumes_permit_then_revalidates_exact_binding(
    monkeypatch,
) -> None:
    from host_access import processes
    from tools import managed_processes
    from tools import host_access_gate

    events: list[object] = []

    async def consume(**kwargs):
        events.append(("consume", kwargs["params"]))
        return SimpleNamespace()

    def send(**kwargs):
        events.append(("signal", kwargs))

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(processes, "signal_bound_process", send)

    result = await managed_processes.ProcessSignalTool().execute(
        pid=42,
        create_time=123.5,
        executable_fingerprint="a" * 64,
        signal="terminate",
        _thread_id="thread-1",
        _thread_project_id=None,
    )

    assert result == "Signaled exact PID 42 with terminate."
    assert events == [
        (
            "consume",
            {
                "pid": 42,
                "create_time": 123.5,
                "executable_fingerprint": "a" * 64,
                "signal": "terminate",
            },
        ),
        (
            "signal",
            {
                "pid": 42,
                "create_time": 123.5,
                "executable_fingerprint": "a" * 64,
                "signal_name": "terminate",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_process_signal_cancellation_waits_for_thread_effect_settlement(
    monkeypatch,
) -> None:
    from host_access import processes
    from tools import managed_processes
    from tools import host_access_gate

    started = threading.Event()
    release = threading.Event()
    settled = threading.Event()
    events: list[str] = []

    async def consume(**_kwargs):
        return SimpleNamespace()

    def send(**_kwargs):
        events.append("signal-started")
        started.set()
        assert release.wait(timeout=5)
        events.append("signal-settled")
        settled.set()

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(processes, "signal_bound_process", send)

    task = asyncio.create_task(
        managed_processes.ProcessSignalTool().execute(
            pid=42,
            create_time=123.5,
            executable_fingerprint="a" * 64,
            signal="terminate",
            _thread_id="thread-1",
            _thread_project_id=None,
        )
    )
    assert await asyncio.wait_for(asyncio.to_thread(started.wait, 2), timeout=3)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done(), "cancellation surfaced before signal settlement"
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert settled.is_set()
    assert events == ["signal-started", "signal-settled"]


@pytest.mark.asyncio
async def test_managed_stop_uses_handle_ownership_without_a_fresh_lease_permit(
    monkeypatch,
) -> None:
    from host_access import processes
    from tools import managed_processes
    from tools import host_access_gate

    async def unexpected(**_kwargs):
        raise AssertionError("managed stop must not consume a lease permit")

    class _Owner:
        async def stop(self, handle, *, thread_id, reason):
            assert (handle, thread_id, reason) == (
                "opaque",
                "thread-1",
                "managed_stop",
            )
            return SimpleNamespace(terminal_state="terminated")

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        unexpected,
    )

    result = await managed_processes.ManagedProcessStopTool(owner=_Owner()).execute(
        handle="opaque",
        _thread_id="thread-1",
    )

    assert json.loads(result) == {
        "handle": "opaque",
        "state": "terminated",
    }


@pytest.mark.asyncio
async def test_controlled_git_subcommand_uses_the_same_owned_process_group(
    tmp_path,
    monkeypatch,
) -> None:
    from host_access import processes
    from tools import managed_processes
    from infra.project_repo_policy import RepoPolicy
    from tools import shell

    repo = tmp_path / "repo"
    repo.mkdir()
    policy = RepoPolicy("project-1", "repo", repo, "main", True)
    calls: list[dict] = []

    class _Owner:
        async def spawn(self, argv, **kwargs):
            calls.append({"argv": list(argv), **kwargs})
            return processes.ForegroundProcessResult(
                record=SimpleNamespace(),
                stdout=b"oid\n",
                stderr=b"",
                returncode=0,
            )

    monkeypatch.setattr(shell, "PROCESS_OWNER", _Owner())
    monkeypatch.setattr(
        shell,
        "list_registered_repo_policies",
        lambda: [policy],
    )
    monkeypatch.setattr(
        shell,
        "execution_envelope_profile",
        lambda *_args, **_kwargs: "(version 1)",
    )
    monkeypatch.setattr(
        shell,
        "safehouse_argv",
        lambda *_args, **_kwargs: [
            "safehouse",
            "--profile",
            "profile.sb",
            "--",
            "/bin/zsh",
            "-lc",
            "",
        ],
    )

    result = await shell._run_controlled_git(
        repo,
        ["rev-parse", "HEAD"],
        "read HEAD",
        3.0,
        policy=policy,
        env={"PATH": "/usr/bin"},
        thread_id="thread-a",
        turn_id="turn-a",
        lease_id="lease-a",
        lease_revision=2,
        permit_id="permit-a",
    )

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.args == calls[0]["argv"]
    assert result.returncode == 0
    assert result.stdout == "oid\n"
    assert result.stderr == ""
    assert calls == [
        {
            "argv": [
                "safehouse",
                "--profile",
                "profile.sb",
                "--",
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "rev-parse",
                "HEAD",
            ],
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "lease_id": "lease-a",
            "lease_revision": 2,
            "permit_id": "permit-a",
            "cwd": repo,
            "env": {"PATH": "/usr/bin"},
            "timeout_seconds": 3.0,
            "background": False,
        }
    ]


def test_bash_background_mode_is_bound_into_the_reviewed_action_hash() -> None:
    from tools import host_access_gate

    tool = SimpleNamespace(default_timeout=120, working_dir=str(Path.cwd()))
    foreground = host_access_gate._prepare_action(
        "bash",
        {
            "command": "python -V",
            "background": False,
        },
        tool=tool,
    )
    background = host_access_gate._prepare_action(
        "bash",
        {
            "command": "python -V",
            "background": True,
        },
        tool=tool,
    )

    assert foreground is not None and background is not None
    assert foreground.action_hash != background.action_hash
    assert foreground.review_descriptor()["background"] is False
    assert background.review_descriptor()["background"] is True
