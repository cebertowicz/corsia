"""Kill test for the plan-aggregates data model: propagation model versus recount.

Random operation sequences (and the named review cases B1, B2, B2', B3 and the
rest) are applied to the in-memory model of ``plan_aggregates_model`` and, after
every step, the model must equal ``plan_aggregates_oracle`` for every node and
for the plan's top level. A disagreement is a counterexample to r3 section 4.

Three generator modes:

``legal``  only sequences the S1 guards allow: no child under a settled parent,
           no fail over open descendants, a follow-up relocated off a settled
           parent.
``wild``   adds what today's code still allows (the S0 and B4 findings): a child
           under a settled parent, fail over open descendants, a follow-up at
           ``source.parent_id`` whatever its state.
``hole``   adds a child under a settled node *below a retired ancestor*, which
           today's ``node_creation._validate_parent`` also allows because it
           reads only the parent's own row. This is the one mode that disagrees
           with the oracle (see ``test_counterexample_open_work_under_retired_ancestor``).

Hypothesis is not a dependency of the backend, so the sequences come from a
seeded ``random.Random``.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from tests.plan_aggregates_model import ACCEPTED, TERMINAL, Folded, Row, Tree, Work, apply
from tests.plan_aggregates_oracle import recount

SEQUENCES = 2_000
MAX_OPS = 40


@dataclass
class Sim:
    tree: Tree = field(default_factory=Tree)
    wild: bool = False
    hole: bool = False
    counter: int = 0
    workers: int = 0
    log: list[str] = field(default_factory=list)

    # -- reads ------------------------------------------------------------
    def row(self, node_id: str) -> Row:
        return self.tree.nodes[node_id].row

    def live(self) -> list[Row]:
        return [node.row for node in self.tree.nodes.values() if not node.row.archived]

    def children(self, node_id: str) -> list[Row]:
        return [row for row in self.live() if row.parent_id == node_id]

    def work_descendants(self, node_id: str) -> list[Row]:
        """Descendants reached only through nodes in the work (``in_work_sql`` descent)."""
        found: list[Row] = []
        stack = [row for row in self.children(node_id) if not row.retired]
        while stack:
            row = stack.pop()
            found.append(row)
            stack.extend(child for child in self.children(row.id) if not child.retired)
        return found

    def retired_above(self, node_id: str) -> bool:
        cursor = self.row(node_id).parent_id
        while cursor is not None:
            if self.row(cursor).retired:
                return True
            cursor = self.row(cursor).parent_id
        return False

    def open_below(self, node_id: str) -> bool:
        return any(row.state not in TERMINAL for row in self.work_descendants(node_id))

    def unaccepted_below(self, node_id: str) -> bool:
        return any(row.state not in ACCEPTED for row in self.work_descendants(node_id))

    # -- writes -----------------------------------------------------------
    def new_id(self) -> str:
        self.counter += 1
        return f"n{self.counter}"

    def add(self, parent_id: str | None, *, state: str = "pending", spawned: str | None = None) -> str:
        node_id = self.new_id()
        self.log.append(f"add {node_id} under {parent_id}")
        self.tree.insert(Row(node_id, parent_id, state=state, spawned=spawned))
        return node_id

    def set(self, node_id: str, label: str, **fields: object) -> None:
        self.log.append(f"{label} {node_id} {fields}")
        self.tree.change(node_id, **fields)

    def follow_up_parent(self, source_id: str) -> str | None:
        """Where a follow-up lands: S1 moves it off a settled parent (r3 section 4)."""
        parent = self.row(source_id).parent_id
        if self.wild:
            return parent
        while parent is not None and self.row(parent).state in TERMINAL:
            parent = self.row(parent).parent_id
        return parent


class Disagreement(AssertionError):
    """Model and oracle disagree: ``field`` names the figure, ``details`` both values."""

    def __init__(self, field: str, details: tuple) -> None:
        super().__init__(field, *details)
        self.field = field


def agree(condition: bool, field: str, *details: object) -> None:
    if not condition:
        raise Disagreement(field, details)


def check(sim: Sim) -> None:
    """Assert model == oracle for every node and the top level."""
    rows = [
        {
            "id": row.id, "parent_id": row.parent_id, "state": row.state,
            "archived_at": "t" if row.archived else None,
            "spawned_thread_id": row.spawned,
        }
        for row in (node.row for node in sim.tree.nodes.values())
    ]
    sent_back = [node.row.id for node in sim.tree.nodes.values() if node.row.sent_back]
    oracle = recount(rows, sent_back)
    context = "\n  ".join(sim.log)
    for node_id, node in sim.tree.nodes.items():
        if node.row.archived:
            agree(node.prop_open == 0 and node.prop_work.zero(), "archived", node_id, context)
            continue
        figures = oracle.nodes[node_id]
        agree(node.prop_open == figures.open_total, "open", node_id, node.prop_open, figures.open_total, context)
        agree(node.sub_open == figures.sub_open, "sub_open", node_id, node.sub_open, figures.sub_open, context)
        agree(node.own.folded == figures.own_folded, "own_folded", node_id, context)
        agree(node.folded == figures.folded, "folded", node_id, node.folded, figures.folded, context)
        if figures.in_work:
            agree(
                node.child_in_work == figures.child_in_work, "child_in_work",
                node_id, node.child_in_work, figures.child_in_work, context,
            )
        if figures.visible:
            agree(
                node.child_folded == Folded(*figures.child_folded), "child_folded",
                node_id, node.child_folded, figures.child_folded, context,
            )
        if figures.passed is not None:
            expected = Work(
                figures.passed.done, figures.passed.failed, figures.passed.closed, figures.passed.in_work,
            )
            agree(node.prop_work == expected, "work", node_id, node.prop_work, expected, context)
    top = Work(oracle.top.done, oracle.top.failed, oracle.top.closed, oracle.top.in_work)
    agree(sim.tree.top_open == oracle.top.open, "top open", sim.tree.top_open, oracle.top.open, context)
    agree(
        sim.tree.top_folded == Folded(*oracle.top_folded), "top folded",
        sim.tree.top_folded, oracle.top_folded, context,
    )
    agree(sim.tree.top_work == top, "top work", sim.tree.top_work, top, context)
    # F3: the in-play ``done`` the pane shows is the roots' done that have not folded.
    in_play_done = sum(
        node.prop_work.done
        for node in sim.tree.nodes.values()
        if not node.row.archived and node.row.parent_id is None and not node.folded
    )
    agree(in_play_done == oracle.progress_done, "progress done", in_play_done, oracle.progress_done, context)


# -- operations -------------------------------------------------------------

def _live_ids(sim: Sim) -> list[str]:
    return [row.id for row in sim.live()]


def op_add_top(sim: Sim, rng: random.Random) -> bool:
    sim.add(None)
    return True


def op_add_child(sim: Sim, rng: random.Random) -> bool:
    parents = [
        row for row in sim.live()
        if row.state != "closed" and (sim.wild or row.state not in TERMINAL)
        and (sim.hole or not sim.retired_above(row.id))
    ]
    if not parents:
        return False
    sim.add(rng.choice(parents).id)
    return True


def _transition(
    sim: Sim, rng: random.Random, sources: set[str], target: str,
    *, guard: Callable[[Sim, Row], bool] | None = None,
) -> bool:
    rows = [row for row in sim.live() if row.state in sources and (guard is None or guard(sim, row))]
    if not rows:
        return False
    sim.set(rng.choice(rows).id, target, state=target)
    return True


def op_start(sim: Sim, rng: random.Random) -> bool:
    return _transition(sim, rng, {"pending"}, "active")


def op_request_review(sim: Sim, rng: random.Random) -> bool:
    return _transition(sim, rng, {"pending", "active"}, "review_requested")


def op_reopen(sim: Sim, rng: random.Random) -> bool:
    return _transition(sim, rng, {"review_requested"}, "active")


def op_accept(sim: Sim, rng: random.Random) -> bool:
    return _transition(
        sim, rng, {"review_requested"}, "accepted", guard=lambda s, r: not s.unaccepted_below(r.id),
    )


def op_fail(sim: Sim, rng: random.Random) -> bool:
    return _transition(
        sim, rng, {"pending", "active", "review_requested"}, "failed",
        guard=lambda s, r: s.wild or not s.open_below(r.id),
    )


def op_close(sim: Sim, rng: random.Random) -> bool:
    return _transition(
        sim, rng, {"pending", "active", "review_requested"}, "closed",
        guard=lambda s, r: not s.open_below(r.id),
    )


def op_accept_with_follow_up(sim: Sim, rng: random.Random) -> bool:
    rows = [
        row for row in sim.live()
        if row.state == "review_requested" and not sim.unaccepted_below(row.id)
    ]
    if not rows:
        return False
    source = rng.choice(rows)
    sim.set(source.id, "accepted_with_followup", state="accepted_with_followup")
    sim.add(sim.follow_up_parent(source.id))
    return True


def op_reject(sim: Sim, rng: random.Random) -> bool:
    rows = [
        row for row in sim.live()
        if row.state == "review_requested" and not sim.open_below(row.id)
    ]
    if not rows:
        return False
    source = rng.choice(rows)
    # The revision is created first (followups.add_follow_up), then the round is
    # sent back and the worker moves to the revision.
    revision = sim.add(sim.follow_up_parent(source.id), spawned=source.spawned)
    sim.set(source.id, "send back", state="closed", sent_back=True)
    assert revision
    return True


def op_archive_single(sim: Sim, rng: random.Random) -> bool:
    rows = [row for row in sim.live() if not sim.children(row.id)]
    if not rows:
        return False
    sim.set(rng.choice(rows).id, "archive", archived=True)
    return True


def op_archive_recursive(sim: Sim, rng: random.Random) -> bool:
    rows = sim.live()
    if not rows:
        return False
    root = rng.choice(rows)
    order: list[Row] = []

    def collect(row: Row) -> None:
        for child in sim.children(row.id):
            collect(child)
        order.append(row)

    collect(root)
    for row in order:
        sim.set(row.id, "archive", archived=True)
    return True


def op_attach(sim: Sim, rng: random.Random) -> bool:
    rows = [row for row in sim.live() if row.spawned is None and not row.retired and row.state not in TERMINAL]
    if not rows:
        return False
    sim.workers += 1
    sim.set(rng.choice(rows).id, "attach", spawned=f"w{sim.workers}")
    return True


def op_detach(sim: Sim, rng: random.Random) -> bool:
    rows = [row for row in sim.live() if row.spawned is not None]
    if not rows:
        return False
    sim.set(rng.choice(rows).id, "detach", spawned=None)
    return True


def op_touch(sim: Sim, rng: random.Random) -> bool:
    ids = _live_ids(sim)
    if not ids:
        return False
    node_id = rng.choice(ids)
    sim.log.append(f"touch {node_id}")
    apply(sim.tree, node_id)
    return True


OPERATIONS = [
    op_add_top, op_add_child, op_add_child, op_start, op_request_review, op_reopen,
    op_accept, op_accept, op_accept_with_follow_up, op_fail, op_close, op_close,
    op_archive_single, op_archive_recursive, op_reject, op_attach, op_detach, op_touch,
]


def run_sequence(seed: int, *, wild: bool, hole: bool = False) -> Sim:
    rng = random.Random(seed)
    sim = Sim(wild=wild or hole, hole=hole)
    target = rng.randint(1, MAX_OPS)
    applied = attempts = 0
    # Many operations have no eligible node yet; keep drawing until `target` apply.
    while applied < target and attempts < 20 * MAX_OPS:
        attempts += 1
        if rng.choice(OPERATIONS)(sim, rng):
            applied += 1
            check(sim)
    return sim


@pytest.mark.parametrize("wild", [False, True], ids=["legal", "wild"])
def test_model_equals_recount_on_random_sequences(wild: bool) -> None:
    applied = 0
    for seed in range(SEQUENCES):
        sim = run_sequence(seed + (10_000 if wild else 0), wild=wild)
        applied += len(sim.log)
    assert applied > SEQUENCES  # the generator really exercised operations


def test_hole_mode_finds_the_counterexample() -> None:
    """Open work under a retired ancestor breaks model == oracle; pin that it does."""
    failing = []
    for seed in range(SEQUENCES):
        try:
            run_sequence(seed + 20_000, wild=True, hole=True)
        except Disagreement as error:
            failing.append(error.field)
    assert failing, "hole mode no longer disagrees: re-read the counterexample note"
    assert set(failing) <= {"open", "sub_open", "own_folded", "folded", "child_in_work", "work",
                            "top open", "top work", "progress done", "child_folded",
                            "top folded"}


def test_generator_reaches_every_operation_and_state() -> None:
    """Guard the property test against a generator that quietly stops covering cases."""
    seen_labels: set[str] = set()
    states: set[str] = set()
    sent_back = containers = 0
    for seed in range(300):
        sim = run_sequence(seed, wild=False)
        seen_labels |= {entry.split()[0] + (" back" if entry.startswith("send") else "") for entry in sim.log}
        for node in sim.tree.nodes.values():
            states.add(node.row.state)
            sent_back += node.row.sent_back
            containers += node.own.folded and node.row.state == "pending"
    assert {"add", "active", "review_requested", "accepted", "failed", "closed", "archive", "attach",
            "detach", "touch", "send back", "accepted_with_followup"} <= seen_labels
    assert states >= {"pending", "active", "review_requested", "accepted", "failed", "closed",
                      "accepted_with_followup"}
    assert sent_back and containers


# -- named cases ------------------------------------------------------------

def _sim(wild: bool = False) -> Sim:
    return Sim(wild=wild)


def _step(sim: Sim) -> None:
    check(sim)


def test_b1_close_last_in_work_child_of_pending_container() -> None:
    sim = _sim()
    parent = sim.add(None)
    child = sim.add(parent)
    _step(sim)
    assert sim.tree.nodes[parent].own.folded  # a container while the child is in work
    sim.set(child, "close", state="closed")
    _step(sim)
    # The container reopens as a plain pending leaf: open moved with a zero incoming work delta.
    assert not sim.tree.nodes[parent].own.folded
    assert sim.tree.top_open == 1


def test_b2_close_of_an_already_folded_container() -> None:
    sim = _sim()
    parent = sim.add(None)
    child = sim.add(parent)
    sim.set(child, "start", state="active")
    sim.set(child, "review", state="review_requested")
    sim.set(child, "accept", state="accepted")
    _step(sim)
    assert sim.tree.nodes[parent].folded
    before = sim.tree.top_work
    sim.set(parent, "close", state="closed")
    _step(sim)
    assert sim.tree.nodes[parent].folded
    assert sim.tree.top_work != before


def test_b2_prime_close_of_phase_with_accepted_and_failed_children() -> None:
    sim = _sim()
    grand = sim.add(None)
    phase = sim.add(grand)
    done_child = sim.add(phase)
    failed_child = sim.add(phase)
    for node in (done_child, failed_child):
        sim.set(node, "start", state="active")
    sim.set(done_child, "review", state="review_requested")
    sim.set(done_child, "accept", state="accepted")
    sim.set(failed_child, "fail", state="failed")
    _step(sim)
    assert sim.tree.nodes[grand].prop_work.done == 1
    assert sim.tree.nodes[grand].prop_work.failed == 1
    sim.set(phase, "close", state="closed")
    _step(sim)
    # The done and failed counts leave every ancestor; only the closed phase remains.
    for ancestor in (grand,):
        work = sim.tree.nodes[ancestor].prop_work
        assert (work.done, work.failed, work.closed) == (0, 0, 1)
    assert (sim.tree.top_work.done, sim.tree.top_work.failed) == (0, 0)


def test_b3_non_recursive_archive_of_child_under_settled_ancestor() -> None:
    sim = _sim()
    parent = sim.add(None)
    child = sim.add(parent)
    for step, state in (("start", "active"), ("review", "review_requested"), ("accept", "accepted")):
        sim.set(child, step, state=state)
    for step, state in (("start", "active"), ("review", "review_requested"), ("accept", "accepted")):
        sim.set(parent, step, state=state)
    _step(sim)
    assert sim.tree.top_work.done == 1  # the accepted parent is not a leaf while its child is in work
    sim.set(child, "archive", archived=True)
    _step(sim)
    assert sim.tree.nodes[parent].child_in_work == 0
    assert sim.tree.top_work.done == 1  # the parent is now the done leaf


def test_accepted_parent_becomes_done_leaf_when_last_in_work_child_is_archived() -> None:
    sim = _sim()
    grand = sim.add(None)
    parent = sim.add(grand)
    child = sim.add(parent)
    for node in (child, parent):
        for step, state in (("start", "active"), ("review", "review_requested"), ("accept", "accepted")):
            sim.set(node, step, state=state)
    _step(sim)
    assert sim.tree.nodes[grand].prop_work.done == 1  # the child; the parent has a child in work
    sim.set(child, "archive", archived=True)
    _step(sim)
    assert sim.tree.nodes[grand].prop_work.done == 1  # now the parent, not the child
    assert sim.tree.nodes[parent].own.work.done == 1


def test_start_attach_detach_under_a_stale_container() -> None:
    sim = _sim()
    parent = sim.add(None)
    child = sim.add(parent)
    sim.set(child, "start", state="active")
    sim.set(child, "review", state="review_requested")
    sim.set(child, "accept", state="accepted")
    _step(sim)
    assert sim.tree.nodes[parent].folded  # stale container: pending, workerless, every child settled
    sim.set(parent, "attach", spawned="w1")
    _step(sim)
    assert not sim.tree.nodes[parent].folded
    sim.set(parent, "detach", spawned=None)
    _step(sim)
    assert sim.tree.nodes[parent].folded
    sim.set(parent, "start", state="active")
    _step(sim)
    assert not sim.tree.nodes[parent].folded
    # A second container: attach and detach on the other half of the pair.
    other = sim.add(None)
    inner = sim.add(other)
    sim.set(inner, "attach", spawned="w2")
    sim.set(other, "attach", spawned="w3")
    sim.set(inner, "detach", spawned=None)
    sim.set(other, "detach", spawned=None)
    _step(sim)


def test_send_back_round_with_children() -> None:
    sim = _sim()
    root = sim.add(None)
    task = sim.add(root, spawned="w1")
    child = sim.add(task)
    for step, state in (("start", "active"), ("review", "review_requested"), ("accept", "accepted")):
        sim.set(child, step, state=state)
    sim.set(task, "start", state="active")
    sim.set(task, "review", state="review_requested")
    _step(sim)
    revision = sim.add(root, spawned="w1")
    sim.set(task, "send back", state="closed", sent_back=True)
    _step(sim)
    work = sim.tree.nodes[root].prop_work
    assert work.closed == 0  # the sent-back round never counts as closed
    assert work.done == 0  # nor does its settled child, cut by the retirement rule
    assert sim.tree.nodes[revision].row.spawned == "w1"


def test_follow_up_under_a_settled_parent() -> None:
    for wild in (False, True):
        sim = _sim(wild)
        parent = sim.add(None)
        source = sim.add(parent)
        for step, state in (("start", "active"), ("review", "review_requested")):
            sim.set(source, step, state=state)
        sim.set(parent, "start", state="active")
        sim.set(parent, "fail", state="failed") if wild else None
        _step(sim)
        if not wild:
            sim.set(source, "accept", state="accepted_with_followup")
            sim.set(parent, "review", state="review_requested")
            sim.set(parent, "accept", state="accepted")
            _step(sim)
            landing = sim.follow_up_parent(source)
            assert landing is None  # relocated off the settled parent to the top level
        else:
            sim.set(source, "accept", state="accepted_with_followup")
            landing = sim.follow_up_parent(source)
            assert landing == parent  # today's placement: under the failed parent
        sim.add(landing)
        _step(sim)


def test_counterexample_open_work_under_retired_ancestor() -> None:
    """Minimal counterexample: an open container below a closed ancestor.

    ``n1`` is closed over a settled (failed) child ``n2``. Today's
    ``node_creation._validate_parent`` refuses only a closed or archived
    *parent*, so a child can still be added under ``n2``, and again under that
    child. ``work_ids`` puts the whole branch out of the work, so the oracle's
    ``parent_ids`` does not count ``n3`` as a parent: ``n3`` is not a container
    and reads open. The model reads ``child_in_work`` from the row's direct
    children, so ``n3`` is a container and folded. The two disagree on ``open``.

    S1's refusal of a child under a settled parent closes this route; the
    historical rows are the backfill's to count (S0 found one open node under a
    settled ancestor).
    """
    sim = Sim(wild=True, hole=True)
    n1 = sim.add(None)
    n2 = sim.add(n1)
    sim.set(n2, "start", state="active")
    sim.set(n2, "fail", state="failed")
    sim.set(n1, "close", state="closed")
    check(sim)
    n3 = sim.add(n2)
    sim.add(n3)
    with pytest.raises(Disagreement) as caught:
        check(sim)
    assert caught.value.field in {"open", "sub_open", "own_folded", "folded"}


def test_folded_accepted_phase_counts_one_completed_at_its_level() -> None:
    """r4: fold rows count rows, not leaves. Five accepted children do not make five."""
    sim = _sim()
    root = sim.add(None)
    phase = sim.add(root)
    for _ in range(5):
        child = sim.add(phase)
        for step, state in (("start", "active"), ("review", "review_requested"), ("accept", "accepted")):
            sim.set(child, step, state=state)
    for step, state in (("start", "active"), ("review", "review_requested"), ("accept", "accepted")):
        sim.set(phase, step, state=state)
    _step(sim)
    assert sim.tree.nodes[phase].folded
    assert sim.tree.nodes[root].child_folded == Folded(done=1)
    assert sim.tree.nodes[phase].child_folded == Folded(done=5)
    assert sim.tree.top_folded == Folded(done=1)  # the pending root is a folded container: one row
    assert sim.tree.nodes[root].folded
    assert sim.tree.nodes[root].bucket == "done"


def test_failed_child_closed_while_folded_moves_failed_to_closed() -> None:
    """A tone change without an unfold: the bucket moves, the fold state does not."""
    sim = _sim()
    parent = sim.add(None)
    keep = sim.add(parent)
    failed = sim.add(parent)
    for node in (keep, failed):
        sim.set(node, "start", state="active")
    sim.set(keep, "review", state="review_requested")
    sim.set(keep, "accept", state="accepted")
    sim.set(failed, "fail", state="failed")
    _step(sim)
    assert sim.tree.nodes[parent].child_folded == Folded(done=1, failed=1)
    assert sim.tree.top_folded == Folded(failed=1)  # the container reads as its leaves: failed leads
    # failed -> closed is not legal in the state machine (the 49->50 and 56->57
    # migrations did it), but a row change is all the hook sees.
    sim.set(failed, "close", state="closed")
    _step(sim)
    assert sim.tree.nodes[parent].child_folded == Folded(done=1, closed=1)
    assert sim.tree.nodes[failed].folded
    assert sim.tree.top_folded == Folded(done=1)  # container tone follows its remaining leaves
