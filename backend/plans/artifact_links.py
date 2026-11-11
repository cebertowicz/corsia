"""Canonical structured-plan node to artifact relations."""

from __future__ import annotations

from plans import hierarchy, node_states, operator_display_state, service
from plans.user_ask import ask_attention_state_sql, askable_sql

WRITABLE_ROLES = frozenset({"input", "result", "attention"})

# An ask link's state belongs to its open review request. The one state SQL
# definition is node-scoped here so a replaced ask on another node cannot leak
# into this link's presentation.
_ATTENTION_STATE_SQL = f"""CASE
    WHEN l.role='attention' AND {askable_sql('i')}
        THEN {ask_attention_state_sql('l.artifact_id', node_id_sql='l.node_id')}
    ELSE json_extract(i.metadata, '$.attention_state')
END"""
LINK_IDENTITY_COLUMNS = f"""i.integration,i.type,i.external_id,i.title,i.path,i.state,
    {_ATTENTION_STATE_SQL} AS attention_state"""


def deferred_attention_artifact_ids(artifact_ids, *, db) -> set[str]:
    """Raised non-ask attention artifacts whose every open-node link is paused.

    A document or image ask is never named here: ``ask_attention_state_sql``
    owns its deferral, and ``registry.attention.list_raised`` already drops one
    that is not raised. For anything else, one link on an open node that is not
    paused keeps the artifact raised; a link on a settled node never counts.
    """
    identities = list(dict.fromkeys(str(artifact_id) for artifact_id in artifact_ids))
    deferred: set[str] = set()
    for start in range(0, len(identities), hierarchy._ID_CHUNK):
        chunk = identities[start:start + hierarchy._ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = db.execute(
            f"""SELECT link.artifact_id FROM plan_node_artifact_links link
                  CROSS JOIN artifact_identities ident ON ident.id=link.artifact_id
                  CROSS JOIN plan_nodes node ON node.id=link.node_id
                  CROSS JOIN plans plan ON plan.id=node.plan_id
                 WHERE link.role='attention' AND link.artifact_id IN ({marks})
                   AND NOT {askable_sql('ident')} AND {node_states.open_sql('node')}
                 GROUP BY link.artifact_id
                HAVING MIN({operator_display_state.ask_deferred_sql('node.id', plan_state_sql='plan.state')})=1""",
            chunk,
        ).fetchall()
        deferred.update(str(row["artifact_id"]) for row in rows)
    return deferred


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


def open_ask_link_sql(link: str, identity: str, node: str, plan: str) -> str:
    """One actionable document ask predicate for link reads and task coverage."""
    raised = ask_attention_state_sql(f'{link}.artifact_id', node_id_sql=f'{link}.node_id')
    return f"""({node_states.open_sql(node)}
        AND NOT {operator_display_state.pause_deferred_sql(f'{node}.id', plan_state_sql=f'{plan}.state')}
        AND (({link}.role='input' AND {node}.kind='task' AND {node}.assignee='user')
             OR ({link}.role='attention' AND {askable_sql(identity)} AND {raised}='raised')))"""


def open_asks_for_artifacts(artifact_ids, *, db) -> list[dict]:
    """Open asks directly linked to any of the supplied artifacts, in one read."""
    import json

    if not artifact_ids:
        return []
    rows = db.execute(
        f"""SELECT DISTINCT l.artifact_id, n.id AS node_id,
                   CASE WHEN l.role='input' THEN 'person_step'
                        ELSE 'review_hold' END AS kind,
                   p.owner_id AS owner_thread_id, t.user_id AS owner_user_id,
                   l.provenance
              FROM plan_node_artifact_links l
              JOIN artifact_identities i ON i.id=l.artifact_id
              JOIN plan_nodes n ON n.id=l.node_id
              JOIN plans p ON p.id=n.plan_id
              LEFT JOIN threads t ON t.id=p.owner_id AND p.owner_kind='thread'
             WHERE l.artifact_id IN (SELECT value FROM json_each(?))
               AND {open_ask_link_sql('l', 'i', 'n', 'p')}
             ORDER BY n.id""",
        (json.dumps(list(artifact_ids)),),
    ).fetchall()
    return [dict(row) for row in rows]


def open_asks_for_artifact(artifact_id: str, *, db) -> list[dict]:
    """Open person steps and raised attention linked directly to this artifact."""
    return open_asks_for_artifacts([artifact_id], db=db)


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


def linked_node_for_artifact(integration: str, type: str, external_id: str, *, db) -> dict:
    """The one open plan node a registered artifact (a PR) is attached to.

    The candidates are the owner registration's node (registered with a node)
    and every node that took the artifact as a result (``review_evidence``).
    Only open work counts. ``lookup_failed`` covers an identity that is unknown
    or not active; a database error raises.
    """
    from registry.registration_events import routing_registrations

    with service._connection(db) as connection:
        routing = routing_registrations(integration, type, external_id, db=connection)
        if routing["identity"] is None:
            return {"status": "lookup_failed", "node_id": None}
        owner = routing["owner"]
        rows = connection.execute(
            f"SELECT n.id FROM plan_nodes n WHERE {node_states.open_sql('n')} AND (n.id=? OR n.id IN ("
            "SELECT node_id FROM plan_node_artifact_links WHERE artifact_id=? AND role='result'))",
            (owner["origin_plan_node_id"] if owner else None, routing["identity"]["id"]),
        ).fetchall()
    if not rows:
        return {"status": "no_linked_task", "node_id": None}
    if len(rows) > 1:
        return {"status": "several_linked_tasks", "node_id": None}
    return {"status": "ok", "node_id": str(rows[0]["id"])}
