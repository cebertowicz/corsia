
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from tools import shell as shell_module  # noqa: E402
from tools.shell import BashTool  # noqa: E402
from core.turn_core import TurnIdentity  # noqa: E402


_SHELL_TURN = TurnIdentity("shell-self-turn", 1)


class _Process:
    pid = 999_996
    returncode = 0

    def __init__(self, value: str) -> None:
        self._value = value

    async def communicate(self):
        return self._value.encode(), b""

    async def wait(self):
        return self.returncode


def _stub_safehouse(monkeypatch, expected_value) -> None:
    from host_access import processes

    async def spawn(*_args, **kwargs):
        return _Process(expected_value(kwargs["env"]))

    monkeypatch.setattr(
        shell_module.asyncio,
        "create_subprocess_exec",
        spawn,
    )
    monkeypatch.setattr(
        processes,
        "_process_creation_fingerprint",
        lambda _pid: "test-fingerprint",
    )
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell_module, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell_module, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell_module, "list_registered_repo_policies", lambda: [])
    monkeypatch.setattr(shell_module, "is_path_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        shell_module,
        "execution_envelope_profile",
        lambda *_a, **_k: "",
    )
    monkeypatch.setattr(
        shell_module,
        "safehouse_argv",
        lambda command, *_a, **_k: ["safehouse", "--", "bash", "-c", command],
    )


@pytest.mark.asyncio
async def test_shell_exports_current_corsia_thread_id(monkeypatch, tmp_path):
    monkeypatch.delenv("CORSIA_THREAD_ID", raising=False)
    _stub_safehouse(
        monkeypatch,
        lambda env: env.get("CORSIA_THREAD_ID", "unset"),
    )

    result = await BashTool(working_dir=str(tmp_path)).execute(
        "printf %s \"$CORSIA_THREAD_ID\"",
        _thread_id="shell-self",
        _host_access_turn_identity=_SHELL_TURN,
    )

    assert result == "shell-self"


@pytest.mark.asyncio
async def test_shell_replaces_inherited_corsia_thread_id_with_current_thread(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("CORSIA_THREAD_ID", "parent-leak")
    _stub_safehouse(
        monkeypatch,
        lambda env: env.get("CORSIA_THREAD_ID", "unset"),
    )

    result = await BashTool(working_dir=str(tmp_path)).execute(
        "printf %s \"${CORSIA_THREAD_ID:-unset}\"",
        _thread_id="shell-self",
        _host_access_turn_identity=_SHELL_TURN,
    )

    assert result == "shell-self"
