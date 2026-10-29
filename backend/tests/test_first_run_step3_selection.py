"""First-run selection keeps deferred app work behind the hand-off."""

import pytest

from test_app_first_run_seed import PASSWORD, _conductor_id, app_seed_facts, fresh_install


@pytest.mark.parametrize("finish", ["mark_done", "close_node"])
@pytest.mark.parametrize("web_ready", [True, False])
def test_step3_ask_and_deferred_app(fresh_install, app_seed_facts, finish, web_ready):
    from infra import accounts
    from infra.database import write_transaction
    from onboarding.app_first_run import cache_completed_facts
    from plans import public
    from plans.current import read_current_status

    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    conductor_id = _conductor_id()
    actor = public.PlanActor(conductor_id, "newcomer", None)
    facts = {"agents_found": [], "connected_app_count": 0, "web_search_ready": web_ready}
    with write_transaction(None, scope="step3_selection") as db:
        cache_completed_facts(conductor_id, "newcomer", facts, db=db)
        listing = public.read_current_plan(actor=actor, include_folded=True, db=db)
        plan_id = listing["plan"]["id"]
        phase = next(n for n in listing["nodes"] if n["title"] == "Where your work lives")
        handoff = next(n for n in listing["nodes"] if n["title"] == "Hand over to your agents")
        ask = next(n for n in listing["nodes"] if n["title"] == "Name the apps you work in")
        assert ask["parent_id"] == phase["id"]
        [seed] = db.execute(
            "SELECT workflow_step, acceptance_criteria FROM plan_nodes WHERE id=?", (ask["id"],),
        ).fetchall()
        assert tuple(seed) == ("3:ask", "you named the apps where you work, or chose not now")
        assert read_current_status(actor=actor, db=db)["next_step"]["id"] == ask["id"]
        cache_completed_facts(conductor_id, "newcomer", facts, db=db)
        listing = public.read_current_plan(actor=actor, include_folded=True, db=db)
        assert sum(n["id"] == ask["id"] for n in listing["nodes"]) == 1
        app = public.add_node(
            plan_id, kind="task", title="Connect Slack", parent_id=phase["id"],
            acceptance_criteria="Connected or skipped", provenance="test", db=db,
        )
        public.mutate(
            plan_id, "add_dependency",
            {"node_id": app.id, "depends_on_node_id": handoff["id"], "expected_revision": app.revision},
            actor=actor, provenance="test", mutation_id="dependency", db=db,
        )
        public.mutate(
            plan_id, "start", {"node_id": ask["id"], "expected_revision": ask["revision"]},
            actor=actor, provenance="test", mutation_id="start-ask", db=db,
        )
        public.mutate(
            plan_id, "mark_done",
            {"node_id": ask["id"], "expected_revision": public.get_node(ask["id"], db=db).revision,
             "evidence": "The person named Slack."},
            actor=actor, provenance="test", mutation_id="done-ask", db=db,
        )
        expected_next = "About you" if web_ready else "Web search"
        assert read_current_status(actor=actor, db=db)["next_step"]["title"] == expected_next
        if finish == "mark_done":
            public.mutate(
                plan_id, "start",
                {"node_id": handoff["id"], "expected_revision": public.get_node(handoff["id"], db=db).revision},
                actor=actor, provenance="test", mutation_id="start-handoff", db=db,
            )
            outcome = {"evidence": "Hand-off complete"}
        else:
            outcome = {"reason": "Not now"}
        public.mutate(
            plan_id, finish,
            {"node_id": handoff["id"], "expected_revision": public.get_node(handoff["id"], db=db).revision,
             **outcome},
            actor=actor, provenance="test", mutation_id="settle-handoff", db=db,
        )
        detail = read_current_status(actor=actor, node_id=app.id, db=db)
        assert detail["execution"]["condition"] == "ready"
        assert read_current_status(actor=actor, db=db)["next_step"]["title"] == "Connect Slack"
