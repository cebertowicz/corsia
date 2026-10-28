"""The plan input-class raise sites, and the route that reaches each one.

This is the table, not the assertions: `test_plan_input_class_sites.py` drives
every entry through the real mutation boundary, and
`test_plan_input_class_gate.py` reads the same table to prove no raise site
exists without a case. Both import from here so the data has one home.

Where two sites carry the same message the cases take deliberately different
routes — `edit` with no editable key reaches `boundary_fields.edit_fields`,
`edit` with an explicit ``title: None`` passes that and reaches
`mutations.edit_node` — so neither is protected by the other.
"""

from __future__ import annotations

from functools import partial

from plans import (
    boundary_dispatch, delegate_attach, holds, intent_words, service, status_cursor, system_user_steps,
)
from plans.boundary import PlanActor, mutate
from plans.current import read_current_plan, read_current_status
from tests.plan_ask_helpers import document_ask
from tests.plan_scene import (  # noqa: F401 - shared plan fixtures
    ACTOR,
    NODE,
    OWN,
    REV,
    _actor,
    _artifact,
    _mid,
    _mutate,
    _operation,
    _remove_inherited_dependency,
    _resolved,
    _revision,
    db,
    raise_decision,
    scene,
)
from tests.plan_writer_authorizer import raw_plan_seed


def _workflow_fields_before_migration(db, scene):
    """A mid-ladder database cannot persist the v51 workflow columns yet."""
    db.execute("ALTER TABLE plan_nodes DROP COLUMN workflow_ref")
    db.commit()
    return partial(
        service.add_node, scene.plan_id, kind="task", title="T",
        acceptance_criteria="DOD", workflow_ref="sample", db=db,
    )


def _negative_order_key(db, scene):
    """`add` and `insert` publish no `order_key`, so `_create_node`'s guard is
    unreachable from the boundary. The public service call is its only caller."""
    return partial(
        service.add_node, scene.plan_id, kind="task", title="T",
        acceptance_criteria="DOD", order_key=-1, db=db,
    )


def _size_before_migration(db, scene):
    """A pre-v51 plan-node table cannot persist a declared size yet."""
    for column in ("size", "workflow_ref", "workflow_version", "workflow_step", "difficulty"):
        db.execute(f"ALTER TABLE plan_nodes DROP COLUMN {column}")
    db.commit()
    return partial(
        service.add_node, scene.plan_id, kind="task", title="T",
        acceptance_criteria="DOD", size="M", db=db,
    )


def _intent_before_migration(db, scene):
    """A pre-v138 plan-node table cannot persist a task's intent yet."""
    db.execute("DROP INDEX idx_plan_nodes_intent_node")
    for column in ("intent_node_id", "intent"):
        db.execute(f"ALTER TABLE plan_nodes DROP COLUMN {column}")
    db.commit()
    return partial(
        service.add_node, scene.plan_id, kind="task", title="T",
        acceptance_criteria="DOD", intent="Keep the request", db=db,
    )


def _agent_plan_change(operation: str):
    """A whole-plan pause or resume by the owning thread, in no user Turn."""

    def build(db, scene):
        revision = service._plan_row(db, scene.plan_id)["revision"]
        return partial(
            _mutate, db, scene.plan_id, operation, {"expected_revision": revision}, _mid(),
        )

    return build


_USER = PlanActor(None, "user-1", None, kind="user")


def _foreign_add(adder: str | None):
    """An `add` into a plan the caller does not own.

    With no adder, the owner names a plan id that does not exist; with one, a
    thread that is not a conductor adds to `root`'s plan.
    """

    def build(db, scene):
        body = {"kind": "task", "title": "T", "acceptance_criteria": "DOD"}
        plan_id = scene.plan_id if adder else "no-such-plan"
        actor = _actor(adder) if adder else ACTOR
        return partial(
            mutate, plan_id, "add", body, actor=actor, provenance="test",
            mutation_id=_mid(), db=db,
        )

    return build


def _node_pause_then_resume(placed_by: PlanActor, resumed_by: PlanActor):
    """One actor pauses the pending task; the other tries to resume it.

    Placed through the runtime writer: an agent can no longer pause, but a
    pause one placed before that stays its own to resume.
    """

    def build(db, scene):
        holds.pause(
            scene.pending, "Hold", expected_revision=_revision(db, scene.pending),
            actor_id=placed_by.audit_id, provenance="test", db=db,
        )
        db.commit()
        hold = holds.active_holds(scene.pending, db=db)[0]["id"]
        resume = {
            "node_id": scene.pending, "hold_id": hold,
            "expected_revision": _revision(db, scene.pending),
        }
        return partial(
            mutate, scene.plan_id, "resume_node", resume, actor=resumed_by,
            provenance="test", mutation_id=_mid(), db=db,
        )

    return build


def _blank_close_reason(db, scene):
    """`boundary_fields.text` refuses a blank reason first, so the writer's own
    guard is reachable only by calling it directly -- which any caller may."""
    from plans import closure

    return partial(
        closure.close_node, scene.pending, reason="   ", provenance="test", db=db,
    )


