"""Model-facing refusal when a helper is still on a task moving to Later."""

HELPER_STILL_ON_TASK = (
    "Not moved to Later: helper {thread} is still on '{title}'. "
    "A task in Later is never worked. Stop the helper first "
    "(delegate_stop thread_id={thread}), then move it, or keep the task above the line."
)
