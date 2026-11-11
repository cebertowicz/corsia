"""Seed a person ask and directed feedback in the disposable E2E instance."""
import json
import runpy
import sys

runpy.run_path('frontend/e2e/support/focus-outcomes.py')['assert_disposable_roots']()
from doc_feedback_helpers import add_comment
from doceditor import comments, feedback_batches
from infra.database import get_db, write_transaction
from plans import artifact_links, person_comment_task, service

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
        reply_ids = []
        for _ in range(2):
            root, _ = add_comment(db, artifact_id, author_kind='user', author_id=None)
            reply, _ = add_comment(db, artifact_id, author_id=thread_id, parent_id=root['id'])
            reply_ids.append([root['id'], reply['id']])
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
    else:
        raise ValueError(mode)
finally:
    db.close()
