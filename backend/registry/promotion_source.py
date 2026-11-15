"""Which file a promote would move: the checks that need no side effect, so a caller can run them first."""

from __future__ import annotations

from pathlib import Path

from core.workspace_layout import resolve_thread_working_root
from registry.shared_documents import _scope_root
from registry.shared_write_errors import DurableWritePolicyError


def promotion_source(
    source: Path, category: str, *, thread_id: str, project_id: str | None, workspace: Path, db,
) -> tuple[Path, Path]:
    """``(scope_root, source_path)`` for a promote of ``source`` into ``category``, else a refusal.

    One exact regular file under the calling thread's own working root, into one category name. Nothing
    is written or held here: ``promote_file`` calls it first, and so does a caller that must hold the
    file's path before the move, so a refused call never touches anyone's open document.
    """
    if not isinstance(category, str) or not category or Path(category).name != category:
        raise DurableWritePolicyError("Promotion requires one approved shared category name.")
    scope_root, _authoritative_project = _scope_root(
        workspace=workspace, thread_id=thread_id, project_id=project_id, direct_user=False, db=db,
    )
    working_root = resolve_thread_working_root(scope_root, thread_id).resolve(strict=False)
    source_input = source.expanduser()
    if not source_input.is_absolute():
        source_input = workspace / source_input
    source_path = source_input.resolve(strict=False)
    if not source_path.is_file() or source_path.is_symlink():
        raise DurableWritePolicyError(f"Promotion source is not a regular file: {source}")
    if source_path == working_root or not source_path.is_relative_to(working_root):
        raise DurableWritePolicyError("Only a file under the exact owning thread working root can be promoted.")
    return scope_root, source_path