def _dispatch_fallthrough(db, scene):
    """`validate_operation_params` rejects an unknown operation first, so the
    dispatch table's own fallthrough is only reachable directly. It is the guard
    that catches an operation added to the vocabulary but not to the table."""
    return partial(
        boundary_dispatch.dispatch,
        scene.plan_id, "teleport", {}, ACTOR, "test", db,
    )


def _blank_detach_reason(db, scene):
    """Detach is not a published operation; its callers always pass a reason,
    so the writer's own guard is reachable only by calling it directly."""
    from plans import delegation

    return partial(delegation.detach_spawned_thread, scene.own, reason="   ", db=db)


def _attach_over_another_worker(db, scene):
    """A node that already names a worker refuses a different one, naming
    replace. The refusal comes before either thread is validated."""
    db.execute(
        "INSERT OR IGNORE INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('worker-a','user-1','test/model','t','t')"
    )
    with raw_plan_seed(db):
        db.execute(
            "UPDATE plan_nodes SET spawned_thread_id='worker-a' WHERE id=?", (scene.own,),
        )
    db.commit()
    return partial(
        _mutate, db, scene.plan_id, "attach_spawned_thread",
        {
            "node_id": scene.own, "expected_revision": _revision(db, scene.own),
            "spawned_thread_id": "worker-b",
        },
        _mid(),
    )


def _delegate_to(node: str | None = None, *, finish: bool = False, worker: bool = False):
    """delegate_task naming a task it cannot hand over, refused before any spawn."""

    def build(db, scene):
        if finish:
            service.transition_node(scene.own, "failed", db=db)
        if worker:
            db.execute(
                "INSERT OR IGNORE INTO threads (id,user_id,model,created_at,updated_at) "
                "VALUES ('worker-a','user-1','test/model','t','t')"
            )
            with raw_plan_seed(db):
                db.execute(
                    "UPDATE plan_nodes SET spawned_thread_id='worker-a' WHERE id=?",
                    (scene.own,),
                )
        db.commit()
        return partial(delegate_attach.choose, "root", node or scene.own, db=db)

    return build


def _reuse_worker(case: str):
    """delegate_send naming a task a worker cannot take over, refused before sending."""

    def build(db, scene):
        spawned_by = None if case == "not_mine" else "root"
        # The worker shares its starter's project, as a real spawn does; the
        # attach under "holds_open" refuses a cross-project worker first.
        db.execute(
            "INSERT OR IGNORE INTO threads "
            "(id,user_id,model,spawned_by,project_id,created_at,updated_at) "
            "SELECT 'worker-r','user-1','test/model',?,project_id,'t','t' "
            "FROM threads WHERE id='root'",
            (spawned_by,),
        )
        if case == "holds_open":
            from plans.delegation import attach_spawned_thread

            other = service.add_node(
                scene.plan_id, kind="task", title="Its job",
                acceptance_criteria="done", db=db,
            )
            attach_spawned_thread(other.id, "worker-r", actor_id="root", db=db)
        db.commit()
        return partial(
            delegate_attach.choose_for_send, "root", "worker-r", scene.own, db=db,
        )

    return build


def _worker_plan(case: str):
    """A live delegation from root: root reaching into it (G7), or its worker assigning the user (G11)."""

    def build(db, scene):
        from plans.delegation import attach_spawned_thread

        db.execute(
            "INSERT OR IGNORE INTO threads "
            "(id,user_id,model,spawned_by,project_id,created_at,updated_at) "
            "SELECT 'worker-d','user-1','test/model','root',project_id,'t','t' "
            "FROM threads WHERE id='root'",
        )
        step = service.add_node(
            scene.plan_id, kind="task", title="Its job", acceptance_criteria="done", db=db,
        )
        worker_plan = service.ensure_plan("thread", "worker-d", db=db)
        attach_spawned_thread(step.id, "worker-d", actor_id="root", db=db)
        db.commit()
        body = {"kind": "task", "title": "Ask", "acceptance_criteria": "answered"}
        if case == "to_user":
            actor = PlanActor("worker-d", ACTOR.user_id, ACTOR.project_id)
            body["assignee"] = "user"
        else:
            actor = ACTOR
        return partial(
            mutate, worker_plan.id, "add", body, actor=actor, provenance="test",
            mutation_id=_mid(), db=db,
        )

    return build


def _correction(*, affected_nodes):
    """Build an `add_correction` whose only defect is its affected-node list.

    A correction requires terminal source work, so the scene's own node is
    failed first; the operation is then the sole boundary route into
    `corrections._affected_rows`, and each malformed list reaches exactly one
    of its guards with every other field already valid.
    """

    def build(db, scene):
        service.transition_node(scene.own, "failed", db=db)
        db.commit()
        entries = [_resolved(db, scene, entry) for entry in affected_nodes]
        return partial(
            _mutate, db, scene.plan_id, "add_correction",
            {
                "node_id": scene.own,
                "expected_revision": _revision(db, scene.own),
                "title": "Correct the result",
                "acceptance_criteria": "The correction is verified",
                "affected_nodes": entries,
            },
            _mid(),
        )

    return build


