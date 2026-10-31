"""Helpers for tests that watch the backend's Git argv."""

from __future__ import annotations

from infra.backend_git import git_argv


def git_subcommand(command) -> str | None:
    """The Git subcommand of a backend ``command``, past the shared leading options."""
    prefix = git_argv()
    if list(command[:len(prefix)]) != prefix or len(command) == len(prefix):
        return None
    return command[len(prefix)]
