"""Assign durable owners to existing files in one scope's shared tree."""

from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.scaffold_layouts import _project_layout, _workspace_layout
from core.workspace_layout import resolve_project_root
from infra.conductor import WORKSPACE_SCOPE
from infra.database import get_db, write_transaction
from infra.settings import set_setting
from plans import public as plans
from registry import node_retirement, service
from registry.doc_freshness import seed_verified_from_headers
from registry.durable_writes import _bounded_shared_files
from registry.identity_metadata import merge_identity_metadata
from registry.ownership import rehome_artifact
from registry.shared_category_index import directory_owner_threads
from registry.document_ownership import ADOPTION_SETTING_PREFIX, conductor_adopts_unowned
from registry.shared_owner_header import header_fields, resolve_header_owner
from registry.shared_principals import acting_owner, artifact_owner, scope_conductor

logger = logging.getLogger(__name__)
_CLAIM_PROVENANCE = "shared.ownership_assignment"
_CLAIM_TITLE = "Claim your shared documents: keep, hand off, or archive"
_CLAIM_ACCEPTANCE = (
    "Every listed document has a completed keep, handoff, or archive disposition under "
    "the applicable shared-document rules. Any unresolved path remains active under a watch "
    "until resolved."
)


@dataclass(frozen=True, slots=True)
class Assignment:
    path: str
    owner: str | None
    rule: str
    conflict: bool
    trigger: str | None
    artifact_id: str | None
    # An Owner: header that named no single principal; the next rule assigned the file.
    unresolved_header: str | None = None


def _scope_root(workspace: Path, project_id: str | None, db) -> Path | None:
    if project_id is None:
        return workspace.resolve(strict=False)
    return resolve_project_root(workspace, project_id, db=db)


