"""Seed a person ask and directed feedback in the disposable E2E instance."""
import json
import runpy
import sys

runpy.run_path('frontend/e2e/support/focus-outcomes.py')['assert_disposable_roots']()
from doc_feedback_helpers import add_comment
from doceditor import comments, feedback_batches
from infra.database import get_db, write_transaction
from plans import artifact_links, mutations, person_comment_task, service

mode, artifact_id, thread_id = sys.argv[1:]
db = get_db()
try:
    if mode == 'ask':
        with write_transaction(db, scope='seed_ask') as connection:
            plan = service.ensure_plan('thread', thread_id, db=connection)
            node = service.add_node(plan.id, kind='task', title='Review launch plan',
                                    acceptance_criteria='Review the document', assignee='user',
                                    actor_id=None, provenance='test', db=connection)
            artifact_links.link(node.id, artifact_id, 'input', actor_id=None,
                                provenance='test', db=connection)
    elif mode == 'feedback':
        # The person's comments are sent as the Send button sends them, and the agent finishes the
        # feedback task they make before replying; the delivery sweep never races the capture.
        reply_ids = []
        feedback_nodes = []
        for _ in range(2):
            root, sent = add_comment(db, artifact_id, author_kind='user', author_id=None)
            [batch_id] = sent['batch_ids']
            feedback_batches.close_batch(db, batch_id, 'send')
            node_id = db.execute('SELECT delivered_node_id FROM doc_feedback_batches WHERE id=?',
                                 (batch_id,)).fetchone()[0]
            assert node_id, batch_id
            if node_id not in feedback_nodes:
                feedback_nodes.append(node_id)
            reply, _ = add_comment(db, artifact_id, author_id=thread_id, parent_id=root['id'])
            reply_ids.append([root['id'], reply['id']])
        with write_transaction(db, scope='finish_feedback') as connection:
            for node_id in feedback_nodes:
                mutations.request_review(node_id, evidence='Answered on the document', owner_thread_id=thread_id,
                                         actor_id=thread_id, db=connection)
                service.transition_node(node_id, 'accepted', actor_id=thread_id, verification_result='passed',
                                        verified_by=thread_id, db=connection)
        user_id = db.execute('SELECT user_id FROM threads WHERE id=?', (thread_id,)).fetchone()[0]
        suggestion_ids = []
        with write_transaction(db, scope='seed_suggestions') as connection:
            for index in range(2):
                row, created = comments.create_with_status(
                    artifact_id, body='Please review', author='Reviewer', author_kind='agent',
                    author_thread_id=thread_id, anchor_text='Launch plan', from_off=0, to_off=11,
                    replacement=f'Revised plan {index}', tagged_user_id=user_id, db=connection)
                feedback_batches.record_comment(connection, row, created=created,
                                               delivery_policy=feedback_batches.IMMEDIATE)
                suggestion_ids.append(row['id'])
            person_comment_task.reconcile(connection, artifact_id)
        print('FIXTURE_RESULT=' + json.dumps({'replies': reply_ids, 'suggestions': suggestion_ids}))
    elif mode == 'long':
        # From '1 comment, 1 suggestion' to the long count '12 comments, 3 suggestions': 11 more tagged agent
        # comments and 2 more suggestions, each delivered to the person as the recipients writer delivers them.
        user_id = db.execute('SELECT user_id FROM threads WHERE id=?', (thread_id,)).fetchone()[0]
        with write_transaction(db, scope='seed_long_count') as connection:
            for index in range(13):
                row, created = comments.create_with_status(
                    artifact_id, body=f'Note {index}', author='Reviewer', author_kind='agent',
                    author_thread_id=thread_id, anchor_text='Launch plan', from_off=0, to_off=11,
                    replacement=f'Long plan {index}' if index >= 11 else None, tagged_user_id=user_id, db=connection)
                feedback_batches.record_comment(connection, row, created=created,
                                               delivery_policy=feedback_batches.IMMEDIATE)
            person_comment_task.reconcile(connection, artifact_id)
    else:
        raise ValueError(mode)
finally:
    db.close()
