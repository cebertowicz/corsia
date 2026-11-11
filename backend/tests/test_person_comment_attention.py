"""Comments for a person remain actionable until read, decided, or dismissed."""

import uuid

import pytest

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import comment_actions, feedback_comments, person_comment_attention
from doceditor.feedback_state import annotate_thread_attention, mark_thread_viewed
from infra.database import get_db, write_transaction
from plans import artifact_links, decision_writes, operator_display_state, person_comment_task, public, service
from plans.boundary import PlanActor


def test_one_task_per_document_closes_and_can_reopen(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact = seed_document(db, "owner", name="brief.md")
        root, _ = add_comment(db, artifact, author_kind="user", author_id=None)
        reply, _ = add_comment(db, artifact, author_id="owner", parent_id=root["id"])
        with write_transaction(db, scope="test_reconcile") as connection:
            first = person_comment_task.reconcile(connection, artifact)
            assert person_comment_task.reconcile(connection, artifact) == first
        row = db.execute("SELECT title,assignee FROM plan_nodes WHERE id=?", (first,)).fetchone()
        assert tuple(row) == ("brief.md", "user")
        with write_transaction(db, scope="test_comment_read") as connection:
            mark_thread_viewed(connection, "owner-token", artifact, root["id"], reply["id"])
            person_comment_task.reconcile(connection, artifact)
        assert db.execute("SELECT state FROM plan_nodes WHERE id=?", (first,)).fetchone()[0] == "accepted"
        add_comment(db, artifact, author_id="owner", parent_id=root["id"])
        with write_transaction(db, scope="test_reopen") as connection:
            second = person_comment_task.reconcile(connection, artifact)
        assert second and second != first
    finally:
        db.close()


def test_two_document_steps_cover_only_the_comment_task(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact = seed_document(db, 'owner')
        root, _ = add_comment(db, artifact, author_kind='user', author_id=None)
        add_comment(db, artifact, author_id='owner', parent_id=root['id'])
        with write_transaction(db, scope='test_comment_coverage') as connection:
            comment_task = person_comment_task.reconcile(connection, artifact)
            plan = service.ensure_plan('thread', 'owner', db=connection)
            asks = []
            for title in ('First ask', 'Second ask'):
                node = service.add_node(
                    plan.id, kind='task', title=title, acceptance_criteria='Done',
                    assignee='user', actor_id=None, provenance='test', db=connection,
                )
                artifact_links.link(node.id, artifact, 'input', actor_id=None,
                                    provenance='test', db=connection)
                asks.append(node.id)
            found = public.open_asks_for_artifact(artifact, db=connection)
            assert {row['node_id'] for row in found} == {comment_task, *asks}
            assert operator_display_state.covered_nodes([comment_task, *asks], db=connection) == {comment_task}
            actor = PlanActor(thread_id=None, user_id='owner-token', project_id=None, kind='user')
            envelope = public.read_operator_thread('owner', actor=actor, db=connection)
            nodes = {node['id']: node for plan in envelope['plans'] for node in plan['nodes']}
            assert not nodes[comment_task]['requires_user_action']
            assert all(nodes[node_id]['requires_user_action'] for node_id in asks)
            assert connection.execute(
                f"SELECT {operator_display_state.ask_deferred_sql('n.id', plan_state_sql='p.state')} "
                'FROM plan_nodes n JOIN plans p ON p.id=n.plan_id WHERE n.id=?', (comment_task,),
            ).fetchone()[0] == 1
            for node_id in asks:
                service.transition_node(node_id, 'closed', actor_id=None, provenance='test', db=connection)
            assert not operator_display_state.covered_nodes([comment_task], db=connection)
            envelope = public.read_operator_thread('owner', actor=actor, db=connection)
            nodes = {node['id']: node for plan in envelope['plans'] for node in plan['nodes']}
            assert nodes[comment_task]['requires_user_action']
    finally:
        db.close()


def test_decision_without_a_document_link_does_not_cover_comments(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact = seed_document(db, 'owner')
        root, _ = add_comment(db, artifact, author_kind='user', author_id=None)
        add_comment(db, artifact, author_id='owner', parent_id=root['id'])
        with write_transaction(db, scope='test_unlinked_decision') as connection:
            comment_task = person_comment_task.reconcile(connection, artifact)
            plan = service.ensure_plan('thread', 'owner', db=connection)
            source = service.add_node(plan.id, kind='task', title='Source',
                                      acceptance_criteria='Done', actor_id=None,
                                      provenance='test', db=connection)
            artifact_links.link(source.id, artifact, 'input', actor_id=None,
                                provenance='test', db=connection)
            decision_writes.create_raised(connection, plan.id, source.id, {
                'title': 'Choose', 'context': 'Context', 'tradeoff': 'Tradeoff',
                'recommendation': 'A', 'rationale': 'Reason', 'other_enabled': True,
                'options': [{'id': 'A', 'label': 'A'}, {'id': 'B', 'label': 'B'}],
            }, 'test')
            assert not operator_display_state.covered_nodes([comment_task], db=connection)
    finally:
        db.close()


@pytest.mark.parametrize('ask_kind', ['person_step', 'review_hold'])
@pytest.mark.parametrize('ask_first', [True, False])
def test_comment_task_returns_when_document_ask_closes(isolated_db, ask_kind, ask_first):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact = seed_document(db, 'owner')
        root, _ = add_comment(db, artifact, author_kind='user', author_id=None)
        add_comment(db, artifact, author_id='owner', parent_id=root['id'])
        actor = PlanActor('owner', 'owner-token', None)
        person = PlanActor(None, 'owner-token', None, kind='user')
        with write_transaction(db, scope='test_ask_order') as connection:
            plan = service.ensure_plan('thread', 'owner', db=connection)
            comment_task = None if ask_first else person_comment_task.reconcile(connection, artifact)
            node = service.add_node(
                plan.id, kind='task', title='Review', acceptance_criteria='Done',
                assignee='user' if ask_kind == 'person_step' else None,
                actor_id=None, provenance='test', db=connection,
            )
            if ask_kind == 'person_step':
                artifact_links.link(node.id, artifact, 'input', actor_id=None,
                                    provenance='test', db=connection)
                hold_id = None
            else:
                public.mutate(plan.id, 'block_node', {
                    'node_id': node.id, 'expected_revision': node.revision,
                    'owner_kind': 'user', 'ask_artifact_id': artifact,
                    'reason_key': 'review', 'reason': 'Please review',
                }, actor=actor, provenance='test', mutation_id=uuid.uuid4().hex, db=connection)
                hold_id = connection.execute(
                    'SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL',
                    (node.id,),
                ).fetchone()['id']
            if comment_task is None:
                comment_task = person_comment_task.reconcile(connection, artifact)
            assert operator_display_state.covered_nodes([comment_task], db=connection) == {comment_task}
            if hold_id:
                public.mutate(plan.id, 'resolve_blocker', {
                    'node_id': node.id, 'expected_revision': service._node_row(connection, node.id)['revision'],
                    'hold_id': hold_id,
                }, actor=actor, provenance='test', mutation_id=uuid.uuid4().hex, db=connection)
            else:
                service.transition_node(node.id, 'closed', actor_id=None, provenance='test', db=connection)
            assert not operator_display_state.covered_nodes([comment_task], db=connection)
            envelope = public.read_operator_thread('owner', actor=person, db=connection)
            nodes = {item['id']: item for item in envelope['plans'][0]['nodes']}
            assert nodes[comment_task]['requires_user_action']
    finally:
        db.close()


def test_tagged_person_is_a_recipient(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact = seed_document(db, "owner")
        comment, _ = add_comment(db, artifact, author_id="author")
        comment["tagged_user_id"] = "owner-token"
        assert "ui" in feedback_comments.recipients(db, comment)
    finally:
        db.close()


def test_read_does_not_decide_suggestions_and_dismiss_preserves_them(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "author")
        artifact = seed_document(db, "ui")
        comment, _ = add_comment(db, artifact, author_id="author")
        suggestion, _ = add_comment(db, artifact, author_id="author", replacement="New text")
        assert person_comment_attention.counts(db, artifact, "owner-token") == (1, 1)
        projected = [{'id': suggestion['id']}]
        annotate_thread_attention(db, 'owner-token', artifact, projected)
        assert projected[0]['new_comment']
        with write_transaction(db, scope="test_read_comment") as connection:
            mark_thread_viewed(connection, "owner-token", artifact, comment["id"], comment["id"])
            mark_thread_viewed(connection, "owner-token", artifact, suggestion["id"], suggestion["id"])
        assert person_comment_attention.counts(db, artifact, "owner-token") == (0, 0)
        assert db.execute("SELECT decision FROM doc_comments WHERE id=?", (suggestion["id"],)).fetchone()[0] is None
        pending, _ = add_comment(db, artifact, author_id="author", replacement="Another change")
        assert person_comment_attention.counts(db, artifact, "owner-token") == (0, 1)
        with write_transaction(db, scope="test_dismiss_comments") as connection:
            person_comment_attention.dismiss(connection, artifact, "owner-token")
        assert person_comment_attention.counts(db, artifact, "owner-token") == (0, 0)
        assert db.execute("SELECT decision FROM doc_comments WHERE id=?", (pending["id"],)).fetchone()[0] is None
        newer, _ = add_comment(db, artifact, author_id="author", replacement="New suggestion")
        assert person_comment_attention.counts(db, artifact, "owner-token") == (0, 1)
        with write_transaction(db, scope="test_decide_suggestion") as connection:
            comment_actions.record_decision(newer['id'], decision='reject', decided_by='Person',
                                            decided_by_thread_id=None, reason=None, db=connection)
        assert person_comment_attention.counts(db, artifact, "owner-token") == (0, 0)
    finally:
        db.close()


def test_projection_batches_fifty_documents_with_twenty_threads_each(isolated_db):
    """The projection uses a fixed number of reads as the document set grows."""
    import time
    from doceditor.person_comment_projection import annotate

    db = get_db()
    try:
        seed_thread(db, 'owner')
        plan = service.ensure_plan('thread', 'owner', db=db)
        node_ids = []
        stamp = '2026-10-01T00:00:00+00:00'
        for i in range(50):
            artifact = seed_document(db, 'owner', name=f'scale-{i}.md')
            node = service.add_node(plan.id, kind='task', title=f'scale-{i}.md',
                                    acceptance_criteria='Read', assignee='user',
                                    actor_id=None, provenance='test', db=db)
            artifact_links.link(node.id, artifact, 'input', actor_id=None,
                                provenance='test', db=db)
            node_ids.append(node.id)
            batch = f'scale-batch-{i}'
            db.execute("""INSERT INTO doc_feedback_batches
                (id,artifact_id,author_kind,author_id,recipient_id,delivery_policy,state,
                 opened_at,last_item_at,closes_at,created_at,updated_at)
                VALUES (?,?, 'agent','owner','ui','immediate','delivered',?,?,?,?,?)""",
                (batch, artifact, stamp, stamp, stamp, stamp, stamp))
            for j in range(20):
                root, reply = f'scale-root-{i}-{j}', f'scale-reply-{i}-{j}'
                db.execute("""INSERT INTO doc_comments
                    (id,artifact_id,author,author_kind,body,created_at,updated_at)
                    VALUES (?,?,'Person','user','Question',?,?)""", (root, artifact, stamp, stamp))
                db.execute("""INSERT INTO doc_comments
                    (id,artifact_id,parent_id,author,author_kind,author_thread_id,body,created_at,updated_at)
                    VALUES (?,?,?,'Agent','agent','owner','Reply',?,?)""", (reply, artifact, root, stamp, stamp))
                db.execute('INSERT INTO doc_feedback_batch_items(batch_id,comment_id) VALUES (?,?)', (batch, reply))
            if i == 0:
                db.commit()
                first_queries = []
                db.set_trace_callback(first_queries.append)
                public.read_operator_thread('owner', actor=PlanActor(None, 'owner-token', None, kind='user'), db=db)
                db.set_trace_callback(None)
        db.commit()
        actor = PlanActor(None, 'owner-token', None, kind='user')
        queries = []
        db.set_trace_callback(queries.append)
        start = time.perf_counter()
        envelope = public.read_operator_thread('owner', actor=actor, db=db)
        projection_seconds = time.perf_counter() - start
        operator_count = len(queries)
        queries.clear()
        nodes = envelope['plans'][0]['nodes']
        assert len(nodes) == 50
        one = {'revision_token': 'one', 'plans': [{'nodes': [nodes[0]]}]}
        annotate(db, one, 'owner-token')
        one_count = len(queries)
        queries.clear()
        start = time.perf_counter()
        annotate(db, envelope, 'owner-token')
        annotation_seconds = time.perf_counter() - start
        many_count = len(queries)
        count_sql = next(query for query in queries if 'FROM doc_comments item' in query)
        db.set_trace_callback(None)
        query_plan = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + count_sql)]
        assert any('idx_doc_comments_artifact' in step for step in query_plan)
        assert len(first_queries) == operator_count
        assert one_count == many_count == 3
        assert all(node['comment_attention']['comments'] == 20 for node in nodes)
        print(f'SCALE nodes=50 documents=50 comment_threads=1000 comments=2000 '
              f'feedback_items=1000 operator_queries={operator_count} annotation_queries={many_count} '
              f'operator_seconds={projection_seconds:.4f} annotation_seconds={annotation_seconds:.4f} '
              f'query_plan={query_plan}')
    finally:
        db.set_trace_callback(None)
        db.close()
