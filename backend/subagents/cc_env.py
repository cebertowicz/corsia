"""Clean environment for spawned ``claude`` processes.

A ``claude`` spawned by CORSIA must always present as a clean **top-level
subscription** invocation. Two classes of inherited env var break that and
must be stripped before every spawn:

- ``CLAUDE_CODE_*`` — nested-session markers (``CLAUDE_CODE_CHILD_SESSION``,
  ``CLAUDE_CODE_SESSION_ID``, ``CLAUDE_CODE_ENTRYPOINT``, ``CLAUDE_CODE_EXECPATH``).
  The backend inherits these whenever it is launched from inside a Claude Code
  session; a child ``claude`` that sees them behaves as a nested invocation and
  **silently falls back to API billing** instead of the subscription.
- ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` — API credentials the backend
  legitimately holds for direct-SDK cloud models. Removing them from the child
  means a spawned ``claude`` *cannot* bill API: it uses the subscription, or it
  fails loud ("login required") — never a silent API charge.

This is the single source of truth for the spawn env; do not pass raw
``os.environ`` to a ``claude`` process.

CORSIA owns continuity checkpoints and memory, so every Claude Code child
disables its independent auto-compaction path and its auto memory (the
system-prompt memory section and the injected ``MEMORY.md`` index).

CORSIA also owns when the user hears from an agent (``post_update`` under
Focus mode), so the child's silent-turn reminder is off: obeying it turns
every long turn into a stream of status lines.
"""

from __future__ import annotations

import os

_STRIP_PREFIXES = ("CLAUDE_CODE_",)
_STRIP_EXACT = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"})


def clean_claude_env() -> dict[str, str]:
    """Return ``os.environ`` minus nested-CC-session markers and API-auth vars.

    The result is safe to hand to ``create_subprocess_exec(env=...)`` for any
    ``claude`` spawn: it forces the subscription auth path and makes a silent
    API-billing fallback impossible.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_STRIP_PREFIXES) and k not in _STRIP_EXACT
    }
    env["DISABLE_AUTO_COMPACT"] = "1"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["CLAUDE_CODE_SILENT_TURN_REMINDER"] = "0"
    return env


def apply_claude_config_dir(
    env: dict[str, str],
    claude_dir: str | None,
) -> None:
    """Bind the explicit account root used by transcript discovery and launch."""
    from subagents.cc_sessions import resolve_claude_dir

    env["CLAUDE_CONFIG_DIR"] = resolve_claude_dir(claude_dir)
