"""Resolve the principals allowed to own and govern shared documents."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Collection

from core.workspace_layout import resolve_project_root
from infra.conductor import WORKSPACE_SCOPE, get_conductor
from infra.workspace_paths import WorkspaceReference, workspace_selection_for_root
from registry.identity_bridge import OWNER_REGISTRATION_STATES

UI_PRINCIPAL = "ui"


def acting_owner(
    thread_id: str,
    direct_user: bool,
    *,
    db,
    excluded_thread_ids: Collection[str] = (),
) -> str | None:
    """Return the persistent principal a write acts for, walking parent links."""
    if direct_user:
        return UI_PRINCIPAL
    excluded = set(excluded_thread_ids)
    current: str | None = thread_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        row = db.execute(
            "SELECT spawned_by,agent_enabled FROM threads WHERE id=?",
            (current,),
        ).fetchone()
        if row is None:
            return None
        if current not in excluded and bool(row["agent_enabled"]):
            return current
        current = row["spawned_by"]
    return None


def scope_conductor(project_id: str | None, *, db) -> str | None:
    """Return the designated conductor for one project or workspace scope."""
    return get_conductor(project_id or WORKSPACE_SCOPE, db=db)


_REPORTS_NOTHING = frozenset({"owner", "owner_subagent", "unowned"})


@dataclass(frozen=True, slots=True)
class WriteAuthority:
    """The one ownership decision for one write to one document (DCOL-FR5.9).

    ``basis`` names why the write is allowed (owner, owner_subagent, conductor,
    conductor_subagent, user, index_entry, unowned) or that it is ``refused``.
    ``actor`` is the principal the write is attributed to and ``path_rel`` the
    document's path relative to its scope root. Every reporter reads this value;
    none decides again.
    """

    basis: str
    owner: str | None
    path_rel: str
    actor: str
    artifact_id: str | None = None

    @property
    def allowed(self) -> bool:
        """Whether the write may land."""
        return self.basis != "refused"

    @property
    def reaches_owner(self) -> bool:
        """Whether a write that lands under this decision is reported to the owner."""
        return self.owner is not None and self.basis not in _REPORTS_NOTHING

    def to_record(self) -> dict:
        """Return the JSON-safe record a durable journal stores."""
        return asdict(self)

    @classmethod
    def from_record(cls, record: dict | None) -> WriteAuthority | None:
        """Restore ``to_record`` output; a record journaled before this field has none."""
        return cls(**record) if record else None


def may_edit_document(
    thread_id: str,
    direct_user: bool,
    *,
    owner: str | None,
    conductor: str | None,
    path_rel: str,
    db,
    artifact_id: str | None = None,
    mechanical_index: bool = False,
) -> WriteAuthority:
    """Decide whether this actor may change a document ``owner`` owns.

    The one decision for every write path. The owner, the owner's temporary
    subagents, the conductor of the scope that holds the document and its
    temporary subagents, and the user may edit; every other agent suggests.
    One ``acting_owner`` walk serves both subagent exceptions. ``conductor`` is
    the document's scope conductor, never the actor's. A mechanical
    ``INDEX.md`` entry is allowed to anyone, and an unowned document is
    nobody's to refuse.
    """
    def decide(basis: str, actor: str) -> WriteAuthority:
        return WriteAuthority(basis, owner, path_rel, actor, artifact_id)

    if direct_user:
        basis = "unowned" if owner is None else "owner" if owner == UI_PRINCIPAL else "user"
        return decide(basis, UI_PRINCIPAL)
    if owner is None:
        return decide("unowned", thread_id)
    if thread_id == owner:
        return decide("owner", thread_id)
    if thread_id == conductor:
        return decide("conductor", thread_id)
    principal = acting_owner(thread_id, False, db=db)
    if principal is not None and principal == owner:
        return decide("owner_subagent", principal)
    if principal is not None and principal == conductor:
        return decide("conductor_subagent", principal)
    if mechanical_index:
        return decide("index_entry", principal or thread_id)
    if principal is not None and principal == acting_owner(owner, False, db=db):
        # An owner thread that is no longer a persistent agent acts through the agent above it.
        return decide("owner_subagent", principal)
    return decide("refused", principal or thread_id)


def document_scope(path: Path | str, *, workspace: Path, db) -> tuple[Path, str | None]:
    """Return the root and conductor of the scope that holds ``path``, in one lookup.

    The project that holds ``path`` is the one whose root, resolved the way
    every other caller resolves it (``core.project_context``, so a legacy row
    the startup converter keeps resolves too), contains it. Any other
    workspace path, and a project folder no project row resolves to, belongs
    to the workspace scope.
    """
    from core.project_context import resolve_project_row_paths

    root = workspace.expanduser().resolve(strict=False)
    resolved = Path(path).expanduser().resolve(strict=False)
    holder: tuple[Path, str] | None = None
    for row in db.execute("SELECT id, name, path FROM projects").fetchall():
        with suppress(ValueError):
            _name, project_root = resolve_project_row_paths(root, project_name=row["name"], raw_path=row["path"])
            if project_root is not None and resolved.is_relative_to(project_root):
                if holder is None or len(project_root.parts) > len(holder[0].parts):
                    holder = (project_root, str(row["id"]))
    if holder is not None:
        return holder[0], scope_conductor(holder[1], db=db)
    return root, scope_conductor(None, db=db)


def scope_relative(path: Path, scope_root: Path) -> str:
    """Return ``path`` relative to its scope root, as events and refusals show it."""
    return path.relative_to(scope_root).as_posix() if path.is_relative_to(scope_root) else str(path)


def resolve_explicit_owner(
    value: str,
    *,
    project_id: str | None,
    user_id: str,
    db,
) -> str:
    """Resolve an explicit owner name, id, or reserved principal in one scope."""
    requested = value.strip()
    lowered = requested.casefold()
    if lowered == "user":
        return UI_PRINCIPAL
    if lowered == "conductor":
        conductor = scope_conductor(project_id, db=db)
        if conductor:
            return conductor
        raise ValueError("This scope has no designated conductor.")

    scope_sql = "project_id IS NULL" if project_id is None else "project_id=?"
    params: list[object] = [user_id]
    if project_id is not None:
        params.append(project_id)
    params.extend((requested, requested))
    rows = db.execute(
        f"""SELECT id FROM threads
            WHERE user_id=? AND {scope_sql}
              AND COALESCE(agent_enabled,0)=1
              AND (id=? OR title=?)
            ORDER BY id""",
        params,
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(
            "owner must resolve to one persistent agent name or ID, "
            "`conductor`, or `user`."
        )
    return str(rows[0]["id"])


def artifact_owner(artifact_id: str, *, db) -> str | None:
    """Return the active owner principal for one artifact identity."""
    marks = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    row = db.execute(
        f"""SELECT parent_thread_id FROM artifact_registrations
            WHERE artifact_id=? AND relationship_state IN ({marks})
            ORDER BY CASE relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                     updated_at DESC LIMIT 1""",
        (artifact_id, *OWNER_REGISTRATION_STATES),
    ).fetchone()
    return str(row["parent_thread_id"]) if row else None


def owner_label(owner_thread_id: str, *, db) -> str:
    """Return a product-facing label for an owner principal."""
    if owner_thread_id == UI_PRINCIPAL:
        return "the user"
    row = db.execute(
        "SELECT title FROM threads WHERE id=?",
        (owner_thread_id,),
    ).fetchone()
    title = str(row["title"] or "").strip() if row else ""
    return title or owner_thread_id


def shared_roots(*, workspace: Path, db) -> list[Path]:
    """Return every workspace and project ``shared/`` root, resolved once."""
    roots = [workspace.resolve(strict=False)]
    rows = db.execute("SELECT id FROM projects").fetchall()
    for row in rows:
        root = resolve_project_root(workspace, str(row["id"]), db=db)
        if root is not None:
            roots.append(root.resolve(strict=False))
    return [root / "shared" for root in roots]


def is_under_shared_roots(path: str | Path, roots: list[Path]) -> bool:
    """Return whether ``path`` is below one of the precomputed ``roots``."""
    candidate = Path(path).expanduser().resolve(strict=False)
    return any(
        candidate != root and candidate.is_relative_to(root) for root in roots
    )


def is_shared_document_path(path: str | Path, *, workspace: Path, db) -> bool:
    """Return whether ``path`` is below any workspace or project shared root."""
    return is_under_shared_roots(path, shared_roots(workspace=workspace, db=db))


def is_scope_conductor_for_shared_path(
    actor_thread_id: str,
    path: str | Path,
    *,
    workspace: Path,
    db,
) -> bool:
    """Return whether actor conducts the scope and ``path`` is a shared document."""
    actor = db.execute(
        "SELECT project_id FROM threads WHERE id=?",
        (actor_thread_id,),
    ).fetchone()
    if actor is None:
        return False
    project_id = actor["project_id"]
    if scope_conductor(project_id, db=db) != actor_thread_id:
        return False
    if project_id is None:
        root = workspace.resolve(strict=False)
    else:
        root = resolve_project_root(workspace, project_id, db=db)
        if root is None:
            return False
        root = root.resolve(strict=False)
    candidate = Path(path).expanduser().resolve(strict=False)
    shared_root = root / "shared"
    return candidate != shared_root and candidate.is_relative_to(shared_root)
