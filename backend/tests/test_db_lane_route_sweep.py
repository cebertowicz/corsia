"""Every route moved to the read or bulk lane, run once on real lane connections over seeded data.

The lanes are the real ``_read_pool``/``_bulk_pool`` threads, so each job opens its own
lane-marked connection to a file-backed database with the production schema. A recorder
on those connections sees each statement as it starts, every authorizer denial and every
``write_transaction`` refusal, so a write the route swallows still fails the sweep.
"""

from __future__ import annotations

import json
import re
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

from infra import database, db_connection
from infra.database import get_db
from infra.db_async import run_bulk, run_in_db, run_read
from infra.db_connection import LaneWriteError
from tests.plan_writer_authorizer import raw_plan_seed

_USER = {"id": "steve", "token_kind": "session"}
_WEEK = "2026-09-23T12:00:00Z"
_TARGET = r"(?:main\.|temp\.)?\"?(\w+)\"?"
_TARGETED = re.compile(
    rf"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|DELETE\s+FROM|UPDATE(?:\s+OR\s+\w+)?)\s+{_TARGET}", re.I)
_TEMP_TABLE = re.compile(rf"\bCREATE\s+TEMP(?:ORARY)?\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_TARGET}", re.I)
_TEMP_DROP = re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?temp\.\"?(\w+)\"?", re.I)
_OTHER_WRITE = re.compile(
    r"\b(CREATE|DROP|ALTER|ATTACH|DETACH|VACUUM|REINDEX)\b|\bBEGIN\s+(IMMEDIATE|EXCLUSIVE)\b|\bPRAGMA\s+\w+\s*=", re.I)


@pytest.fixture
def lane_record(monkeypatch):
    """Statements, denials and refusals seen on read- and bulk-lane connections."""
    record = {"statements": [], "denied": [], "refused": []}
    apply_lane, refuse = database.apply_thread_lane, database.refuse_lane_write

    def traced(conn):
        apply_lane(conn)
        lane = conn.db_lane
        if lane is None:
            return
        conn.set_trace_callback(lambda sql: record["statements"].append((lane, sql)))
        if lane == "bulk":
            def authorize(*args):
                verdict = db_connection._bulk_authorizer(*args)
                if verdict != sqlite3.SQLITE_OK:
                    record["denied"].append(args[:4])
                return verdict
            conn.set_authorizer(authorize)

    def refusing(conn, scope):
        try:
            refuse(conn, scope)
        except LaneWriteError:
            record["refused"].append(scope)
            raise

    monkeypatch.setattr(database, "apply_thread_lane", traced)
    monkeypatch.setattr(database, "refuse_lane_write", refusing)
    return record


def lane_writes(record) -> list[tuple[str, str]]:
    """Every write a lane connection attempted; only bulk TEMP tables are exempt.

    Bulk may create a TEMP table, write it and drop it by its ``temp.`` name; an unqualified
    DROP, or one of a table bulk never created as TEMP, still counts."""
    temp = {match.group(1).lower() for lane, sql in record["statements"] if lane == "bulk"
            for match in _TEMP_TABLE.finditer(sql)}

    def without_temp(sql: str) -> str:
        sql = _TEMP_TABLE.sub("", sql)
        return _TEMP_DROP.sub(lambda match: "" if match.group(1).lower() in temp else match.group(0), sql)

    writes = []
    for lane, sql in record["statements"]:
        exempt = lane == "bulk"
        targets = [match.group(1).lower() for match in _TARGETED.finditer(sql)]
        other = _OTHER_WRITE.search(without_temp(sql) if exempt else sql)
        if other or any(not (exempt and target in temp) for target in targets):
            writes.append((lane, sql))
    return writes + [("bulk", f"denied {args}") for args in record["denied"]] + [
        ("lane", f"write_transaction {scope}") for scope in record["refused"]]


