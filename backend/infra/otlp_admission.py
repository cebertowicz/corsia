"""What the OTLP receiver does with decoded records: stamp identity from the key, decide, and admit.

Every record goes through ``infra.measure_admission.admit`` (or event intake for a ``route`` or
``both`` type), the one admission path. The server stamps the project and agent from the key, and the
workflow ref and version from the plan node the record's run id names or, with no plan node, from the
agent's own workflow in Git. The payload may only repeat those values: one that names another
project, agent or plan node, a workflow the agent does not own, or a different workflow version is
refused as an identity mismatch. A run id that is not a plan node, the subject and the time are the
producer's own labels (for example a CI run id) and are kept as sent. Lookups here only read, so the receiver runs them on the read lane; the writes
come last, in transactions of at most :data:`WRITE_CHUNK` rows.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from infra.database import get_db, write_transaction
from infra.measure_admission import _KIND_RULES, UNNAMED_LINE, admit
from infra.measure_registry import REGISTRY
from infra.otlp_decode import WireRecord
from infra.outcome_event_attributes import AttributeSpec
from infra.outcome_events import count_outcome_event

WRITE_CHUNK = 100
SOURCE = "otlp"
PROVENANCE = "agent_recorded"
_NODE_SQL = (
    "SELECT n.id, n.workflow_ref, n.workflow_version, "
    "(p.owner_kind='thread' AND (p.owner_id=? OR EXISTS "
    "(SELECT 1 FROM threads t WHERE t.id=p.owner_id AND t.spawned_by=?))) "
    "FROM plan_nodes n JOIN plans p ON p.id=n.plan_id WHERE n.id IN ({marks})"
)
_IDENTITY_KEYS = ("corsia.project_id", "corsia.agent_thread_id")
# The stamps a record may repeat; every other attribute, corsia.* included, goes to admit() as sent.
_STAMP_KEYS = frozenset((*_IDENTITY_KEYS, "corsia.run_id", "corsia.workflow.ref", "corsia.workflow.version", "corsia.subject"))


@dataclass(frozen=True)
class NodeStamp:
    own: bool  # on the stamped agent's plan, or the plan of a worker it spawned
    workflow_ref: str | None
    workflow_version: str | None


@dataclass(frozen=True)
class Lookup:
    project_id: str | None
    nodes: dict[str, NodeStamp]  # the run ids that are real plan nodes
    uses: dict[str, str]  # the agent's own declared event names and their use


@dataclass(frozen=True)
class Item:
    """One record, decided: refused with a counter and text, or admitted with the stamps the server resolved."""

    name: str | None
    refusal: tuple[str, str] | None = None  # (counter, text)
    route: bool = False
    attributes: dict[str, Any] | None = None
    ts: str | None = None
    subject: str | None = None
    run_id: str | None = None
    workflow_ref: str | None = None
    workflow_version: str | None = None


def _claimed(record: WireRecord, key: str) -> Any:
    """What the payload says for ``key``: the record's own value first, else its resource's."""
    return record.attributes.get(key, record.resource.get(key))


def read_lookup(db: sqlite3.Connection, agent: str, records: list[WireRecord]) -> Lookup:
    """Everything the decision needs from the database, in one read."""
    row = db.execute("SELECT project_id FROM threads WHERE id=?", (agent,)).fetchone()
    state = REGISTRY.ensure_loaded(db)
    uses = {name: event.use for name, event in state.events.items() if event.owner == agent}
    run_ids = sorted({rid for r in records if isinstance(rid := _claimed(r, "corsia.run_id"), str)})
    nodes: dict[str, NodeStamp] = {}
    for start in range(0, len(run_ids), 500):
        part = run_ids[start:start + 500]
        sql = _NODE_SQL.format(marks=",".join("?" * len(part)))
        for node_id, ref, version, own in db.execute(sql, [agent, agent, *part]):
            nodes[node_id] = NodeStamp(bool(own), ref, version)
    return Lookup(row[0] if row and row[0] else None, nodes, uses)


def workflow_stamps(agent: str, records: list[WireRecord]) -> dict[str, tuple[str, str | None] | None]:
    """The (ref, current version) of each workflow ref a record names, None when the agent doesn't own it.

    Reads Git for the version, so it runs off the database pool, on this worker thread's pooled connection.
    """
    refs = {ref for r in records if isinstance(ref := _claimed(r, "corsia.workflow.ref"), str)}
    if not refs:
        return {}
    from tools.workflow_owners import workflow_stamp

    db = get_db()
    return {ref: workflow_stamp(agent, ref, db) for ref in refs}


def _mismatch(name: str, what: str) -> Item:
    return Item(name, ("identity_mismatch", f"{name}: {what}; the record was dropped. Remove it: the key already says who sends."))


def prepare(agent: str, records: list[WireRecord], lookup: Lookup, workflows: dict[str, Any]) -> list[Item]:
    """Decide every record without touching the database."""
    return [_decide(agent, record, lookup, workflows) for record in records]


def _decide(agent: str, record: WireRecord, lookup: Lookup, workflows: dict[str, Any]) -> Item:
    name = record.attributes.get("event.name")
    if not isinstance(name, str) or not name:
        return Item(None, ("record_refused", "an OTLP log record without an event.name was refused; only declared events are accepted, and plain logs are not stored here"))
    stamped = {"corsia.project_id": lookup.project_id, "corsia.agent_thread_id": agent}
    for key in _IDENTITY_KEYS:
        if (value := _claimed(record, key)) is not None and value != stamped[key]:
            return _mismatch(name, f"{key} does not match the {'project' if 'project' in key else 'agent'} this key is bound to")
    run_id = _claimed(record, "corsia.run_id")
    run_id = run_id if isinstance(run_id, str) else None
    ref = version = None
    node = lookup.nodes.get(run_id) if run_id else None
    if node is not None:
        if not node.own:
            return _mismatch(name, "corsia.run_id names a plan node outside this agent's plans and its workers' plans")
        ref, version = node.workflow_ref, node.workflow_version
    elif isinstance(claimed_ref := _claimed(record, "corsia.workflow.ref"), str):
        current = workflows.get(claimed_ref)
        if current is None:
            return _mismatch(name, "corsia.workflow.ref is not a workflow this agent owns")
        claimed_version = _claimed(record, "corsia.workflow.version")
        if claimed_version is not None and claimed_version != current[1]:
            return _mismatch(name, "corsia.workflow.version is not the version CORSIA stamps for this workflow")
        ref, version = claimed_ref, current[1]
    subject = _claimed(record, "corsia.subject")
    if subject is not None and not (isinstance(subject, str) and AttributeSpec("id").accepts(subject)):
        return Item(name, ("record_refused", f"{name}: corsia.subject is not an id ({_KIND_RULES['id']}); the record was dropped. Send an id, or leave corsia.subject out."))
    attributes = {k: v for k, v in record.attributes.items() if k != "event.name" and k not in _STAMP_KEYS}
    return Item(
        name, route=lookup.uses.get(name, "record") != "record", attributes=attributes, ts=record.ts,
        subject=subject, run_id=run_id,
        workflow_ref=ref, workflow_version=version,
    )


def _label(item: Item, lookup_uses: dict[str, str]) -> str:
    """The counter's event name: the agent's own declared name, else a fixed label, so a payload never mints counter rows."""
    return item.name if item.name in lookup_uses else UNNAMED_LINE


def write_batch(agent: str, project_id: str | None, uses: dict[str, str], items: list[Item]) -> dict[int, str]:
    """Count the refused records and admit the rest in short transactions; returns each refusal's text by position.

    ``route`` records are not written here: event intake takes them one by one.
    """
    refused: dict[int, str] = {}
    pending = [(i, item) for i, item in enumerate(items) if not item.route]
    for start in range(0, len(pending), WRITE_CHUNK):
        with write_transaction(None, scope="otlp_intake") as db:
            for position, item in pending[start:start + WRITE_CHUNK]:
                text = _admit_one(db, agent, project_id, uses, item)
                if text is not None:
                    refused[position] = text
    return refused


def _admit_one(db: sqlite3.Connection, agent: str, project_id: str | None, uses: dict[str, str], item: Item) -> str | None:
    if item.refusal is not None:
        counter, text = item.refusal
        count_outcome_event(db, counter, _label(item, uses), agent_thread_id=agent)
        REGISTRY.note(agent, text)
        return text
    assert item.name is not None and item.attributes is not None
    admission = admit(
        db, owner_thread_id=agent, event_name=item.name, attributes=item.attributes,
        source=SOURCE, provenance=PROVENANCE, subject_key=item.subject, project_id=project_id,
        run_id=item.run_id, workflow_ref=item.workflow_ref, workflow_version=item.workflow_version, ts=item.ts,
    )
    return admission.refusal
