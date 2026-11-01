"""Outside tools send declared measure events over OTLP/HTTP to the real loopback listener."""
from __future__ import annotations

import asyncio
import gzip
import json
import socket
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2 as service
from opentelemetry.proto.common.v1 import common_pb2 as common

from infra import database, otlp_state
from infra import measure_keys
from infra.database import get_db
from infra.measure_registry import REGISTRY, sync
from infra.measure_status import status
from infra.otlp_listener import PORT_ENV, OtlpListener
from tests.event_routing_helpers import mk_session
from tools.measure_keys import MeasureKeyTool

OWNER, OTHER = "otlp-owner", "otlp-other"
PROTOBUF = "application/x-protobuf"
DECLARATION = """schema: corsia.measures/1
namespace: qa
events:
  qa.rec:
    attributes: {code: id, n: count}
    dedupe: [code]
  qa.route:
    attributes: {code: id}
    dedupe: [code]
    use: route
  qa.both:
    attributes: {code: id}
    dedupe: [code]
    use: both
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest_asyncio.fixture
async def world(isolated_db, tmp_path: Path, monkeypatch):
    REGISTRY.reset()
    otlp_state.REFUSALS.reset()
    otlp_state.CAPS.__init__()
    mk_session(OWNER, project_id="project-a")
    mk_session(OTHER, project_id="project-a")
    folder = tmp_path / ".threads" / OWNER
    folder.mkdir(parents=True)
    path = folder / "measures.yaml"
    path.write_text(DECLARATION)
    db = get_db()
    sync(db, owner_thread_id=OWNER, path=str(path), text=DECLARATION)
    db.commit()
    db.close()
    port = _free_port()
    monkeypatch.setenv(PORT_ENV, str(port))
    listener = OtlpListener()
    assert await listener.start()
    _, key = measure_keys.mint(OWNER, "test")
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        yield client, key, port
    await listener.stop()


def _attr(key: str, value) -> common.KeyValue:
    pair = common.KeyValue(key=key)
    if isinstance(value, int):
        pair.value.int_value = value
    elif isinstance(value, float):
        pair.value.double_value = value
    else:
        pair.value.string_value = value
    return pair


def request_of(*records: dict, resource: dict | None = None) -> service.ExportLogsServiceRequest:
    request = service.ExportLogsServiceRequest()
    block = request.resource_logs.add()
    for key, value in (resource or {}).items():
        block.resource.attributes.append(_attr(key, value))
    scope = block.scope_logs.add()
    for attributes in records:
        record = scope.log_records.add()
        for key, value in attributes.items():
            record.attributes.append(_attr(key, value))
    return request


def send(client, key, request, *, headers=None, content=None):
    merged = {"Authorization": f"Bearer {key}", "Content-Type": PROTOBUF, **(headers or {})}
    return client.post("/v1/logs", content=request.SerializeToString() if content is None else content, headers=merged)


def rejected(response: httpx.Response) -> tuple[int, str]:
    reply = service.ExportLogsServiceResponse()
    reply.ParseFromString(response.content)
    return reply.partial_success.rejected_log_records, reply.partial_success.error_message


def own_status() -> dict:
    db = get_db()
    try:
        return status(db, OWNER)
    finally:
        db.close()


def rows(name: str) -> int:
    return own_status()["events"].get(name, {}).get("today", {}).get("rows", 0)


@pytest.mark.asyncio
async def test_declared_record_lands_as_the_keys_agent(world) -> None:
    client, key, _ = world
    response = await send(client, key, request_of({"event.name": "qa.rec", "code": "a1", "n": 3}))
    assert response.status_code == 200 and rejected(response) == (0, "")
    assert rows("qa.rec") == 1
    db = get_db()
    stamped = db.execute("SELECT agent_thread_id, source, provenance, project_id FROM outcome_events WHERE event_name='qa.rec'").fetchone()
    db.close()
    assert tuple(stamped) == (OWNER, "otlp", "agent_recorded", "project-a")


@pytest.mark.asyncio
async def test_json_and_gzip_bodies_are_accepted(world) -> None:
    client, key, _ = world
    request = request_of({"event.name": "qa.rec", "code": "j1", "n": 1}, {"event.name": "qa.rec", "code": "j2", "n": 2})
    from google.protobuf import json_format
    plain = json_format.MessageToJson(request).encode()
    first = await send(client, key, request, headers={"Content-Type": "application/json"}, content=plain)
    second = await send(client, key, request_of({"event.name": "qa.rec", "code": "z1", "n": 1}),
                        headers={"Content-Encoding": "gzip"}, content=gzip.compress(request_of({"event.name": "qa.rec", "code": "z1", "n": 1}).SerializeToString()))
    assert first.status_code == 200 and second.status_code == 200
    assert json.loads(first.content) == {} and rows("qa.rec") == 3


@pytest.mark.asyncio
async def test_undeclared_name_is_refused_with_a_reason_and_other_records_land(world) -> None:
    client, key, _ = world
    response = await send(client, key, request_of({"event.name": "qa.nope", "code": "x"}, {"event.name": "qa.rec", "code": "ok", "n": 1}))
    count, text = rejected(response)
    assert response.status_code == 200 and count == 1 and "qa.nope" in text
    assert rows("qa.rec") == 1


@pytest.mark.asyncio
async def test_record_without_event_name_is_refused(world) -> None:
    client, key, _ = world
    count, text = rejected(await send(client, key, request_of({"body": "plain log line"})))
    assert count == 1 and "event.name" in text


@pytest.mark.asyncio
async def test_claiming_another_identity_is_refused_and_counted(world) -> None:
    client, key, _ = world
    response = await send(client, key, request_of({"event.name": "qa.rec", "code": "m1", "n": 1, "corsia.agent_thread_id": OTHER}))
    count, text = rejected(response)
    assert count == 1 and "corsia.agent_thread_id" in text
    assert rows("qa.rec") == 0
    assert own_status()["events"]["qa.rec"]["today"]["counters"]["identity_mismatch"] == 1
    resource_claim = await send(client, key, request_of({"event.name": "qa.rec", "code": "m2", "n": 1}, resource={"corsia.project_id": "project-b"}))
    assert rejected(resource_claim)[0] == 1


@pytest.mark.asyncio
async def test_missing_malformed_or_revoked_key_is_401_and_writes_nothing(world) -> None:
    client, key, _ = world
    body = request_of({"event.name": "qa.rec", "code": "k1", "n": 1})
    assert (await client.post("/v1/logs", content=body.SerializeToString(), headers={"Content-Type": PROTOBUF})).status_code == 401
    assert (await send(client, "mlk_short", body)).status_code == 401
    assert (await send(client, "mlk_" + "A" * 43, body)).status_code == 401
    key_id = measure_keys.list_keys(get_db(), OWNER)[0]["key_id"]
    assert measure_keys.revoke(OWNER, key_id)
    assert (await send(client, key, body)).status_code == 401
    assert rows("qa.rec") == 0


@pytest.mark.asyncio
async def test_request_cap_answers_429_with_retry_after(world, monkeypatch) -> None:
    client, key, _ = world
    monkeypatch.setattr(otlp_state, "REQUESTS_PER_MINUTE", 2)
    body = request_of({"event.name": "qa.rec", "code": "c1", "n": 1})
    assert [(await send(client, key, body)).status_code for _ in range(2)] == [200, 200]
    refused = await send(client, key, body)
    assert refused.status_code == 429 and int(refused.headers["Retry-After"]) >= 1
    assert own_status()["request_refusals"] == {"rate_limited": 1}


@pytest.mark.asyncio
async def test_record_cap_and_batch_size(world, monkeypatch) -> None:
    client, key, _ = world
    many = request_of(*({"event.name": "qa.rec", "code": f"b{i}", "n": 1} for i in range(501)))
    assert (await send(client, key, many)).status_code == 413
    monkeypatch.setattr(otlp_state, "RECORDS_PER_MINUTE", 2)
    three = request_of(*({"event.name": "qa.rec", "code": f"d{i}", "n": 1} for i in range(3)))
    assert (await send(client, key, three)).status_code == 429
    assert rows("qa.rec") == 0


@pytest.mark.asyncio
async def test_drain_answers_503_before_reading(world, monkeypatch) -> None:
    client, key, _ = world
    monkeypatch.setattr("infra.otlp_receiver.is_draining", lambda: True)
    response = await send(client, key, request_of({"event.name": "qa.rec", "code": "r1", "n": 1}))
    assert response.status_code == 503 and response.headers["Retry-After"]
    assert rows("qa.rec") == 0


@pytest.mark.asyncio
async def test_unsupported_type_and_encoding_are_415(world) -> None:
    client, key, _ = world
    body = request_of({"event.name": "qa.rec", "code": "t1", "n": 1})
    assert (await send(client, key, body, headers={"Content-Type": "text/plain"})).status_code == 415
    assert (await send(client, key, body, headers={"Content-Encoding": "br"})).status_code == 415


async def _raw(port: int, head: str) -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode())
    await writer.drain()
    status_line = await asyncio.wait_for(reader.readline(), 5)
    writer.close()
    return int(status_line.split(b" ")[1])


@pytest.mark.asyncio
async def test_length_is_required_and_capped_before_the_body_is_read(world) -> None:
    _, key, port = world
    common_head = f"POST /v1/logs HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {key}\r\nContent-Type: {PROTOBUF}\r\n"
    assert await _raw(port, common_head + "Transfer-Encoding: chunked\r\n\r\n") == 411
    assert await _raw(port, common_head + f"Content-Length: {2 * 1024 * 1024}\r\n\r\n") == 413


@pytest.mark.asyncio
async def test_inflated_body_over_four_mib_is_413(world) -> None:
    client, key, _ = world
    bomb = gzip.compress(b"\0" * (5 * 1024 * 1024))
    assert len(bomb) < 1024 * 1024
    assert (await send(client, key, request_of(), headers={"Content-Encoding": "gzip"}, content=bomb)).status_code == 413


@pytest.mark.asyncio
async def test_garbage_body_is_400(world) -> None:
    client, key, _ = world
    assert (await send(client, key, request_of(), content=b"\xff\xff\xff not protobuf")).status_code == 400


@pytest.mark.asyncio
async def test_route_and_both_types_go_through_event_intake(world) -> None:
    client, key, _ = world
    response = await send(client, key, request_of({"event.name": "qa.route", "code": "r1"}, {"event.name": "qa.both", "code": "b1"}))
    assert response.status_code == 200 and rejected(response) == (0, "")
    db = get_db()
    received = db.execute("SELECT event_type, count(*) FROM event_intake_records GROUP BY event_type").fetchall()
    db.close()
    assert {row[0] for row in received} >= {"qa.route", "qa.both"}
    assert rows("qa.both") == 1 and rows("qa.route") == 0


@pytest.mark.asyncio
async def test_route_records_differing_only_by_subject_are_two_events(world) -> None:
    client, key, _ = world
    response = await send(client, key, request_of({"event.name": "qa.route", "code": "r1", "corsia.subject": "alpha"},
                                                  {"event.name": "qa.route", "code": "r1", "corsia.subject": "beta"}))
    assert response.status_code == 200 and rejected(response) == (0, "")
    db = get_db()
    received = db.execute("SELECT count(*) FROM event_intake_records WHERE event_type='qa.route'").fetchone()[0]
    db.close()
    assert received == 2


def test_a_record_with_no_event_time_takes_its_observed_time() -> None:
    from infra import otlp_decode

    request = request_of({"event.name": "qa.rec", "code": "o1", "n": 1})
    request.resource_logs[0].scope_logs[0].log_records[0].observed_time_unix_nano = 1_700_000_000 * 10**9
    assert otlp_decode.records(request)[0].ts.startswith("2023-11-14T22:13:20")


@pytest.mark.asyncio
async def test_main_app_has_no_otlp_route() -> None:
    from main import app

    assert "/v1/logs" not in {getattr(route, "path", None) for route in app.routes}


@pytest.mark.asyncio
async def test_get_and_unknown_paths(world) -> None:
    client, key, _ = world
    assert (await client.get("/v1/logs")).status_code == 405
    assert (await client.post("/v1/metrics", content=b"")).status_code == 404


@pytest.mark.asyncio
async def test_measure_key_tool_mints_once_lists_and_revokes(world) -> None:
    client, _, port = world
    minted = json.loads(await MeasureKeyTool().execute(_thread_id=OWNER, action="mint", label="ci"))
    assert minted["endpoint"] == f"http://127.0.0.1:{port}" and minted["key"].startswith("mlk_")
    assert (await send(client, minted["key"], request_of({"event.name": "qa.rec", "code": "tk", "n": 1}))).status_code == 200
    listing = await MeasureKeyTool().execute(_thread_id=OWNER, action="list")
    assert minted["key"] not in listing and "ci" in listing
    assert json.loads(await MeasureKeyTool().execute(_thread_id=OWNER, action="revoke", key_id=minted["key_id"])) == {"revoked": minted["key_id"]}
    assert (await send(client, minted["key"], request_of({"event.name": "qa.rec", "code": "tk2", "n": 1}))).status_code == 401
    assert (await MeasureKeyTool().execute(_thread_id=OTHER, action="revoke", key_id=minted["key_id"])).startswith("Error")


@pytest.mark.asyncio
async def test_mint_refuses_while_not_listening(isolated_db) -> None:
    otlp_state.set_port(None)
    mk_session(OWNER, project_id="project-a")
    assert (await MeasureKeyTool().execute(_thread_id=OWNER, action="mint", label="x")).startswith("Error: the measure receiver is not listening")


@pytest.mark.asyncio
async def test_deleting_the_thread_deletes_its_measure_keys(world, monkeypatch) -> None:
    from api import thread_mutations

    async def _quiesce(_thread_id, *, user_id):
        return {}

    monkeypatch.setattr(thread_mutations, "authenticate", lambda _credential: {"id": "user-1"})
    monkeypatch.setattr(thread_mutations.runtime_boundary, "quiesce_thread_runtimes", _quiesce)
    client, measure_key, _ = world
    assert (await send(client, measure_key, request_of({"event.name": "qa.rec", "code": "d1", "n": 1}))).status_code == 200
    await thread_mutations.delete_thread(OWNER, "session")
    db = get_db()
    try:
        assert measure_keys.resolve(db, measure_key) is None
        assert measure_keys.list_keys(db, OWNER) == []
    finally:
        db.close()
    assert (await send(client, measure_key, request_of({"event.name": "qa.rec", "code": "d2", "n": 1}))).status_code == 401


@pytest.mark.asyncio
async def test_workflow_version_is_the_servers_and_a_different_claim_is_refused(world, monkeypatch) -> None:
    import infra.otlp_admission as admission

    monkeypatch.setattr(admission, "workflow_stamps", lambda agent, records: {"wf": ("wf", "v-server")})
    client, measure_key, _ = world
    base = {"event.name": "qa.rec", "n": 1, "corsia.workflow.ref": "wf"}
    count, text = rejected(await send(client, measure_key, request_of({**base, "code": "w1", "corsia.workflow.version": "v-forged"})))
    assert count == 1 and "corsia.workflow.version" in text
    assert own_status()["events"]["qa.rec"]["today"]["counters"]["identity_mismatch"] == 1
    assert rows("qa.rec") == 0
    same = await send(client, measure_key, request_of({**base, "code": "w2", "corsia.workflow.version": "v-server"}, {**base, "code": "w3"}))
    assert rejected(same) == (0, "")
    assert rows("qa.rec") == 2


@pytest.mark.asyncio
async def test_run_id_that_is_not_a_plan_node_is_the_producers_label(world) -> None:
    client, measure_key, _ = world
    response = await send(client, measure_key, request_of({"event.name": "qa.rec", "code": "r1", "n": 1, "corsia.run_id": "ci-1234"}))
    assert rejected(response) == (0, "") and rows("qa.rec") == 1


@pytest.mark.asyncio
async def test_a_numeric_run_id_is_kept_and_a_stamp_of_the_wrong_shape_is_refused(world) -> None:
    client, measure_key, _ = world
    base = {"event.name": "qa.rec", "n": 1}
    assert rejected(await send(client, measure_key, request_of({**base, "code": "k1", "corsia.run_id": 4321}))) == (0, "")
    db = get_db()
    stored = [json.loads(row[0]).get("run_id") for row in db.execute("SELECT attributes FROM outcome_events WHERE event_name='qa.rec'")]
    db.close()
    assert stored == ["4321"]
    count, text = rejected(await send(client, measure_key, request_of(
        {**base, "code": "k2", "corsia.run_id": 1.5},
        {**base, "code": "k3", "corsia.workflow.version": "v1"},
        {**base, "code": "k4", "corsia.workflow.ref": 7})))
    assert count == 3 and rows("qa.rec") == 1
    assert "corsia.run_id" in text and "corsia.workflow.version" in text and "corsia.workflow.ref" in text


@pytest.mark.asyncio
async def test_an_array_map_or_bytes_value_is_refused_as_a_stamp_and_noted_as_an_attribute(world) -> None:
    client, measure_key, _ = world
    request = request_of(*({"event.name": "qa.rec", "code": f"s{i}", "n": 1} for i in range(4)))
    records = request.resource_logs[0].scope_logs[0].log_records
    records[0].attributes.add(key="corsia.run_id").value.array_value.values.add(string_value="r1")
    records[1].attributes.add(key="corsia.workflow.ref").value.kvlist_value.values.add(key="a").value.string_value = "b"
    records[2].attributes.add(key="corsia.workflow.version").value.bytes_value = b"v1"
    records[3].attributes.add(key="n").value.array_value.values.add(int_value=2)
    del records[3].attributes[2]  # the scalar n, so only the array value is sent
    count, text = rejected(await send(client, measure_key, request))
    assert count == 3 and "corsia.run_id" in text and "corsia.workflow.ref" in text
    assert rows("qa.rec") == 1
    assert any('attribute "n"' in note for note in own_status()["recent_refusals"])


@pytest.mark.asyncio
async def test_a_plan_node_run_id_stamps_its_workflow_and_another_agents_node_is_refused(world) -> None:
    from tests.plan_writer_authorizer import raw_plan_seed

    mk_session("otlp-worker", spawned_by=OWNER, project_id="project-a")
    db = get_db()
    with raw_plan_seed(db):
        for plan, owner in (("p-own", OWNER), ("p-worker", "otlp-worker"), ("p-other", OTHER)):
            db.execute("INSERT INTO plans(id,owner_kind,owner_id,created_at,updated_at) VALUES (?,'thread',?,'2026-01-01','2026-01-01')", (plan, owner))
            db.execute("INSERT INTO plan_nodes(id,plan_id,kind,title,acceptance_criteria,order_key,commitment_id,"
                       "workflow_ref,workflow_version,created_at,updated_at) "
                       "VALUES (?,?,'task','Task','done',1,?,'wf',?,'2026-01-01','2026-01-01')",
                       (f"n-{plan}", plan, f"n-{plan}", f"v-{plan}"))
    db.commit()
    client, measure_key, _ = world
    base = {"event.name": "qa.rec", "n": 1}
    accepted = await send(client, measure_key, request_of(
        {**base, "code": "o1", "corsia.run_id": "n-p-own"}, {**base, "code": "o2", "corsia.run_id": "n-p-worker"}))
    assert rejected(accepted) == (0, "")
    stored = sorted(json.loads(row[0])["workflow_version"] for row in db.execute(
        "SELECT attributes FROM outcome_events WHERE event_name='qa.rec'"))
    assert stored == ["v-p-own", "v-p-worker"]
    count, text = rejected(await send(client, measure_key, request_of({**base, "code": "o3", "corsia.run_id": "n-p-other"})))
    assert count == 1 and "corsia.run_id" in text and rows("qa.rec") == 2
    forged = await send(client, measure_key, request_of(
        {**base, "code": "o4", "corsia.run_id": "n-p-own", "corsia.workflow.version": "v-forged"},
        {**base, "code": "o5", "corsia.run_id": "n-p-own", "corsia.workflow.ref": "other-wf"}))
    count, text = rejected(forged)
    db.close()
    assert count == 2 and "corsia.workflow.version" in text and "corsia.workflow.ref" in text and rows("qa.rec") == 2


@pytest.mark.asyncio
async def test_a_bad_subject_is_refused_and_other_corsia_attributes_reach_admission(world) -> None:
    client, measure_key, _ = world
    count, text = rejected(await send(client, measure_key, request_of({"event.name": "qa.rec", "code": "s1", "n": 1, "corsia.subject": "no spaces allowed"})))
    assert count == 1 and "corsia.subject" in text and rows("qa.rec") == 0
    kept = await send(client, measure_key, request_of({"event.name": "qa.rec", "code": "s2", "n": 1, "corsia.extra": "x"}))
    assert rejected(kept) == (0, "") and rows("qa.rec") == 1
    assert any("corsia.extra" in note for note in REGISTRY.refusals[OWNER])


@pytest.mark.asyncio
async def test_a_sender_stalled_mid_body_does_not_hold_the_write_slot(world) -> None:
    client, measure_key, port = world
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write((f"POST /v1/logs HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {measure_key}\r\n"
                  f"Content-Type: {PROTOBUF}\r\nContent-Length: 100\r\n\r\n").encode() + b"0123456789")
    await writer.drain()
    await asyncio.sleep(0.1)
    response = await send(client, measure_key, request_of({"event.name": "qa.rec", "code": "t1", "n": 1}))
    writer.close()
    assert response.status_code == 200 and rows("qa.rec") == 1


@pytest.mark.asyncio
async def test_a_busy_port_falls_back_and_no_free_port_leaves_the_listener_off(monkeypatch) -> None:
    import infra.otlp_listener as listener_module

    monkeypatch.delenv(PORT_ENV, raising=False)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        free = _free_port()
        monkeypatch.setattr(listener_module, "DEFAULT_PORT", busy.getsockname()[1])
        monkeypatch.setattr(listener_module, "FALLBACK_PORTS", (free,))
        bound = listener_module._prebind()
        assert bound is not None and bound.getsockname()[1] == free
        bound.close()
        monkeypatch.setattr(listener_module, "FALLBACK_PORTS", ())
        assert await OtlpListener().start() is False


def test_a_collector_on_every_address_keeps_its_port(monkeypatch) -> None:
    import infra.otlp_listener as listener_module

    monkeypatch.delenv(PORT_ENV, raising=False)
    with socket.socket() as collector:
        collector.bind(("0.0.0.0", 0))  # macOS lets SO_REUSEADDR bind loopback over this and take its traffic
        collector.listen()
        free = _free_port()
        monkeypatch.setattr(listener_module, "DEFAULT_PORT", collector.getsockname()[1])
        monkeypatch.setattr(listener_module, "FALLBACK_PORTS", (free,))
        bound = listener_module._prebind()
        assert bound is not None and bound.getsockname()[1] == free
        bound.close()


@pytest.mark.asyncio
async def test_list_returns_the_current_endpoint(world) -> None:
    _, _, port = world
    listing = json.loads(await MeasureKeyTool().execute(_thread_id=OWNER, action="list"))
    assert listing["endpoint"] == f"http://127.0.0.1:{port}"
    minted = json.loads(await MeasureKeyTool().execute(_thread_id=OWNER, action="mint", label="ci"))
    assert "next free one" in minted["send"] and "action list" in minted["send"]


def test_the_largest_wire_time_decodes_to_a_real_timestamp() -> None:
    from infra import otlp_decode

    assert otlp_decode._time(2**64 - 1).startswith("2554-07-21T")


@pytest.mark.asyncio
async def test_mint_and_an_accepted_request_each_emit_one_product_event(world, monkeypatch) -> None:
    import infra.otlp_receiver as receiver
    import tools.measure_keys as tool

    seen: list[tuple] = []
    monkeypatch.setattr(receiver, "emit_product_event", lambda category, action, **kw: seen.append((category, action, kw)))
    monkeypatch.setattr(tool, "emit_product_event", lambda category, action, **kw: seen.append((category, action, kw)))
    client, measure_key, _ = world
    minted = json.loads(await MeasureKeyTool().execute(_thread_id=OWNER, action="mint", label="ci"))
    await send(client, measure_key, request_of({"event.name": "qa.rec", "code": "p1", "n": 1}, {"event.name": "qa.nope", "code": "p2"}))
    assert [(c, a) for c, a, _ in seen] == [("measure_key", "minted"), ("otlp_intake", "request_accepted")]
    assert seen[1][2]["properties"] == {"records": 2, "rejected": 1}
    assert minted["key"] not in repr(seen)