def _held_by_another_thread(db, scene):
    """A watch may name only an artifact this thread owns.

    The artifact exists, so the unknown-artifact guard passes and this is the
    only route to the one that checks who holds it.
    """
    # The artifact has to exist and be held by someone else: registering to
    # nobody registers nothing, and would reach the unknown-artifact guard
    # instead of the one this case is named for.
    db.execute(
        "INSERT OR IGNORE INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('another','user-1','test/model','t','t')"
    )
    db.commit()
    artifact_id = _artifact(db, external_id="owner/repo#7", owner="another")
    return partial(
        _mutate, db, scene.plan_id, "arm_watch",
        {
            "node_id": scene.own, "expected_revision": _revision(db, scene.own),
            "condition": {"type": "event", "value": "proof.ready"},
            "artifact_id": artifact_id, "expected_event": "proof.ready",
        },
        _mid(),
    )


def _watch_on_own_artifact(db, scene):
    """A binding this thread holds, with nothing named to end the wait."""
    artifact_id = _artifact(db, external_id="owner/repo#8", owner="root")
    return partial(
        _mutate, db, scene.plan_id, "arm_watch",
        {
            "node_id": scene.own, "expected_revision": _revision(db, scene.own),
            "condition": {"type": "event", "value": "proof.ready"},
            "artifact_id": artifact_id,
        },
        _mid(),
    )


def _invalid_hold_kind(db, scene):
    """`block_node` and `pause_node` each pass their own literal kind, so the
    kind guard is unreachable from the boundary. `place` is its only caller."""
    from plans import holds

    return partial(
        holds.place_hold, scene.own, kind="deferred", reason_key="external",
        reason="Vendor is late", owner_kind="user", owner_id=None,
        expected_revision=_revision(db, scene.own), actor_id="root",
        provenance="test", db=db,
    )


def _wait_owner_as_hold(db, scene):
    """`block_node` arms a wait for these owners, so the hold writer's refusal
    is unreachable from the boundary. `block_hold` is its only caller."""
    from plans import holds

    return partial(
        holds.block_hold, scene.own, reason_key="external", reason="Vendor is late",
        owner_kind="external", owner_id=None, expected_revision=_revision(db, scene.own),
        actor_id="root", provenance="test", db=db,
    )


def _blocked_wait(condition: dict):
    return _operation(
        "block_node", {"node_id": OWN, "expected_revision": REV,
                       "reason_key": "external", "reason": "Vendor is late",
                       "owner_kind": "external", "owner_id": None,
                       "condition": condition},
    )


def _scheduled(**params):
    return _operation(
        "arm_watch", {"node_id": OWN, "expected_revision": REV, "reason": "Later",
                      "condition": {"type": "deadline", "value": "2999-01-01T00:00:00+00:00"},
                      **params},
    )


def _unowned_result_artifact(db, scene):
    """Evidence citing a result another thread owns.

    An artifact this thread owns would be linked by the submission itself, so
    only one held elsewhere reaches the refusal that names it.
    """
    db.execute(
        "INSERT OR IGNORE INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('another','user-1','test/model','t','t')"
    )
    db.commit()
    artifact_id = _artifact(db, external_id="owner/repo#9", owner="another")
    return partial(
        _mutate, db, scene.plan_id, "request_review",
        {"node_id": scene.own, "expected_revision": _revision(db, scene.own),
         "evidence": "Focused checks passed.", "artifact_ids": [artifact_id]},
        _mid(),
    )


def _lane_owned_node(db, scene):
    """A raised decision's review node: the lane owns its settlement, and the
    caller reaches the same outcome through the operation the message names."""
    decision = raise_decision(db, "root", "root")
    node_id = decision["node_id"]
    return partial(
        _mutate, db, decision["plan_id"], "fail",
        {"node_id": node_id, "expected_revision": _revision(db, node_id)},
        _mid(),
    )


#: One case per raise site, keyed by (module, the raise's own argument source).
#: The key is what the gate below reads out of the tree, so a site that loses
#: its case — or a case that outlives its site — is a failure, not a silence.
def _folded_cursor_contradicted(db, scene):
    """A full-read cursor cut without folded work, continued asking for it."""
    cursor = status_cursor.encode({
        "v": 1, "p": scene.plan_id, "s": status_cursor.FULL, "o": 0,
        "t": "0", "a": "2026-09-18T00:00:00+00:00", "f": False,
    })
    return partial(
        read_current_plan, actor=ACTOR, cursor=cursor, include_folded=True, db=db,
    )


def _user_block(db, scene, **extra):
    """An owning agent tries to put its pending task on the user."""
    params = {
        "node_id": scene.pending, "reason_key": "review", "reason": "Review it",
        "owner_kind": "user", "expected_revision": _revision(db, scene.pending),
        **extra,
    }
    return partial(_mutate, db, scene.plan_id, "block_node", params, _mid())


def _user_block_on_open_decision(db, scene):
    decision = raise_decision(db, "root", "root")
    return partial(
        _mutate, db, decision["plan_id"], "block_node", {
            "node_id": decision["source_node_id"], "reason_key": "decision",
            "reason": "Answer it", "owner_kind": "user",
            "expected_revision": _revision(db, decision["source_node_id"]),
        }, _mid(),
    )


def _user_block_on_foreign_document(db, scene):
    return _user_block(db, scene, ask_artifact_id=document_ask(db, "other"))


