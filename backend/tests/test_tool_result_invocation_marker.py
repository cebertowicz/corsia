"""The echo marker survives blocks a harness appends after CORSIA's text.

Claude Code echoes an image ``read_file`` result as three blocks: CORSIA's
text (ending in the marker), the image, and a note of its own about the
image. Joined, the marker is no longer the last thing in the echo, and the
reader used to call that malformed provenance and refuse every such echo.
"""

from __future__ import annotations

from core.tool_result_invocation import (
    invocation_reference_from_echo,
    with_invocation_reference,
)
from subagents.cc_event_messages import from_user


def _cc_image_echo(text: str) -> str:
    [event] = from_user({"content": [{
        "type": "tool_result",
        "tool_use_id": "toolu_A",
        "content": [
            {"type": "text", "text": text},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png"}},
            {"type": "text", "text": "[Image: original 2400x1600, displayed at 1200x800.]"},
        ],
    }]})
    return event["content"]


def test_cc_image_echo_keeps_its_invocation_reference() -> None:
    echo = _cc_image_echo(with_invocation_reference("[Image: shot.png]", "inv-1"))

    assert invocation_reference_from_echo(echo) == (True, "inv-1")


def test_last_marker_wins_over_one_quoted_in_the_output() -> None:
    quoted = with_invocation_reference("old transcript line", "quoted")
    echo = _cc_image_echo(with_invocation_reference(quoted + "\nmore", "inv-1"))

    assert invocation_reference_from_echo(echo) == (True, "inv-1")


def test_a_cut_marker_is_still_malformed() -> None:
    whole = with_invocation_reference("result", "inv-1")

    assert invocation_reference_from_echo(whole[:-1]) == (True, None)
    assert invocation_reference_from_echo(whole[:-8]) == (True, None)
    assert invocation_reference_from_echo(whole[:-1] + "x") == (True, None)


def test_no_marker_reads_as_absent() -> None:
    assert invocation_reference_from_echo(_cc_image_echo("plain")) == (False, None)
