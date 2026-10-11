"""Register a newly published shared file in its category index."""

from __future__ import annotations

from dataclasses import replace
import fcntl
import re

from infra.database import write_transaction
from registry import service
from registry.file_resolution import find_file_identity
from registry.identity_metadata import merge_identity_metadata, sha256_file
from registry.shared_document_events import record_post_write_events
from registry.shared_documents import resolve_shared_target
from registry.shared_target import SharedTarget
from registry.shared_principals import artifact_owner, may_edit_document, scope_conductor


_INDEX_ENTRY_NAME = re.compile(r"^\s*-\s*`([^`]+)`")
_INDEX_DIRECTORY_OWNER = re.compile(
    r"^\s*-\s*`([^`]+?)/?`\s+.*\(([^()\s]+)\)\s*$"
)


def directory_owner_threads(index_text: str) -> dict[str, str]:
    """Parse ``shared/INDEX.md`` directory rows into category owner thread IDs."""
    owners: dict[str, str] = {}
    for line in index_text.splitlines():
        match = _INDEX_DIRECTORY_OWNER.match(line)
        if match is not None:
            owners[match.group(1)] = match.group(2)
    return owners


def _index_lists_file(index_text: str, name: str) -> bool:
    """Return whether a category index has a bullet for the exact filename."""
    for line in index_text.splitlines():
        match = _INDEX_ENTRY_NAME.match(line)
        if match is not None and match.group(1) == name:
            return True
    return False


def append_category_index_entry(
    shared: SharedTarget,
    description: str | None,
    *,
    thread_id: str | None = None,
    direct_user: bool = False,
    db=None,
    index_lookup=_index_lists_file,
) -> str | None:
    """Append an index row, ensuring the INDEX identity and owner event exist."""
    index_path = shared.index_path
    if shared.path == index_path:
        return None
    summary = " ".join((description or "").split())
    entry = f"- `{shared.path.name}`" + (f" — {summary}" if summary else "")
    try:
        # The duplicate check and the append must be one critical section:
        # two concurrent first-time publications would otherwise both observe
        # a missing bullet and both append one.
        with index_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            existing = handle.read()
            if index_lookup(existing, shared.path.name):
                return None
            prefix = "" if existing == "" or existing.endswith("\n") else "\n"
            updated = f"{existing}{prefix}{entry}\n"
            index_shared = None
            if thread_id and db is not None:
                from infra.config import WORKSPACE

                project_id = None if shared.scope_id == "workspace" else shared.scope_id
                index_shared = resolve_shared_target(
                    index_path,
                    workspace=WORKSPACE,
                    thread_id=thread_id,
                    project_id=project_id,
                    description=None,
                    direct_user=direct_user,
                    db=db,
                    proposed_content=updated.encode("utf-8"),
                )
            handle.write(f"{prefix}{entry}\n")
        if index_shared is not None and db is not None and thread_id is not None:
            project_id = None if shared.scope_id == "workspace" else shared.scope_id
            conductor = scope_conductor(project_id, db=db)
            with write_transaction(db, scope="register_category_index"):
                artifact = find_file_identity(str(index_path), db=db)
                if artifact is None:
                    artifact = service.register_write_strict(
                        conductor or index_shared.actor_principal or thread_id,
                        str(index_path),
                        f"Index for shared/{shared.category_root.name}",
                        db=db,
                    )
                index_owner = artifact_owner(str(artifact["id"]), db=db)
                merge_identity_metadata(
                    db,
                    artifact["id"],
                    {"last_written_sha256": sha256_file(index_path)},
                )
                authority = index_shared.authority
                if authority is None or authority.owner != index_owner:
                    # A new index has its owner only once registered above: decide the append against that owner.
                    authority = may_edit_document(
                        thread_id, direct_user, owner=index_owner, conductor=conductor,
                        path_rel=authority.path_rel if authority else index_path.name,
                        artifact_id=str(artifact["id"]), db=db, mechanical_index=True,
                    )
                record_post_write_events(
                    replace(index_shared, owner_thread_id=index_owner, authority=authority),
                    artifact,
                    before=existing.encode("utf-8"),
                    after=updated.encode("utf-8"),
                    actor_thread_id=thread_id,
                    db=db,
                )
    except Exception as exc:
        return f"{index_path}: {exc}"
    return None
