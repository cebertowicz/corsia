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
        row = db.execute(
            "SELECT n.title,n.assignee,n.parent_id,p.owner_kind,p.owner_id FROM plan_nodes n"
            " JOIN plans p ON p.id=n.plan_id WHERE n.id=?", (first,),
        ).fetchone()
        # Coverage joins the plan owner's thread, so the task must be a thread-owned root leaf.
        assert tuple(row) == ("brief.md", "user", None, "thread", "owner")
        with write_transaction(db, scope="test_comment_read") as connection:
            mark_thread_viewed(connection, "owner-token", artifact, root["id"], reply["id"])
            person_comment_task.reconcile(connection, artifact)
        assert db.execute("SELECT state FROM plan_nodes WHERE id=?", (first,)).fetchone()[0] == "accepted"
        assert db.execute("SELECT COUNT(*) FROM thread_updates WHERE node_id=?", (first,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM messages WHERE json_extract(metadata,'$.node_id')=?", (first,)).fetchone()[0] == 0
        add_comment(db, artifact, author_id="owner", parent_id=root["id"])
        with write_transaction(db, scope="test_reopen") as connection:
            second = person_comment_task.reconcile(connection, artifact)
        assert second and second != first
        from doceditor.person_comment_projection import annotate
        actor = PlanActor(thread_id=None, user_id="owner-token", project_id=None, kind="user")
        envelope = public.read_operator_thread("owner", actor=actor, db=db)
        shown = {n["id"]: n for n in annotate(db, envelope, "owner-token")["plans"][0]["nodes"]}
        # The settled task stays listed as finished; only its open successor carries the count.
        assert first in shown and "comment_attention" not in shown[first]
        assert shown[second]["comment_attention"]["comments"] == 1
    finally:
        db.close()


@pytest.mark.parametrize('feedback_first', [True, False])
@pytest.mark.parametrize('moving', [True, False])
def test_feedback_covers_person_task_until_settled(isolated_db, feedback_first, moving):
    from doceditor.person_comment_projection import annotate
    from plans import operator_chat_state

    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact = seed_document(db, 'owner')
        with write_transaction(db, scope='test_feedback_plan') as connection:
            plan = service.ensure_plan('thread', 'owner', db=connection)

        def add_feedback(connection):
            node = service.add_node(plan.id, kind='task', title='Agent feedback',
                                    acceptance_criteria='Done', actor_id=None,
                                    provenance='document.feedback_batch', db=connection)
            artifact_links.link(node.id, artifact, 'input', actor_id=None,
                                provenance='document.feedback_batch', db=connection)
            if moving:
                service.transition_node(node.id, 'active', actor_id='owner', db=connection)
            return node

        if feedback_first:
            with write_transaction(db, scope='test_feedback_first') as connection:
                feedback = add_feedback(connection)
        root, _ = add_comment(db, artifact, author_kind='user', author_id=None)
        add_comment(db, artifact, author_id='owner', parent_id=root['id'])
        with write_transaction(db, scope='test_comment_coverage') as connection:
            person_task = person_comment_task.reconcile(connection, artifact)
            if not feedback_first:
                feedback = add_feedback(connection)
            actor = PlanActor(thread_id=None, user_id='owner-token', project_id=None, kind='user')

            def nodes():
                envelope = public.read_operator_thread('owner', actor=actor, db=connection)
                return {n['id']: n for n in annotate(connection, envelope, 'owner-token')['plans'][0]['nodes']}

            def attention():
                return operator_chat_state.read_thread_states(['owner'], actor=actor, db=connection)['states']['owner']['attention_items']

            assert operator_display_state.covered_nodes([feedback.id, person_task], db=connection) == {person_task}
            shown = nodes()
            assert person_task not in shown
            assert shown[feedback.id]['comment_attention']['comments'] == 1
            assert len(attention()) == 1
            ask = service.add_node(plan.id, kind='task', title='Review', acceptance_criteria='Done',
                                   assignee='user', actor_id=None, provenance='test', db=connection)
            artifact_links.link(ask.id, artifact, 'input', actor_id=None, provenance='test', db=connection)
            shown = nodes()
            assert shown[ask.id]['comment_attention']['comments'] == 1
            assert 'comment_attention' not in shown[feedback.id]
            assert len(attention()) == 1
            service.transition_node(ask.id, 'closed', actor_id=None, provenance='test', db=connection)
            service.transition_node(feedback.id, 'closed', actor_id=None, provenance='test', db=connection)
            shown = nodes()
            assert shown[person_task]['comment_attention']['comments'] == 1
            assert len(attention()) == 1
    finally:
        db.close()


def test_covered_comment_task_leaves_every_plan_count_unchanged(isolated_db):
    """Hiding happens in the plans projection, so the pane and chat list read the same counts."""
    from doceditor.person_comment_projection import annotate
    from plans import operator_chat_state

    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact = seed_document(db, 'owner')
        actor = PlanActor(thread_id=None, user_id='owner-token', project_id=None, kind='user')
        with write_transaction(db, scope='test_count_parity') as connection:
            plan = service.ensure_plan('thread', 'owner', db=connection)
            feedback = service.add_node(plan.id, kind='task', title='Agent feedback',
                                        acceptance_criteria='Done', actor_id=None,
                                        provenance='document.feedback_batch', db=connection)
            artifact_links.link(feedback.id, artifact, 'input', actor_id=None,
                                provenance='document.feedback_batch', db=connection)
            service.transition_node(feedback.id, 'active', actor_id='owner', db=connection)

            def counts():
                envelope = public.read_operator_thread('owner', actor=actor, db=connection)
                raw = dict(envelope['plans'][0]['progress'])
                annotated = annotate(connection, envelope, 'owner-token')['plans'][0]['progress']
                assert annotated == raw
                state = operator_chat_state.read_thread_states(['owner'], actor=actor, db=connection)
                return raw, state['states']['owner']['label_key']

            before = counts()
        root, _ = add_comment(db, artifact, author_kind='user', author_id=None)
        add_comment(db, artifact, author_id='owner', parent_id=root['id'])
        with write_transaction(db, scope='test_count_parity_after') as connection:
            person_task = person_comment_task.reconcile(connection, artifact)
            assert operator_display_state.covered_nodes([person_task], db=connection) == {person_task}
            assert counts() == before
    finally:
        db.close()


def test_other_agents_feedback_does_not_cover_owner_comment_task(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        seed_thread(db, 'reviewer')
        artifact = seed_document(db, 'owner')
        root, _ = add_comment(db, artifact, author_kind='user', author_id=None)
        add_comment(db, artifact, author_id='owner', parent_id=root['id'])
        with write_transaction(db, scope='test_other_feedback') as connection:
            person_task = person_comment_task.reconcile(connection, artifact)
            plan = service.ensure_plan('thread', 'reviewer', db=connection)
            node = service.add_node(plan.id, kind='task', title='Other feedback',
                                    acceptance_criteria='Done', actor_id=None,
                                    provenance='document.feedback_batch', db=connection)
            artifact_links.link(node.id, artifact, 'input', actor_id=None,
                                provenance='document.feedback_batch', db=connection)
            assert not operator_display_state.covered_nodes([person_task], db=connection)
    finally:
        db.close()


def test_feedback_parent_with_unlisted_settled_child_keeps_projected_progress(isolated_db):
    from doceditor.person_comment_projection import annotate
    from datetime import UTC, datetime
    from plans import mutations, pane_views

    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact = seed_document(db, 'owner')
        with write_transaction(db, scope='test_nested_coverage') as connection:
            plan = service.ensure_plan('thread', 'owner', db=connection)
            parent = service.add_node(plan.id, kind='phase', title='Feedback', acceptance_criteria='Done',
                                      provenance='document.feedback_batch', db=connection)
            artifact_links.link(parent.id, artifact, 'input', actor_id=None,
                                provenance='document.feedback_batch', db=connection)
            child = service.add_node(plan.id, kind='task', title='Completed child', acceptance_criteria='Done',
                                     parent_id=parent.id, db=connection)
            service.transition_node(child.id, 'active', db=connection)
            mutations.request_review(child.id, evidence='Done', actor_id='owner', db=connection)
            service.transition_node(child.id, 'accepted', verification_result='passed', verified_by='owner', db=connection)
            service.transition_node(parent.id, 'active', db=connection)
            ask = service.add_node(plan.id, kind='task', title='Review', acceptance_criteria='Done',
                                   assignee='user', provenance='test', db=connection)
            artifact_links.link(ask.id, artifact, 'input', actor_id=None, provenance='test', db=connection)
            actor = PlanActor(thread_id=None, user_id='owner-token', project_id=None, kind='user')
            pane_views.record(connection, user_id='owner-token', thread_id='owner',
                              seen_through=datetime.now(UTC).isoformat())
            envelope = public.read_operator_thread('owner', actor=actor, db=connection)
            projected = envelope['plans'][0]
            nodes = {node['id']: node for node in projected['nodes']}
            assert parent.id in nodes and child.id not in nodes
            assert nodes[parent.id]['branch_progress'] is not None
            assert not nodes[parent.id]['comment_task_covered']
            before = dict(projected['progress'])
            annotated = annotate(connection, envelope, 'owner-token')['plans'][0]
            assert annotated['progress'] == before
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
            assert comment_task not in nodes
            assert all(nodes[node_id]['requires_user_action'] for node_id in asks)
            assert connection.execute(
                f"SELECT {operator_display_state.ask_deferred_sql('n.id', plan_state_sql='p.state')} "
                'FROM plan_nodes n JOIN plans p ON p.id=n.plan_id WHERE n.id=?', (comment_task,),
            ).fetchone()[0] == 1
            deferred = operator_display_state.ask_deferred_sql('?', plan_state_sql='?')
            assert connection.execute(f'SELECT {deferred}', ('active', comment_task)).fetchone()[0] == 1
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


def test_review_bar_reads_the_comment_task_with_its_counts(isolated_db):
    from api.attention import _review_bar

    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact = seed_document(db, "owner", name="brief.md")
        root, _ = add_comment(db, artifact, author_kind="user", author_id=None)
        add_comment(db, artifact, author_id="owner", parent_id=root["id"])
        with write_transaction(db, scope="test_reconcile") as connection:
            task = person_comment_task.reconcile(connection, artifact)
    finally:
        db.close()
    step = _review_bar(artifact, "owner-token")["person_step"]
    # The bar and the plan menu read the same marker, so neither offers Done or Won't do for this task.
    assert step["node_id"] == task
    assert step["comment_attention"] == {
        "artifact_id": artifact, "comments": 1, "suggestions": 0, "system_task": True,
    }
