"""Recovery guidance for refused writes in registered repositories.

Covers a protected or pinned base checkout, a protected repository's worktree
folders, and the shell's sandbox-refusal reply that chooses between them.
"""

import os
from pathlib import Path

from infra.project_repo_policy import RepoPolicy
from tools.host_path_resolution import containment_key, refusals_in


def worktree_root_denial_guidance(output: str, cwd: Path, policies: list[RepoPolicy]) -> str | None:
    """Recognize output whose only refusals are time-sets on a protected repository's worktree folders.

    Only ``touch`` and ``utimensat`` refusals (``cp -p``) count: the same folder
    rules also refuse creating, removing and linking these folders, which this
    reply does not describe. Any other refusal in the same output keeps the
    general reply, so its guidance is never dropped.
    """
    from tools.sandbox_config import policy_enforceable

    # The folder rules exist only for an enforceable, protected repository.
    containers = [containment_key(Path(os.path.abspath(policy.path / ".worktrees")))
                  for policy in policies if policy_enforceable(policy) and policy.protect_base_checkout]

    def root_time_refusal(program: str, written: str) -> bool:
        if not written or not (program == "touch" or program.endswith(": utimensat")):
            return False
        path = Path(os.path.abspath(cwd / written))
        return containment_key(path) in containers or containment_key(path.parent) in containers

    refusals = refusals_in(output)
    if not (containers and refusals and all(root_time_refusal(*refusal) for refusal in refusals)):
        return None
    return (
        "A worktree's own folder and the .worktrees folder cannot have their times "
        "set from an agent shell; files inside remain writable. "
        "Copy without preserving the folder's times (for example, `cp -R` without "
        "`-p`), or touch the files rather than the folder. Host access is not needed."
    )


async def shell_denial_guidance(output: str, cwd: Path, policies: list[RepoPolicy],
                                thread_id: str, leased: bool, linux: bool) -> str:
    """Compose fallback guidance without changing other denial paths."""
    from host_access.refusal_messages import protected_scope_denial
    from infra.db_async import run_in_db
    from tools.host_path_policy import denial_guidance_prefix
    from tools.shell_cwd import _thread_has_live_lease

    root_guidance = None if linux else worktree_root_denial_guidance(output, cwd, policies)
    if root_guidance:
        return root_guidance
    guidance = await run_in_db(lambda: denial_guidance_prefix(output, thread_id, cwd))
    if leased or "No host-access lease can open it." in guidance:
        return guidance
    return guidance + protected_scope_denial(
        "the sandbox may restrict access to the path" if linux else
        "this command was blocked by the protected-mode sandbox",
        lease_active=await _thread_has_live_lease(thread_id), conditional=linux,
    )


def branch_pinned_worktree_guidance(policy: RepoPolicy) -> str:
    """Return task-neutral recovery steps for a pinned base checkout."""
    if policy.protect_base_checkout:
        return protected_checkout_worktree_guidance(policy)
    return (
        f"Repository '{policy.repo_name}' is branch-pinned. Direct changes to its "
        f"base checkout are blocked: {policy.path}. Run these as separate direct "
        "Git commands with the tool's working directory set to the base checkout. "
        "Use `origin` below by default. If the configured remote name differs, "
        "substitute the configured remote name for `origin` in both commands:\n"
        "`git fetch origin`\n"
        "`git worktree add --no-track -b <new-branch> .worktrees/<slug> "
        "origin/<pinned-branch>`\n"
        "Then continue in `.worktrees/<slug>`. If direct single-checkout work is "
        "intentional, unpin the repository in Project settings."
    )


def protected_checkout_worktree_guidance(policy: RepoPolicy) -> str:
    """Return recovery steps for a protected base checkout."""
    state = (
        "is branch-pinned and its base checkout is protected"
        if policy.branch_pinned
        else "has a protected base checkout"
    )
    base = policy.pinned_branch or "<base-branch>"
    return (
        f"Repository '{policy.repo_name}' {state}. Direct changes to its base "
        f"checkout are blocked: {policy.path}. Use the `worktree_create` tool "
        f"with repo_name={policy.repo_name!r}, branch='<new-branch>', "
        f"descriptor='<slug>', and base_branch={base!r}. The tool fetches the "
        "configured remote before creating the branch. Then continue in the "
        "returned `.worktrees/<thread-id>-<slug>` path. If direct single-checkout "
        "work is intentional, "
        "disable base-checkout protection in Project settings."
    )
