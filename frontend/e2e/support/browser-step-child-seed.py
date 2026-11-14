"""Make one thread the subagent of another, behind a delegated step in the parent's plan."""
import runpy
import sys

runpy.run_path('frontend/e2e/support/focus-outcomes.py')['assert_disposable_roots']()
from infra.database import get_db, write_transaction
from plans import mutations, public, service

parent_id, child_id, step_title = sys.argv[1:]
db = get_db()
try:
    plan = service.ensure_plan('thread', parent_id, db=db)
    # The parent spawned the child, as delegate_create_thread records it; like every
    # spawner writer, re-derive the child plan's parent in the same transaction.
    with write_transaction(db, scope='seed_browser_child') as connection:
        connection.execute('UPDATE threads SET spawned_by=? WHERE id=?', (parent_id, child_id))
        public.rehome(child_id, db=connection)
    step = service.add_node(plan.id, kind='task', title=step_title, acceptance_criteria=f'{step_title} is done',
                            spawn_kind='claude-code', actor_id=None, provenance='test', db=db)
    service.transition_node(step.id, 'active', db=db)
    mutations.attach_spawned_thread(step.id, child_id, db=db)
    print(f'seeded step={step.id} parent={parent_id} child={child_id}')
finally:
    db.close()
