"""Limits of the urgent push design, run at the nearest case (design prototype)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from infra.corsia_agent import set_corsia_agent
from infra.conductor import ensure_project_conductor, set_project_conductor
from infra.database import get_db
from infra.push_guard import (
    PushRefused, TITLE_MAX, clean, outcome_for_status, tap_path, valid_tap_path, validate_endpoint,
)
from subagents.child_thread import create_child_thread
from tests.registry_helpers import make_session
from tools import push

PUBLIC = ["203.0.113.9"]  # a documentation address standing in for a public one: the check is on the name first
APPLE = "https://web.push.apple.com/QAbc123"
GOOGLE = "https://fcm.googleapis.com/fcm/send/abc"


def public(host: str) -> list[str]:
    return ["8.8.8.8"]


# ---- R21: where the install may send ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    APPLE, GOOGLE, "https://updates.push.services.mozilla.com/wpush/v2/x", "https://wns2-par02p.notify.windows.com/w/?token=x",
    "https://FCM.GOOGLEAPIS.COM./fcm/send/abc",
])
def test_known_hosts_are_accepted(url):
    assert validate_endpoint(url, public) == url


@pytest.mark.parametrize("url", [
    "https://fcm.googleapis.com.evil.example/fcm/send/abc",     # lookalike: the known name is only a prefix
    "https://evilfcm.googleapis.com.example/x",
    "https://notfcm.googleapis.com@evil.example/x",
    "https://fcm.googleapis.com@evil.example/x",                # userinfo smuggles another host
    "https://evil@fcm.googleapis.com/x",                         # userinfo on a known host
    "https://xn--fcm-googleapis-9w8d.com/x",                     # punycode lookalike
    "https://fcm.googIeapis.com/x",                              # capital I for l
    "http://fcm.googleapis.com/x",                               # not https
    "https://fcm.googleapis.com:8443/x",                         # another port
    "https://10.0.0.5/x", "https://127.0.0.1/x", "https://[::1]/x", "https://169.254.169.254/latest/meta-data",
    "https://localhost/x", "https://192.168.1.10:443/x", "ftp://fcm.googleapis.com/x", "", "https://",
    "https://fcm.googleapis.com/x y", "https://fcm.googleapis.com/x\n",
    "https://fcm.googleapis.com\\@evil.example/x",
])
def test_unknown_or_unsafe_endpoints_are_refused(url):
    with pytest.raises(PushRefused):
        validate_endpoint(url, public)


@pytest.mark.parametrize("address", ["10.1.2.3", "127.0.0.1", "169.254.169.254", "::1", "fe80::1", "192.168.0.7", "0.0.0.0"])
def test_a_known_name_that_resolves_to_a_private_address_is_refused(address):
    with pytest.raises(PushRefused):
        validate_endpoint(GOOGLE, lambda host: [address])
    with pytest.raises(PushRefused):
        validate_endpoint(GOOGLE, lambda host: ["8.8.8.8", address])  # one bad address among good ones


def test_a_name_that_does_not_resolve_is_refused():
    def fail(host):
        raise OSError("no such host")
    with pytest.raises(PushRefused):
        validate_endpoint(GOOGLE, fail)


def test_a_redirect_is_not_followed_and_counts_as_failed():
    """A known host that answers 302 to a private address: one request, no follow, outcome failed."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False, timeout=10.0) as client:
            return await client.post(validate_endpoint(GOOGLE, public), content=b"x")

    response = asyncio.run(go())
    assert seen == [GOOGLE], "the redirect target was requested"
    assert outcome_for_status(response.status_code) == "failed"


def test_the_send_client_settings_are_the_guard_settings():
    from infra.push_guard import SEND_TIMEOUT_SECONDS
    assert SEND_TIMEOUT_SECONDS == 10.0


@pytest.mark.parametrize("status,outcome", [(201, "sent"), (200, "sent"), (404, "gone"), (410, "gone"), (400, "gone"), (403, "gone"),
                                            (401, "failed"), (413, "failed"), (429, "failed"), (500, "failed"), (503, "failed"), (302, "failed")])
