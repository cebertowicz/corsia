"""A workflow's worker settings: the limits and requirements for the worker that runs it.

The block states requirements, never a model: the worker runs on its rule owner's
model (or that model's larger-context variant), and the runner refuses visibly at
start when that model can't meet them. Install checks the shape only, so a shared
workflow installs on any machine whatever its catalog holds.

    worker:
      effort: high
      max_turns: 200
      time_limit_minutes: 30
      min_context_tokens: 200000
      profiles:
        main: {min_context_tokens: 1000000}

A profile lays its keys over the base block; the caller names the profile.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_COUNTS = ("max_turns", "time_limit_minutes", "min_context_tokens")
SETTINGS = ("effort", *_COUNTS)


class UnknownProfile(ValueError):
    """The caller named a profile the workflow doesn't define."""


class InvalidWorker(ValueError):
    """The workflow's ``worker`` block fails its install-time shape check."""


def _settings_errors(block: Any, label: str, *, profiles_allowed: bool) -> list[str]:
    if not isinstance(block, dict):
        return [f"{label} must be a mapping"]
    allowed = {*SETTINGS, "profiles"} if profiles_allowed else set(SETTINGS)
    errors = [f"{label}.{key} is an unknown key" for key in block if key not in allowed]
    effort = block.get("effort")
    if effort is not None and not (isinstance(effort, str) and re.fullmatch(r"[a-z]+", effort)):
        errors.append(f"{label}.effort must be one lowercase word, such as high")
    for key in _COUNTS:
        value = block.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            errors.append(f"{label}.{key} must be a positive whole number")
    return errors


def worker_errors(workflow: Mapping[str, Any]) -> list[str]:
    """Install-time shape errors of ``workflow``'s ``worker`` block; none when it has none."""
    if "worker" not in workflow:
        return []
    block = workflow["worker"]
    errors = _settings_errors(block, "corsia.workflow.worker", profiles_allowed=True)
    profiles = block.get("profiles") if isinstance(block, dict) else None
    if profiles is None:
        return errors
    if not isinstance(profiles, dict):
        return [*errors, "corsia.workflow.worker.profiles must be a mapping of profile names"]
    for name, profile in profiles.items():
        errors.extend(_settings_errors(profile, f"corsia.workflow.worker.profiles.{name}", profiles_allowed=False))
    return errors


def worker_settings(corsia: Mapping[str, Any] | None, profile: str | None = None) -> dict[str, Any]:
    """The worker settings of a workflow skill's CORSIA data, with ``profile`` laid over the base.

    An unset key is None: the runner's own default applies. A workflow with no
    ``worker`` block has every key unset.

    Raises:
        InvalidWorker: the block fails ``worker_errors`` (a file edited after install).
        UnknownProfile: ``profile`` is not one the workflow defines.
    """
    workflow = (corsia or {}).get("workflow") or {}
    if not isinstance(workflow, Mapping):
        raise InvalidWorker("corsia.workflow must be a mapping")
    errors = worker_errors(workflow)
    if errors:
        raise InvalidWorker("; ".join(errors))
    block = workflow.get("worker") or {}
    settings = {key: block.get(key) for key in SETTINGS}
    if profile is None:
        return settings
    profiles = block.get("profiles") or {}
    if profile not in profiles:
        known = ", ".join(sorted(profiles)) or "none"
        raise UnknownProfile(f"the workflow defines no profile {profile!r}; its profiles: {known}")
    settings.update(profiles[profile])
    return settings
