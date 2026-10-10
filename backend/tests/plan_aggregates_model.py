"""In-memory model of the plan-aggregates propagation (r3/r4 section 4). Test helper.

Pure Python, no SQLite: a tree of rows, each carrying the stored aggregates the
design keeps per node, and the hook ``apply`` that keeps them exact on every row
change. Only row effects are reproduced (state, archived, parent, spawned worker,
sent-back flag); nothing here calls the plan service.

Names follow the design: ``contribution`` restates today's classification, the
node stores its own contribution and the sums of its descendants (``sub_*``),
and passes up two propagated vectors:

* the fold vector (``open``) always propagates, because folding ignores
  retirement;
* the work vector (``done``, ``failed``, ``closed``, ``in_work``) propagates only
  while the node is not retired; a retired node passes up its own contribution.

Fold rows (r4): each node also stores ``bucket``, the tone it shows once folded,
and ``child_folded``, the count of its folded direct children by tone. The plan's
root level has the same counter (``Tree.top_folded``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

TERMINAL = frozenset({"accepted", "accepted_with_followup", "failed", "closed"})
ACCEPTED = frozenset({"accepted", "accepted_with_followup"})


@dataclass
class Row:
    """The row facts that are aggregate inputs."""

    id: str
    parent_id: str | None
    state: str = "pending"
    archived: bool = False
    spawned: str | None = None
    sent_back: bool = False

    @property
    def retired(self) -> bool:
        return self.archived or self.state == "closed"


@dataclass(frozen=True)
class Work:
    done: int = 0
    failed: int = 0
    closed: int = 0
    in_work: int = 0

    def __add__(self, other: Work) -> Work:
        return Work(
            self.done + other.done, self.failed + other.failed,
            self.closed + other.closed, self.in_work + other.in_work,
        )

    def __sub__(self, other: Work) -> Work:
        return Work(
            self.done - other.done, self.failed - other.failed,
            self.closed - other.closed, self.in_work - other.in_work,
        )

    def zero(self) -> bool:
        return self == Work()


@dataclass(frozen=True)
class Folded:
    """Folded rows at one level, by the tone each shows (``foldLabel``)."""

    done: int = 0
    failed: int = 0
    closed: int = 0

    def __add__(self, other: Folded) -> Folded:
        return Folded(self.done + other.done, self.failed + other.failed, self.closed + other.closed)

    def __sub__(self, other: Folded) -> Folded:
        return Folded(self.done - other.done, self.failed - other.failed, self.closed - other.closed)

    def zero(self) -> bool:
        return self == Folded()


@dataclass(frozen=True)
class Own:
    """One node's own contribution (``contribution`` output)."""

    open: int = 0
    folded: bool = False
    container: bool = False
    work: Work = Work()


def contribution(row: Row, child_in_work: int) -> Own:
    """Restate today's classification for one row. An archived row contributes zero."""
    if row.archived:
        return Own()
    container = child_in_work > 0 and row.state == "pending" and row.spawned is None
    folded = row.state in TERMINAL or container
    leaf = child_in_work == 0 and not row.retired
    return Own(
        open=int(not folded),
        folded=folded,
        container=container,
        work=Work(
            done=int(leaf and row.state in ACCEPTED),
            failed=int(leaf and row.state == "failed"),
            closed=int(row.state == "closed" and not row.sent_back),
            in_work=int(not row.retired),
        ),
    )