def test_r22_only_the_four_codes_turn_a_device_off(status, outcome):
    assert outcome_for_status(status) == outcome


# ---- R20: where a tap may open ---------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "//evil.example", "/\\evil.example", "/\t/evil.example", "/\n/evil.example", "https://evil.example", "/%2F%2Fevil.example",
    "/.//evil.example", "/?activity=x&next=//evil.example", "/?activity=", "/?activity=../x", "/?activity=x/../../y", "\\\\evil.example",
    "javascript:alert(1)", "/x", "/?other=1", "", None, 5,
])
def test_a_tap_path_off_the_activity_route_is_refused(path):
    assert valid_tap_path(path) is False


def test_the_tap_path_is_built_from_an_item_id_not_from_caller_text():
    assert tap_path("01HZX-9_a.b~c") == "/?activity=01HZX-9_a.b~c"
    assert valid_tap_path(tap_path("01HZX"))
    for bad in ("//evil.example", "/\\evil.example", "a/b", "a b", "a?b", ""):
        with pytest.raises(PushRefused):
            tap_path(bad)


# ---- R19: what a push may say ----------------------------------------------------------------------------------

def test_a_title_of_40_passes_and_41_is_refused():
    assert clean("T" * 40, TITLE_MAX, "title") == "T" * 40
    with pytest.raises(PushRefused):
        clean("T" * 41, TITLE_MAX, "title")


def test_control_characters_are_stripped_and_counted_after_stripping():
    assert clean("A\x00B\x07C\x1b[31mD", 40, "title") == "ABC[31mD"          # controls dropped, the rest kept
    assert clean("Line\none\ttwo\r\nthree", 40, "title") == "Line one two three"  # whitespace becomes one space
    assert clean("‮evil​", 40, "title") == "evil"                    # bidi override and zero-width dropped
    assert clean("T" * 40 + "\x00", 40, "title") == "T" * 40                   # the stripped control isn't counted
    for empty in ("", "\x00\x07", "  \n "):
        with pytest.raises(PushRefused):
            clean(empty, 40, "title")


# ---- R1: one sender --------------------------------------------------------------------------------------------

@pytest.fixture
def actors(isolated_db):
    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('alpha','Alpha','t','t')")
        db.commit()
        corsia = make_session(db, prefix="corsia")
        conductor = make_session(db, prefix="conductor", project_id="alpha")
        ordinary = make_session(db, prefix="ordinary")
        worker = create_child_thread(user_id="steve", parent_thread_id=corsia, title="worker", model="native/test", db=db)
        assert set_corsia_agent(corsia, db=db)
        assert set_project_conductor("alpha", conductor, db=db)
        return {"corsia": corsia, "conductor": conductor, "ordinary": ordinary, "worker": worker}
    finally:
        db.close()


@pytest.fixture
def hooks():
    log: list[dict] = []
    calls: list[tuple] = []

    async def deliver(device, title, line, path):
        calls.append((device["id"], title, line, path))
        return "sent"

    push.HOOKS = push.PushHooks(is_here=lambda: False, item_open=lambda thread, item: True, devices=lambda: [{"id": "d1", "endpoint": GOOGLE}], deliver=deliver, log=log.append)
    yield log, calls
    push.HOOKS = push.PushHooks()


ARGS = {"item_id": "item-1", "title": "Something needs you", "line": "A decision can't wait.", "reason": "Deadline today."}


def call(thread_id, **extra):
    return asyncio.run(push.UrgentPushTool().execute(_thread_id=thread_id, **{**ARGS, **extra}))


def test_only_the_corsia_agent_sends(actors, hooks):
    log, calls = hooks
    assert call(actors["corsia"]).startswith("sent:")
    for other in ("worker", "conductor", "ordinary"):
        assert call(actors[other]) == push.REFUSED, other
    assert call(None) == push.REFUSED
    assert call("not-a-thread") == push.REFUSED
    assert len(calls) == 1, "a refused caller reached the sender"
    assert [entry["outcome"] for entry in log] == ["sent", "refused", "refused", "refused", "refused", "refused"]


