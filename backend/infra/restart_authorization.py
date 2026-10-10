"""Operator-gated authorization for the sanctioned self-restart capability.

'Restart this instance' is one generic capability gated by a single operator-set
flag on the instance itself — NOT by any dev-vs-prod or topology inference. The
operator decides once, per instance, whether agents may restart it; the flag IS
the authorization decision. Fail closed: unset, false, or unparseable means not
authorized, so an instance the operator has not opted in (e.g. production) can
never be restarted through this path.

The flag is read from the process environment, which the operator pins in the
instance's own launch configuration (e.g. the launchd plist). A running agent
cannot mutate its own process environment, so the gate is agent-immune — unlike
a mutable in-app setting an agent could flip.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess

# Operator-set, per-instance, agent-immune restart-authorization flag.
RESTART_FLAG_ENV = "CORSIA_AGENT_RESTART_ALLOWED"
_TRUTHY = {"1", "true", "yes", "on"}

# Serving checkout root: backend/infra/restart_authorization.py -> repo root.
# Resolves the checkout the running backend code is actually served from, used
# to record the serving SHA in the restart audit (best effort — not a gate).
_SERVING_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class RestartAuthorization:
    """This instance is operator-authorized for agent-initiated restart."""

    serving_root: str
    head_sha: str
    short_sha: str
    branch: str


@dataclass(frozen=True)
class RestartRefusal:
    """A fail-closed refusal to restart this instance on an agent's request."""

    reason: str


def agent_restart_allowed() -> bool:
    """Whether the operator enabled agent-initiated restart for THIS instance."""
    return (os.environ.get(RESTART_FLAG_ENV) or "").strip().lower() in _TRUTHY


def _git(args: list[str], cwd: Path) -> str:
    """Run one git command in ``cwd``; return stripped stdout, empty on failure."""
    try:
        out = subprocess.check_output(
            ["git", *args], cwd=cwd, stderr=subprocess.DEVNULL, timeout=3,
        )
        return out.decode("utf-8").strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def serving_head_sha() -> str:
    """Return the serving checkout's current HEAD sha, or empty on failure."""
    return _git(["rev-parse", "HEAD"], _SERVING_ROOT)


def authorize_self_restart() -> RestartAuthorization | RestartRefusal:
    """Authorize restarting THIS instance, or refuse (fail closed on the flag).

    The single gate is the operator flag. The serving SHA/branch are resolved
    best-effort for the audit trail and never block the restart (an instance the
    operator opted in may be a non-git deploy).
    """
    if not agent_restart_allowed():
        return RestartRefusal(
            reason=(
                f"Sanctioned restart refused: agent-initiated restart is not "
                f"enabled for this instance. An operator must set "
                f"{RESTART_FLAG_ENV} in this instance's own launch configuration "
                f"to allow it."
            )
        )

    head_sha = serving_head_sha()
    short_sha = (
        _git(["rev-parse", "--short=7", "HEAD"], _SERVING_ROOT)
        or (head_sha[:7] if head_sha else "unknown")
    )
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], _SERVING_ROOT) or "unknown"
    return RestartAuthorization(
        serving_root=str(_SERVING_ROOT),
        head_sha=head_sha or "unknown",
        short_sha=short_sha,
        branch=branch,
    )
