"""Working-directory scope resolution for the terminal sandbox.

Extracted from ``BashTool.execute``, which was 375 lines at ~57 decision
points. This owns one question: which directory may a shell command actually
run in, and when must it be refused instead.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable


log = logging.getLogger(__name__)


async def _thread_has_live_lease(thread_id: str) -> bool:
    """True when this thread can resolve a live in-memory host-access lease."""
    from host_access.supervisor import resolve_active_host_access_lease

    return await resolve_active_host_access_lease(thread_id) is not None


def _cwd_within_registered_repo(cwd: Path, policies) -> bool:
    """True when cwd is at or inside a registered repository root.

    A registered repository (and any path within it, including its
    ``.worktrees/``) is in protected scope: the guard and execution envelope
    already trust these policies. The stale-cwd degrade must never relocate such
    a path — that would run a guard-validated repo/worktree command from the
    wrong directory.
    """
    resolved = cwd.expanduser().resolve(strict=False)
    for policy in policies:
        root = Path(policy.path).expanduser().resolve(strict=False)
        if resolved == root or resolved.is_relative_to(root):
            return True
    return False


async def _resolve_execution_cwd(
    cwd: str,
    working_dir: str | None,
    *,
    leased: bool,
    policies: Any,
    thread_id: str | None,
    path_allowed: Callable[..., bool],
) -> tuple[Path, str | None]:
    """Resolve the sandbox working directory, or return a denial message.

    ``(resolved_cwd, None)`` means execution may proceed; ``(cwd, denial)``
    means the caller must refuse.

    ``path_allowed`` is injected rather than imported so the caller's binding is
    the one consulted — the shell tests patch it on the shell module, and an
    import here would quietly bypass that.

    A cwd at or inside a registered repository is in protected scope even if
    the authoritative policy view has not caught up yet: the
    guard and envelope already trust these policies. Only genuinely
    out-of-scope cwds are blocked or relocated.
    """
    # Deferred like its call site: tools.host_access_gate imports back into
    # this package, so a module-level import here would close a cycle.
    from tools.host_access_gate import protected_scope_denial

    canonical_cwd = Path(cwd).expanduser().resolve(strict=False)
    # The host path policy reads SQLite and runs Git, so it runs off the loop.
    cwd_out_of_scope = (
        not leased
        and not await asyncio.to_thread(path_allowed, canonical_cwd, for_write=False)
        and not _cwd_within_registered_repo(canonical_cwd, policies)
    )
    if not cwd_out_of_scope:
        return canonical_cwd, None

    if working_dir is not None:
        # An explicitly passed out-of-scope working_dir keeps exact semantics
        # and denies.
        lease_active = await _thread_has_live_lease(thread_id)
        return canonical_cwd, protected_scope_denial(
            f"the shell working directory {canonical_cwd} is outside "
            "protected scope",
            lease_active=lease_active,
        )

    # §6a: a stale thread-default or process cwd (no explicit working_dir)
    # that falls outside protected scope degrades to the configured workspace
    # root instead of raising a false out-of-scope error. Persisted thread
    # working directories re-break on every workspace move, so this is a
    # standing trap, not a one-off.
    from infra.config import WORKSPACE

    workspace_root = Path(WORKSPACE).expanduser().resolve(strict=False)
    log.warning(
        "bash cwd fallback %s is outside protected scope; "
        "degrading to workspace root %s (stale thread default)",
        canonical_cwd,
        workspace_root,
    )
    return workspace_root, None
