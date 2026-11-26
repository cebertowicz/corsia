"""A heavy single-person install for timing Activity's queue read.

The queue's response is small (tens of rows): what it needs right now. Its cost comes
from the history around those rows, which every read has to step past. So the seed is
two parts:

- the live rows, made through the real operations so their shape is what the product
  writes: raised decisions, open document asks, tasks waiting on the person, and
  documents with unread agent replies to the person's comments;
- the history, written in bulk: finished and archived plan nodes, answered decisions,
  cleared asks and holds, registered files, and read comment threads.

``scale`` multiplies the history only; a fraction gives a small install for a fast test.
``scale=1`` is a heavy install: 3,000 threads,
300,000 plan nodes, 60,000 artifact identities, 45,000 holds and 40,000 comments.
Everything is deterministic for a given scale, so two reads of the same seed compare.
"""
from __future__ import annotations

import json
import random
import sqlite3
from contextlib import contextmanager

from infra.database import PooledConnection
from infra.schema import SCHEMA_SQL
from plans import decisions, service, writer_context
from plans.aggregates_rebuild import rebuild
from plans.boundary import PlanActor, mutate
from registry import models

USER = 'user-1'
OTHER_USER = 'user-2'
PROJECTS = 15
#: History per unit of scale.
THREADS = 3000
NODES_PER_PLAN = 100
DECISIONS = 10_000
DOCUMENTS = 25_000
OTHER_ARTIFACTS = 25_000
ASK_HISTORY = 4_000
OTHER_HOLDS = 30_000
COMMENTED_DOCUMENTS = 4_000
COMMENTS_PER_DOCUMENT = 10
#: Live rows, the same at every scale.
LIVE_DECISIONS = 20
LIVE_ASKS = 6
LIVE_TASKS = 10
LIVE_UNREAD_DOCUMENTS = 8


def connect(path: str = ':memory:') -> sqlite3.Connection:
    connection = sqlite3.connect(path, check_same_thread=False, factory=PooledConnection)
    connection.row_factory = sqlite3.Row
    return connection


def _ts(i: int) -> str:
    """A fixed, ordered timestamp in 2026 for sequence number ``i``."""
    day, rest = divmod(i, 86_400)
    hour, rest = divmod(rest, 3600)
    minute, second = divmod(rest, 60)
    return f'2026-{1 + day // 28 % 10:02d}-{1 + day % 28:02d}T{hour:02d}:{minute:02d}:{second:02d}+00:00'


@contextmanager
def _bulk(db):
    """Raw history writes; every plan's aggregates are rebuilt after, as the operations keep them."""
    db.execute('PRAGMA foreign_keys=OFF')
    with writer_context.sanctioned_write():
        yield
        for plan_id in [row[0] for row in db.execute('SELECT id FROM plans')]:
            rebuild(db, plan_id)
    db.commit()
    db.execute('PRAGMA foreign_keys=ON')


def _threads(db, count: int) -> list[str]:
    db.executemany("INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,'t','t')",
                   [(f'project-{p}', f'Project {p}') for p in range(PROJECTS)])
    rows: list[tuple] = []
    ids: list[str] = []
    for i in range(count):
        thread_id = f'thread-{i:05d}'
        root = i < PROJECTS * 4
        spawned_by = None if root else f'thread-{i % (PROJECTS * 4):05d}'
        archived = None if root or i % 3 else _ts(i * 7)
        rows.append((thread_id, USER, f'project-{i % PROJECTS}', spawned_by, archived,
                     f'Thread {i}', _ts(i), _ts(i)))
        ids.append(thread_id)
    rows.append(('foreign-thread', OTHER_USER, None, None, None, 'Foreign', 't', 't'))
    db.executemany(
        """INSERT INTO threads (id,user_id,project_id,spawned_by,archived_at,title,model,created_at,updated_at)
           VALUES (?,?,?,?,?,?,'claude-code/sonnet',?,?)""", rows)
    db.commit()
    return ids


