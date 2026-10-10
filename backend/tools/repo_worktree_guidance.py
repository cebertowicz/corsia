"""Recovery guidance shown when a write targets a protected or pinned base checkout."""

from infra.project_repo_policy import RepoPolicy


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
