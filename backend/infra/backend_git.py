"""The one fixed set of Git options every backend Git runner shares.

These options stop Git running hooks, a filesystem monitor, the ext transport or
submodule recursion that a repository's own config names. Other
repository-named programs are out of scope for this layer.
"""

from __future__ import annotations

GIT_CONFIG_ARGS = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "protocol.ext.allow=never",
    "-c", "submodule.recurse=false",
)


def git_argv(*args: str) -> list[str]:
    """Return ``git`` with the shared options, followed by ``args``."""
    return ["git", *GIT_CONFIG_ARGS, *args]
