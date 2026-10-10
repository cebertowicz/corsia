"""One deterministic canonical row for a native result after its row commits.

The native producer writes through the canonical tool-result writer, so its row
carries the deterministic invocation identity and the canonical error flag, and
a raw ``None`` result is normalised once to the stored empty text. When
publication fails after that row commits, the retained-result path proves the
row is the same invocation and returns it instead of refusing.

Each case asserts, in the order the code runs it:

1. the fault happened after the canonical row committed (one row, its content,
   its turn, step and provider call);
2. the retained writer returns that SAME row instead of refusing;
3. that row is the canonical writer's deterministic identity;
4. the row's error state is ``is_error_result(content)``, with a raw ``None``
   result normalised to the stored empty text;
5. the Turn terminal applies over the same row.
"""
from __future__ import annotations

import pytest

from core._tool_result_delivery_support import message_id
from core.turn_core import persist_turn_terminal
from infra.completed_tool_results import persist_completed_tool_results
from infra.thread_driver import TerminalResult
from tests import native_tool_result_lab as tray
from tools.base import Tool
from tools.recorded_call import CompletedToolFailure


@pytest.fixture
def lab(tmp_path, monkeypatch, isolated_db):
    lab = tray.install(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "subagents.harness_checkpoint_resume.maintain_external_tool_boundary",
        _no_boundary,
    )
    yield lab
    lab.db.close()


async def _no_boundary(*_args, **_kwargs):
    return False


def _bash(lab):
    from tools import shell

    return shell.BashTool(working_dir=str(lab.tmp_path), default_timeout=120)


def _status(lab):
    return lab.db.execute(
        "SELECT status FROM active_turns WHERE id=?", (lab.identity.turn_id,),
    ).fetchone()["status"]


async def _post_commit_fault(
    lab, arguments, *, outcome, call_id="call-1", tool=None,
    expected_content=None, expected_raw=None, expected_is_error=False,
):
    """One post-commit publication fault, driven to the Turn terminal.

    ``expected_content`` is the canonical text the row must hold;
    ``expected_raw`` is the value the retained call must carry for the writer to
    prove identity.
    """
    with pytest.raises(CompletedToolFailure) as raised:
        await tray.native_call(
            lab, tool or _bash(lab), arguments, call_id=call_id, fail_frame="tool_done",
        )
    [completed] = raised.value.completed
    rows = lab.tool_rows()
    # (1) the row committed before the fault, and it is this call's one row.
    assert len(rows) == 1
    (row_id, content, metadata) = rows[0]
    assert metadata["turn_id"] == lab.identity.turn_id
    assert metadata["tool_call_id"] == call_id
    assert completed.assistant_step_id == metadata["assistant_step_id"]
    assert content == (expected_content if expected_content is not None else completed.result)
    # (2) the retained writer returns that same row instead of refusing.
    [replayed] = persist_completed_tool_results(
        lab.identity.turn_id, raised.value, db=lab.db,
    )
    assert replayed.message_id == row_id
    assert [row[0] for row in lab.tool_rows()] == [row_id]
    # (3) that one row is the canonical writer's deterministic identity.
    assert replayed.message_id == message_id(
        "thread-1", metadata["assistant_step_id"], call_id,
    )
    assert replayed.invocation_id == call_id
    # (4) the canonical error state and the retained call's exact bytes agree.
    if expected_raw is not None:
        assert completed.result == expected_raw
    assert replayed.is_error is expected_is_error
    assert bool(metadata.get("is_error")) is expected_is_error
    # (5) the Turn terminal applies over the same row.
    assert persist_turn_terminal(
        lab.identity.turn_id, outcome, raised.value, db=lab.db,
    ) is TerminalResult.APPLIED
    assert _status(lab) != "running"
    return row_id, content, metadata


@pytest.mark.asyncio
async def test_post_commit_fault_success_result_is_one_row_the_writer_replays(lab):
    row_id, content, metadata = await _post_commit_fault(
        lab, {"command": lab.command("ok")}, outcome="error",
    )
    assert tray.SENTINEL in content and metadata["tool_invocation_id"] == "call-1"


@pytest.mark.asyncio
async def test_post_commit_fault_error_result_carries_the_canonical_error_flag(lab):
    row_id, content, metadata = await _post_commit_fault(
        lab, {"command": lab.command("hang"), "timeout": 1}, outcome="error",
        expected_is_error=True,
    )
    # Post-fix contract: the canonical writer stamps is_error for a real error
    # result, which is what every other writer already does. Today the row
    # carries no flag at all; that observation lives in the passing control
    # test_native_error_result_projects_failed_from_text_without_a_stored_flag.
    from infra.tool_result_errors import is_error_result

    assert content.startswith("Error:") and is_error_result(content)
    assert metadata["is_error"] is True
    assert metadata["tool_invocation_id"] == "call-1"


@pytest.mark.asyncio
async def test_post_commit_fault_none_result_is_normalised_to_the_canonical_text(lab):
    row_id, content, metadata = await _post_commit_fault(
        lab, {}, outcome="error", tool=_NoneResultTool(),
        # The row already stores the executor's empty text today; the post-fix
        # contract is that the retained call carries that same canonical value,
        # because infra/completed_tool_results compares them byte for byte.
        expected_content="", expected_raw="",
    )
    assert content == "" and metadata["tool_invocation_id"] == "call-1"


class _NoneResultTool(Tool):
    """A test-local tool whose raw result is None, not the empty string."""

    name = "lab_none_result"
    description = "returns None"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **_kwargs):
        return None