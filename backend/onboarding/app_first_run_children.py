"""Seed the actionable question under the first-run app connections phase."""


def seed_ask(plan_id, *, db):
    from plans import public

    phase = db.execute(
        "SELECT id FROM plan_nodes WHERE plan_id=? AND workflow_ref='first-run-app' "
        "AND workflow_step='3:common' AND archived_at IS NULL", (plan_id,),
    ).fetchone()
    if phase is not None:
        public.add_node(
            plan_id, kind="task", parent_id=phase["id"], title="Name the apps you work in",
            acceptance_criteria="you named the apps where you work, or chose not now",
            workflow_ref="first-run-app", workflow_step="3:ask", difficulty="routine",
            provenance="onboarding.app_first_run", db=db,
        )
