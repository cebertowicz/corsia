"""The room a warm checkpoint fork has for its own instruction."""

from __future__ import annotations

from core.checkpoint_fit import soft_pressure_remaining
from core.checkpoint_types import CHECKPOINT_TOOL_ROUND_TRIP_TOKENS


def fork_instruction_room(context_window: int, output_tokens: int) -> int:
    """Tokens a warm fork has for its instruction after the launch reserve.

    The reserve a launch holds back (``soft_pressure_remaining``) covers the
    fork's output and one tool round trip first; what is left is the room its
    instruction, the summary prompt and any review block, may fill. A window
    whose reserve is capped at a quarter of it has less, and may have none.
    """
    room = (
        soft_pressure_remaining(context_window)
        - output_tokens - CHECKPOINT_TOOL_ROUND_TRIP_TOKENS
    )
    return max(room, 0)
