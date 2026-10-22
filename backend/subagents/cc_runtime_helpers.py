from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from core.project_context import resolve_project_paths

log = logging.getLogger(__name__)


class CCWorkingDirMissing(RuntimeError):
    """Raised when a configured project cwd is unavailable on this host."""

    def __init__(self, working_dir: str, project_id: str | None):
        self.working_dir = working_dir
        self.project_id = project_id
        if not working_dir and project_id:
            super().__init__(
                f"Project working directory unresolved for project_id={project_id}"
            )
            return
        where = f" (project {project_id})" if project_id else ""
        super().__init__(
            f"project working directory does not exist: {working_dir}{where}. "
            f"The project's path is stale or points at a directory not present "
            f"on this host. Fix the project's path before running a turn here."
        )


def _validate_working_dir(working_dir: str | None, project_id: str | None) -> None:
    if working_dir is None:
        return
    if not os.path.isdir(working_dir):
        raise CCWorkingDirMissing(working_dir, project_id)


def working_dir_in_current_scope(working_dir: str | None) -> bool:
    """True if a cwd exists AND resolves inside a currently-authorized scope.

    A working directory cached at spawn or registered before a workspace/project
    cutover can still exist on disk yet sit outside every scope the sandbox now
    authorizes (issue #841). Reuse the exact allow-list the shell guard enforces
    so "in scope" means one thing across the tool bridge, the working-directory
    binding, and host access.
    """
    if not working_dir or not os.path.isdir(working_dir):
        return False
    from tools.sandbox_config import is_path_allowed

    return is_path_allowed(Path(working_dir), for_write=False)


def native_stale_working_dir(
    working_dir: str | None,
    in_scope_default: str | None,
) -> str | None:
    """Return ``in_scope_default`` when it would fix a stale native cwd, else None.

    A CORSIA-native session must run at the current project/workspace root. A
    pre-cutover snapshot can still exist on disk yet fall outside the current
    scope, so it keeps being served in the per-turn context and derived as the
    default tool cwd — which host access then refuses (issue #841). This returns
    the corrected default ONLY when the cwd is genuinely stale (missing or out of
    scope) AND the caller-resolved default is itself in scope, so the rebind is
    always a real fix and a legitimately customized or out-of-band cwd is left
    untouched. The caller resolves the default (via its own project/workspace
    resolution) and applies this only to native (non-foreign) sessions.
    """
    if not working_dir or working_dir_in_current_scope(working_dir):
        return None
    if (
        in_scope_default
        and in_scope_default != working_dir
        and working_dir_in_current_scope(in_scope_default)
    ):
        return in_scope_default
    return None


def _check_live_cap() -> None:
    """Refuse a new spawn while cap-many Claude Code processes are working.

    Idle handles never count; the idle-retirement pass owns closing them.
    """
    from infra.subagent_caps import LiveSpawnCapExceeded, get_max_concurrent_live
    from subagents.cc_idle_retirement import thread_titles, working_thread_ids

    cap = get_max_concurrent_live()
    working = working_thread_ids()
    if len(working) >= cap:
        raise LiveSpawnCapExceeded(
            cap=cap, active=len(working), working=thread_titles(working),
        )


def _default_broadcast(session_id: str, message: dict) -> None:
    from infra import state
    state.broadcast_to_thread(session_id, message)


def _parent_user_id(parent_thread_id: str, db) -> str:
    row = db.execute(
        "SELECT user_id FROM threads WHERE id = ?", (parent_thread_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"parent session {parent_thread_id!r} not found")
    return row["user_id"]


def _proc_is_alive(session) -> bool:
    return session is not None and session.returncode is None


def _artifact_cc_session_id(thread_session_id: str, db) -> str | None:
    from registry.models import find_for_thread
    row = find_for_thread(thread_session_id, "claude-code", "session", db=db)
    return row["external_id"] if row else None


def _artifact_cc_session_cwd(thread_session_id: str, db) -> str | None:
    """Return the registered provider cwd used when its transcript is missing."""
    from registry.models import find_for_thread

    row = find_for_thread(thread_session_id, "claude-code", "session", db=db)
    if not row:
        return None
    raw_metadata = row.get("metadata")
    if not isinstance(raw_metadata, str) or not raw_metadata:
        return None
    try:
        metadata = json.loads(raw_metadata)
    except (TypeError, json.JSONDecodeError):
        return None
    cwd = metadata.get("cwd") if isinstance(metadata, dict) else None
    return cwd if isinstance(cwd, str) and cwd else None


def registered_cwd_or_default(session_id: str, db, default: str) -> str:
    """Use a current-scope registered cwd, otherwise the current default.

    A confined child always runs in its own directory, even after its
    provider session was retired.
    """
    from subagents.cc_confinement import registered_confinement
    from subagents.cc_invocation import CONFINEMENT_NONE

    confinement, _add_dirs, confined_dir = registered_confinement(session_id, db)
    if confinement != CONFINEMENT_NONE and confined_dir:
        return confined_dir
    registered = _artifact_cc_session_cwd(session_id, db)
    if not registered:
        return default
    corrected = native_stale_working_dir(registered, default)
    if corrected is not None:
        log.warning(
            "CC session %s: registered cwd %s is stale; using project default",
            session_id,
            registered,
        )
        return corrected
    if os.path.isdir(registered):
        return registered
    log.warning(
        "CC session %s: registered cwd %s is gone; using project default",
        session_id,
        registered,
    )
    return default


def _artifact_cc_session_invocation(
    thread_session_id: str,
    db,
) -> object | None:
    """Restore only the exact invocation persisted by the real CC owner."""
    from registry.models import find_for_thread

    row = find_for_thread(thread_session_id, "claude-code", "session", db=db)
    if not row:
        return None
    raw_metadata = row.get("metadata")
    if not isinstance(raw_metadata, str) or not raw_metadata:
        return None
    try:
        metadata = json.loads(raw_metadata)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict) or "claude_invocation" not in metadata:
        return None
    from subagents.cc_invocation import claude_invocation_from_metadata

    return claude_invocation_from_metadata(metadata["claude_invocation"])


def _build_seed(working_dir: str | None) -> str:
    return (
        "You are a CORSIA delegated subagent (Claude Code), spawned by an "
        "owning thread to carry out a task and then await any follow-up.\n\n"
        f"Working directory: {working_dir or os.getcwd()}\n\n"
        "Work directly and concisely. When the task is complete, summarize what "
        "you did in one short message, then wait for further instructions."
    )


def _write_seed_file(child_id: str, seed: str) -> str:
    fd, path = tempfile.mkstemp(prefix=f"cc-seed-{child_id[:8]}-", suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write(seed)
    return path


def _build_main_seed(working_dir: str) -> str:
    return (
        "You are Claude Code running as the agent for a CORSIA chat thread. "
        "Respond to the user directly and conversationally; use your tools "
        "when the request calls for them.\n\n"
        f"Working directory: {working_dir}"
    )


def _main_working_dir(project_id: str | None, db) -> str:
    if project_id:
        from infra.config import WORKSPACE

        _name, planning_root = resolve_project_paths(
            WORKSPACE,
            project_id,
            db=db,
        )
        if planning_root is not None:
            return str(planning_root)
        raise CCWorkingDirMissing("", project_id)
    from infra.config import WORKSPACE
    return str(WORKSPACE)