@dataclass
class Node:
    row: Row
    child_in_work: int = 0
    own: Own = Own()
    sub_open: int = 0
    sub_work: Work = Work()
    #: The last value this node passed up: the baseline ``apply`` diffs against.
    prop_open: int = 0
    prop_work: Work = Work()
    #: Tone this row shows once folded: "done", "failed", "closed", or ``None``
    #: (not folded, archived, or a sent-back round, which the pane never lists).
    bucket: str | None = None
    #: Folded direct children at this node's level, by tone.
    child_folded: Folded = Folded()

    @property
    def folded(self) -> bool:
        return self.own.folded and self.sub_open == 0

    def fold_bucket(self) -> str | None:
        """The tone this row shows once folded, from its row and stored aggregates.

        Mirrors the server: a container reads as its leaves through
        ``operator_nodes.branch_display`` (the leading outcome: failed, else
        done, else empty, which reads closed); any other folded row shows its
        own terminal state (``TERMINAL_DISPLAY``), however many children it has.
        """
        row = self.row
        if row.archived or row.sent_back or not self.folded:
            return None
        if self.own.container:
            if self.sub_work.failed:
                return "failed"
            return "done" if self.sub_work.done else "closed"
        if row.state in ACCEPTED:
            return "done"
        return "failed" if row.state == "failed" else "closed"


@dataclass
class Tree:
    nodes: dict[str, Node] = field(default_factory=dict)
    top_open: int = 0
    top_work: Work = Work()
    top_folded: Folded = Folded()

    def insert(self, row: Row) -> None:
        self.nodes[row.id] = Node(row)
        apply(self, row.id)

    def change(self, node_id: str, **fields: object) -> None:
        """Write row facts, then run the hook, as ``touch_node`` will."""
        row = self.nodes[node_id].row
        for name, value in fields.items():
            setattr(row, name, value)
        apply(self, node_id)


def _propagated(node: Node) -> tuple[int, Work]:
    open_ = node.own.open + node.sub_open
    work = node.own.work if node.row.retired else node.own.work + node.sub_work
    return open_, work


def _weight(bucket: str | None) -> Folded:
    return Folded(
        done=int(bucket == "done"), failed=int(bucket == "failed"), closed=int(bucket == "closed"),
    )


def _rebucket(node: Node) -> Folded:
    """Re-derive the node's tone; return the change to its parent's level counters."""
    old, node.bucket = node.bucket, node.fold_bucket()
    return _weight(node.bucket) - _weight(old)


def apply(tree: Tree, node_id: str) -> None:
    """Recompute ``node_id``'s contribution and push the change to the root.

    The delta is the new propagated value minus the stored one, never a
    recomputation of the old value. At each ancestor the direct-child count is
    adjusted and the ancestor's own contribution recomputed *before* the zero
    test, since its leafness or container status can change with a zero
    incoming delta.

    Fold-row counters: every visited node re-derives its ``bucket``; any change
    (folding, unfolding, or failed -> closed while staying folded) adjusts its
    parent's ``child_folded`` at the next step. That adjustment is pending work,
    so the zero test includes it: a node whose propagated value did not move can
    still have changed tone (a closed node's sub_work moves a container's tone
    without moving what it passes up).
    """
    node = tree.nodes[node_id]
    was_in_work = node.own.work.in_work
    node.own = contribution(node.row, node.child_in_work)
    child_delta = node.own.work.in_work - was_in_work
    open_, work = _propagated(node)
    d_open, d_work = open_ - node.prop_open, work - node.prop_work
    node.prop_open, node.prop_work = open_, work
    d_folded = _rebucket(node)
    cursor = node.row.parent_id
    while d_open or not d_work.zero() or child_delta or not d_folded.zero():
        if cursor is None:
            tree.top_open += d_open
            tree.top_work = tree.top_work + d_work
            tree.top_folded = tree.top_folded + d_folded
            return
        parent = tree.nodes[cursor]
        parent.child_in_work += child_delta
        child_delta = 0
        parent.child_folded = parent.child_folded + d_folded
        parent.sub_open += d_open
        parent.sub_work = parent.sub_work + d_work
        parent.own = contribution(parent.row, parent.child_in_work)
        open_, work = _propagated(parent)
        d_open, d_work = open_ - parent.prop_open, work - parent.prop_work
        parent.prop_open, parent.prop_work = open_, work
        d_folded = _rebucket(parent)
        cursor = parent.row.parent_id
