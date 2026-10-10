"""Public composition surface for exact Claude Code handle retirement."""

from subagents.cc_handle_close import (
    close_and_finalize,
    close_live,
    emit_logical_close,
    finalize,
    finalize_pending_process_exit,
    settle_and_finalize_retired_live,
    stop,
)
from subagents.cc_process_exit import (
    finish_process_exit,
    handle_process_exit,
)
from subagents.cc_shutdown import (
    has_pending_or_live,
    is_live,
    retire,
    retire_live,
    shutdown_all,
)

__all__ = [
    "close_and_finalize", "close_live",
    "emit_logical_close", "finalize", "finalize_pending_process_exit",
    "finish_process_exit", "handle_process_exit", "has_pending_or_live",
    "is_live", "retire", "retire_live",
    "settle_and_finalize_retired_live", "shutdown_all", "stop",
]
