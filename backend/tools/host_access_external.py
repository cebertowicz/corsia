"""Classify external-agent host launches from immutable lease data only."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from infra.host_access_exceptions import (
    EXTERNAL_AGENT_POLICY,
    external_agent_exception_matches,
)


class ExternalLaunchMismatch(ValueError):
    """An external lease was used for a different command or directory."""


def classify_external_launch(
    lease: Mapping[str, Any],
    *,
    host_bash: bool,
    command: str | None,
    working_directory: Path | None,
) -> bool:
    """Return trusted external mode, refusing a mismatched external lease.

    An external-agent lease's human approval covers its one bound launch, so
    every other action under that lease is refused, not sent to review.
    """
    if host_bash and (command is None or working_directory is None):
        raise ExternalLaunchMismatch("External host launch binding is unavailable.")
    requested = any(
        isinstance(exception, Mapping)
        and exception.get("policy") == EXTERNAL_AGENT_POLICY
        for exception in lease.get("requested_policy_exceptions", [])
    )
    if not requested:
        return False
    if (
        host_bash
        and command is not None
        and working_directory is not None
        and external_agent_exception_matches(
            lease, command=command, working_directory=working_directory
        )
    ):
        return True
    raise ExternalLaunchMismatch(
        "This lease's human approval covers only the external-agent launch "
        "it names (exact command and working directory), and this action "
        "is not that launch. To do anything else, call "
        "release_host_access({}) for this thread's own request, then submit "
        "a new application."
    )


def external_agent_sandbox_rules(
    generated_profile: Path, safehouse_paths: list[Path]
) -> str:
    """Render canonical hard-floor rules for an external child envelope."""
    from infra.config import WORKSPACE
    from tools.host_path_policy import (
        GENERATED_WORKSPACE_PROJECTIONS,
        current_protected_host_path_policy,
    )

    return current_protected_host_path_policy(
        generated_profile=generated_profile,
        safehouse_paths=safehouse_paths,
        write_roots=tuple(WORKSPACE / path for path in GENERATED_WORKSPACE_PROJECTIONS),
    ).sandbox_rules()
