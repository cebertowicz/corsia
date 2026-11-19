"""Stored per-node usage groups equal the ledger after every kind of write, and readers serve the same figures."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.measurement_model_cost import query_model_costs
from api.measurement_scope import resolve_grain
from core import node_usage_groups as groups
from core.cost_identity import IdentityResolver
from infra.turn_usage_receipts import attribute_unbound_turn_usage
from model_identity_fixtures import seed_identity_catalog
from plans import commitment_cost, usage_cost
from tests.plan_writer_authorizer import raw_plan_seed

GRAIN = resolve_grain("weeks", "UTC", now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
SCOPE = {"scope_project": "all", "scope_agent": None, "user_id": "test-user"}
RECEIPT = "0123456789abcdef01234{:03d}"  # 24 hex: a Codex receipt id proves the token contract


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    connection.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_status,created_at,updated_at) VALUES ('root','test-user','root',1,'active','t','t')")
    connection.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES ('plan','thread','root','active',1,'t','t')")
    seed_identity_catalog(connection)
    connection.commit()
    groups.ensure(connection)
    try:
        yield connection
    finally:
        connection.close()


def _node(db, node: str, *, accepted_at: str | None = None, parent: str | None = None) -> None:
    with raw_plan_seed(db):
        db.execute(
            "INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,commitment_id,"
            "requirements_revision,revision,created_at,updated_at) VALUES (?, 'plan',?,1,'task','task','done',?,?,1,1,'2026-09-28T00:00:00+00:00','2026-09-30T00:00:00+00:00')",
            (node, parent, "accepted" if accepted_at else "active", node))
        if accepted_at:
            db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                       "VALUES (?,?,'test','accepted','active','accepted',?)", (f"accept-{node}", node, accepted_at))
    db.commit()


_COUNT = iter(range(1000))


def _usage(db, node: str | None, *, model: str = "claude-code/opus", source: str = "external_harness:claude-code",
           at: str = "2026-09-29T12:00:00Z", prompt=1000, completion=10, cache_read=0, total="sum", turn: str | None = "t1",
           manifest: str | None = None, usage_id: str | None = None, commit: bool = True) -> str:
    usage_id = usage_id or f"u-{next(_COUNT)}"
    if total == "sum":
        total = None if None in (prompt, completion) else prompt + completion + cache_read
    plan = ("plan", node, "attempt", node, "execution") if node else (None,) * 5
    db.execute(
        "INSERT INTO llm_usage (id,thread_id,turn_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,"
        "cache_read_tokens,total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,"
        "plan_usage_role,context_manifest_id) VALUES (?, 'root',?,?,?,?,0,?,?,?,?,?,?,?,?,?,?)",
        (usage_id, turn, model, prompt, completion, cache_read, total, source, at, *plan, manifest))
    if commit:
        db.commit()
    return usage_id


def _figures(db, nodes: list[str]):
    dollars = usage_cost.usage_dollars(db, {"plan_node_id": nodes, "plan_commitment_id": nodes})
    own = usage_cost.own_usage(db, nodes, None, lambda c, sql, ids: [dict(r) for r in c.execute(
        sql.format(marks=",".join("?" * len(ids))), ids).fetchall()])
    commitments = commitment_cost.project_many(nodes, db=db, now=datetime(2026, 10, 5, tzinfo=timezone.utc))
    costs = query_model_costs(db, {**SCOPE, "grain": GRAIN}, IdentityResolver(db))
    for row in costs:
        row["identity"] = repr(row["identity"])
    return tuple(repr(part) for part in (dollars, own, commitments, costs))


def _assert_exact(db, monkeypatch, nodes: list[str]) -> None:
    assert groups.recount(db) == []
    assert groups.ready(db).complete
    with monkeypatch.context() as patch:
        patch.setattr(groups, "ready", lambda _db: groups.NO_COVERAGE)
        ledger = _figures(db, nodes)
    assert _figures(db, nodes) == ledger


def _mixed(db) -> list[str]:
    """Rows of every shape a group keys or sums on."""
    _node(db, "a", accepted_at="2026-09-30T00:00:00Z")
    _node(db, "b", accepted_at="2026-09-30T00:00:00Z")
    _usage(db, "a")
    _usage(db, "a", completion=30)  # same group
    _usage(db, "a", turn=None)  # Turn-less
    _usage(db, "a", prompt=None, total=None)  # tokens unreported
    _usage(db, "a", total=7)  # total disagrees
    _usage(db, "a", total="12.5")  # an odd stored total
    _usage(db, "a", prompt=300_000)  # long-context tier
    _usage(db, "a", model="codex/gpt-5.6-terra", source="external_harness:codex", cache_read=50)  # contract unproven
    _usage(db, "b", model="codex/gpt-5.6-terra", source="external_harness:codex", usage_id=RECEIPT.format(1))
    _usage(db, "b", manifest="manifest-1")
    _usage(db, "b", at="2026-10-01T00:00:00Z")  # after its settlement
    _usage(db, None)  # unattributed: not stored
    return ["a", "b"]


def test_inserts_of_every_shape_equal_the_ledger(db, monkeypatch):
    nodes = _mixed(db)
    assert db.execute("SELECT COUNT(*) FROM llm_usage_node_groups WHERE plan_node_id IS NULL").fetchone()[0] == 0
    _assert_exact(db, monkeypatch, nodes)
    # The readers really read the groups: each figure moves when only the stored rows do.
    before = _figures(db, nodes)
    db.execute("UPDATE llm_usage_node_groups SET records=records+1,total_tokens=total_tokens+1,"
               "prompt_tokens=prompt_tokens+1000 WHERE admission='ok'")
    after = _figures(db, nodes)
    assert all(old != new for old, new in zip(before, after))
    assert groups.recount(db) != []
    db.rollback()


def test_a_late_insert_lands_in_its_group_and_the_settlement_cut_reads_the_ledger(db, monkeypatch):
    nodes = _mixed(db)
    _usage(db, "a", at="2026-09-01T00:00:00Z")  # written late, created early
    _usage(db, "a", at="2026-10-02T00:00:00Z")  # after settlement: node a now falls back
    _assert_exact(db, monkeypatch, nodes)


def test_attribute_unbound_turn_usage_moves_rows_into_the_node(db, monkeypatch):
    nodes = _mixed(db)
    for _ in range(3):
        _usage(db, None, turn="turn-late")
    attribute_unbound_turn_usage(db, thread_id="root", turn_id="turn-late", binding={
        "plan_id": "plan", "plan_node_id": "b", "plan_attempt_id": "attempt",
        "plan_commitment_id": "b", "plan_usage_role": "execution"})
    db.commit()
    assert db.execute("SELECT SUM(records) FROM llm_usage_node_groups WHERE turn_id='turn-late'").fetchone()[0] == 3
    _assert_exact(db, monkeypatch, nodes)


def test_attribution_and_token_changes_move_the_contribution(db, monkeypatch):
    nodes = _mixed(db)
    db.execute("UPDATE llm_usage SET plan_node_id='b',plan_commitment_id='b' WHERE plan_node_id='a' AND turn_id IS NULL")
    db.execute("UPDATE llm_usage SET completion_tokens=completion_tokens+5,total_tokens=total_tokens+5 "
               "WHERE plan_node_id='b' AND total_tokens IS NOT NULL")
    db.execute("UPDATE llm_usage SET created_at='2026-09-29T13:00:00Z' WHERE plan_node_id='a'")
    db.commit()
    _assert_exact(db, monkeypatch, nodes)


def test_a_node_move_changes_no_stored_row(db, monkeypatch):
    nodes = _mixed(db)
    _node(db, "c")
    before = db.execute("SELECT * FROM llm_usage_node_groups ORDER BY rowid").fetchall()
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET parent_id='c' WHERE id='a'")
    db.commit()
    assert db.execute("SELECT * FROM llm_usage_node_groups ORDER BY rowid").fetchall() == before
    _assert_exact(db, monkeypatch, [*nodes, "c"])


def test_repricing_is_applied_at_read(db, monkeypatch):
    nodes = _mixed(db)
    from llm import api_equivalent

    real = api_equivalent.api_equivalent
    monkeypatch.setattr("core.measurement_pricing.api_equivalent", lambda *a, **k: (real(*a, **k) or 0) * 3)
    _assert_exact(db, monkeypatch, nodes)


def test_deletes_including_one_fired_by_another_write(db, monkeypatch):
    nodes = _mixed(db)
    db.execute("DELETE FROM llm_usage WHERE plan_node_id='a' AND turn_id IS NULL")
    # A foreign-key action runs as a nested trigger program. No foreign key cascades into llm_usage today
    # (PRAGMA foreign_key_list shows only vendor_accounts, with no action), so the nested path is driven
    # by a trigger on another table, the same way an action would reach it.
    assert [row[6] for row in db.execute("PRAGMA foreign_key_list(llm_usage)")] == ["NO ACTION"]
    db.execute("CREATE TEMP TABLE usage_owner (node_id TEXT)")
    db.execute("INSERT INTO usage_owner VALUES ('b')")
    db.execute("CREATE TEMP TRIGGER cascade_usage AFTER DELETE ON usage_owner "
               "BEGIN DELETE FROM llm_usage WHERE plan_node_id=OLD.node_id; END")
    db.execute("DELETE FROM usage_owner")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM llm_usage_node_groups WHERE plan_node_id='b'").fetchone()[0] == 0
    db.execute("DROP TABLE temp.usage_owner")
    _assert_exact(db, monkeypatch, nodes)


def test_a_changed_definition_rebuilds_in_node_batches_and_reads_stay_exact_part_way(db, monkeypatch):
    nodes = _mixed(db)
    _usage(db, "c")  # a node with no plan row still has groups
    db.execute("UPDATE llm_usage_node_state SET definition='old'")
    db.execute("DROP TRIGGER llm_usage_node_groups_insert")
    db.execute("DELETE FROM llm_usage_node_groups WHERE plan_node_id='a'")  # drift the old rows
    db.commit()
    assert groups.ready(db) == groups.NO_COVERAGE
    with monkeypatch.context() as patch:
        patch.setattr(groups, "ready", lambda _db: groups.NO_COVERAGE)
        ledger = _figures(db, nodes)
    assert _figures(db, nodes) == ledger
    # Stop part way: only node a's batch is rebuilt.
    monkeypatch.setattr(groups, "REBUILD_ROWS", 3)
    groups._adopt(db)
    groups._rebuild_batch(db, "", "b")
    assert groups.ready(db) == groups.Coverage(False, "b")
    assert groups.recount(db, ["a"]) == []
    assert _figures(db, nodes) == ledger
    result = groups.ensure(db)
    assert result["rebuilt"] and result["batches"] >= 2
    assert groups.ensure(db) == {"rebuilt": False, "reason": "current"}
    _assert_exact(db, monkeypatch, nodes)


def test_missing_triggers_are_replaced_at_start(db):
    _mixed(db)
    db.execute("DROP TRIGGER llm_usage_node_groups_delete")
    db.commit()
    assert groups.ensure(db)["rebuilt"]
    assert set(groups._installed(db)) == set(groups.TRIGGERS)
    db.execute("DELETE FROM llm_usage WHERE plan_node_id='a'")
    db.commit()
    assert groups.recount(db) == []


def test_where_to_look_probes_each_chain_node_by_key_on_both_paths(db):
    """Covered or not, each chain row is one key probe of the stored groups and one of the ledger, never a scan."""
    from api.measurement_model_cost import _cost_sql
    from api.measurement_prepared import CHAIN_DDL, CHAIN_TABLE

    db.execute(CHAIN_DDL)
    for coverage in ({"groups_complete": 1, "groups_below": ""}, {"groups_complete": 0, "groups_below": ""}):
        details = [str(row[-1]) for row in db.execute("EXPLAIN QUERY PLAN " + _cost_sql(CHAIN_TABLE), coverage)]
        reads = sorted(detail for detail in details if detail.startswith(("SEARCH g ", "SEARCH l ", "SEARCH u ", "SCAN g", "SCAN l", "SCAN u")))
        assert reads == [
            "SEARCH g USING INDEX idx_llm_usage_node_groups_key (plan_node_id=?)",
            "SEARCH l USING INDEX idx_llm_usage_node_groups_key (plan_node_id=?)",
            "SEARCH u USING INDEX idx_llm_usage_plan_node (plan_node_id=?)",
        ], details
