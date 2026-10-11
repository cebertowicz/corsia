"""Which key environment variables are set but no longer read.

CORSIA reads keys from the key store only. A person who still has one of these
variables set would otherwise wonder why it does nothing, so startup notes
whether each is *present* and the provider card and Settings › Keys row say it
is ignored. Only presence is recorded: the value is never read into anything.

The SDKs fall back to these variables when CORSIA passes no key, so once the
startup migration has had its one chance to import them, they are also dropped
from this process: an empty or removed key means no access, as the UI says.
"""

from __future__ import annotations

import os

#: Key store entry -> the variable that no longer feeds it.
IGNORED_VARIABLES: dict[str, str] = {
    "provider-anthropic": "ANTHROPIC_API_KEY",
    "provider-openai": "OPENAI_API_KEY",
    "web-search": "BRAVE_API_KEY",
}

_present: frozenset[str] = frozenset()


def note_ignored_environment() -> None:
    """Record, once at startup, which of the variables are set in this process."""
    global _present
    _present = frozenset(entry for entry, variable in IGNORED_VARIABLES.items() if variable in os.environ)


def ignored_variable(entry: str) -> str | None:
    """The variable name set for ``entry`` and ignored, or None."""
    return IGNORED_VARIABLES[entry] if entry in _present else None


def drop_ignored_environment() -> None:
    """Remove the variables from this process; call after the startup migration."""
    for variable in IGNORED_VARIABLES.values():
        os.environ.pop(variable, None)
