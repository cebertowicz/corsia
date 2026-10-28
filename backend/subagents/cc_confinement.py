"""Where a confined Claude Code child runs and what it may read.

A ``review`` child reads only its own empty working directory and the
add-dirs its parent could already read, and holds no credential. The
confinement is stored on the thread row. A cold launch reads that row;
a live process retains the confinement it was launched with.
"""

from __future__ import annotations

from pathlib import Path

from subagents.cc_invocation import CONFINEMENT_NONE, UNCONFINED, LaunchConfinement


class ConfinementRefused(ValueError):
    """A confined launch cannot be given the reads or record it needs."""


def unsupported_model_message(model_name: str) -> str:
    """The shared refusal for a model unable to launch a read-only worker."""
    return f"Couldn’t start this read-only agent on {model_name}: it can’t run on a Codex model. Choose another model."


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
    """Remove every credential a review child could use (design §8.1, "No tokens").

    The rule is the one agent shells use, so a name added later is covered too.
    """
    from tools.shell_environment import is_credential_env_name

    for name in [name for name in env if is_credential_env_name(name)]:
        del env[name]


def launch_confinement(thread_id: str, live, db) -> LaunchConfinement:
    """The one confinement read for a launch, made at its start.

    A live process keeps the confinement it was launched with; otherwise the
    thread's row (``infra.thread_confinement``) decides, including for a
    worker made confined on another harness.
    """
    invocation = getattr(getattr(live, "session", None), "invocation", None)
    if callable(invocation):
        launched = invocation()
        if launched.confinement == CONFINEMENT_NONE:
            return UNCONFINED
        return LaunchConfinement(launched.confinement, launched.add_dirs, launched.working_dir)
    from infra.thread_confinement import ConfinementRecordDamaged, read_dirs
    from subagents.cc_invocation import CONFINEMENT_REVIEW

    owned = db is None
    if owned:
        from infra.database import get_db

        db = get_db()
    try:
        try:
            dirs = read_dirs(thread_id, db)
        except ConfinementRecordDamaged as error:
            raise ConfinementRefused(str(error)) from error
        if dirs is None:
            return UNCONFINED
        project_id = db.execute("SELECT project_id FROM threads WHERE id=?", (thread_id,)).fetchone()[0]
        return LaunchConfinement(CONFINEMENT_REVIEW, dirs, review_working_dir(thread_id, project_id, db))
    finally:
        if owned:
            db.close()


def launch_tools(working_dir: str | None, confined: bool) -> tuple[object, tuple]:
    """The CORSIA tool registry and definitions a launch installs and budgets.

    A review child has no tool bridge, so it gets neither, however it launches.
    """
    if confined:
        return None, ()
    from subagents.harness_tool_bridge import build_tool_registry, dynamic_tool_definitions

    registry = build_tool_registry(working_dir)
    return registry, dynamic_tool_definitions(registry)
