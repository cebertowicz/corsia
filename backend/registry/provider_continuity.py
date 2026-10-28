"""Registry identity types used for external harness continuity."""

HARNESS_CONTINUITY_TYPES = {
    "claude-code": ("claude-code", "session"),
    "codex": ("codex", "thread"),
}


def is_continuity(identity) -> bool:
    """A provider session record: it belongs to the thread that launched it."""
    return (identity["integration"], identity["type"]) in HARNESS_CONTINUITY_TYPES.values()
