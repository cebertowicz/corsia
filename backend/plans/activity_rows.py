"""Activity review and task rows.

The caller must pass only the output of annotate_attention(_authorized_raised(...)).
All state reads here are batched; the existing authorized-list producer has its own costs.
"""
from __future__ import annotations

import json
from collections import defaultdict

from plans import activity_facts, activity_projection, activity_reducer, attention_detail, node_states, operator_display_state, person_comment_task, user_ask
from plans.boundary import PlanActor
from infra.activity_queue_base import clip_title
from infra.auth import may_access_artifact


def _due_person_tasks(db, plans):
    """The person's open task leaves that are waiting on them now."""
    compact = activity_facts.compact_nodes(list(plans), db=db)
    _, human, waits = activity_facts.decision_indexes(list(plans), db=db)
    task_nodes = []
    for plan_id, nodes in compact.items():
        work = node_states.work_ids(nodes)
        parents = node_states.parent_ids(nodes, work=work)
        leaves = node_states.leaves(nodes, work=work, parents=parents)
        for node in leaves:
            if node["assignee"] != "user" or node["kind"] != "task" or node["state"] not in ("pending", "active"):
                continue
            reduced = activity_reducer.reduce_node(node, plans[plan_id], review_kind=activity_facts.review_kind(node, plans[plan_id], human), decision_wait=node["id"] in waits, child_cause=None, executor_stop_fact=None)
            _, causes, *_ = activity_reducer._execution_facts(
                node, plans[plan_id], decision_wait=node['id'] in waits, child_cause=None,
            )
            # A linked ask may need the person while the task itself is blocked.
            task_due = operator_display_state.asks_user_from_facts(
                node['state'], reduced.condition, node['assignee'], None,
                has_attention=False, causes=causes,
            )
            if reduced.requires_user_action and task_due:
                task_nodes.append(node)
    return task_nodes