def _seed() -> None:
    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('proj','Project',?,?)", (_WEEK, _WEEK))
        for thread_id, parent, model in (("root", None, "m"), ("child", "root", "m"),
                                         ("codex", None, "codex/gpt-5.6-terra"), ("conductor", None, "m")):
            db.execute(
                "INSERT INTO threads (id,user_id,title,model,project_id,parent_thread_id,agent_enabled,"
                "agent_status,created_at,updated_at) VALUES (?,'steve',?,?,'proj',?,1,'active',?,?)",
                (thread_id, thread_id, model, parent, "2026-09-01T00:00:00Z", _WEEK))
            db.execute("INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
                       "VALUES (?,?,'user','hello',?,1)", (f"msg-{thread_id}", thread_id, _WEEK))
        db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) "
                   "VALUES ('plan-root','thread','root','active',1,?,?)", (_WEEK, _WEEK))
        for number, state in enumerate(("accepted", "accepted", "active", "pending")):
            node_id = f"node-{number}"
            with raw_plan_seed(db):
                db.execute(
                    "INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,"
                    "commitment_id,requirements_revision,revision,created_at,updated_at) "
                    "VALUES (?,'plan-root',NULL,?,'task','Measured task','done',?,?,1,1,?,?)",
                    (node_id, number, state, node_id, _WEEK, _WEEK))
            if state == "accepted":
                db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,"
                           "created_at) VALUES (?,?,'test','accepted','active','accepted',?)",
                           (f"event-{number}", node_id, _WEEK))
        db.execute("INSERT INTO context_manifests (id,harness,entries_json,entry_count,created_at) "
                   "VALUES ('manifest','native','[]',0,?)", (_WEEK,))
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,prompt_tokens,completion_tokens,"
            "cache_creation_tokens,cache_read_tokens,cost_usd,source,created_at,context_manifest_id) "
            "VALUES ('usage-1','root','test',30,10,20,0,0,0.25,'test',?,'manifest')", (_WEEK,))
        decision = {"thread_id": "root", "raised_at": "2026-09-15T12:00:00Z", "answered_at": _WEEK,
                    "state": "answered", "recommendation": "B", "answer": {"option_id": "B"}}
        db.execute("INSERT INTO artifact_identities (id,integration,type,external_id,metadata,created_at,"
                   "updated_at) VALUES ('decision-1','local','plan-decision','decision-1',?,?,?)",
                   (json.dumps({"plan_decision": decision}), _WEEK, _WEEK))
        db.commit()
    finally:
        db.close()
    from infra.conductor import set_conductor

    set_conductor("workspace", "conductor")


async def _read_routes(monkeypatch) -> None:
    from api import conductor, model_account_scope, models, plans, projects, thread_messages, threads
    from api import vendor_accounts
    from connections import harness_refresh
    from infra.runtime_truth import read_runtime_truth

    for module in (conductor, model_account_scope, projects, thread_messages, threads):
        monkeypatch.setattr(module, "authenticate", lambda _token: _USER)
    monkeypatch.setattr(harness_refresh, "schedule_stale_refresh", lambda: None)
    request = SimpleNamespace(query_params={"token": "tok"}, headers={})

    assert (await threads.list_threads("tok"))["threads"]
    assert (await projects.list_projects("tok"))["count"] == 1
    await thread_messages.get_messages("root", "tok", limit=20)
    await conductor.get_conductor_route("tok", scope="workspace")
    assert await models.list_models()
    for scope in ({"project_id": "proj"}, {"thread_id": "root"}):
        assert await models.list_models(request=request, **scope)
    assert (await vendor_accounts.list_vendor_accounts())["accounts"]
    await plans.read_activity_workspace(Response(), None, _USER)
    await plans.read_activity_project("proj", Response(), None, _USER)
    await plans.read_operator_workspace(Response(), None, _USER)
    await plans.read_operator_project("proj", Response(), None, _USER)
    await _found(plans.read_operator_thread("root", Response(), None, _USER))
    await _found(plans.read_operator_child("node-2", Response(), None, _USER))
    await plans.read_operator_thread_states("root,child", _USER)
    for thread_id in ("root", "codex"):
        row = await run_read(_thread_row, thread_id)
        assert isinstance(await read_runtime_truth(thread_id, row), dict)


async def _found(read) -> None:
    """Await one plan read; an unavailable target is a 404 read result, not a lane failure."""
    try:
        await read
    except HTTPException as error:
        assert error.status_code == 404


def _thread_row(thread_id: str) -> dict:
    db = get_db()
    try:
        return dict(db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone())
    finally:
        db.close()


