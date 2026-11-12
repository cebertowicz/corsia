"""Comments for a person remain actionable until read, decided, or dismissed."""

import uuid

import pytest

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import comment_actions, feedback_comments, person_comment_attention
from doceditor.feedback_state import annotate_thread_attention, mark_thread_viewed
from infra.database import get_db, write_transaction
from plans import artifact_links, person_comment_task, public, service
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


def _person():
    return PlanActor(None, 'owner-token', None, kind='user')


def _waiting_document(db, owner='owner'):
    """A document whose owner replied to the person, so one comment waits on them."""
    artifact = seed_document(db, owner)
    root, _ = add_comment(db, artifact, author_kind='user', author_id=None)
    reply, _ = add_comment(db, artifact, author_id=owner, parent_id=root['id'])
    return artifact, root, reply


def _ask_node(connection, thread_id, title='Review'):
    plan = service.ensure_plan('thread', thread_id, db=connection)
    return service.add_node(plan.id, kind='task', title=title, acceptance_criteria='Done',
                            actor_id=None, provenance='test', db=connection)


def _raise(connection, node_id, artifact, thread_id):
    node = service._node_row(connection, node_id)
    public.mutate(node['plan_id'], 'block_node', {
        'node_id': node_id, 'expected_revision': node['revision'], 'owner_kind': 'user',
        'ask_artifact_id': artifact, 'reason_key': 'review', 'reason': 'Please review',
    }, actor=PlanActor(thread_id, 'owner-token', None), provenance='test',
        mutation_id=uuid.uuid4().hex, db=connection)
    return connection.execute(
        'SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL ORDER BY created_at DESC, id DESC',
        (node_id,),
    ).fetchone()['id']


def _end(connection, node_id, hold_id, thread_id):
    node = service._node_row(connection, node_id)
    public.mutate(node['plan_id'], 'resolve_blocker', {
        'node_id': node_id, 'expected_revision': node['revision'], 'hold_id': hold_id,
    }, actor=PlanActor(thread_id, 'owner-token', None), provenance='test',
        mutation_id=uuid.uuid4().hex, db=connection)


def _tasks(db, artifact):
    return [tuple(row) for row in db.execute(
        'SELECT n.id, n.state FROM plan_node_artifact_links l JOIN plan_nodes n ON n.id=l.node_id'
        ' WHERE l.provenance=? AND l.artifact_id=? ORDER BY n.created_at, n.id',
        (person_comment_task.PROVENANCE, artifact),
    )]


def _reason(db, node_id):
    return db.execute(
        "SELECT json_extract(payload_json,'$.reason') FROM plan_node_events"
        " WHERE node_id=? AND json_extract(payload_json,'$.reason') IS NOT NULL ORDER BY rowid DESC LIMIT 1",
        (node_id,),
    ).fetchone()[0]


def _open_tasks(db, artifact):
    return [node_id for node_id, state in _tasks(db, artifact) if state not in public.TERMINAL_STATES]