def test_a_refused_caller_learns_nothing_about_presence_or_devices(actors, hooks):
    push.HOOKS.is_here = lambda: True
    assert call(actors["worker"]) == push.REFUSED
    assert call(actors["corsia"]) == push.HELD


def test_a_fork_of_corsias_own_thread_is_not_corsia(actors):
    """A fork or side run runs under CORSIA's thread id, so the sender check alone would admit it. The guard is the
    fork kinds' fixed tool lists: the tool is in none of them, and a new kind must list it to get it."""
    from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
    from core.checkpoint_fork_kind import CHECKPOINT_KIND
    assert "urgent_push" not in ARTIFACT_REVIEW_KIND.tool_allowlist
    assert "urgent_push" not in CHECKPOINT_KIND.tool_allowlist
    # The native fork refuses any call outside the list with the kind's own refusal text (core/fork_native_tools.py:249).
    from tools.catalog import TOOL_CATALOG
    assert "urgent_push" in TOOL_CATALOG
    assert not {"urgent_push"} & (ARTIFACT_REVIEW_KIND.tool_allowlist | CHECKPOINT_KIND.tool_allowlist)


def test_results_other_than_sent(actors, hooks):
    log, calls = hooks
    push.HOOKS.is_here = lambda: True
    assert call(actors["corsia"]) == push.HELD
    push.HOOKS.is_here = lambda: False
    push.HOOKS.devices = lambda: []
    assert call(actors["corsia"]) == push.NO_DEVICE
    push.HOOKS.devices = lambda: [{"id": "d1", "endpoint": "https://10.0.0.5/x"}]
    assert call(actors["corsia"]) == push.FAILED
    assert calls == []


def test_a_gone_device_is_turned_off_and_any_other_answer_leaves_it_on(actors, hooks):
    log, calls = hooks
    off: list[str] = []

    async def answer(status):
        return outcome_for_status(status)

    results = {"d1": 410, "d2": 500}

    async def deliver(device, title, line, path):
        return outcome_for_status(results[device["id"]])

    push.HOOKS = push.PushHooks(
        is_here=lambda: False, item_open=lambda thread, item: True,
        devices=lambda: [{"id": "d1", "endpoint": GOOGLE}, {"id": "d2", "endpoint": APPLE}],
        deliver=deliver, turn_off=lambda device: off.append(device["id"]), log=log.append,
    )
    assert call(actors["corsia"]) == push.FAILED
    assert off == ["d1"], "J1: only the R22 code turns the device Off; the 500 leaves d2 On"


def test_bad_input_sends_nothing(actors, hooks):
    log, calls = hooks
    for bad in ({"title": "T" * 41}, {"line": "L" * 121}, {"item_id": "//evil.example"}, {"item_id": "/\\evil.example"}, {"title": "\x00"}):
        assert call(actors["corsia"], **bad).startswith("Error: Nothing sent."), bad
    assert calls == []


# ---- Design round 5 -----------------------------------------------------------------------------------------------

def test_every_outcome_writes_one_row_with_the_reason(actors, hooks):
    """R3: sent, held, no_device, failed and declined each leave exactly one row carrying the reason."""
    log, calls = hooks
    reason = {"reason": "Deadline is tonight."}
    assert call(actors["corsia"], **reason).startswith("sent:")
    push.HOOKS.is_here = lambda: True
    assert call(actors["corsia"], **reason) == push.HELD
    push.HOOKS.is_here = lambda: False
    push.HOOKS.devices = lambda: []
    assert call(actors["corsia"], **reason) == push.NO_DEVICE
    push.HOOKS.devices = lambda: [{"id": "d1", "endpoint": "https://10.0.0.5/x"}]
    assert call(actors["corsia"], **reason) == push.FAILED
    assert call(actors["corsia"], decline=True, **reason) == push.DECLINED
    assert [(row["outcome"], row["reason"]) for row in log] == [(o, "Deadline is tonight.") for o in ("sent", "held", "no_device", "failed", "declined")]


