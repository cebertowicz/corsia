"""Project person-directed comment attention onto the existing plan surfaces."""

import hashlib
import json

from doceditor import person_comment_attention
from plans import public


def person_step_attention(db, node_id: str, artifact_id: str, user_id: str) -> dict | None:
    """The counts a document's comment task carries in its review bar, as its plan row shows them."""
    if public.comment_tasks(db, [node_id]).get(node_id) != artifact_id:
        return None
    comments, suggestions = person_comment_attention.counts(db, artifact_id, user_id)
    return {'artifact_id': artifact_id, 'comments': comments, 'suggestions': suggestions, 'system_task': True}


def annotate(db, envelope: dict, user_id: str) -> dict:
    """Attach the waiting counts to each open comment task and to the ask that carries them.

    A comment task is marked from its provenance whatever its count. An ask raised to the
    person on a document carries that document's counts; with several open, only the
    earliest raised does (``public.covering_asks``).
    """
    nodes = [node for plan in envelope.get('plans', []) for node in plan.get('nodes', [])
             if node.get('state') not in public.TERMINAL_STATES]
    tasks = public.comment_tasks(db, [str(node['id']) for node in nodes])
    documents = set(tasks.values())
    for node in nodes:
        documents.update(str(link['id']) for link in node.get('artifacts', [])
                         if link.get('type') == 'document' and link.get('role') == 'attention')
    counts_by_document = person_comment_attention.counts_many(db, documents, user_id)
    covering = public.covering_asks(db, documents, user_id)
    carriers = {asks[0]: artifact_id for artifact_id, asks in covering.items() if asks}
    for node in nodes:
        node_id = str(node['id'])
        document = tasks.get(node_id) or carriers.get(node_id)
        counts = counts_by_document.get(document, (0, 0)) if document else (0, 0)
        if document and (node_id in tasks or sum(counts)):
            node['comment_attention'] = {
                'artifact_id': document, 'comments': counts[0], 'suggestions': counts[1],
                'system_task': node_id in tasks,
            }
    fingerprint = [(key, counts_by_document.get(key, (0, 0)), covering[key][:1]) for key in sorted(documents)]
    if fingerprint:
        digest = hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()
        envelope['revision_token'] = hashlib.sha256(
            (envelope['revision_token'] + digest).encode()
        ).hexdigest()
    return envelope