def _plans(db, threads: list[str]) -> dict[str, str]:
    """One plan per thread, under the real scope plans."""
    plans = {}
    for thread_id in threads[:PROJECTS * 4]:
        plans[thread_id] = service.ensure_plan('thread', thread_id, db=db).id
    db.commit()
    parents = {row['owner_id']: row['id'] for row in db.execute(
        "SELECT id, owner_id FROM plans WHERE owner_kind='project_scope'")}
    rows = []
    for i, thread_id in enumerate(threads[PROJECTS * 4:], PROJECTS * 4):
        plan_id = f'plan-{i:05d}'
        plans[thread_id] = plan_id
        state = 'archived' if i % 5 == 0 else 'active'
        rows.append((plan_id, thread_id, parents.get(f'project-{i % PROJECTS}'), state, _ts(i), _ts(i),
                     _ts(i * 3) if state == 'archived' else None))
    db.executemany(
        """INSERT INTO plans (id,owner_kind,owner_id,parent_plan_id,state,created_at,updated_at,archived_at)
           VALUES (?,'thread',?,?,?,?,?,?)""", rows)
    db.commit()
    return plans


_HISTORY_STATES = ('accepted',) * 14 + ('closed',) * 3 + ('failed', 'accepted_with_followup', 'review_requested')


