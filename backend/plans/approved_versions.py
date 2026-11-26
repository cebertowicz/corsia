"""Read approved versions tied to work from plan lineage."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3

from infra.database import get_db

from doceditor.document_scope import scope_roots
from plans import artifact_links, commitments, service


def _ancestors(node_id: str, db: sqlite3.Connection) -> list[str]:
    """Walk parents and the live task attaching each child plan."""
    pending = node_id
    seen: set[str] = set()
    while pending:
        rows = db.execute(
            """WITH RECURSIVE up(id,parent_id,plan_id) AS (
                   SELECT id,parent_id,plan_id FROM plan_nodes WHERE id=?
                   UNION SELECT n.id,n.parent_id,n.plan_id FROM plan_nodes n JOIN up ON n.id=up.parent_id
               ) SELECT id,parent_id,plan_id FROM up""", (pending,),
        ).fetchall()
        if not rows:
            raise service.PlanError("approval lookup node not found")
        for row in rows:
            if row["id"] in seen:
                raise service.PlanError("approval lookup has cyclic plan attachment")
            seen.add(str(row["id"]))
        top = next(row for row in rows if row["parent_id"] is None)
        attached = commitments.attached_parent_node(str(top["plan_id"]), db=db)
        pending = str(attached["id"]) if attached is not None else ""
    return list(seen)


def _project_id(artifact_id: str, db: sqlite3.Connection) -> str | None:
    known, owner = artifact_links.identity_owner(artifact_id, db=db)
    if not known:
        raise service.PlanError("approval artifact is not registered")
    if owner and owner.startswith("project:"):
        return owner.removeprefix("project:")
    if owner:
        row = db.execute("SELECT project_id FROM threads WHERE id=?", (owner,)).fetchone()
        if row:
            return row["project_id"]
    return None


def _copies(row: sqlite3.Row, db: sqlite3.Connection) -> list[dict]:
    artifact_id = str(row["artifact_id"])
    _, roots = scope_roots(_project_id(artifact_id, db), db)
    if not roots:
        raise service.PlanError("approval document scope has no planning folder")
    root = roots[-1] / ".corsia" / "approved"
    document = str(row["path"] or row["external_id"])
    sha = str(row["doc_sha256"])
    result = [{"role": "page", "path": document, "copy_path": str(root / (sha[:16] + Path(document).suffix)), "sha256": sha}]
    for image in json.loads(row["images"]):
        item = {"role": "frame", **image}
        if image.get("sha256"):
            item["copy_path"] = str(root / (image["sha256"][:16] + Path(image["path"]).suffix))
        result.append(item)
    return result


def approved_versions_for_node(node_id: str, *, db: sqlite3.Connection) -> list[dict]:
    """Return each tied artifact at its latest recorded version; never read files."""
    ancestors = _ancestors(node_id, db)
    rows = db.execute(
        """WITH RECURSIVE
           walk(id) AS (SELECT value FROM json_each(?)),
           follows(id) AS (
               SELECT id FROM walk
               UNION SELECT n.follow_up_of FROM plan_nodes n JOIN follows f ON n.id=f.id
                 WHERE n.follow_up_of IS NOT NULL
           ),
           targets(id) AS (
               SELECT id FROM follows
               UNION SELECT d.depends_on_node_id FROM plan_node_dependencies d JOIN walk w ON d.node_id=w.id
           ),
           tied AS (
               SELECT a.artifact_id, MIN(a.node_id) AS approval_node
               FROM targets t JOIN artifact_approvals a ON a.node_id=t.id
               GROUP BY a.artifact_id
           )
           SELECT a.*,i.title,i.path,i.external_id,t.approval_node
           FROM tied t JOIN artifact_identities i ON i.id=t.artifact_id
           JOIN artifact_approvals a ON a.rowid=(
               SELECT x.rowid FROM artifact_approvals x WHERE x.artifact_id=t.artifact_id
               ORDER BY x.at DESC,x.rowid DESC LIMIT 1
           )""", (json.dumps(ancestors),),
    ).fetchall()
    return [{"artifact_id": str(row["artifact_id"]), "title": row["title"],
             "path": row["path"] or row["external_id"], "approved_at": row["at"],
             "approval_node": row["approval_node"], "source": row["source"],
             "copies": _copies(row, db)} for row in rows]


def approved_versions_for_pr(integration: str, type: str, external_id: str, *, db=None) -> dict:
    """Find a PR's open work and the versions tied to it, preserving lookup failures."""
    connection = db if db is not None else get_db()
    try:
        link = artifact_links.linked_node_for_artifact(integration, type, external_id, db=connection)
        status = link["status"]
        if status != "ok":
            return {"read_status": {"no_linked_task": "no open task", "several_linked_tasks": "several open tasks",
                                    "lookup_failed": "not registered"}[status],
                    "tie_status": None, "owning_node": None, "artifacts": []}
        node_id = str(link["node_id"])
        artifacts = approved_versions_for_node(node_id, db=connection)
        return {"read_status": "ok", "tie_status": "tied" if artifacts else "none tied",
                "owning_node": node_id, "artifacts": artifacts}
    except Exception:
        logging.getLogger(__name__).exception("Approved artifact lookup failed")
        return {"read_status": "lookup failed", "tie_status": None, "owning_node": None, "artifacts": []}
    finally:
        if db is None:
            connection.close()
