"""Filesystem path resolution helper used by all filesystem tools."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval.tool_allowlist import carry_in_hides


@dataclass(frozen=True)
class AuthorizedPath:
    """Canonical path plus whether a reviewed host permit authorized it."""

    resolved: Path
    reviewed: bool


def _join_relative_to_workspace(path: str, workspace: Path | None) -> Path:
    """Join a possibly-relative path onto ``workspace``; no resolve, no authorization.

    The one join rule every tool's target-path computation shares: relative
    paths resolve against the caller's own workspace, not any other notion
    of "current directory". Both ``join_and_resolve`` and ``resolve_path``
    call this same function for the join step, so there is exactly one
    place that decision is made — neither can silently diverge from it.
    """
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    return p


def join_and_resolve(path: str, workspace: Path | None = None) -> Path:
    """Join a possibly-relative path onto ``workspace`` and canonicalize it.

    Carries no authorization decision — callers needing the sandbox
    boundary use ``resolve_path``, which joins the same way (through
    ``_join_relative_to_workspace``) and then applies it.
    """
    return _join_relative_to_workspace(path, workspace).resolve(strict=False)


def canonical_target_path(path: str, workspace: Path | None = None) -> str:
    """Return a canonical, portable target identity relative to its workspace.

    Files inside a workspace retain a POSIX workspace-relative identity in
    durable lessons and outcome facts. Files outside it keep their resolved
    absolute identity, because there is no portable local spelling for them.
    """
    resolved = join_and_resolve(path, workspace=workspace)
    if workspace is not None:
        try:
            return resolved.relative_to(workspace.expanduser().resolve(strict=False)).as_posix()
        except ValueError:
            pass
    return str(resolved)


def resolve_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    *,
    for_write: bool = False,
    thread_id: str | None = None,
) -> Path:
    """Resolve path against workspace (if relative) and enforce directory restriction.

    The configured workspace/registered-repository boundary applies independent
    of whether the optional Safehouse executable is installed. ``allowed_dir``
    preserves the legacy denial detail. When supplied, ``thread_id`` applies
    the caller's project fence independently of registered-repo grants.
    """
    p = _join_relative_to_workspace(path, workspace)
    resolved = p.resolve(strict=False)
    import tools.confined_reads as confined_reads
    from tools.host_path_policy import ordinary_host_path_denial

    confined_reads.check(p, resolved, for_write=for_write)
    if thread_id:
        from tools.project_fence import fence_for_thread

        if refusal := fence_for_thread(thread_id).refusal(p):
            raise PermissionError(refusal)
    if not for_write and carry_in_hides(resolved, reading=True):
        raise FileNotFoundError(path)  # absent for this carry-in side

    for candidate in dict.fromkeys((p.absolute(), resolved)):
        if denial := ordinary_host_path_denial(
            candidate,
            for_write=for_write,
        ):
            raise PermissionError(denial)

    outside_allowed_dir = False
    if allowed_dir:
        try:
            resolved.relative_to(allowed_dir.resolve())
        except ValueError:
            outside_allowed_dir = True

    # Canonical path gating is always active; Safehouse is defense in depth.
    from tools.sandbox_config import (
        check_repo_write_path,
        is_path_allowed,
    )

    if is_path_allowed(p, for_write=for_write):
        return resolved
    if for_write and (error := check_repo_write_path(resolved)):
        raise PermissionError(error)
    if outside_allowed_dir:
        raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
    raise PermissionError(
        f"Path {path} is outside the sandbox. "
        "File access is restricted to the CORSIA workspace and registered "
        "project repositories."
    )


async def _resolve_path_for_action(
    *,
    tool: Any,
    tool_name: str,
    params: dict[str, Any],
    path: str,
    workspace: Path | None,
    allowed_dir: Path | None,
    thread_id: str | None,
    project_id: str | None,
    for_write: bool = False,
) -> AuthorizedPath:
    """Consume one exact host permit, or apply the ordinary path boundary.

    The gate recomputes the complete canonical action at this final boundary;
    when it consumes a permit its resolved path is the only path the caller may
    use for the effect. Without a permit, the ordinary protected boundary is
    unchanged.
    """
    import tools.confined_reads as confined_reads
    from tools.host_access_gate import consume_current_action_permit
    from tools.project_fence import Fence, fence_for_thread

    fence = await asyncio.to_thread(fence_for_thread, thread_id)
    target = _join_relative_to_workspace(path, workspace)
    local_fence = Fence(tuple(root for root in fence.roots if root.local_only), fence.caller)
    if refusal := local_fence.refusal(target):
        raise PermissionError(refusal)

    # A confined worker's reads never take a host permit: its read paths are all it has.
    consumed = None if confined_reads.confined() else await consume_current_action_permit(
        thread_id=thread_id,
        project_id=project_id,
        tool=tool,
        tool_name=tool_name,
        params=params,
    )
    if consumed is None:
        # Repository policy behind the boundary reads SQLite and runs Git.
        return AuthorizedPath(
            resolved=await asyncio.to_thread(
                resolve_path,
                path,
                workspace,
                allowed_dir,
                for_write=for_write,
                thread_id=thread_id,
            ),
            reviewed=False,
        )

    execution_path = consumed.resolved_path
    if execution_path is None:
        raise PermissionError("Host action path is unavailable.")
    return AuthorizedPath(resolved=execution_path, reviewed=True)