def test_a_decline_sends_nothing_and_needs_no_title_or_line(actors, hooks):
    log, calls = hooks
    result = asyncio.run(push.UrgentPushTool().execute(_thread_id=actors["corsia"], item_id="item-1", reason="It can wait until morning.", decline=True))
    assert result == push.DECLINED
    assert calls == []
    assert [(row["outcome"], row["title"], row["reason"]) for row in log] == [("declined", None, "It can wait until morning.")]
    # Without decline, title and line are still required, and a missing reason is refused either way.
    missing = asyncio.run(push.UrgentPushTool().execute(_thread_id=actors["corsia"], item_id="item-1", reason="x"))
    assert missing.startswith("Error: Nothing sent.")
    no_reason = asyncio.run(push.UrgentPushTool().execute(_thread_id=actors["corsia"], item_id="item-1", decline=True))
    assert no_reason.startswith("Error: Nothing sent.")
    assert len(log) == 1


def test_a_decline_by_another_agent_is_refused_and_sends_nothing(actors, hooks):
    log, calls = hooks
    result = asyncio.run(push.UrgentPushTool().execute(_thread_id=actors["worker"], item_id="item-1", reason="x", decline=True))
    assert result == push.REFUSED and calls == []


def test_held_is_the_same_each_time_and_the_tool_keeps_no_hold_count(actors, hooks):
    """R5: every `held` says arm one plan watch; the tool counts nothing, so CORSIA's own judgement ends the loop."""
    log, calls = hooks
    push.HOOKS.is_here = lambda: True
    results = [call(actors["corsia"]) for _ in range(3)]
    assert results == [push.HELD] * 3 and "Arm one plan watch" in push.HELD
    assert [row["outcome"] for row in log] == ["held"] * 3
    assert not hasattr(push, "HELD_AGAIN") and not hasattr(push.PushHooks, "rows")
    assert "held again" not in push.UrgentPushTool().description


def test_the_six_results_and_no_other(actors, hooks):
    """R10: sent, held, no_device, failed, refused, declined; the description names exactly these."""
    log, calls = hooks
    outcomes = []
    outcomes.append(call(actors["corsia"]).split(":")[0])
    push.HOOKS.is_here = lambda: True
    outcomes.append(call(actors["corsia"]).split(":")[0])
    push.HOOKS.is_here = lambda: False
    push.HOOKS.devices = lambda: []
    outcomes.append(call(actors["corsia"]).split(":")[0])
    push.HOOKS.devices = lambda: [{"id": "d1", "endpoint": "https://10.0.0.5/x"}]
    outcomes.append(call(actors["corsia"]).split(":")[0])
    outcomes.append(call(actors["worker"]).split(":")[0])
    outcomes.append(call(actors["corsia"], decline=True).split(":")[0])
    assert outcomes == ["sent", "held", "no_device", "failed", "refused", "declined"]
    assert "sent, held, no_device, failed, refused or declined" in push.UrgentPushTool().description


# ---- Design round 6: item_id is checked before any send ------------------------------------------------------------

def test_an_unknown_or_closed_item_sends_nothing_and_logs_nothing_as_sent(actors, hooks):
    log, calls = hooks
    open_items = {"item-1"}
    push.HOOKS.item_open = lambda thread, item: item in open_items
    assert call(actors["corsia"]).startswith("sent:")
    for bad in ("never-existed", "answered-yesterday"):
        result = call(actors["corsia"], item_id=bad)
        assert result.startswith("Error: Nothing sent.") and "not open in the person's Activity" in result, bad
    open_items.clear()  # the item was answered: it is closed now
    assert call(actors["corsia"]).startswith("Error: Nothing sent.")
    assert len(calls) == 1 and [row["outcome"] for row in log] == ["sent"]


def test_a_decline_does_not_need_an_open_item(actors, hooks):
    log, calls = hooks
    push.HOOKS.item_open = lambda thread, item: False
    assert call(actors["corsia"], decline=True) == push.DECLINED