def test_an_open_comment_task_alone_never_covers(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact, _, _ = _waiting_document(db)
        assert len(_open_tasks(db, artifact)) == 1
        assert not person_comment_task.is_covered(db, artifact, 'owner-token')
    finally:
        db.close()


def test_feedback_task_never_covers(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact, _, _ = _waiting_document(db)
        with write_transaction(db, scope='test_feedback_task') as connection:
            plan = service.ensure_plan('thread', 'owner', db=connection)
            feedback = service.add_node(plan.id, kind='task', title='Agent feedback', acceptance_criteria='Done',
                                        actor_id=None, provenance='document.feedback_batch', db=connection)
            artifact_links.link(feedback.id, artifact, 'input', actor_id=None,
                                provenance='document.feedback_batch', db=connection)
            assert not person_comment_task.is_covered(connection, artifact, 'owner-token')
            assert person_comment_task.reconcile(connection, artifact) in _open_tasks(connection, artifact)
    finally:
        db.close()


def test_an_ask_closes_the_task_and_its_end_brings_it_back(isolated_db):
    asker = 'owner'
    db = get_db()
    try:
        seed_thread(db, 'owner')
        seed_thread(db, 'other')
        artifact, root, _ = _waiting_document(db)
        add_comment(db, artifact, author_id='owner', parent_id=root['id'], replacement='better words')
        [task] = _open_tasks(db, artifact)
        before = person_comment_attention.counts(db, artifact, 'owner-token')
        with write_transaction(db, scope='test_ask_opens') as connection:
            ask = _ask_node(connection, asker)
            hold = _raise(connection, ask.id, artifact, asker)
        # Closed, not accepted: nothing was read or decided, and the comments are as they were.
        assert _tasks(db, artifact) == [(task, 'closed')]
        assert _reason(db, task) == person_comment_task.COVERED
        assert person_comment_attention.counts(db, artifact, 'owner-token') == before == (1, 1)
        assert db.execute('SELECT COUNT(*) FROM doc_comment_thread_views').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM doc_comments WHERE decision IS NOT NULL').fetchone()[0] == 0
        with write_transaction(db, scope='test_later') as connection:
            # The person's Later keeps the request open, so it still carries the comments.
            assert public.defer_asks(connection, artifact) == 1
            assert person_comment_task.is_covered(connection, artifact, 'owner-token')
        assert _tasks(db, artifact) == [(task, 'closed')]
        with write_transaction(db, scope='test_ask_ends') as connection:
            _end(connection, ask.id, hold, asker)
        [again] = _open_tasks(db, artifact)
        assert again != task
    finally:
        db.close()


def test_an_ask_ending_with_nothing_waiting_brings_nothing_back(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact, root, reply = _waiting_document(db)
        with write_transaction(db, scope='test_ask_opens') as connection:
            ask = _ask_node(connection, 'owner')
            hold = _raise(connection, ask.id, artifact, 'owner')
        with write_transaction(db, scope='test_read') as connection:
            mark_thread_viewed(connection, 'owner-token', artifact, root['id'], reply['id'])
        with write_transaction(db, scope='test_ask_ends') as connection:
            _end(connection, ask.id, hold, 'owner')
        assert _open_tasks(db, artifact) == []
    finally:
        db.close()


def test_a_re_raise_in_one_transaction_adds_no_task(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact, _, _ = _waiting_document(db)
        with write_transaction(db, scope='test_ask_node') as connection:
            ask = _ask_node(connection, 'owner')
        nodes = db.execute('SELECT COUNT(*) FROM plan_nodes').fetchone()[0]
        with write_transaction(db, scope='test_re_raise') as connection:
            hold = _raise(connection, ask.id, artifact, 'owner')
            _end(connection, ask.id, hold, 'owner')
            _raise(connection, ask.id, artifact, 'owner')
        assert db.execute('SELECT COUNT(*) FROM plan_nodes').fetchone()[0] == nodes
        assert _open_tasks(db, artifact) == []
    finally:
        db.close()


def test_a_paused_owner_plan_keeps_the_task_until_it_resumes(isolated_db):
    from plans import lifecycle

    db = get_db()
    try:
        seed_thread(db, 'owner')
        seed_thread(db, 'other')
        artifact, _, _ = _waiting_document(db)
        [task] = _open_tasks(db, artifact)
        plan_id = service._node_row(db, task)['plan_id']
        with write_transaction(db, scope='test_pause') as connection:
            lifecycle.transition(plan_id, 'paused', expected_revision=service._plan_row(connection, plan_id)['revision'],
                                 actor_id=None, provenance='test', db=connection)
        with write_transaction(db, scope='test_ask_opens') as connection:
            ask = _ask_node(connection, 'owner')
            _raise(connection, ask.id, artifact, 'owner')
        assert _tasks(db, artifact) == [(task, 'pending')]
        with write_transaction(db, scope='test_resume') as connection:
            lifecycle.transition(plan_id, 'active', expected_revision=service._plan_row(connection, plan_id)['revision'],
                                 actor_id=None, provenance='test', db=connection)
        assert _tasks(db, artifact) == [(task, 'closed')]
    finally:
        db.close()


def test_a_missing_document_closes_its_task(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact, _, _ = _waiting_document(db)
        [task] = _open_tasks(db, artifact)
        with write_transaction(db, scope='test_orphan') as connection:
            connection.execute('PRAGMA defer_foreign_keys=ON')
            connection.execute('DELETE FROM artifact_registrations WHERE artifact_id=?', (artifact,))
            assert person_comment_task.reconcile(connection, artifact) is None
        assert _tasks(db, artifact) == [(task, 'closed')]
        assert _reason(db, task) == 'The document is gone.'
    finally:
        db.close()


def test_two_asks_put_the_counts_on_the_earliest_only(isolated_db):
    from doceditor.person_comment_projection import annotate

    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact, _, _ = _waiting_document(db)
        with write_transaction(db, scope='test_two_asks') as connection:
            first = _ask_node(connection, 'owner', 'First ask')
            second = _ask_node(connection, 'owner', 'Second ask')
            hold = _raise(connection, first.id, artifact, 'owner')
            # One open request per document is the product rule (one_open_ask); a second
            # open one is seeded here, raised earlier, to prove which of them carries the counts.
            row = dict(connection.execute('SELECT * FROM plan_node_holds WHERE id=?', (hold,)).fetchone())
            row.update(id='0-earlier-hold', node_id=second.id, created_at='2000-01-01T00:00:00+00:00')
            connection.execute(
                f"INSERT INTO plan_node_holds ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                tuple(row.values()),
            )
            artifact_links.link(second.id, artifact, 'attention', actor_id=None, provenance='test', db=connection)
        assert person_comment_task.covering_asks(db, [artifact], 'owner-token')[artifact] == [second.id, first.id]
        envelope = public.read_operator_thread('owner', actor=_person(), db=db)
        nodes = {node['id']: node for node in annotate(db, envelope, 'owner-token')['plans'][0]['nodes']}
        assert nodes[second.id]['comment_attention'] == {
            'artifact_id': artifact, 'comments': 1, 'suggestions': 0, 'system_task': False,
        }
        assert 'comment_attention' not in nodes[first.id]
    finally:
        db.close()


def test_the_task_coming_and_going_does_not_reenter_asks_changed(isolated_db, monkeypatch):
    from plans import user_ask

    calls = []
    monkeypatch.setattr(user_ask, 'asks_changed', lambda db, ids: calls.append(list(ids)))
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact, root, reply = _waiting_document(db)
        assert len(_open_tasks(db, artifact)) == 1
        with write_transaction(db, scope='test_read') as connection:
            mark_thread_viewed(connection, 'owner-token', artifact, root['id'], reply['id'])
            person_comment_task.reconcile(connection, artifact)
        assert _open_tasks(db, artifact) == []
        assert calls == []
    finally:
        db.close()


def test_comment_task_is_marked_whatever_its_count(isolated_db):
    from doceditor.person_comment_projection import annotate

    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact, root, reply = _waiting_document(db)
        [task] = _open_tasks(db, artifact)
        plan_id = service._node_row(db, task)['plan_id']
        from plans import lifecycle

        with write_transaction(db, scope='test_pause') as connection:
            lifecycle.transition(plan_id, 'paused', expected_revision=service._plan_row(connection, plan_id)['revision'],
                                 actor_id=None, provenance='test', db=connection)
            # A paused plan keeps the task open when its count reaches zero (S26).
            mark_thread_viewed(connection, 'owner-token', artifact, root['id'], reply['id'])
            person_comment_task.reconcile(connection, artifact)
        envelope = public.read_operator_thread('owner', actor=_person(), db=db)
        nodes = {node['id']: node for node in annotate(db, envelope, 'owner-token')['plans'][0]['nodes']}
        assert nodes[task]['comment_attention'] == {
            'artifact_id': artifact, 'comments': 0, 'suggestions': 0, 'system_task': True,
        }
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
                                    actor_id=None, provenance=person_comment_task.PROVENANCE, db=db)
            artifact_links.link(node.id, artifact, 'input', actor_id=None,
                                provenance=person_comment_task.PROVENANCE, db=db)
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


def _cards_and_receipts(db, node_id):
    cards = db.execute('SELECT COUNT(*) FROM thread_updates WHERE node_id=?', (node_id,)).fetchone()[0]
    receipts = db.execute(
        "SELECT COUNT(*) FROM messages WHERE json_extract(metadata,'$.node_id')=?", (node_id,),
    ).fetchone()[0]
    return cards, receipts


def test_a_task_created_unannounced_leaves_no_card_or_receipt(isolated_db):
    """The comment task's create, close and settle are bookkeeping; a person step added the usual way is news."""
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact, root, reply = _waiting_document(db)
        [task] = _open_tasks(db, artifact)
        assert _cards_and_receipts(db, task) == (0, 0)
        with write_transaction(db, scope='test_ask_opens') as connection:
            ask = _ask_node(connection, 'owner')
            hold = _raise(connection, ask.id, artifact, 'owner')
        assert _tasks(db, artifact) == [(task, 'closed')]
        assert _cards_and_receipts(db, task) == (0, 0)
        with write_transaction(db, scope='test_ask_ends') as connection:
            _end(connection, ask.id, hold, 'owner')
        [again] = _open_tasks(db, artifact)
        with write_transaction(db, scope='test_read') as connection:
            mark_thread_viewed(connection, 'owner-token', artifact, root['id'], reply['id'])
            person_comment_task.reconcile(connection, artifact)
        assert _tasks(db, artifact)[-1] == (again, 'accepted')
        assert _cards_and_receipts(db, again) == (0, 0)
        with write_transaction(db, scope='test_announced') as connection:
            plan = service.ensure_plan('thread', 'owner', db=connection)
            step = service.add_node(plan.id, kind='task', title='Sign in', acceptance_criteria='Done',
                                    assignee='user', actor_id=None, provenance='test', db=connection)
        assert _cards_and_receipts(db, step.id)[0] == 1
    finally:
        db.close()


def test_a_covered_task_spared_by_its_own_hold_closes_when_the_hold_clears(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact, _, _ = _waiting_document(db)
        [task] = _open_tasks(db, artifact)
        node = service._node_row(db, task)
        with write_transaction(db, scope='test_task_hold') as connection:
            public.mutate(node['plan_id'], 'pause_node', {
                'node_id': task, 'expected_revision': node['revision'], 'reason': 'Not now',
            }, actor=_person(), provenance='test', mutation_id=uuid.uuid4().hex, db=connection)
        task_hold = db.execute(
            'SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL', (task,),
        ).fetchone()['id']
        with write_transaction(db, scope='test_ask_opens') as connection:
            ask = _ask_node(connection, 'owner')
            _raise(connection, ask.id, artifact, 'owner')
        # Its own hold spares it from the close while the ask covers it.
        assert _open_tasks(db, artifact) == [task]
        with write_transaction(db, scope='test_task_hold_clears') as connection:
            node = service._node_row(connection, task)
            public.mutate(node['plan_id'], 'resume_node', {
                'node_id': task, 'expected_revision': node['revision'], 'hold_id': task_hold,
            }, actor=_person(), provenance='test', mutation_id=uuid.uuid4().hex, db=connection)
        assert _tasks(db, artifact) == [(task, 'closed')]
        assert _reason(db, task) == person_comment_task.COVERED
    finally:
        db.close()


def test_after_commit_reconcile_raises_every_failure_and_tries_every_document(isolated_db, monkeypatch, caplog):
    import logging

    from plans import user_ask

    db = get_db()
    try:
        seed_thread(db, 'owner')
        broken, _, _ = _waiting_document(db)
        fine = seed_document(db, 'owner', name='fine.md')
        tried = []
        real = person_comment_task.reconcile

        def reconcile(connection, artifact_id):
            tried.append(artifact_id)
            if artifact_id == broken:
                raise RuntimeError('reconcile broke')
            return real(connection, artifact_id)

        monkeypatch.setattr(person_comment_task, 'reconcile', reconcile)
        with pytest.raises(ExceptionGroup) as caught:
            user_ask._reconcile_comment_tasks(db, [broken, fine])
        assert tried == [broken, fine]
        [error] = caught.value.exceptions
        assert isinstance(error, RuntimeError)
        assert f'comment task reconcile for document {broken}' in error.__notes__
        # Through a commit, the group reaches the after-commit failure log; the write itself stands.
        tried.clear()
        with caplog.at_level(logging.ERROR, logger='infra.write_effects'):
            with write_transaction(db, scope='test_committed') as connection:
                user_ask.asks_changed(connection, [broken, fine])
        assert tried == [broken, fine]
        assert any(record.message == 'after-commit effect failed' and isinstance(record.exc_info[1], ExceptionGroup)
                   for record in caplog.records)
    finally:
        db.close()


def test_in_place_reconcile_failure_fails_the_callers_write(isolated_db, monkeypatch):
    from plans import user_ask

    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact, _, _ = _waiting_document(db)

        def reconcile(connection, artifact_id):
            raise RuntimeError('reconcile broke')

        monkeypatch.setattr(person_comment_task, 'reconcile', reconcile)
        db.execute('BEGIN IMMEDIATE')
        try:
            # A transaction nobody here owns: nothing would report its commit, so it reconciles in place.
            with pytest.raises(RuntimeError, match='reconcile broke'):
                user_ask.asks_changed(db, [artifact])
        finally:
            db.rollback()
    finally:
        db.close()


def test_dismiss_reads_the_unread_items_once_whatever_the_thread_count(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact = seed_document(db, 'owner')
        for _ in range(5):
            root, _ = add_comment(db, artifact, author_kind='user', author_id=None)
            add_comment(db, artifact, author_id='owner', parent_id=root['id'])
            add_comment(db, artifact, author_id='owner', parent_id=root['id'])
        assert person_comment_attention.counts(db, artifact, 'owner-token') == (10, 0)
        queries = []
        with write_transaction(db, scope='test_dismiss') as connection:
            connection.set_trace_callback(queries.append)
            person_comment_attention.dismiss(connection, artifact, 'owner-token')
            connection.set_trace_callback(None)
        reads = [query for query in queries if query.lstrip().upper().startswith('SELECT')]
        stamps = [query for query in queries if 'INSERT INTO doc_comment_thread_views' in query]
        assert len(reads) == 1 and len(stamps) == 5
        assert person_comment_attention.counts(db, artifact, 'owner-token') == (0, 0)
    finally:
        db.set_trace_callback(None)
        db.close()


def test_counts_and_unread_rows_share_one_watermark_including_ties(isolated_db):
    """Both readers use ``past_watermark_sql``: never viewed, a tie on time broken by id, and fully read."""
    from doceditor.feedback_state import _unread_items, stamp_thread_view

    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact = seed_document(db, 'owner')
        root, _ = add_comment(db, artifact, author_kind='user', author_id=None)
        first, _ = add_comment(db, artifact, author_id='owner', parent_id=root['id'])
        second, _ = add_comment(db, artifact, author_id='owner', parent_id=root['id'])
        low, high = sorted([first['id'], second['id']])
        stamp = '2026-10-01T00:00:00+00:00'
        with write_transaction(db, scope='test_tie') as connection:
            connection.execute('UPDATE doc_comments SET created_at=? WHERE id IN (?,?)', (stamp, low, high))

        def unread():
            return (person_comment_attention.counts(db, artifact, 'owner-token'),
                    sorted(item for _, item in _unread_items(db, 'owner-token', artifact) if item != root['id']))

        assert unread() == ((2, 0), [low, high])
        with write_transaction(db, scope='test_stamp_low') as connection:
            stamp_thread_view(connection, 'owner-token', root['id'], stamp, low)
        assert unread() == ((1, 0), [high])
        with write_transaction(db, scope='test_stamp_back') as connection:
            # Forward only: an earlier item never moves the watermark back.
            stamp_thread_view(connection, 'owner-token', root['id'], '2026-01-01T00:00:00+00:00', low)
        assert unread() == ((1, 0), [high])
        with write_transaction(db, scope='test_stamp_high') as connection:
            stamp_thread_view(connection, 'owner-token', root['id'], stamp, high)
        assert unread() == ((0, 0), [])
    finally:
        db.close()
