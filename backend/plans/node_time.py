"""Wall-clock time a roll-up counts once: Working and Waiting over a subtree.

Steps run and wait in parallel, so a parent's rolled time is not the sum of its
children's. A span carries merged, sorted intervals (epoch seconds):

- ``working``: every Turn interval of the node, its descendants and the plans
  attached beneath them;
- ``waiting``: every interval something waited on, over the same set.

Rolled Working is the length of the working union. Rolled Waiting is the
length of the waiting union minus the working union: time something waited
and nothing was working. Own figures stay per-node sums (``node_cost``).
Merging is linear in the intervals carried; it adds no query.
"""

from __future__ import annotations

from plans.node_waiting import Interval, subtract, union

Span = dict[str, list[Interval]]


def empty() -> Span:
    return {"working": [], "waiting": []}


def merge(spans: list[Span]) -> Span:
    return {
        "working": union([item for span in spans for item in span["working"]]),
        "waiting": union([item for span in spans for item in span["waiting"]]),
    }


def seconds(span: Span) -> tuple[int, int]:
    """``(working, waiting)`` whole seconds of one merged span."""
    # Whole seconds per interval, as a node's own figure floors each Turn, so
    # a node whose Turns never overlap reads the same rolled and own.
    working = sum(int(end - start) for start, end in span["working"])
    waited = subtract(span["waiting"], span["working"])
    return working, int(sum(end - start for start, end in waited))


def interval_count(span: Span) -> int:
    return len(span["working"]) + len(span["waiting"])