def test_the_default_item_check_reads_activitys_own_queue(actors, monkeypatch):
    """The default hook asks the queue Activity's list reads (`activity_queue`, view=activity) for the item's external id."""
    import api.attention as attention
    seen = {}

    def queue(db, user_id, *, project_id=None, view=None):
        seen.update(user_id=user_id, project_id=project_id, view=view)
        return [{"external_id": "dec-1", "kind": "decision"}, {"external_id": "node-7", "kind": "task"}], None, "workspace"

    monkeypatch.setattr(attention, "activity_queue", queue)
    assert push._open_in_activity(actors["corsia"], "node-7") is True
    assert push._open_in_activity(actors["corsia"], "gone") is False
    assert seen["view"] == "activity" and seen["project_id"] is None


# ---- Design round 6: the app-wide here signal (R4) ----------------------------------------------------------------

@pytest.fixture
def here():
    from infra import here_signal
    here_signal._connections.clear()
    now = [1000.0]
    here_signal.clock = lambda: now[0]
    yield here_signal, now
    here_signal._connections.clear()
    import time
    here_signal.clock = time.monotonic


def test_a_here_message_over_the_socket_writes_no_presence_row_and_no_seen_mark(here, isolated_db):
    """The real receive loop: a `here` frame reaches the signal, never `handle_presence`, and writes nothing.
    Seen marks are columns of `thread_presence` rows, so no row means no mark."""
    from fastapi import WebSocketDisconnect
    import ws as ws_module
    signal, now = here
    frames = [{"type": "here", "state": "here"}, {"type": "here", "state": "away", "hidden": True}, {"type": "here", "state": "here"}]

    class Socket:
        async def receive_json(self):
            if not frames:
                raise WebSocketDisconnect()
            return frames.pop(0)

    reached = []

    async def forbidden(*args, **kwargs):
        reached.append(args)

    db = get_db()
    try:
        tables = ("thread_presence", "presence_visits")
        before = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        original = ws_module._handle_presence_impl
        ws_module._handle_presence_impl = forbidden
        try:
            asyncio.run(ws_module._receive_frames(Socket(), {"id": "steve"}, asyncio.Queue()))
        finally:
            ws_module._handle_presence_impl = original
        after = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        db.close()
    assert before == after == {"thread_presence": 0, "presence_visits": 0}
    assert reached == [], "a here frame went down the thread-presence path"
    assert signal.is_here("steve")


def test_two_connections_one_here_reads_here_and_a_closed_one_reads_away(here):
    signal, now = here
    signal.apply("ws:phone", "steve", {"state": "away"})
    signal.apply("ws:laptop", "steve", {"state": "here"})
    assert signal.is_here("steve")
    signal.drop("ws:laptop")
    assert not signal.is_here("steve"), "a closed connection stopped counting"
    signal.apply("ws:phone", "steve", {"state": "here"})
    assert signal.is_here("steve") and not signal.is_here("someone-else")


def test_a_hidden_page_is_here_for_the_thirty_second_grace_then_away(here):
    signal, now = here
    signal.apply("ws:1", "steve", {"state": "here"})
    signal.apply("ws:1", "steve", {"state": "away", "hidden": True})
    now[0] += 29
    assert signal.is_here()
    now[0] += 2
    assert not signal.is_here()
    signal.apply("ws:1", "steve", {"state": "here"})
    assert signal.is_here(), "the page came back"


def test_a_malformed_message_changes_nothing(here):
    signal, now = here
    signal.apply("ws:1", "steve", {"state": "here"})
    for bad in ({}, {"state": "maybe"}, {"state": None}):
        signal.apply("ws:1", "steve", bad)
    assert signal.is_here()


def test_the_default_hook_reads_the_signal_and_the_socket_close_drops_it(here, actors):
    signal, now = here
    assert push.PushHooks().is_here() is False
    signal.apply("ws:1", "steve", {"state": "here"})
    assert push.PushHooks().is_here() is True
    from ws import thread_control
    from infra.ws_session import session_id

    class Socket: pass
    socket = Socket()
    signal.apply(session_id(socket), "steve", {"state": "here"})
    signal.drop("ws:1")
    assert signal.is_here()
    thread_control.here_signal.drop(session_id(socket))
    assert not signal.is_here()


