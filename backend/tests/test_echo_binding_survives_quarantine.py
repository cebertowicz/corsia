"""A provider echo binds to the canonical result, not to quarantined residue.

Production 2026-08-14 (thread da670ce5, a FRESH thread's first turn): a tool
call was interrupted, leaving a ``quarantined_unbound`` row (partial bytes that
never became provider truth) beside the synthetic settled row the terminal
wrote. When Claude Code later echoed the tool result, ``_bind_echo`` counted
the quarantined row as a candidate: an echo carrying the original invocation
marker matched the quarantined row and could bind it, after which the call had
TWO bound rows and every later echo raised "does not identify one canonical
result" — killing the thread's turn on its first message.

A quarantined-unbound row is explicitly "never bound to a provider call"; it
must not compete to BE the binding.
"""

from __future__ import annotations

import json
import uuid
from contextlib import closing

import pytest


def _db():
    from infra.database import get_db

    return get_db()


def _seed(thread_id: str) -> str:
    step_id = str(uuid.uuid4())
    with closing(_db()) as db:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?,'steve','test/model','t','t')",
            (thread_id,),
        )
        rows = [
            (
                step_id,
                "assistant",
                "",
                {
                    "turn_id": "t1",
                    "llm_tool_calls": [{
                        "id": "toolu_A",
                        "function": {"name": "Read", "arguments": "{}"},
                    }],
                },
            ),
            (
                f"{thread_id}-quarantined",
                "tool",
                "partial bytes that never became provider truth",
                {
                    "turn_id": "t1",
                    "assistant_step_id": step_id,
                    "tool_invocation_id": "orig-inv-1",
                    "tool_result_binding_state": "quarantined_unbound",
                    "name": "Read",
                },
            ),
            (
                f"{thread_id}-settled",
                "tool",
                "No result was recorded before the turn ended",
                {
                    "turn_id": "t1",
                    "assistant_step_id": step_id,
                    "tool_call_id": "toolu_A",
                    "provider_tool_call_id": "toolu_A",
                    "tool_invocation_id": "synthetic:provider_missing_result:toolu_A",
                    "tool_result_binding_state": "bound",
                    "synthetic_tool_result": True,
                    "synthetic_reason": "provider_missing_result",
                    "name": "Read",
                },
            ),
        ]
        for seq, (rid, role, content, meta) in enumerate(rows):
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,metadata,"
                "created_at,delivery_state,delivery_revision,seq) "
                "VALUES (?,?,?,?,?,?,'delivered',1,?)",
                (rid, thread_id, role, content, json.dumps(meta), "t", seq + 1),
            )
        db.commit()
    return step_id


_MARKER = '\n\n[CORSIA_TOOL_INVOCATION reference={"invocation_id": "orig-inv-1"}]'


@pytest.mark.parametrize(
    "label,echo",
    [
        ("original_invocation_marker", "partial bytes" + _MARKER),
        (
            "marker_before_harness_image_blocks",
            "partial bytes" + _MARKER + "\n[image: image/png]\n[Image: resized note]",
        ),
        ("no_marker_call_id_only", "plain echoed bytes"),
        (
            "synthetic_marker",
            'x\n\n[CORSIA_TOOL_INVOCATION reference='
            '{"invocation_id": "synthetic:provider_missing_result:toolu_A"}]',
        ),
    ],
)
def test_every_echo_shape_binds_to_the_settled_row(
    isolated_db, label: str, echo: str,
) -> None:
    from core.tool_result_binding import _bind_echo

    thread_id = f"echo-{label}"
    step_id = _seed(thread_id)

    with closing(_db()) as db:
        result = _bind_echo(
            db=db,
            thread_id=thread_id,
            provider_tool_call_id="toolu_A",
            assistant_step_id=step_id,
            echoed_content=echo,
        )
        db.commit()

    assert result.message_id == f"{thread_id}-settled", (
        f"echo bound to {result.message_id!r} instead of the canonical result"
    )

    # And binding must be stable: a second echo still finds exactly one.
    with closing(_db()) as db:
        again = _bind_echo(
            db=db,
            thread_id=thread_id,
            provider_tool_call_id="toolu_A",
            assistant_step_id=step_id,
            echoed_content="another echo",
        )
    assert again.message_id == f"{thread_id}-settled"
