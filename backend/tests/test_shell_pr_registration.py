from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from loguru import logger

from core.turn_core import TurnIdentity
from core.turn_core import persist_turn_start
from events import observe_rules
from infra.database import get_db
from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL
from infra.thread_driver import admit_input
from registry import tool_result_observation as observation_module
from registry import models
from registry.command_match import command_matches_rule
from subagents.child_thread import persist_tool_use
from subagents.harness_tool_bridge import execute_tool
from tests.registry_helpers import artifact_rows_for_thread, make_session
from tools import shell as shell_module
from tools.registry import ToolRegistry
from tools.shell import (
    BASH_REGISTRATION_WARNING_RESULT_CAP,
    BashTool,
)


_URL = "https://github.com/owner/repo/pull/42"


def _shipped_rule() -> observe_rules.ObserveRule:
    """The shipped gh pr create row, as a fresh install seeds it."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(EVENT_RULE_SCHEMA_SQL)
    db.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, conductor_thread_id TEXT)")
    observe_rules.sync_shipped_rules(db)
    return observe_rules.from_row(db.execute("SELECT * FROM event_rules").fetchone())


_GITHUB_TRIGGER = _shipped_rule()


def _direct_shell_context(thread_id: str) -> dict[str, object]:
    return {
        "_thread_id": thread_id,
        "_host_access_turn_identity": TurnIdentity(
            f"{thread_id}-turn",
            1,
        ),
    }


def _persist_test_turn(db, thread_id: str) -> TurnIdentity:
    turn_id = f"{thread_id}-turn"
    admission = admit_input(
        thread_id,
        role="user",
        content="test bash tool execution",
        intent="apply_now",
        message_id=f"{turn_id}:input",
        turn_id=turn_id,
        boot_epoch="test-boot",
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None
    identity = persist_turn_start(
        turn_id,
        thread_id,
        "native/test",
        "test bash tool execution",
        db=db,
    )
    db.commit()
    return identity


class _Process:
    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.pid = 999_997
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _stub_process(
    monkeypatch,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> None:
    from host_access import processes

    async def create_subprocess(*_args, **_kwargs):
        return _Process(stdout, stderr, returncode)

    monkeypatch.setattr(
        shell_module.asyncio,
        "create_subprocess_exec",
        create_subprocess,
    )
    monkeypatch.setattr(
        processes,
        "_process_creation_fingerprint",
        lambda _pid: "test-fingerprint",
    )
    monkeypatch.setattr(shell_module, "list_registered_repo_policies", lambda: [])
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell_module, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell_module, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell_module, "is_path_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        shell_module,
        "execution_envelope_profile",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        shell_module,
        "safehouse_argv",
        lambda command, *_args, **_kwargs: [
            "safehouse",
            "--",
            "bash",
            "-c",
            command,
        ],
    )
    monkeypatch.setattr(
        shell_module,
        "check_worktree_targets",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        shell_module,
        "check_command_safety",
        lambda *_args: None,
    )


def _pr_rows(db, thread_id: str) -> list[dict]:
    return [
        row
        for row in artifact_rows_for_thread(db, thread_id)
        if row["integration"] == "github" and row["type"] == "pull-request"
    ]


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --title $HOME",
        "gh pr create --title ${HOME}",
        "gh pr create --title $?",
        "gh pr create --title $$",
        "gh pr create --title $1",
        "gh pr create --title $@",
        'gh pr create --title "$HOME"',
        r'gh pr create --title "\\$HOME"',
        'gh pr create --title "$(whoami)"',
        'gh pr create --title "`whoami`"',
    ],
)
def test_direct_classifier_rejects_active_shell_expansion(command):
    assert not command_matches_rule(command, _GITHUB_TRIGGER)


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --title '$HOME ${HOME} $? $(whoami) `whoami`'",
        r"gh pr create --title \$HOME",
        r"gh pr create --title \`literal\`",
        r'gh pr create --title "\$HOME \`literal\`"',
        "gh pr create --title '&&'",
        'gh pr create --title "x && y"',
        r"gh pr create --title \&\&",
    ],
)
def test_direct_classifier_allows_literal_shell_metacharacters(command):
    assert command_matches_rule(command, _GITHUB_TRIGGER)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [(_URL + "\n", ""), ("", _URL + "\n")],
)
async def test_direct_success_registers_from_full_output_and_notifies(
    isolated_db,
    monkeypatch,
    stdout,
    stderr,
):
    db = get_db()
    thread_id = make_session(db, prefix="bash-pr")
    notifications = []
    monkeypatch.setattr(observation_module, "notify_changed", notifications.append)
    _stub_process(monkeypatch, stdout=stdout, stderr=stderr)

    result = await BashTool().execute(
        "gh pr create --title 'Fix bug' --body ''",
        **_direct_shell_context(thread_id),
    )

    rows = _pr_rows(db, thread_id)
    assert len(rows) == 1
    assert rows[0]["external_id"] == "owner/repo#42"
    assert rows[0]["title"] == "PR #42"
    assert notifications and notifications[0]["external_id"] == "owner/repo#42"
    assert _URL in result


@pytest.mark.asyncio
async def test_url_beyond_shell_truncation_still_registers(
    isolated_db,
    monkeypatch,
):
    db = get_db()
    thread_id = make_session(db, prefix="bash-long")
    _stub_process(monkeypatch, stdout=("x" * 10_100) + "\n" + _URL + "\n")

    result = await BashTool().execute(
        "gh pr create --title long",
        **_direct_shell_context(thread_id),
    )

    assert len(_pr_rows(db, thread_id)) == 1
    assert "truncated" in result
    assert _URL not in result


@pytest.mark.asyncio
async def test_nonzero_pr_command_never_registers(isolated_db, monkeypatch):
    db = get_db()
    thread_id = make_session(db, prefix="bash-failed")
    _stub_process(monkeypatch, stdout=_URL + "\n", returncode=1)

    result = await BashTool().execute(
        "gh pr create --title failed",
        **_direct_shell_context(thread_id),
    )

    assert _pr_rows(db, thread_id) == []
    assert "Exit code: 1" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --title x && echo done",
        "gh pr create --title x > /tmp/pr-url",
        'gh pr create --title "$(whoami)"',
        "$(gh pr create --title nested)",
        "env GH_TOKEN=x gh pr create --title wrapped",
        "/opt/homebrew/bin/gh pr create --title qualified",
        "command gh pr create --title aliased",
        "gh pr view 42",
        "gh pr create --title x | tee /tmp/pr-url",
    ],
)
async def test_only_literal_direct_gh_pr_create_is_eligible(
    isolated_db,
    monkeypatch,
    command,
):
    db = get_db()
    thread_id = make_session(db, prefix="bash-shape")
    _stub_process(monkeypatch, stdout=_URL + "\n")

    await BashTool().execute(command, **_direct_shell_context(thread_id))

    assert _pr_rows(db, thread_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        "created without a URL\n",
        _URL + "\n" + _URL + "\n",
        "http://github.com/owner/repo/pull/42\n",
        "https://github.com/owner/repo/pull/0\n",
        _URL + "?utm_source=cli\n",
        "Created: " + _URL + "\n",
        "https://github.com/owner/repo/pulls/42\n",
    ],
)
async def test_output_requires_one_canonical_full_line_url(
    isolated_db,
    monkeypatch,
    output,
):
    db = get_db()
    thread_id = make_session(db, prefix="bash-url")
    _stub_process(monkeypatch, stdout=output)

    await BashTool().execute(
        "gh pr create --title canonical",
        **_direct_shell_context(thread_id),
    )

    assert _pr_rows(db, thread_id) == []


@pytest.mark.asyncio
async def test_same_thread_registration_is_idempotent(isolated_db, monkeypatch):
    db = get_db()
    thread_id = make_session(db, prefix="bash-idempotent")
    _stub_process(monkeypatch, stdout=_URL + "\n")
    tool = BashTool()

    context = _direct_shell_context(thread_id)
    await tool.execute("gh pr create --title once", **context)
    await tool.execute("gh pr create --title once", **context)

    assert len(_pr_rows(db, thread_id)) == 1


@pytest.mark.asyncio
async def test_same_user_second_thread_preserves_original_owner(
    isolated_db,
    monkeypatch,
):
    db = get_db()
    owner_id = make_session(db, prefix="bash-owner", user_id="steve")
    second_id = make_session(db, prefix="bash-second", user_id="steve")
    _stub_process(monkeypatch, stdout=_URL + "\n")
    tool = BashTool()

    await tool.execute(
        "gh pr create --title owner",
        **_direct_shell_context(owner_id),
    )
    await tool.execute(
        "gh pr create --title second",
        **_direct_shell_context(second_id),
    )

    row = models.resolve("github", "pull-request", "owner/repo#42", db=db)
    assert row is not None and row["parent_thread_id"] == owner_id
    assert len(_pr_rows(db, owner_id)) == 1
    assert len(_pr_rows(db, second_id)) == 1


@pytest.mark.asyncio
async def test_cross_user_preowned_pr_cannot_be_claimed(isolated_db, monkeypatch):
    db = get_db()
    owner_id = make_session(db, prefix="bash-owner", user_id="steve")
    other_id = make_session(db, prefix="bash-other", user_id="leksi")
    _stub_process(monkeypatch, stdout=_URL + "\n")
    tool = BashTool()

    await tool.execute(
        "gh pr create --title owner",
        **_direct_shell_context(owner_id),
    )
    other_result = await tool.execute(
        "gh pr create --title other",
        **_direct_shell_context(other_id),
    )

    row = models.resolve("github", "pull-request", "owner/repo#42", db=db)
    assert row is not None and row["parent_thread_id"] == owner_id
    assert _pr_rows(db, other_id) == []
    assert "Warning: CORSIA couldn't register the file the command's output names" in other_result
    assert "artifact_register" in other_result


@pytest.mark.asyncio
async def test_registration_exception_is_logged_and_appended_without_failing_command(
    isolated_db,
    monkeypatch,
    caplog,
):
    db = get_db()
    thread_id = make_session(db, prefix="bash-warning")
    _stub_process(monkeypatch, stdout=_URL + "\n")

    def fail_registration(*_args, **_kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(observation_module, "observe_tool_result", fail_registration)

    sink = logger.add(caplog.handler, level="ERROR", format="{message}")
    try:
        result = await BashTool().execute(
            "gh pr create --title warning",
            **_direct_shell_context(thread_id),
        )
    finally:
        logger.remove(sink)

    assert result.startswith(_URL)
    assert not result.startswith("Error")
    assert "Warning: CORSIA couldn't register the file the command's output names" in result
    assert "artifact_register" in result
    assert "registry unavailable" in caplog.text
    assert _pr_rows(db, thread_id) == []


@pytest.mark.asyncio
async def test_slow_registration_does_not_block_event_loop_timer(
    isolated_db,
    monkeypatch,
):
    db = get_db()
    thread_id = make_session(db, prefix="bash-async")
    _stub_process(monkeypatch, stdout=_URL + "\n")
    events = []

    def slow_registration(*_args, **kwargs):
        events.append("db-start")
        time.sleep(0.05)
        events.append("db-done")
        artifact = {
            "integration": "github",
            "type": "pull-request",
            "external_id": "owner/repo#42",
        }
        kwargs["changed"].append(artifact)
        return artifact

    monkeypatch.setattr(observation_module, "observe_tool_result", slow_registration)
    monkeypatch.setattr(
        observation_module, "notify_changed", lambda _artifact: events.append("notified"),
    )

    async def timer():
        await asyncio.sleep(0.005)
        events.append("timer")

    await asyncio.gather(
        BashTool().execute(
            "gh pr create --title async",
            **_direct_shell_context(thread_id),
        ),
        timer(),
    )

    assert events.index("timer") < events.index("db-done")
    assert events[-1] == "notified"


@pytest.mark.asyncio
async def test_bash_registration_warning_budget_preserves_warning(
    isolated_db,
    monkeypatch,
):
    db = get_db()
    thread_id = make_session(db, prefix="bash-cap")
    _stub_process(monkeypatch, stdout=("x" * 6_000) + "\n" + _URL + "\n")

    def fail_registration(*_args, **_kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(observation_module, "observe_tool_result", fail_registration)

    result = await BashTool().execute(
        "gh pr create --title capped",
        **_direct_shell_context(thread_id),
    )

    assert len(result) <= BASH_REGISTRATION_WARNING_RESULT_CAP
    assert "Warning: CORSIA couldn't register the file the command's output names" in result
    assert "artifact_register" in result


@pytest.mark.asyncio
async def test_a_registration_note_keeps_the_full_output_budget(isolated_db, monkeypatch):
    db = get_db()
    thread_id = make_session(db, prefix="bash-note")
    stdout = ("x" * 6_000) + "\n" + _URL + "\n"
    _stub_process(monkeypatch, stdout=stdout)

    result = await BashTool().execute("gh pr create --title noted", **_direct_shell_context(thread_id))

    assert result.startswith(stdout.rstrip("\n")) and "truncated" not in result
    assert result.endswith("\n\nRegistered pull request owner/repo#42 to you.")



@pytest.mark.asyncio
async def test_d8_cc_bridge_executes_the_same_bash_registration_seam(
    isolated_db,
    monkeypatch,
):
    db = get_db()
    thread_id = make_session(db, prefix="bash-cc")
    _stub_process(monkeypatch, stdout=_URL + "\n")
    registry = ToolRegistry()
    registry.register(BashTool())
    turn_identity = _persist_test_turn(db, thread_id)
    arguments = {"command": "gh pr create --title cc"}
    assistant_step_id = persist_tool_use(
        thread_id,
        turn_id=turn_identity.turn_id,
        tool_id="bash-provider-call",
        name="bash",
        arguments=arguments,
        db=db,
    )
    db.commit()

    result = await execute_tool(
        registry,
        thread_id=thread_id,
        turn_identity=turn_identity,
        invocation_id="bash-invocation",
        provider_tool_call_id="bash-provider-call",
        assistant_step_id=assistant_step_id,
        name="bash",
        arguments=arguments,
    )

    assert _URL in result
    assert len(_pr_rows(db, thread_id)) == 1