def _directory_owners(shared_root: Path, project_id: str | None, db) -> dict[str, str]:
    index = shared_root / "INDEX.md"
    if not index.is_file() or index.is_symlink():
        return {}
    parsed = directory_owner_threads(index.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for category, thread_id in parsed.items():
        scope_sql = "project_id IS NULL" if project_id is None else "project_id=?"
        params: tuple[object, ...] = (thread_id,) if project_id is None else (thread_id, project_id)
        row = db.execute(
            f"SELECT id FROM threads WHERE id=? AND {scope_sql} AND agent_enabled=1",
            params,
        ).fetchone()
        if row is not None:
            result[category] = thread_id
    return result


def _registry_rule(registered: str | None, db) -> tuple[str | None, str]:
    """Keep a persistent registry owner, or lift a temporary one to its acting owner.

    A temporary owner with no persistent ancestor matches neither rule, so the
    caller falls through to the directory and conductor rules.
    """
    if registered is None:
        return None, ""
    thread = db.execute(
        "SELECT agent_enabled FROM threads WHERE id=?", (registered,),
    ).fetchone()
    if registered == "ui" or (thread and thread["agent_enabled"]):
        return registered, "registry"
    return acting_owner(registered, False, db=db), "acting_owner"


def inventory_shared_ownership(workspace: Path, project_id: str | None, *, db) -> tuple[Assignment, ...]:
    """Return the deterministic, side-effect-free assignment table for one scope."""
    root = _scope_root(workspace, project_id, db)
    if root is None:
        return ()
    shared_root = root / "shared"
    conductor = scope_conductor(project_id, db=db)
    directory_owners = _directory_owners(shared_root, project_id, db)
    rows: list[Assignment] = []
    files = sorted(_bounded_shared_files(shared_root), key=lambda item: item.as_posix())
    for path in files:
        artifact = service.find_file_identity(str(path), db=db)
        registered = artifact_owner(str(artifact["id"]), db=db) if artifact else None
        fields = header_fields(path)
        header_value = fields.get("owner")
        header = resolve_header_owner(header_value, project_id, db) if header_value else None
        conflict = bool(header and registered and header != registered)
        trigger = fields.get("review")
        owner, rule = (header, "header") if header else _registry_rule(registered, db)
        if owner is None:
            category = path.relative_to(shared_root).parts[0]
            if category in directory_owners:
                owner, rule = directory_owners[category], "directory"
            else:
                owner, rule = conductor, "fallback"
        rows.append(
            Assignment(
                str(path.relative_to(root)), owner, rule, conflict, trigger,
                str(artifact["id"]) if artifact else None,
                header_value if header_value and header is None else None,
            )
        )
    return tuple(rows)


def _claim_exists(db, plan_id: str, scope_id: str) -> bool:
    return db.execute(
        """SELECT 1 FROM plan_node_artifact_links l JOIN plan_nodes n ON n.id=l.node_id
           WHERE n.plan_id=? AND l.provenance=? AND l.source_event_id=? LIMIT 1""",
        (plan_id, _CLAIM_PROVENANCE, scope_id),
    ).fetchone() is not None


def _claim_guidance(scope_root: Path) -> str:
    standard = scope_root / "shared" / "standards" / "WORKSPACE-STANDARD.md"
    if standard.is_file() and not standard.is_symlink():
        return (
            "Before any effect, read shared/standards/WORKSPACE-STANDARD.md. Keep means an "
            "authoritative approved/current document belongs in your responsibility: make no "
            "content or ownership change and record keep. For a handoff, resolve the linked "
            "input with artifact_resolve, verify the authorized persistent principal and its "
            "thread ID in agent_roster, then use artifact_rehome with new_owner_thread_id. "
            "Archive only when the standard's criteria apply. For either mutation, follow the "
            "standard's file, status, index, archive, and reference rules."
        )
    return (
        "Keep means retain the document in your responsibility: make no content or ownership "
        "change and record keep. For a handoff, resolve the linked input with artifact_resolve, "
        "verify the authorized persistent principal and its thread ID in agent_roster, then use "
        "artifact_rehome with new_owner_thread_id; Do not archive without a scope standard: record "
        "that prerequisite in task state, arm a deadline watch, and ask the conductor to establish "
        "it with destination, status header, live and archive indexes, and living-reference "
        "preservation before archive effects."
    )


def _claim_tasks(
    rows: tuple[Assignment, ...], scope_id: str, scope_root: Path, *, db,
) -> None:
    grouped: dict[str, list[Assignment]] = {}
    for row in rows:
        if row.owner and row.owner != "ui":
            grouped.setdefault(row.owner, []).append(row)
    for owner, owned in grouped.items():
        plan = plans.ensure_plan("thread", owner, db=db)
        if _claim_exists(db, plan.id, scope_id):
            continue
        body = [
            (
                "This one-time claim task applies only to the paths listed below; unrelated work "
                "on those paths neither triggers nor satisfies it, so do not add its dispositions "
                "here. The assignments are already applied: you are the current registry owner. "
                "Review each path and keep, hand off, or archive it."
            ),
            f"Paths are relative to the `{scope_id}` scope root: `{scope_root}`.",
            (
                "The rule label is assignment provenance, not a precedence check to rerun: "
                "header = resolved **Owner:** field; registry = existing persistent owner; "
                "acting owner = persistent principal above a temporary owner; directory = "
                "shared/INDEX.md directory row; fallback = no higher rule resolved, so the "
                "conductor became owner. Registry ownership is authoritative; headers and "
                "indexes are projections."
            ),
            _claim_guidance(scope_root),
            (
                "After every path succeeds, call plan_update accept with verification_result=passed "
                "and one disposition per path in evidence. If a path, standard, target owner, or "
                "authority is unavailable, do not accept or guess: record the exact prerequisite "
                "and its owner in task state, then call plan_update arm_watch with a deadline for "
                "external work (use an event watch only when its exact event is supplied). Use "
                "block_node only when user action is required. Never substitute chat for plan state."
            ),
            (
                "When that watch fires, nothing happens automatically: re-read the current path, "
                "roster and prerequisite. If the prerequisite is still missing, send its named "
                "owner one message with the exact ask and re-arm the deadline watch. The user does "
                "not own the prerequisite; raise a plan decision only when a distinct material "
                "choice needs the user."
            ),
        ]
        body.extend(f"- `{row.path}` — assigned by {_rule_label(row)}" for row in owned)
        node = plans.add_node(
            plan.id, kind="task", title=_CLAIM_TITLE,
            acceptance_criteria=_CLAIM_ACCEPTANCE, description="\n".join(body),
            actor_id=None, provenance=_CLAIM_PROVENANCE, db=db,
        )
        # Input links both expose all assigned documents and carry the scope key,
        # following the same retry-safe route as document feedback nodes.
        for row in owned:
            plans.link_node_artifact(
                node.id, str(row.artifact_id), "input", actor_id=None,
                provenance=_CLAIM_PROVENANCE, source_event_id=scope_id, db=db,
            )
        plans.transition_node(node.id, "active", actor_id=None, provenance=_CLAIM_PROVENANCE, db=db)
        plans.reconcile_plan(plan.id, actor_id=None, provenance=_CLAIM_PROVENANCE, db=db)


def _scaffold_shared_tree(workspace: Path, project_id: str | None, *, db) -> bool:
    """Whether a scope still has exactly the shared files its scaffold created."""
    root = _scope_root(workspace, project_id, db)
    if root is None:
        return False
    layout = _workspace_layout() if project_id is None else _project_layout("", "")
    expected = {
        root / relative: content
        for relative, content in layout.files
        if relative.startswith("shared/")
    }
    actual = tuple(_bounded_shared_files(root / "shared"))
    return set(actual) == set(expected) and all(
        path.read_text(encoding="utf-8") == content
        for path, content in expected.items()
    )


def enforce_scaffold_ownership(
    workspace: Path, project_id: str | None, *, db,
) -> tuple[Assignment, ...]:
    """Apply initial ownership without a legacy-claim task for an untouched scaffold."""
    if not _scaffold_shared_tree(workspace, project_id, db=db):
        return ()
    return apply_shared_ownership(
        workspace, project_id, apply=True, create_claim_tasks=False, db=db,
    )


def apply_shared_ownership(
    workspace: Path, project_id: str | None, *, apply: bool, db,
    create_claim_tasks: bool = True,
) -> tuple[Assignment, ...]:
    """Dry-run or atomically apply one scope's assignment table."""
    rows = inventory_shared_ownership(workspace, project_id, db=db)
    if not apply:
        return rows
    scope_id = project_id or WORKSPACE_SCOPE
    if conductor_adopts_unowned(scope_id, db=db):
        return rows
    unassigned = [row for row in rows if row.owner is None]
    if unassigned:
        details = ", ".join(f"{row.path} ({row.rule})" for row in unassigned)
        raise ValueError(f"shared ownership assignment has unassignable files: {details}")
    root = _scope_root(workspace, project_id, db)
    assert root is not None
    applied: list[Assignment] = []
    with write_transaction(db, scope="assign_shared_ownership") as connection:
        for row in rows:
            path = root / row.path
            artifact = service.find_file_identity(str(path), db=connection)
            if artifact is None:
                initial_owner = scope_conductor(project_id, db=connection) or row.owner
                artifact = service.register_write_strict(
                    str(initial_owner), str(path), db=connection,
                )
                # Assignment records accountability; it puts nothing in a pane.
                node_retirement.retire_ownership_row(
                    connection, artifact_id=str(artifact["id"]),
                    thread_id=str(initial_owner), trigger="ownership_assignment",
                )
            rehome_artifact(
                integration=str(artifact["integration"]), type=str(artifact["type"]),
                external_id=str(artifact["external_id"]),
                new_owner_thread_id=str(row.owner), actor_thread_id=None,
                authority="system", reason="ownership_assignment",
                source_path=str(path), db=connection, show_to_new_owner=False,
            )
            if row.trigger:
                merge_identity_metadata(
                    connection, str(artifact["id"]), {"update_trigger": row.trigger},
                )
            # The header date is imported once, with the assignment it rides on.
            seed_verified_from_headers(str(artifact["id"]), path, db=connection)
            applied.append(
                Assignment(
                    row.path, row.owner, row.rule, row.conflict, row.trigger,
                    str(artifact["id"]), row.unresolved_header,
                )
            )
        if create_claim_tasks:
            _claim_tasks(tuple(applied), scope_id, root, db=connection)
        set_setting(
            f"{ADOPTION_SETTING_PREFIX}{scope_id}",
            {"shared_ownership_enforced_at": datetime.now(timezone.utc).isoformat()},
            db=connection,
        )
    return rows


def run_startup_assignment(workspace: Path) -> None:
    """Apply every unenforced scope using one explicitly closed startup connection."""
    db = get_db()
    try:
        scopes = [None] + [
            str(row["id"]) for row in db.execute("SELECT id FROM projects ORDER BY id")
        ]
        for project_id in scopes:
            scope_id = project_id or WORKSPACE_SCOPE
            # One scope's failure, including its adoption-setting read, must not hold
            # back every other scope.
            try:
                if conductor_adopts_unowned(scope_id, db=db):
                    continue
                apply_shared_ownership(workspace, project_id, apply=True, db=db)
            except Exception:
                logger.exception(
                    "shared ownership assignment failed for scope %s; retrying next start",
                    scope_id,
                )
    finally:
        db.close()


def _rule_label(row: Assignment) -> str:
    if row.unresolved_header is None:
        return row.rule
    return f"{row.rule} (its Owner header named no single agent: {row.unresolved_header})"


def _markdown(rows: tuple[Assignment, ...]) -> str:
    fallback = sum(row.rule == "fallback" for row in rows)
    unresolved = sum(row.unresolved_header is not None for row in rows)
    lines = [
        f"Conductor fallback: {fallback}", f"Unresolved Owner headers: {unresolved}", "",
        "| Path | Owner | Rule | Conflict | Trigger | Unresolved header |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {row.path} | {row.owner or ''} | {row.rule} | {str(row.conflict).lower()} "
        f"| {row.trigger or ''} | {row.unresolved_header or ''} |"
        for row in rows
    )
    return "\n".join(lines) + "\n"


_DRY_RUN_TABLES = {
    "artifact_identities", "artifact_registrations", "projects", "settings", "threads",
}


def _open_readonly_database(path: Path) -> sqlite3.Connection:
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise SystemExit(f"cannot open --db read-only: {exc}") from exc
    db.row_factory = sqlite3.Row
    found = {
        str(row[0])
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = sorted(_DRY_RUN_TABLES - found)
    if missing:
        db.close()
        raise SystemExit(
            "--db is missing the CORSIA registry schema: " + ", ".join(missing)
        )
    return db


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--project-id")
    args = parser.parse_args()
    if args.apply or not args.dry_run:
        parser.error("only --dry-run is supported")
    db = _open_readonly_database(args.db)
    try:
        print(_markdown(inventory_shared_ownership(args.workspace, args.project_id, db=db)), end="")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