def _user_block_on_decision_artifact(db, scene):
    from plans import decision_projection

    decision = raise_decision(db, "root", "root")
    ask = decision_projection.identity_row(db, decision["id"])["artifact_id"]
    return _user_block(db, scene, ask_artifact_id=str(ask))


def _user_block_on_document_asked_elsewhere(db, scene):
    """Another task already holds the one open review request on this document."""
    ask = document_ask(db, "root")
    _user_block(db, scene, ask_artifact_id=ask)()
    return partial(
        _mutate, db, scene.plan_id, "block_node", {
            "node_id": scene.own, "reason_key": "review", "reason": "Review it",
            "owner_kind": "user", "ask_artifact_id": ask,
            "expected_revision": _revision(db, scene.own),
        }, _mid(),
    )


def _user_block_on_non_document(db, scene):
    ask = _artifact(db, external_id="user-ask-pr", owner="root")
    return _user_block(db, scene, ask_artifact_id=ask)


def _missing_intent_word(_db, _scene):
    return partial(intent_words.validate_intent, "")


def _too_many_intent_words(db, scene):
    words = [{"quote": f"Word {index}", "author": "agent"} for index in range(intent_words.MAX_WORDS_PER_CALL + 1)]
    return partial(intent_words.resolve_words, db, words, actor_id=None)


def _person_quote_without_thread(db, _scene):
    return partial(intent_words.resolve_words, db, [{"quote": "Build it", "role": "authority"}], actor_id=None)


def _oversized_intent_word(_db, _scene):
    return partial(intent_words.validate_intent, "x" * (intent_words.MAX_INTENT_BYTES + 1))


def _missing_task_intent(db, scene):
    return partial(_mutate, db, scene.plan_id, "add", {
        "title": "Intent", "acceptance_criteria": "Recorded", "size": "unknown",
    }, _mid())


def _missing_task_size_and_intent(db, scene):
    return partial(_mutate, db, scene.plan_id, "add", {
        "title": "Intent", "acceptance_criteria": "Recorded",
    }, _mid())


def _missing_authority_quote(db, scene):
    return partial(_mutate, db, scene.plan_id, "add", {
        "title": "Intent", "acceptance_criteria": "Recorded", "size": "unknown",
        "intent": "Keep the request aligned",
    }, _mid())


def _person_step(extra: dict):
    return _operation("add", {
        "kind": "task", "title": "Do setup", "acceptance_criteria": "Ready",
        "assignee": "user", "description": "Run setup. Already tried: the supported route.",
        **extra,
    })


def _foreign_instructions_document(db, scene):
    from registry import models

    artifact = models.register_artifact(
        integration="local", type="document", external_id="/shared/foreign-instructions.md",
        parent_thread_id="child", title="Foreign instructions", path="/shared/foreign-instructions.md",
        db=db,
    )
    db.commit()
    return partial(_mutate, db, scene.plan_id, "add", {
        "kind": "task", "title": "Do setup", "acceptance_criteria": "Ready", "size": "unknown",
        "assignee": "user", "description": "Run setup. Already tried: the supported route.",
        "instructions_artifact_id": artifact["id"],
    }, _mid())


def _wrong_type_instructions_document(db, scene):
    artifact = _artifact(db, external_id="instructions-pr", owner="root")
    return partial(_mutate, db, scene.plan_id, "add", {
        "kind": "task", "title": "Do setup", "acceptance_criteria": "Ready", "size": "unknown",
        "assignee": "user", "description": "Run setup. Already tried: the supported route.",
        "instructions_artifact_id": artifact,
    }, _mid())


