"""Checkpoint launch reserve and pressure threshold."""

from core.fork_types import FORK_OUTPUT_TOKENS, FORK_TOOL_ROUND_TRIP_TOKENS

# A warm fork needs its output reserve, checkpoint instruction and tool round trip.
CHECKPOINT_INSTRUCTION_BUDGET_TOKENS = 8_192
WARM_FORK_RESERVE_TOKENS = (
    FORK_OUTPUT_TOKENS + CHECKPOINT_INSTRUCTION_BUDGET_TOKENS + FORK_TOOL_ROUND_TRIP_TOKENS
)


def soft_pressure_remaining(context_window: int) -> int:
    """Return the one static launch reserve, raised so a warm fork can finish."""
    if isinstance(context_window, bool) or not isinstance(context_window, int):
        raise ValueError("context window must be a positive integer")
    if context_window <= 0:
        raise ValueError("context window must be a positive integer")
    base = min(32_768, max(8_192, context_window // 10))
    return min(max(base, WARM_FORK_RESERVE_TOKENS), max(base, context_window // 4))


def at_checkpoint_launch_point(current_tokens: int, context_window: int) -> bool:
    """Return whether occupancy has reached the pressure checkpoint launch point."""
    return context_window - current_tokens <= soft_pressure_remaining(context_window)
