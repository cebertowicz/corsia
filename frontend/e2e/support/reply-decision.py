"""Seed a raised decision in e2e-local's disposable database for reply-chip frames."""
import runpy
import sys
from pathlib import Path

assert_disposable_roots = runpy.run_path(str(Path(__file__).with_name("focus-outcomes.py")))["assert_disposable_roots"]
from infra.database import get_db
from plans import decisions, service
from plans.boundary import PlanActor

assert_disposable_roots()
db = get_db()
thread_id = sys.argv[1]
thread = db.execute("SELECT user_id, project_id, spawned_by FROM threads WHERE id=?", (thread_id,)).fetchone()
assert thread is not None and thread["spawned_by"] is None
actor = PlanActor(thread_id, thread["user_id"], thread["project_id"])
plan = service.ensure_plan("thread", thread_id, db=db)
node = service.add_node(plan.id, kind="task", title="Launch decision", acceptance_criteria="Choose an approach", db=db)
db.commit()
decision = decisions.draft(
    plan.id, source_node_id=node.id, mutation_id="reply-frame-draft", actor=actor,
    provenance="e2e fixture", db=db, title="Choose the launch approach",
    context="The launch plan needs a decision.", tradeoff="Choose the right timing.",
    recommendation="A", rationale="It keeps the launch on track.",
    options=[{"id": "A", "label": "Proceed", "detail": "Keep the current date."},
             {"id": "B", "label": "Wait", "detail": "Allow more time."}], other_enabled=True,
).result["decision"]
decision = decisions.parent_review(decision["id"], expected_revision=decision["revision"],
    mutation_id="reply-frame-review", actor=actor, provenance="e2e fixture", db=db).result["decision"]
decisions.raise_to_user(decision["id"], expected_revision=decision["revision"],
    mutation_id="reply-frame-raise", actor=actor, provenance="e2e fixture", db=db)
db.commit()
db.close()