_CASES = {
    # --- plans/service.py ---
    ("service.py", 'f"{field} is required"'): _operation(
        # `edit_fields` passes a supplied title straight through, so the blank
        # is refused by `service._required_text`, not `boundary_fields.text`.
        "edit", {"node_id": NODE, "expected_revision": REV, "title": "   "},
    ),
    ("service.py", '"checkpoint DOD must name the resulting plan expansion"'):
        _operation("add", {
            "kind": "task", "title": "T", "acceptance_criteria": "Tests pass",
            "checkpoint_exception": True,
        }),
    (
        "service.py",
        'f"node title is {len(normalized)} characters, over the " '
        'f"{MAX_NODE_TITLE}-character limit the plan row can show. Name the " '
        '"outcome in 1-3 words and move detail to the description."',
    ): _operation(
        # One line, ellipsis-truncated: a title this long hides its own tail in
        # the plan row, so the authoring boundary refuses it.
        "add",
        {
            "kind": "task",
            "title": "A node title deliberately written past the sixty "
                     "character limit the plan row can show",
            "acceptance_criteria": "DOD",
        },
    ),
    # --- plans/intent_words.py ---
    ("intent_words.py", 'f"{field} is required"'): _missing_intent_word,
    ("intent_words.py", 'f"{field} exceeds the {limit}-byte limit"'): _oversized_intent_word,
    ("intent_words.py", '"intent_words must be a nonempty array"'): _missing_intent_word,
    ("intent_words.py", 'f"intent_words takes at most {MAX_WORDS_PER_CALL} quotes per call; keep the strongest {MAX_WORDS_PER_CALL}"'): _too_many_intent_words,
    ("intent_words.py", '"intent_words items require quote, author?, and role?"'): _missing_intent_word,
    ("intent_words.py", '"intent word author must be person or agent"'): _missing_intent_word,
    ("intent_words.py", '"intent word role must be origin, bears_on, or authority"'): _missing_intent_word,
    ("intent_words.py", "QUOTE_NOT_FOUND"): _missing_intent_word,
    ("intent_words.py", '"person quotes must be recorded from the agent\'s own thread"'): _person_quote_without_thread,
    # --- plans/boundary_dispatch.py ---
    ("intent_creation.py", '"task creation needs intent_words with an exact person quote and role: authority"'): _missing_authority_quote,
    # --- plans/boundary_fields.py ---
    ("boundary_fields.py", '"task creation requires size and intent"'): _missing_task_size_and_intent,
    ("boundary_fields.py", '"task creation requires intent"'): _missing_task_intent,
    ("boundary_fields.py", "PERSON_STEP_DESCRIPTION_LIMIT_REFUSAL"):
        _person_step({"description": "x" * 380 + " Already tried: route"}),
    # --- plans/user_step_instructions.py ---
    ("user_step_instructions.py", '"instructions_artifact_id is only valid for an assignee: user task"'):
        _operation("add", {"title": "T", "acceptance_criteria": "DOD", "instructions_artifact_id": "doc"}),
    ("user_step_instructions.py", '"instructions_artifact_id must name a registered document"'):
        _person_step({"instructions_artifact_id": ""}),
    ("user_step_instructions.py", 'f"instructions_artifact_id {artifact_id!r} names no registered document"'):
        _person_step({"instructions_artifact_id": "missing"}),
    ("user_step_instructions.py", 'f"instructions_artifact_id must name a registered document, not {identity[\'type\']!r}"'):
        _wrong_type_instructions_document,
    ("user_step_instructions.py", '"instructions_artifact_id must name a document this thread owns"'):
        _foreign_instructions_document,
    # --- plans/node_creation.py ---
    ("node_creation.py", '"size requires schema version 91"'): _size_before_migration,
    # --- plans/size_fields.py ---
    ("size_fields.py", "TASK_SIZE_REQUIRED"): _operation(
        "add", {"kind": "task", "title": "T", "acceptance_criteria": "DOD"},
        default_task_size=False,
    ),
    (
        "size_fields.py",
        'f"size must be one of: {\', \'.join(SIZE_CLASSES)}. {SIZE_MEANING} {SIZE_SCALE_DESCRIPTION}."',
    ): _operation(
        "add", {"kind": "task", "title": "T", "acceptance_criteria": "DOD", "size": "large"},
    ),
    # --- plans/mutations.py ---
    ("mutations.py", "TASK_SIZE_REQUIRED"): _operation(
        "edit", {"node_id": NODE, "expected_revision": REV, "size": None},
    ),
    # --- plans/user_ask.py ---
    ("user_ask.py", "DECISION_HOLDS"): _user_block_on_open_decision,
    (
        "user_ask.py",
        '"condition is not used for a user ask; the ask itself ends this hold"',
    ): partial(
        _user_block,
        condition={"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
    ),
    (
        "user_ask.py",
        '"owner_id is derived from ask_artifact_id for a user ask; omit owner_id"',
    ): partial(_user_block, owner_id="someone-else"),
    ("user_ask.py", "NO_ASK"): _user_block,
    (
        "user_ask.py",
        'f"ask_artifact_id {ask!r} names no registered artifact"',
    ): partial(_user_block, ask_artifact_id="missing-artifact"),
    (
        "user_ask.py",
        '"ask_artifact_id must name an artifact this thread owns"',
    ): _user_block_on_foreign_document,
    (
        "user_ask.py",
        '"a decision is not blocked on; raise it, and it holds its task by itself"',
    ): _user_block_on_decision_artifact,
    ("user_ask.py", "NOT_ASKABLE"): _user_block_on_non_document,
    ("one_open_ask.py", 'f"this document already has an open review request on {held}, waiting for " '
                        '"the user\'s answer. It is theirs to answer, so do not resolve it; ask again " '
                        '"once they have answered it"'): _user_block_on_document_asked_elsewhere,
    # --- plans/node_creation.py ---
    ("node_creation.py", 'f"invalid plan node kind: {kind}"'): _operation(
        "add", {"kind": "epic", "title": "T", "acceptance_criteria": "DOD"},
    ),
    ("node_creation.py", 'f"invalid plan node assignee: {assignee}"'): _operation(
        "add", {"kind": "task", "title": "T", "acceptance_criteria": "DOD",
                "assignee": "robot"},
    ),
    ("node_creation.py", '"watchdog budget seconds must be a positive integer"'):
        _operation("add", {
            "kind": "task", "title": "T", "acceptance_criteria": "DOD",
            "watchdog_budget_seconds": 0,
        }),
    ("node_creation.py", '"order key must be non-negative"'): _negative_order_key,
    ("node_creation.py", '"task nodes are leaves and cannot contain children"'):
        _operation("add", {
            "kind": "task", "title": "T", "acceptance_criteria": "DOD",
            "parent_id": NODE,
        }),
    ("node_creation.py", '"workflow fields require schema version 51"'):
        _workflow_fields_before_migration,
    ("node_creation.py", '"task intent requires schema version 138"'): _intent_before_migration,
    # --- plans/workflow_fields.py ---
    ("workflow_fields.py", 'f"{field} must be a non-empty string or null"'):
        _operation("add", {
            "kind": "task", "title": "T", "acceptance_criteria": "DOD",
            "workflow_ref": "",
        }),
    ("workflow_fields.py", 'f"{field} exceeds the {MAX_WORKFLOW_VALUE}-character limit"'):
        _operation("add", {
            "kind": "task", "title": "T", "acceptance_criteria": "DOD",
            "workflow_ref": "x" * 201,
        }),
    ("workflow_fields.py", '"difficulty must be one of: routine, standard, hard"'):
        _operation("add", {
            "kind": "task", "title": "T", "acceptance_criteria": "DOD",
            "difficulty": "unknown",
        }),
    ("workflow_fields.py", "difficulty_rubric()"): _operation("add", {
        "kind": "task", "title": "T", "acceptance_criteria": "DOD",
        "workflow_step": "build",
    }),
    ("workflow_fields.py", '"difficulty_rerated must be {level, reason}"'):
        _operation("edit", {
            "node_id": NODE, "expected_revision": REV, "difficulty_rerated": {},
        }),
    ("workflow_fields.py", '"difficulty_rerated.level must be one of: routine, standard, hard"'):
        _operation("edit", {
            "node_id": NODE, "expected_revision": REV,
            "difficulty_rerated": {"level": "unknown", "reason": "Re-scoped"},
        }),
    ("workflow_fields.py", '"difficulty_rerated.reason is required"'):
        _operation("edit", {
            "node_id": NODE, "expected_revision": REV,
            "difficulty_rerated": {"level": "routine", "reason": None},
        }),
    # --- plans/boundary_fields.py ---
    ("boundary_fields.py", 'f"unsupported plan operation: {operation}"'):
        _operation("teleport", {"node_id": NODE, "expected_revision": REV}),
    ("boundary_fields.py", 'f"unexpected parameters for {operation}: {joined}"'):
        _operation("activate", {"node_id": NODE, "expected_revision": REV,
                                "order_key": 1}),
    ("boundary_fields.py", '"expected_revision is required"'): _operation(
        "activate", {"node_id": NODE},
    ),
    ("boundary_fields.py", '"expected_revision must be a non-negative integer"'):
        _operation("activate", {"node_id": NODE, "expected_revision": -1}),
    ("boundary_fields.py", '"next_node_id must be a node id"'): _operation(
        # A closing operation accepts the field, so only its type can refuse it.
        "request_review", {"node_id": OWN, "expected_revision": REV,
                           "evidence": "Focused checks passed.", "next_node_id": 7},
    ),
    ("boundary_fields.py", "USER_STEP_DESCRIPTION_REFUSAL"): _operation(
        "add", {"kind": "task", "title": "Run command", "acceptance_criteria": "Done",
                "assignee": "user"},
    ),
    ("boundary_fields.py", '"edit requires at least one changed field"'):
        _operation("edit", {"node_id": NODE, "expected_revision": REV}),
    ("boundary_fields.py", '"verification_result must be \'passed\' when supplied"'):
        _operation("accept", {"node_id": NODE, "expected_revision": REV,
                              "verification_result": "failed"}),
    ("boundary_fields.py", 'f"{field} is required"'): _operation(
        # `spawned_thread_id` is never a `service._required_text` field, so this
        # message can only have come from `boundary_fields.text`.
        "attach_spawned_thread",
        {"node_id": NODE, "expected_revision": REV, "spawned_thread_id": ""},
    ),
    ("boundary_fields.py", 'f"{field} must be an integer"'): _operation(
        "reorder", {"node_id": NODE, "expected_revision": REV, "order_key": "3"},
    ),
    ("boundary_fields.py", "_ARCHIVE_TARGET"): _operation("archive_node", {}),
    ("boundary_fields.py", "_ARCHIVE_BULK_ALONE"): _operation(
        # The shape is refused before any id is read, so literal values do;
        # `_resolved` substitutes only top-level scalars and needs a node_id.
        "archive_node", {"node_ids": ["node-1"], "expected_revision": 1},
    ),
    ("boundary_fields.py", "_ARCHIVE_IDS"): _operation(
        "archive_node", {"node_ids": []},
    ),
    # --- plans/closure.py ---
    ("closure.py", "REASON_REQUIRED"): _blank_close_reason,
    # --- plans/delegation.py ---
    ("delegation.py", '"detaching a worker requires a reason"'): _blank_detach_reason,
    (
        "delegation.py",
        'f"node already delegates to {current}; use replace_spawned_thread"'
        ' f" to hand it to {spawned_thread_id}"',
    ): _attach_over_another_worker,
    # --- plans/boundary.py ---
    ("boundary.py", '"mutation_id is required"'): _operation(
        "activate", {"node_id": NODE, "expected_revision": REV},
        mutation_id="   ",
    ),
    ("boundary.py", '"node_id is required"'): _operation(
        "activate", {"expected_revision": 0},
    ),
    ("boundary.py", 'f"{field} must be a node id"'): _operation(
        "insert", {"kind": "task", "title": "T", "acceptance_criteria": "DOD",
                   "parent_id": 7},
    ),
    ("boundary.py", "FOREIGN_PLAN_MISSING"): _foreign_add(None),
    ("boundary.py", "FOREIGN_PLAN_MUTATION"): _foreign_add("other-root"),
    # --- plans/boundary_dispatch.py ---
    ("boundary_dispatch.py", 'f"unsupported plan operation: {operation}"'):
        _dispatch_fallthrough,
    ("boundary_dispatch.py", "FINISHED_WORK_NEEDS_EVIDENCE"): _operation(
        "accept", {"node_id": OWN, "expected_revision": REV,
                   "verification_result": "passed"},
    ),
    # --- plans/mutations.py ---
    ("mutations.py", '"order key must be non-negative"'): _operation(
        "reorder", {"node_id": NODE, "expected_revision": REV, "order_key": -1},
    ),
    ("mutations.py", '"edit requires at least one changed field"'): _operation(
        # An explicit null is a supplied key, so `edit_fields` accepts it and
        # only `edit_node` sees that nothing was actually changed.
        "edit", {"node_id": NODE, "expected_revision": REV, "title": None},
    ),
    ("mutations.py", '"watchdog budget seconds must be a positive integer"'):
        _operation("edit", {"node_id": NODE, "expected_revision": REV,
                            "watchdog_budget_seconds": 0}),
    # --- plans/watch_operations.py ---
    ("watch_operations.py", '"watch_id is required"'): _operation(
        "resolve_watch", {"node_id": OWN, "expected_revision": REV,
                          "watch_id": ""},
    ),
    ("watch_operations.py", "str(exc)"): _operation(
        "arm_watch", {"node_id": OWN, "expected_revision": REV, "condition": {}},
    ),
    ("watch_operations.py",
     '"artifact_id must be the id of an artifact CORSIA already knows"'): _operation(
        "arm_watch", {
            "node_id": OWN, "expected_revision": REV,
            "condition": {"type": "event", "value": "proof.ready"},
            "artifact_id": "never-registered",
        },
    ),
    ("watch_operations.py", "watches.UNBOUND_REFUSED"): _operation(
        "arm_watch", {
            "node_id": OWN, "expected_revision": REV,
            "condition": {"type": "event", "value": "proof.ready"},
            "expected_event": "proof.ready",
        },
    ),
    ("watch_operations.py",
     '"artifact_id must name an artifact this thread owns"'):
        _held_by_another_thread,
    ("watch_operations.py",
     '"a watch on an artifact needs the expected_event that ends it"'):
        _watch_on_own_artifact,
    # --- plans/dependencies.py ---
    ("dependencies.py", 'f"Dependency is inherited from ancestor {inherited[\'via\']}; remove it there"'): _remove_inherited_dependency,
    ("dependencies.py", "_DEPENDENCY_REFUSED"): _operation(
        "add_dependency",
        {"node_id": NODE, "depends_on_node_id": NODE, "expected_revision": REV},
    ),
    # --- plans/node_settlement.py ---
    ("node_settlement.py", "refusal_message(lane, operation)"):
        _lane_owned_node,
    # --- plans/corrections.py ---
    ("corrections.py",
     '"affected_nodes must be a nonempty array of node/revision pairs"'):
        _correction(affected_nodes=[]),
    ("corrections.py",
     '"affected_nodes entries require only node_id and expected_revision"'):
        _correction(affected_nodes=[{"node_id": NODE}]),
    ("corrections.py",
     '"affected_nodes entries require a node id and non-negative revision"'):
        _correction(
            affected_nodes=[{"node_id": NODE, "expected_revision": -1}],
        ),
    ("corrections.py", '"affected_nodes must not contain duplicates"'):
        _correction(affected_nodes=[
            {"node_id": NODE, "expected_revision": REV},
            {"node_id": NODE, "expected_revision": REV},
        ]),
    # --- plans/holds.py ---
    ("holds.py", '"invalid plan hold owner kind"'): _operation(
        "block_node", {"node_id": OWN, "expected_revision": REV,
                       "reason_key": "external", "reason": "Vendor is late",
                       "owner_kind": "alien", "owner_id": None},
    ),
    ("holds.py", '"invalid plan hold kind"'): _invalid_hold_kind,
    ("holds.py", '"a blocker anyone but the user clears is a wait, not a hold; " '
                 '"block_node arms it with a deadline"'): _wait_owner_as_hold,
    # --- plans/blocked_wait.py ---
    ("blocked_wait.py", '"a blocker only the user can clear is a hold; any other is a wait " '
                        '"that ends at a deadline, so its condition must be a deadline"'):
        _blocked_wait({"type": "event", "value": "ci.complete"}),
    ("blocked_wait.py", '"owner_kind parent is available only in a delegated plan; block on " '
                        '"owner_kind user or raise a decision instead"'):
        _operation("block_node", {"node_id": OWN, "expected_revision": REV,
                                    "reason_key": "input", "reason": "Need input",
                                    "owner_kind": "parent", "owner_id": None}),
    ("blocked_wait.py", '"a blocked wait ends within 7 days; name an earlier deadline, " '
                        '"and call block_node again then if it still holds"'):
        _blocked_wait({"type": "deadline", "value": "2999-01-01T00:00:00+00:00"}),
    ("blocked_wait.py", "str(exc)"): _blocked_wait({"type": "deadline", "value": "someday"}),
    # --- plans/review_evidence.py ---
    ("review_evidence.py", '"artifact_ids must be an array"'): _operation(
        "request_review", {"node_id": OWN, "expected_revision": REV,
                           "evidence": "Focused checks passed.",
                           "artifact_ids": "artifact-1"},
    ),
    ("review_evidence.py", '"artifact_ids must contain nonempty ids"'): _operation(
        "request_review", {"node_id": OWN, "expected_revision": REV,
                           "evidence": "Focused checks passed.",
                           "artifact_ids": ["   "]},
    ),
    (
        "review_evidence.py",
        '"artifact_ids must name active results this thread owns; " '
        'f"not owned: {\', \'.join(unowned)}"',
    ): _unowned_result_artifact,
    # --- plans/pause_authority.py (pause contract rule 1) ---
    ("pause_authority.py", "RESUME_REFUSAL"): _agent_plan_change("resume"),
    ("pause_authority.py", 'pause_refusal(params.get("node_id"))'): _agent_plan_change("pause"),
    # --- plans/scheduled_wait.py (an agent schedules a task instead of pausing it) ---
    ("scheduled_wait.py", '"reason schedules this task until a deadline, so it takes no " '
                          '"artifact_id. To wait for an event on the artifact, drop reason; " '
                          '"to schedule the task, drop artifact_id"'): _scheduled(artifact_id="artifact-1"),
    ("scheduled_wait.py", '"a scheduled task comes back at a date: its condition must be a deadline"'):
        _scheduled(condition={"type": "event", "value": "ci.complete"}),
    ("scheduled_wait.py", '"arm_watch needs the expected_event that ends the wait, or a reason " '
                          '"to schedule the task until the deadline"'): _operation(
        "arm_watch", {"node_id": OWN, "expected_revision": REV,
                      "condition": {"type": "deadline", "value": "2999-01-01T00:00:00+00:00"}},
    ),
    # --- plans/delegate_attach.py (delegate_task, before the worker is spawned) ---
    (
        "delegate_attach.py",
        'f"plan_node_id {wanted} is not a task in your plan; pass one of" '
        '" your task ids, \'none\', or omit it for your current task"',
    ): _delegate_to("no-such-task"),
    ("delegate_attach.py", 'f"task {node[\'id\']} is finished; a new worker cannot take it over"'):
        _delegate_to(finish=True),
    (
        "delegate_attach.py",
        'f"task {node[\'id\']} already has worker {node[\'spawned_thread_id\']};" '
        '" use plan_update replace_spawned_thread to hand it to another"',
    ): _delegate_to(worker=True),
    # --- plans/delegate_attach.py (delegate_send, before the message goes) ---
    (
        "delegate_attach.py",
        'f"plan_node_id hands a task only to a worker you started;" '
        'f" {child_thread_id} is not one. Send without plan_node_id"',
    ): _reuse_worker("not_mine"),
    (
        "delegate_attach.py",
        'f"worker {child_thread_id} still holds an open task; settle or" '
        '" close it before handing it another, or send without plan_node_id"',
    ): _reuse_worker("holds_open"),
    # --- plans/boundary_worker.py (matrix section G) ---
    (
        "boundary_worker.py",
        'plan_text.workers_own_step(step["title"], parent_plan_id, step["id"])',
    ): _worker_plan("parent"),
    ("boundary_worker.py", "plan_text.WORKER_STEP_TO_USER"): _worker_plan("to_user"),
    # --- plans/user_steering.py (the user's pause on a task is theirs to end) ---
    ("user_steering.py", "pause_authority.NODE_RESUME_REFUSAL"): _node_pause_then_resume(
        _USER, ACTOR,
    ),
    # --- plans/current.py (a read, not a mutation) ---
    ("current.py", "KNOWN_REVISION_NARROWED"): lambda db, scene: partial(
        read_current_plan, actor=ACTOR, cursor="stale", known_revision=0, db=db,
    ),
    (
        "current.py",
        "KNOWN_REVISION_NARROWED if narrowed else KNOWN_REVISION_STATUS",
    ): lambda db, scene: partial(
        read_current_status, actor=ACTOR, known_revision=0, db=db,
    ),
    ("current.py", "INCLUDE_FOLDED_FULL_ONLY"): lambda db, scene: partial(
        read_current_status, actor=ACTOR, include_folded=True, db=db,
    ),
    (
        "current.py",
        'INCLUDE_FOLDED_CURSOR.format(cut=str(cut["f"]).lower())',
    ): _folded_cursor_contradicted,
    # --- plans/system_user_steps.py (system code, not the plan tool) ---
    ("system_user_steps.py", '"open_user_step needs a non-empty key"'): lambda db, scene: partial(
        system_user_steps.open_user_step, db, "root", key=" ", title="T",
        description="D", provenance="test",
    ),
    (
        "system_user_steps.py",
        '"settle_user_step settles only a step open_user_step opened"',
    ): lambda db, scene: partial(
        system_user_steps.settle_user_step, db, scene.own,
        evidence="E", provenance="test",
    ),
}