async def _bulk_routes(tmp_path, monkeypatch) -> None:
    from api import costs, measurement
    from api.measurement_limits import measurement_limits
    from api.measurement_recent import measurement_key_figures
    from core.lesson_report_access import report_for_identity

    monkeypatch.setattr(costs, "authenticate", lambda _token: _USER)
    for project, agent in (("all", "none"), ("proj", "root")):
        await costs.cost_summary("tok", project=project, agent=agent)
        await measurement.measurement_index(project=project, agent=agent, user=_USER)
        ids = ",".join(name for name, metric in measurement.REGISTRY.items() if metric.available)
        await measurement.measurement_series(ids=ids, project=project, agent=agent, grain="weeks", timezone_name="UTC", user=_USER)
        await measurement.measurement_agents(project=project, agent=agent, timezone_name="UTC", user=_USER)
        await measurement.measurement_models(project=project, agent=agent, timezone_name="UTC", user=_USER)
        await _measurement_cost()(project=project, agent=agent, grain="weeks", timezone_name="UTC", user=_USER)
        await measurement_limits(project=project, agent=agent, grain="weeks", timezone_name="UTC", user=_USER)
        await measurement_key_figures(project=project, agent=agent, timezone_name="America/Los_Angeles",
                                      user=_USER)
    await report_for_identity(user_id="steve", caller_thread_id="root", workspace=tmp_path)
    await report_for_identity(user_id="steve", scope="all", workspace=tmp_path)


def _measurement_cost():
    from api import measurement

    return next(route.endpoint for route in measurement.router.routes
                if getattr(route, "path", "").endswith("/cost"))


@pytest.mark.asyncio
async def test_every_migrated_route_reads_without_a_write_on_its_lane(isolated_db, tmp_path, monkeypatch,
                                                                     lane_record):
    await run_in_db(_seed)

    await _read_routes(monkeypatch)
    await _bulk_routes(tmp_path, monkeypatch)

    lanes = {lane for lane, _ in lane_record["statements"]}
    assert lanes == {"read", "bulk"}, "the recorder must see both lanes, or the sweep proves nothing"
    assert lane_writes(lane_record) == []


def test_only_a_bulk_temp_table_bulk_created_may_be_dropped():
    created = ("bulk", "CREATE TEMP TABLE chain AS SELECT 1")
    assert lane_writes({"statements": [created, ("bulk", "DROP TABLE IF EXISTS temp.chain")],
                        "denied": [], "refused": []}) == []
    for lane, sql in (("bulk", "DROP TABLE IF EXISTS temp.other"), ("bulk", "DROP TABLE chain"),
                      ("read", "DROP TABLE IF EXISTS temp.chain")):
        assert lane_writes({"statements": [created, (lane, sql)], "denied": [], "refused": []}) == [(lane, sql)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("lane", "sql"), [
    ("read", "UPDATE threads SET title='swallowed' WHERE id='root'"),
    ("bulk", "INSERT INTO settings (key, value) VALUES ('swallowed', 'x')"),
])
async def test_the_sweep_catches_a_write_its_route_swallows(isolated_db, lane_record, lane, sql):
    await run_in_db(_seed)

    def writes_and_hides_it():
        db = get_db()
        try:
            db.execute(sql)
        except sqlite3.DatabaseError:
            pass
        finally:
            db.close()

    await {"read": run_read, "bulk": run_bulk}[lane](writes_and_hides_it)

    assert [found for found in lane_writes(lane_record) if found[0] == lane]


@pytest.mark.asyncio
async def test_the_sweep_catches_a_swallowed_write_transaction(isolated_db, lane_record):
    def opens_a_write():
        try:
            with database.write_transaction(None, scope="swallowed"):
                pass
        except LaneWriteError:
            pass

    await run_read(opens_a_write)

    assert ("lane", "write_transaction swallowed") in lane_writes(lane_record)


def test_bulk_temp_table_writes_are_exempt_and_main_writes_are_not():
    record = {"denied": [], "refused": [], "statements": [
        ("bulk", "CREATE TEMP TABLE IF NOT EXISTS scratch (id TEXT)"),
        ("bulk", "WITH x AS (SELECT 1) INSERT INTO scratch(id) SELECT 1 FROM x"),
        ("bulk", "DELETE FROM scratch"),
        ("bulk", "DELETE FROM threads"),
        ("read", "SELECT updated_at, deleted_at FROM threads"),
    ]}

    assert lane_writes(record) == [("bulk", "DELETE FROM threads")]