def test_a_deliver_that_raises_the_endpoint_returns_failed_and_leaks_nothing(actors, hooks, caplog):
    import logging
    log, calls = hooks
    secret_endpoint = "https://fcm.googleapis.com/fcm/send/SECRET-TOKEN-123"
    spans: list[str] = []

    async def boom(device, title, line, path):
        raise RuntimeError(f"connect failed for {secret_endpoint}")

    push.HOOKS.devices = lambda: [{"id": "d1", "endpoint": secret_endpoint}]
    push.HOOKS.deliver = boom
    with caplog.at_level(logging.DEBUG):
        result = call(actors["corsia"])
    assert result == push.FAILED
    assert "SECRET-TOKEN" not in result and "fcm.googleapis.com" not in result
    assert "SECRET-TOKEN" not in caplog.text
    assert all("SECRET-TOKEN" not in str(row) for row in log)
    assert [row["outcome"] for row in log] == ["failed"]


def test_one_device_failing_does_not_stop_the_others(actors, hooks):
    log, calls = hooks

    async def deliver(device, title, line, path):
        if device["id"] == "d1":
            raise ValueError("boom")
        return "sent"

    push.HOOKS.devices = lambda: [{"id": "d1", "endpoint": GOOGLE}, {"id": "d2", "endpoint": APPLE}]
    push.HOOKS.deliver = deliver
    assert call(actors["corsia"]) == push.sent(1)


def test_r13_a_row_that_is_not_on_keeps_only_five_fields():
    from infra.push_guard import clear_secrets
    row = {"id": "d1", "endpoint_hash": "ab12", "name": "iPhone", "added_at": "2026-11-24T10:00:00Z", "state": "on",
           "endpoint": GOOGLE, "p256dh": "k1", "auth": "k2"}
    for state in ("stopped", "off"):  # the R22 update, Turn off or Remove, key regeneration
        cleared = clear_secrets(row, state)
        assert cleared == {"id": "d1", "endpoint_hash": "ab12", "name": "iPhone", "added_at": "2026-11-24T10:00:00Z",
                           "state": state, "endpoint": None, "p256dh": None, "auth": None}
    with pytest.raises(ValueError):
        clear_secrets(row, "on")


def test_the_vapid_sub_is_one_fixed_value_that_matches_the_shipped_client_uri():
    import json
    from pathlib import Path
    from infra.push_guard import VAPID_SUB, vapid_claims
    meta = json.loads((Path(__file__).resolve().parents[2] / "docs" / "client-metadata" / "corsia-client.json").read_text())
    assert VAPID_SUB == meta["client_uri"] == "https://github.com/cebertowicz/corsia"
    for endpoint in (GOOGLE, APPLE):
        claims = vapid_claims(endpoint, 1)
        assert claims["sub"] == VAPID_SUB and "@" not in claims["sub"] and "mailto" not in claims["sub"]


def test_pinning_verifies_the_certificate_against_the_host_name_not_the_address(tmp_path):
    """A resolved address whose certificate is not the host's is refused; the same address with the right name connects."""
    import datetime, ssl, threading
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from infra.push_guard import connect_pinned

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "attacker.example")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("attacker.example")]), False).sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")

    async def go():
        async def handle(reader, writer):
            writer.close()
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
        port = server.sockets[0].getsockname()[1]
        trusting = ssl.create_default_context(cafile=str(tmp_path / "c.pem"))  # trusts the certificate itself
        try:
            # The right name: connects (proves the refusal below is the name check, not a broken server).
            reader, writer = await connect_pinned("attacker.example", "127.0.0.1", trusting, port)
            writer.close()
            # The delivery company's name at the attacker's address: refused.
            with pytest.raises(PushRefused):
                await connect_pinned("web.push.apple.com", "127.0.0.1", trusting, port)
            # Switching the checks off is refused before any connection.
            loose = ssl.create_default_context()
            loose.check_hostname = False
            loose.verify_mode = ssl.CERT_NONE
            with pytest.raises(PushRefused):
                await connect_pinned("web.push.apple.com", "127.0.0.1", loose, port)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(go())
