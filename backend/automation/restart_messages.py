"""Shared agent-facing messages for restart and turn lifecycle refusals."""

RESTART_DRAIN_REFUSAL = (
    "Error: CORSIA is restarting. Until it is back, every tool call except "
    "restart_resume is refused, so retrying or switching tools will not help. "
    "If unfinished work must continue after startup, call restart_resume now "
    "(load it with tools_load if it is not in your tool list); it will wake this "
    "thread when CORSIA is back. Otherwise end your turn."
)
TURN_STOPPED_REFUSAL = (
    "Error: This Turn is being stopped; end your turn. Do not retry."
)
