"""Where a confined Claude Code child runs and what it may read.

A ``review`` child reads only its own empty working directory and the
add-dirs its parent could already read. The confinement is stored with the
child's launch description, so every later launch of it, cold or after a
restart, reads it back here and fails closed when that record is damaged.
"""

from __future__ import annotations

import json
from pathlib import Path

from subagents.cc_invocation import CONFINEMENT_NONE, stored_confinement

#: Credentials a review child must never hold (design §8.1, "No tokens").
_REVIEW_ENV_DROP = frozenset({
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_CONFIG_DIR",
    "CORSIA_AGENT_TOKEN", "CORSIA_DELEGATE_TOKEN",
})


class ConfinementRefused(ValueError):
    """A confined launch cannot be given the reads or record it needs."""


def review_working_dir(thread_id: str, project_id: str | None, db) -> str:
    """Create and return the child's own empty ``confined`` directory.

    It sits in the child's thread root, which is inside the workspace, so the
    cold-start cwd rule (``registered_cwd_or_default``) keeps it rather than
    swapping in the project root.
    """
    from core.workspace_layout import resolve_thread_root
    from subagents.cc_runtime_helpers import _main_working_dir

    path = resolve_thread_root(_main_working_dir(project_id, db), thread_id) / "confined"
    if path.is_symlink():
        raise ConfinementRefused(f"confined working directory is a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def require_readable_dirs(paths: list[str], workspace: str | None) -> tuple[str, ...]:
    """Resolve each path under the parent's own read checks, or refuse one by name.

    These are the checks every filesystem tool applies to the parent: the
    protected host-path denial, then the workspace and repository scope.
    """
    from tools.filesystem_gate import _resolve_path

    base = Path(workspace) if workspace else None
    resolved: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise ConfinementRefused("read_paths entries must be non-empty paths")
        try:
            path = _resolve_path(raw, base, for_write=False)
        except PermissionError as error:
            raise ConfinementRefused(f"read path {raw} refused: {error}") from error
        if not path.is_dir():
            raise ConfinementRefused(f"read path {raw} refused: not a directory")
        resolved.append(str(path))
    return tuple(dict.fromkeys(resolved))


def drop_review_credentials(env: dict[str, str]) -> None:
    """Remove every credential a review child could use from its environment."""
    for name in _REVIEW_ENV_DROP:
        env.pop(name, None)


def _latest_launch_metadata(thread_id: str, db) -> object:
    """Read the newest owned CC session record, active ahead of retired."""
    from infra.database import get_db
    from registry.identity_bridge import OWNER_REGISTRATION_STATES

    conn = db or get_db()
    try:
        owner_states = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
        row = conn.execute(
            f"""SELECT i.metadata FROM artifact_identities i
                JOIN artifact_registrations r ON r.artifact_id=i.id
                WHERE r.parent_thread_id=? AND i.integration='claude-code'
                  AND i.type='session' AND r.relationship_state IN ({owner_states})
                ORDER BY i.state='active' DESC, i.updated_at DESC, i.id ASC
                LIMIT 1""",
            (thread_id, *OWNER_REGISTRATION_STATES),
        ).fetchone()
        return row["metadata"] if row else None
    finally:
        if not db:
            conn.close()


def registered_confinement(
    thread_id: str, db,
) -> tuple[str, tuple[str, ...], str | None]:
    """Return the registered launch's confinement, add-dirs and working dir.

    A thread with no stored launch, or one stored before confinement existed,
    is unconfined. A stored launch whose confinement cannot be read raises
    rather than launching with every tool. Confinement belongs to the thread,
    not one provider session, so a retired session's record still counts:
    retiring the transcript must not reopen every tool on the next launch.
    """
    raw = _latest_launch_metadata(thread_id, db)
    if not isinstance(raw, str) or not raw:
        return CONFINEMENT_NONE, (), None
    try:
        metadata = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ConfinementRefused("Claude Code launch record is unreadable") from error
    invocation = metadata.get("claude_invocation") if isinstance(metadata, dict) else None
    if not isinstance(invocation, dict):
        return CONFINEMENT_NONE, (), None
    try:
        confinement, add_dirs = stored_confinement(invocation)
    except ValueError as error:
        raise ConfinementRefused(f"Claude Code launch confinement is damaged: {error}") from error
    working_dir = invocation.get("working_dir")
    return confinement, add_dirs, working_dir if isinstance(working_dir, str) else None
