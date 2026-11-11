"""Project person-directed comment attention onto the existing plan surfaces."""

import hashlib
import json

from doceditor import person_comment_attention
from plans import public

PROVENANCE = 'document.person_comment_task'


def _linked_tasks(db, node_ids: list[str]) -> dict[str, str]:
    if not node_ids:
        return {}
    rows = db.execute(
        """SELECT node_id,artifact_id FROM plan_node_artifact_links
            WHERE provenance=? AND node_id IN (SELECT value FROM json_each(?))""",
        (PROVENANCE, json.dumps(node_ids)),
    ).fetchall()
    return {str(row['node_id']): str(row['artifact_id']) for row in rows}


def annotate(db, envelope: dict, user_id: str) -> dict:
    """Attach counts and hide the comment task while a document ask is open."""
    nodes = [node for plan in envelope.get('plans', []) for node in plan.get('nodes', [])]
    linked = _linked_tasks(db, [str(node['id']) for node in nodes])
    documents = set(linked.values())
    documents.update(carrier['artifact_id'] for plan in envelope.get('plans', [])
                     for carrier in plan.get('_comment_carriers', []))
    for node in nodes:
        documents.update(str(link['id']) for link in node.get('artifacts', [])
                         if link.get('type') == 'document' and link.get('role') in ('input', 'attention'))
    counts_by_document = person_comment_attention.counts_many(db, documents, user_id)
    asks_by_document: dict[str, set[str]] = {artifact_id: set() for artifact_id in documents}
    for row in public.open_asks_for_artifacts(documents, db=db):
        node_id = str(row['node_id'])
        if (row['owner_user_id'] == user_id and row['provenance'] != PROVENANCE
                and node_id not in linked):
            asks_by_document[str(row['artifact_id'])].add(node_id)
    attention = {artifact_id: (counts_by_document.get(artifact_id, (0, 0)), asks)
                 for artifact_id, asks in asks_by_document.items()}
    node_document = {node_id: artifact_id for artifact_id, (_, asks) in attention.items()
                     for node_id in asks}
    node_document.update(linked)
    for plan in envelope.get('plans', []):
        for carrier in plan.pop('_comment_carriers', []):
            node_document[carrier['carrier_id']] = carrier['artifact_id']
        kept = []
        for node in plan.get('nodes', []):
            node_id = str(node['id'])
            artifact_id = linked.get(node_id)
            document = node_document.get(node_id)
            if document:
                counts = attention[document][0]
                if sum(counts):
                    node['comment_attention'] = {
                        'artifact_id': document, 'comments': counts[0], 'suggestions': counts[1],
                        'system_task': artifact_id == document,
                    }
            kept.append(node)
        plan['nodes'] = kept
    fingerprint = [(key, counts, sorted(asks)) for key, (counts, asks) in sorted(attention.items())]
    if fingerprint:
        digest = hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()
        envelope['revision_token'] = hashlib.sha256(
            (envelope['revision_token'] + digest).encode()
        ).hexdigest()
    return envelope