def _history_nodes(db, plans: dict[str, str], per_plan: int) -> list[tuple[str, str]]:
    """Finished and archived work; a few agent tasks still open. Returns (node, plan) for user nodes."""
    rng = random.Random(7)
    rows, user_nodes = [], []
    seq = 0
    for p, plan_id in enumerate(plans.values()):
        for k in range(per_plan):
            seq += 1
            node_id = f'n-{p:05d}-{k:04d}'
            roll = rng.random()
            if roll < 0.03:
                state = rng.choice(('pending', 'active'))
            else:
                state = rng.choice(_HISTORY_STATES)
            assignee = 'user' if rng.random() < 0.12 else None
            archived = _ts(seq * 2) if rng.random() < 0.3 else None
            if assignee == 'user' and state in ('pending', 'active') and archived is None:
                state = 'accepted'  # open person tasks are the live rows only
            rows.append((node_id, plan_id, k, 'task', f'Work item {seq}', 'Done', state, assignee,
                         node_id, _ts(seq), _ts(seq), archived))
            if assignee == 'user':
                user_nodes.append((node_id, plan_id))
    db.executemany(
        """INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,assignee,
                                   commitment_id,created_at,updated_at,archived_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return user_nodes


def _decision_metadata(i: int, plan_id: str, node_id: str, thread_id: str) -> str:
    return json.dumps({
        'plan_decision': {
            'version': 1, 'plan_id': plan_id, 'node_id': node_id, 'source_node_id': node_id,
            'thread_id': thread_id, 'title': f'Old decision {i}', 'context': 'Context ' * 20,
            'tradeoff': 'Tradeoff ' * 10, 'recommendation': 'A', 'rationale': 'Because ' * 10,
            'options': [{'id': 'A', 'label': 'One', 'detail': 'First ' * 8},
                        {'id': 'B', 'label': 'Two', 'detail': 'Second ' * 8}],
            'other_enabled': True, 'pick': 'one', 'order_key': 0, 'state': 'answered',
            'revision': 4, 'raised_at': _ts(i), 'answer': {'option_id': 'A'},
        },
        'attention_state': 'cleared',
    })


def _count(base: int, scale: float) -> int:
    """``base`` history rows at ``scale``; at least one, so a small seed still has each kind."""
    return max(1, round(base * scale))


def _history_artifacts(db, plans: dict[str, str], user_nodes, scale: float) -> list[str]:
    """Answered decisions, documents with cleared asks, other files, and their holds."""
    owners = {plan_id: thread_id for thread_id, plan_id in plans.items()}
    identities: list[tuple] = []
    registrations: list[tuple] = []
    links: list[tuple] = []
    holds: list[tuple] = []
    for i in range(_count(DECISIONS, scale)):
        node_id, plan_id = user_nodes[i % len(user_nodes)]
        artifact_id = f'dec-{i:06d}'
        identities.append((artifact_id, 'local', 'plan-decision', f'decision-{i:06d}', f'Old decision {i}', None,
                           _decision_metadata(i, plan_id, node_id, owners[plan_id]), _ts(i)))
        registrations.append((f'reg-{artifact_id}', artifact_id, owners[plan_id], 'owner-active', _ts(i)))
        links.append((node_id, artifact_id, 'attention', _ts(i)))
    documents = []
    for i in range(_count(DOCUMENTS, scale)):
        artifact_id = f'doc-{i:06d}'
        thread_id = list(plans)[i % len(plans)]
        identities.append((artifact_id, 'workspace', 'document', f'/workspace/notes/doc-{i:06d}.md',
                           f'Document {i}', f'/workspace/notes/doc-{i:06d}.md', None, _ts(i)))
        registrations.append((f'reg-{artifact_id}', artifact_id, thread_id, 'owner-active', _ts(i)))
        documents.append(artifact_id)
    for i in range(_count(OTHER_ARTIFACTS, scale)):
        artifact_id = f'file-{i:06d}'
        kind = ('image', 'upload', 'url', 'pull-request')[i % 4]
        identities.append((artifact_id, 'upload' if kind == 'upload' else 'workspace', kind, f'file-{i:06d}',
                           f'File {i}', f'/workspace/files/file-{i:06d}', json.dumps({'size': i}), _ts(i)))
        registrations.append((f'reg-{artifact_id}', artifact_id, list(plans)[i % len(plans)],
                              'owner-active' if i % 7 else 'released', _ts(i)))
    for i in range(_count(ASK_HISTORY, scale)):  # cleared document asks
        node_id, plan_id = user_nodes[(i * 13) % len(user_nodes)]
        artifact_id = documents[i * 3 % len(documents)]
        links.append((node_id, artifact_id, 'attention', _ts(i)))
        holds.append((f'hold-ask-{i:06d}', node_id, 'blocked', 'review', 'Review this', 'user', artifact_id,
                      _ts(i), 'actor', _ts(i + 50), f'ev-ask-{i:06d}'))
    rng = random.Random(11)
    for i in range(_count(OTHER_HOLDS, scale)):
        node_id, _ = user_nodes[rng.randrange(len(user_nodes))]
        kind = 'paused' if i % 4 == 0 else 'blocked'
        owner_kind = ('thread', 'system', 'external', 'user')[i % 4]
        holds.append((f'hold-{i:06d}', node_id, kind, 'wait', 'Waiting', owner_kind, None,
                      _ts(i), 'actor', _ts(i + 60), f'ev-{i:06d}'))
    db.executemany(
        """INSERT INTO artifact_identities (id,integration,type,external_id,title,path,metadata,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?8)""", identities)
    db.executemany(
        """INSERT INTO artifact_registrations (id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at)
           VALUES (?,?,?,?,?,?5)""", registrations)
    db.executemany(
        """INSERT OR IGNORE INTO plan_node_artifact_links (node_id,artifact_id,role,provenance,created_at)
           VALUES (?,?,?,'seed',?)""", links)
    db.executemany(
        """INSERT INTO plan_node_holds (id,node_id,kind,reason_key,reason,owner_kind,owner_id,provenance,
                                        created_at,cleared_by_actor_id,cleared_at,clearance_event_id)
           VALUES (?,?,?,?,?,?,?,'seed',?,?,?,?)""", holds)
    return documents


def _history_comments(db, documents: list[str], plans: dict[str, str], scale: float) -> None:
    """Comment threads on documents, all read by the person."""
    threads = list(plans)
    comments: list[tuple] = []
    views: list[tuple] = []
    seq = 0
    for d in range(min(_count(COMMENTED_DOCUMENTS, scale), len(documents))):
        artifact_id = documents[d]
        agent = threads[d % len(threads)]
        for r in range(COMMENTS_PER_DOCUMENT // 2):
            seq += 2
            root_id, reply_id = f'c-{d:06d}-{r}', f'c-{d:06d}-{r}-reply'
            person_root = r % 2 == 0
            comments.append((root_id, artifact_id, None, 'Person' if person_root else 'Agent',
                             'user' if person_root else 'agent', None if person_root else agent, _ts(seq)))
            comments.append((reply_id, artifact_id, root_id, 'Agent' if person_root else 'Person',
                             'agent' if person_root else 'user', agent if person_root else None, _ts(seq + 1)))
            views.append((USER, root_id, _ts(seq + 1), reply_id))
    db.executemany(
        """INSERT INTO doc_comments (id,artifact_id,parent_id,author,author_kind,author_thread_id,body,created_at,updated_at)
           VALUES (?,?,?,?,?,?,'Comment text',?,?7)""", comments)
    db.executemany(
        'INSERT INTO doc_comment_thread_views (user_id,root_id,seen_at,seen_reply_id) VALUES (?,?,?,?)', views)


def _actor(db, thread_id: str) -> PlanActor:
    project = db.execute('SELECT project_id FROM threads WHERE id=?', (thread_id,)).fetchone()[0]
    return PlanActor(thread_id, USER, project)


def _raise_decision(db, plan_id: str, owner: str, i: int) -> None:
    """Draft, review and raise one decision as a root thread, which reviews its own."""
    node = service.add_node(plan_id, kind='task', title=f'Work {i}', acceptance_criteria='Done', db=db)
    db.commit()
    actor = _actor(db, owner)
    decision = decisions.draft(
        plan_id, source_node_id=node.id, mutation_id=f'seed-draft-{i}', actor=actor,
        provenance='tool.plans.decision', title=f'Live decision {i}', context='Context for the choice.',
        tradeoff='One or the other.', recommendation='A', rationale='A is simpler.',
        options=[{'id': 'A', 'label': 'One', 'detail': 'First'}, {'id': 'B', 'label': 'Two', 'detail': 'Second'}],
        other_enabled=True, db=db,
    ).result['decision']
    decision = decisions.parent_review(
        decision['id'], expected_revision=decision['revision'], mutation_id=f'seed-review-{i}',
        actor=actor, provenance='p', db=db).result['decision']
    decisions.raise_to_user(
        decision['id'], expected_revision=decision['revision'], mutation_id=f'seed-raise-{i}',
        actor=actor, provenance='p', db=db)
    db.commit()


def _live(db, plans: dict[str, str]) -> None:
    """What waits on the person now, made through the real operations."""
    roots = list(plans)[:PROJECTS * 4]
    for i in range(LIVE_DECISIONS):
        owner = roots[i % len(roots)]
        _raise_decision(db, plans[owner], owner, i)
    for i in range(LIVE_ASKS):
        owner = roots[(i * 3) % len(roots)]
        external_id = f'/workspace/review/ask-{i}.md'
        models.register_artifact(integration='workspace', type='document', external_id=external_id,
                                 parent_thread_id=owner, title=f'Review {i}', db=db)
        artifact_id = db.execute("SELECT id FROM artifact_identities WHERE external_id=?",
                                 (external_id,)).fetchone()['id']
        node = service.add_node(plans[owner], kind='task', title=f'Draft {i}', acceptance_criteria='Reviewed', db=db)
        db.commit()
        mutate(plans[owner], 'block_node', {
            'node_id': node.id, 'reason_key': 'review', 'reason': f'Please review draft {i}',
            'owner_kind': 'user', 'ask_artifact_id': artifact_id, 'expected_revision': node.revision,
        }, actor=_actor(db, owner), provenance='seed', mutation_id=f'seed-ask-{i}', db=db)
        db.commit()
    for i in range(LIVE_TASKS):
        owner = roots[(i * 5 + 1) % len(roots)]
        service.add_node(plans[owner], kind='task', title=f'Sign in to service {i}',
                         acceptance_criteria='Signed in', assignee='user',
                         description='How-to: open the page and sign in.', db=db)
        db.commit()
    for i in range(LIVE_UNREAD_DOCUMENTS):
        owner = roots[(i * 7 + 2) % len(roots)]
        external_id = f'/workspace/live/notes-{i}.md'
        models.register_artifact(integration='workspace', type='document', external_id=external_id,
                                 parent_thread_id=owner, title=f'Notes {i}', db=db)
        artifact_id = db.execute("SELECT id FROM artifact_identities WHERE external_id=?",
                                 (external_id,)).fetchone()['id']
        db.execute("""INSERT INTO doc_comments (id,artifact_id,author,author_kind,body,created_at,updated_at)
                      VALUES (?,?,'Person','user','Question','2026-12-01','2026-12-01')""",
                   (f'live-root-{i}', artifact_id))
        db.execute("""INSERT INTO doc_comments (id,artifact_id,parent_id,author,author_kind,author_thread_id,body,
                                                created_at,updated_at)
                      VALUES (?,?,?,'Agent','agent',?,'Answer','2026-12-02','2026-12-02')""",
                   (f'live-reply-{i}', artifact_id, f'live-root-{i}', owner))
        db.commit()


def seed(db, scale: float = 1) -> dict[str, int]:
    """Build the install into an empty connection; return the table counts."""
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA_SQL)
    # The first PROJECTS * 4 threads are roots; keep some spawned threads at any scale.
    threads = _threads(db, max(PROJECTS * 5, _count(THREADS, scale)))
    plans = _plans(db, threads)
    with _bulk(db):
        user_nodes = _history_nodes(db, plans, NODES_PER_PLAN)
        documents = _history_artifacts(db, plans, user_nodes, scale)
        _history_comments(db, documents, plans, scale)
    _live(db, plans)
    tables = ('threads', 'plans', 'plan_nodes', 'plan_node_holds', 'plan_node_artifact_links',
              'artifact_identities', 'artifact_registrations', 'doc_comments')
    return {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables}