def rows(db, user_id, annotated):
    actor = PlanActor(None, user_id, None, kind="user")
    reviews = [item for item in annotated if item.get("hold_ids") and not item.get("deferred")
               and item["identity"]["type"] in {"document", "image"}]
    keys = [(i["identity"]["integration"], i["identity"]["type"], i["external_id"]) for i in reviews]
    ids = {}
    if keys:
        for r in attention_detail.active_identities(db, "i.id,i.integration,i.type,i.external_id,i.created_at", keys):
            ids[(r["integration"], r["type"], r["external_id"])] = dict(r)
    ask_rows = []
    if ids:
        ask_rows = [dict(r) for r in db.execute(user_ask.open_asks_sql(
            "l.artifact_id, h.id AS hold_id, h.created_at AS raised_at, h.reason, p.id AS plan_id, p.owner_id AS asker_id, p.owner_kind",
            by="all") + " AND l.artifact_id IN (SELECT value FROM json_each(?))"
            + " AND " + user_ask.ask_not_deferred_sql('h.id')
            + " AND NOT " + operator_display_state.ask_deferred_sql('n.id', plan_state_sql='p.state')
            + " ORDER BY h.created_at DESC,h.id DESC", (json.dumps([v["id"] for v in ids.values()]),))]
    eligible = activity_projection.counted_plan_ids({r["plan_id"] for r in ask_rows}, actor=actor, db=db)
    asks = defaultdict(list)
    for ask in ask_rows:
        if ask["plan_id"] in eligible:
            asks[ask["artifact_id"]].append(ask)
    # user_settle.prepare refuses non-thread plans; boundary.mutate refuses them too.
    candidate_plans = [str(r[0]) for r in db.execute(
        "SELECT DISTINCT p.id FROM plans p JOIN threads t ON t.id=p.owner_id "
        "JOIN plan_nodes n ON n.plan_id=p.id WHERE p.owner_kind='thread' AND t.user_id=? "
        "AND n.assignee='user' AND n.kind='task' AND n.state IN ('pending','active') "
        "AND " + node_states.in_work_sql("n"), (user_id,))]
    allowed = activity_projection.counted_plan_ids(candidate_plans, actor=actor, db=db)
    plans = {r["id"]: dict(r) for r in db.execute("SELECT * FROM plans WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(allowed)),))}
    task_nodes = _due_person_tasks(db, plans)
    node_ids = [n["id"] for n in task_nodes]
    details = {r["id"]: dict(r) for r in db.execute("SELECT id,title,description,revision,created_at FROM plan_nodes WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(node_ids),))}
    links = person_comment_task.comment_tasks(db, node_ids)
    # Match the artifact route's owner selection; access is that route's own predicate, one
    # thread read per comment-linked task, so a change to the rule governs these rows too.
    authorized_artifacts = set()
    if links:
        for r in db.execute("""SELECT artifact_id, parent_thread_id FROM (
            SELECT i.id AS artifact_id, reg.parent_thread_id,
                   row_number() OVER (PARTITION BY i.id ORDER BY
                       CASE reg.relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                       reg.updated_at DESC, reg.id ASC) AS rn
              FROM artifact_identities i JOIN artifact_registrations reg ON reg.artifact_id=i.id
             WHERE i.state='active' AND reg.relationship_state IN ('owner-pinned','owner-active')
               AND i.id IN (SELECT value FROM json_each(?))) WHERE rn=1""", (json.dumps(list(links.values())),)):
            owner = r['parent_thread_id']
            if owner and may_access_artifact(owner, user_id, db=db):
                authorized_artifacts.add(r['artifact_id'])
    source_ids = {a["asker_id"] for a in ask_rows if a["owner_kind"] == "thread"} | {plans[n["plan_id"]]["owner_id"] for n in task_nodes}
    sources = attention_detail._sources(db, source_ids)
    out = []
    for item in reviews:
        identity = item["identity"]
        found = ids.get((identity["integration"], identity["type"], item["external_id"]))
        if not found or not asks[found["id"]]:
            continue
        newest = asks[found["id"]][0]
        artifact_id = found["id"]
        out.append({"renderer":"review", "kind":"review", "key":f"review:{artifact_id}", "external_id":item["external_id"], "title":clip_title(newest.get("reason")) or item.get("title") or item["external_id"], "raised_at":newest["raised_at"], "source":sources.get(newest["asker_id"], dict(attention_detail._UNKNOWN_SOURCE)), "open":{"artifact_id":artifact_id}, "detail":{"artifact_id":artifact_id, "hold_ids":item["hold_ids"]}, "priority":0})
    for node in task_nodes:
        detail = details[node["id"]]
        artifact_id = links.get(node["id"])
        if artifact_id and artifact_id not in authorized_artifacts:
            continue
        out.append({"renderer":"task", "kind":"task", "key":f"task:{node['id']}", "external_id":node["id"], "title":detail["title"], "raised_at":detail["created_at"], "source":sources.get(plans[node["plan_id"]]["owner_id"], dict(attention_detail._UNKNOWN_SOURCE)), "open":{"artifact_id":artifact_id} if artifact_id else None, "detail":{"plan_id":node["plan_id"], "node_id":node["id"], "revision":detail["revision"], "description":detail["description"], **({"comment_artifact_id":artifact_id} if artifact_id else {})}, "priority":0})
    return out


def dedupe_comment_tasks(db, user_id, projected, document_rows):
    """Use the *actual* authorized document rows; do not suppress unrelated directed work."""
    reviewed = {r["detail"]["artifact_id"] for r in projected if r["kind"] == "review"}
    reply_ids = {thread['reply_id']: (r['detail']['artifact_id'], thread['root_id'])
                 for r in document_rows if r.get('kind') == 'document' and r.get('detail', {}).get('artifact_id')
                 for thread in r['detail'].get('threads', ()) if thread.get('root_id') and thread.get('reply_id')}
    reply_roots = {}
    if reply_ids:
        for r in db.execute('SELECT id, artifact_id, parent_id, created_at FROM doc_comments WHERE id IN (SELECT value FROM json_each(?))', (json.dumps(list(reply_ids)),)):
            expected = reply_ids[r['id']]
            if (r['artifact_id'], r['parent_id']) == expected:
                reply_roots[expected] = max(reply_roots.get(expected, ('', '')), (r['created_at'], r['id']))
    artifacts = {r["detail"]["comment_artifact_id"] for r in projected if r["kind"] == "task" and r["detail"].get("comment_artifact_id")}
    directed = defaultdict(set)
    if artifacts:
        from doceditor import person_comment_attention as pc
        for r in db.execute(f"SELECT item.artifact_id, COALESCE(item.parent_id,item.id) AS root_id, item.created_at, item.id {pc._DIRECTED} {pc._UNREAD}", pc._params(artifacts, user_id)):
            directed[r['artifact_id']].add((r['root_id'], r['created_at'], r['id']))
    return [r for r in projected if not (r['kind'] == 'task' and (artifact := r['detail'].get('comment_artifact_id')) and (artifact in reviewed or (directed[artifact] and all(reply_roots.get((artifact, root), ('', '')) >= (created, item_id) for root, created, item_id in directed[artifact]))))]
