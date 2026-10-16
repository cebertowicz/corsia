"""Canonical structured-plan node to artifact relations."""

from __future__ import annotations

from plans import service

WRITABLE_ROLES = frozenset({"input", "result", "attention"})

# An ask link's state belongs to its open user hold. Registry metadata retains
# only non-ask attention and a deferral stamp for one concrete open ask.
_ATTENTION_STATE_SQL = """CASE
    WHEN l.role='attention' AND i.type IN ('document','image') THEN
        CASE
            WHEN EXISTS (
                SELECT 1 FROM plan_node_holds h
                 WHERE h.node_id=l.node_id AND h.kind='blocked'
                   AND h.owner_kind='user' AND h.owner_id=l.artifact_id
                   AND h.cleared_at IS NULL
                   AND json_extract(i.metadata, '$.attention_state')='deferred'
                   AND json_extract(i.metadata, '$.deferred_hold_id')=h.id
            ) THEN 'deferred'
            WHEN EXISTS (
                SELECT 1 FROM plan_node_holds h
                 WHERE h.node_id=l.node_id AND h.kind='blocked'
                   AND h.owner_kind='user' AND h.owner_id=l.artifact_id
                   AND h.cleared_at IS NULL
            ) THEN 'raised'
            ELSE 'cleared'
        END
    ELSE json_extract(i.metadata, '$.attention_state')
END"""
LINK_IDENTITY_COLUMNS = f"""i.integration,i.type,i.external_id,i.title,i.path,i.state,
    {_ATTENTION_STATE_SQL} AS attention_state"""


def raised_attention_nodes(node_ids, *, db) -> set[str]:
    """The same linked-attention scalar the full node presentation reads."""
    from plans import node_waits

    if not node_ids:
        return set()
    return {
        str(row["node_id"])
        for row in db.execute(
            f"""SELECT DISTINCT l.node_id FROM plan_node_artifact_links l
                JOIN artifact_identities i ON i.id=l.artifact_id
                WHERE l.node_id IN (SELECT value FROM json_each(?))
                  AND l.role='attention' AND {_ATTENTION_STATE_SQL}='raised'""",
            (node_waits.encoded_ids(node_ids),),
        )
    }


def _attention_owner(connection, node) -> str:
    """Resolve the owning surface for a genuine plan attention artifact."""

    from registry.listing import project_sentinel

    plan = service._plan_row(connection, node["plan_id"])
    if plan["owner_kind"] == "thread":
        return plan["owner_id"]
    if plan["owner_kind"] == "project_scope":
        return project_sentinel(plan["owner_id"])
    return "ui"


def _projection(row) -> dict:
    return {key: row[key] for key in row.keys()}


def identity_owner(artifact_id: str, *, db) -> tuple[bool, str | None]:
    """Return whether this exact artifact id is known, and the thread that owns it.

    Only the artifact's *own* id resolves. ``identity_row_for`` also answers to
    an external id, and an external id is not an identity: two integrations can
    share one, so authorizing through it could land on another integration's
    record. Unknown and unowned are answered separately because the callers
    refuse them with different messages.
    """
    from registry import artifact_record

    named = artifact_id.strip() if isinstance(artifact_id, str) else ""
    row = artifact_record.identity_row_for(named, db=db) if named else None
    if row is None or str(row["id"]) != named:
        return False, None
    record = artifact_record.register_or_resolve(
        {"integration": row["integration"], "type": row["type"],
         "external_id": row["external_id"]},
        db=db,
    )
    return True, record.owner_thread


def link(
    node_id: str,
    artifact_id: str,
    role: str,
    *,
    actor_id: str | None,
    provenance: str,
    source_event_id: str | None = None,
    db=None,
) -> dict:
    """Idempotently link one trusted artifact identity to one plan node."""

    if role not in WRITABLE_ROLES:
        raise service.PlanError(f"invalid plan artifact role: {role}")
    provenance = service._required_text(provenance, "provenance")
    with service._connection(db) as connection, service._write(connection):
        service._node_row(connection, node_id)
        artifact = connection.execute(
            "SELECT id,state FROM artifact_identities WHERE id=?", (artifact_id,)
        ).fetchone()
        if artifact is None or artifact["state"] != "active":
            raise service.PlanError("plan artifact link requires an active identity")
        existing = connection.execute(
            """SELECT * FROM plan_node_artifact_links
               WHERE node_id=? AND artifact_id=? AND role=?""",
            (node_id, artifact_id, role),
        ).fetchone()
        if existing is not None:
            return _projection(existing)
        connection.execute(
            """INSERT INTO plan_node_artifact_links
               (node_id,artifact_id,role,actor_id,provenance,source_event_id,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                node_id,
                artifact_id,
                role,
                actor_id,
                provenance,
                source_event_id,
                service._now(),
            ),
        )
        return _projection(
            connection.execute(
                """SELECT * FROM plan_node_artifact_links
                   WHERE node_id=? AND artifact_id=? AND role=?""",
                (node_id, artifact_id, role),
            ).fetchone()
        )


def list_for_node(node_id: str, *, db=None) -> list[dict]:
    """Return stable artifact links and their immutable provenance."""

    with service._connection(db) as connection:
        service._node_row(connection, node_id)
        rows = connection.execute(
            f"""SELECT l.*,{LINK_IDENTITY_COLUMNS}
                FROM plan_node_artifact_links l
                JOIN artifact_identities i ON i.id=l.artifact_id
                WHERE l.node_id=? ORDER BY l.role,l.artifact_id""",
            (node_id,),
        ).fetchall()
        return [_projection(row) for row in rows]
