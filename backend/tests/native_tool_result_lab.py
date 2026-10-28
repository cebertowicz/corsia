"""Lab tray for native tool-result tests. Not runtime code.

Drives the real ``BashTool``, process owner, native step handler and canonical
result writer with a real child process. Only the platform jail prefix is
removed, because the sandbox cannot nest inside the protected shell; jailed
forwarding of the invocation export is covered by
``test_real_safehouse_forwards_only_bound_invocation``.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys

from infra.database import get_db

SENTINEL = "CORSIA_OUTCOME_V1 "

# Child process whose output is one effect line and one sentinel line.
EMITTER = r'''
import os, sys, time
mode = sys.argv[1]
out = sys.stdout
out.write("lab-effect-done\n")
out.write("CORSIA_OUTCOME_V1 " + os.environ.get("CORSIA_TOOL_INVOCATION_ID", "") + "\n")
if mode == "hang":
    out.flush(); time.sleep(30)
'''


class Lab:
    """One seeded thread and Turn plus a command for the real foreground bash path."""

    def __init__(self, tmp_path, identity, db):
        self.tmp_path = tmp_path
        self.identity = identity
        self.db = db
        self.script = tmp_path / "lab_emit.py"
        self.script.write_text(EMITTER, encoding="utf-8")

    def command(self, mode: str) -> str:
        """``ok`` exits after its output; ``hang`` outlives the caller's timeout."""
        return shlex.join([sys.executable, str(self.script), mode])

    def tool_rows(self) -> list[tuple[str, str, dict]]:
        return [
            (row["id"], row["content"], json.loads(row["metadata"]))
            for row in get_db().execute(
                "SELECT id,content,metadata FROM messages "
                "WHERE thread_id='thread-1' AND role='tool' ORDER BY rowid",
            )
        ]


async def native_call(lab, tool, arguments, *, call_id="call-1", fail_frame=None):
    """Run one call through the native executor and its real step handler.

    ``fail_frame`` faults the broadcast of one frame type, which for
    ``tool_done`` is after the row commits.
    """
    from types import SimpleNamespace

    from core.agent import CORSIAAgent
    from core.step_handler import StepHandler
    from core.turns_steps import execute_tool_calls
    from infra import state
    from tests.plan_execution_helpers import registry_of, selected_plan

    selected_plan(lab.db, "thread-1")
    agent = CORSIAAgent(workspace=lab.tmp_path)
    agent.tools = registry_of(tool)
    response = SimpleNamespace(content="", reasoning_content=None, tool_calls=[
        SimpleNamespace(id=call_id, name=tool.name, arguments=arguments),
    ])

    async def broadcast(frame):
        if fail_frame and frame.get("type") == fail_frame:
            raise RuntimeError("synthetic publication fault")

    handler = StepHandler("thread-1", {"model": "test/model"}, broadcast, lab.identity.turn_id)
    handler.bind_turn_identity(lab.identity)

    bound_thread = state._current_thread_id.set("thread-1")
    bound_turn = state._current_turn_identity.set(lab.identity)
    try:
        await execute_tool_calls(agent, response, [], "thread-1", asyncio.Event(), handler.handle,
                                 [], "test/model", tool_result_budget_tokens=1000)
    finally:
        state._current_turn_identity.reset(bound_turn)
        state._current_thread_id.reset(bound_thread)


def install(tmp_path, monkeypatch) -> Lab:
    """Patch only the jail and policy lookups and seed one Turn."""
    from tests.test_cc_tool_result_echo_binding import _seed_thread
    from tools import host_access_gate, shell, shell_guard

    real_exec = asyncio.create_subprocess_exec

    async def unjailed(*argv, **kwargs):
        return await real_exec(*argv[argv.index("--") + 1:], **kwargs)

    async def no_permit(**_kwargs):
        return None

    # The process owner is one per process and remembers every Turn it closed;
    # an earlier test that ended the same thread-1/turn-1 must not close this one.
    monkeypatch.setattr(shell.PROCESS_OWNER, "_closed_turns", set())
    monkeypatch.setattr(host_access_gate, "consume_current_action_permit", no_permit)
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [])
    monkeypatch.setattr(shell, "controlled_fast_forward_policy", lambda *_a, **_k: None)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell, "is_path_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(shell_guard, "list_registered_repo_policies", lambda: [])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_rw", lambda: [tmp_path])
    monkeypatch.setattr(shell_guard, "get_sandbox_add_dirs_rw", lambda _thread_id, policies=None: [tmp_path])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_ro", lambda policies=None: [])
    monkeypatch.setattr(shell_guard.shutil, "which", lambda _name: "/opt/homebrew/bin/safehouse")
    if sys.platform == "linux":
        from tools import linux_sandbox_probe, linux_shell
        monkeypatch.setattr(linux_sandbox_probe, "sandbox_error", lambda: None)
        monkeypatch.setattr(linux_shell, "shell_argv", lambda command, *_args, **_kwargs:
                            ["bwrap", "--", "/bin/bash", "-lc", command])
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", unjailed)
    db = get_db()
    identity = _seed_thread(db)
    return Lab(tmp_path, identity, db)
