"""Real report adapters preserve identity, filters, pagination and read-only state."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import sqlite3
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from plans import lesson_reviews
from tests.test_lesson_review_history import review


@pytest.fixture
def report_target(isolated_db, tmp_path, monkeypatch):
    from api import measurement
    from tools import lesson_report
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(measurement, "WORKSPACE", workspace)
    db = sqlite3.connect(isolated_db)
    db.row_factory = sqlite3.Row
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES('project','P','t','t')")
    db.execute("INSERT INTO artifact_identities(id,integration,type,external_id,title,state,created_at,updated_at) "
               "VALUES('artifact','workspace','skill','workspace://artifact','Lesson','active','t','t')")
    for owner, user, parent in (("owner", "user", None), ("child", "user", "owner"),
                                ("foreign", "other-user", "owner"), ("orphan", "user", "deleted")):
        db.execute("INSERT INTO threads(id,user_id,project_id,parent_thread_id,agent_enabled,model,created_at,updated_at) "
                   "VALUES(?,?,'project',?,1,'test/model','t','t')", (owner, user, parent))
        review(db, owner=owner, subject=owner)
    for i in range(22):
        review(db, subject=f"lesson-{i}")
    db.commit()
    app = FastAPI()
    app.include_router(measurement.router)
    app.dependency_overrides[measurement.require_user] = lambda: {"id": "user"}
    yield db, app, lesson_report.LessonReportTool(workspace)
    db.close()


@pytest.mark.asyncio
async def test_tool_and_http_share_scoped_result_without_writes(report_target):
    db, app, tool = report_target
    before = list(db.iterdump())
    tool_result = json.loads(await tool.execute(_thread_id="owner", scope="project", project="project"))
    with TestClient(app) as client:
        response = client.get("/api/measurement/lessons", params={"project": "project", "agent": "none"})
    assert response.status_code == 200, response.text
    assert response.json() == tool_result
    assert list(db.iterdump()) == before
    assert len(tool_result["rows"]) == 20 and tool_result["known_versions"] == 24
    assert not {"foreign", "orphan"} & {row["id"] for row in tool_result["rows"]}
    assert 'scope="project"' in tool_result["rows"][0]["next_call"]
    second = json.loads(await tool.execute(_thread_id="owner", scope="project", project="project",
                                           cursor=tool_result["next_cursor"]))
    assert len(second["rows"]) == 4 and second["next_cursor"] is None
    wrong_scope = json.loads(await tool.execute(_thread_id="owner", cursor=tool_result["next_cursor"]))
    assert "scope differs" in wrong_scope["error"]
    foreign = db.execute(
        """SELECT n.id,p.owner_id,t.user_id,l.source_event_id FROM plans p
           JOIN plan_nodes n ON n.plan_id=p.id
           JOIN plan_node_artifact_links l ON l.node_id=n.id
           JOIN threads t ON t.id=p.owner_id
           WHERE p.owner_id='foreign' AND l.provenance=?
           ORDER BY n.id,l.created_at,l.artifact_id,l.role LIMIT 1""",
        (lesson_reviews.PROVENANCE,),
    ).fetchone()
    assert foreign is not None
    assert foreign["owner_id"] == "foreign" and foreign["user_id"] == "other-user"
    assert foreign["source_event_id"] == lesson_reviews.lesson_key("foreign", "a" * 64)
    db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (foreign["id"],))
    db.commit()
    foreign_changed = json.loads(await tool.execute(
        _thread_id="owner", scope="project", project="project", cursor=tool_result["next_cursor"],
    ))
    assert "error" not in foreign_changed
    assert foreign_changed["snapshot"] == tool_result["snapshot"]
    assert len(foreign_changed["rows"]) == 4 and foreign_changed["next_cursor"] is None

    visible = next(row for row in tool_result["rows"] if row["id"].startswith("lesson-"))
    selected = db.execute(
        """SELECT n.id,p.owner_id,t.user_id,l.source_event_id FROM plans p
           JOIN plan_nodes n ON n.plan_id=p.id
           JOIN plan_node_artifact_links l ON l.node_id=n.id
           JOIN threads t ON t.id=p.owner_id
           WHERE p.owner_id='owner' AND l.provenance=? AND l.source_event_id=?
           ORDER BY n.id,l.created_at,l.artifact_id,l.role LIMIT 1""",
        (lesson_reviews.PROVENANCE, lesson_reviews.lesson_key(visible["id"], visible["version"])),
    ).fetchone()
    assert selected is not None
    assert selected["owner_id"] == "owner" and selected["user_id"] == "user"
    assert selected["source_event_id"] == lesson_reviews.lesson_key(visible["id"], visible["version"])
    db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (selected["id"],))
    db.commit()
    changed = json.loads(await tool.execute(_thread_id="owner", scope="project", project="project",
                                           cursor=tool_result["next_cursor"]))
    assert "report changed" in changed["error"] and "scope differs" not in changed["error"]
    assert "without cursor" in changed["next"]
    own = json.loads(await tool.execute(_thread_id="owner"))
    assert own["known_versions"] == 23
    assert own["period"]["basis"] == "completed weeks; current week excluded; end exclusive"
    assert not {"child", "foreign", "orphan"} & {row["id"] for row in own["rows"]}
    preview = json.loads(await tool.execute(_thread_id="owner", preview=True))
    assert len(preview["rows"]) == 5


@pytest.mark.asyncio
async def test_adapters_refuse_invalid_identity_scope_and_range(report_target):
    _db, app, tool = report_target
    identity_error = json.loads(await tool.execute(_thread_id="missing"))
    assert "caller identity" in identity_error["error"]
    assert "runtime" in identity_error["next"] and "cursor" not in identity_error["next"]
    assert "range must" in json.loads(await tool.execute(_thread_id="owner", range="bad"))["error"]
    assert "does not accept" in json.loads(await tool.execute(_thread_id="owner", project="project"))["error"]
    with TestClient(app) as client:
        for params, status in (({"project": "project", "agent": "foreign"}, 404),
                               ({"project": "missing", "agent": "none"}, 404),
                               ({"project": "all", "agent": "none", "range": "bad"}, 400)):
            response = client.get("/api/measurement/lessons", params=params)
            assert response.status_code == status, response.text
        empty = client.get("/api/measurement/lessons", params={"project": "all", "agent": "none", "lesson_id": "unknown"})
        assert empty.status_code == 200 and empty.json()["detail"] is None
        assert 'scope="all"' in empty.json()["next_call"]
        assert 'lesson_id="unknown"' in empty.json()["next_call"]


@pytest.mark.asyncio
async def test_reports_leave_same_thread_writer_usable_on_success_and_refusal(report_target, monkeypatch):
    from api import measurement
    from core import lesson_report_access
    from infra.database import get_db

    async def same_thread(operation, *args, **kwargs):
        return operation(*args, **kwargs)

    monkeypatch.setattr(lesson_report_access, "run_bulk", same_thread)
    monkeypatch.setattr(lesson_report_access, "run_off_db", same_thread)
    _db, _app, tool = report_target

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    writer = get_db()
    await tool.execute(_thread_id="owner")
    await tool.execute(_thread_id="missing")
    await measurement.measurement_lessons(
        request=ConnectedRequest(), project="all", agent="none", cursor=None, user={"id": "user"})
    assert writer.execute("PRAGMA query_only").fetchone()[0] == 0
    assert not writer.in_transaction
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE threads SET title='Writer still usable' WHERE id='owner'")
    writer.commit()
    assert writer.execute("SELECT title FROM threads WHERE id='owner'").fetchone()[0] == "Writer still usable"


@pytest.mark.asyncio
async def test_recorded_git_resolution_runs_after_snapshot_closes(report_target, monkeypatch):
    from contextlib import contextmanager
    from core import lesson_report_access, lesson_report_reads
    from core.lesson_report_db import report_snapshot
    from tests.test_lesson_report_reads import read_call, load

    db, _app, tool = report_target
    read_call(db)
    load(db, digest="c" * 64)
    db.commit()
    snapshot_closed = False

    @contextmanager
    def observed_snapshot():
        nonlocal snapshot_closed
        with report_snapshot() as snapshot:
            yield snapshot
        # This runs on the DB worker that created the snapshot; a closed error
        # cannot be confused with SQLite's cross-thread affinity protection.
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            snapshot.execute("SELECT 1")
        snapshot_closed = True

    calls = []

    def git_after_close(reader, record):
        # A slow or timed-out object read must never retain a live SQLite snapshot.
        assert snapshot_closed
        calls.append(record["git_blob_oid"])
        return None, "exact recorded file object unreadable"

    monkeypatch.setattr(lesson_report_access, "report_snapshot", observed_snapshot)
    monkeypatch.setattr(lesson_report_reads, "recorded_source", git_after_close)
    result = json.loads(await tool.execute(_thread_id="owner"))
    assert calls == ["a" * 40]
    assert result["coverage"]["unidentified_body_reads"] == 1


@pytest.mark.asyncio
async def test_report_render_uses_real_off_db_executor_after_closed_collection(report_target, monkeypatch):
    """Both filesystem stages must leave the DB worker after collection returns."""
    from contextlib import contextmanager
    from core import lesson_report_access
    from core.lesson_report_db import report_snapshot

    facts = {"blob_calls": 0}
    original_snapshot = report_snapshot
    original_collect = lesson_report_access.collect_for_identity
    original_sources = lesson_report_access.current_sources
    original_blob_opener = lesson_report_access.recorded_blob_reader

    @contextmanager
    def observed_snapshot():
        with original_snapshot() as snapshot:
            facts["snapshot_thread"] = threading.current_thread().name
            yield snapshot
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            snapshot.execute("SELECT 1")
        facts["snapshot_closed"] = True
        facts["snapshot_close_thread"] = threading.current_thread().name

    def observed_collect(*args, **kwargs):
        collection = original_collect(*args, **kwargs)
        facts["collection_thread"] = threading.current_thread().name
        facts["collection_returned"] = True
        return collection

    def observed_sources(*args):
        assert facts.get("snapshot_closed") and facts.get("collection_returned")
        facts["sources_thread"] = threading.current_thread().name
        return original_sources(*args)

    def observed_blob_opener(*args):
        assert facts.get("snapshot_closed") and facts.get("collection_returned")
        facts["blob_calls"] += 1
        facts["blob_thread"] = threading.current_thread().name
        return original_blob_opener(*args)

    monkeypatch.setattr(lesson_report_access, "report_snapshot", observed_snapshot)
    monkeypatch.setattr(lesson_report_access, "collect_for_identity", observed_collect)
    monkeypatch.setattr(lesson_report_access, "current_sources", observed_sources)
    monkeypatch.setattr(lesson_report_access, "recorded_blob_reader", observed_blob_opener)
    _db, _app, tool = report_target
    result = json.loads(await tool.execute(_thread_id="owner"))

    assert result["scope"] == "self:owner"
    assert facts["snapshot_thread"] == facts["snapshot_close_thread"] == facts["collection_thread"]
    assert facts["snapshot_thread"].startswith("db")
    assert facts["blob_calls"] == 1
    assert facts["sources_thread"] == facts["blob_thread"]
    assert not facts["sources_thread"].startswith("db")


def test_the_fingerprint_frames_any_snapshot_string_or_key_as_data():
    """The deferred digest equals the eager digest for the same text, whatever that text is.

    The offers field used to be found by searching the serialized document for a marker, so a
    preserved value or a key equal to that marker either collided with it or landed after it.
    The framing is structural now, so this asserts byte equality with the canonical encoding of
    the same payload — including the marker's own string as a value, as a key, and as an empty
    offers list — and that an unserializable value still raises the same error on both paths.
    """
    import hashlib

    from core.lesson_report import _canonical
    from core.lesson_report_offers import snapshot_fingerprint

    marker = "\x00lesson-report-period-offers"

    class _Feed:
        def __init__(self, text: bytes) -> None:
            self._text = text

        def feed(self, digest) -> None:
            digest.update(self._text)

    def pair(offers: list, extra: list | None = None):
        period = {"calls": 1, "offers": offers, "unavailable_calls": {marker: [marker]}}
        eager = {"coverage": {marker: "as-a-key"}, "period": period, "reads": [{"turn_id": marker}],
                 "rows": [{"reviews": [{"close_reason": marker}]}, *(extra or [])]}
        deferred = {**eager, "period": {**period, "offers": _Feed(_canonical(offers)[1:-1])}}
        return eager, deferred

    for offers in ([], [{"id": marker, "version": marker, "offered": True}],
                   [{"id": "one", "usage_id": "as-a-value"}]):
        eager, deferred = pair(offers)
        assert snapshot_fingerprint(deferred, _canonical) == hashlib.sha256(_canonical(eager)).hexdigest()

    eager, deferred = pair([{"id": "one"}], extra=[{"unserializable": object()}])
    with pytest.raises(TypeError):
        _canonical(eager)
    with pytest.raises(TypeError):
        snapshot_fingerprint(deferred, _canonical)


def test_report_tool_is_deferred_memory_and_registered(report_target):
    from core.agent import CORSIAAgent, _register_memory_tools
    from tools.catalog import TOOL_CATALOG, tool_pack
    _db, _app, tool = report_target
    agent = CORSIAAgent(tool.workspace)
    _register_memory_tools(agent)
    assert agent.tools.get("lesson_report").name == "lesson_report"
    assert TOOL_CATALOG["lesson_report"].category == "memory"
    assert not TOOL_CATALOG["lesson_report"].core
    assert tool_pack("lesson_report") == "memory"


def test_harness_registry_keeps_cwd_tools_local_but_binds_report_to_selected_workspace(
    report_target, tmp_path, monkeypatch,
):
    """A report source root is process-selected, unlike ordinary cwd-bound tools."""
    from core import agent as agent_module
    from subagents.harness_tool_bridge import build_tool_registry

    _db, _app, direct = report_target
    workspace = direct.workspace
    assert workspace is not None
    project_cwd = workspace / "projects" / "p"
    second_workspace = tmp_path / "second-workspace"
    project_cwd.mkdir(parents=True)
    second_workspace.mkdir()
    escaped_cwd = project_cwd / "escaped"
    escaped_cwd.symlink_to(second_workspace, target_is_directory=True)
    monkeypatch.setattr(agent_module, "WORKSPACE", workspace)

    for cwd in (project_cwd, second_workspace, escaped_cwd):
        registry = build_tool_registry(str(cwd))
        assert registry.get("lesson_report").workspace == workspace
        assert registry.get("read_file").workspace == cwd


@pytest.mark.asyncio
async def test_registered_reports_match_http_selected_workspace_source_coverage(
    report_target, monkeypatch,
):
    """Native and harness reports retain selected workspace, role, and project sources."""
    import subprocess

    from core import agent as agent_module
    from core.agent import CORSIAAgent, _register_memory_tools
    from core.lesson_files import render_trial
    from core.lesson_trials import Trial
    from subagents.harness_tool_bridge import build_tool_registry

    _db, app, direct = report_target
    workspace = direct.workspace
    assert workspace is not None
    project_cwd = workspace / "projects" / "p"
    project_cwd.mkdir(parents=True)

    def write_trial(path, lesson_id):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_trial(
            lesson_id=lesson_id, owner="owner", name=lesson_id,
            description="Source-root parity fixture.", text=f"{lesson_id} body.",
            trial=Trial("hypothesis", "2026-10-01"),
            source_refs=[{"kind": "test", "reference": lesson_id}],
        ))

    workspace_lesson = "11111111-1111-4111-8111-111111111111"
    agent_lesson = "22222222-2222-4222-8222-222222222222"
    project_lesson = "33333333-3333-4333-8333-333333333333"
    write_trial(workspace / "skills" / "workspace-source" / "SKILL.md", workspace_lesson)
    write_trial(workspace / "context" / "roles" / "agent" / "agent-source.md", agent_lesson)
    write_trial(project_cwd / "skills" / "project-source" / "SKILL.md", project_lesson)
    escaped_skill = workspace.parent / "outside" / "skills" / "escaped-source" / "SKILL.md"
    write_trial(escaped_skill, "44444444-4444-4444-8444-444444444444")
    (project_cwd / "skills" / "escaped-source").symlink_to(escaped_skill.parent, target_is_directory=True)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
    monkeypatch.setattr(agent_module, "WORKSPACE", workspace)

    native = CORSIAAgent(project_cwd)
    _register_memory_tools(native)
    native_tool = native.tools.get("lesson_report")
    harness_tool = build_tool_registry(str(project_cwd)).get("lesson_report")

    async def tool_detail(tool, lesson_id):
        return json.loads(await tool.execute(
            _thread_id="owner", scope="project", project="project", lesson_id=lesson_id,
        ))

    expected_scopes = {
        workspace_lesson: {"scope": "workspace", "scope_id": "workspace"},
        agent_lesson: {"scope": "workspace", "scope_id": "workspace", "role": "agent"},
        project_lesson: {"scope": "project", "scope_id": "project"},
    }
    with TestClient(app) as client:
        for lesson_id, source_scope in expected_scopes.items():
            native_result = await tool_detail(native_tool, lesson_id)
            harness_result = await tool_detail(harness_tool, lesson_id)
            response = client.get("/api/measurement/lessons", params={
                "project": "project", "agent": "none", "lesson_id": lesson_id,
            })
            assert response.status_code == 200, response.text
            assert native_result == harness_result == response.json()
            assert native_result["detail"]["current_source"]["source_scope"] == source_scope
            assert native_result["coverage"]["guidance_history"] is None
            assert any("escaped-source" in item["path"] for item in native_result["coverage"]["unavailable_sources"])


def test_composed_render_identifies_recorded_reads_on_one_git_process(report_target, tmp_path, monkeypatch):
    import hashlib
    import subprocess
    from core import lesson_report_access
    from core.guidance_manifest import git_blob_oid
    from core.lesson_files import render_trial, with_review_ref
    from core.lesson_trials import Trial
    from infra.guidance_git import recorded_blob_reader
    from tests.test_lesson_report_inventory import SUBJECT
    from tests.test_lesson_report_reads import read_call

    _db, _app, _tool = report_target
    repo = tmp_path / "recorded-workspace"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)

    def store(data):
        return subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=data,
                              check=True, capture_output=True).stdout.decode().strip()

    lesson = with_review_ref(render_trial(
        lesson_id=SUBJECT, owner="owner", name="deciding", description="Use before deciding.",
        text="The recorded rule.", trial=Trial("hypothesis", "2026-10-01"),
        source_refs=[{"kind": "test", "reference": "full-render"}],
    ), "review").encode()
    unreadable = b"\xff\xfe not utf-8"
    receipts = [
        ("valid", store(lesson), hashlib.sha256(lesson).hexdigest()),
        ("missing", "1" * 40, "0" * 64),
        ("unreadable", store(unreadable), hashlib.sha256(unreadable).hexdigest()),
    ]

    for name, oid, sha in receipts:
        read_call(_db, call_id=f"call-{name}", path=f"/skills/{name}/SKILL.md")
        _db.execute(
            "INSERT INTO guidance_loads(id,thread_id,turn_id,path,raw_sha256,git_blob_oid,"
            "delivered_sha256,byte_count,partial,created_at) VALUES(?,'owner','turn',?,?,?,?,100,0,'2026-09-21')",
            (f"load-{name}", f"/skills/{name}/SKILL.md", sha, oid, "b" * 64))
    _db.commit()

    def render(reader_factory, **options):
        monkeypatch.setattr(lesson_report_access, "recorded_blob_reader", reader_factory)
        collection = lesson_report_access.collect_for_identity(
            caller_thread_id="owner", workspace=repo, scope="project", project="project", **options)
        return lesson_report_access.render_collection(collection, repo)

    real_factory = recorded_blob_reader
    created = []

    def batched_factory(workspace):
        reader, reason = real_factory(workspace)
        created.append(reader)
        return reader, reason

    batched = render(batched_factory)

    reasons = {read["path"].split("/")[2]: read["identity_reason"] for read in batched["unidentified_reads"]}
    assert reasons == {
        "missing": "exact recorded file object unavailable or exceeds guidance limit",
        "unreadable": "exact recorded file object unreadable",
    }
    assert [reader.spawned for reader in created] == [1] and created[0]._proc is None
    assert batched["coverage"]["period_body_reads"] == 3 and batched["coverage"]["unidentified_body_reads"] == 2


@pytest.mark.asyncio
async def test_http_pages_summarize_only_their_slice_and_match_the_tool(report_target, monkeypatch):
    from core import lesson_report
    _db, app, tool = report_target
    summarized = []
    original = lesson_report._summary
    monkeypatch.setattr(lesson_report, "_summary", lambda row, *rest: summarized.append(row["id"]) or original(row, *rest))
    with TestClient(app) as client:
        first = client.get("/api/measurement/lessons", params={"project": "project", "agent": "none"})
        assert first.status_code == 200, first.text
        cursor = first.json()["next_cursor"]
        assert len(summarized) == len(first.json()["rows"]) == 20
        summarized.clear()
        second = client.get("/api/measurement/lessons", params={"project": "project", "agent": "none", "cursor": cursor})
    assert second.status_code == 200, second.text
    assert len(second.json()["rows"]) == 4 and summarized == [row["id"] for row in second.json()["rows"]]
    tool_second = json.loads(await tool.execute(_thread_id="owner", scope="project", project="project", cursor=cursor))
    assert second.json() == tool_second


def test_collection_cancellation_stops_after_first_count_chunk_and_closes_snapshot(report_target, monkeypatch, tmp_path):
    """Cancellation between aggregate chunks must release the read snapshot immediately."""
    from core import lesson_report_access, lesson_report_offers
    from core.lesson_report_db import report_snapshot

    _db, _app, _tool = report_target
    cancellation = threading.Event()
    facts = {"count_chunks": 0, "text_chunks": 0, "snapshot_closed": False}
    original_snapshot = report_snapshot

    @contextmanager
    def observed_snapshot(*, cancellation_event=None):
        assert cancellation_event is cancellation
        try:
            with original_snapshot(cancellation_event=cancellation_event) as snapshot:
                yield snapshot
        finally:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                snapshot.execute("SELECT 1")
            facts["snapshot_closed"] = True

    class CancelAfterFirstChunk:
        def __iter__(self):
            yield {"id": "lesson", "version": "a" * 64, "n": 1, "calls": 1,
                   "tasks": 0, "attempts": 0, "calls_without_task": 1}
            cancellation.set()

    def count_chunks(_db, sql, *_args, **_kwargs):
        assert sql == lesson_report_offers._COUNTS
        facts["count_chunks"] += 1
        yield CancelAfterFirstChunk()
        pytest.fail("a second aggregate count chunk started after cancellation")

    def text_chunks(*_args, **_kwargs):
        facts["text_chunks"] += 1
        pytest.fail("canonical offer text began after cancellation")

    monkeypatch.setattr(lesson_report_access, "report_snapshot", observed_snapshot)
    monkeypatch.setattr(lesson_report_offers, "json_row_chunk_lists", count_chunks)
    monkeypatch.setattr(lesson_report_offers, "json_text_chunks", text_chunks)

    with pytest.raises(lesson_report_offers.LessonReportCollectionCancelled):
        lesson_report_access.collect_for_identity(
            user_id="user", workspace=tmp_path, scope="all", project="all", agent="none",
            cancellation_event=cancellation,
        )

    assert facts == {"count_chunks": 1, "text_chunks": 0, "snapshot_closed": True}


def test_collection_cancellation_stops_after_first_canonical_text_chunk(monkeypatch):
    from core import lesson_report_offers

    cancellation = threading.Event()
    chunks = []

    def count_chunks(*_args, **_kwargs):
        return iter(())

    class CancelAfterFirstText(str):
        def __getitem__(self, index):
            value = super().__getitem__(index)
            cancellation.set()
            return value

    def text_chunks(*_args, **_kwargs):
        chunks.append("first")
        yield CancelAfterFirstText("[{\"id\":\"lesson\"}]")
        pytest.fail("a second canonical text chunk started after cancellation")

    monkeypatch.setattr(lesson_report_offers, "json_row_chunk_lists", count_chunks)
    monkeypatch.setattr(lesson_report_offers, "json_text_chunks", text_chunks)
    offers = lesson_report_offers.DeferredOffers([], [], "user", "from", "to")

    with pytest.raises(lesson_report_offers.LessonReportCollectionCancelled):
        offers.collect(object(), cancellation_event=cancellation)

    assert chunks == ["first"] and len(offers._chunks) == 1


def test_private_snapshot_interrupts_early_collection_and_closes_connection(report_target, monkeypatch, tmp_path):
    """An abandoned request interrupts real SQLite work before deferred offers begin."""
    from core import lesson_report_access, lesson_report_offers

    _db, _app, _tool = report_target
    cancellation = threading.Event()
    barrier = threading.Barrier(2)
    entered = threading.Event()
    released = threading.Event()
    facts = {}

    def blocked_source_inputs(snapshot, *_args):
        facts["snapshot"] = snapshot

        def pause(value):
            if not entered.is_set():
                entered.set()
                barrier.wait(timeout=5)
                released.set()
            return value

        snapshot.create_function("pause", 1, pause)
        snapshot.execute(
            "WITH RECURSIVE numbers(value) AS (VALUES(1) UNION ALL "
            "SELECT value + 1 FROM numbers WHERE value < 100000) "
            "SELECT sum(pause(value)) FROM numbers"
        ).fetchone()
        pytest.fail("early SQLite work finished after cancellation")

    monkeypatch.setattr(lesson_report_access, "source_inputs", blocked_source_inputs)

    def collect():
        try:
            lesson_report_access.collect_for_identity(
                user_id="user", workspace=tmp_path, scope="all", project="all", agent="none",
                cancellation_event=cancellation,
            )
        except BaseException as error:
            facts["error"] = error
        finally:
            try:
                facts["snapshot"].execute("SELECT 1")
            except sqlite3.ProgrammingError as error:
                facts["closed"] = str(error)

    worker = threading.Thread(target=collect)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        cancellation.set()
        barrier.wait(timeout=5)
        assert released.wait(timeout=5)
    finally:
        if not released.is_set():
            barrier.abort()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert isinstance(facts["error"], lesson_report_offers.LessonReportCollectionCancelled)
    assert "closed" in facts


def test_private_snapshot_allows_unset_or_absent_cancellation_event(report_target, tmp_path):
    from core import lesson_report_access

    _db, _app, _tool = report_target
    unset = threading.Event()
    with_event = lesson_report_access.collect_for_identity(
        user_id="user", workspace=tmp_path, scope="all", project="all", agent="none",
        cancellation_event=unset,
    )
    without_event = lesson_report_access.collect_for_identity(
        user_id="user", workspace=tmp_path, scope="all", project="all", agent="none",
    )

    assert not unset.is_set()
    assert with_event.scope == without_event.scope


def test_private_snapshot_preserves_unrelated_operational_error_after_cancellation(report_target):
    from core.lesson_report_db import report_snapshot

    _db, _app, _tool = report_target
    cancellation = threading.Event()

    def cancel_during_authorization(*_args):
        cancellation.set()
        return sqlite3.SQLITE_OK

    snapshot = None
    with pytest.raises(sqlite3.OperationalError, match="no such table") as raised:
        with report_snapshot(cancellation_event=cancellation) as snapshot:
            snapshot.set_authorizer(cancel_during_authorization)
            snapshot.execute("SELECT * FROM report_snapshot_missing_table")

    assert raised.value.sqlite_errorcode != sqlite3.SQLITE_INTERRUPT
    assert snapshot is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        snapshot.execute("SELECT 1")


@pytest.mark.asyncio
async def test_http_disconnect_before_snapshot_returns_no_content_without_render(report_target, monkeypatch):
    from api import measurement
    from core import lesson_report_access

    _db, app, _tool = report_target
    original_report = measurement.report_for_identity

    async def cancelled_report(**kwargs):
        kwargs["cancellation_event"].set()
        return await original_report(**kwargs)

    monkeypatch.setattr(measurement, "report_for_identity", cancelled_report)
    monkeypatch.setattr(lesson_report_access, "render_collection", lambda *_args: pytest.fail("render ran"))
    with TestClient(app) as client:
        response = client.get("/api/measurement/lessons", params={"project": "all", "agent": "none"})

    assert response.status_code == 204
    assert response.content == b""


@pytest.mark.asyncio
async def test_disconnect_monitor_sets_the_collection_cancellation_event():
    from api import measurement

    class DisconnectedRequest:
        async def is_disconnected(self):
            return True

    cancellation = threading.Event()
    await measurement._monitor_client_disconnect(DisconnectedRequest(), cancellation)
    assert cancellation.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("success", "error", "cancellation", "disconnect"))
async def test_measurement_lessons_joins_disconnect_monitor_for_every_outcome(monkeypatch, outcome):
    from api import measurement
    from core.lesson_report_offers import LessonReportCollectionCancelled

    started, joined = asyncio.Event(), asyncio.Event()
    facts = {}

    async def monitor(_request, cancellation):
        facts["monitor_event"] = cancellation
        started.set()
        try:
            await asyncio.Future()
        finally:
            joined.set()

    async def report(**kwargs):
        facts["report_event"] = kwargs["cancellation_event"]
        await started.wait()
        if outcome == "error":
            raise RuntimeError("report failed")
        if outcome == "cancellation":
            raise asyncio.CancelledError
        if outcome == "disconnect":
            raise LessonReportCollectionCancelled
        return {"ok": True}

    monkeypatch.setattr(measurement, "_monitor_client_disconnect", monitor, raising=False)
    monkeypatch.setattr(measurement, "report_for_identity", report)

    args = dict(request=object(), project="all", agent="none", cursor=None, user={"id": "user"})
    if outcome == "success":
        assert await measurement.measurement_lessons(**args) == {"ok": True}
    elif outcome == "error":
        with pytest.raises(RuntimeError, match="report failed"):
            await measurement.measurement_lessons(**args)
    elif outcome == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            await measurement.measurement_lessons(**args)
    else:
        response = await measurement.measurement_lessons(**args)
        assert response.status_code == 204
        assert response.body == b""

    assert facts["monitor_event"] is facts["report_event"]
    assert joined.is_set()
